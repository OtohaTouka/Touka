"""Setup Router — First-Run Setup Wizard endpoints.

Provides:
  GET  /admin/setup/status  — returns current setup state (admin only)
  POST /admin/setup/apply   — writes config values to .env (admin only)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from src.core.exceptions import ErrorCode, ToukaException
from src.routers.admin import require_admin
from src.services import embedding_service
from src.services.setup_service import SetupService, SetupStatus
from src.utils.task_utils import spawn_logged

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

router = APIRouter(prefix="/admin/setup", tags=["setup"])
logger = logging.getLogger(__name__)


def get_setup_service() -> SetupService:
    """Dependency for SetupService."""
    return SetupService()


class SetupApplyRequest(BaseModel):
    """Request body for POST /admin/setup/apply."""

    dispatch_token: str | None = None
    auth_secret: str | None = None
    data_path: str | None = None
    workspace_root: str | None = None
    embedding_model: str | None = None

    @field_validator(
        "dispatch_token", "auth_secret", "data_path", "workspace_root", "embedding_model"
    )
    @classmethod
    def _reject_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            msg = "must not be empty or whitespace-only"
            raise ValueError(msg)
        return v


@router.get("/status", response_model=SetupStatus)
async def get_setup_status(
    _admin: AgentIdentity = Depends(require_admin),  # noqa: B008
    svc: SetupService = Depends(get_setup_service),  # noqa: B008
) -> SetupStatus:
    """Return current first-run setup status. Admin only."""
    return svc.get_status()


@router.post("/apply")
async def apply_setup(
    body: SetupApplyRequest,
    _admin: AgentIdentity = Depends(require_admin),  # noqa: B008
    svc: SetupService = Depends(get_setup_service),  # noqa: B008
) -> dict[str, str]:
    """Write provided setup values to .env. Admin only."""
    try:
        svc.apply(
            dispatch_token=body.dispatch_token,
            auth_secret=body.auth_secret,
            data_path=body.data_path,
            workspace_root=body.workspace_root,
            embedding_model=body.embedding_model,
        )
    except ValueError as e:
        # 宿主目錄驗證失敗（原生模式路徑不存在等）→ 乾淨的在地化 422，避免未捕捉例外變 500。
        raise ToukaException(ErrorCode.VALIDATION_ERROR, detail_override=str(e)) from e
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 嵌入模型：立即下載（背景＋輪詢進度）＋ 切換防呆（bug①）
# ---------------------------------------------------------------------------


class EmbeddingModelRequest(BaseModel):
    """觸發嵌入模型下載的請求。"""

    model: str


class EmbeddingSwitchRequest(BaseModel):
    """切換嵌入模型的請求；force=True 表示使用者已確認既有記憶失效風險。"""

    model: str
    force: bool = False


@router.post("/embedding/download")
async def download_embedding_model(
    body: EmbeddingModelRequest,
    _admin: AgentIdentity = Depends(require_admin),  # noqa: B008
) -> dict:
    """觸發嵌入模型下載（背景執行、additive）。回目前狀態；已快取/下載中則不重啟（防呆）。"""
    state, should_spawn = embedding_service.begin_download(body.model)
    if should_spawn:
        spawn_logged(
            asyncio.to_thread(embedding_service.perform_download, body.model),
            name=f"embedding-download:{body.model}",
        )
    return state


@router.get("/embedding/download-status")
async def embedding_download_status(
    model: str,
    _admin: AgentIdentity = Depends(require_admin),  # noqa: B008
) -> dict:
    """輪詢某嵌入模型的下載進度。"""
    return embedding_service.get_download_state(model)


@router.post("/embedding/switch")
async def switch_embedding_model(
    body: EmbeddingSwitchRequest,
    _admin: AgentIdentity = Depends(require_admin),  # noqa: B008
) -> dict:
    """切換嵌入模型。偵測到既有記憶且換不同模型時，未 force 則回 confirmation_required（不寫入）。

    force=True（使用者已確認）才寫入 EMBEDDING_MODEL 並重置向量單例；**絕不自動 re-embed**。
    """
    evaluation = embedding_service.evaluate_switch(body.model)
    if evaluation["requires_confirmation"] and not body.force:
        return {"status": "confirmation_required", **evaluation}
    embedding_service.apply_switch(body.model)
    return {"status": "ok", **evaluation}
