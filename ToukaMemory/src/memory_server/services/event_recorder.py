"""記憶的可信活動事件：存、忘、一步取代在事實已提交後各記一筆.

記錄器由 Proxy 在啟動時登記（``register_event_recorder``），Memory 不 import Proxy。
沒登記時（Proxy 沒啟動、單元測試）什麼都不做。記錄器出任何錯都在這裡接住、只記 warning，
不影響記憶工具的回傳。只記 id，不記內容。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from memory_server.protocols import IContinuityEventRecorder

logger = logging.getLogger(__name__)

MemoryEventKind = Literal["memory_saved", "memory_archived", "memory_superseded"]


class _Registration:
    """目前登記的記錄器（只在 Proxy 啟動與關閉時改）."""

    recorder: IContinuityEventRecorder | None = None


def register_event_recorder(recorder: IContinuityEventRecorder | None) -> None:
    """登記（或用 None 清掉）記錄器."""
    _Registration.recorder = recorder


def record_memory_event(
    identity: Any, kind: MemoryEventKind, fact_id: str, *, new_fact_id: str | None = None
) -> None:
    """事實已提交後記一筆記憶事件；沒登記就略過，絕不丟例外."""
    recorder = _Registration.recorder
    if recorder is None:
        return
    metadata = {"new_fact_id": new_fact_id} if new_fact_id else {}
    try:
        recorder.record(identity, kind, fact_id, metadata)
    except Exception:
        logger.warning("Memory event %s was not recorded.", kind, exc_info=True)
