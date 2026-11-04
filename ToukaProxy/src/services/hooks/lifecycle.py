"""回合、子代理、剪裁、任務、MCP 表單、心跳與設定的 hook（ADR-0292）.

- 只通知（背景執行、呼叫端不等、結束碼 2 與 JSON 都沒有效果）：StopFailure、SubagentStop、
  PreCompact、PostCompact、TaskCreated、TaskCompleted、ConfigChange、HeartbeatStart、
  HeartbeatStop。
- SubagentStart：等 hook 跑完；``additionalContext`` 當成資料接在子代理的第一則訊息後面。
- Elicitation：表單跳出之前等 hook 跑完；只能拒絕，回 ``accept``、``content`` 照樣問人。

事件內容是白名單；上游錯誤先遮罩憑證與本機路徑。沒有符合的 hook 時不組事件內容。hook 服務出錯
只記 log，不影響呼叫端。等 hook 的期間使用者按了停止，就不往下走（禁令 67 ⑦）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from src.services.hooks.background import schedule_background_hook
from src.services.hooks.events import base_payload, wrap_hook_output
from src.services.hooks.service import hook_service
from src.services.hooks.stopping import request_stopped
from src.utils.mcp_secrets import sanitize_diagnostic
from src.utils.secret_text import redact_credentials

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)

STOP_FAILURE_EVENT: Final[str] = "StopFailure"
SUBAGENT_START_EVENT: Final[str] = "SubagentStart"
SUBAGENT_STOP_EVENT: Final[str] = "SubagentStop"
PRE_COMPACT_EVENT: Final[str] = "PreCompact"
POST_COMPACT_EVENT: Final[str] = "PostCompact"
TASK_CREATED_EVENT: Final[str] = "TaskCreated"
TASK_COMPLETED_EVENT: Final[str] = "TaskCompleted"
ELICITATION_EVENT: Final[str] = "Elicitation"
CONFIG_CHANGE_EVENT: Final[str] = "ConfigChange"
HEARTBEAT_START_EVENT: Final[str] = "HeartbeatStart"
HEARTBEAT_STOP_EVENT: Final[str] = "HeartbeatStop"

SUBAGENT_TYPE: Final[str] = "subagent"
AUTO_COMPACT: Final[str] = "auto"  # 手動切分是前端做的，後端沒有這個時點（ADR-0292 差別表）
MAX_COMPACT_SUMMARY_CHARS: Final[int] = 10_000

# ConfigChange 的比對規則看這些名稱
ConfigSection = Literal[
    "autonomy",
    "backup_schedule",
    "bots",
    "connections",
    "custom_models",
    "embedding",
    "env",
    "group_whitelist",
    "hooks",
    "identity",
    "mcp",
    "models",
    "permissions",
    "restore",
    "settings_import",
    "setup",
    "skills",
    "sliding_window",
    "workspace",
]


def _notify(
    event: str,
    ctx: Mapping[str, Any],
    fields: Callable[[], dict[str, Any]],
    match: Iterable[str] = (),
) -> None:
    """有符合的 hook 時在背景執行；``fields`` 只在要執行時才呼叫。"""
    schedule_background_hook(
        event,
        lambda: {**base_payload(ctx), **fields()},
        match=match,
        incognito=bool(ctx.get("incognito")),
    )


def notify_stop_failure(ctx: Mapping[str, Any], error_type: str, details: str) -> None:
    """這一輪因為錯誤結束（重試和備援都用完）；子代理的回合不發，比照 Stop。"""
    if ctx.get("subagent_run"):
        return
    _notify(
        STOP_FAILURE_EVENT,
        ctx,
        lambda: {
            "error": error_type,
            "error_details": sanitize_diagnostic(redact_credentials(details)),
        },
        match=(error_type,),
    )


@dataclass(frozen=True)
class SubagentStart:
    """SubagentStart 的結果：``text`` 接在第一則訊息後面；``stopped`` 表示不要送模型。"""

    text: str = ""
    stopped: bool = False


async def run_subagent_start_hooks(
    ctx: Mapping[str, Any], sub_identity: AgentIdentity
) -> SubagentStart:
    """子代理開始之前執行；``ctx`` 是父代理的。出錯時當成沒有 hook。"""
    try:
        if not hook_service.has_matching_hooks(SUBAGENT_START_EVENT, (SUBAGENT_TYPE,)):
            return SubagentStart()
        decision = await hook_service.dispatch(
            SUBAGENT_START_EVENT,
            {**base_payload(ctx), "agent_id": sub_identity.agent_id, "agent_type": SUBAGENT_TYPE},
            match=(SUBAGENT_TYPE,),
            incognito=bool(ctx.get("incognito")),
        )
    except Exception:
        logger.exception("SubagentStart hooks failed; continuing without them")
        return SubagentStart()
    if decision.ran and request_stopped(ctx):
        return SubagentStart(stopped=True)
    if not decision.context:
        return SubagentStart()
    return SubagentStart(text="\n\n" + wrap_hook_output(SUBAGENT_START_EVENT, decision.context))


def notify_subagent_stop(
    ctx: Mapping[str, Any], sub_identity: AgentIdentity, last_message: str
) -> None:
    """子代理結束；失敗或逾時沒有回覆時 ``last_message`` 是空字串。"""
    _notify(
        SUBAGENT_STOP_EVENT,
        ctx,
        lambda: {
            "agent_id": sub_identity.agent_id,
            "agent_type": SUBAGENT_TYPE,
            "last_assistant_message": last_message,
        },
        match=(SUBAGENT_TYPE,),
    )


def notify_pre_compact(identity: Any, session_id: str) -> None:
    """滑動視窗要剪裁了；不等 hook（禁令 28、57）。"""
    _notify(
        PRE_COMPACT_EVENT,
        {"session_id": session_id, "identity": identity},
        lambda: {"trigger": AUTO_COMPACT},
        match=(AUTO_COMPACT,),
    )


def notify_post_compact(identity: Any, session_id: str, summary: str | None) -> None:
    """剪裁確認存好了；``compact_summary`` 有上限。"""
    _notify(
        POST_COMPACT_EVENT,
        {"session_id": session_id, "identity": identity},
        lambda: {
            "trigger": AUTO_COMPACT,
            "compact_summary": (summary or "")[:MAX_COMPACT_SUMMARY_CHARS],
        },
        match=(AUTO_COMPACT,),
    )


def _notify_task(event: str, ctx: Mapping[str, Any], mission_id: str, task: Any) -> None:
    _notify(
        event,
        ctx,
        lambda: {"mission_id": mission_id, "task_id": task.id, "task_subject": task.description},
    )


def notify_tasks_created(ctx: Mapping[str, Any], mission: Any) -> None:
    """任務看板建好了：每個任務各一次。"""
    for task in mission.tasks:
        _notify_task(TASK_CREATED_EVENT, ctx, mission.mission_id, task)


def notify_task_completed(ctx: Mapping[str, Any], mission: Any, task_id: str) -> None:
    """任務變成完成。"""
    for task in mission.tasks:
        if task.id == task_id:
            _notify_task(TASK_COMPLETED_EVENT, ctx, mission.mission_id, task)


async def run_elicitation_hooks(
    ctx: Mapping[str, Any], server: str, message: str, schema: Mapping[str, Any]
) -> bool:
    """MCP 表單跳出之前執行；回傳 True 就不跳表單、直接拒絕。出錯時照樣問人。"""
    try:
        if not hook_service.has_matching_hooks(ELICITATION_EVENT, (server,)):
            return False
        decision = await hook_service.dispatch(
            ELICITATION_EVENT,
            {
                **base_payload(ctx),
                "mcp_server_name": server,
                "message": message,
                "mode": "form",
                "requested_schema": dict(schema),
            },
            match=(server,),
            incognito=bool(ctx.get("incognito")),
        )
    except Exception:
        logger.exception("Elicitation hooks failed; asking the user as usual")
        return False
    return decision.blocked or (decision.ran and request_stopped(ctx))


def notify_config_change(section: ConfigSection) -> None:
    """設定存好了（路由確認成功之後才呼叫）；不放值。關閉 hooks 那次已經沒有 hook，不會觸發。"""
    _notify(CONFIG_CHANGE_EVENT, {}, lambda: {"source": section}, match=(section,))


def notify_heartbeat_start(
    identity: Any, session_id: str, *, pulses_total: int, home: bool
) -> None:
    """心跳開始了。"""
    _notify(
        HEARTBEAT_START_EVENT,
        {"session_id": session_id, "identity": identity},
        lambda: {"pulses_total": pulses_total, "home": home},
    )


def notify_heartbeat_stop(
    identity: Any, session_id: str, *, pulses_total: int, pulses_done: int, end: str
) -> None:
    """心跳結束了；``end`` 是結束原因。

    completed、interrupted、quota、budget、no_model、error 或 cancelled。
    """
    _notify(
        HEARTBEAT_STOP_EVENT,
        {"session_id": session_id, "identity": identity},
        lambda: {"pulses_total": pulses_total, "pulses_done": pulses_done, "end": end},
    )
