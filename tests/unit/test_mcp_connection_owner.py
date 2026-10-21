"""life1：每條 MCP 連線一個專屬 owner task 的行為測試。

外部可觸發的案例在 ``test_mcp_connection_isolation_security.py``；這裡釘 owner 的生命週期、
manager 的放置點與失聯通知、關機與 contextvars。假連線在 owner task 進入一個 anyio task group，
``fail()`` 讓 group 的子 task 拋錯，跟 SDK transport 途中失敗同一條取消路徑。真 stdio 案例用
``tests/fixtures/mcp_v2/lifecycle_stdio_server.py``（只用 ``sys.executable``，不連外）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import psutil
import pytest
from src.protocols import manager as manager_mod
from src.protocols.manager import ProtocolManager
from src.protocols.mcp import owner as owner_mod
from src.protocols.mcp.canonical import McpOperationException
from src.protocols.mcp.handles import LegacyMcpHandle, McpHandleKey
from src.protocols.mcp.owner import McpConnectionOwner, root_cause
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.utils.task_utils import cancel_all_background_tasks, spawn_logged

from tests.helpers import mcp_asgi_peers as peers
from tests.helpers.mcp_transport import _FakeTransport
from tests.support.isolation import PASSTHROUGH_MCP

_WAIT = 10.0
_STDIO_SERVER = Path(__file__).parents[1] / "fixtures" / "mcp_v2" / "lifecycle_stdio_server.py"
_CREATOR_MARK: ContextVar[str | None] = ContextVar("life1_creator_mark", default=None)


class _Sequenced:
    """每次連線輪流交給下一個假連線。"""

    def __init__(self, *transports: _FakeTransport) -> None:
        self._transports = iter(transports)

    async def connect(self, *args: Any, **kwargs: Any) -> Any:
        return await next(self._transports).connect(*args, **kwargs)


class _LostSpy:
    def __init__(self) -> None:
        self.calls: list[BaseException] = []
        self.cancelling: list[int] = []

    async def __call__(self, _owner: McpConnectionOwner, error: BaseException) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.cancelling.append(task.cancelling())
        await asyncio.sleep(0)  # 取消狀態沒清乾淨的話，這裡就會被取消
        self.calls.append(error)


class _Supervisor:
    def __init__(self) -> None:
        self.stops = 0

    async def stop(self) -> None:
        self.stops += 1


def _owner(
    transport: _FakeTransport,
    spy: _LostSpy,
    registry: set[McpConnectionOwner],
) -> McpConnectionOwner:
    return McpConnectionOwner("fake", transport.connect, on_lost=spy, registry=registry)


async def _until(predicate: Any, limit: float = 5.0) -> None:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    msg = "condition not reached"
    raise AssertionError(msg)


def _assert_ended_quietly(task: asyncio.Task[Any] | None) -> None:
    """結束時沒有未讀取的例外（不會出現 Task exception was never retrieved）。"""
    assert task is not None
    assert task.done()
    assert task.cancelled() or task.exception() is None


@pytest.fixture
def no_unretrieved(caplog: pytest.LogCaptureFixture) -> Any:
    yield
    assert peers.task_leak_messages(caplog) == []


@pytest.fixture
def short_close(monkeypatch: pytest.MonkeyPatch) -> tuple[float, float]:
    """把每條連線的關閉期限與寬限縮短（不真的等 7 秒）。"""
    close, grace = 0.5, 0.2
    for module in (owner_mod, manager_mod):
        monkeypatch.setattr(module, "OWNER_CLOSE_SECONDS", close)
        monkeypatch.setattr(module, "OWNER_CLOSE_GRACE_SECONDS", grace)
    return close, grace


# ---------------------------------------------------------------------------
# owner 本身
# ---------------------------------------------------------------------------


async def test_connect_runs_inside_owner_task_not_caller(no_unretrieved):
    transport, spy, registry = _FakeTransport(), _LostSpy(), set()
    owner = _owner(transport, spy, registry)

    client = await owner.start()

    assert client.transport is transport
    assert transport.entered_in == [owner.task]
    assert owner.task is not asyncio.current_task()
    assert registry == {owner}
    assert owner.alive
    await owner.aclose()
    _assert_ended_quietly(owner.task)
    assert registry == set()
    assert transport.closed == 1
    assert spy.calls == []
    assert not owner._hard_closed


async def test_transport_failure_calls_on_lost_once_with_root_cause_and_clean_cancel_state(
    no_unretrieved,
):
    transport, spy, registry = _FakeTransport(), _LostSpy(), set()
    owner = _owner(transport, spy, registry)
    await owner.start()

    transport.fail()  # stack.aclose() 會拋 ExceptionGroup，不能因此跳過 on_lost
    await _until(lambda: owner.task.done())
    await owner.aclose()  # 之後再關也不會第二次通知

    assert [type(error) for error in spy.calls] == [ConnectionResetError]
    assert spy.cancelling == [0]
    assert transport.closed == 1
    assert not owner.alive
    _assert_ended_quietly(owner.task)
    assert registry == set()


async def test_requested_close_never_calls_on_lost_even_if_transport_fails_while_closing(
    no_unretrieved,
):
    transport, spy, registry = _FakeTransport(close_gate=asyncio.Event()), _LostSpy(), set()
    owner = _owner(transport, spy, registry)
    await owner.start()

    closer = asyncio.create_task(owner.aclose())
    await _until(lambda: transport.closed == 1)
    transport.fail()  # 關閉途中傳輸出錯
    await asyncio.sleep(0.05)
    transport.close_gate.set()
    await asyncio.wait_for(closer, timeout=_WAIT)
    await asyncio.gather(owner.aclose(), owner.aclose())

    assert spy.calls == []
    assert not owner._hard_closed
    _assert_ended_quietly(owner.task)


async def test_close_within_deadline_is_not_a_hard_close(short_close, caplog, no_unretrieved):
    transport, spy, registry = _FakeTransport(shielded_close=0.1), _LostSpy(), set()
    owner = _owner(transport, spy, registry)
    await owner.start()

    await owner.aclose()

    assert not owner._hard_closed
    assert not [r for r in caplog.records if "close deadline" in r.getMessage()]


async def test_stuck_close_hits_owner_deadline_without_on_lost(short_close, no_unretrieved):
    close, grace = short_close
    transport, spy, registry = _FakeTransport(hang_close=True), _LostSpy(), set()
    owner = _owner(transport, spy, registry)
    await owner.start()

    started = time.monotonic()
    await owner.aclose()
    elapsed = time.monotonic() - started

    assert elapsed < close + grace + 0.3
    assert transport.hard_cancelled is True
    assert owner._hard_closed
    assert spy.calls == []
    _assert_ended_quietly(owner.task)
    assert registry == set()


async def test_cancelled_closer_reraises_without_hard_close(no_unretrieved):
    transport, spy, registry = _FakeTransport(close_gate=asyncio.Event()), _LostSpy(), set()
    owner = _owner(transport, spy, registry)
    await owner.start()

    closer = asyncio.create_task(owner.aclose())
    await _until(lambda: transport.closed == 1)
    closer.cancel()
    await asyncio.wait({closer}, timeout=_WAIT)
    still_closing = not owner.task.done()
    transport.close_gate.set()  # owner 在背景照常關完
    await _until(lambda: owner.task.done())

    assert closer.cancelled()
    assert still_closing
    assert not owner._hard_closed
    assert transport.hard_cancelled is False
    assert spy.calls == []


async def test_external_cancel_ends_owner_without_lost_notice(no_unretrieved):
    transport, spy, registry = _FakeTransport(), _LostSpy(), set()
    owner = _owner(transport, spy, registry)
    await owner.start()

    owner.task.cancel()  # 例如關機時事件迴圈取消全部 task
    await asyncio.wait({owner.task}, timeout=_WAIT)

    assert owner.task.cancelled()
    assert spy.calls == []
    assert transport.closed == 1


async def test_handshake_failure_reaches_caller_unchanged(no_unretrieved):
    registry: set[McpConnectionOwner] = set()

    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        msg = "handshake refused"
        raise ConnectionRefusedError(msg)

    owner = McpConnectionOwner("fake", refuse, on_lost=_LostSpy(), registry=registry)
    with pytest.raises(ConnectionRefusedError, match="handshake refused"):
        await owner.start()
    await _until(lambda: owner.task.done())

    _assert_ended_quietly(owner.task)
    assert registry == set()


async def test_handshake_base_exception_leaves_nothing_unretrieved(no_unretrieved):
    async def explode(*_args: Any, **_kwargs: Any) -> Any:
        msg = "boom"
        raise BaseExceptionGroup(msg, [asyncio.CancelledError(), ValueError("x")])

    owner = McpConnectionOwner("fake", explode, on_lost=_LostSpy(), registry=set())
    with pytest.raises(ConnectionError):
        await owner.start()
    await _until(lambda: owner.task.done())

    assert not owner.task.cancelled()
    assert owner.task.exception() is None


@pytest.mark.parametrize("stop", [KeyboardInterrupt, SystemExit])
def test_interpreter_stop_during_handshake_is_not_swallowed(stop):
    """KeyboardInterrupt／SystemExit 和取消一樣照樣往外拋（另開事件迴圈，免得停掉整個測試）。"""
    holder: dict[str, McpConnectionOwner] = {}

    async def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise stop

    async def connect() -> None:
        holder["owner"] = McpConnectionOwner("fake", explode, on_lost=_LostSpy(), registry=set())
        await holder["owner"].start()

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(stop):
            loop.run_until_complete(connect())
        assert isinstance(holder["owner"].task.exception(), stop)
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    finally:
        loop.close()


async def test_close_error_is_logged_without_details(caplog, no_unretrieved):
    leaked = "https://mcp.example.test/mcp?api_key=sk-life1SECRET0123456789"
    transport = _FakeTransport(close_error=RuntimeError(leaked))
    owner = _owner(transport, _LostSpy(), set())
    await owner.start()

    await owner.aclose()

    messages = [record.getMessage() for record in caplog.records]
    assert [m for m in messages if "closed with error: RuntimeError" in m]
    assert not [m for m in messages if "sk-life1SECRET" in m or "api_key" in m]


def test_root_cause_skips_cancellations_inside_groups():
    error = BaseExceptionGroup(
        "outer",
        [asyncio.CancelledError(), ExceptionGroup("inner", [ConnectionResetError("x")])],
    )
    assert isinstance(root_cause(error), ConnectionResetError)
    cancelled = asyncio.CancelledError()
    assert root_cause(cancelled) is cancelled


# ---------------------------------------------------------------------------
# manager：放置點、失聯通知、關機
# ---------------------------------------------------------------------------


def _fake_manager(monkeypatch: pytest.MonkeyPatch, transport: Any, *names: str) -> ProtocolManager:
    monkeypatch.setattr(manager_mod, "connect_mcp_server", transport.connect)
    registry = ProtocolRegistry("__missing_owner_registry__.json")
    registry.servers = {name: ProtocolServerConfig(command=name) for name in names or ("alpha",)}
    manager = ProtocolManager(registry)
    monkeypatch.setattr(
        manager, "connect_server", partial(manager.connect_server, launch=PASSTHROUGH_MCP)
    )
    return manager


def _live_owners(manager: ProtocolManager) -> list[McpConnectionOwner]:
    return [owner for owner in manager._connection_owners if not owner.task.done()]


@pytest.mark.parametrize("style", ["asyncio-once", "asyncio-twice", "anyio-scope"])
async def test_caller_cancelled_during_handshake_leaves_only_after_owner_finished(
    monkeypatch, no_unretrieved, style
):
    transport = _FakeTransport(gate=asyncio.Event(), abort_delay=0.3)
    manager = _fake_manager(monkeypatch, transport)
    config = manager.registry.get_server("alpha")
    observed: dict[str, Any] = {}

    async def request() -> None:
        with anyio.CancelScope() as scope:
            observed["scope"] = scope
            try:
                await manager.connect_server("alpha", config)
            finally:
                observed["running_when_left"] = _live_owners(manager)
                observed["abort_finished_when_left"] = transport.abort_finished

    caller = asyncio.create_task(request())
    await _until(lambda: transport.entered_in)
    if style == "anyio-scope":
        observed["scope"].cancel()  # starlette 請求斷線屬這類，anyio 會一直重送取消
    else:
        caller.cancel()
        if style == "asyncio-twice":
            await asyncio.sleep(0.05)
            caller.cancel()
    await asyncio.wait({caller}, timeout=_WAIT)

    assert observed["running_when_left"] == []
    assert observed["abort_finished_when_left"] == 1
    assert manager.handle_pool.get(manager.handle_key_for("alpha", None)) is None
    assert manager._connection_owners == set()


async def test_concurrent_connects_keep_one_live_owner(monkeypatch, no_unretrieved):
    transport = _FakeTransport()
    manager = _fake_manager(monkeypatch, transport)
    config = manager.registry.get_server("alpha")

    await asyncio.gather(
        manager.connect_server("alpha", config),
        manager.connect_server("alpha", config),
    )

    assert len(transport.entered_in) == 2
    assert len(_live_owners(manager)) == 1
    installed = manager.handle_pool.get(manager.handle_key_for("alpha", None))
    assert installed.stack in _live_owners(manager)
    assert transport.closed == 1  # 被換掉的那條已關，不留孤兒
    await manager.cleanup()
    assert manager._connection_owners == set()


async def test_install_swaps_in_new_handle_and_stops_old_subscription_before_closing(
    monkeypatch, no_unretrieved
):
    transport = _FakeTransport(close_gate=asyncio.Event())
    manager = _fake_manager(monkeypatch, transport)
    config = manager.registry.get_server("alpha")
    key = manager.handle_key_for("alpha", None)
    await manager.connect_server("alpha", config)
    old = manager.handle_pool.get(key).stack
    supervisor = _Supervisor()
    manager._subscription_supervisors[key] = supervisor

    second = asyncio.create_task(manager.connect_server("alpha", config))
    await _until(lambda: transport.closed == 1)  # 舊的正在關
    during_close = manager.handle_pool.get(key).stack
    transport.close_gate.set()
    await asyncio.wait_for(second, timeout=_WAIT)

    assert during_close is not old  # 關舊的期間，新請求已拿到新連線
    assert during_close is manager.handle_pool.get(key).stack
    assert supervisor.stops == 1
    assert old.task.done()
    await manager.cleanup()


async def test_install_handle_lets_caller_cancellation_through(monkeypatch, no_unretrieved):
    transport = _FakeTransport(close_gate=asyncio.Event())
    manager = _fake_manager(monkeypatch, transport)
    config = manager.registry.get_server("alpha")
    await manager.connect_server("alpha", config)
    old = manager.handle_pool.get(manager.handle_key_for("alpha", None)).stack

    second = asyncio.create_task(manager.connect_server("alpha", config))
    await _until(lambda: transport.closed == 1)  # 正在關被換掉的舊連線
    second.cancel()
    await asyncio.wait({second}, timeout=_WAIT)
    transport.close_gate.set()
    await _until(lambda: old.task.done())  # 舊 owner 在背景照常關完

    assert second.cancelled()
    assert not old._hard_closed
    await manager.cleanup()


async def test_loss_during_install_keeps_lost_error(monkeypatch, no_unretrieved):
    old_transport = _FakeTransport(close_gate=asyncio.Event())
    new_transport = _FakeTransport()
    manager = _fake_manager(monkeypatch, _Sequenced(old_transport, new_transport))
    config = manager.registry.get_server("alpha")
    key = manager.handle_key_for("alpha", None)
    await manager.connect_server("alpha", config)

    second = asyncio.create_task(manager.connect_server("alpha", config))
    await _until(lambda: old_transport.closed == 1)
    new_owner = manager.handle_pool.get(key).stack
    new_transport.fail()  # 新連線在舊連線還在關的時候斷了
    await _until(lambda: new_owner.task.done())
    old_transport.close_gate.set()
    await asyncio.wait_for(second, timeout=_WAIT)

    assert (manager._installations["alpha"].last_error or "").startswith("MCP connection lost")
    await manager.cleanup()


async def test_add_server_twice_keeps_one_live_owner(monkeypatch, no_unretrieved):
    transport = _FakeTransport()
    manager = _fake_manager(monkeypatch, transport)
    config = manager.registry.get_server("alpha")

    assert await manager.add_server("alpha", config)
    assert await manager.add_server("alpha", config)

    assert len(_live_owners(manager)) == 1
    assert transport.closed == 1
    await manager.cleanup()


async def test_connection_lost_records_and_stops_subscription_without_reconnect(
    monkeypatch, no_unretrieved
):
    transport = _FakeTransport()
    manager = _fake_manager(monkeypatch, transport)
    token = _CREATOR_MARK.set("creator")
    try:
        await manager.connect_server("alpha", manager.registry.get_server("alpha"))
    finally:
        _CREATOR_MARK.reset(token)
    key = manager.handle_key_for("alpha", None)
    owner = manager.handle_pool.get(key).stack
    supervisor = _Supervisor()
    manager._subscription_supervisors[key] = supervisor
    reconnects: list[str] = []
    hooks: list[tuple[Any, ...]] = []

    async def no_reconnect(name: str, *_args: Any, **_kwargs: Any) -> None:
        reconnects.append(name)

    def hook(*args: Any) -> None:
        hooks.append((*args, _CREATOR_MARK.get()))

    monkeypatch.setattr(manager, "connect_server", no_reconnect)
    monkeypatch.setattr(manager, "_connection_lost_hook", hook)
    transport.fail()
    await _until(lambda: owner.task.done())

    assert reconnects == []
    ((hook_key, hook_error, hook_owner, creator_mark),) = hooks
    assert hook_key == key
    assert isinstance(hook_error, ConnectionResetError)
    assert hook_owner is owner
    assert creator_mark == "creator"  # hook 在 owner task 內，會繼承建立者的 contextvars
    assert "ConnectionResetError" in (manager._installations["alpha"].last_error or "")
    assert supervisor.stops == 1
    assert key not in manager._subscription_supervisors
    # handle 留在 pool：之後快速失敗，維持手動 restart
    assert manager.handle_pool.get(key).stack is owner
    await manager.cleanup()


async def test_loss_of_replaced_owner_is_ignored(monkeypatch, no_unretrieved):
    old_transport = _FakeTransport(close_gate=asyncio.Event())
    new_transport = _FakeTransport()
    manager = _fake_manager(monkeypatch, _Sequenced(old_transport, new_transport))
    config = manager.registry.get_server("alpha")
    key = manager.handle_key_for("alpha", None)
    await manager.connect_server("alpha", config)
    old = manager.handle_pool.get(key).stack
    hooks: list[tuple[Any, ...]] = []
    monkeypatch.setattr(manager, "_connection_lost_hook", lambda *args: hooks.append(args))

    second = asyncio.create_task(manager.connect_server("alpha", config))
    await _until(lambda: old_transport.closed == 1)  # 舊的已被換掉、正在關
    await manager._on_connection_lost(key, old, ConnectionResetError("old"))
    error_after = manager._installations["alpha"].last_error
    old_transport.close_gate.set()
    await asyncio.wait_for(second, timeout=_WAIT)

    assert error_after is None  # 不把舊連線的事記到新連線
    assert hooks == []
    await manager.cleanup()


class _RacingSupervisor:
    def __init__(self, during_stop: Any = None) -> None:
        self.during_stop = during_stop
        self.stops = 0

    async def stop(self) -> None:
        self.stops += 1
        if self.during_stop is not None:
            await self.during_stop()


async def test_stop_subscriptions_tolerates_lost_handler_popping_first(monkeypatch):
    transport = _FakeTransport()
    manager = _fake_manager(monkeypatch, transport)
    await manager.connect_server("alpha", manager.registry.get_server("alpha"))
    shared = manager.handle_key_for("alpha", None)
    principal = McpHandleKey("alpha", "principal-1", "2026-07-28", 1)
    other = McpConnectionOwner("p1", transport.connect, on_lost=_LostSpy(), registry=set())
    manager.handle_pool.put(principal, LegacyMcpHandle(session=object(), stack=other))
    second = _RacingSupervisor()

    async def lose_principal() -> None:  # 第一個 stop 等待時，第二個 key 失聯先停掉自己的訂閱
        await manager._on_connection_lost(principal, other, ConnectionResetError("x"))

    manager._subscription_supervisors[shared] = _RacingSupervisor(lose_principal)
    manager._subscription_supervisors[principal] = second

    await manager._stop_subscriptions_for_installation("alpha")

    assert second.stops == 1
    assert manager._subscription_supervisors == {}
    manager.handle_pool.pop_principal("principal-1")
    await manager.cleanup()


async def test_cleanup_closes_stuck_connections_concurrently_within_one_budget(
    monkeypatch, short_close, no_unretrieved
):
    close, grace = short_close
    transport = _FakeTransport(hang_close=True)
    manager = _fake_manager(monkeypatch, transport, "alpha", "beta", "gamma")
    for name in ("alpha", "beta", "gamma"):
        await manager.connect_server(name, manager.registry.get_server(name))

    started = time.monotonic()
    await manager.cleanup()
    elapsed = time.monotonic() - started

    assert elapsed < close + grace + 0.3  # 逐台關會是三倍
    assert transport.hard_cancelled is True
    assert manager._connection_owners == set()


async def test_cleanup_warns_once_about_connection_outliving_the_budget(
    monkeypatch, short_close, caplog, no_unretrieved
):
    close, grace = short_close
    transport = _FakeTransport(shielded_close=close + grace + 0.5)
    manager = _fake_manager(monkeypatch, transport)
    await manager.connect_server("alpha", manager.registry.get_server("alpha"))
    (owner,) = _live_owners(manager)

    started = time.monotonic()
    await manager.cleanup()
    elapsed = time.monotonic() - started
    left = [o.label for o in _live_owners(manager)]
    await _until(lambda: owner.task.done())

    assert elapsed < close + grace + 0.3  # 不再等
    assert left == ["alpha"]  # 不保證集合清空
    warnings = [r.getMessage() for r in caplog.records if "still closing" in r.getMessage()]
    assert warnings == ["MCP cleanup: 1 connection(s) still closing: ['alpha']"]


async def test_cleanup_closes_handle_installed_while_it_runs_normally(monkeypatch, no_unretrieved):
    transport = _FakeTransport()
    manager = _fake_manager(monkeypatch, transport)
    config = manager.registry.get_server("alpha")
    cleanups: list[asyncio.Task[None]] = []
    # 握手剛完成、呼叫端還沒裝進 pool 時 cleanup 開始跑。
    transport.before_return = lambda: cleanups.append(asyncio.create_task(manager.cleanup()))

    await manager.connect_server("alpha", config)
    await asyncio.wait_for(cleanups[0], timeout=_WAIT)

    assert manager.handle_pool.get(manager.handle_key_for("alpha", None)) is None
    assert transport.closed == 1
    assert transport.aborted == 0
    assert transport.hard_cancelled is False
    assert manager._connection_owners == set()


async def test_handshake_aborted_by_cleanup_is_not_retried(monkeypatch, no_unretrieved):
    attempts: list[_FakeTransport] = []

    async def connect(*args: Any, **kwargs: Any) -> Any:
        transport = _FakeTransport(gate=asyncio.Event() if not attempts else None)
        attempts.append(transport)
        return await transport.connect(*args, **kwargs)

    manager = _fake_manager(monkeypatch, SimpleNamespace(connect=connect))
    config = manager.registry.get_server("alpha")
    caller = asyncio.create_task(manager.connect_server("alpha", config))
    await _until(lambda: attempts and attempts[0].entered_in)

    await manager.cleanup()
    await asyncio.wait({caller}, timeout=_WAIT)  # 真的 with_retry：重試會在 1 秒後
    await asyncio.sleep(1.2)

    assert caller.exception() is None
    assert len(attempts) == 1
    assert attempts[0].aborted == 1
    assert manager.handle_pool.get(manager.handle_key_for("alpha", None)) is None
    assert manager._connection_owners == set()


async def test_background_shutdown_does_not_cancel_owner(monkeypatch, no_unretrieved):
    transport = _FakeTransport()
    manager = _fake_manager(monkeypatch, transport)
    await manager.connect_server("alpha", manager.registry.get_server("alpha"))
    spawn_logged(asyncio.Event().wait(), name="life1-background")

    assert await cancel_all_background_tasks() >= 1

    assert len(_live_owners(manager)) == 1
    await manager.cleanup()
    assert manager._connection_owners == set()
    assert transport.closed == 1


async def test_owner_drops_workload_ticket_after_ready(monkeypatch, no_unretrieved):
    marker = object()
    seen_in_connect: list[Any] = []
    transport = _FakeTransport()

    async def connect(*args: Any, **kwargs: Any) -> Any:
        seen_in_connect.append(manager_mod._CURRENT_WORKLOAD_AUTHORIZATION.get())
        return await transport.connect(*args, **kwargs)

    manager = _fake_manager(monkeypatch, SimpleNamespace(connect=connect))
    token = manager_mod._CURRENT_WORKLOAD_AUTHORIZATION.set(marker)
    try:
        await manager.connect_server("alpha", manager.registry.get_server("alpha"))
        assert manager_mod._CURRENT_WORKLOAD_AUTHORIZATION.get() is marker
    finally:
        manager_mod._CURRENT_WORKLOAD_AUTHORIZATION.reset(token)
    (owner,) = _live_owners(manager)

    assert seen_in_connect == [marker]  # 握手時仍看得到呼叫端的 bootstrap 票
    assert owner.task.get_context()[manager_mod._CURRENT_WORKLOAD_AUTHORIZATION] is None
    await manager.cleanup()


async def test_subscription_supervisor_starts_without_workload_ticket(monkeypatch, no_unretrieved):
    @asynccontextmanager
    async def listen(**_filters: Any) -> Any:
        await asyncio.Event().wait()
        yield

    session = SimpleNamespace(
        protocol_version="2026-07-28",
        listen=listen,
        server_capabilities=SimpleNamespace(tools=SimpleNamespace(list_changed=True)),
    )
    manager = _fake_manager(monkeypatch, _FakeTransport(session=session))
    key = manager.handle_key_for("alpha", None)
    token = manager_mod._CURRENT_WORKLOAD_AUTHORIZATION.set(object())
    try:
        await manager.connect_server("alpha", manager.registry.get_server("alpha"))
    finally:
        manager_mod._CURRENT_WORKLOAD_AUTHORIZATION.reset(token)
    supervisor_task = manager._subscription_supervisors[key]._task

    assert supervisor_task.get_context()[manager_mod._CURRENT_WORKLOAD_AUTHORIZATION] is None
    await manager.cleanup()


# ---------------------------------------------------------------------------
# 真 SDK：建立者已結束、MCP 頁、送出 context、Dispatch 下線一段時間
# ---------------------------------------------------------------------------


async def test_failure_after_creator_finished_spares_its_other_connection(
    monkeypatch, caplog, no_unretrieved
):
    router = peers.PeerRouter("alpha", "beta")
    peers.install(monkeypatch, router)
    manager = ProtocolManager(router.registry())

    async with router.serving():

        async def first_request() -> None:  # 聊天請求依序建立兩條後結束
            assert await manager.ensure_connected("alpha")
            assert await manager.ensure_connected("beta")

        await asyncio.create_task(first_request())
        router.failures["alpha"] = peers.remote_disconnect
        with pytest.raises(McpOperationException):
            await peers.ping(manager, "alpha")
        await asyncio.sleep(0.05)
        beta = await peers.ping(manager, "beta")
        await manager.cleanup()

    messages = [record.getMessage() for record in caplog.records]
    assert "pong-beta" in beta
    assert [m for m in messages if "MCP 連線中斷 server=alpha" in m]
    assert not [m for m in messages if "cancel scope" in m]
    assert manager._connection_owners == set()


async def _cancel_post_like_workload_fence(request: Any) -> Any:
    """workload fence 的 ``cancel_tasks`` 會原生取消送出中的 POST 子 task。"""
    task = asyncio.current_task()
    assert task is not None
    task.cancel()
    await asyncio.sleep(0)
    raise AssertionError(request)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(peers.remote_disconnect, id="remote-disconnect"),
        # 子 task 被原生取消時 group 不帶例外、Client 會正常退出（屬性改拋 RuntimeError）。
        pytest.param(_cancel_post_like_workload_fence, id="post-task-cancelled"),
    ],
)
async def test_lost_connection_shows_error_on_mcp_page_instead_of_empty_list(
    monkeypatch, no_unretrieved, failure
):
    router = peers.PeerRouter("alpha", "beta")
    peers.install(monkeypatch, router)
    manager = ProtocolManager(router.registry())
    forward = router.handle_async_request
    hooks: list[Any] = []

    def hook(*args: Any) -> None:  # 記下通知當下的失聯紀錄（之後的呼叫或健康檢查會蓋掉）
        hooks.append((*args, manager._installations["alpha"].last_error))

    monkeypatch.setattr(manager, "_connection_lost_hook", hook)

    async def route(request: Any) -> Any:
        if failure is _cancel_post_like_workload_fence and request.method == "POST":
            method = json.loads(await request.aread() or b"{}").get("method")
            if request.url.port == router.ports["alpha"] and method == "tools/call":
                return await failure(request)
        return await forward(request)

    monkeypatch.setattr(router, "handle_async_request", route)
    async with router.serving():
        for name in ("alpha", "beta"):
            await manager.connect_server(name, router.config(name))
        owner = manager.handle_pool.get(manager.handle_key_for("alpha", None)).stack
        if failure is peers.remote_disconnect:
            router.failures["alpha"] = failure
        with pytest.raises(McpOperationException):
            await peers.ping(manager, "alpha")
        await _until(lambda: owner.task.done())
        # 已關閉的 SDK Client 讀屬性會拋 RuntimeError；MCP 頁與工具清單不能因此整個失敗。
        info = {entry["name"]: entry for entry in await manager.get_active_server_info()}
        tools = await manager.get_all_tools()
        await manager.cleanup()

    ((_key, _error, hook_owner, lost_record),) = hooks
    assert hook_owner is owner
    assert (lost_record or "").startswith("MCP connection lost")  # 來自失聯通知，不是健康檢查
    assert info["alpha"]["status"] == "error"
    assert info["beta"]["status"] == "connected"
    assert {tool["server"] for tool in tools} == {"beta"}


async def test_sends_use_caller_context_while_owner_holds_no_ticket(monkeypatch, no_unretrieved):
    router = peers.PeerRouter("alpha")
    peers.install(monkeypatch, router)
    manager = ProtocolManager(router.registry())
    ticket = manager_mod._CURRENT_WORKLOAD_AUTHORIZATION
    seen: list[tuple[str, Any]] = []
    forward = router.handle_async_request

    async def observe(request: Any) -> Any:
        if request.method == "POST":
            method = json.loads(await request.aread() or b"{}").get("method")
            seen.append((method, ticket.get()))
        return await forward(request)

    monkeypatch.setattr(router, "handle_async_request", observe)
    async with router.serving():
        token = ticket.set("bootstrap-ticket")
        try:
            await manager.connect_server("alpha", router.config("alpha"))
        finally:
            ticket.reset(token)
        token = ticket.set("call-ticket")
        try:
            await peers.ping(manager, "alpha")
        finally:
            ticket.reset(token)
        (owner,) = _live_owners(manager)
        owner_ticket = owner.task.get_context()[ticket]
        await manager.cleanup()

    assert ("server/discover", "bootstrap-ticket") in seen
    assert ("tools/call", "call-ticket") in seen
    assert owner_ticket is None


async def test_dispatch_offline_window_then_restart_recovers(monkeypatch, no_unretrieved):
    router = peers.PeerRouter("dispatch", "beta")
    peers.install(monkeypatch, router)
    manager = ProtocolManager(router.registry())

    async with router.serving():
        ready = asyncio.Event()
        stop = asyncio.Event()

        async def lifespan() -> str:
            for name in ("dispatch", "beta"):
                await manager.connect_server(name, router.config(name))
            ready.set()
            await stop.wait()
            return "completed"

        host = asyncio.create_task(lifespan())
        await asyncio.wait_for(ready.wait(), timeout=_WAIT)
        router.fail_methods = None  # 下線期間所有 POST 都連不上
        router.failures["dispatch"] = peers.connect_error
        for _ in range(2):
            with pytest.raises(McpOperationException):
                await peers.ping(manager, "dispatch")
        await asyncio.sleep(0.05)
        router.failures["dispatch"] = None  # 重新上線
        restarted = await asyncio.create_task(manager.restart_server("dispatch"))
        recovered = await peers.ping(manager, "dispatch")
        beta = await peers.ping(manager, "beta")
        alive = not host.done()
        stop.set()
        result = await asyncio.wait_for(host, timeout=_WAIT)
        await manager.cleanup()

    assert alive
    assert result == "completed"
    assert restarted is True
    assert "pong-dispatch" in recovered
    assert "pong-beta" in beta
    assert router.handshakes("dispatch") == 2  # 只有手動 restart 那一次重連


# ---------------------------------------------------------------------------
# 真 stdio：期限到的硬關仍讓 SDK 殺掉行程；伺服器自己結束時目前不通知
# ---------------------------------------------------------------------------


def _stdio_manager(monkeypatch: pytest.MonkeyPatch, pid_file: Path, *extra: str) -> ProtocolManager:
    connect = manager_mod.connect_mcp_server

    async def sandboxed(config: Any, **kwargs: Any) -> Any:  # 子行程不帶 Proxy 環境
        return await connect(config, launch_environment={}, **kwargs)

    monkeypatch.setattr(manager_mod, "connect_mcp_server", sandboxed)
    registry = ProtocolRegistry("__missing_owner_stdio_registry__.json")
    registry.servers = {
        "alpha": ProtocolServerConfig(
            command=sys.executable,
            args=[str(_STDIO_SERVER), str(pid_file), *extra],
        )
    }
    manager = ProtocolManager(registry)
    monkeypatch.setattr(
        manager, "connect_server", partial(manager.connect_server, launch=PASSTHROUGH_MCP)
    )
    return manager


def _process_alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _kill_quietly(pid: int) -> None:
    with suppress(psutil.NoSuchProcess):
        psutil.Process(pid).kill()


async def test_stubborn_stdio_server_is_killed_after_the_close_deadline(
    monkeypatch, tmp_path, short_close, no_unretrieved
):
    pid_file = tmp_path / "pid"
    manager = _stdio_manager(monkeypatch, pid_file, "ignore-eof")
    await manager.connect_server("alpha", manager.registry.get_server("alpha"))
    pid = int(pid_file.read_text(encoding="utf-8"))
    owner = manager.handle_pool.get(manager.handle_key_for("alpha", None)).stack
    try:
        # 期限（0.5 秒）比 SDK 等伺服器自己結束的時間（2 秒）短：硬關要讓 SDK 的 shielded
        # 收尾跑完並殺掉行程，不能留下孤兒或卡住的 owner。
        await manager._close_handles(manager.handle_pool.pop_all())
        await _until(lambda: owner.task.done(), limit=_WAIT)
        await _until(lambda: not _process_alive(pid), limit=_WAIT)
    finally:
        _kill_quietly(pid)
        await manager.cleanup()

    assert owner._hard_closed
    assert manager._connection_owners == set()


async def test_stdio_server_exit_is_not_reported_until_the_next_call(
    monkeypatch, tmp_path, no_unretrieved
):
    """釘住目前行為（已知限制）：SDK 沒有可 await 的「讀端結束」訊號，stdio 自己結束不通知。"""
    pid_file = tmp_path / "pid"
    manager = _stdio_manager(monkeypatch, pid_file)
    hooks: list[Any] = []
    monkeypatch.setattr(manager, "_connection_lost_hook", lambda *args: hooks.append(args))
    await manager.connect_server("alpha", manager.registry.get_server("alpha"))
    pid = int(pid_file.read_text(encoding="utf-8"))
    owner = manager.handle_pool.get(manager.handle_key_for("alpha", None)).stack
    context = peers.request_context("alpha")
    try:
        with pytest.raises(McpOperationException):
            await manager.call_tool_on_server("alpha", "die", {}, request_context=context)
        await _until(lambda: not _process_alive(pid), limit=_WAIT)
        await asyncio.sleep(0.5)
        owner_running = not owner.task.done()
    finally:
        _kill_quietly(pid)
        await manager.cleanup()

    assert owner_running
    assert hooks == []
    assert not (manager._installations["alpha"].last_error or "").startswith("MCP connection lost")
