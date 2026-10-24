"""U3e-2：啟動判定、連線序列化與失聯重連；全部使用 sandbox 與假 transport。"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, suppress
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.shared.exceptions import MCPError
from src import main as main_mod
from src.core import retry as retry_mod
from src.models.identity import AgentIdentity
from src.protocols import manager as mod
from src.protocols.mcp.canonical import McpOperationException, McpRequestContext
from src.protocols.mcp.handles import LegacyMcpHandle
from src.protocols.mcp.isolated_stdio import McpServerExited
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.routers import mcp as mcp_router
from src.services.isolation.mcp_launch import (
    McpLaunch,
    McpLaunchBook,
    McpLaunchInfo,
    McpLaunchWaiting,
)

from tests.helpers.mcp_transport import _FakeTransport

_MARK = ContextVar("u3e_creator", default=None)
_INFO = McpLaunchInfo("self", "creation", "ok", "launched", "landlock")
_CODE = "MCP_STDIO_PRINCIPAL_UNSUPPORTED"


def context(name="s"):
    return McpRequestContext(
        request_id="u3e",
        human_principal_id="human",
        identity=AgentIdentity.for_role("Probe", "Debugger"),
        installation_id=name,
        auth_level="Manual",
        origin="foreground",
    )


@pytest.fixture
def harness(monkeypatch):
    registry = ProtocolRegistry("__no_u3e_config__.json")
    registry.servers = {"s": ProtocolServerConfig(command="fake")}
    manager = mod.ProtocolManager(registry)
    manager._book = McpLaunchBook()
    manager._launcher = SimpleNamespace(plan_server_launch=AsyncMock(return_value=McpLaunch(_INFO)))
    manager._monitor = SimpleNamespace(
        ensure_ready=AsyncMock(),
        on_level_change=Mock(),
        local_decision=Mock(),
        report_failure=Mock(),
    )
    manager._isolation_reader = Mock(return_value=object())
    monkeypatch.setattr(mod, "discover_ide_servers", AsyncMock(return_value={}))
    monkeypatch.setattr(manager, "_start_subscription", Mock())
    return manager


def fake_connection(monkeypatch, manager):
    connect = AsyncMock()

    async def install(name, config, request_context=None, **kwargs):
        key = manager.handle_key_for(name, request_context)
        manager.handle_pool.put(key, LegacyMcpHandle(SimpleNamespace(), AsyncExitStack()))

    connect.side_effect = install
    monkeypatch.setattr(manager, "connect_server", connect)
    return connect


async def settle(manager):
    async with asyncio.timeout(2):
        while manager._relaunch_pending:
            await asyncio.sleep(0.001)


async def test_old_reload_cannot_replace_a_completed_new_connection(harness, monkeypatch):
    manager = harness
    slow = ProtocolServerConfig(command="slow")
    old = ProtocolServerConfig(command="old")
    new = ProtocolServerConfig(command="new")
    manager.registry.servers = {"slow": slow, "target": old}
    entered, resume = asyncio.Event(), asyncio.Event()
    connect = fake_connection(monkeypatch, manager)

    async def judge(name, *args, **kwargs):
        if name == "slow":
            entered.set()
            await resume.wait()
        return McpLaunch(_INFO)

    manager._launcher.plan_server_launch.side_effect = judge
    first = asyncio.create_task(manager.initialize_all())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        manager.registry.servers = {"target": new}
        await asyncio.wait_for(manager.initialize_all(), 1)
        key = manager.handle_key_for("target", None)
        installed = manager.handle_pool.get(key)
        resume.set()
        await asyncio.wait_for(first, 1)
        assert manager.handle_pool.get(key).stack is installed.stack
        assert [call.args[1].command for call in connect.await_args_list] == ["new"]
    finally:
        resume.set()
        await asyncio.gather(first, return_exceptions=True)
        await manager.cleanup()


async def test_retry_does_not_adopt_a_new_generation(harness, monkeypatch):
    config = harness.registry.servers["s"]
    transport = AsyncMock(side_effect=ConnectionError("handshake failed"))
    monkeypatch.setattr(mod, "connect_mcp_server", transport)

    async def change_config(_delay):
        harness.registry.servers["s"] = config.model_copy(update={"env": {"KEY": "new"}})
        harness._refresh_installations()

    monkeypatch.setattr(retry_mod.asyncio, "sleep", change_config)
    with suppress(ConnectionError):
        await harness.connect_server("s", config, **await harness._judge("s", config))
    assert transport.await_count == 1


@pytest.mark.parametrize("prior", [None, "old"])
async def test_hot_mount_publishes_config_and_connects_transport(harness, monkeypatch, prior):
    manager = harness
    manager.registry.servers = {} if prior is None else {
        "hot": ProtocolServerConfig(command=prior)
    }
    manager._refresh_installations()
    config = ProtocolServerConfig(command="new")
    transport = _FakeTransport()
    connect = AsyncMock(side_effect=transport.connect)
    monkeypatch.setattr(mod, "connect_mcp_server", connect)
    try:
        assert await manager.add_server("hot", config)
        connect.assert_awaited_once()
        assert connect.await_args.args[0] == config
        assert manager.registry.get_server("hot") == config
        assert manager.handle_pool.get(manager.handle_key_for("hot", None)).stack.alive
    finally:
        await manager.shutdown()


@pytest.mark.parametrize("stage", ["close", "judge"])
async def test_hot_mount_cannot_restore_a_generation_superseded_by_reload(
    harness, monkeypatch, stage
):
    manager = harness
    transport = _FakeTransport()
    connect = AsyncMock(side_effect=transport.connect)
    monkeypatch.setattr(mod, "connect_mcp_server", connect)
    assert await manager.add_server("s", manager.registry.servers["s"])
    connect.reset_mock()
    entered, resume = asyncio.Event(), asyncio.Event()
    original = manager._close_handles

    async def close(handles, **kwargs):
        if stage == "close" and handles:
            entered.set()
            await resume.wait()
        await original(handles, **kwargs)

    async def judge(name, config, *args, **kwargs):
        if stage == "judge" and config.command == "obsolete":
            entered.set()
            await resume.wait()
        return McpLaunch(_INFO)

    monkeypatch.setattr(manager, "_close_handles", close)
    manager._launcher.plan_server_launch.side_effect = judge
    task = asyncio.create_task(manager.add_server("s", ProtocolServerConfig(command="obsolete")))
    reload_task = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        current = ProtocolServerConfig(command="current")
        manager.registry.servers["s"] = current
        epoch = manager._reload_epoch
        reload_task = asyncio.create_task(manager.initialize_all())
        async with asyncio.timeout(1):
            while manager._reload_epoch == epoch:
                await asyncio.sleep(0)
        generation = manager.get_installation_generation("s")
        resume.set()
        assert not await asyncio.wait_for(task, 1)
        await asyncio.wait_for(reload_task, 1)
        assert manager.get_installation_generation("s") == generation
        assert manager._installations["s"].config == current
        assert [call.args[0].command for call in connect.await_args_list] == ["current"]
        assert manager.handle_pool.get(manager.handle_key_for("s", None)).stack.alive
    finally:
        resume.set()
        await asyncio.gather(task, *([reload_task] if reload_task else []), return_exceptions=True)
        await manager.shutdown()


async def test_reload_snapshot_cannot_close_a_newer_hot_mount(harness, monkeypatch):
    manager = harness
    manager.registry.servers = {
        "slow": ProtocolServerConfig(command="slow"),
        "hot": ProtocolServerConfig(command="old"),
    }
    entered, resume = asyncio.Event(), asyncio.Event()
    async def connect(*args, **kwargs):
        return await _FakeTransport().connect()

    transport = AsyncMock(side_effect=connect)
    monkeypatch.setattr(mod, "connect_mcp_server", transport)

    async def judge(name, *args, **kwargs):
        if name == "slow":
            entered.set()
            await resume.wait()
        return McpLaunch(_INFO)

    manager._launcher.plan_server_launch.side_effect = judge
    reload_task = asyncio.create_task(manager.initialize_all())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await manager.add_server("hot", ProtocolServerConfig(command="new"))
        owner = manager.handle_pool.get(manager.handle_key_for("hot", None)).stack
        resume.set()
        await asyncio.wait_for(reload_task, 1)
        handle = manager.handle_pool.get(manager.handle_key_for("hot", None))
        assert handle is not None and handle.stack is owner and owner.alive
        assert [call.args[0].command for call in transport.await_args_list] == ["new", "slow"]
    finally:
        resume.set()
        await asyncio.gather(reload_task, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["initialize", "ensure", "add", "restart", "background"])
@pytest.mark.parametrize("waiting", [False, True])
async def test_decision_failure_never_connects(harness, monkeypatch, path, waiting):
    manager = harness
    connect = fake_connection(monkeypatch, manager)
    error = (
        McpLaunchWaiting("waiting_isolation", "none") if waiting else RuntimeError("judge failed")
    )
    manager._launcher.plan_server_launch.side_effect = error
    if path == "initialize":
        await manager.initialize_all()
    elif path == "ensure":
        with pytest.raises(McpOperationException) as caught:
            await manager._ensure_handle("s", context())
        assert caught.value.error.kind == "transport"
    elif path == "add":
        assert not await manager.add_server("s", manager.registry.servers["s"])
    elif path == "restart":
        assert not await manager.restart_server("s")
    else:
        manager._schedule_relaunch("s")
        await settle(manager)
    connect.assert_not_called()
    assert manager._launcher.plan_server_launch.await_count == 1
    last = manager._server_meta.get("s", {}).get("last_error")
    assert last is None if waiting else last == "judge failed"


@pytest.mark.asyncio
async def test_initialize_orders_monitor_and_skips_remote(harness, monkeypatch):
    manager = harness
    events = []
    manager._monitor.on_level_change.side_effect = lambda cb: events.append("listen")
    manager._monitor.ensure_ready.side_effect = lambda: events.append("ready")
    manager._launcher.plan_server_launch.side_effect = lambda *a, **kw: (
        events.append("judge") or McpLaunch(_INFO)
    )
    connect = fake_connection(monkeypatch, manager)
    manager._book.record("removed", _INFO)
    await manager.initialize_all()
    await manager.initialize_all()
    assert events == ["listen", "ready", "judge", "ready", "judge"]
    assert manager._book.get("removed") is None
    assert connect.await_count == 2
    manager.registry.servers = {"r": ProtocolServerConfig(url="https://peer.invalid/mcp")}
    await manager.initialize_all()
    assert manager._monitor.ensure_ready.await_count == 2
    assert manager._launcher.plan_server_launch.await_count == 2
    await manager.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["tools", "call", "connect", "initialize"])
async def test_principal_stdio_refused_and_visible(harness, monkeypatch, entry):
    manager = harness
    cfg = ProtocolServerConfig(command="fake", auth_mode="oauth_user")
    manager.registry.servers["s"] = cfg
    manager._refresh_installations()
    transport = AsyncMock(return_value=(SimpleNamespace(), AsyncExitStack()))
    monkeypatch.setattr(mod, "connect_mcp_server", transport)
    manager._oauth_auth_service = SimpleNamespace(
        bearer_credential_guard=Mock(), has_usable_grant=AsyncMock(return_value=True)
    )
    try:
        if entry == "tools":
            assert await manager.get_all_tools(context()) == []
        elif entry == "initialize":
            await manager.initialize_all()
        else:
            with pytest.raises(Exception, match=_CODE):
                if entry == "call":
                    await manager.call_tool_on_server("s", "ping", {}, request_context=context())
                else:
                    await manager.connect_server("s", cfg, context())
        transport.assert_not_called()
        info = await manager.get_active_server_info()
        assert info[0]["last_error"] == _CODE
        assert info[0]["status"] == "error"
    finally:
        await manager.cleanup()


@pytest.mark.asyncio
async def test_isolated_launch_and_root_error_are_safe_before_retry(harness, monkeypatch, caplog):
    manager = harness
    marker = "odd.secret.value"
    launch = McpLaunch(_INFO, acquired=object(), exec_env={"TOKEN": marker})
    seen = []

    async def fail(_config, **kwargs):
        seen.append(kwargs["isolated"])
        raise ExceptionGroup("outer", [MCPError(code=-32000, message="denied " + marker)])

    monkeypatch.setattr(mod, "connect_mcp_server", fail)
    with pytest.raises(ConnectionError) as caught:
        await manager.connect_server("s", manager.registry.servers["s"], launch=launch)
    assert len(seen) == 3
    assert all(item.launch is launch and item.on_eof == "raise" for item in seen)
    assert all(item.report_failure == manager._monitor.report_failure for item in seen)
    assert marker not in caplog.text + str(caught.value)
    assert "denied" in str(caught.value) and "outer" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.asyncio
async def test_handshake_eof_keeps_root_cause(harness, monkeypatch):
    async def fail(*args, **kwargs):
        raise ExceptionGroup("outer", [McpServerExited("peer ended")])

    monkeypatch.setattr(mod, "connect_mcp_server", fail)
    with pytest.raises(ConnectionError, match="McpServerExited.*peer ended"):
        await harness.connect_server(
            "s",
            harness.registry.servers["s"],
            launch=McpLaunch(_INFO, acquired=object()),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [False, True])
@pytest.mark.parametrize("stale", ["generation", "removed", "closing"])
async def test_late_install_is_closed(harness, principal, stale):
    manager = harness
    if principal:
        manager.registry.servers["s"] = ProtocolServerConfig(
            url="https://peer.invalid/mcp", auth_mode="oauth_user"
        )
        manager._refresh_installations()
    key = manager.handle_key_for("s", context())
    if stale == "generation":
        manager._installations["s"].generation += 1
    elif stale == "removed":
        manager._installations.pop("s")
    else:
        manager._closing = True
    stack = SimpleNamespace(aclose=AsyncMock())
    assert not await manager._install_handle(key, LegacyMcpHandle(object(), stack))
    stack.aclose.assert_awaited_once()
    assert manager.handle_pool.get(key) is None


@pytest.mark.asyncio
async def test_waiting_catalog_and_busy_catalog_do_not_judge(harness, monkeypatch):
    manager = harness
    connect = fake_connection(monkeypatch, manager)
    manager._book.record("s", replace(_INFO, state="waiting_isolation"))
    assert await manager.get_all_tools(context()) == []
    manager._book.record("s", _INFO)
    async with manager._installation_lock("s"):
        assert await asyncio.wait_for(manager.get_all_tools(context()), 0.1) == []
    manager._launcher.plan_server_launch.assert_not_called()
    connect.assert_not_called()


@pytest.mark.asyncio
async def test_lock_identity_and_bounded_foreground_wait(harness, monkeypatch):
    manager = harness
    monkeypatch.setattr(mod, "MCP_HTTP_RESTART_LOCK_SECONDS", 0.01, raising=False)
    async with manager._installation_lock("s"):
        locks = manager._installation_locks
        lock = locks["s"]
        await manager.cleanup()
        assert manager._installation_locks is locks and locks["s"] is lock
        with pytest.raises(mod.McpServerBusyError):
            await manager.restart_server("s")
        with pytest.raises(McpOperationException, match="reconnecting"):
            await manager._ensure_handle("s", context())


@pytest.mark.asyncio
async def test_pending_unconditional_relaunch_survives_owner_replacement(harness, monkeypatch):
    manager = harness
    connect = fake_connection(monkeypatch, manager)
    owner = object()
    manager.handle_pool.put(manager.handle_key_for("s", None), LegacyMcpHandle(object(), owner))
    manager._schedule_relaunch("s", delay=60, expect_owner=owner)
    manager.handle_pool.put(
        manager.handle_key_for("s", None), LegacyMcpHandle(object(), AsyncExitStack())
    )
    manager._schedule_relaunch("s")
    await settle(manager)
    connect.assert_awaited_once()
    await manager.cleanup()


@pytest.mark.asyncio
async def test_epoch_and_owner_checks_after_lock(harness, monkeypatch):
    manager = harness
    connect = fake_connection(monkeypatch, manager)
    async with manager._installation_lock("s"):
        manager._schedule_relaunch("s")
        await asyncio.sleep(0)
        await manager.cleanup()
    await settle(manager)
    manager._schedule_relaunch("s", expect_owner=object())
    await settle(manager)
    connect.assert_not_called()


@pytest.mark.asyncio
async def test_background_wait_does_not_bump_generation(harness):
    manager = harness
    before = manager.get_installation_generation("s")
    manager._launcher.plan_server_launch.side_effect = McpLaunchWaiting("waiting_isolation", "none")
    manager._schedule_relaunch("s")
    await settle(manager)
    assert manager.get_installation_generation("s") == before


@pytest.mark.asyncio
async def test_lost_hook_reconnects_in_empty_context(harness, monkeypatch, caplog):
    manager = harness
    manager._book.record("s", _INFO)
    manager._launcher.plan_server_launch.return_value = McpLaunch(_INFO, acquired=object())
    monkeypatch.setattr(mod, "MCP_LOST_RELAUNCH_DELAY_SECONDS", 0.001, raising=False)
    transports = [_FakeTransport(), _FakeTransport()]
    marks = []

    async def connect(*args, **kwargs):
        marks.append(_MARK.get())
        return await transports[len(marks) - 1].connect()

    monkeypatch.setattr(mod, "connect_mcp_server", connect)
    hook = Mock(wraps=manager._connection_lost_hook)
    monkeypatch.setattr(manager, "_connection_lost_hook", hook)
    token = _MARK.set("creator")
    try:
        config = manager.registry.servers.pop("s")
        assert await manager.add_server("s", config)
        old = manager.handle_pool.get(manager.handle_key_for("s", None)).stack
        transports[0].fail()
        async with asyncio.timeout(2):
            while len(marks) < 2:
                await asyncio.sleep(0.001)
        await settle(manager)
        current = manager.handle_pool.get(manager.handle_key_for("s", None)).stack
        assert marks == ["creator", None]
        assert old is not current and current.alive and old.task.done()
        hook.assert_called_once()
        assert transports[0].closed == 1
        assert manager._installations["s"].restart_count == 0
        assert asyncio.current_task().cancelling() == 0
    finally:
        _MARK.reset(token)
        await manager.shutdown()
    assert "never retrieved" not in caplog.text


@pytest.mark.asyncio
async def test_hook_backoff_cap_reset_and_scope(harness, monkeypatch):
    manager = harness
    manager._book.record("s", _INFO)
    schedule = Mock()
    monkeypatch.setattr(manager, "_schedule_relaunch", schedule)
    key = manager.handle_key_for("s", None)
    owner = object()
    for _ in range(4):
        manager._connection_lost_hook(key, ConnectionError(), owner)
    assert [c.kwargs["delay"] for c in schedule.call_args_list] == [5, 10, 20]
    manager._installed_at["s"] = 0
    monkeypatch.setattr(mod, "get_monotonic_ms", lambda: 601000)
    manager._connection_lost_hook(key, ConnectionError(), owner)
    assert schedule.call_args.kwargs["delay"] == 5
    schedule.reset_mock()
    for info in [replace(_INFO, level="none"), None]:
        manager._book.retain([])
        if info:
            manager._book.record("s", info)
        manager._connection_lost_hook(key, ConnectionError(), owner)
    manager._book.record("s", _INFO)
    manager._connection_lost_hook(replace(key, principal_key="human"), ConnectionError(), owner)
    manager.registry.servers["s"] = ProtocolServerConfig(url="https://peer.invalid/mcp")
    manager._connection_lost_hook(key, ConnectionError(), owner)
    manager._closing = True
    manager._connection_lost_hook(key, ConnectionError(), owner)
    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_blocks_new_owners_and_cancels_relaunches(harness, monkeypatch):
    manager = harness
    transport = AsyncMock(return_value=(object(), AsyncExitStack()))
    monkeypatch.setattr(mod, "connect_mcp_server", transport)
    manager._schedule_relaunch("s", delay=60)
    await manager.shutdown()
    await manager.connect_server("s", manager.registry.servers["s"])
    transport.assert_not_called()
    assert not manager._relaunch_pending
    await manager.initialize_all()
    assert manager._closing
    transport.assert_not_called()
    manager._schedule_relaunch("s")
    assert not manager._relaunch_pending
    manager.reopen_for_test()
    await manager.initialize_all()
    transport.assert_awaited_once()
    await manager.shutdown()


@pytest.mark.asyncio
async def test_lifespan_closes_scheduling_before_background_cancellation(harness, monkeypatch):
    events = []

    def begin_shutdown():
        events.append("begin_shutdown")
        harness.begin_shutdown()

    async def cancel():
        assert harness._closing
        events.append("cancel")

    manager = SimpleNamespace(
        begin_shutdown=begin_shutdown,
        shutdown=AsyncMock(side_effect=lambda: events.append("shutdown")),
    )
    monkeypatch.setattr(main_mod, "protocol_manager", manager)
    monkeypatch.setattr(main_mod, "stop_recording", lambda: events.append("stop_recording"))
    monkeypatch.setattr(main_mod, "cancel_all_background_tasks", cancel)
    await main_mod._cleanup_proxy_runtime(
        SimpleNamespace(state=SimpleNamespace()),
        main_mod._LifespanResources(protocol_cleanup_required=True),
    )
    assert events == ["stop_recording", "begin_shutdown", "cancel", "shutdown"]


def test_principal_error_catalog_and_ui_translation_contract():
    root = Path(__file__).parents[2]
    errors = json.loads((root / "ToukaProxy/config/errors.json.example").read_text("utf-8"))
    entry = errors[_CODE]
    for language in ["zh-TW", "en"]:
        current = json.loads(
            (root / f"ToukaChat/src/locales/{language}/translation.json").read_text("utf-8")
        )
        for part in entry["i18n"].split("."):
            current = current[part]
        assert "auth_mode" in current and "stdio" in current


@pytest.mark.asyncio
async def test_all_connect_paths_share_installation_lock(harness, monkeypatch):
    manager = harness
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    calls = []

    async def connect(name, config, request_context=None, **kwargs):
        nonlocal active
        active += 1
        assert active == 1
        calls.append(name)
        entered.set()
        await release.wait()
        active -= 1

    monkeypatch.setattr(manager, "connect_server", connect)
    manager._schedule_relaunch("s")
    await asyncio.wait_for(entered.wait(), 1)
    tasks = [
        asyncio.create_task(manager.restart_server("s")),
        asyncio.create_task(manager.add_server("s", manager.registry.servers["s"])),
        asyncio.create_task(manager.initialize_all()),
    ]
    await asyncio.sleep(0.01)
    assert calls == ["s"]
    assert await asyncio.wait_for(manager.get_all_tools(context()), 0.1) == []
    release.set()
    await asyncio.gather(*tasks)
    await settle(manager)
    # 較新的熱掛載已接手，舊重載快照不再關閉它並重連。
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_listener_limits_concurrency_and_has_no_http_budget(harness, monkeypatch):
    manager = harness
    manager.registry.servers = {str(i): ProtocolServerConfig(command="fake") for i in range(6)}
    manager._refresh_installations()
    for name in manager.registry.servers:
        manager._book.record(name, replace(_INFO, state="waiting_isolation"))
    active = peak = 0
    release = asyncio.Event()
    entered = asyncio.Event()

    async def connect(name, config, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 4:
            entered.set()
        await release.wait()
        active -= 1

    monkeypatch.setattr(manager, "connect_server", connect)
    monkeypatch.setattr(mod, "MCP_HTTP_RESTART_SECONDS", 0.001)
    await manager._on_isolation_change(SimpleNamespace(executor=None))
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.sleep(0.02)
    assert active == peak == 4
    release.set()
    await settle(manager)
    assert len(manager._launcher.plan_server_launch.call_args_list) == 6
    assert all(
        call.kwargs["consent"] is None
        for call in manager._launcher.plan_server_launch.call_args_list
    )
    assert all(
        state.restart_count == 0 and state.generation == 2
        for state in manager._installations.values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["initialize", "add", "restart", "background"])
async def test_old_handle_closes_before_connect(harness, monkeypatch, path):
    manager = harness
    closed = []
    stack = AsyncExitStack()
    stack.callback(closed.append, "old")
    handle = LegacyMcpHandle(object(), stack)
    manager.handle_pool.put(manager.handle_key_for("s", None), handle)
    if path == "initialize":
        cleanup = manager.cleanup

        async def racing_lazy_connection():
            await cleanup()
            closed.clear()
            replacement = AsyncExitStack()
            replacement.callback(closed.append, "old")
            manager.handle_pool.put(
                manager.handle_key_for("s", None), LegacyMcpHandle(object(), replacement)
            )

        monkeypatch.setattr(manager, "cleanup", racing_lazy_connection)

    async def connect(*args, **kwargs):
        assert closed == ["old"]

    connect_mock = AsyncMock(side_effect=connect)
    monkeypatch.setattr(manager, "connect_server", connect_mock)
    if path == "initialize":
        await manager.initialize_all()
    elif path == "add":
        await manager.add_server("s", manager.registry.servers["s"])
    elif path == "restart":
        await manager.restart_server("s")
    else:
        manager._schedule_relaunch("s")
        await settle(manager)
    connect_mock.assert_awaited_once()
    assert closed == ["old"]


@pytest.mark.asyncio
async def test_request_during_active_relaunch_is_not_lost(harness, monkeypatch):
    manager = harness
    entered, release = asyncio.Event(), asyncio.Event()

    async def connect(*args, **kwargs):
        entered.set()
        await release.wait()

    connecting = AsyncMock(side_effect=connect)
    monkeypatch.setattr(manager, "connect_server", connecting)
    manager._schedule_relaunch("s")
    await asyncio.wait_for(entered.wait(), 1)
    manager._schedule_relaunch("s")
    release.set()
    await settle(manager)
    assert connecting.await_count == 2


@pytest.mark.asyncio
async def test_new_loss_during_reconnect_keeps_its_backoff(harness, monkeypatch):
    manager = harness
    entered, release = asyncio.Event(), asyncio.Event()
    replacement = AsyncExitStack()
    clock = {"ms": 10000}
    monkeypatch.setattr(mod, "get_monotonic_ms", lambda: clock["ms"])
    waiting = asyncio.Event()
    delays = []

    async def wait_for(awaitable, timeout):
        delays.append(timeout)
        waiting.set()
        return await awaitable

    scheduler = Mock(wraps=asyncio)
    scheduler.wait_for = wait_for
    monkeypatch.setattr(mod, "asyncio", scheduler)

    async def connect(*args, **kwargs):
        manager.handle_pool.put(
            manager.handle_key_for("s", None), LegacyMcpHandle(object(), replacement)
        )
        entered.set()
        await release.wait()

    connecting = AsyncMock(side_effect=connect)
    monkeypatch.setattr(manager, "connect_server", connecting)
    manager._schedule_relaunch("s")
    await asyncio.wait_for(entered.wait(), 1)
    manager._schedule_relaunch("s", delay=60, expect_owner=replacement)
    release.set()
    await asyncio.wait_for(waiting.wait(), 1)
    assert delays == [60]
    assert connecting.await_count == 1
    clock["ms"] += 60000
    manager._relaunch_pending["s"].wake.set()
    await settle(manager)
    assert connecting.await_count == 2
    await manager.cleanup()


@pytest.mark.asyncio
async def test_busy_restart_route_uses_existing_response(harness, monkeypatch):
    monkeypatch.setattr(mcp_router, "verify_origin", Mock())
    monkeypatch.setattr("src.main.protocol_manager", harness)
    monkeypatch.setattr(
        harness, "restart_server", AsyncMock(side_effect=mod.McpServerBusyError("busy"))
    )
    result = await mcp_router.restart_mcp_server(
        "s",
        SimpleNamespace(),
        identity=AgentIdentity.for_role("Probe", "Debugger"),
    )
    assert result["status"] == "busy" and result["server"]["name"] == "s"


@pytest.mark.asyncio
async def test_waiting_restart_closes_existing_and_manual_restart_clears_retry_budget(harness):
    manager = harness
    key = manager.handle_key_for("s", None)
    closer = SimpleNamespace(aclose=AsyncMock())
    manager.handle_pool.put(key, LegacyMcpHandle(object(), closer))
    manager._lost_relaunches["s"] = 3
    manager._launcher.plan_server_launch.side_effect = McpLaunchWaiting("waiting_isolation", "none")
    assert not await manager.restart_server("s")
    closer.aclose.assert_awaited_once()
    assert manager.handle_pool.get(key) is None
    assert manager.get_installation_generation("s") == key.config_generation + 1
    assert "s" not in manager._lost_relaunches


@pytest.mark.asyncio
async def test_lost_catalog_does_not_probe_or_log_again(harness, monkeypatch, caplog):
    transport = _FakeTransport()
    monkeypatch.setattr(mod, "connect_mcp_server", transport.connect)
    await harness.add_server("s", harness.registry.servers["s"])
    owner = harness.handle_pool.get(harness.handle_key_for("s", None)).stack
    transport.fail()
    await asyncio.wait_for(owner.task, 1)
    caplog.clear()
    assert await harness.get_all_tools(context()) == []
    assert caplog.text == ""
    await harness.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("wall_jump", [-10000, 10000])
async def test_relaunch_deadline_and_delay_ignore_wall_clock(harness, monkeypatch, wall_jump):
    clock = {"wall": 1000, "mono_ms": 10000}
    monkeypatch.setattr(mod, "get_current_epoch", lambda: clock["wall"])
    monkeypatch.setattr(mod, "get_monotonic_ms", lambda: clock["mono_ms"], raising=False)
    waiting, release = asyncio.Event(), asyncio.Event()
    delays = []

    async def wait_for(awaitable, timeout):
        delays.append(timeout)
        waiting.set()
        try:
            await release.wait()
        finally:
            awaitable.close()
        raise TimeoutError

    scheduler = Mock(wraps=asyncio)
    scheduler.wait_for = wait_for
    monkeypatch.setattr(mod, "asyncio", scheduler)
    reconnect = AsyncMock()
    monkeypatch.setattr(harness, "_reconnect", reconnect)
    try:
        harness._schedule_relaunch("s", delay=5)
        assert harness._relaunch_pending["s"].due == 15
        clock["wall"] += wall_jump
        clock["mono_ms"] += 1000
        await asyncio.wait_for(waiting.wait(), 1)
        assert delays == [4]
        reconnect.assert_not_called()
        release.set()
        await settle(harness)
        reconnect.assert_awaited_once()
    finally:
        release.set()
        await harness.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("wall_jump", [-10000, 10000])
async def test_stable_connection_budget_uses_monotonic_clock(harness, monkeypatch, wall_jump):
    clock = {"wall": 1000, "mono_ms": 10000}
    monkeypatch.setattr(mod, "get_current_epoch", lambda: clock["wall"])
    monkeypatch.setattr(mod, "get_monotonic_ms", lambda: clock["mono_ms"], raising=False)
    schedule = Mock()
    monkeypatch.setattr(harness, "_schedule_relaunch", schedule)
    harness._book.record("s", _INFO)
    key = harness.handle_key_for("s", None)
    owner = AsyncExitStack()
    try:
        assert await harness._install_handle(key, LegacyMcpHandle(object(), owner))
        assert harness._installed_at["s"] == 10
        harness._lost_relaunches["s"] = 3
        clock["wall"] += wall_jump
        clock["mono_ms"] += 599000
        harness._connection_lost_hook(key, ConnectionError(), owner)
        schedule.assert_not_called()
        clock["mono_ms"] += 1000
        harness._connection_lost_hook(key, ConnectionError(), owner)
        assert schedule.call_args.kwargs["delay"] == 5
    finally:
        await harness.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["connect", "schedule", "install"])
async def test_shutdown_latch_survives_cleared_closing_state(harness, monkeypatch, entry):
    harness.begin_shutdown()
    harness._closing = False
    transport = AsyncMock(return_value=(object(), AsyncExitStack()))
    monkeypatch.setattr(mod, "connect_mcp_server", transport)
    try:
        if entry == "connect":
            await harness.connect_server(
                "s", harness.registry.servers["s"], launch=McpLaunch(_INFO)
            )
        elif entry == "schedule":
            harness._schedule_relaunch("s", delay=60)
            assert not harness._relaunch_pending
        else:
            closer = SimpleNamespace(aclose=AsyncMock())
            assert not await harness._install_handle(
                harness.handle_key_for("s", None), LegacyMcpHandle(object(), closer)
            )
            closer.aclose.assert_awaited_once()
        transport.assert_not_called()
        assert not harness.handle_pool.keys_for_installation("s")
    finally:
        await harness.shutdown()


@pytest.mark.asyncio
async def test_principal_stdio_rejection_does_not_retry(harness, monkeypatch, caplog):
    cfg = ProtocolServerConfig(command="fake", auth_mode="oauth_user")
    check = Mock(wraps=harness._check_stdio_scope)
    monkeypatch.setattr(harness, "_check_stdio_scope", check)
    with pytest.raises(mod.ProtocolError, match=_CODE):
        await harness.connect_server("s", cfg, context())
    assert check.call_count == 1
    assert not [record for record in caplog.records if record.name == "src.core.retry"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs", [{}, {"judged_direct": True}, {"launch": SimpleNamespace(acquired=None)}]
)
async def test_unjudged_stdio_cannot_start(harness, monkeypatch, kwargs):
    transport = AsyncMock(return_value=(object(), AsyncExitStack()))
    monkeypatch.setattr(mod, "connect_mcp_server", transport)
    try:
        with pytest.raises(mod.ProtocolError, match="launch decision"):
            await harness.connect_server("s", harness.registry.servers["s"], **kwargs)
        transport.assert_not_called()
    finally:
        await harness.cleanup()


@pytest.mark.asyncio
async def test_judged_direct_passes_neither_launch_nor_isolated(harness, monkeypatch):
    transport = AsyncMock(return_value=(object(), AsyncExitStack()))
    monkeypatch.setattr(mod, "connect_mcp_server", transport)
    connect = AsyncMock(wraps=harness.connect_server)
    monkeypatch.setattr(harness, "connect_server", connect)
    harness._launcher.plan_server_launch.return_value = McpLaunch(_INFO, acquired=None)
    try:
        assert await harness.add_server("s", harness.registry.servers["s"])
        assert "launch" not in connect.call_args.kwargs
        assert "judged_direct" in connect.call_args.kwargs
        assert "isolated" not in transport.call_args.kwargs
    finally:
        await harness.cleanup()


@pytest.mark.asyncio
async def test_waiting_reconnect_closes_orphan_handle_without_installation(harness):
    key = harness.handle_key_for("s", None)
    closer = SimpleNamespace(aclose=AsyncMock())
    harness.handle_pool.put(key, LegacyMcpHandle(object(), closer))
    harness._installations.pop("s")
    harness._launcher.plan_server_launch.side_effect = McpLaunchWaiting("waiting_isolation", "none")
    assert not await harness.restart_server("s")
    closer.aclose.assert_awaited_once()
    assert harness.handle_pool.get(key) is None
    assert "s" not in harness._installations


@pytest.mark.asyncio
async def test_closing_connection_does_not_enter_retry(harness, monkeypatch):
    retry = AsyncMock()
    monkeypatch.setattr(harness, "_connect_server_with_retry", retry)
    harness.begin_shutdown()
    await harness.connect_server("s", harness.registry.servers["s"])
    retry.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_during_retry_backoff_prevents_another_owner(harness, monkeypatch):
    transport = AsyncMock(side_effect=ConnectionError("handshake failed"))
    monkeypatch.setattr(mod, "connect_mcp_server", transport)
    retry_asyncio = Mock(wraps=asyncio)
    retry_asyncio.sleep = AsyncMock(side_effect=lambda _: harness.begin_shutdown())
    monkeypatch.setattr(retry_mod, "asyncio", retry_asyncio)
    await harness.connect_server("s", harness.registry.servers["s"], launch=McpLaunch(_INFO))
    transport.assert_awaited_once()
    retry_asyncio.sleep.assert_awaited_once_with(1.0)
    assert not harness.handle_pool.keys_for_installation("s")
    await harness.shutdown()
