"""Continuity v1：靈魂請求附上的 ``<continuity>`` 區塊（時間、上一輪、人類與自主活動、看板）。

位置：只附在這次請求**最後一則 user 訊息尾端**（比照 ``<ide_context>``）。所有廠商的提示快取
都是前綴比對；每次都變的內容放進 system 會讓之後的整段歷史失去快取，放在最尾端只損失尾端
（Claude 的歷史斷點由轉接層打在它之前，見 ``prompt_blocks.REQUEST_TAIL_TAGS``）。只在記憶體
組字、不寫任何紀錄：前端與心跳存的是自己的對話樹，下一輪送回的歷史不含它。前景對話由 SoulLayer
注入；心跳由 ``routers/heartbeat._execute_single_pulse`` 注入（心跳模式，不算「這段對話上一輪」）。

每行標來源：
- 現在時間：伺服器時鐘。
- 這段對話上一輪：先看記憶體裡這個 (agent, session) 上一次前景請求的時間（伺服器）；沒有時看可信
  活動事件的 user_turn（同一個對話、不含這一輪）；再沒有就在目前分支上往回找帶時間戳的節點——
  對話樹是前端整棵寫回的，節點時間可能來自使用者裝置的時鐘，照實標示，也絕不寫進事件表。
- 這段對話較早幾輪的時間（舊到新，最近幾輪；隔一小時以上標出間隔，最近一次長間隔就在
  最舊那一輪前面時也標在它後面；更早的話補在行尾）：可信活動事件的 user_turn。
  前一輪送出的區塊不存進紀錄，沒有這行時模型回頭看自己說過的「隔了幾天」找不到依據，
  會把對的話收回（11/24 試用）。
  沒有紀錄或讀不到就不加；心跳、無痕與 SNS 觸發的請求不加（SNS 的家模式會借用家的
  session，列出的會是擁有者的時間）。
- 上次有人類訊息、上次自主執行、最近完成：可信活動事件（``utils/continuity_events_db``）。
- 任務看板：MissionManager 唯讀（SSOT），只補 ``<mission_awareness>`` 沒有的時間。
- 可信紀錄起點：只在有「沒有紀錄」時出現。
- 這段對話是什麼、心跳第幾個脈衝與被誰叫醒（ADR-0322 PR 3）：先看觸發來源，再看是不是家。
事件表讀不到時照實寫讀不到，區塊照樣送出（不丟例外）。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, get_args

from memory_server.services.mission_manager import MissionManager
from memory_server.utils.legacy_time import to_epoch_ms

from src.adapters.content_normalize import append_to_last_user
from src.handlers.adapters.retry import KNOWN_ERROR_TYPES
from src.models.heartbeat_schedule import HeartbeatSchedule
from src.services.continuity_events import HeartbeatEnd, HeartbeatSkip, safe_ref
from src.services.continuity_lines import (
    CONVERSATION_LABEL,
    HEADER,
    KIND_INCOGNITO,
    current_time_line,
)
from src.services.home_handover import (
    CHANNEL_REPORT_EVERY_MS,
    CHILD_IDLE_MS,
    unreported_children,
)
from src.services.prompt_blocks import CONTINUITY, MISSION_AWARENESS, wrap_block
from src.services.subagent_registry import (
    FINISHED_KEEP_SECONDS,
    SubagentOutcome,
    subagent_registry,
)
from src.utils.async_db import session_service
from src.utils.continuity_events_db import read_snapshot
from src.utils.history_manager import TreeMessageManager
from src.utils.session_scope import can_be_reported, is_heartbeat_channel
from src.utils.time_utils import (
    CONVERSATION_RECORD_TIME_NOTE,
    format_epoch_to_taipei,
    get_current_epoch,
)

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity
    from src.utils.continuity_events_db import TimelineSnapshot

logger = logging.getLogger(__name__)

# 記憶體最多記幾個 (agent, session) 的上一次前景請求時間；超過丟最舊的。
MAX_TRACKED_SESSIONS = 1024
_MS_PER_SECOND = 1000
_MS_PER_HOUR = 3_600_000
_MINUTE = 60
_HOUR = 3600
_DAY = 86400
# 最近完成：看幾天、最多幾筆
RECENT_WINDOW_DAYS = 7
RECENT_LIMIT = 3
# 顯示到分鐘（format_epoch_to_taipei 回 "YYYY-MM-DD HH:MM:SS"）
_MINUTE_PRECISION = 16
_MISSION_REF_CHARS = 8

HEARTBEAT_LINE = "- Run type: autonomous heartbeat; no human is present in this run."
# 心跳不帶聊天的技能與專案說明（ADR-0323）：靈魂另有人格與前情，不是靈魂的只有列出的工具
_HEARTBEAT_SCOPE = (
    "Heartbeat runs do not load chat skills or the conversation's project context; only "
)
HEARTBEAT_SCOPE_SOUL = f"{_HEARTBEAT_SCOPE}your persona, prelude and the listed tools."
HEARTBEAT_SCOPE_OTHER = f"{_HEARTBEAT_SCOPE}the listed tools."
# 這段對話是什麼（ADR-0322 PR 3 家-1）：先看觸發來源（SNS 一律外人），再看是不是家
KIND_SNS = (
    "messages come from outsiders through an SNS platform, not from your owner - even when "
    "this is your home conversation; your home conversation and <soul_prelude> are private "
    "background, do not pass them on"
)
KIND_HOME = "your home conversation with your owner"
KIND_HEARTBEAT_HOME = f"{KIND_HOME}, but no human is present in this run"
KIND_HEARTBEAT_CHANNEL = (
    "a heartbeat channel; its key points are reported to your home (at most once every "
    f"{CHANNEL_REPORT_EVERY_MS // _MS_PER_HOUR} hours per channel)"
)
KIND_CHILD = (
    f"not your home; after {CHILD_IDLE_MS // _MS_PER_HOUR} hours idle, or when the home changes, "
    "its key points are reported into your home's <soul_prelude>"
)
KIND_NOT_REPORTED = "not your home, and nothing here is reported to your home"
# 子對話與心跳頻道：對得上前情裡回報的標頭（soul_prelude_service 的 child_report；ADR-0323 家-5）
CONVERSATION_ID_TEMPLATE = (
    "This conversation's id: {id}; reports in <soul_prelude> marked "
    '"Report from conversation {id}" were written earlier by this conversation.'
)
KIND_UNKNOWN = "unknown (your home could not be determined)"
# 心跳第幾個脈衝、被誰叫醒（ADR-0322 PR 3 心跳-2、心跳-3）；觸發種類只認這幾個值
HEARTBEAT_TRIGGERS = ("schedule", "manual")
SCHEDULE_MODES = get_args(HeartbeatSchedule.model_fields["mode"].annotation)  # 投影是 SSOT
RETRIGGER = "retrigger"  # 內部值：只有 /v1/heartbeat/retrigger 會設，/awaken 收不到
PULSE_LINE = "- Pulse {pulse} of {total}: replying with text ends this pulse only; {after}"
PULSE_MORE = "{left} more pulse(s) will wake you again."
PULSE_LAST = "this is the last pulse of this run."
WOKEN_LABEL = "Woken by"
WAKE_TEXTS = {
    "schedule": "a schedule",
    "manual": "a manual trigger",
    RETRIGGER: "a re-run of an interrupted background run",
}
SCHEDULE_MODE_TEXTS = {"interval": "repeating interval", "daily": "daily time", "once": "one-time"}
WAKE_UNSTATED = "not stated"
RERUN_FIRST = (
    "- Re-run: the previous background run did not finish ({when}). "
    "First check what was already done, then continue."
)
RERUN_LATER = "- Re-run: this is pulse {pulse} of the re-run."
RERUN_STARTED = "it started {ago}"
RERUN_START_UNKNOWN = "start time unknown"
PREVIOUS_TURN_LABEL = "Since your previous turn in this conversation"
# 這段對話較早幾輪的時間：最多列幾輪、間隔多久以上標出來
EARLIER_TURNS_SHOWN = 5
EARLIER_TURNS_BREAK_SECONDS = _HOUR
EARLIER_TURNS_LABEL = (
    f"Earlier turns in this conversation (Asia/Taipei, oldest first, last {EARLIER_TURNS_SHOWN}; "
    f"+gap marks breaks of {EARLIER_TURNS_BREAK_SECONDS // _MINUTE}m or more)"
)
EARLIER_BREAK_NOTE = (
    f"latest break of {EARLIER_TURNS_BREAK_SECONDS // _MINUTE}m or more before these: "
    "{gap} before the turn at {at}"
)
UNKNOWN_PREVIOUS = "unknown (no record of when the previous turn happened)"
HUMAN_LABEL = "Last message from a human (any conversation)"
RUN_LABEL = "Last autonomous run"
RUN_LABEL_HEARTBEAT = "Previous autonomous run (before this one)"
RECENT_LABEL = (
    f"Recently completed (last {RECENT_WINDOW_DAYS} days, newest first, max {RECENT_LIMIT})"
)
RECORDS_START_LABEL = "Activity records start"
# 家裡：最近在群聊與 SNS 說過話（ADR-0323 家-4）；內容不在這段對話
ELSEWHERE_WINDOW_HOURS = 24
ELSEWHERE_LABEL = f"In the last {ELSEWHERE_WINDOW_HOURS} hours you also spoke in"
ELSEWHERE_PLACES = {"group_turn": "group chats", "sns_turn": "SNS channels"}
ELSEWHERE_NOT_HERE = "their content is not in this conversation"
MISSION_LABEL = "Mission board"
RUNNING_SUBAGENTS_LABEL = "Sub-agents you dispatched in this conversation, still running"
RUNNING_SUBAGENTS_SHOWN = 5  # 最多列幾個，其餘只寫數量（ADR-0318）
FINISHED_SUBAGENTS_LABEL = (
    "Background sub-agents you dispatched in this conversation that finished in the last "
    f"{FINISHED_KEEP_SECONDS // _HOUR} hours (newest first)"
)
RESULT_READ = "result already read; re-read only if you need the full result"
RESULT_UNREAD = "result not read yet; read it"
# 讀過的也附：結果檔還在、可以重讀，之後的回合才拿得到全文（審查 LOW-2）；寫明需要才讀，免得白讀
READ_RESULT_CALL = 'mission_tools(action="await_dispatch_result", callback_token="{token}")'
UNREPORTED_LABEL = "Other conversations with new activity not yet reported to this home"
NO_ACTIVE_MISSION = "no active mission"
NONE_RECORDED = "none recorded"
TIMELINE_UNAVAILABLE = "unavailable (the activity record could not be read)"
# 來源標示（伺服器時鐘那個在 continuity_lines）
SERVER_MEMORY = "(server memory)"
ACTIVITY_RECORD = "(activity record)"
MISSION_BOARD = "(mission board)"
_END_REASONS = frozenset(get_args(HeartbeatEnd))
_SKIP_REASONS = frozenset(get_args(HeartbeatSkip))
_SUBAGENT_OUTCOMES = frozenset(get_args(SubagentOutcome))

_last_request_at: OrderedDict[tuple[str, str], float] = OrderedDict()


def elapsed_seconds(now_s: float, then: float, unit: Literal["s", "ms"]) -> float:
    """``then`` 到 ``now_s``（秒）經過幾秒；``then`` 是毫秒時先換成秒。時鐘倒退夾成 0。"""
    then_s = then / _MS_PER_SECOND if unit == "ms" else then
    return max(0.0, now_s - then_s)


def format_elapsed(seconds: float) -> str:
    """經過時間的精簡呈現：45s／12m／3h 5m／2d 4h。"""
    total = int(seconds)
    if total < _MINUTE:
        return f"{total}s"
    if total < _HOUR:
        return f"{total // _MINUTE}m"
    if total < _DAY:
        return f"{total // _HOUR}h {total % _HOUR // _MINUTE}m"
    return f"{total // _DAY}d {total % _DAY // _HOUR}h"


def last_backend_timestamp_ms(tree: Any) -> int | None:
    """目前分支上最近一個帶 ``timestamp`` 的節點（秒或毫秒，回毫秒）；不含這一輪的使用者訊息。

    名稱沿用；節點時間可能是後端寫的（心跳等），也可能是前端蓋的（使用者裝置時鐘），分不出來。
    """
    path = TreeMessageManager.get_linear_path(tree)
    if path and isinstance(path[-1], dict) and path[-1].get("role") == "user":
        path = path[:-1]
    for node in reversed(path):
        stamp = to_epoch_ms(node.get("timestamp")) if isinstance(node, dict) else None
        if stamp is not None:
            return stamp
    return None


def _swap_last_request(key: tuple[str, str], now_s: float) -> float | None:
    """記下這一次、回傳上一次（秒）；超過上限丟最舊的。"""
    previous = _last_request_at.pop(key, None)
    _last_request_at[key] = now_s
    while len(_last_request_at) > MAX_TRACKED_SESSIONS:
        _last_request_at.popitem(last=False)
    return previous


def _at_minute(epoch_ms: int) -> str:
    """台北時間到分鐘。"""
    return format_epoch_to_taipei(epoch_ms / _MS_PER_SECOND)[0][:_MINUTE_PRECISION]


def _when(now_s: float, epoch_ms: int) -> str:
    """相對時間加台北絕對時間：``2d 4h ago, 2026-10-13 12:02``。"""
    return f"{format_elapsed(elapsed_seconds(now_s, epoch_ms, 'ms'))} ago, {_at_minute(epoch_ms)}"


async def _read_timeline(
    soul: AgentIdentity,
    session_id: str | None,
    current_message_id: Any,
    now_s: float,
    *,
    elsewhere: bool = False,
    turns: bool = False,
) -> TimelineSnapshot | None:
    """一次讀完這個靈魂的活動事件；讀不到回 None（照實寫讀不到，不丟例外）。"""
    try:
        return await read_snapshot(
            soul,
            session_id=session_id,
            exclude_message_id=current_message_id if isinstance(current_message_id, str) else None,
            since_ms=int((now_s - RECENT_WINDOW_DAYS * _DAY) * _MS_PER_SECOND),
            limit=RECENT_LIMIT,
            # 群聊與 SNS 的次數只有家用得到
            elsewhere_since_ms=(
                int((now_s - ELSEWHERE_WINDOW_HOURS * _HOUR) * _MS_PER_SECOND)
                if elsewhere
                else None
            ),
            turns_here_limit=EARLIER_TURNS_SHOWN if turns else 0,
            turns_break_ms=EARLIER_TURNS_BREAK_SECONDS * _MS_PER_SECOND if turns else 0,
        )
    except Exception:
        logger.exception("continuity: reading the activity record failed")
        return None


async def _previous_turn_line(
    soul: AgentIdentity, session_id: str | None, now_s: float, timeline: TimelineSnapshot | None
) -> str:
    seconds: float | None = None
    note = ""
    if session_id:
        previous = _swap_last_request((soul.agent_id, session_id), now_s)
        if previous is not None:
            seconds = elapsed_seconds(now_s, previous, "s")
            note = f" {SERVER_MEMORY}"
        elif timeline is not None and timeline.last_turn_here_ms is not None:
            seconds = elapsed_seconds(now_s, timeline.last_turn_here_ms, "ms")
            note = f" {ACTIVITY_RECORD}"
        else:
            try:
                session = await session_service.get_session_by_id(session_id)
                stamp = last_backend_timestamp_ms(session.get("messages")) if session else None
            except Exception:
                logger.exception("continuity: reading session %s failed", session_id)
                stamp = None
            if stamp is not None:
                seconds = elapsed_seconds(now_s, stamp, "ms")
                note = f" {CONVERSATION_RECORD_TIME_NOTE}"
    text = UNKNOWN_PREVIOUS if seconds is None else format_elapsed(seconds) + note
    return f"- {PREVIOUS_TURN_LABEL}: {text}"


def _earlier_turns_line(timeline: TimelineSnapshot | None) -> str | None:
    """這段對話較早幾輪的時間，舊到新；隔一小時以上標出間隔.

    最近一次長間隔就在最舊那一輪前面時標在它後面；更早的話補在行尾，聊了很多輪之後依據
    也不會消失。沒有紀錄或讀不到就不加（「上一輪」那行已照實寫）。
    """
    if timeline is None or not timeline.turns_here_ms:
        return None
    latest = timeline.latest_break_here
    items: list[str] = []
    previous: int | None = None
    for at_ms in timeline.turns_here_ms:
        item = _at_minute(at_ms)
        if previous is not None:
            gap = elapsed_seconds(at_ms / _MS_PER_SECOND, previous, "ms")
            if gap >= EARLIER_TURNS_BREAK_SECONDS:
                item = f"{item} (+{format_elapsed(gap)})"
        elif latest is not None and latest[0] == at_ms:
            item = f"{item} (+{format_elapsed(latest[1] / _MS_PER_SECOND)})"
        items.append(item)
        previous = at_ms
    if latest is not None and latest[0] < timeline.turns_here_ms[0]:
        items.append(
            EARLIER_BREAK_NOTE.format(
                gap=format_elapsed(latest[1] / _MS_PER_SECOND), at=_at_minute(latest[0])
            )
        )
    return f"- {EARLIER_TURNS_LABEL}: {'; '.join(items)} {ACTIVITY_RECORD}"


def _human_line(now_s: float, timeline: TimelineSnapshot | None) -> str:
    if timeline is None:
        return f"- {HUMAN_LABEL}: {TIMELINE_UNAVAILABLE}"
    if timeline.last_human_ms is None:
        return f"- {HUMAN_LABEL}: {NONE_RECORDED} {ACTIVITY_RECORD}"
    return f"- {HUMAN_LABEL}: {_when(now_s, timeline.last_human_ms)} {ACTIVITY_RECORD}"


def _count(value: Any) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "?"


def _run_line(now_s: float, timeline: TimelineSnapshot | None, *, heartbeat: bool) -> str:
    label = RUN_LABEL_HEARTBEAT if heartbeat else RUN_LABEL
    if timeline is None:
        return f"- {label}: {TIMELINE_UNAVAILABLE}"
    if timeline.last_run is None:
        return f"- {label}: {NONE_RECORDED} {ACTIVITY_RECORD}"
    kind, at_ms, metadata = timeline.last_run
    if kind == "heartbeat_skipped":  # 入口被擋（同一原因連續被擋只記第一次，ADR-0318）
        reason = metadata.get("reason")
        return (
            f"- {label}: not started, refused since {_when(now_s, at_ms)}; "
            f"reason {reason if reason in _SKIP_REASONS else 'unknown'} {ACTIVITY_RECORD}"
        )
    end = metadata.get("end")
    started = metadata.get("started_ms")
    if end == "crashed" and isinstance(started, int) and not isinstance(started, bool):
        return (
            f"- {label}: did not finish (Touka stopped during it); started "
            f"{_when(now_s, started)}; found at restart {_when(now_s, at_ms)}; "
            f"{_count(metadata.get('pulses_total'))} pulses planned, crashed {ACTIVITY_RECORD}"
        )
    error = metadata.get("error")
    kind_note = f" ({error})" if end == "model_error" and error in KNOWN_ERROR_TYPES else ""
    detail = (
        f"{_count(metadata.get('pulses_done'))}/{_count(metadata.get('pulses_total'))} pulses, "
        f"{end if end in _END_REASONS else 'unknown'}{kind_note}"
    )
    return f"- {label}: ended {_when(now_s, at_ms)}, {detail} {ACTIVITY_RECORD}"


def _completed_item(now_s: float, kind: str, epoch_ms: int, subject: str, metadata: dict) -> str:
    """只用系統產生的 id 與過濾過的任務 id；不顯示任何描述文字。"""
    if kind == "task_done":
        what = f"task {safe_ref(metadata.get('task_id', '?'))} done"
    else:
        mission = safe_ref(subject[:_MISSION_REF_CHARS])
        what = f"mission {mission} {'finalized' if kind == 'mission_finalized' else 'completed'}"
    return f"{what} {_when(now_s, epoch_ms)}"


def running_subagents_line(soul: AgentIdentity, session_id: str | None, now_s: float) -> str | None:
    """還在跑的子代理（ADR-0318）；沒有就不加這一行（區塊和以前一樣）。

    標籤是模型給的，經 ``safe_ref``；只看記憶體裡的登記表。
    """
    running = subagent_registry.running_in(soul.agent_id, session_id)
    if not running:
        return None
    items = [
        f"{safe_ref(entry.label)} (started {_when(now_s, int(entry.started_at * _MS_PER_SECOND))})"
        for entry in running[:RUNNING_SUBAGENTS_SHOWN]
    ]
    extra = len(running) - RUNNING_SUBAGENTS_SHOWN
    if extra > 0:
        items.append(f"and {extra} more")
    return f"- {RUNNING_SUBAGENTS_LABEL}: {'; '.join(items)} {SERVER_MEMORY}"


def finished_subagents_line(
    soul: AgentIdentity, session_id: str | None, now_s: float
) -> str | None:
    """背景派出、最近結束的子代理（ADR-0319 PR 4）；沒有就不加這一行.

    標籤經 ``safe_ref``、結果只用固定值；附上讀結果的憑證（系統產生的）。讀過的也附：結果檔還在，
    之後的回合要全文時可以重讀。
    """
    finished = subagent_registry.finished_in(soul.agent_id, session_id, now_s)
    if not finished:
        return None
    items = [
        f"{safe_ref(entry.label)} "
        f"{entry.outcome if entry.outcome in _SUBAGENT_OUTCOMES else 'unknown'}, finished "
        f"{_when(now_s, int(entry.finished_at * _MS_PER_SECOND))}; "
        f"{RESULT_READ if entry.read else RESULT_UNREAD}: "
        f"{READ_RESULT_CALL.format(token=entry.token)}"
        for entry in finished
    ]
    return f"- {FINISHED_SUBAGENTS_LABEL}: {' | '.join(items)} {SERVER_MEMORY}"


async def _unreported_line(soul: AgentIdentity, now_s: float) -> str | None:
    """在家裡才加：其他對話有新活動、還沒回報（ADR-0319）；沒有就不加這一行.

    只放段數與最後活動時間，不放對話編號或內容；時間是對話紀錄的，可能來自使用者裝置。
    """
    try:
        unreported = await unreported_children(soul)
    except Exception:
        logger.exception("continuity: reading unreported conversations failed")
        return f"- {UNREPORTED_LABEL}: unknown (read failed)"
    if not unreported.count:
        return None
    when = (
        ""
        if unreported.latest_ms is None
        else f"; latest activity {_when(now_s, unreported.latest_ms)} "
        f"{CONVERSATION_RECORD_TIME_NOTE}"
    )
    return f"- {UNREPORTED_LABEL}: {unreported.count}{when}"


@dataclass(frozen=True)
class HeartbeatWake:
    """心跳這一輪是第幾個脈衝、被誰叫醒；入口放進 ctx，背景迴圈每個脈衝換上編號."""

    pulse: int = 0
    total: int = 0
    trigger: str | None = None
    schedule_mode: str | None = None
    rerun_started_at: float | None = None  # 被重跑的那次開始時間（秒，伺服器時鐘）


def _wake_lines(wake: HeartbeatWake, now_s: float) -> list[str]:
    if wake.pulse < 1:
        return []
    left = wake.total - wake.pulse
    woken = WAKE_TEXTS.get(wake.trigger or "", WAKE_UNSTATED)
    if wake.trigger == "schedule" and wake.schedule_mode in SCHEDULE_MODE_TEXTS:
        woken = f"{woken} ({SCHEDULE_MODE_TEXTS[wake.schedule_mode]})"
    after = PULSE_MORE.format(left=left) if left > 0 else PULSE_LAST
    lines = [
        PULSE_LINE.format(pulse=wake.pulse, total=wake.total, after=after),
        f"- {WOKEN_LABEL}: {woken}",
    ]
    if wake.trigger == RETRIGGER:
        started = wake.rerun_started_at
        when = (
            RERUN_STARTED.format(ago=_when(now_s, int(started * _MS_PER_SECOND)))
            if started else RERUN_START_UNKNOWN
        )
        first = RERUN_FIRST.format(when=when)
        lines.append(first if wake.pulse == 1 else RERUN_LATER.format(pulse=wake.pulse))
    return lines


async def _at_home(soul: AgentIdentity, session_id: str) -> bool | None:
    """這段對話是不是這個靈魂唯一的家；讀不到或有兩個家（資料矛盾）回 None."""
    try:
        homes = await session_service.home_session_ids(soul)
        return None if len(homes) > 1 else homes == [session_id]
    except Exception:
        logger.exception("continuity: reading the home failed")
        return None


def _conversation_line(
    session_id: str, at_home: bool | None, *, heartbeat: bool, sns: bool
) -> str:
    if sns:
        kind = KIND_SNS
    elif is_heartbeat_channel(session_id):
        kind = KIND_HEARTBEAT_CHANNEL
    elif at_home is None:
        kind = KIND_UNKNOWN
    elif at_home:
        kind = KIND_HEARTBEAT_HOME if heartbeat else KIND_HOME
    else:
        kind = KIND_CHILD if can_be_reported(session_id) else KIND_NOT_REPORTED
    return f"- {CONVERSATION_LABEL}: {kind}"


def _conversation_id_line(session_id: str, at_home: bool | None) -> str | None:
    """子對話與心跳頻道（會回報給家、而且不是家）寫出自己的編號；家、讀不到家、不回報的不寫."""
    if at_home is not False or not can_be_reported(session_id):
        return None
    return f"- {CONVERSATION_ID_TEMPLATE.format(id=session_id)}"


def _elsewhere_line(now_s: float, timeline: TimelineSnapshot | None) -> str | None:
    """家裡：最近在哪些群聊、SNS 說過話（次數與最後一次）；都沒有或讀不到就不加."""
    if timeline is None:
        return None
    parts = [
        f"{ELSEWHERE_PLACES[kind]} ({count} turn{'' if count == 1 else 's'}, "
        f"last {_when(now_s, last_ms)})"
        for kind, count, last_ms in timeline.elsewhere
        if kind in ELSEWHERE_PLACES and count > 0
    ]
    if not parts:
        return None
    return f"- {ELSEWHERE_LABEL} {' and '.join(parts)}; {ELSEWHERE_NOT_HERE} {ACTIVITY_RECORD}"


def _recent_line(now_s: float, timeline: TimelineSnapshot | None) -> str:
    if timeline is None:
        return f"- {RECENT_LABEL}: {TIMELINE_UNAVAILABLE}"
    if not timeline.recent:
        return f"- {RECENT_LABEL}: {NONE_RECORDED} {ACTIVITY_RECORD}"
    items = "; ".join(_completed_item(now_s, *item) for item in timeline.recent)
    return f"- {RECENT_LABEL}: {items} {ACTIVITY_RECORD}"


def _records_start_line(timeline: TimelineSnapshot | None) -> str | None:
    """只在有「沒有紀錄」時告訴模型可信紀錄從哪裡開始（之前的沒有記，不代表沒發生）。"""
    if timeline is None or timeline.records_start_ms is None:
        return None
    if timeline.last_human_ms is not None and timeline.last_run is not None and timeline.recent:
        return None
    return (
        f"- {RECORDS_START_LABEL} {_at_minute(timeline.records_start_ms)} (Asia/Taipei); "
        "anything earlier was not recorded."
    )


def _ago(now_s: float, then_s: float | None) -> str:
    return f"{format_elapsed(elapsed_seconds(now_s, then_s, 's'))} ago" if then_s else "unknown"


async def _mission_line(identity: Any, now_s: float) -> str:
    """和 ToolLayer 讀任務看板用同一個身分（ctx 的身分）。沒有身分的只有 SNS，SNS 不讀看板。"""
    try:
        mission = await MissionManager().get_current_mission(identity)
    except Exception:
        logger.exception("continuity: reading mission board failed")
        return f"- {MISSION_LABEL}: unknown (read failed)"
    if not mission or mission.status == "DONE":  # 與 ToolLayer 注入 mission_awareness 同條件
        return f"- {MISSION_LABEL}: {NO_ACTIVE_MISSION} {MISSION_BOARD}"
    # 任務 id 是模型取的：這個區塊標成事實，id 只顯示過濾過的參照（ADR-0285）
    doing = [f"{safe_ref(t.id)} (updated {_ago(now_s, t.updated_at)})" for t in mission.tasks
             if t.status == "DOING"]
    return (
        f"- {MISSION_LABEL}: updated {_ago(now_s, mission.updated_at)}; in progress: "
        f"{', '.join(doing) or 'none'}; details are in the <{MISSION_AWARENESS}> block. "
        f"{MISSION_BOARD}"
    )


async def build_continuity_block(
    *,
    soul: AgentIdentity,
    session_id: str | None,
    identity: Any,
    incognito: bool,
    heartbeat: bool = False,
    sns: bool = False,
    wake: HeartbeatWake | None = None,
    current_message_id: Any = None,
    now_s: float | None = None,
) -> str:
    """組出 ``<continuity>`` 區塊。

    無痕只放現在時間，不讀事件、不讀任務、不留紀錄。心跳不放「這段對話上一輪」、不更新記憶體
    紀錄（家對話和人類共用 session id）。``now_s`` 由呼叫端傳入時用同一個時間（心跳的時間前綴）。
    SNS 觸發的請求（外人）不加其他對話的動態與最近結束的子代理（ADR-0319），也不帶任務看板與
    最近完成的任務（ADR-0276 2026-11-24 修訂）。
    """
    now_s = get_current_epoch() if now_s is None else now_s
    lines = [HEADER, current_time_line(now_s)]
    if incognito:
        lines.append(f"- {CONVERSATION_LABEL}: {KIND_INCOGNITO}")
    else:
        at_home = await _at_home(soul, session_id) if session_id and not sns else False
        if session_id:
            lines.append(_conversation_line(session_id, at_home, heartbeat=heartbeat, sns=sns))
            own_id = None if sns else _conversation_id_line(session_id, at_home)
            if own_id is not None:
                lines.append(own_id)
        timeline = await _read_timeline(
            soul,
            session_id,
            current_message_id,
            now_s,
            elsewhere=at_home is True,
            turns=not heartbeat and not sns,
        )
        if heartbeat:
            lines.append(HEARTBEAT_LINE)
            lines.extend(_wake_lines(wake or HeartbeatWake(), now_s))
            lines.append(f"- {HEARTBEAT_SCOPE_SOUL}")
        else:
            lines.append(await _previous_turn_line(soul, session_id, now_s, timeline))
            earlier = _earlier_turns_line(timeline)
            if earlier is not None:
                lines.append(earlier)
        lines.append(_human_line(now_s, timeline))
        lines.append(_run_line(now_s, timeline, heartbeat=heartbeat))
        if not sns:  # 外人觸發的回合不帶看板與最近完成的任務（照無痕；ToolLayer 也不放看板）
            lines.append(_recent_line(now_s, timeline))
            lines.append(await _mission_line(identity, now_s))
        running = running_subagents_line(soul, session_id, now_s)
        if running is not None:
            lines.append(running)
        finished = None if sns else finished_subagents_line(soul, session_id, now_s)
        if finished is not None:
            lines.append(finished)
        unreported = await _unreported_line(soul, now_s) if at_home else None
        if unreported is not None:
            lines.append(unreported)
        elsewhere = _elsewhere_line(now_s, timeline) if at_home else None
        if elsewhere is not None:
            lines.append(elsewhere)
        start = _records_start_line(timeline)
        if start is not None:
            lines.append(start)
    return wrap_block(CONTINUITY, "\n".join(lines))


def build_heartbeat_minimal_continuity(
    wake: HeartbeatWake | None, *, now_s: float | None = None
) -> str:
    """不是靈魂的代理人跑心跳（例如 DebugAgent）：時間、沒人在場、第幾個脈衝與被誰叫醒。

    不讀活動紀錄、看板與家：那些是靈魂的（ADR-0323；ADR-0267 原本只給靈魂）。
    """
    now_s = get_current_epoch() if now_s is None else now_s
    lines = [
        HEADER,
        current_time_line(now_s),
        HEARTBEAT_LINE,
        *_wake_lines(wake or HeartbeatWake(), now_s),
        f"- {HEARTBEAT_SCOPE_OTHER}",
    ]
    return wrap_block(CONTINUITY, "\n".join(lines))


def inject_heartbeat_minimal_continuity(
    request: Any, ctx: Any, *, now_s: float | None = None
) -> None:
    """把精簡版附在這次請求最後一則 user 訊息尾端（不寫回對話樹）。"""
    wake = ctx.get("heartbeat_wake")
    block = build_heartbeat_minimal_continuity(
        wake if isinstance(wake, HeartbeatWake) else None, now_s=now_s
    )
    append_to_last_user(request, "\n\n" + block)


async def inject_continuity(
    request: Any, ctx: Any, soul: AgentIdentity, *, now_s: float | None = None
) -> None:
    """把 ``<continuity>`` 附在這次請求最後一則 user 訊息尾端（呼叫端負責靈魂路閘門）。"""
    wake = ctx.get("heartbeat_wake")
    block = await build_continuity_block(
        soul=soul,
        session_id=ctx.get("session_id"),
        identity=ctx.get("identity"),
        incognito=bool(ctx.get("incognito")),
        heartbeat=bool(ctx.get("heartbeat_run")),
        sns=bool(ctx.get("sns_source")),
        wake=wake if isinstance(wake, HeartbeatWake) else None,
        current_message_id=ctx.get("source_message_id"),
        now_s=now_s,
    )
    append_to_last_user(request, "\n\n" + block)
