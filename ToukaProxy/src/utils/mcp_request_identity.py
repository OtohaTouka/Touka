"""MCP 請求的身分：誰能拿到使用者各自授權的 MCP、MCP 操作的請求上下文（ADR-0281 T1）。

使用者各自授權（``oauth_user``）的 MCP 只代表「人在場時」（ADR-0206）：只有人在場、和靈魂的前景
對話才為這類伺服器帶身分。判斷只看伺服器設定的 ctx 欄位，不讀模型參數或客戶端標頭（禁令 16）。
純函式（禁令 18）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.models.identity import AgentIdentity
from src.protocols.mcp.canonical import ExecutionKind, McpRequestContext, McpRequestOrigin
from src.utils.presence import is_human_present

if TYPE_CHECKING:
    from src.models.policy import PolicyContext

GROUP_MODEL = "ToukaGroup"
# 群聊分發器分給每個成員的 ctx 設這個旗標：成員的 ctx 看起來和直接跟靈魂聊天一樣
GROUP_MEMBER_FLAG = "group_member_run"


def user_scoped_mcp_allowed(ctx: Any, *, request_model: str | None = None) -> bool:
    """這次執行能不能用使用者各自授權的 MCP：已驗證的靈魂身分、人在場、不是群聊、有原始人類。"""
    identity = ctx.get("identity")
    return (
        isinstance(identity, AgentIdentity)
        and not identity.is_human
        and bool(ctx.get("is_toukasoul"))
        and is_human_present(ctx)
        and request_model != GROUP_MODEL
        and not ctx.get(GROUP_MEMBER_FLAG)
        and bool(ctx.get("human_principal_id"))
    )


def mcp_request_context(
    ctx: Any,
    identity: AgentIdentity,
    *,
    request_id: str,
    installation_id: str,
    policy_context: PolicyContext | None = None,
    human_approved: bool = False,
) -> McpRequestContext:
    """一次 MCP 操作的請求上下文；派工（子代理）優先於心跳：心跳派出的子代理算派工（U2c-2）。"""
    origin = ctx.get("request_origin")
    if origin is None:
        origin = (
            McpRequestOrigin.FOREGROUND
            if is_human_present(ctx)
            else McpRequestOrigin.DISPATCH
            if ctx.get("dispatch_run")
            else McpRequestOrigin.HEARTBEAT
            if ctx.get("heartbeat_run")
            else McpRequestOrigin.FOREGROUND
        )
    return McpRequestContext(
        request_id=request_id,
        session_id=ctx.get("session_id"),
        identity=identity,
        human_principal_id=(
            ctx.get("human_principal_id") or (identity.agent_id if identity.is_human else None)
        ),
        installation_id=installation_id,
        auth_level=ctx.get("auth_level") or "Manual",
        execution_kind=(
            ExecutionKind.DISPATCH
            if ctx.get("dispatch_run")
            else ExecutionKind.HEARTBEAT
            if ctx.get("heartbeat_run")
            else ExecutionKind.FOREGROUND
        ),
        origin=origin,
        policy_context=policy_context,
        cancellation_id=ctx.get("cancellation_id"),
        trace_context=ctx.get("trace_context") or {},
        human_approved=human_approved,
    )
