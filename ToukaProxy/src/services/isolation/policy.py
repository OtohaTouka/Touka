"""政策當下的隔離層級與專案根的唯一來源（iso-c2）.

專案根只來自 ``ctx["project_id"]`` 或 session 綁定的專案，絕不讀模型給的 ``args``；
``args`` 只用來算同意帳本的 subject。終端、驗證指令與 profile 都經 ``session_project_root``；
工具執行器給檔案工具的專案編號經 ``session_project_id``（ADR-0304）。
還沒接上 runner 的隔離必要工具（``ISOLATION_WIRED_ACTIONS`` 以外）一律 ``none``。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.models.identity import AgentIdentity
from src.models.safety import (
    PROJECT_SPACE_DENY_PREFIX,
    SUBAGENT_COMMANDS_NEED_EXECUTOR,
    SUBAGENT_COMMANDS_NOT_ALLOWED,
    SUBAGENT_DENY_PREFIX,
)
from src.sandbox_kernel.spec import RELAY_FAILURE_CODES
from src.services.isolation.consent import (
    ConsentKey,
    consent_ledger,
    consent_subject,
)
from src.services.isolation.profiles import configured_projects_root
from src.services.isolation.project_space import structural_scope
from src.services.isolation.status import (
    BLOCK_PORTS_INCOMPLETE,
    CHECK_FAILED,
    EXECUTOR_UNREACHABLE,
    PROBE_PENDING,
    PROJECT_NOT_IN_EXECUTOR,
    LevelDecision,
    isolation_monitor,
)
from src.services.resource_manager import resource_manager
from src.utils.async_db import PLACEHOLDER_PROJECT_ID, session_service
from src.utils.isolation_policy import (
    ISOLATION_REQUIRED_TOOLS,
    ISOLATION_WIRED_ACTIONS,
    RUNNER_NOT_WIRED,
    TERMINAL_TOOLS,
    VERIFY_ACTION,
)
from src.utils.presence import PARENT_TURN_ENDED, commands_attended, no_human_followup
from src.utils.touka_dirs import SCAN_LIMIT

if TYPE_CHECKING:
    from collections.abc import Mapping

    from src.models.context import PipelineContext
    from src.utils.isolation_policy import IsolationLevel

logger = logging.getLogger(__name__)

# 會在專案裡執行的指令（終端與互審的驗證指令）
PROJECT_TOOLS = TERMINAL_TOOLS | {VERIFY_ACTION}
_GENERIC_DENIAL_SUGGESTION = (
    "Do not retry this tool; explain the restriction to the user or use an allowed alternative."
)
# 專用空間被拒絕（ADR-0332）：這幾種通常會自己好（掃描、探測、封鎖埠、執行器重啟、判定出錯），
# 其他的要人處理
_TEMPORARY_REFUSALS = frozenset(
    {SCAN_LIMIT, PROBE_PENDING, BLOCK_PORTS_INCOMPLETE, EXECUTOR_UNREACHABLE, CHECK_FAILED}
    | RELAY_FAILURE_CODES
)
# 子代理的指令被拒絕的原因（ADR-0333）
_SUBAGENT_DENIALS = {
    SUBAGENT_COMMANDS_NOT_ALLOWED: (
        "a sub-agent may run commands only for a project in the dedicated project space, "
        "dispatched from a conversation where someone is present, in Auto or YOLO mode, and "
        "not as read-only."
    ),
    PARENT_TURN_ENDED: (
        "the turn that dispatched you has ended, so no more commands run for you."
    ),
    SUBAGENT_COMMANDS_NEED_EXECUTOR: (
        "sub-agent commands only run in the isolated executor, and it is not available now."
    ),
}


async def session_project_id(
    ctx: Mapping[str, Any] | PipelineContext, session_id: str | None
) -> str | None:
    """這次執行的專案：ctx 的專案優先，否則 session 綁定的專案；沒有就是 None。

    檔案工具、終端與驗證指令共用（ADR-0304）；不讀模型給的參數。session 綁定專案時
    已檢查過擁有權（``ui_sessions.save_session``）。心跳建家用的佔位編號不是專案，
    回 None（就算有人搶建了同名的列也不用；ADR-0304 2026-11-16 補記）。
    """
    project_id = ctx.get("project_id")
    if not project_id and session_id:
        session = await session_service.get_session_by_id(session_id)
        project_id = session.get("project_id") if session else None
    if not project_id or project_id == PLACEHOLDER_PROJECT_ID:
        return None
    return str(project_id)


async def session_project_root(
    ctx: Mapping[str, Any] | PipelineContext, session_id: str | None
) -> Path | None:
    """這次執行的專案根：``session_project_id`` 的專案；沒有就是 None。"""
    project_id = await session_project_id(ctx, session_id)
    if not project_id:
        return None
    identity = ctx.get("identity")
    caller = identity if isinstance(identity, AgentIdentity) else None
    root = await resource_manager.get_project_path(project_id, caller)
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
    projects = configured_projects_root()
    if ctx.get("subagent_run") is True and not structural_scope(project_root, projects).dedicated:
        # 子代理只在專用空間的專案裡跑指令，不退回自己的個人空間（ADR-0333）
        return LevelDecision("none", PROJECT_NOT_IN_EXECUTOR), project_root
    attended = commands_attended(ctx)
    try:
        decision = await isolation_monitor.level_for(identity, project_root, attended=attended)
    except Exception:
        # 專用空間的專案判定出錯也直接拒絕、不跳卡（ADR-0332）；其他照舊交給外層當成 none
        if not structural_scope(project_root, projects).dedicated:
            raise
        logger.exception("Isolation level check failed for a project space command (%s)", tool)
        return LevelDecision("none", CHECK_FAILED, refused=True), project_root
    return decision, project_root


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


def _subagent_denial_text(code: str) -> tuple[str, str]:
    """子代理的指令被拒絕（ADR-0333）：說原因，叫它交回派它的主代理."""
    why = _SUBAGENT_DENIALS.get(code, _SUBAGENT_DENIALS[SUBAGENT_COMMANDS_NOT_ALLOWED])
    return (
        f"Commands cannot run here ({code}): {why}",
        f"Do not retry the command; {no_human_followup({'subagent_run': True})}.",
    )


def denial_text(tool: str, role: str, reason: str | None) -> tuple[str, str]:
    """政策拒絕給模型的（說明、建議）；專用空間被拒絕時說原因，暫時的說可以稍後再試."""
    if (reason or "").startswith(SUBAGENT_DENY_PREFIX):
        return _subagent_denial_text((reason or "").removeprefix(SUBAGENT_DENY_PREFIX))
    refused = (reason or "").removeprefix(PROJECT_SPACE_DENY_PREFIX)
    if not reason or refused == reason:
        return f"Tool '{tool}' was denied by policy for role '{role}'.", _GENERIC_DENIAL_SUGGESTION
    message = (
        f"Commands in this project cannot run ({refused}): projects in the dedicated project "
        "space only run in the isolated executor, and this one did not pass its checks."
    )
    if role == "Subagent":  # 子代理碰不到使用者，交回派它的主代理（ADR-0333）
        return message, f"Do not retry the command; {no_human_followup({'subagent_run': True})}."
    if refused in _TEMPORARY_REFUSALS:
        return message, (
            "This is usually temporary; you may try again in a few minutes. "
            "It will not be run in any other way."
        )
    return message, "Do not retry the same command; tell the user the reason so they can fix it."


def reviewer_level(tool: str, decision: LevelDecision, *, builtin: bool) -> IsolationLevel:
    """審查員看的層級：只有經 runner 的內建工具給政策層級，其餘（含所有 MCP 工具）一律 none。

    只改給審查員看的，決策表用的層級不變；MCP 工具可能和已接 runner 的名稱同名，所以要看內建。
    """
    return decision.level if builtin and tool in ISOLATION_WIRED_ACTIONS else "none"
