"""ToolExecutor 注入工具呼叫的執行中繼資料 SSOT。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from src.models.identity import AgentIdentity

EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]

# ``_approved_by`` 唯一的可信值：Auto 審查員放行了 REVIEW（ADR-0246 U2c-3）。只滿足 REVIEW；
# HUMAN_ONLY 只認人類審批卡設下的 ``_hitl_preapproved=True``。
APPROVED_BY_AUTO_REVIEWER = "auto_reviewer"


class ToolExecutionContextKwargs(TypedDict):
    """跨工具執行中繼資料；領域身分 ``identity`` 不屬於此集合。"""

    request_id: str
    agent_id: str  # noqa: IDENTITY_SSOT - string metadata, not an authorization principal
    session_id: str | None
    project_id: str | None
    model: str | None
    is_toukasoul: bool
    auth_level: str
    _hitl_preapproved: bool
    _approved_by: str | None
    _interactive: bool
    _emit_event: EventEmitter
    _call_id: str
    _ctx: dict[str, Any]


EXECUTION_CONTEXT_KWARG_NAMES = frozenset(ToolExecutionContextKwargs.__annotations__)


def build_execution_context_kwargs(
    *,
    request_id: str,
    identity: AgentIdentity,
    session_id: str | None,
    project_id: str | None,
    model: str | None,
    is_toukasoul: bool,
    auth_level: str,
    hitl_preapproved: bool,
    interactive: bool,
    emit_event: EventEmitter,
    call_id: str,
    context: dict[str, Any],
    approved_by: str | None = None,
) -> ToolExecutionContextKwargs:
    """建立 ToolExecutor 注入的完整中繼 kwargs。"""
    return {
        "request_id": request_id,
        "agent_id": identity.agent_id,
        "session_id": session_id,
        "project_id": project_id,
        "model": model,
        "is_toukasoul": is_toukasoul,
        "auth_level": auth_level,
        "_hitl_preapproved": hitl_preapproved,
        "_approved_by": approved_by,
        "_interactive": interactive,
        "_emit_event": emit_event,
        "_call_id": call_id,
        "_ctx": context,
    }
