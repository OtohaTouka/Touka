"""看板每一格的狀態圖示、狀態字與原因（ADR-0316 2026-11-16 PR 2 補記）。

系統提示裡的看板摘要（``<mission_awareness>``）與 ``mission_tools`` 的 ``get_board`` 共用，
說法只有這一個來源。純函式：不跳脫（兩個呼叫端情境不同，由呼叫端處理）。
"""

from typing import Any

from src.utils.model_text import single_line

TASK_ICONS: dict[str, str] = {
    "TODO": "⬜",
    "DOING": "⏳",
    "DONE": "✅",
    "BLOCKED": "⛔",
    "FAILED": "❌",
}
# 只有卡住或失敗的才寫原因
_REASON_STATUSES = frozenset({"BLOCKED", "FAILED"})
REASON_LIMIT = 120


def task_status_parts(status: Any) -> tuple[str, str]:
    """（圖示, 狀態字）。看不懂的值不原樣寫出（這段會進 system）。"""
    word = str(status)
    if word not in TASK_ICONS:
        return TASK_ICONS["TODO"], "?"
    return TASK_ICONS[word], word


def task_reason(task: Any, limit: int = REASON_LIMIT) -> str | None:
    """卡住或失敗的原因：先看 blocker_detail，沒有再看 test_log；壓成一行，超過截斷。"""
    if str(getattr(task, "status", "")) not in _REASON_STATUSES:
        return None
    raw = getattr(task, "blocker_detail", None) or getattr(task, "test_log", None)
    if not raw:
        return None
    text = single_line(str(raw)).strip()
    return text if len(text) <= limit else f"{text[:limit]}…"
