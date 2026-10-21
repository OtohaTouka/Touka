"""Shared in-memory MCP transport with controllable handshake, loss and cleanup."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Any

import anyio


class _FakeTransport:
    """假連線：記下進入的 task；``fail()``＝途中斷線。

    ``gate``＝握手停在這裡；``abort_delay``＝握手被中止後在 shield 裡收尾多久（像 SDK stdio）；
    ``close_gate``＝關閉停在這裡直到放行；``hang_close``＝關閉卡住直到被取消；
    ``shielded_close``＝關閉時在 shield 裡多睡幾秒（取消不掉）；``close_error``＝關閉時拋這個。
    """

    def __init__(
        self,
        *,
        gate: asyncio.Event | None = None,
        abort_delay: float = 0.0,
        close_gate: asyncio.Event | None = None,
        hang_close: bool = False,
        shielded_close: float = 0.0,
        close_error: Exception | None = None,
        session: Any = None,
    ) -> None:
        self.gate = gate
        self.abort_delay = abort_delay
        self.close_gate = close_gate
        self.hang_close = hang_close
        self.shielded_close = shielded_close
        self.close_error = close_error
        self.session = session
        self.before_return: Any = None
        self.failure = asyncio.Event()
        self.entered_in: list[asyncio.Task[Any] | None] = []
        self.closed = 0
        self.aborted = 0
        self.abort_finished = 0
        self.hard_cancelled = False

    async def connect(self, *_args: Any, **_kwargs: Any) -> tuple[Any, AsyncExitStack]:
        self.entered_in.append(asyncio.current_task())
        stack = AsyncExitStack()
        tg = await stack.enter_async_context(anyio.create_task_group())
        stack.callback(tg.cancel_scope.cancel)
        stack.push_async_callback(self._close)
        tg.start_soon(self._transport)
        try:
            if self.gate is not None:
                await self.gate.wait()
            else:
                await asyncio.sleep(0)
        except BaseException:
            self.aborted += 1
            try:
                if self.abort_delay:
                    with anyio.CancelScope(shield=True):
                        await asyncio.sleep(self.abort_delay)
                await stack.aclose()
            finally:
                self.abort_finished += 1
            raise
        if self.before_return is not None:
            self.before_return()
        return self.session or SimpleNamespace(protocol_version="legacy", transport=self), stack

    def fail(self) -> None:
        self.failure.set()

    async def _transport(self) -> None:
        await self.failure.wait()
        msg = "peer reset mid-flight"
        raise ConnectionResetError(msg)

    async def _close(self) -> None:
        self.closed += 1
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.shielded_close:
            with anyio.CancelScope(shield=True):
                await asyncio.sleep(self.shielded_close)
        if self.hang_close:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.hard_cancelled = True
                raise
        if self.close_error is not None:
            raise self.close_error


