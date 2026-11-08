"""每個靈魂一把鎖：交接、子對話回報、前情合併一次只做一件（ADR-0305、ADR-0307）.

鎖的順序：靈魂鎖在最外層 → 剪裁的對話鎖 → 存檔的對話鎖 → SQLite 交易；拿著 SQLite 交易時不等
asyncio 鎖，模型呼叫一律在交易外。
"""

from __future__ import annotations

import asyncio

_SOUL_LOCKS: dict[str, asyncio.Lock] = {}


def soul_lock(agent: str) -> asyncio.Lock:
    """這個靈魂（正式 ID）的鎖."""
    lock = _SOUL_LOCKS.get(agent)
    if lock is None:
        lock = _SOUL_LOCKS[agent] = asyncio.Lock()
    return lock
