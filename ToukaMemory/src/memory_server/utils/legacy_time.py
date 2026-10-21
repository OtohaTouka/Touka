"""舊對話與記憶的時間：秒統一成毫秒，給模型看的時間一律台北時間並標時區。

純函式：只做轉換與格式化，不取現在時間（現在時間一律經 ``config_utils.get_current_epoch``）。
台灣自 1980 年起沒有日光節約，固定 UTC+8 與 Asia/Taipei 時區規則結果相同，也不依賴 tzdata；
時區名稱與 ``<continuity>`` 區塊相同，同一時刻在舊對話、隨機回想、查記憶與 continuity
顯示同一個台北時間（ADR-0258）。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

TAIPEI_TZ_NAME = "Asia/Taipei"
_TAIPEI = timezone(timedelta(hours=8), TAIPEI_TZ_NAME)
# 小於此值的數字時間戳視為秒（1e11 秒是公元 5138 年；1e11 毫秒是 1973 年 3 月）。
SECONDS_CUTOFF = 100_000_000_000
_MS_PER_SECOND = 1000


def to_epoch_ms(value: Any) -> int | None:
    """秒或毫秒時間戳統一成毫秒整數；無法判讀（空值、非正數、非數字、無時區文字）回 None。"""
    if isinstance(value, str):
        return _text_to_epoch_ms(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return round(value * _MS_PER_SECOND if value < SECONDS_CUTOFF else value)


def _text_to_epoch_ms(text: str) -> int | None:
    """數字字串照數字讀；ISO 8601 只收帶時區的（沒有時區就不知道是哪裡的時間）。"""
    stripped = text.strip()
    try:
        return to_epoch_ms(float(stripped))
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(stripped)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.timestamp() <= 0:
        return None
    return round(moment.timestamp() * _MS_PER_SECOND)


def format_taipei(value: Any, *, with_time: bool = True, unknown: str = "unknown time") -> str:
    """給模型看的時間，例如 ``2026-05-13 18:00 Asia/Taipei``；無法判讀回 ``unknown``。"""
    epoch_ms = to_epoch_ms(value)
    if epoch_ms is None:
        return unknown
    try:
        moment = datetime.fromtimestamp(epoch_ms / _MS_PER_SECOND, _TAIPEI)
    except (OverflowError, OSError, ValueError):
        return unknown
    pattern = "%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d"
    return f"{moment.strftime(pattern)} {TAIPEI_TZ_NAME}"


def taipei_day_start(date_text: str) -> int:
    """``YYYY-MM-DD`` 當成台北那天 00:00，回 Unix 秒；格式不對丟 ValueError。"""
    day = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=_TAIPEI)
    return int(day.timestamp())


def seconds_range_to_ms(
    start_s: int | None, end_s: int | None
) -> tuple[int | None, int | None]:
    """含頭含尾的秒範圍換成毫秒範圍；結尾那一秒整秒都算進去。"""
    start_ms = None if start_s is None else start_s * _MS_PER_SECOND
    end_ms = None if end_s is None else (end_s + 1) * _MS_PER_SECOND - 1
    return start_ms, end_ms
