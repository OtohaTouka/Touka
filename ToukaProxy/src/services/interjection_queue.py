"""插話佇列（ADR-0249 第 4a 段）— AI 工作中使用者補充的暫存 SSOT。

使用者在生成中送出的補充先排在這裡，react_core 在工具邊界取出（U4a-2）。
- 鍵是 (session_id, stream_id)：上一個串流的插話不會漏到下一個串流。
- 和 rhythm_service 的中斷旗標分開放（心跳會清同一 session 的旗標）。
- 只放記憶體：重啟就清，無痕也不存檔；串流結束或同 session 開新串流時由
  StreamBufferService 呼叫 ``clear``。
- 狀態：queued → pending(step=N) → delivered(step=N)；或 → undelivered、retracted。
  queued 超過 TTL 改成 undelivered（不再交給模型）；閒置超過 TTL、沒有 pending 也沒有
  delivered 的整串清掉（已送達的要留給存檔、也佔著上限，等串流結束的 ``clear``）。

所有方法都是同步、不 await，在單一事件迴圈上各自原子（收回與取出不會交錯）。
第 4b 段：``urgent_signal`` 是每個串流的 Ctrl+Enter 訊號，有 urgent 排隊中才 set，
react_core 接收上游時同時等它（U4b-1）。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from src.core.exceptions import ErrorCode, ToukaException
from src.models.interjection import Interjection
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from collections.abc import Iterable

    from src.models.interjection import InterjectionStatus

# 每個串流最多幾則（含已送達；收回的不算）
INTERJECTION_MAX_PER_STREAM = 10
# 每則最多幾個字元
INTERJECTION_MAX_CHARS = 4000
# queued 的有效秒數；也是整串閒置後清掉的秒數
INTERJECTION_TTL_SECONDS = 600

# SSE 契約（ADR-0249 技術主管追加 6）：前端 ToukaChat/src/lib/chat/interjections.ts 同名常數；
# 取出點（U4a-2）照這份送，不另起名稱。
INTERJECTION_DELIVERED_EVENT = "interjection_delivered"
INTERJECTION_UNDELIVERED_EVENT = "interjection_undelivered"
# 第 4b 段（U4b-1）：Ctrl+Enter 打斷了正在接收的這一輪；前端同名常數由 U4b-2 加
ROUND_INTERRUPTED_BY_USER_EVENT = "round_interrupted_by_user"


def interjection_delivered_event(
    *, interjection_id: str, step: int, urgent: bool, text: str
) -> dict[str, Any]:
    """下一輪送出成功後才送：這則插話在第 ``step`` 步送達。

    帶原文（使用者自己寫的），重整後重播時前端也能寫進節點、組歷史標註。
    """
    return {
        "type": INTERJECTION_DELIVERED_EVENT,
        "interjection_id": interjection_id,
        "step": step,
        "urgent": urgent,
        "text": text,
    }


def interjection_undelivered_event(*, interjection_ids: list[str]) -> dict[str, Any]:
    """回合結束時還沒送達的（含重試全失敗退回的）；前端在回合結束後當成下一則送出。"""
    return {"type": INTERJECTION_UNDELIVERED_EVENT, "interjection_ids": list(interjection_ids)}


def round_interrupted_by_user_event(*, step: int) -> dict[str, Any]:
    """Ctrl+Enter 打斷了正在接收的這一輪；``step``＝已完成的工具步數（第一輪＝0）。"""
    return {"type": ROUND_INTERRUPTED_BY_USER_EVENT, "step": step}


@runtime_checkable
class InterjectionQueue(Protocol):
    """插話佇列合約；``ids`` 不在該鍵或狀態不符的會略過，回傳實際改到的項目。"""

    def enqueue(
        self, session_id: str, stream_id: str, text: str, *, urgent: bool = False
    ) -> Interjection:
        """加入一則 queued；超量、過長、空白回 ToukaException。"""
        ...

    def drain(self, session_id: str, stream_id: str, *, step: int) -> list[Interjection]:
        """取出全部 queued，原子地改成 pending(step)；同一則不會被取出兩次。"""
        ...

    def list_items(self, session_id: str, stream_id: str) -> list[Interjection]:
        """列出該串流目前的插話（照加入順序；收回的不列）。"""
        ...

    def retract(self, session_id: str, stream_id: str, interjection_id: str) -> Interjection:
        """收回一則 queued；已交給模型回 409、找不到回 404。"""
        ...

    def mark_pending(
        self, session_id: str, stream_id: str, ids: Iterable[str], *, step: int
    ) -> list[Interjection]:
        """把 queued 改成 pending(step)。"""
        ...

    def mark_delivered(
        self, session_id: str, stream_id: str, ids: Iterable[str]
    ) -> list[Interjection]:
        """把 pending 改成 delivered（模型已看到）。"""
        ...

    def mark_undelivered(
        self, session_id: str, stream_id: str, ids: Iterable[str]
    ) -> list[Interjection]:
        """queued／pending → undelivered（模型沒看到）。"""
        ...

    def clear(self, session_id: str, stream_id: str | None = None) -> list[Interjection]:
        """清掉一個串流（``stream_id`` 為 None 時清掉整個 session），回傳被清掉的項目。"""
        ...

    def urgent_signal(self, session_id: str, stream_id: str) -> asyncio.Event:
        """Ctrl+Enter 訊號（第 4b 段）：有 urgent 排隊中就 set；取出、收回、過期、清掉後 clear。"""
        ...


@dataclass
class _StreamQueue:
    items: dict[str, Interjection] = field(default_factory=dict)
    touched_at: float = 0.0


class InMemoryInterjectionQueue:
    """記憶體實作（重啟就清）。"""

    def __init__(self) -> None:
        self._queues: dict[tuple[str, str], _StreamQueue] = {}
        self._signals: dict[tuple[str, str], asyncio.Event] = {}

    def enqueue(
        self, session_id: str, stream_id: str, text: str, *, urgent: bool = False
    ) -> Interjection:
        """加入一則 queued。"""
        if not text.strip():
            raise ToukaException(ErrorCode.VALIDATION_ERROR)
        if len(text) > INTERJECTION_MAX_CHARS:
            raise ToukaException(ErrorCode.INTERJECTION_TOO_LONG)
        now = get_current_epoch()
        self._sweep(now)
        queue = self._queues.get((session_id, stream_id))
        if queue is not None and len(queue.items) >= INTERJECTION_MAX_PER_STREAM:
            raise ToukaException(ErrorCode.INTERJECTION_QUEUE_FULL)
        if queue is None:
            queue = self._queues[(session_id, stream_id)] = _StreamQueue()
        item = Interjection(
            id=uuid.uuid4().hex, text=text, urgent=urgent, status="queued", created_at=now
        )
        queue.items[item.id] = item
        queue.touched_at = now
        self._sync_signal((session_id, stream_id))
        return item

    def drain(self, session_id: str, stream_id: str, *, step: int) -> list[Interjection]:
        """取出全部 queued 並改成 pending(step)。"""
        queued = [i.id for i in self.list_items(session_id, stream_id) if i.status == "queued"]
        return self.mark_pending(session_id, stream_id, queued, step=step)

    def list_items(self, session_id: str, stream_id: str) -> list[Interjection]:
        """列出該串流目前的插話。"""
        queue = self._live(session_id, stream_id)
        return list(queue.items.values()) if queue else []

    def retract(self, session_id: str, stream_id: str, interjection_id: str) -> Interjection:
        """收回一則 queued，從佇列移除（收回的不佔上限）。"""
        queue = self._live(session_id, stream_id)
        item = queue.items.get(interjection_id) if queue else None
        if queue is None or item is None:
            raise ToukaException(ErrorCode.INTERJECTION_NOT_FOUND)
        if item.status != "queued":
            raise ToukaException(ErrorCode.INTERJECTION_NOT_RETRACTABLE)
        del queue.items[interjection_id]
        queue.touched_at = get_current_epoch()
        self._sync_signal((session_id, stream_id))
        return item.model_copy(update={"status": "retracted"})

    def mark_pending(
        self, session_id: str, stream_id: str, ids: Iterable[str], *, step: int
    ) -> list[Interjection]:
        """把 queued 改成 pending(step)。"""
        return self._transition(session_id, stream_id, ids, ("queued",), "pending", step)

    def mark_delivered(
        self, session_id: str, stream_id: str, ids: Iterable[str]
    ) -> list[Interjection]:
        """把 pending 改成 delivered。"""
        return self._transition(session_id, stream_id, ids, ("pending",), "delivered")

    def mark_undelivered(
        self, session_id: str, stream_id: str, ids: Iterable[str]
    ) -> list[Interjection]:
        """queued／pending → undelivered。"""
        return self._transition(session_id, stream_id, ids, ("queued", "pending"), "undelivered")

    def clear(self, session_id: str, stream_id: str | None = None) -> list[Interjection]:
        """清掉一個串流或整個 session 的佇列（連同 Ctrl+Enter 訊號）。"""

        def matches(key: tuple[str, str]) -> bool:
            return key[0] == session_id and (stream_id is None or key[1] == stream_id)

        for key in [key for key in self._signals if matches(key)]:
            self._signals.pop(key).clear()  # 拿著舊訊號的人不會看到殘留的 set
        keys = [key for key in self._queues if matches(key)]
        return [item for key in keys for item in self._queues.pop(key).items.values()]

    def _transition(
        self,
        session_id: str,
        stream_id: str,
        ids: Iterable[str],
        allowed: tuple[InterjectionStatus, ...],
        status: InterjectionStatus,
        step: int | None = None,
    ) -> list[Interjection]:
        queue = self._live(session_id, stream_id)
        if queue is None:
            return []
        update: dict[str, object] = {"status": status}
        if step is not None:
            update["step"] = step
        changed: list[Interjection] = []
        for interjection_id in ids:
            item = queue.items.get(interjection_id)
            if item is not None and item.status in allowed:
                item = queue.items[interjection_id] = item.model_copy(update=update)
                changed.append(item)
        if changed:
            queue.touched_at = get_current_epoch()
            self._sync_signal((session_id, stream_id))
        return changed

    def _live(self, session_id: str, stream_id: str) -> _StreamQueue | None:
        self._sweep(get_current_epoch())
        return self._queues.get((session_id, stream_id))

    def _sweep(self, now: float) -> None:
        """過期的 queued 改成 undelivered；閒置超過 TTL、沒有 pending／delivered 的整串清掉。"""
        for key, queue in list(self._queues.items()):
            for interjection_id, item in list(queue.items.items()):
                if item.status == "queued" and now - item.created_at > INTERJECTION_TTL_SECONDS:
                    queue.items[interjection_id] = item.model_copy(update={"status": "undelivered"})
            idle = now - queue.touched_at > INTERJECTION_TTL_SECONDS
            keep = any(i.status in ("pending", "delivered") for i in queue.items.values())
            if idle and not keep:  # 已送達的留到串流結束（存檔讀它；上限不歸零）
                del self._queues[key]
            self._sync_signal(key)

    def urgent_signal(self, session_id: str, stream_id: str) -> asyncio.Event:
        """這個串流的 Ctrl+Enter 訊號（同一個串流回傳同一個 Event）。"""
        key = (session_id, stream_id)
        signal = self._signals.setdefault(key, asyncio.Event())
        self._sweep(get_current_epoch())
        self._sync_signal(key)
        return signal

    def _sync_signal(self, key: tuple[str, str]) -> None:
        """訊號跟著佇列走：有 urgent 排隊中才 set（取出、收回、過期、整串清掉後 clear）。"""
        signal = self._signals.get(key)
        if signal is None:
            return
        queue = self._queues.get(key)
        items = queue.items.values() if queue is not None else ()
        if any(item.urgent and item.status == "queued" for item in items):
            signal.set()
        else:
            signal.clear()


# 模組級單例（SSOT）；StreamBufferService 預設注入同一個實例
interjection_queue: InterjectionQueue = InMemoryInterjectionQueue()
