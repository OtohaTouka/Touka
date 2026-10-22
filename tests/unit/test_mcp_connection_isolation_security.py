"""life1（禁令 20）：一條 MCP 連線出事或被關，不得取消建立它的 task，也不得連累其他連線。

可被外部觸發的可用性弱點：建立連線的 task（lifespan、聊天請求、restart／存設定請求）是
SDK transport task group 的 host。遠端在呼叫途中斷線或回 3xx、或另一個 task restart／重載
關掉這條連線，anyio 都會把取消送回 host，並遞迴殺掉 host 之後建立的其他連線。

全離線：真 ``MCPServer`` 經 ASGI 服務（``tests/helpers/mcp_asgi_peers.py``），連線走正式的
``connect_mcp_server``。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from src.protocols.manager import ProtocolManager
from src.protocols.mcp.canonical import McpOperationException
from src.services.isolation.status import IsolationMonitor, SelfTestResult
from src.utils.task_utils import cancel_all_background_tasks

from tests.helpers import mcp_asgi_peers as peers
from tests.support.isolation import fake_kernel_layout

_WAIT = 10.0


@pytest.fixture
def no_unretrieved(caplog: pytest.LogCaptureFixture) -> Any:
    yield
    assert peers.task_leak_messages(caplog) == []


def _record_hooks(monkeypatch: pytest.MonkeyPatch, manager: ProtocolManager) -> list[Any]:
    """記下失聯通知（owner 回報後 manager 呼叫的 hook）。"""
    hooks: list[Any] = []
    monkeypatch.setattr(manager, "_connection_lost_hook", lambda *args: hooks.append(args))
    return hooks


async def _settle() -> None:
    """讓 anyio 有機會送出取消。"""
    for _ in range(20):
        await asyncio.sleep(0.01)


class _Creator:
    """在自己的 task 依序建立連線，之後停住直到 ``stop``。

    ``lazy=False`` 模擬 lifespan（``connect_server``）；``lazy=True`` 模擬聊天請求在 ToolLayer
    第一次用到時建立（``ensure_connected``）。
    """

    def __init__(
        self,
        manager: ProtocolManager,
        router: peers.PeerRouter,
        *names: str,
        lazy: bool = False,
    ) -> None:
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.cancelled = False
        self._manager = manager
        self._router = router
        self._names = names
        self._lazy = lazy
        self.task = asyncio.create_task(self._run(), name="isolation-creator")

    async def _run(self) -> str:
        try:
            for name in self._names:
                if self._lazy:
                    assert await self._manager.ensure_connected(name)
                else:
                    await self._manager.connect_server(name, self._router.config(name))
            self.ready.set()
            await self.stop.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            return "cancelled"
        return "completed"

    async def finish(self) -> str | None:
        """結束 creator；不讓它的取消冒到測試 task。"""
        self.stop.set()
        await asyncio.wait({self.task}, timeout=_WAIT)
        if self.task.done() and not self.task.cancelled():
            return self.task.result()
        return None


async def test_restart_from_another_task_keeps_creator_and_other_connection(
    monkeypatch, no_unretrieved
):
    router = peers.PeerRouter("alpha", "beta")
    peers.install(monkeypatch, router)
    manager = ProtocolManager(router.registry())
    hooks = _record_hooks(monkeypatch, manager)

    async with router.serving():
        creator = _Creator(manager, router, "alpha", "beta")
        await asyncio.wait_for(creator.ready.wait(), timeout=_WAIT)

        # restart 請求 task（routers/mcp.py restart → restart_server）
        restart = asyncio.create_task(manager.restart_server("alpha"))
        await asyncio.wait({restart}, timeout=_WAIT)
        await _settle()
        creator_alive = not creator.task.done()
        restarted = restart.done() and not restart.cancelled() and restart.result()
        # creator 已被取消時不再呼叫：那會在測試 task 重新連線，掩蓋連線已被關掉。
        alpha = await peers.ping(manager, "alpha") if creator_alive else ""
        beta = await peers.ping(manager, "beta") if creator_alive else ""
        result = await creator.finish()
        await manager.cleanup()

    assert creator.cancelled is False, "restart from another task cancelled the creator"
    assert creator_alive
    assert result == "completed"
    assert restarted is True
    assert "pong-alpha" in alpha
    assert "pong-beta" in beta
    assert hooks == []  # 要求關閉不算失聯


async def test_reload_from_another_task_keeps_creator(monkeypatch, no_unretrieved):
    router = peers.PeerRouter("alpha", "beta")
    peers.install(monkeypatch, router)
    manager = ProtocolManager(router.registry())
    hooks = _record_hooks(monkeypatch, manager)

    async def _no_ide() -> dict:
        return {}

    monkeypatch.setattr("src.protocols.manager.discover_ide_servers", _no_ide)

    async with router.serving():
        creator = _Creator(manager, router, "alpha", "beta")
        await asyncio.wait_for(creator.ready.wait(), timeout=_WAIT)

        # 存 MCP 設定／Hub 安裝請求 task（routers/mcp.py → initialize_all）
        reload_task = asyncio.create_task(manager.initialize_all())
        await asyncio.wait({reload_task}, timeout=_WAIT)
        await _settle()
        creator_alive = not creator.task.done()
        reloaded = reload_task.done() and not reload_task.cancelled()
        alpha = await peers.ping(manager, "alpha") if creator_alive else ""
        beta = await peers.ping(manager, "beta") if creator_alive else ""
        result = await creator.finish()
        await manager.cleanup()

    assert creator.cancelled is False, "reload from another task cancelled the creator"
    assert creator_alive
    assert result == "completed"
    assert reloaded is True
    assert "pong-alpha" in alpha
    assert "pong-beta" in beta
    assert hooks == []


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(peers.remote_disconnect, id="remote-disconnect"),
        pytest.param(peers.redirect_302, id="redirect-302"),
    ],
)
async def test_midflight_failure_only_breaks_that_connection(monkeypatch, no_unretrieved, failure):
    router = peers.PeerRouter("alpha", "beta")
    peers.install(monkeypatch, router)
    manager = ProtocolManager(router.registry())
    hooks = _record_hooks(monkeypatch, manager)

    async with router.serving():
        # 聊天請求 R1 建立兩條連線後還在跑；另一個請求 R2 用 alpha 時對方出事。
        creator = _Creator(manager, router, "alpha", "beta", lazy=True)
        await asyncio.wait_for(creator.ready.wait(), timeout=_WAIT)
        router.failures["alpha"] = failure
        owner = manager.handle_pool.get(manager.handle_key_for("alpha", None)).stack
        caller = asyncio.create_task(peers.ping(manager, "alpha"))
        await asyncio.wait({caller}, timeout=_WAIT)
        await _settle()
        creator_alive = not creator.task.done()
        caller_error = caller.exception() if caller.done() and not caller.cancelled() else None
        router.failures["alpha"] = None
        beta = await peers.ping(manager, "beta") if creator_alive else ""
        # 斷掉的那台不自己重連：之後的呼叫快速失敗，要手動 restart。
        again: BaseException | None = None
        if creator_alive:
            try:
                await asyncio.wait_for(peers.ping(manager, "alpha"), timeout=_WAIT)
            except McpOperationException as exc:
                again = exc
        result = await creator.finish()
        await manager.cleanup()

    assert router.injected["alpha"] == 1
    assert creator.cancelled is False, "a mid-flight failure cancelled the creator"
    assert creator_alive
    assert result == "completed"
    assert isinstance(caller_error, McpOperationException)
    assert "pong-beta" in beta
    assert isinstance(again, McpOperationException)
    assert router.handshakes("alpha") == 1
    assert [args[2] for args in hooks] == [owner]  # 失聯通知恰好一次，就是斷掉的那條


async def _never(_seconds: float) -> None:
    await asyncio.Event().wait()


async def _selftest_ok(_kernel_dir: Any, _test_dir: Any) -> SelfTestResult:
    """和隔離層 `_SelfTest` 預設同形狀的假自測：不碰真的 Landlock 或 Docker。"""
    return SelfTestResult(ok=True, abi=7)


async def test_midflight_failure_keeps_isolation_background_reprobe_running(
    monkeypatch, tmp_path, no_unretrieved
):
    """I1：lifespan 被取消時，收尾會先取消所有 spawn_logged 背景任務（含隔離層的背景重測）。"""
    router = peers.PeerRouter("alpha", "beta")
    peers.install(monkeypatch, router)
    manager = ProtocolManager(router.registry())
    layout = fake_kernel_layout(tmp_path)
    monitor = IsolationMonitor(
        layout_provider=lambda: layout, selftest=_selftest_ok, platform="linux", sleep=_never
    )
    ready, stop = asyncio.Event(), asyncio.Event()

    async def lifespan() -> None:
        try:
            await monitor.ensure_ready()  # 開機自測，並以 spawn_logged 啟動背景重測
            for name in ("alpha", "beta"):
                await manager.connect_server(name, router.config(name))
            ready.set()
            await stop.wait()
        finally:  # main.py 的收尾順序：先取消所有 spawn_logged 背景任務，再收 MCP
            await cancel_all_background_tasks()
            await manager.cleanup()

    async with router.serving():
        host = asyncio.create_task(lifespan())
        await asyncio.wait_for(ready.wait(), timeout=_WAIT)
        reprobe = monitor._background
        router.failures["alpha"] = peers.remote_disconnect
        with pytest.raises(McpOperationException):
            await peers.ping(manager, "alpha")
        await _settle()
        reprobe_alive = reprobe is not None and not reprobe.done()
        host_alive = not host.done()
        stop.set()
        await asyncio.wait({host}, timeout=_WAIT)

    assert reprobe_alive, "a mid-flight MCP failure cancelled the isolation background re-probe"
    assert host_alive
