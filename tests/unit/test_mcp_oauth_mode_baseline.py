"""P8-A：在互動式 OAuth 接線前鎖定既有 MCP auth mode 行為。"""

from __future__ import annotations

from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Any

import pytest
from src.models.identity import AgentIdentity
from src.protocols.manager import ProtocolManager
from src.protocols.mcp.canonical import McpRequestContext
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig


def _registry(servers: dict[str, ProtocolServerConfig]) -> ProtocolRegistry:
    registry = ProtocolRegistry("__missing_p8a_registry__.json")
    registry.servers = servers
    return registry


def _context(installation_id: str, human: str = "human-p8a") -> McpRequestContext:
    return McpRequestContext(
        request_id="p8a-request",
        session_id="p8a-session",
        identity=AgentIdentity.for_role("P8AOwner", "Owner"),
        human_principal_id=human,
        installation_id=installation_id,
        auth_level="Manual",
        origin="foreground",
    )


@pytest.mark.asyncio
async def test_none_and_static_headers_keep_existing_shared_startup_path(monkeypatch):
    calls: list[ProtocolServerConfig] = []

    async def _connect(
        config: ProtocolServerConfig,
        **_kwargs: Any,
    ) -> tuple[Any, AsyncExitStack]:
        calls.append(config)
        return SimpleNamespace(protocol_version="2026-07-28"), AsyncExitStack()

    async def _discover_ide() -> dict[str, Any]:
        return {}

    monkeypatch.setattr("src.protocols.manager.connect_mcp_server", _connect)
    monkeypatch.setattr("src.protocols.manager.discover_ide_servers", _discover_ide)

    manager = ProtocolManager(
        _registry(
            {
                "anonymous": ProtocolServerConfig(command="anon"),
                "static": ProtocolServerConfig(
                    url="https://mcp.example.test/mcp",
                    auth_mode="static_headers",
                    headers={"Authorization": "Bearer ${MCP_PAT}"},
                    env={"MCP_PAT": "test-static-token"},
                ),
            }
        )
    )

    await manager.initialize_all()

    assert [config.auth_mode for config in calls] == ["none", "static_headers"]
    assert manager.handle_pool.get(manager.handle_key_for("anonymous", None)) is not None
    assert manager.handle_pool.get(manager.handle_key_for("static", None)) is not None


@pytest.mark.asyncio
async def test_oauth_user_remains_lazy_and_principal_isolated_at_startup(monkeypatch):
    calls: list[str] = []

    async def _connect(*_args: Any, **_kwargs: Any) -> tuple[Any, AsyncExitStack]:
        calls.append("connect")
        return SimpleNamespace(protocol_version="2026-07-28"), AsyncExitStack()

    async def _discover_ide() -> dict[str, Any]:
        return {}

    monkeypatch.setattr("src.protocols.manager.connect_mcp_server", _connect)
    monkeypatch.setattr("src.protocols.manager.discover_ide_servers", _discover_ide)
    manager = ProtocolManager(
        _registry(
            {
                "oauth": ProtocolServerConfig(
                    url="https://mcp.example.test/mcp",
                    auth_mode="oauth_user",
                    auth_profile_id="profile-a",
                )
            }
        )
    )

    await manager.initialize_all()

    assert calls == []
    assert manager.handle_key_for("oauth", _context("oauth", "human-a")) != (
        manager.handle_key_for("oauth", _context("oauth", "human-b"))
    )


@pytest.mark.asyncio
async def test_p8b_auth_modes_do_not_attempt_runtime_connection(monkeypatch):
    calls: list[str] = []

    async def _connect(
        config: ProtocolServerConfig,
        **_kwargs: Any,
    ) -> tuple[Any, AsyncExitStack]:
        calls.append(config.auth_mode)
        return SimpleNamespace(protocol_version="2026-07-28"), AsyncExitStack()

    async def _discover_ide() -> dict[str, Any]:
        return {}

    monkeypatch.setattr("src.protocols.manager.connect_mcp_server", _connect)
    monkeypatch.setattr("src.protocols.manager.discover_ide_servers", _discover_ide)
    manager = ProtocolManager(
        _registry(
            {
                "client-credentials": ProtocolServerConfig(
                    url="https://mcp.example.test/client",
                    auth_mode="oauth_client_credentials",
                    auth_profile_id="profile-client",
                    workload_authority_kind="direct",
                ),
                "enterprise": ProtocolServerConfig(
                    url="https://mcp.example.test/enterprise",
                    auth_mode="enterprise",
                    auth_profile_id="profile-enterprise",
                ),
            }
        )
    )

    await manager.initialize_all()

    assert calls == []
