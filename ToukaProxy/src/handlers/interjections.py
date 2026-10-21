"""工具邊界插話（ADR-0249 第 4a 段；U4a-2）：react_core 每一回合的取出、送達與收尾。

- 只在 UI 對話建立（``ui_authorized``、有 session、串流進行中）；心跳、子代理（沒有 session）、
  complete_once 拿不到佇列。串流代（stream_id）在回合開始時從串流緩衝讀一次，之後固定用它：
  新串流取代舊串流時，舊的 producer 會在它下一個 await 被取消，不會讀到新串流的佇列。
- 取出＝pending(step=N)；下一輪送出成功才 delivered；回合結束時還沒送達的一律 undelivered。
- 佇列是空的時候一個事件都不送（golden 零差異）。
- 第 4b 段：``urgent_signal`` 交給 pump；Ctrl+Enter 打斷時取出全部（urgent 優先），
  pending 累加到下一輪送出成功（被打斷的那一輪沒送成）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.services.interjection_queue import (
    interjection_delivered_event,
    interjection_undelivered_event,
)
from src.utils.sse import sse_line

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from src.models.interjection import Interjection
    from src.services.interjection_queue import InterjectionQueue


class InterjectionRound:
    """一個回合的插話：取出（pending）→ 送達（delivered）或收尾（undelivered）。"""

    def __init__(self, queue: InterjectionQueue, session_id: str, stream_id: str) -> None:
        self._queue = queue
        self._session_id, self._stream_id = session_id, stream_id
        self._pending: list[Interjection] = []
        self._finished = False

    @classmethod
    def begin(
        cls,
        context: dict[str, Any],
        queue: InterjectionQueue,
        stream_state: Callable[[str], dict[str, Any] | None],
    ) -> InterjectionRound | None:
        """只有 UI 對話、有 session、串流進行中才回傳；其他情況回 None（拿不到佇列）。"""
        session_id = context.get("session_id")
        ui_conversation = context.get("ui_authorized") is True and not context.get("subagent_run")
        if not ui_conversation or not session_id:
            return None
        state = stream_state(session_id)
        if not state or state.get("status") != "active" or not state.get("stream_id"):
            return None
        return cls(queue, session_id, state["stream_id"])

    def drain(self, step: int, *, urgent_first: bool = False) -> list[Interjection]:
        """取出全部排隊中的，改成 pending(step)；``urgent_first``＝打斷時 urgent 優先。"""
        items = self._queue.drain(self._session_id, self._stream_id, step=step)
        if urgent_first:
            items.sort(key=lambda item: not item.urgent)  # 穩定排序：其餘照順序
        self._pending += items
        return items

    def urgent_signal(self) -> asyncio.Event:
        """這個串流的 Ctrl+Enter 訊號（第 4b 段）：react_core 接收上游時同時等它。"""
        return self._queue.urgent_signal(self._session_id, self._stream_id)

    def delivered(self) -> list[str]:
        """下一輪送出成功：pending → delivered，回傳要送的 ``interjection_delivered``。"""
        if not self._pending:
            return []
        done = self._queue.mark_delivered(
            self._session_id, self._stream_id, [item.id for item in self._pending]
        )
        self._pending = []
        return [
            sse_line(interjection_delivered_event(
                interjection_id=item.id, step=item.step or 0, urgent=item.urgent, text=item.text
            ))
            for item in done
        ]

    def finish(self) -> list[str]:
        """回合結束：排隊中與送不成的一律 undelivered，送一次 ``interjection_undelivered``。"""
        if self._finished:
            return []
        self._finished = True
        items = self._queue.list_items(self._session_id, self._stream_id)
        self._queue.mark_undelivered(
            self._session_id, self._stream_id,
            [item.id for item in items if item.status in ("queued", "pending")],
        )
        ids = [item.id for item in self._queue.list_items(self._session_id, self._stream_id)
               if item.status == "undelivered"]
        self._pending = []
        return [sse_line(interjection_undelivered_event(interjection_ids=ids))] if ids else []
