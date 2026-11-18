"""SNS 頻道最近的對話（ADR-0319 PR 4，SNS-2）.

外面的人說「剛剛那個連結再貼一次」時，以前 AI 只看得到這一句。SNS 用獨立頻道（不是家）時，這一輪前面
帶這個頻道最近 ``SNS_HISTORY_WINDOW_MS``、最多 ``SNS_HISTORY_MAX_MESSAGES`` 則、估算不超過
``SNS_HISTORY_TOKEN_BUDGET`` 的對話，從新往舊收。整段是舊紀錄：每則標時間、包進歷史資料界線、
和家原文同一套中和；助理那則只帶簡短工具紀錄（名稱與固定的成敗值，不帶參數與結果）。

由主線（SoulLayer）加：使用者原意（MonitorLayer）與 UserPromptSubmit hook 在它之前，只看到這一輪。
第一則從外人那則開始；相鄰兩則外人訊息（回覆失敗時只存了外人那則）合成一則；最後一則是外人訊息時
註明沒有回覆，不和這一輪合併，相鄰角色交給轉接器。家模式已帶家原文、群聊沒有對話編號，都不加。
"""

from __future__ import annotations

from typing import Any

from memory_server.utils.legacy_time import to_epoch_ms

from src.services.soul_service import mark_history, stamp_message_times
from src.utils.async_db import session_service
from src.utils.history_manager import TreeMessageManager
from src.utils.session_scope import is_sns_channel
from src.utils.time_utils import get_current_epoch
from src.utils.token_estimator import estimate_messages_tokens
from src.utils.tool_records import RECORD_BRIEF, annotate_history

SNS_CHANNEL_HISTORY_SOURCE = "sns_channel"
SNS_HISTORY_WINDOW_MS = 24 * 3600 * 1000
SNS_HISTORY_MAX_MESSAGES = 10
SNS_HISTORY_TOKEN_BUDGET = 4000
NO_REPLY_NOTE = "（這一則沒有回覆）"
_DIALOGUE_ROLES = frozenset({"user", "assistant"})
_MS_PER_SECOND = 1000


def channel_history(session: dict[str, Any], now_ms: int) -> list[dict[str, Any]]:
    """要送的頻道歷史（只有角色與內容）；沒有就回空清單."""
    since = now_ms - SNS_HISTORY_WINDOW_MS
    recent = [
        m
        for m in TreeMessageManager.get_linear_path(session.get("messages"))
        if m.get("role") in _DIALOGUE_ROLES and (to_epoch_ms(m.get("timestamp")) or 0) >= since
    ][-SNS_HISTORY_MAX_MESSAGES:]
    kept = _newest_within_budget(stamp_message_times(annotate_history(recent, level=RECORD_BRIEF)))
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    merged = _merge_adjacent_users(kept)
    if not merged:
        return []
    last = merged[-1]
    if last["role"] == "user" and isinstance(last["content"], str):
        merged[-1] = {**last, "content": f"{last['content']}\n{NO_REPLY_NOTE}"}
    marked = mark_history(merged, source=SNS_CHANNEL_HISTORY_SOURCE)
    return [{"role": m["role"], "content": m["content"]} for m in marked]


def _newest_within_budget(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """從最新的往前收，估算超過額度就停."""
    remaining = SNS_HISTORY_TOKEN_BUDGET
    start = len(messages)
    for message in reversed(messages):
        cost = estimate_messages_tokens([message])
        if cost > remaining:
            break
        remaining -= cost
        start -= 1
    return messages[start:]


def _merge_adjacent_users(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for message in messages:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and previous["role"] == message["role"] == "user"
            and isinstance(previous["content"], str)
            and isinstance(message["content"], str)
        ):
            merged[-1] = {**previous, "content": f"{previous['content']}\n\n{message['content']}"}
        else:
            merged.append(message)
    return merged


async def inject_channel_history(request: Any, session_id: str) -> None:
    """SNS 獨立頻道：把這個頻道最近的對話放在這一輪（最後一則）前面；其他對話不動."""
    if not is_sns_channel(session_id):
        return
    session = await session_service.get_session_by_id(session_id)
    now_ms = int(get_current_epoch() * _MS_PER_SECOND)
    history = channel_history(session, now_ms) if session else []
    if history:
        request.messages[-1:-1] = history
