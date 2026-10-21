"""政策當下的隔離層級與專案根的唯一來源（iso-c2）.

專案根只來自 ``ctx["project_id"]`` 或 session 綁定的專案，絕不讀模型給的 ``args``；
``args`` 只用來算同意帳本的 subject。終端、驗證指令與 profile 都經 ``session_project_root``。
還沒接上 runner 的隔離必要工具（``ISOLATION_WIRED_ACTIONS`` 以外）一律 ``none``。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.models.identity import AgentIdentity
from src.services.isolation.consent import (
    ConsentKey,
    consent_ledger,
    consent_subject,
)
from src.services.isolation.status import CHECK_FAILED, LevelDecision, isolation_monitor
from src.services.resource_manager import resource_manager
from src.utils.async_db import session_service
from src.utils.isolation_policy import (
    ISOLATION_REQUIRED_TOOLS,
    ISOLATION_WIRED_ACTIONS,
    RUNNER_NOT_WIRED,
    TERMINAL_TOOLS,
    VERIFY_ACTION,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from src.models.context import PipelineContext
    from src.utils.isolation_policy import IsolationLevel

logger = logging.getLogger(__name__)

# 會在專案裡執行的指令（終端與互審的驗證指令）
PROJECT_TOOLS = TERMINAL_TOOLS | {VERIFY_ACTION}


async def session_project_root(
    ctx: Mapping[str, Any] | PipelineContext, session_id: str | None
) -> Path | None:
    """這次執行的專案根：ctx 的專案優先，否則 session 綁定的專案；沒有就是 None。"""
    project_id = ctx.get("project_id")
    if not project_id and session_id:
        session = await session_service.get_session_by_id(session_id)
        project_id = session.get("project_id") if session else None
    if not project_id:
        return None
    identity = ctx.get("identity")
    caller = identity if isinstance(identity, AgentIdentity) else None
    root = await resource_manager.get_project_path(str(project_id), caller)
    return Path(root) if root else None


def _records_consent(tool: str, builtin: bool) -> bool:
    # MCP 等外部工具不經 runner：和隔離動作同名也不進隔離判定、不記帳本
    return builtin and (tool in PROJECT_TOOLS or tool in ISOLATION_REQUIRED_TOOLS)


async def _decide(
    identity: AgentIdentity,
    ctx: Mapping[str, Any] | PipelineContext,
    session_id: str | None,
    tool: str,
    builtin: bool,
) -> tuple[LevelDecision, Path | None]:
    """層級與這次用到的專案根（審批卡要用專案根算降級去處）。"""
    await isolation_monitor.ensure_ready()
    if not _records_consent(tool, builtin):
        snap = isolation_monitor.snapshot()
        return LevelDecision(snap.level, snap.reason), None
    if tool not in ISOLATION_WIRED_ACTIONS:
        return LevelDecision("none", RUNNER_NOT_WIRED), None
    if tool == "create_mcp":
        snap = isolation_monitor.snapshot()
        return LevelDecision(snap.level, snap.reason), None
    if tool == "scaffold_mcp":
        return isolation_monitor.local_level(), None
    project_root = await session_project_root(ctx, session_id) if tool in PROJECT_TOOLS else None
    return await isolation_monitor.level_for(identity, project_root), project_root


async def policy_level(
    identity: AgentIdentity,
    ctx: Mapping[str, Any] | PipelineContext,
    session_id: str | None,
    call_id: str | None,
    *,
    tool: str,
    args: Mapping[str, Any] | None,
    builtin: bool = True,
) -> LevelDecision:
    """政策評估用的層級；隔離必要的內建工具同時把「層級＋subject」記進同意帳本。"""
    project_root: Path | None = None
    eligible = False
    try:
        decision, project_root = await _decide(identity, ctx, session_id, tool, builtin)
        # 審批卡在之後才組，手上沒有身分：這次在不在執行器範圍在這裡就記下
        if builtin and tool in {"create_mcp", "scaffold_mcp"}:
            eligible = tool == "create_mcp"
        else:
            eligible = isolation_monitor.executor_scope_issue(identity, project_root) is None
    except Exception:
        # 每個工具呼叫都會經過這裡：判定出錯不能讓呼叫卡住，照 none 處理（fail-closed）
        logger.exception("Isolation level check failed for %s; treating it as none", tool)
        decision = LevelDecision("none", CHECK_FAILED)
    if _records_consent(tool, builtin):
        consent_ledger.record_policy(
            ConsentKey.of(identity, session_id, call_id),
            level=decision.level,
            subject=consent_subject(tool, args),
            reason=decision.reason,
            project_root=project_root,
            executor_eligible=eligible,
        )
    return decision


def reviewer_level(tool: str, decision: LevelDecision, *, builtin: bool) -> IsolationLevel:
    """審查員看的層級：只有經 runner 的內建工具給政策層級，其餘（含所有 MCP 工具）一律 none。

    只改給審查員看的，決策表用的層級不變；MCP 工具可能和已接 runner 的名稱同名，所以要看內建。
    """
    return decision.level if builtin and tool in ISOLATION_WIRED_ACTIONS else "none"
