"""技能管理路由 (Skills Router)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends

if TYPE_CHECKING:
    from pathlib import Path
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.authorization_gateway import require_owner_or_admin, require_privileged
from src.services.resource_manager import resource_manager
from src.services.skill_catalog_service import SkillCatalogService
from src.services.skill_service import skill_service

router = APIRouter(prefix="/skills", tags=["skills"])

# Require Owner/Debugger/admin to read or modify skills.
_require_skills_role = require_privileged


# Require Owner/admin to sync from external skill sources.
_require_owner_role = require_owner_or_admin


class SkillModel(BaseModel):
    """技能資料模型。"""

    id: str
    name: str
    description: str
    system_prompt: str
    required_tools: list[str] = []
    mcp_servers: list[str] = []


class SkillInstallRequest(BaseModel):
    """從 URL 下載 .skill 安裝的請求模型。"""

    url: str


@router.get("/list")
async def get_skills(
    include_system: bool = True,
    _identity: AgentIdentity = Depends(_require_skills_role),  # noqa: B008
) -> dict:
    """列出所有技能。

    P4（ADR-0161）：``include_system=false`` 排除 is_system 系統技能（三個 dispatcher／
    coordinator）——聊天技能選單 SSOT 過濾入口，避免內部調度器外洩成使用者可勾選項。
    預設 True＝設定頁 SkillsTab 等既有消費者行為不變（零破壞）。
    """
    skills = skill_service.list_skills()
    if not include_system:
        skills = [s for s in skills if not s.get("is_system")]
    return {"skills": skills}


@router.get("/{skill_id}/export")
async def export_skill(
    skill_id: str,
    _identity: AgentIdentity = Depends(_require_skills_role),  # noqa: B008
) -> FileResponse:
    """打包技能目錄為 .skill 檔下載。需 Owner/Debugger/admin。"""
    path = skill_service.package_skill(skill_id)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"{skill_id}.skill",
        background=BackgroundTask(resource_manager.remove_file, path),
    )


@router.post("/install")
async def install_skill(
    request: SkillInstallRequest,
    _identity: AgentIdentity = Depends(_require_owner_role),  # noqa: B008
) -> dict:
    """從受信任 URL 下載 .skill 並安裝。需 Owner/admin 權限。"""
    skill_id = await skill_service.install_skill_from_url(request.url)
    return {"status": "success", "skill_id": skill_id}


class SkillCatalogInstallRequest(BaseModel):
    """從策展目錄安裝真技能的請求模型。"""

    catalog_id: str


def get_skill_catalog_path() -> Path:
    """獲取 Skill Catalog 清單檔路徑 (SSOT)。"""
    return get_config_dir() / "skill_catalog.json"


@router.get("/catalog")
async def get_skill_catalog(
    _identity: AgentIdentity = Depends(_require_skills_role),  # noqa: B008
) -> dict:
    """回傳真技能策展目錄（從 skill_catalog.json 讀取，fallback 內建 .example）。"""
    svc = SkillCatalogService(catalog_path=str(get_skill_catalog_path()))
    return {"catalog": svc.get_catalog()}


@router.post("/catalog/install")
async def install_catalog_skill(
    request: SkillCatalogInstallRequest,
    _identity: AgentIdentity = Depends(_require_owner_role),  # noqa: B008
) -> dict:
    """從策展目錄安裝真技能（逐檔釘 commit raw 抓取）。需 Owner/admin 權限。"""
    skill_id = await skill_service.install_skill_from_catalog(request.catalog_id)
    return {"status": "success", "skill_id": skill_id}


@router.get("/{skill_id}")
async def get_skill(
    skill_id: str,
    _identity: AgentIdentity = Depends(_require_skills_role),  # noqa: B008
) -> dict:
    """取得指定技能。"""
    skill = skill_service.get_skill(skill_id)
    if not skill:
        raise ToukaException(ErrorCode.RESOURCE_NOT_FOUND, resource_type="Skill")
    return skill


@router.post("")
async def update_skill(
    skill: SkillModel,
    _identity: AgentIdentity = Depends(_require_skills_role),  # noqa: B008
) -> dict:
    """新增或更新技能。"""
    skill_service.update_skill(skill.id, skill.model_dump())
    return {"status": "success", "skill_id": skill.id}


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: str,
    _identity: AgentIdentity = Depends(_require_skills_role),  # noqa: B008
) -> dict:
    """刪除技能。"""
    skill_service.delete_skill(skill_id)
    return {"status": "success"}
