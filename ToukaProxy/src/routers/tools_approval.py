"""統一工具審批解算端點（hitl-unification H2）。

``POST /v1/tools/approve|reject/{exec_id}``——解算 approval_service 的待決審批
（審批卡經聊天 SSE 送達，見 tool_executor._await_human_approval）。

授權立場（比遺留 /terminal/approve 的 is_privileged 更嚴）：
1. **必須是人類**（``is_human``）——HITL 的 H 是人；agent token（含 Owner／admin
   代打）一律 403，杜絕 agent 自批自己的工具調用。
2. 且滿足其一：admin 權威（``is_admin_authority``）／待批 agent 的擁有者
   （``agent_id ∈ owned_agents``）／該 session 的擁有者（``owner_user_id`` 相符）。
3. Origin 驗證（CSRF）與 /terminal/approve 同款。

``/terminal/approve|reject`` 保留原樣（skill 安裝與遺留 ToukaUI 生態仍在用）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request  # noqa: TC002
from jsonschema import Draft202012Validator

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.models.terminal import InteractionResolutionRequest  # noqa: TC001
from src.services.approval_service import approval_service

if TYPE_CHECKING:
    from src.services.approval_service import ApprovalDecision, PendingApproval
from src.services.identity_router import get_verified_identity
from src.utils.async_db import session_service
from src.utils.csrf_guard import verify_origin

router = APIRouter(prefix="/tools", tags=["Tool Approval"])


async def _authorize_resolver(identity: AgentIdentity, pending: PendingApproval) -> None:
    """驗證解算者資格；不符即拋 AUTH_PERMISSION_DENIED（fail-closed）。"""
    if not identity or not identity.is_human:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    if identity.is_admin_authority():
        return
    if pending.agent_id and pending.agent_id in (identity.owned_agents or []):
        return
    if pending.session_id:
        session = await session_service.get_session_by_id(pending.session_id)
        if session and session.get("owner_user_id") == identity.agent_id:
            return
    raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)


async def _resolve(
    exec_id: str,
    http_request: Request,
    identity: AgentIdentity,
    *,
    decision: ApprovalDecision,
    response_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    verify_origin(http_request)
    pending = approval_service.get(exec_id)
    if pending is None:
        raise ToukaException(ErrorCode.TERMINAL_APPROVAL_NOT_FOUND)
    await _authorize_resolver(identity, pending)
    if (
        decision == "accepted"
        and pending.interaction_schema is not None
        and not Draft202012Validator(pending.interaction_schema).is_valid(
            response_payload or {}
        )
    ):
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR,
            detail_override="Interaction payload does not match its schema.",
        )
    resolved = await approval_service.resolve(
        exec_id,
        decision=decision,
        resolver=identity.agent_id,
        response_payload=response_payload,
    )
    if not resolved:  # 極窄競態：檢查後、解算前被他人搶先/逾時
        raise ToukaException(ErrorCode.TERMINAL_APPROVAL_NOT_FOUND)
    return {
        "status": {
            "accepted": "approved",
            "declined": "rejected",
            "cancelled": "cancelled",
        }[decision],
        "id": exec_id,
    }


@router.post("/approve/{exec_id}")
async def approve_tool_call(
    exec_id: str,
    http_request: Request,
    resolution: InteractionResolutionRequest | None = None,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
) -> dict[str, Any]:
    """批准一筆待決工具調用（統一 broker；等待中的 Gate-2 隨即續行執行）。"""
    return await _resolve(
        exec_id,
        http_request,
        identity,
        decision="accepted",
        response_payload=resolution.payload if resolution else None,
    )


@router.post("/reject/{exec_id}")
async def reject_tool_call(
    exec_id: str,
    http_request: Request,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
) -> dict[str, Any]:
    """拒絕一筆待決工具調用（模型收到結構化 approval_rejected、零執行）。"""
    return await _resolve(exec_id, http_request, identity, decision="declined")


@router.post("/cancel/{exec_id}")
async def cancel_tool_interaction(
    exec_id: str,
    http_request: Request,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
) -> dict[str, Any]:
    """取消一筆互動；與 decline 分流，供 MRTR 回傳 action=cancel。"""
    return await _resolve(exec_id, http_request, identity, decision="cancelled")
