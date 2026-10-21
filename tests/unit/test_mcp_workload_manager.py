"""P8-B Slice 4：ProtocolManager exact ticket 與 task-local bearer 接線。"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from pydantic import SecretStr
from src.models.identity import AgentIdentity
from src.protocols.manager import ProtocolError, ProtocolManager
from src.protocols.mcp.canonical import (
    McpInvocationAuthorization,
    McpInvocationBindingError,
    McpRequestContext,
    McpTransportBootstrapAuthorization,
    digest_mcp_arguments,
)
from src.protocols.mcp.oauth.service import OAuthBearerCredential
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig


def _registry(config: ProtocolServerConfig) -> ProtocolRegistry:
    registry = ProtocolRegistry("__missing_workload_manager_registry__.json")
    registry.servers = {"calendar": config}
    return registry


def _context() -> McpRequestContext:
    return McpRequestContext(
        request_id="request-1",
        identity=AgentIdentity.for_role("Alice", "Owner"),
        installation_id="calendar",
        auth_level="SafeAuto",
        execution_kind="heartbeat",
        origin="heartbeat",
    )


class _WorkloadRuntime:
    def __init__(self) -> None:
        self.authorizations: list[McpInvocationAuthorization] = []
        self.bearer_sends: list[dict[str, Any]] = []

    async def authorize_operation(self, **kwargs: Any) -> McpInvocationAuthorization:
        arguments = kwargs["arguments"]
        ticket = McpInvocationAuthorization(
            authorization_id=f"authz-{len(self.authorizations) + 1}",
            authority_kind="workload",
            workload_principal_id="heartbeat:Alice",
            owner_user_id="owner-1",
            execution_agent_id="Alice",
            origin="heartbeat",
            installation_id="calendar",
            installation_generation=kwargs["installation_generation"],
            auth_profile_id="calendar-workload",
            profile_generation=6,
            target_binding_digest=f"sha256:{'2' * 64}",
            credential_generation=2,
            operation_method="tools/call",
            raw_tool_name=kwargs["selector"],
            arguments_digest=digest_mcp_arguments(arguments),
            required_scopes=frozenset({"calendar.read"}),
            auth_level="SafeAuto",
            policy_digest=f"sha256:{'1' * 64}",
            request_id=kwargs["request_context"].request_id,
            issued_at=100.0,
            expires_at=110.0,
        )
        self.authorizations.append(ticket)
        return ticket

    async def authorize_transport_bootstrap(
        self,
        **kwargs: Any,
    ) -> McpTransportBootstrapAuthorization:
        ticket = kwargs["authorization"]
        return McpTransportBootstrapAuthorization(
            authorization_id=str(uuid4()),
            parent_authorization=ticket,
            issued_at=100.0,
            expires_at=105.0,
        )

    @asynccontextmanager
    async def workload_bearer_credential_guard(self, **kwargs: Any):
        ticket = kwargs["authorization"]
        self.bearer_sends.append(kwargs)
        yield OAuthBearerCredential(
            grant_id="workload-token",
            grant_version=ticket.credential_generation,
            authorization_header=SecretStr("Bearer workload-canary"),
        )


@pytest.mark.asyncio
async def test_manager_binds_current_exact_ticket_for_connect_and_every_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        protocol_mode="2026-07-28",
        auth_mode="oauth_client_credentials",
        auth_profile_id="calendar-workload",
        workload_authority_kind="direct",
    )
    runtime = _WorkloadRuntime()

    async def connect(_config: ProtocolServerConfig, **kwargs: Any):
        provider = kwargs["oauth_credential_provider"]
        initialize_request = httpx2.Request(
            "POST",
            "https://mcp.example.test/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        async with provider(wire_request=initialize_request):
            pass

        async def call_tool(tool_name: str, arguments: dict[str, Any]) -> str:
            tool_request = httpx2.Request(
                "POST",
                "https://mcp.example.test/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                },
            )
            async with provider(wire_request=tool_request):
                return "ok"

        return (
            SimpleNamespace(protocol_version="legacy", call_tool=call_tool),
            AsyncExitStack(),
        )

    async def catalog(*_args: Any, **_kwargs: Any) -> Any:
        # dep1：呼叫當下核對清單，被叫的工具必須在當次清單上。
        tool = SimpleNamespace(name="calendar.read", input_schema={"type": "object"})
        return SimpleNamespace(items=(tool,), issues=(), supported=True)

    monkeypatch.setattr("src.protocols.manager.connect_mcp_server", connect)
    monkeypatch.setattr("src.protocols.manager.adapt_call_tool_result", lambda value, **_: value)
    # ADR-0207 的設計路徑；ADR-0246 U2c-2 起產品預設關閉，這裡只為測這條路徑打開。
    monkeypatch.setattr("src.protocols.manager.HEARTBEAT_WORKLOAD_OAUTH_ENABLED", True)
    manager = ProtocolManager(_registry(config), workload_oauth_service=runtime)
    monkeypatch.setattr(manager, "_tool_catalog_from_session", catalog)

    first = await manager.call_tool_on_server(
        "calendar",
        "calendar.read",
        {"day": 1},
        request_context=_context(),
    )
    second = await manager.call_tool_on_server(
        "calendar",
        "calendar.read",
        {"day": 2},
        request_context=_context(),
    )

    assert first == second == "ok"
    assert len(manager.handle_pool.keys_for_installation("calendar")) == 1
    assert [send["wire_attempt"].egress_kind for send in runtime.bearer_sends] == [
        "transport_bootstrap",
        "mcp_bearer",
        "mcp_bearer",
    ]
    assert runtime.bearer_sends[0]["bootstrap_authorization"] is not None
    assert runtime.bearer_sends[1]["bootstrap_authorization"] is None
    assert runtime.bearer_sends[1]["transport_url"] == ("https://mcp.example.test/mcp")
    assert runtime.bearer_sends[1]["wire_attempt"].arguments_digest == (
        runtime.authorizations[0].arguments_digest
    )
    assert runtime.authorizations[0].arguments_digest != (
        runtime.authorizations[1].arguments_digest
    )
    with pytest.raises(ProtocolError, match="exact workload authorization"):
        manager.handle_key_for("calendar", _context())

    authority_key = runtime.authorizations[-1].handle_principal_key
    assert await manager.close_workload_authority_handles(authority_key) == 1
    assert manager.handle_pool.keys_for_installation("calendar") == []


@pytest.mark.asyncio
async def test_manager_rejects_mutated_actual_wire_before_bearer_resolution() -> None:
    config = ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        protocol_mode="2026-07-28",
        auth_mode="oauth_client_credentials",
        auth_profile_id="calendar-workload",
        workload_authority_kind="direct",
    )
    runtime = _WorkloadRuntime()
    manager = ProtocolManager(_registry(config), workload_oauth_service=runtime)
    authorization = await runtime.authorize_operation(
        installation_generation=manager.get_installation_generation("calendar"),
        selector="calendar.read",
        arguments={"day": 1},
        request_context=_context(),
    )
    mutated_request = httpx2.Request(
        "POST",
        "https://mcp.example.test/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "calendar.read", "arguments": {"day": 2}},
        },
    )

    with (
        manager._workload_authorization_scope(authorization),
        pytest.raises(McpInvocationBindingError),
    ):
        async with manager._workload_bearer_credential_guard(
            server_name="calendar",
            wire_request=mutated_request,
        ):
            pytest.fail("mutated wire request obtained a bearer credential")

    assert runtime.bearer_sends == []


@pytest.mark.asyncio
async def test_manager_rejects_wrong_actual_transport_before_runtime_or_network() -> None:
    config = ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        protocol_mode="2026-07-28",
        auth_mode="oauth_client_credentials",
        auth_profile_id="calendar-workload",
        workload_authority_kind="direct",
    )
    runtime = _WorkloadRuntime()
    manager = ProtocolManager(_registry(config), workload_oauth_service=runtime)
    authorization = await runtime.authorize_operation(
        installation_generation=manager.get_installation_generation("calendar"),
        selector="calendar.read",
        arguments={"day": 1},
        request_context=_context(),
    )
    wrong_target = httpx2.Request(
        "POST",
        "https://attacker.example.test/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "calendar.read", "arguments": {"day": 1}},
        },
    )

    with (
        manager._workload_authorization_scope(authorization),
        pytest.raises(
            ProtocolError,
            match="transport target",
        ),
    ):
        async with manager._workload_bearer_credential_guard(
            server_name="calendar",
            wire_request=wrong_target,
        ):
            pytest.fail("wrong transport reached workload runtime")

    assert runtime.bearer_sends == []


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["foreground", "dispatch"])
async def test_workload_oauth_non_heartbeat_origin_fails_before_network(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    config = ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        auth_mode="oauth_client_credentials",
        auth_profile_id="calendar-workload",
        workload_authority_kind="direct",
    )
    connected = False

    async def connect(*_args: Any, **_kwargs: Any) -> None:
        nonlocal connected
        connected = True

    monkeypatch.setattr("src.protocols.manager.connect_mcp_server", connect)
    manager = ProtocolManager(
        _registry(config),
        workload_oauth_service=_WorkloadRuntime(),
    )
    context = McpRequestContext.model_validate({**_context().model_dump(), "origin": origin})

    with pytest.raises(ProtocolError, match="heartbeat-only"):
        await manager.call_tool_on_server(
            "calendar",
            "calendar.read",
            {},
            request_context=context,
        )

    assert connected is False
