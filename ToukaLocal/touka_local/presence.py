"""小幫手退到背景：什麼時候顯示、叫出或浮出主視窗（ADR-0312 修訂，2026-11）。

只有判斷，沒有視窗也沒有系統呼叫，CI 的 Linux 也能測。視窗的動作在 ``app.py``，
命名事件在 ``winapi.py``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from touka_local.service import STATE_EXPOSED, STATE_UNPAIRED, STATE_UPGRADE_NEEDED

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

# 只有開機啟動檔帶這個旗標
BACKGROUND_FLAG: Final = "--background"

# 同一種狀況消失後又出現，距上次提醒滿這麼久才再提醒（Touka 觸發得到其中幾種）
ATTENTION_COOLDOWN_SECONDS: Final = 600

# 第二個小幫手要怎麼結束
QUIET: Final = "quiet"
SHOWN: Final = "shown"
ALREADY_OPEN: Final = "already_open"

# 每一輪對主視窗做的事
SHOW: Final = "show"
SURFACE: Final = "surface"
NONE: Final = "none"

# 按主視窗的 ×
HIDE: Final = "hide"
MINIMIZE: Final = "minimize"

# 需要人處理的狀況：服務狀態之外的兩種
REASON_OPEN_PAUSED: Final = "open_paused"
REASON_RECORDS_UNREADABLE: Final = "records_unreadable"
_ATTENTION_STATES: Final = frozenset({STATE_UNPAIRED, STATE_UPGRADE_NEEDED, STATE_EXPOSED})


def wants_background(argv: Iterable[str]) -> bool:
    """啟動參數裡有沒有開機啟動的旗標；只認完全一樣的寫法。"""
    return BACKGROUND_FLAG in argv


def visible_at_start(*, background: bool, paired: bool, show_ready: bool) -> bool:
    """開機啟動、已經配對、而且之後叫得出來，才不顯示主視窗。"""
    return not (background and paired and show_ready)


def second_instance_action(*, background: bool, signalled: bool) -> str:
    """已經有一個小幫手在跑時，這一個怎麼結束。"""
    if background:
        return QUIET
    return SHOWN if signalled else ALREADY_OPEN


def attention_reasons(*, state: str, open_paused: bool, records_readable: bool) -> frozenset[str]:
    """目前需要人處理的狀況。連不上 Touka 是平常的事，不算。"""
    reasons = {state} & _ATTENTION_STATES
    if open_paused:
        reasons.add(REASON_OPEN_PAUSED)
    if not records_readable:
        reasons.add(REASON_RECORDS_UNREADABLE)
    return frozenset(reasons)


class AttentionTracker:
    """狀況剛出現時提醒一次；一直在不再提醒；消失再出現要距上次提醒滿冷卻時間。

    每一輪都要呼叫，主視窗開著時也一樣：開著時出現的狀況算已經提醒過。
    """

    def __init__(
        self, clock: Callable[[], float], cooldown: float = ATTENTION_COOLDOWN_SECONDS
    ) -> None:
        self._clock = clock
        self._cooldown = cooldown
        self._present: frozenset[str] = frozenset()
        self._raised_at: dict[str, float] = {}

    def check(self, reasons: frozenset[str]) -> bool:
        """有沒有該提醒的新狀況。"""
        now = self._clock()
        new = reasons - self._present
        self._present = reasons
        due = [
            reason
            for reason in new
            if reason not in self._raised_at or now - self._raised_at[reason] >= self._cooldown
        ]
        for reason in due:
            self._raised_at[reason] = now
        return bool(due)


def window_action(*, show_requested: bool, attention: bool, withdrawn: bool) -> str:
    """這一輪對主視窗做什麼：有人再開一次就叫出；有新狀況而且收著，就最小化浮出。"""
    if show_requested:
        return SHOW
    if attention and withdrawn:
        return SURFACE
    return NONE


def close_button_action(*, show_ready: bool) -> str:
    """按 ×：叫得出來才收起；叫不出來只最小化，免得小幫手在背景卻再也找不到。"""
    return HIDE if show_ready else MINIMIZE
