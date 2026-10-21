"""P8-B Slice 5：production owner 與 PaC adapter fail-closed 行為。"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from src.models.identity import AgentIdentity
from src.protocols.mcp.canonical import McpRequestContext


def _adapters() -> Any:
    return importlib.import_module("src.protocols.mcp.oauth.workload.adapters")


class _Auth:
    def __init__(self, users: list[Any]) -> None:
        self.users = users

    def get_all_users(self) -> list[Any]:
        return self.users


def _context() -> McpRequestContext:
    return McpRequestContext(
        request_id="request-1",
        identity=AgentIdentity.for_role("Alice", "Owner"),
        installation_id="calendar",
        auth_level="SafeAuto",
        execution_kind="heartbeat",
        origin="heartbeat",
    )


@pytest.mark.asyncio
async def test_owner_adapter_requires_exactly_one_active_explicit_owner() -> None:
    owner = SimpleNamespace(id="owner-1", is_active=True, owned_agents=["Alice"])
    adapter = _adapters().CurrentOwnerAuthority(auth_service=_Auth([owner]))
    assert await adapter.active_owner_for_agent("Alice") == "owner-1"

    duplicate = SimpleNamespace(id="owner-2", is_active=True, owned_agents=["Alice"])
    adapter = _adapters().CurrentOwnerAuthority(
        auth_service=_Auth([owner, duplicate])
    )
    assert await adapter.active_owner_for_agent("Alice") is None


@pytest.mark.asyncio
async def test_policy_adapter_requires_allow_and_digest_changes_with_policy() -> None:
    snapshot = {"version": 1}
    decision = SimpleNamespace(effect="ALLOW", reason="ok", metadata={})
    adapter = _adapters().CurrentWorkloadPolicyAuthority(
        identity_lookup=lambda _agent_id: AgentIdentity.for_role("Alice", "Owner"),
        mcp_tool_allowed=lambda _server, _tool, _identity: True,
        policy_evaluate=lambda _context: decision,
        policy_snapshot=lambda: snapshot,
    )

    first = await adapter.authorize(
        request_context=_context(),
        server_name="calendar",
        raw_tool_name="calendar.read",
        required_scopes=frozenset({"calendar.read"}),
    )
    snapshot["version"] = 2
    second = await adapter.authorize(
        request_context=_context(),
        server_name="calendar",
        raw_tool_name="calendar.read",
        required_scopes=frozenset({"calendar.read"}),
    )
    decision.effect = "CONFIRM"

    assert first != second
    with pytest.raises(_adapters().WorkloadCurrentPolicyDenied):
        await adapter.authorize(
            request_context=_context(),
            server_name="calendar",
            raw_tool_name="calendar.read",
            required_scopes=frozenset({"calendar.read"}),
        )
