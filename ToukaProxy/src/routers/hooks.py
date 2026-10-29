"""Hook 管理路由（ADR-0292）：只給人類管理員讀寫 ``config/hooks.json`` 與看執行紀錄.

- 讀取回傳目前生效的設定；設定檔壞掉時是上一份好的，``health`` 標 ``error``。
- 存檔要帶載入時的 revision（蓋掉別人的修改回 409）；所有 command hook 先過路徑界線（422）。
- 代理人碰不到這裡：閘門是人類 admin（admin 代打不算），寫入另查 CSRF Origin。
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
    HooksConfigResponse,
    HooksSaveRequest,
    HooksSaveResponse,
)
from src.models.identity import AgentIdentity
from src.services.authorization_gateway import require_admin_human
from src.services.hooks.service import hook_service
from src.utils.csrf_guard import verify_origin
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hooks", tags=["hooks"])


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
    return HooksSaveResponse(revision=revision)


@router.get("/runs", response_model=HookRunsResponse)
async def list_hook_runs(
    _admin: Annotated[AgentIdentity, Depends(require_admin_human)],
) -> HookRunsResponse:
    """最近的執行紀錄，新的在前。"""
    return HookRunsResponse(runs=[HookRunOut(**asdict(run)) for run in hook_service.runs()])
