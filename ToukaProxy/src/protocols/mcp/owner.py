"""每條 MCP 連線一個專屬 owner task（life1）。

SDK v2 的 ``Client`` 與 transport 把 anyio task group 掛在「進入它們的 task」上：連線途中任何一個
請求失敗、或從別的 task 關閉，anyio 都會把取消送回那個 task，並遞迴殺掉它之後建立的連線。
owner 讓進入與退出都只發生在自己的 task：

- 連線函式（``connect_mcp_server``）原樣在 owner task 執行，傳輸仍由 ``_connect_once`` 建立；
- 整條連線包在 owner 自己的 anyio ``CancelScope`` 裡：中止握手、關閉期限到都只取消這個 scope，
  SDK 包在 shield 裡的 stdio 收尾（關 stdin、等、殺行程樹）照常跑完；
- 連上後 owner 停著等；被要求關閉或傳輸自己出事時，在同一個 task 關 stack，期限 7 秒；
- 傳輸自己出事、沒有人要求關閉時，呼叫 ``on_lost`` 恰好一次（此時取消狀態已清乾淨）；
- 被要求關閉（含期限到的硬關）不算失聯、不呼叫 ``on_lost``。

owner 不用 ``spawn_logged``：關機時 ``cancel_all_background_tasks`` 先於 MCP cleanup，會硬取消它。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import anyio

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AsyncExitStack
    from contextvars import ContextVar

logger = logging.getLogger(__name__)

# 每條連線的關閉期限：SDK stdio 在 POSIX 最壞約 6.5 秒才殺完行程樹（Windows 約 2.5 秒）。
OWNER_CLOSE_SECONDS = 7.0
# 期限到之後再等 SDK shielded 收尾的寬限；cleanup 的總期限＝期限＋寬限，不論幾台都不疊加。
OWNER_CLOSE_GRACE_SECONDS = 1.0
_HANDSHAKE_ABORTED = "MCP 連線在握手階段被中止。"
_TRANSPORT_CLOSED = "MCP 連線被傳輸層關閉。"


class McpConnectionAborted(ConnectionError):
    """cleanup 中止了還在握手的連線：呼叫端不安裝、``with_retry`` 不重試。"""


def root_cause(error: BaseException) -> BaseException:
    """從 ExceptionGroup 取出第一個不是取消的葉例外；找不到就回原例外。"""
    if isinstance(error, BaseExceptionGroup):
        for inner in error.exceptions:
            cause = root_cause(inner)
            if not isinstance(cause, asyncio.CancelledError):
                return cause
    return error


class McpConnectionOwner:
    """持有一條 MCP 連線的 task；``aclose`` 可從任何 task 呼叫，可重複呼叫。"""

    def __init__(
        self,
        label: str,
        connect: Callable[[], Awaitable[tuple[Any, AsyncExitStack]]],
        *,
        on_lost: Callable[[McpConnectionOwner, BaseException], Awaitable[None]],
        registry: set[McpConnectionOwner],
        clear_on_ready: tuple[ContextVar[Any], ...] = (),
    ) -> None:
        self.label = label
        self._connect = connect
        self._on_lost = on_lost
        self._registry = registry
        self._clear_on_ready = clear_on_ready
        self._close_requested = asyncio.Event()
        self._close_deadline: float | None = None
        self._aborted = False
        self._lost = False
        self._hard_closed = False
        self._ready: asyncio.Future[Any] | None = None
        self._scope: anyio.CancelScope | None = None
        self.task: asyncio.Task[None] | None = None

    @property
    def alive(self) -> bool:
        """已連上、沒斷線，也沒有人要求關閉。"""
        return (
            self.task is not None
            and not self.task.done()
            and self._ready is not None
            and self._ready.done()
            and not self._close_requested.is_set()
            and not self._lost
        )

    async def start(self) -> Any:
        """在 owner task 連線；回傳已 discover／adopt 的 SDK Client，失敗照原例外拋出。"""
        self._ready = asyncio.get_running_loop().create_future()
        self._scope = anyio.CancelScope()
        self.task = asyncio.create_task(self._run(), name=f"mcp-owner:{self.label}")
        self._registry.add(self)
        self.task.add_done_callback(lambda _task: self._registry.discard(self))
        try:
            return await asyncio.shield(self._ready)
        except asyncio.CancelledError:
            # 呼叫端不要這條連線了：還在握手就中止，已連上就正常關；收完才離開（不留孤兒）。
            self._request_close()
            if not self._ready.done():
                self._scope.cancel()
            await self._wait_closed(swallow_cancel=True)
            if self._ready.done() and not self._ready.cancelled():
                self._ready.exception()
            raise

    def abort(self) -> None:
        """給 cleanup：還在握手就中止（拋 ``McpConnectionAborted``）；已連上就要求關閉。"""
        if self._ready is not None and not self._ready.done():
            self._aborted = True
            assert self._scope is not None
            self._scope.cancel()
        else:
            self._request_close()

    async def aclose(self) -> None:
        """要求關閉並等 owner 收完（最多到期限＋寬限）；呼叫端被取消就重拋，owner 背景照常關。"""
        self._request_close()
        await self._wait_closed()

    def _request_close(self) -> None:
        if not self._close_requested.is_set():
            loop = asyncio.get_running_loop()  # anyio 的 scope 期限用同一個事件迴圈時鐘
            self._close_deadline = loop.time() + OWNER_CLOSE_SECONDS
            self._close_requested.set()

    async def _wait_closed(self, *, swallow_cancel: bool = False) -> None:
        """等 owner 結束；``swallow_cancel`` 時期限內吞掉後續取消（anyio 的會一直重送）。"""
        loop = asyncio.get_running_loop()
        end = (self._close_deadline or loop.time()) + OWNER_CLOSE_GRACE_SECONDS
        with anyio.CancelScope(shield=swallow_cancel):
            while self.task is not None and not self.task.done():
                left = end - loop.time()
                if left <= 0:
                    return
                try:
                    await asyncio.wait({self.task}, timeout=left)
                except asyncio.CancelledError:
                    if not swallow_cancel:
                        raise

    async def _run(self) -> None:
        assert self._ready is not None and self._scope is not None
        loop = asyncio.get_running_loop()
        connected = False
        close_error: BaseException | None = None
        with self._scope:
            try:
                client, stack = await self._connect()
            except BaseException as exc:
                self._fail_ready(exc)
                # 取消與解譯器結束照樣往外拋；其他（例如 BaseExceptionGroup）已交給 _ready。
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                return
            connected = True
            # create_task 複製了呼叫端 context（含 workload 一次性票）；owner 不長期持有。
            for var in self._clear_on_ready:
                var.set(None)
            self._ready.set_result(client)
            try:
                await self._close_requested.wait()
            except asyncio.CancelledError:
                self._lost = True
            if self._close_deadline is None:
                self._close_deadline = loop.time() + OWNER_CLOSE_SECONDS
            self._scope.deadline = self._close_deadline
            close_error = await self._close_stack(stack)
        if not connected:
            return
        self._hard_closed = self._scope.cancel_called
        # anyio 離開 scope 時已 uncancel 自己送的取消；還有剩就是關機的外部原生取消。
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        if self._close_requested.is_set():
            if self._hard_closed:
                logger.warning("MCP connection %s: close deadline reached", self.label)
            elif close_error is not None:
                name = type(root_cause(close_error)).__name__  # 只記型別，不記內容（禁令 23）
                logger.warning("MCP connection %s closed with error: %s", self.label, name)
            return
        cause = root_cause(close_error) if close_error is not None else None
        if cause is None or isinstance(cause, asyncio.CancelledError):
            cause = ConnectionError(_TRANSPORT_CLOSED)
        try:
            await self._on_lost(self, cause)
        except Exception:
            logger.exception("MCP connection %s: lost handler failed", self.label)

    def _fail_ready(self, error: BaseException) -> None:
        assert self._ready is not None
        if self._ready.done():
            return
        if self._aborted:
            self._ready.set_exception(McpConnectionAborted(_HANDSHAKE_ABORTED))
        elif self._close_requested.is_set():
            self._ready.cancel()  # 呼叫端已放棄，沒有人會讀結果
        elif isinstance(error, Exception):
            self._ready.set_exception(error)
        else:
            self._ready.set_exception(ConnectionError(_HANDSHAKE_ABORTED))

    @staticmethod
    async def _close_stack(stack: AsyncExitStack) -> BaseException | None:
        """在 owner task 關 stack；回傳關閉時的例外（含 ExceptionGroup 與取消）。"""
        try:
            await stack.aclose()
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as exc:
            return exc
        return None
