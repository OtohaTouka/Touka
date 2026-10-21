"""群聊白名單設定路由 (Group Settings Router)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.group_router import drop_legacy_sections
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager

router = APIRouter(prefix="/group", tags=["Group Chat Settings"])


class GroupWhitelist(BaseModel):
    """群聊白名單資料模型（群聊只限靈魂，只有 ToukaSoul 一區）."""

    # SOLID - SRP: 嚴格定義資料結構，防止不法注入
    model_config = ConfigDict(extra="forbid")

    ToukaSoul: dict[str, str] = Field(default_factory=dict)


def _get_whitelist_path() -> Path:
    """動態獲取配置目錄，以支援測試沙盒 (SSOT)."""
    return Path(get_config_dir()) / "group_whitelist.json"


def _align_ids_with_ssot(data: GroupWhitelist) -> dict:
    """SSOT Alignment: 自動修正舊格式 (如 ToukaSoulAlice -> Alice) (SOLID - SRP)."""
    dump = data.model_dump()
    new_soul = {}
    for key, val in dump.get("ToukaSoul", {}).items():
        if isinstance(val, str) and val.startswith("ToukaSoul") and len(val) > 9:
            new_soul[key] = val.replace("ToukaSoul", "")
        else:
            new_soul[key] = val
    dump["ToukaSoul"] = new_soul
    return dump


@router.get("/whitelist", response_model=GroupWhitelist)
async def get_whitelist(
    identity: AgentIdentity | None = Depends(get_verified_identity),  # noqa: B008
) -> GroupWhitelist:
    """獲取群聊白名單.

    Grandfather Batch-2 fix: added explicit None identity check to prevent
    anonymous access (previously had Depends but did not check for None identity).
    """
    if not identity:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    path = _get_whitelist_path()
    try:
        data = resource_manager.read_json(path)
        if data:
            # ADR-0245：舊 Native／Custom 區讀到就略過並記警告，不改檔
            return GroupWhitelist(**drop_legacy_sections(data))
        return GroupWhitelist()
    except Exception as e:
        raise ToukaException(ErrorCode.CONFIG_LOAD_FAILED, error_output=str(e)) from e


@router.post("/whitelist")
async def update_whitelist(
    data: GroupWhitelist,
    identity: AgentIdentity | None = Depends(get_verified_identity),  # noqa: B008
) -> dict:
    """更新群聊白名單 (僅限 Owner)."""
    if not identity:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    # RBAC 權限校驗 (SOLID - Gate 2)：人類 admin（users.json 角色）與 RBAC Owner 皆可管理群聊白名單
    # （擁有者決策；對齊 admin.py require_admin 的人類角色檢查）。
    if not identity.is_owner_or_admin():
        raise ToukaException(ErrorCode.AUTH_OWNER_REQUIRED)

    path = _get_whitelist_path()
    aligned_dump = _align_ids_with_ssot(data)

    try:
        resource_manager.make_dir(Path(path).parent)
        resource_manager.write_json(path, aligned_dump, indent=2)
    except Exception as e:
        raise ToukaException(ErrorCode.CONFIG_SAVE_FAILED, error_output=str(e)) from e
    else:
        return {"message": "Group whitelist updated and aligned with SSOT."}
