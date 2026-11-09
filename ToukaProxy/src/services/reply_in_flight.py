"""不走可恢復串流的回覆正在跑（SNS 家模式）：換家交接要等它存好（ADR-0307）.

都在事件迴圈裡改，不需要鎖；同一段對話可能同時有好幾則外部訊息在回。
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_running: Counter[str] = Counter()


@contextmanager
def replying(session_id: str | None) -> Iterator[None]:
    """這段期間算這段對話有回覆在跑（沒有對話時什麼都不做）."""
    if session_id is None:
        yield
        return
    _running[session_id] += 1
    try:
        yield
    finally:
        _running[session_id] -= 1
        if _running[session_id] <= 0:
            del _running[session_id]


def is_replying(session_id: str) -> bool:
    """這段對話還有回覆在跑."""
    return session_id in _running
