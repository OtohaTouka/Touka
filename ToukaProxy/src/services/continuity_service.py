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
- 上次有人類訊息、上次自主執行、最近完成：可信活動事件（``utils/continuity_events_db``）。
- 任務看板：MissionManager 唯讀（SSOT），只補 ``<mission_awareness>`` 沒有的時間。
- 可信紀錄起點：只在有「沒有紀錄」時出現。
事件表讀不到時照實寫讀不到，區塊照樣送出（不丟例外）。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Literal

from memory_server.services.mission_manager import MissionManager
from memory_server.utils.legacy_time import to_epoch_ms

from src.adapters.content_normalize import append_text_part
from src.models.identity import AgentIdentity
from src.services.agent_service import agent_service
from src.services.continuity_events import safe_ref
from src.services.prompt_blocks import CONTINUITY, MISSION_AWARENESS, wrap_block
from src.utils.async_db import session_service
from src.utils.continuity_events_db import read_snapshot
from src.utils.history_manager import TreeMessageManager
from src.utils.time_utils import (
    CONVERSATION_RECORD_TIME_NOTE,
    format_epoch_to_taipei,
    get_current_epoch,
)

if TYPE_CHECKING:
    from src.utils.continuity_events_db import TimelineSnapshot

logger = logging.getLogger(__name__)

# 記憶體最多記幾個 (agent, session) 的上一次前景請求時間；超過丟最舊的。
MAX_TRACKED_SESSIONS = 1024
_MS_PER_SECOND = 1000
_MINUTE = 60
_HOUR = 3600
_DAY = 86400
# 最近完成：看幾天、最多幾筆
RECENT_WINDOW_DAYS = 7
RECENT_LIMIT = 3
# 顯示到分鐘（format_epoch_to_taipei 回 "YYYY-MM-DD HH:MM:SS"）
_MINUTE_PRECISION = 16
_MISSION_REF_CHARS = 8

