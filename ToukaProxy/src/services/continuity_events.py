"""可信活動事件的記錄器與各掛點用的小函式.

只在領域自己確認事情真的做成之後呼叫：使用者這一輪已保存（SoulLayer）、心跳開始與結束、
任務狀態已保存（mission_tools）、記憶已提交（Memory 經 ``event_recorder`` 呼叫同一個記錄器）。
結果不明或失敗都不呼叫，也不另寫 failed；例外是心跳的 ``crashed``（重啟時確認上次沒跑完）與
``heartbeat_skipped``（入口被擋），記的都是確定的事實（ADR-0318）。模型沒有任何寫入路徑。

``record()`` 同步、不阻塞、執行緒安全、絕不丟例外：
- 在綁定的主事件迴圈裡 → ``spawn_logged`` 排背景寫入；
- 其他情況（工作執行緒，或工作執行緒裡 ``asyncio.run`` 開的短命迴圈）→ ``call_soon_threadsafe``
  排回主迴圈，寫入不會跟著短命迴圈被取消。
沒綁定（Proxy 沒啟動、單元測試）就不記。只記正式靈魂；無痕、子代理、派工不記。
寫失敗只記 warning、不重試。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal, get_args

from memory_server.services.event_recorder import register_event_recorder

from src.handlers.adapters.retry import KNOWN_ERROR_TYPES
from src.models.identity import AgentIdentity
from src.services.soul_service import soul_service
from src.utils.continuity_events_db import EventRow, insert_event, read_records_start
from src.utils.task_utils import spawn_logged
from src.utils.time_utils import get_current_epoch_ms

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from memory_server.models.mission import MissionState, TaskStatus

logger = logging.getLogger(__name__)

EventKind = Literal[
    "user_turn",
    "heartbeat_started",
    "heartbeat_finished",
    "heartbeat_skipped",
    "mission_started",
    "task_todo",
    "task_doing",
    "task_blocked",
    "task_failed",
    "task_done",
    "mission_completed",
    "mission_finalized",
    "memory_saved",
    "memory_archived",
    "memory_superseded",
]
EVENT_KINDS: frozenset[str] = frozenset(get_args(EventKind))
# 會重複發生的轉換（FAILED → 重置 → 再 FAILED、兩次 BLOCKED）：event_id 加當下毫秒。
# 其餘是終端狀態，或主體天生唯一、不會再發生第二次的事件，用固定鍵。
REPEATABLE_KINDS: frozenset[str] = frozenset(
    {"task_todo", "task_doing", "task_blocked", "task_failed", "heartbeat_skipped"}
)
# 心跳結束原因（固定列舉）；crashed 是重啟時發現上次沒跑完（ADR-0318）
HeartbeatEnd = Literal[
    "completed",
    "interrupted",
    "quota",
    "budget",
    "no_model",
    "model_error",
    "error",
    "cancelled",
    "crashed",
]
# 心跳在入口被擋的原因（固定列舉，ADR-0318）：沒設大腦、擁有者登入過期
# no_model、quota、budget：寫初始提示之前先檢查（ADR-0323）
HeartbeatSkip = Literal["no_brain", "no_model", "quota", "budget", "owner_login_expired"]
# 無痕、子代理、派工：不記
_SKIP_FLAGS = ("incognito", "subagent_run", "dispatch_run")
# 模型寫的 id（任務 id）只保留這種短字串，其他換成雜湊
_SAFE_REF = re.compile(r"[A-Za-z0-9._:-]{1,32}")
_SAFE_REF_HASH_CHARS = 12

_suppressed: ContextVar[bool] = ContextVar("continuity_events_suppressed", default=False)


def event_id(kind: str, agent: str, subject: str, occurred_ms: int) -> str:
    """事件的冪等鍵：一律帶靈魂；會重複發生的轉換加毫秒，其餘固定."""
    base = f"{kind}:{agent}:{subject}"
    return f"{base}:{occurred_ms}" if kind in REPEATABLE_KINDS else base


def safe_ref(value: object) -> str:
    """模型寫的 id 只保留短的英數 id；其他換成 ``h:`` 加雜湊（原文不進表）."""
    text = str(value)
    if _SAFE_REF.fullmatch(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:_SAFE_REF_HASH_CHARS]
    return f"h:{digest}"


def _skipped(ctx: Any) -> bool:
    """無痕、子代理、派工的執行不記."""
    return ctx is not None and any(bool(ctx.get(flag)) for flag in _SKIP_FLAGS)


def _session(ctx: Any) -> str | None:
    session_id = ctx.get("session_id") if ctx is not None else None
    return session_id if isinstance(session_id, str) and session_id else None


async def _write(row: EventRow, order: asyncio.Lock) -> None:
    """照呼叫的順序一筆一筆寫（同一任務的狀態判斷要看到前一筆）；失敗只記 warning、不重試."""
    async with order:
        try:
            await insert_event(row)
        except Exception:
            logger.warning(
                "Activity event %s could not be written; not retried.", row.kind, exc_info=True
            )


def _spawn_write(row: EventRow, order: asyncio.Lock) -> None:
    """在主事件迴圈的執行緒上排背景寫入（``call_soon_threadsafe`` 的回呼）."""
    spawn_logged(_write(row, order), name="continuity_event")


class ContinuityEventRecorder:
    """可信活動事件的記錄器（實作 ``memory_server.protocols.IContinuityEventRecorder``）."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._order: asyncio.Lock | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """綁定主事件迴圈（Proxy 啟動時）；寫入在這個迴圈裡依序進行."""
        self._order = asyncio.Lock()
        self._loop = loop

    def unbind(self) -> None:
        """解除綁定（Proxy 關閉時）；之後的呼叫都不記."""
        self._loop = None
        self._order = None

    def record(
        self,
        identity: Any,
        kind: str,
        subject: str,
        metadata: Mapping[str, str | int | bool] | None = None,
        *,
        session_id: str | None = None,
    ) -> None:
        """記一筆事件；同步、不阻塞、執行緒安全，任何錯誤只記 warning."""
        try:
            loop, order = self._loop, self._order
            if loop is None or order is None or _suppressed.get():
                return
            if kind not in EVENT_KINDS:
                logger.warning("Unknown activity event kind %r was not recorded.", kind)
                return
            if (
                not isinstance(identity, AgentIdentity)
                or identity.is_human
                or not soul_service.is_toukasoul(identity)
            ):
                return
            occurred = get_current_epoch_ms()
            row = EventRow(
                event_id=event_id(kind, identity.agent_id, subject, occurred),
                agent=identity.agent_id,
                kind=kind,
                occurred_at=occurred,
                subject=subject,
                session_id=session_id,
                metadata=dict(metadata or {}),
            )
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                _spawn_write(row, order)
            else:
                loop.call_soon_threadsafe(_spawn_write, row, order)
        except Exception:
            logger.warning("Activity event %s was not recorded.", kind, exc_info=True)


recorder = ContinuityEventRecorder()


def start_recording() -> None:
    """Proxy 啟動時：綁定目前的主事件迴圈並登記給 Memory."""
    recorder.bind(asyncio.get_running_loop())
    register_event_recorder(recorder)


def stop_recording() -> None:
    """Proxy 關閉時：清掉 Memory 的登記並解除綁定."""
    register_event_recorder(None)
    recorder.unbind()


@contextmanager
def events_scope(ctx: Any) -> Iterator[None]:
    """這段執行裡（含 ``to_thread`` 的工作執行緒）無痕、子代理、派工的事件一律不記."""
    token = _suppressed.set(_suppressed.get() or _skipped(ctx))
    try:
        yield
    finally:
        _suppressed.reset(token)


def record_user_turn(ctx: Any, soul: AgentIdentity) -> None:
    """人類送來、已保存、屬於這個靈魂的這一輪（心跳、無痕、子代理、派工都不算）."""
    try:
        message_id = ctx.get("source_message_id")
        requester = ctx.get("ui_requester")
        if (
            not isinstance(message_id, str)
            or not message_id
            or ctx.get("heartbeat_run")
            or _skipped(ctx)
            or not ctx.get("session_bound")
            or not isinstance(requester, AgentIdentity)
            or not requester.is_human
        ):
            return
        recorder.record(soul, "user_turn", message_id, session_id=_session(ctx))
    except Exception:
        logger.warning("Activity event user_turn was not recorded.", exc_info=True)


def heartbeat_run_key(session_id: str, t0: float) -> str:
    """一次心跳 run 的主體 id：對話 id 加 run 開始的毫秒."""
    return f"{session_id}:{int(t0 * 1000)}"


def record_heartbeat_started(
    identity: AgentIdentity, session_id: str, run_key: str, *, pulses_total: int, home: bool
) -> None:
    """心跳迴圈真的開始了（in-flight 標記已寫入）."""
    recorder.record(
        identity,
        "heartbeat_started",
        run_key,
        {"pulses_total": pulses_total, "home": home},
        session_id=session_id,
    )


def record_heartbeat_finished(
    identity: AgentIdentity,
    session_id: str,
    run_key: str,
    *,
    pulses_total: int,
    pulses_done: int,
    turns: int,
    end: HeartbeatEnd,
    error: object = None,
) -> None:
    """心跳迴圈結束（正常、提早停、出錯或被取消）；模型失敗另記種類，表外的記 unknown."""
    metadata: dict[str, str | int | bool] = {
        "pulses_total": pulses_total,
        "pulses_done": pulses_done,
        "turns": turns,
        "end": end,
    }
    if end == "model_error":
        known = isinstance(error, str) and error in KNOWN_ERROR_TYPES
        metadata["error"] = error if known else "unknown"
    recorder.record(identity, "heartbeat_finished", run_key, metadata, session_id=session_id)


def record_heartbeat_skipped(
    identity: AgentIdentity, reason: HeartbeatSkip, *, session_id: str | None
) -> None:
    """心跳在入口被擋；同一原因連續被擋只記第一次（寫入時比對最新一筆心跳事件）."""
    recorder.record(
        identity, "heartbeat_skipped", reason, {"reason": reason}, session_id=session_id
    )


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


async def record_crashed_heartbeats(
    runs: Iterable[Mapping[str, Any]], resolve: Callable[[str], AgentIdentity | None]
) -> None:
    """重啟時發現上次沒跑完的心跳（啟動掃描到的 in-flight 標記）：各記一筆 crashed 結束.

    和正常結束用同一個鍵，已經記過結束的不會被改；那次開始得比這個靈魂的可信紀錄起點早
    （例如之後重置過）就不記（ADR-0287）。逐筆處理，出錯只記 warning。
    """
    for run in runs:
        try:
            agent, session_id = run.get("agent"), run.get("session_id")
            started, total = run.get("started_at"), run.get("total")
            if not (isinstance(agent, str) and isinstance(session_id, str) and session_id):
                continue
            if not (_number(started) and _number(total) and isinstance(total, int)):
                continue
            identity = resolve(agent)
            if not isinstance(identity, AgentIdentity):
                continue
            started_ms = int(float(started) * 1000)
            records_start = await read_records_start(identity)
            if records_start is not None and started_ms < records_start:
                continue
            recorder.record(
                identity,
                "heartbeat_finished",
                heartbeat_run_key(session_id, float(started)),
                {"pulses_total": total, "end": "crashed", "started_ms": started_ms},
                session_id=session_id,
            )
        except Exception:
            logger.warning("A crashed heartbeat run was not recorded.", exc_info=True)


def _board_meta(identity: AgentIdentity, board_owner: str) -> dict[str, str]:
    """跨看板（例如改 DispatchAgent 的看板）時標明看板擁有者；事件仍記在動手的靈魂名下."""
    return {} if board_owner == identity.agent_id else {"board_owner": safe_ref(board_owner)}


def record_mission_started(identity: AgentIdentity, mission: MissionState, ctx: Any) -> None:
    """新任務看板已存好."""
    try:
        if _skipped(ctx):
            return
        metadata = {"tasks": len(mission.tasks), **_board_meta(identity, mission.agent_id)}
        recorder.record(
            identity, "mission_started", mission.mission_id, metadata, session_id=_session(ctx)
        )
    except Exception:
        logger.warning("Activity event mission_started was not recorded.", exc_info=True)


def record_task_update(
    identity: AgentIdentity, state: MissionState, task_id: str, status: TaskStatus, ctx: Any
) -> None:
    """任務狀態已存好；整個看板因此全部完成時另記 mission_completed."""
    try:
        if _skipped(ctx):
            return
        board = _board_meta(identity, state.agent_id)
        task_ref = safe_ref(task_id)
        recorder.record(
            identity,
            f"task_{str(status).lower()}",
            f"{state.mission_id}/{task_ref}",
            {"mission_id": state.mission_id, "task_id": task_ref, **board},
            session_id=_session(ctx),
        )
        if str(state.status) == "DONE":
            recorder.record(
                identity, "mission_completed", state.mission_id, board, session_id=_session(ctx)
            )
    except Exception:
        logger.warning("Activity event for task %s was not recorded.", task_id, exc_info=True)


def record_mission_finalized(identity: AgentIdentity, state: MissionState, ctx: Any) -> None:
    """驗收結論已存好（只記一次；結論文字不進表）."""
    try:
        if _skipped(ctx):
            return
        recorder.record(
            identity,
            "mission_finalized",
            state.mission_id,
            _board_meta(identity, state.agent_id),
            session_id=_session(ctx),
        )
    except Exception:
        logger.warning("Activity event mission_finalized was not recorded.", exc_info=True)
