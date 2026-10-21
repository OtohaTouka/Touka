"""上游串流的兩段逾時與整輪上限（ADR-0248 D3；3b 各家串流共用）。

首位元組前最多 ``FIRST_EVENT_TIMEOUT_S``；之後任兩次收到位元組最多隔 ``IDLE_TIMEOUT_S``（ping、
``:`` 註解行、空行都算活著；anthropic SDK 內部丟掉 ping，所以 Claude 經 ``liveness_event_hooks``
在 httpx2 層看位元組）；整輪最多 ``ROUND_TIMEOUT_S``。呼叫端在 ``deadline()`` 外面接
``TimeoutError``；區塊只包送出與彙整，不跨過交給下游的 yield。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import httpx2

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

FIRST_EVENT_TIMEOUT_S = 120.0
IDLE_TIMEOUT_S = 60.0  # 推測值：長思考期間的 ping 間隔未實測（實測 L3）
ROUND_TIMEOUT_S = 600.0  # ＝anthropic SDK 的非串流上限；整輪逾時不重試（U5a）
_PHASE_LABELS = {"first_event": "等不到第一個回應", "idle": "閒置太久", "round": "整輪超過上限"}


class RoundClock:
    """一輪串流的期限；``phase``＝目前生效的那一段（first_event／idle／round）。"""

    def __init__(self, *, wait_for_content: bool = False) -> None:
        self._timeout: asyncio.Timeout | None = None
        self._start = 0.0
        # 會先送空殼事件的上游（R-3b ② M2）：收到有內容的事件（``content()``）才結束首事件階段
        self._waiting = wait_for_content
        self.saw_output = False  # 已有輸出：之後的閒置逾時不重試（R-3b ② L3）
        self.phase = "first_event"

    @asynccontextmanager
    async def deadline(self) -> AsyncIterator[RoundClock]:
        """包住一輪的送出與彙整；期間經 httpx2 hook 收到的位元組都會延後期限。"""
        async with asyncio.timeout(FIRST_EVENT_TIMEOUT_S) as timeout:
            self._timeout, self._start = timeout, timeout.when() - FIRST_EVENT_TIMEOUT_S
            token = _ACTIVE.set(self)
            try:
                yield self
            finally:
                self._timeout = None
                _ACTIVE.reset(token)

    def touch(self) -> None:
        """收到位元組：期限改成「現在＋閒置值」（還在等內容時不早於首事件期限），不超過整輪上限。"""
        timeout = self._timeout
        if timeout is None or timeout.expired():
            return
        self.saw_output = self.saw_output or not self._waiting
        loop = asyncio.get_running_loop()
        idle = loop.time() + IDLE_TIMEOUT_S
        first = self._start + FIRST_EVENT_TIMEOUT_S
        when, phase = (first, "first_event") if self._waiting and first > idle else (idle, "idle")
        cap = self._start + ROUND_TIMEOUT_S
        self.phase = phase if when < cap else "round"
        timeout.reschedule(min(when, cap))

    def content(self) -> None:
        """收到第一個有內容的事件：首事件階段結束，之後照位元組閒置計時。"""
        if self._waiting:
            self._waiting = False
            self.touch()

    async def timed(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """自寫 SSE 用：每收到一段位元組就 touch（ping、註解行、空行都算）。"""
        async for chunk in chunks:
            self.touch()
            yield chunk

    @property
    def timeout_text(self) -> str:
        """逾時後給使用者看的說明（哪一段逾時）。"""
        return f"上游串流逾時：{_PHASE_LABELS[self.phase]}"


_ACTIVE: ContextVar[RoundClock | None] = ContextVar("touka_round_clock", default=None)


class _TimedByteStream(httpx2.AsyncByteStream):
    """回應位元組串流的外層：每收到一段位元組就延後這一輪的期限。"""

    def __init__(self, inner: httpx2.AsyncByteStream, clock: RoundClock) -> None:
        self._inner = inner
        self._clock = clock

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._clock.touch()
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


async def _time_response(response: httpx2.Response) -> None:
    clock = _ACTIVE.get()
    if clock is not None:
        response.stream = _TimedByteStream(response.stream, clock)


def liveness_event_hooks() -> dict[str, list[Any]]:
    """主迴圈 Claude client 的 httpx2 event hooks；只計時在 ``deadline()`` 裡送出的請求。"""
    return {"response": [_time_response]}