HEADER = (
    "Added by the Touka OS harness to this turn, not written by the user. Facts only; "
    "refreshed on every request and never saved. When you save any of this to memory, write "
    'absolute dates (for example 2026-10-15), not relative times such as "3 hours ago".'
)
HEARTBEAT_LINE = "- Run type: autonomous heartbeat; no human is present in this run."
PREVIOUS_TURN_LABEL = "Since your previous turn in this conversation"
UNKNOWN_PREVIOUS = "unknown (no record of when the previous turn happened)"
HUMAN_LABEL = "Last message from a human (any conversation)"
RUN_LABEL = "Last autonomous run"
RUN_LABEL_HEARTBEAT = "Previous autonomous run (before this one)"
RECENT_LABEL = (
    f"Recently completed (last {RECENT_WINDOW_DAYS} days, newest first, max {RECENT_LIMIT})"
)
RECORDS_START_LABEL = "Activity records start"
MISSION_LABEL = "Mission board"
NO_ACTIVE_MISSION = "no active mission"
NONE_RECORDED = "none recorded"
TIMELINE_UNAVAILABLE = "unavailable (the activity record could not be read)"
# 來源標示
SERVER_CLOCK = "(server clock)"
SERVER_MEMORY = "(server memory)"
ACTIVITY_RECORD = "(activity record)"
MISSION_BOARD = "(mission board)"
_END_REASONS = frozenset(
    {"completed", "interrupted", "quota", "budget", "no_model", "error", "cancelled"}
)

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
    soul: AgentIdentity, session_id: str | None, current_message_id: Any, now_s: float
) -> TimelineSnapshot | None:
    """一次讀完這個靈魂的活動事件；讀不到回 None（照實寫讀不到，不丟例外）。"""
    try:
        return await read_snapshot(
            soul,
            session_id=session_id,
            exclude_message_id=current_message_id if isinstance(current_message_id, str) else None,
            since_ms=int((now_s - RECENT_WINDOW_DAYS * _DAY) * _MS_PER_SECOND),
            limit=RECENT_LIMIT,
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
    ended_ms, metadata = timeline.last_run
    end = metadata.get("end")
    detail = (
        f"{_count(metadata.get('pulses_done'))}/{_count(metadata.get('pulses_total'))} pulses, "
        f"{end if end in _END_REASONS else 'unknown'}"
    )
    return f"- {label}: ended {_when(now_s, ended_ms)}, {detail} {ACTIVITY_RECORD}"


def _completed_item(now_s: float, kind: str, epoch_ms: int, subject: str, metadata: dict) -> str:
    """只用系統產生的 id 與過濾過的任務 id；不顯示任何描述文字。"""
    if kind == "task_done":
        what = f"task {safe_ref(metadata.get('task_id', '?'))} done"
    else:
        mission = safe_ref(subject[:_MISSION_REF_CHARS])
        what = f"mission {mission} {'finalized' if kind == 'mission_finalized' else 'completed'}"
    return f"{what} {_when(now_s, epoch_ms)}"


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


def _board_identity(identity: Any, soul: AgentIdentity) -> AgentIdentity:
    """和 ToolLayer 讀任務看板用同一個身分：ctx 的身分，沒有才依 agent 註冊角色建。"""
    if isinstance(identity, AgentIdentity):
        return identity
    agent_info = agent_service.identify_agent(soul.agent_id) or {"role": "Base"}
    return AgentIdentity.for_role(soul.agent_id, agent_info.get("role", "Base"), capabilities=[])


async def _mission_line(identity: Any, soul: AgentIdentity, now_s: float) -> str:
    try:
        mission = await MissionManager().get_current_mission(_board_identity(identity, soul))
    except Exception:
        logger.exception("continuity: reading mission board failed")
        return f"- {MISSION_LABEL}: unknown (read failed)"
    if not mission or mission.status == "DONE":  # 與 ToolLayer 注入 mission_awareness 同條件
        return f"- {MISSION_LABEL}: {NO_ACTIVE_MISSION} {MISSION_BOARD}"
    # 任務 id 是模型取的：這個區塊標成事實，id 只顯示過濾過的參照（ADR-0284）
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
    current_message_id: Any = None,
    now_s: float | None = None,
) -> str:
    """組出 ``<continuity>`` 區塊。

    無痕只放現在時間，不讀事件、不讀任務、不留紀錄。心跳不放「這段對話上一輪」、不更新記憶體
    紀錄（家對話和人類共用 session id）。``now_s`` 由呼叫端傳入時用同一個時間（心跳的時間前綴）。
    """
    now_s = get_current_epoch() if now_s is None else now_s
    time_str, weekday = format_epoch_to_taipei(now_s)
    lines = [HEADER, f"- Current time (Asia/Taipei): {time_str} ({weekday}) {SERVER_CLOCK}"]
    if not incognito:
        timeline = await _read_timeline(soul, session_id, current_message_id, now_s)
        if heartbeat:
            lines.append(HEARTBEAT_LINE)
        else:
            lines.append(await _previous_turn_line(soul, session_id, now_s, timeline))
        lines.append(_human_line(now_s, timeline))
        lines.append(_run_line(now_s, timeline, heartbeat=heartbeat))
        lines.append(_recent_line(now_s, timeline))
        lines.append(await _mission_line(identity, soul, now_s))
        start = _records_start_line(timeline)
        if start is not None:
            lines.append(start)
    return wrap_block(CONTINUITY, "\n".join(lines))


def _append_to_last_user(request: Any, text: str) -> None:
    """接在最後一則訊息（須是 user）的尾端；不是 user 就不附（不改動歷史訊息）。"""
    messages = getattr(request, "messages", None)
    if not messages:
        return
    last = messages[-1]
    is_dict = isinstance(last, dict)
    if (last.get("role") if is_dict else getattr(last, "role", None)) != "user":
        return
    # ToukaChat 的多模態 JSON 字串先還原成 list，接一個 part（串接字串會讓轉接層認不出圖片）
    updated = append_text_part(
        last.get("content") if is_dict else getattr(last, "content", None), text
    )
    if updated is None:
        return
    if is_dict:
        last["content"] = updated
    else:
        last.content = updated


async def inject_continuity(
    request: Any, ctx: Any, soul: AgentIdentity, *, now_s: float | None = None
) -> None:
    """把 ``<continuity>`` 附在這次請求最後一則 user 訊息尾端（呼叫端負責靈魂路閘門）。"""
    block = await build_continuity_block(
        soul=soul,
        session_id=ctx.get("session_id"),
        identity=ctx.get("identity"),
        incognito=bool(ctx.get("incognito")),
        heartbeat=bool(ctx.get("heartbeat_run")),
        current_message_id=ctx.get("source_message_id"),
        now_s=now_s,
    )
    _append_to_last_user(request, "\n\n" + block)
