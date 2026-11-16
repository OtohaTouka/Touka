"""小幫手自己的底線（ADR-0317）：Touka 改不到，碰到任一條就改在小幫手視窗按。

數字寫在這裡，協議裡沒有任何欄位能改它們。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from touka_local.paths import parts_of, same_path

if TYPE_CHECKING:
    from touka_local.operations import Plan

MAX_MODE_ITEMS: Final = 20
MAX_MODE_ENTRIES: Final = 200
HOURLY_MODE_BATCHES: Final = 10
HOURLY_MODE_ITEMS: Final = 100
HOURLY_MODE_ENTRIES: Final = 300
HOUR_SECONDS: Final = 3600.0
VACATED_SECONDS: Final = 7 * 24 * 3600.0
# 審查員只看參數、看不到資料夾裡有什麼，這幾種一律給人看（hidden 太常見，不列）
MARKS_NEEDING_WINDOW: Final = frozenset({"secret", "executable", "many_items"})

REASON_NO_DECISION: Final = "no_decision"
REASON_NOT_FOLLOWING: Final = "not_following"
REASON_JOURNAL_UNREADABLE: Final = "journal_unreadable"
REASON_TOO_MANY_ITEMS: Final = "too_many_items"
REASON_TOO_MANY_ENTRIES: Final = "too_many_entries"
REASON_MARKED: Final = "marked"
REASON_HOURLY_LIMIT: Final = "hourly_limit"
REASON_CROSS_FOLDER: Final = "cross_folder"
REASON_RECENTLY_VACATED: Final = "recently_vacated"


@dataclass(frozen=True)
class ModeBatch:
    """一批照 Touka 的模式做掉的清單（不管每項做沒做成，整批都算）。"""

    at: float
    items: int
    entries: int


@dataclass(frozen=True)
class History:
    """從紀錄整理出來的、底線要看的東西。``readable`` 是 False 時一律跳視窗。

    ``unreadable`` 是讀不懂的紀錄檔，主視窗顯示給人看；整個紀錄資料夾讀不到時是空的。
    """

    mode_batches: tuple[ModeBatch, ...] = ()
    vacated: tuple[tuple[str, ...], ...] = ()
    reset_at: float | None = None
    readable: bool = True
    unreadable: tuple[str, ...] = ()


def vacated_key(path: str) -> tuple[str, ...]:
    """7 天比對用的 key：先轉大寫再 casefold，比只 casefold 更寬，寧可多擋。"""
    return tuple(part.upper().casefold() for part in parts_of(path))


def is_vacated(path: str, vacated: tuple[tuple[str, ...], ...]) -> bool:
    """``path`` 等於或位在近 7 天被搬走、刪掉的位置底下。"""
    key = vacated_key(path)
    return any(key[: len(gone)] == gone for gone in vacated)


def holds_vacated(path: str, vacated: tuple[tuple[str, ...], ...]) -> bool:
    """``path`` 本身或它底下有近 7 天被搬走、刪掉的位置（搬走資料夾時空位不能跟著消失）。"""
    key = vacated_key(path)
    return any(gone[: len(key)] == key for gone in vacated)


def plan_entries(plan: Plan) -> int:
    """整份清單碰到的檔案加資料夾總數。"""
    return sum(1 + item.contained for item in plan.items)


def _over_hourly(plan: Plan, history: History, now: float) -> bool:
    since = now - HOUR_SECONDS
    if history.reset_at is not None and history.reset_at <= now:
        since = max(since, history.reset_at)
    # 時間在未來的紀錄也算：時鐘被往回調，不能因此把總量歸零
    recent = [batch for batch in history.mode_batches if batch.at >= since]
    return (
        len(recent) + 1 > HOURLY_MODE_BATCHES
        or sum(batch.items for batch in recent) + len(plan.items) > HOURLY_MODE_ITEMS
        or sum(batch.entries for batch in recent) + plan_entries(plan) > HOURLY_MODE_ENTRIES
    )


def window_reason(
    plan: Plan, history: History, *, following: bool, decision_ok: bool, now: float
) -> str | None:
    """要不要改在小幫手視窗按；回 None 才能照 Touka 的決定直接做。"""
    if not decision_ok:
        return REASON_NO_DECISION
    if not following:
        return REASON_NOT_FOLLOWING
    if not history.readable:
        return REASON_JOURNAL_UNREADABLE
    if len(plan.items) > MAX_MODE_ITEMS:
        return REASON_TOO_MANY_ITEMS
    if plan_entries(plan) > MAX_MODE_ENTRIES:
        return REASON_TOO_MANY_ENTRIES
    if any(MARKS_NEEDING_WINDOW.intersection(item.unusual) for item in plan.items):
        return REASON_MARKED
    if _over_hourly(plan, history, now):
        return REASON_HOURLY_LIMIT
    for item in plan.items:
        if item.destination_full is None:
            continue
        if item.destination_root is None or not same_path(item.source_root, item.destination_root):
            return REASON_CROSS_FOLDER
        if is_vacated(item.destination_full, history.vacated) or holds_vacated(
            item.source_full, history.vacated
        ):
            return REASON_RECENTLY_VACATED
    return None
