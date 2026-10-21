"""P8-A ProtocolManager lazy OAuth provider and composite principal isolation。"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from src.models.identity import AgentIdentity
from src.protocols.manager import ProtocolManager
from src.protocols.mcp.canonical import McpRequestContext
from src.protocols.mcp.oauth.service import OAuthBearerCredential
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig


def _registry(config: ProtocolServerConfig) -> ProtocolRegistry:
    registry = ProtocolRegistry("__missing_oauth_manager_registry__.json")
    registry.servers = {"oauth": config}
    return registry


def _context(agent_id: str) -> McpRequestContext:
    return McpRequestContext(
        request_id=f"request-{agent_id}",
        identity=AgentIdentity.for_role(agent_id, "Owner"),
        human_principal_id="same-human",
        installation_id="oauth",
        auth_level="Manual",
        origin="foreground",
    )


class _OAuthRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.deny_operation = False

    @asynccontextmanager
    async def bearer_credential_guard(self, **kwargs):
        self.calls.append(kwargs)
        yield OAuthBearerCredential(
            grant_id="grant-a",
            grant_version=3,
            authorization_header=SecretStr("Bearer access-canary"),
        )

    async def authorize_operation(self, **kwargs):
        self.calls.append({"operation": kwargs})
        if self.deny_operation:
            raise PermissionError("scope-denied")


@pytest.mark.asyncio
async def test_manager_supplies_request_time_oauth_provider_without_mutating_config(
    monkeypatch,
):
    config = ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        protocol_mode="2026-07-28",
        auth_mode="oauth_user",
        auth_profile_id="profile-a",
    )
    runtime = _OAuthRuntime()
    captured: dict[str, Any] = {}

    async def connect(_config, **kwargs):
        captured.update(kwargs)
        async with kwargs["oauth_credential_provider"]() as credential:
            assert credential.authorization_header.get_secret_value() == ("Bearer access-canary")
        return SimpleNamespace(protocol_version="2026-07-28"), AsyncExitStack()

    monkeypatch.setattr("src.protocols.manager.connect_mcp_server", connect)
    manager = ProtocolManager(_registry(config), oauth_auth_service=runtime)
    context = _context("soul-a")

    await manager.connect_server("oauth", config, context)

    assert config.headers is None
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["request_context"] is context
    assert runtime.calls[0]["installation_generation"] == 1
    assert callable(captured["oauth_credential_provider"])


def test_same_human_different_souls_never_share_oauth_handle_key():
    config = ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        auth_mode="oauth_user",
        auth_profile_id="profile-a",
    )
    manager = ProtocolManager(_registry(config), oauth_auth_service=_OAuthRuntime())

    assert manager.handle_key_for("oauth", _context("soul-a")) != (
        manager.handle_key_for("oauth", _context("soul-b"))
    )


@pytest.mark.asyncio
async def test_scope_denial_happens_before_handle_or_network_creation(monkeypatch):
    config = ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        auth_mode="oauth_user",
        auth_profile_id="profile-a",
    )
    runtime = _OAuthRuntime()
    runtime.deny_operation = True
    connected = False

    async def connect(*_args, **_kwargs):
        nonlocal connected
        connected = True

    monkeypatch.setattr("src.protocols.manager.connect_mcp_server", connect)
    manager = ProtocolManager(_registry(config), oauth_auth_service=runtime)

    with pytest.raises(PermissionError, match="scope-denied"):
        await manager.call_tool_on_server(
            "oauth",
            "search",
            {},
            request_context=_context("soul-a"),
        )

    assert connected is False
    operation = runtime.calls[-1]["operation"]
    assert operation["operation"] == "tools/call"
    assert operation["selector"] == "search"
