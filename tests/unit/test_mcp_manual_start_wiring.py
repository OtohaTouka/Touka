"""隔離狀態、手動同意與背景重連的沙盒合約。"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from contextvars import ContextVar
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from src import main as main_module
from src.models.identity import AgentIdentity
from src.models.mcp_isolation import McpRestartRequest
from src.protocols import manager as manager_module
from src.protocols.manager import ProtocolManager
from src.protocols.mcp.handles import LegacyMcpHandle
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.routers import mcp as mcp_router
from src.services.identity_router import get_verified_identity
from src.services.isolation import mcp_launch as launch_module
from src.services.isolation.consent import Consent
from src.services.isolation.mcp_launch import (
    McpLaunch,
    McpLaunchBook,
    McpLaunchInfo,
    McpLaunchWaiting,
    manual_start_subject,
)
from src.services.isolation.status import FallbackPlan
from src.services.mcp_isolation_store import bound_of

from tests.support.isolation import reset_consent_ledger

WAITING = McpLaunchInfo("self", "creation", "ok", "waiting_isolation", "none")
PROBING = McpLaunchInfo("third_party", "manual", "ok", "waiting_probe", "none")
HUMAN = AgentIdentity.for_role("admin", "admin", is_human=True)
MARK = ContextVar("manual_request_context", default=None)


def test_waiting_clock_excludes_pending_and_counts_are_disjoint():
    now = [0.0]
    book = McpLaunchBook(clock=lambda: now[0])
    book.record("pending", replace(PROBING, reason="probe_pending"))
    book.record("slow", replace(PROBING, reason="probe_timeout"))
    book.record("self", WAITING)
    book.record("direct", replace(PROBING, state="launched"))
    now[0] = 659
    book.record("slow", replace(PROBING, reason="probe_timeout"))
    assert not book.suspended("slow")
    now[0] = 660
    assert book.suspended("slow")
    assert book.public("slow", connected=False)["state"] == "suspended"
    assert book.public("slow", connected=False)["reason"] == "probe_timeout"
    assert not book.suspended("pending") and not book.suspended("self")
    counts = book.counts()
    assert (counts.unisolated, counts.waiting, counts.suspended) == (1, 2, 1)
    book.record("slow", replace(PROBING, reason="probe_pending"))
    assert not book.suspended("slow")
    book.record("slow", replace(PROBING, reason="probe_timeout"))
    now[0] = 1319
    assert not book.suspended("slow")
    book.retain([])
    assert book.public("slow", connected=False) is None
    book.record("slow", replace(PROBING, reason="probe_timeout"))
    assert not book.suspended("slow")


@pytest.fixture
def manager(monkeypatch):
    registry = ProtocolRegistry("__no_manual_config__.json")
    registry.servers = {"s": ProtocolServerConfig(command="fake", args=["server.py"])}
    monitor = SimpleNamespace(
        fallback_for=Mock(return_value=FallbackPlan("landlock", frozenset({"fs", "tcp"}))),
        ensure_ready=AsyncMock(), on_level_change=Mock(), report_failure=Mock(),
    )
    book = McpLaunchBook()
    book.record("s", WAITING)
    launcher = SimpleNamespace(plan_server_launch=AsyncMock())
    mgr = ProtocolManager(registry, launch_book=book, monitor=monitor, launcher=launcher)
    monkeypatch.setattr(manager_module, "discover_ide_servers", AsyncMock(return_value={}))

    async def judge(name, config, reader, *, consent=None, **kwargs):
        if consent is None:
            book.record(name, WAITING)
            raise McpLaunchWaiting("waiting_isolation", None)
        info = replace(WAITING, state="launched", consented_direct=True)
        book.record(name, info)
        return McpLaunch(info)

    async def connect(name, config, **kwargs):
        key = mgr.handle_key_for(name, None)
        session = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])))
        await mgr._install_handle(key, LegacyMcpHandle(session, AsyncExitStack()))

    launcher.plan_server_launch.side_effect = judge
    monkeypatch.setattr(mgr, "connect_server", AsyncMock(side_effect=connect))
    return mgr


def consent_for(manager):
    entry = manager.registry.get_server("s")
    subject = manual_start_subject("s", manager.get_installation_generation("s"),
                                   bound_of(entry.model_dump()))
    return Consent(subject, "landlock", frozenset({"fs", "tcp"}))


async def test_manual_view_uses_the_consent_planner(manager, monkeypatch):
    planner = Mock(return_value=FallbackPlan("direct", frozenset()))
    monkeypatch.setattr(manager.manual_starter, "manual_start_plan", planner)
    [entry] = await manager.get_active_server_info()
    assert entry["isolation"]["manual_start"]["fallback"] == "direct"
    assert entry["isolation"]["manual_start"]["features"] == []
    planner.assert_called_once_with("s")


async def test_manual_consent_allows_only_one_handshake_attempt(manager, monkeypatch):
    monkeypatch.setattr(manager, "connect_server", ProtocolManager.connect_server.__get__(manager))
    transport = AsyncMock(side_effect=ConnectionError("handshake failed"))
    monkeypatch.setattr(manager_module, "connect_mcp_server", transport)
    assert not await manager.manual_starter.start_with_consent("s", consent_for(manager))
    assert transport.await_count == 1


async def test_reload_revokes_consent_before_cleanup_can_suspend(manager, monkeypatch):
    consent = consent_for(manager)
    entered, resume = asyncio.Event(), asyncio.Event()

    async def cleanup():
        entered.set()
        await resume.wait()

    monkeypatch.setattr(manager, "cleanup", cleanup)
    manager.registry.servers["s"] = manager.registry.servers["s"].model_copy(
        update={"env": {"TOKEN": "changed"}}
    )
    task = asyncio.create_task(manager.initialize_all())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert manager.manual_starter.consent_refusal("s", consent) is not None
    finally:
        resume.set()
        await task


@pytest.mark.parametrize("info", [WAITING, PROBING, replace(WAITING, state="launched")])
async def test_manual_view_only_for_waiting_self(manager, info):
    manager._book.record("s", info)
    [entry] = await manager.get_active_server_info()
    isolation = entry["isolation"]
    if info == WAITING:
        assert isolation["manual_start"] == {
            "fallback": "landlock", "features": ["fs", "tcp"],
            "generation": entry["generation"],
            "bound": bound_of(manager.registry.get_server("s").model_dump()),
        }
        assert "server.py" not in json.dumps(isolation)
    else:
        assert "manual_start" not in isolation


def test_list_values_drive_one_time_restart(manager, monkeypatch):
    monkeypatch.setattr(main_module, "protocol_manager", manager)
    monkeypatch.setattr(main_module.app.state, "context", {
        "protocol_manager": manager, "mcp_manual_starter": manager.manual_starter,
    }, raising=False)
    main_module.app.dependency_overrides[get_verified_identity] = lambda: HUMAN
    try:
        client = TestClient(main_module.app)
        entry = client.get("/v1/mcp/list").json()["servers"][0]
        plan = entry["isolation"]["manual_start"]
        body = {**plan, "isolation_ack": plan["fallback"]}
        body.pop("fallback")
        response = client.post("/v1/mcp/s/restart", json=body)
        assert response.json()["status"] == "restarted"
        assert client.post("/v1/mcp/s/restart").json()["status"] == "waiting"
        assert manager.connect_server.await_count == 1
        assert manager._launcher.plan_server_launch.call_args.kwargs["consent"] is None
    finally:
        main_module.app.dependency_overrides.pop(get_verified_identity, None)


@pytest.mark.parametrize("change", ["running", "generation", "features", "bound"])
async def test_manual_start_rechecks_after_acquiring_lock(manager, change, caplog):
    consent = consent_for(manager)
    entered = asyncio.Event()
    original_lock = manager._installation_lock

    def observed_lock(*args, **kwargs):
        entered.set()
        return original_lock(*args, **kwargs)

    manager._installation_lock = observed_lock
    async with original_lock("s", timeout=None):
        task = asyncio.create_task(manager.manual_starter.start_with_consent("s", consent))
        await asyncio.wait_for(entered.wait(), timeout=1)
        if change == "running":
            manager._book.record("s", replace(WAITING, state="launched", level="landlock"))
        elif change == "generation":
            manager._installations["s"].generation += 1
        elif change == "features":
            manager._monitor.fallback_for.return_value = FallbackPlan("landlock", frozenset({"fs"}))
        else:
            manager.registry.servers["s"] = ProtocolServerConfig(command="another")
    assert await task is False
    reason = {
        "running": "not_waiting_isolation", "features": "isolation_ack_mismatch",
        "generation": "entry_changed", "bound": "entry_changed",
    }[change]
    assert f"AUDIT: manual start of MCP s refused ({reason})" in caplog.text
    manager.connect_server.assert_not_awaited()
    manager._launcher.plan_server_launch.assert_not_awaited()


async def test_restart_reports_running_after_listener_wins_the_lock(manager, monkeypatch):
    monkeypatch.setattr(main_module, "protocol_manager", manager)
    monkeypatch.setattr(mcp_router, "verify_origin", Mock())
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(
        context={"mcp_manual_starter": manager.manual_starter},
    ))})
    body = McpRestartRequest(
        isolation_ack="landlock", features=["fs", "tcp"],
        generation=manager.get_installation_generation("s"),
        bound=bound_of(manager.registry.get_server("s").model_dump()),
    )
    entered = asyncio.Event()
    original_lock = manager._installation_lock

    def observed_lock(*args, **kwargs):
        entered.set()
        return original_lock(*args, **kwargs)

    manager._installation_lock = observed_lock
    async with original_lock("s", timeout=None):
        task = asyncio.create_task(mcp_router.restart_mcp_server("s", request, body, HUMAN))
        await asyncio.wait_for(entered.wait(), timeout=1)
        manager._book.record("s", replace(WAITING, state="launched", level="landlock"))
        await manager.connect_server("s", manager.registry.get_server("s"))
    result = await task
    assert result["server"]["isolation"]["state"] == "running"
    assert result["status"] == "restarted"
    assert manager.connect_server.await_count == 1


async def test_manual_start_reports_busy(manager, monkeypatch):
    monkeypatch.setattr(manager_module, "MCP_HTTP_RESTART_LOCK_SECONDS", 0.01)
    async with manager._installation_lock("s", timeout=None):
        with pytest.raises(manager_module.McpServerBusyError):
            await manager.manual_starter.start_with_consent("s", consent_for(manager))


async def test_rejected_installation_does_not_report_success(manager, monkeypatch):
    monkeypatch.setattr(manager, "_install_handle", AsyncMock(return_value=False))
    assert await manager.manual_starter.start_with_consent("s", consent_for(manager)) is False


async def test_monitor_replacement_registers_again(manager):
    await manager.initialize_all()
    old = manager._monitor
    await manager.initialize_all()
    old.on_level_change.assert_called_once()
    manager._monitor = SimpleNamespace(ensure_ready=AsyncMock(), on_level_change=Mock())
    await manager.initialize_all()
    manager._monitor.on_level_change.assert_called_once()


async def test_replaced_callback_on_same_monitor_registers_again(manager):
    await manager.initialize_all()
    manager._monitor.on_level_change = Mock()
    await manager.initialize_all()
    manager._monitor.on_level_change.assert_called_once()


def test_fixture_reset_clears_shared_launch_book():
    launch_module.mcp_launch_book.record("leftover", WAITING)
    reset_consent_ledger()
    assert launch_module.mcp_launch_book.get("leftover") is None


async def test_schedule_relaunch_has_empty_request_context(manager, monkeypatch):
    seen = []

    async def reconnect(*args, **kwargs):
        seen.append(MARK.get())

    monkeypatch.setattr(manager, "_reconnect", reconnect)
    token = MARK.set("http-human")
    try:
        manager.schedule_relaunch("s")
        manager.schedule_relaunch("absent")
    finally:
        MARK.reset(token)
    await asyncio.gather(*manager._relaunch_tasks)
    assert seen == [None]


def test_shared_book_starts_empty():
    assert launch_module.mcp_launch_book.get("leftover") is None
    launch_module.mcp_launch_book.record("leftover", WAITING)


def test_shared_book_does_not_keep_previous_test():
    assert launch_module.mcp_launch_book.get("leftover") is None


def test_lifespan_injects_the_manager_starter():
    with TestClient(main_module.app) as client:
        assert client.app.state.context[mcp_router.MCP_MANUAL_START_STARTER] is (
            main_module.protocol_manager.manual_starter
        )
