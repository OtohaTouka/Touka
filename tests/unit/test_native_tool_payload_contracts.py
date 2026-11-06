"""Complete model-facing native schemas, including the Memory injection path."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import guard_native_tool_contracts as guard
import pytest
from memory_server.tools import memory_tool_registry, memory_tools_schema
from src.adapters.openai_to_claude import convert_openai_to_anthropic
from src.adapters.openai_to_gemini import convert_openai_to_gemini
from src.adapters.schemas import OpenAIChatRequest
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext
from src.models.safety import AuthPoliciesConfig
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor
from src.tools.builtin import tool_registry
from src.tools.builtin.schemas import (
    CREATE_SKILL_SCHEMA,
    DISPATCH_SUBAGENT_GROUP_SCHEMA,
    EDIT_SOUL_SCHEMA,
    EVOLUTION_IMPLEMENT_AND_VERIFY_SCHEMA,
)
from src.tools.contracts import PUBLIC_TOOL_CONTRACTS, native_parameters
from src.tools.editor import IDE_OPEN_DIFF_SCHEMA
from src.tools.mission_tools import MISSION_TOOLS_SCHEMA
from src.utils.permission_grants import NameGrant


async def _execute_public_call(
    monkeypatch: pytest.MonkeyPatch, name: str, args: dict[str, Any]
) -> tuple[str, AsyncMock]:
    handler = AsyncMock(return_value='{"ok": true}')
    permission = MagicMock()
    permission.get_tool_action_param.return_value = "action"
    permission.evaluate_policy.return_value = SimpleNamespace(effect="ALLOW", reason=None)
    monkeypatch.setattr("src.services.tool_executor.permission_service", permission)
    monkeypatch.setattr(
        "src.services.tool_executor.soul_service.is_toukasoul", lambda _identity: False
    )
    monkeypatch.setattr(
        "src.services.tool_executor.session_service.get_session_by_id", AsyncMock(return_value=None)
    )
    executor = ToolExecutor(None, {name: handler}, {}, {}, enforce_advertised=False)
    result = await executor.execute_to_queue(
        name,
        args,
        "request-contract",
        {
            "identity": AgentIdentity.for_role(agent_id="DebugAgent", role="Owner"),
            "session_id": "trusted-session",
            "auth_level": "Auto",
        },
        asyncio.Queue(),
    )
    return result, handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"action": "save"},
        {"action": "save", "content": ["private-content-must-not-be-echoed"]},
        {"action": "query", "date_start": "not-a-date-private-value"},
        {"action": "query", "date_end": "2026-02-31"},
        {"action": "query", "unknown-private-key": "private-value"},
        {"action": "forget"},
        {"action": "manage_dups", "operation": "resolve"},
        {"action": "manage_dups", "operation": "supersede", "target_id": "a"},
        {"action": "manage_dups", "operation": "supersede", "content": "private-value"},
        {"action": "manage_dups", "operation": "scan", "content": "private-value"},
        {"action": "query", "include_history": "private-value"},
        {"action": ["private-action"]},
        {"action": {"private-action": "private-value"}},
    ],
)
async def test_invalid_public_memory_input_is_rejected_without_echo(
    monkeypatch: pytest.MonkeyPatch,
    args: dict[str, Any],
) -> None:
    result, handler = await _execute_public_call(monkeypatch, "memory_tools", args)
    handler.assert_not_called()
    payload = json.loads(result)
    assert payload["error_class"] == "validation_error"
    assert "private" not in result
    assert "Traceback" not in result and "input_value" not in result


@pytest.mark.asyncio
async def test_model_reserved_values_never_become_trusted_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, handler = await _execute_public_call(
        monkeypatch,
        "ide_open_diff",
        {
            "path": "example.py",
            "new_content": "pass",
            "session_id": "forged-session",
            "project_id": "forged-project",
            "identity": "forged-identity",
            "auth_level": "YOLO",
        },
    )
    assert json.loads(result)["ok"] is True
    actual = handler.await_args.kwargs
    assert actual["session_id"] == "trusted-session"
    assert actual["project_id"] is None
    assert actual["identity"].agent_id == "DebugAgent"
    assert actual["auth_level"] == "Auto"  # noqa: PAC_MANUAL_LEVEL_CHECK


@pytest.mark.asyncio
async def test_mission_legacy_artifact_fields_keep_independent_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, handler = await _execute_public_call(
        monkeypatch,
        "mission_tools",
        {
            "action": "update_status",
            "task_id": "one",
            "status": "DOING",
            "artifact_ref": "legacy.txt",
            "artifact_refs": ["modern.txt"],
        },
    )
    assert handler.await_args.kwargs["artifact_ref"] == "legacy.txt"
    assert handler.await_args.kwargs["artifact_refs"] == ["modern.txt"]


@pytest.mark.asyncio
async def test_memory_importance_alias_preserves_null_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, handler = await _execute_public_call(
        monkeypatch,
        "memory_tools",
        {
            "action": "query",
            "importance": None,
            "importance_min": "4",
        },
    )
    assert handler.await_args.kwargs["importance"] == "4"


@pytest.mark.parametrize("field", ["review_required", "review_risk", "test_log", "artifact_refs"])
def test_initial_mission_tasks_expose_review_and_evidence(field: str) -> None:
    fields = MISSION_TOOLS_SCHEMA["function"]["parameters"]["properties"]["tasks"]["items"][
        "properties"
    ]
    assert field in fields


@pytest.mark.parametrize("field", ["timeout_override", "max_storage_mb"])
def test_evolution_exposes_verification_quotas(field: str) -> None:
    assert field in EVOLUTION_IMPLEMENT_AND_VERIFY_SCHEMA["function"]["parameters"]["properties"]


def test_skill_author_is_public_metadata_only() -> None:
    fields = CREATE_SKILL_SCHEMA["function"]["parameters"]["properties"]
    assert "author" in fields
    assert {"is_system", "identity", "sdk_major"}.isdisjoint(fields)


def test_diff_session_is_runtime_owned() -> None:
    parameters = IDE_OPEN_DIFF_SCHEMA["function"]["parameters"]
    assert "session_id" not in parameters["properties"]
    assert "session_id" not in parameters.get("required", [])


def test_soul_schema_discloses_existing_constraints() -> None:
    text = str(EDIT_SOUL_SCHEMA)
    assert "0-based" in text
    assert "50" in text
    assert "soul.md" in text and "config.json" in text


def test_group_schema_discloses_batch_and_concurrency_caps() -> None:
    text = str(DISPATCH_SUBAGENT_GROUP_SCHEMA)
    assert "8" in text and "4" in text


def test_mission_notification_description_names_target_condition() -> None:
    text = MISSION_TOOLS_SCHEMA["function"]["parameters"]["properties"]["notify_on_complete"][
        "description"
    ]
    assert "DispatchAgent" in text


@pytest.mark.asyncio
async def test_complete_native_payload_excludes_legacy_memory_vault() -> None:
    request = SimpleNamespace(
        tools=None,
        project_id=None,
        ide_context=None,
        system=None,
        messages=[{"role": "user", "content": "hi"}],
    )
    ctx = PipelineContext(identity=AgentIdentity.for_role(agent_id="DebugAgent", role="Owner"))
    ctx["agent_id"] = "DebugAgent"
    ctx["memory_tools_schema"] = memory_tools_schema
    permission = MagicMock()
    permission.get_allowed_tools.return_value = NameGrant.from_lists(["*"])
    permission.is_tool_allowed.side_effect = lambda name, grant: grant.allows(name)
    permission.is_mcp_server_allowed.return_value = True
    permission.evaluate_policy.return_value = MagicMock(effect="ALLOW", reason=None)
    with patch("src.middleware.layers.tool.permission_service", permission):
        await ToolLayer().process(request, ctx)
    names = {tool["function"]["name"] for tool in request.tools if isinstance(tool, dict)}
    assert {"memory_tools", "read", "write"} <= names
    assert {"vault_tools", "project_tools", "update_my_soul", "sns_control"}.isdisjoint(names)
    selected = {
        tool["function"]["name"]: tool["function"]["parameters"]
        for tool in request.tools
        if tool["function"]["name"] in PUBLIC_TOOL_CONTRACTS
    }
    assert selected.keys() == PUBLIC_TOOL_CONTRACTS.keys()
    provider_request = OpenAIChatRequest(
        model="mock-model", messages=[{"role": "user", "content": "hi"}], tools=request.tools
    )
    anthropic = {
        tool["name"]: tool["input_schema"]
        for tool in convert_openai_to_anthropic(provider_request).tools
    }
    gemini = {
        tool["name"]: tool["parameters"]
        for group in convert_openai_to_gemini(provider_request)["tools"]
        for tool in group["functionDeclarations"]
    }
    for name, parameters in selected.items():
        for projected in (parameters, anthropic[name], gemini[name]):
            assert guard.schema_errors(native_parameters(name), projected) == [], name
    dropped = copy.deepcopy(gemini["memory_tools"])
    dropped["properties"].pop("emotions")
    assert guard.schema_errors(native_parameters("memory_tools"), dropped)


def test_actual_runtime_registries_and_duplicate_winners_match_source_classification() -> None:
    baseline = json.loads(guard.BASELINE_PATH.read_text(encoding="utf-8"))
    assert (
        guard.inventory_errors(set(tool_registry) | set(memory_tool_registry), baseline["tools"])
        == []
    )
    # The executor uses the builtin first, then the Memory registry. Check the
    # actual callable selected for duplicate names, not only the union of keys.
    assert tool_registry["memory_tools"].__module__ == "src.tools.builtin.memory"
    assert tool_registry["mission_tools"].__module__ == "src.tools.mission_tools"
    for relative, name, actual in (
        (guard.BUILTIN_REGISTRY, "tool_registry", tool_registry),
        (guard.MEMORY_REGISTRY, "memory_tool_registry", memory_tool_registry),
    ):
        source = (Path(__file__).resolve().parents[2] / relative).read_text(encoding="utf-8")
        assert set(actual) == guard.assigned_keys(source, name)
        assert all(callable(handler) for handler in actual.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "initialize", "goal": "x", "tasks": 42},
        {
            "action": "initialize",
            "goal": "x",
            "tasks": [{"task_id": "one", "description": "x", "artifact_ref": "../private-value"}],
        },
        {
            "action": "update_status",
            "task_id": "one",
            "status": "DOING",
            "artifact_ref": ["private-value"],
        },
        {
            "action": "update_status",
            "task_id": "one",
            "status": "DOING",
            "artifact_refs": ["../private-value"],
        },
        {
            "action": "initialize",
            "goal": "x",
            "tasks": [
                {"task_id": "one", "description": "x", "artifact_refs": ["../private-value"]}
            ],
        },
    ],
)
async def test_invalid_legacy_mission_values_are_safely_rejected(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]
) -> None:
    result, handler = await _execute_public_call(monkeypatch, "mission_tools", arguments)
    assert json.loads(result)["error_class"] == "validation_error"
    assert "private-value" not in result
    handler.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["Manual", "Auto", "YOLO"])
@pytest.mark.parametrize("action", ["forget", "manage_dups"])
async def test_native_write_risk_and_denials_preserve_existing_gates(
    monkeypatch: pytest.MonkeyPatch, level: str, action: str
) -> None:
    source = Path(__file__).resolve().parents[2] / "ToukaProxy/config/auth_policies.json.example"
    policy = AuthPoliciesConfig.model_validate_json(source.read_text(encoding="utf-8"))
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")
    context = PolicyContext(
        actor=identity,
        resource="memory_tools",
        action="tool_call",
        environment={"auth_level": level, "tool_action": action},
    )
    assert (
        policy.evaluate(context).effect
        == {"Manual": "CONFIRM", "Auto": "REVIEW", "YOLO": "ALLOW"}[level]
    )
    # Even YOLO must obey an explicit PaC denial (e.g. blacklist).
    if level == "YOLO":
        policy.policies.critical_blacklist = ["^memory_tools$"]
    gate = MagicMock()
    gate.get_tool_action_param.return_value = "action"
    gate.evaluate_policy.side_effect = policy.evaluate
    monkeypatch.setattr("src.services.tool_executor.permission_service", gate)
    backend = AsyncMock()
    executor = ToolExecutor(None, {"memory_tools": backend}, {}, {}, enforce_advertised=False)
    result = await executor.execute_to_queue(
        "memory_tools",
        {
            "action": action,
            "operation": "resolve",
            "target_id": "fact-one",
            "fact_id_to_archive": "fact-one",
        },
        "denied",
        {"identity": identity, "auth_level": level},
        asyncio.Queue(),
    )
    backend.assert_not_called()
    assert json.loads(result)["error_class"] == "permission_denied"


def test_subagent_cannot_call_memory_despite_knowing_schema() -> None:
    sub = AgentIdentity.for_role(agent_id="SubagentProbe", role="Subagent")
    assert not permission_service.is_authorized(sub, "memory_tools", {}, {})
