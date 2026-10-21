"""P8-B workload OAuth must be a fail-closed, lifespan-owned Proxy service。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.config import settings
from src.main import app, protocol_manager, registry


class _Bundle:
    def __init__(self, events: list[str]) -> None:
        self.runtime = object()
        self.control = object()
        self.rotation = object()
        self._events = events

    async def run_maintenance(self) -> None:
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self._events.append("close")


def _enable_direct(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TOUKA_MCP_OAUTH_WORKLOAD_ENABLED", True)
    monkeypatch.setattr(settings, "TOUKA_MCP_OAUTH_DELEGATION_ENABLED", False)
    monkeypatch.setattr(
        settings,
        "TOUKA_MCP_OAUTH_WORKLOAD_FILESYSTEM_KIND",
        "local",
    )


def test_lifespan_wires_freezes_and_closes_workload_runtime(
    monkeypatch,
) -> None:
    _enable_direct(monkeypatch)
    events: list[str] = []
    bundle = _Bundle(events)

    async def build(**kwargs: Any) -> _Bundle:
        config = kwargs["config"]
        assert config.direct_enabled is True
        assert config.delegation_enabled is False
        events.append("build")
        return bundle

    def set_runtime(runtime: object | None) -> None:
        events.append("set" if runtime is bundle.runtime else "clear")

    async def initialize_all() -> None:
        events.append("initialize")

    monkeypatch.setattr(
        "src.main.build_mcp_workload_oauth_runtime_bundle",
        build,
    )
    monkeypatch.setattr(
        protocol_manager,
        "set_workload_oauth_service",
        set_runtime,
    )
    monkeypatch.setattr(protocol_manager, "initialize_all", initialize_all)
    monkeypatch.setattr(
        registry,
        "freeze_workload_oauth_config",
        lambda: events.append("freeze"),
    )

    with TestClient(app) as client:
        assert client.app.state.context["mcp_workload_oauth_runtime"] is bundle.runtime
        assert client.app.state.context["mcp_workload_oauth_control"] is bundle.control
        assert client.app.state.context["mcp_workload_oauth_rotation"] is bundle.rotation

    assert events.index("build") < events.index("set")
    assert events.index("set") < events.index("freeze") < events.index("initialize")
    assert events[-2:] == ["clear", "close"]


def test_enabled_workload_bootstrap_error_stops_proxy_startup(monkeypatch) -> None:
    _enable_direct(monkeypatch)

    async def fail(**_kwargs: Any) -> None:
        raise RuntimeError("workload-bootstrap-denied")

    monkeypatch.setattr(
        "src.main.build_mcp_workload_oauth_runtime_bundle",
        fail,
    )

    with pytest.raises(RuntimeError, match="workload-bootstrap-denied"), TestClient(app):
        pass
