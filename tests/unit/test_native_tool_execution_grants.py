"""Native execution must apply existing role grants before PaC or HITL."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.models.identity import AgentIdentity
from src.models.safety import AuthPoliciesConfig
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor
from src.utils.permission_grants import NameGrant


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["Subagent", "Base", None])
@pytest.mark.parametrize("level", ["Manual", "Auto", "YOLO"])
async def test_ungranted_native_never_reaches_handler_even_when_pac_allows(
    monkeypatch: pytest.MonkeyPatch,
    role: str | None,
    level: str,
) -> None:
    gate = MagicMock()
    gate.resolve_grants.side_effect = permission_service.resolve_grants
    gate.get_tool_action_param.return_value = "action"
    gate.evaluate_policy.return_value = SimpleNamespace(effect="ALLOW", reason=None)
    monkeypatch.setattr("src.services.tool_executor.permission_service", gate)
    monkeypatch.setattr("src.services.tool_executor.soul_service.is_toukasoul", lambda _: False)
    handler = AsyncMock(return_value="unexpected backend call")
    executor = ToolExecutor(None, {"memory_tools": handler}, {}, {}, enforce_advertised=False)
    result = await executor.execute_to_queue(
        "memory_tools",
        {"action": "query"},
        "grant-probe",
        {
            "identity": AgentIdentity.for_role(agent_id="GrantProbe", role=role),
            "auth_level": level,
        },
        asyncio.Queue(),
    )
    handler.assert_not_called()
    assert "permission_denied" in result
    gate.evaluate_policy.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "allow", "deny", "expected"),
    [
        ("memory_tools", ["*"], [], True),
        ("memory_tools", ["*"], ["memory_tools"], False),
        ("memory_tools", ["*"], ["*"], False),
        ("vault_tools", ["read"], [], False),
        ("vault_tools", ["vault_tools"], [], True),
    ],
)
async def test_native_owner_grants_aliases_and_duplicate_precedence(
    monkeypatch: pytest.MonkeyPatch, name: str, allow: list[str], deny: list[str], expected: bool
) -> None:
    gate = MagicMock()
    gate.resolve_grants.return_value = SimpleNamespace(tools=NameGrant.from_lists(allow, deny))
    gate.get_tool_action_param.return_value = "action"
    gate.evaluate_policy.return_value = SimpleNamespace(effect="ALLOW", reason=None)
    monkeypatch.setattr("src.services.tool_executor.permission_service", gate)
    monkeypatch.setattr("src.services.tool_executor.soul_service.is_toukasoul", lambda _: False)
    builtin, memory = (
        AsyncMock(return_value="builtin result"),
        AsyncMock(return_value="wrong duplicate"),
    )
    protocol = MagicMock()
    executor = ToolExecutor(protocol, {name: builtin}, {name: memory}, {}, enforce_advertised=False)
    args = (
        {"action": "query"} if name == "memory_tools" else {"action": "list", "vault": "personal"}
    )
    result = await executor.execute_to_queue(
        name,
        args,
        "grants",
        {
            "identity": AgentIdentity.for_role(agent_id="GrantProbe", role="Owner"),
            "auth_level": "YOLO",
        },
        asyncio.Queue(),
    )
    assert builtin.await_count == int(expected)
    assert result.startswith("builtin result") is expected
    memory.assert_not_called()
    protocol.resolve_tool.assert_not_called()
    gate.resolve_grants.assert_called_once_with("Owner")


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["mcp", "plugin"])
async def test_non_native_keeps_existing_authorization_path(
    monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    gate = MagicMock()
    gate.get_tool_action_param.return_value = None
    gate.evaluate_policy.return_value = SimpleNamespace(effect="DENY", reason="existing policy")
    gate.is_mcp_tool_allowed.return_value = False
    monkeypatch.setattr("src.services.tool_executor.permission_service", gate)
    protocol = MagicMock()
    protocol.resolve_tool.return_value = ("server", "remote")
    handler = AsyncMock()
    executor = ToolExecutor(
        protocol,
        {},
        {},
        {"remote": handler} if branch == "plugin" else {},
        enforce_advertised=False,
    )
    result = await executor.execute_to_queue(
        "remote",
        {},
        "non-native",
        {"identity": AgentIdentity.for_role(agent_id="GrantProbe", role="Owner")},
        asyncio.Queue(),
    )
    assert "permission_denied" in result
    gate.resolve_grants.assert_not_called()
    handler.assert_not_called()
    if branch == "mcp":
        gate.is_mcp_tool_allowed.assert_called_once()
    else:
        gate.is_mcp_tool_allowed.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [["private-value"], {"private-value": "x"}])
async def test_invalid_action_stays_safe_with_real_pac_model(
    monkeypatch: pytest.MonkeyPatch, action: object
) -> None:
    policy = AuthPoliciesConfig.model_validate_json(
        (
            Path(__file__).resolve().parents[2] / "ToukaProxy/config/auth_policies.json.example"
        ).read_text(encoding="utf-8")
    )
    gate = MagicMock()
    gate.resolve_grants.return_value = SimpleNamespace(tools=NameGrant.from_lists(["*"]))
    gate.get_tool_action_param.side_effect = policy.tool_risk.action_param_for
    gate.evaluate_policy.side_effect = policy.evaluate
    monkeypatch.setattr("src.services.tool_executor.permission_service", gate)
    handler = AsyncMock()
    executor = ToolExecutor(None, {"memory_tools": handler}, {}, {}, enforce_advertised=False)
    result = await executor.execute_to_queue(
        "memory_tools",
        {"action": action},
        "invalid-action",
        {
            "identity": AgentIdentity.for_role(agent_id="GrantProbe", role="Owner"),
            "auth_level": "YOLO",
        },
        asyncio.Queue(),
    )
    assert "validation_error" in result and "private-value" not in result
    handler.assert_not_called()
