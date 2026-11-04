"""Hook 管理路由（ADR-0292）：只給人類管理員讀寫 ``config/hooks.json`` 與看執行紀錄.

- 讀取回傳目前生效的設定；設定檔壞掉時是上一份好的，``health`` 標 ``error``。
- 存檔要帶載入時的 revision（蓋掉別人的修改回 409）；所有 command hook 先過路徑界線（422）。
- 代理人碰不到這裡：閘門是人類 admin（admin 代打不算），寫入另查 CSRF Origin。
- 例外：``/hooks/active`` 給任何已登入身分查「有沒有會收到對話內容的 hook」，無痕對話要提示；
  只回布林值，不透露設定。
"""

import logging
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from src.models.hooks import (
    MATCHER_EVENTS,
    WIRED_EVENTS,
    HookHealthOut,
    HookRunOut,
    HookRunsResponse,
    HooksActiveResponse,
    HooksConfigResponse,
    HooksSaveRequest,
    HooksSaveResponse,
)
from src.models.identity import AgentIdentity
from src.services.authorization_gateway import require_admin_human, require_authenticated
from src.services.hooks.lifecycle import notify_config_change
from src.services.hooks.service import hook_service
from src.utils.csrf_guard import verify_origin
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hooks", tags=["hooks"])
_require_authenticated = require_authenticated


@router.get("", response_model=HooksConfigResponse)
async def get_hooks_config(
    _admin: Annotated[AgentIdentity, Depends(require_admin_human)],
) -> HooksConfigResponse:
    """目前生效的 hook 設定、設定檔狀態與可用的事件。"""
    config = hook_service.current()
    health = hook_service.health()
    return HooksConfigResponse(
        config=config,
        health=HookHealthOut(status=health.status, revision=health.revision, detail=health.detail),
        wired_events=list(WIRED_EVENTS),
        matcher_events=[event for event in WIRED_EVENTS if event in MATCHER_EVENTS],
    )


@router.put("", response_model=HooksSaveResponse)
async def save_hooks_config(
    payload: HooksSaveRequest,
    request: Request,
    admin: Annotated[AgentIdentity, Depends(require_admin_human)],
) -> HooksSaveResponse:
    """存整份設定；``author``／``updated_at`` 由伺服器填。"""
    verify_origin(request)
    config = payload.config.model_copy(
        update={"author": admin.agent_id, "updated_at": get_current_epoch()}
    )
    revision = await hook_service.save(config, payload.revision)
    logger.info("hook config saved by %s (enabled=%s)", admin.agent_id, config.enabled)
    notify_config_change("hooks")
    return HooksSaveResponse(revision=revision)


@router.get("/active", response_model=HooksActiveResponse)
async def hooks_active(
    _identity: Annotated[AgentIdentity, Depends(_require_authenticated)],
) -> HooksActiveResponse:
    """有沒有啟用中的 hook 會收到對話內容。"""
    return HooksActiveResponse(active=hook_service.has_conversation_hooks())


@router.get("/runs", response_model=HookRunsResponse)
async def list_hook_runs(
    _admin: Annotated[AgentIdentity, Depends(require_admin_human)],
) -> HookRunsResponse:
    """最近的執行紀錄，新的在前。"""
    return HookRunsResponse(runs=[HookRunOut(**asdict(run)) for run in hook_service.runs()])
