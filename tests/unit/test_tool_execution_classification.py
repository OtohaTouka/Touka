"""F01: only a trusted registered handler may use an approval exception."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.models.identity import AgentIdentity
from src.models.safety import AuthPoliciesConfig
from src.services import tool_executor as executor_module
from src.services.approval_service import approval_service
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor
from src.tools import project_tools as project_module
from src.tools import terminal as terminal_module
from src.tools.builtin import evolution as evolution_module
from src.tools.builtin import system as system_module
from src.tools.builtin import tool_registry

ROOT = Path(__file__).resolve().parents[2]
IDENTITY = AgentIdentity.for_role(agent_id="ClassificationProbe", role="Owner")


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch) -> AuthPoliciesConfig:
    """Use the actual policy evaluator and role grants, with a stricter write rule."""
    model = AuthPoliciesConfig.model_validate_json(
        (ROOT / "ToukaProxy/config/auth_policies.json.example").read_text(encoding="utf-8")
    )
    model.tool_risk.tools["write"] = "dangerous"
    model.tool_risk.tools["project_tools"].actions["write"] = "dangerous"
    monkeypatch.setattr(permission_service, "policies", model)
    monkeypatch.setattr(permission_service, "_auto_reload_check", lambda: None)
    monkeypatch.setattr(executor_module.soul_service, "is_toukasoul", lambda _: False)
    approval_service.reset_for_test()
    yield model
    approval_service.reset_for_test()


@pytest.fixture
def write_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, AsyncMock]:
    """Real verb/project/core file writes, with only the resource root sandboxed."""
    target = tmp_path / "note.txt"
    target.write_bytes(b"original")
    monkeypatch.setattr(
        project_module.resource_manager, "get_project_path", AsyncMock(return_value=str(tmp_path))
    )
    monkeypatch.setattr(project_module, "add_project_event", AsyncMock())
    writer = AsyncMock(wraps=project_module.core_write)
    monkeypatch.setattr(project_module, "core_write", writer)
    return target, writer


def _args(name: str, extra_command: bool) -> dict[str, Any]:
    args = {"path": "note.txt", "content": "changed"}
    args.update({"scope": "project"} if name == "write" else {"action": "write"})
    if extra_command:
        args["command"] = "echo harmless"
    return args


def _ctx(level: str, interactive: bool = False) -> dict[str, Any]:
    return {
        "identity": IDENTITY,
        "auth_level": level,
        "project_id": "sandbox-project",
        "resumable_stream": interactive,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["write", "project_tools"])
@pytest.mark.parametrize("level", ["Manual", "SafeAuto"])
@pytest.mark.parametrize("extra_command", [False, True])
async def test_nonterminal_command_cannot_change_policy_or_headless_approval(
    policy: AuthPoliciesConfig,
    write_probe: tuple[Path, AsyncMock],
    name: str,
    level: str,
    extra_command: bool,
) -> None:
    target, writer = write_probe
    executor = ToolExecutor(None, tool_registry, {}, {}, enforce_advertised=False)
    queue: asyncio.Queue = asyncio.Queue()
    result = await executor.execute_to_queue(
        name, _args(name, extra_command), "classification", _ctx(level), queue
    )
    assert target.read_bytes() == b"original"
    writer.assert_not_called()
    assert json.loads(result)["reason"] == "approval_required"
    assert all(item is None or item.get("type") != "tool_start" for item in queue._queue)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["write", "project_tools"])
@pytest.mark.parametrize("extra_command", [False, True])
@pytest.mark.parametrize("approved", [False, True])
async def test_real_write_waits_for_one_human_decision(
    policy: AuthPoliciesConfig,
    write_probe: tuple[Path, AsyncMock],
    name: str,
    extra_command: bool,
    approved: bool,
) -> None:
    target, writer = write_probe
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        ToolExecutor(None, tool_registry, {}, {}, enforce_advertised=False).execute_to_queue(
            name, _args(name, extra_command), "classification", _ctx("Manual", True), queue
        )
    )
    try:
        while True:
            event = await asyncio.wait_for(queue.get(), 1)
            assert event is not None, "call ended before approval"
            if event.get("type") == "tool_approval_request":
                break
        assert target.read_bytes() == b"original"
        writer.assert_not_called()
        pending = next(iter(approval_service._pending))
        await approval_service.resolve(pending, approved=approved, resolver="synthetic-owner")
        result = await task
        assert writer.await_count == int(approved)
        assert target.read_bytes() == (b"changed" if approved else b"original")
        if not approved:
            assert json.loads(result)["reason"] == "approval_rejected"
        await approval_service.resolve(pending, approved=True, resolver="synthetic-owner")
        assert writer.await_count == int(approved)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "execute_command",
        "bash",
        "request_skill_install",
        "request_mcp_creation",
    ],
)
async def test_privileged_name_with_unknown_handler_fails_closed(
    policy: AuthPoliciesConfig, name: str
) -> None:
    handler = AsyncMock(return_value="unexpected execution")
    result = await ToolExecutor(
        None, {name: handler}, {}, {}, enforce_advertised=False
    ).execute_to_queue(
        name,
        {"command": "echo harmless", "skill_id": "synthetic"},
        "classification-drift",
        _ctx("Manual"),
        asyncio.Queue(),
    )
    handler.assert_not_called()
    assert json.loads(result)["reason"] == "execution_classification_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["execute_command", "bash"])
@pytest.mark.usefixtures("zone_data_root")  # 個人空間在 tmp_path（ADR-0293）
async def test_real_terminal_alias_keeps_its_internal_approval(
    monkeypatch: pytest.MonkeyPatch, policy: AuthPoliciesConfig, name: str
) -> None:
    core = AsyncMock(return_value=(0, "synthetic output", 1))
    monkeypatch.setattr(terminal_module, "_execute_core", core)
    result = await ToolExecutor(
        None, tool_registry, {}, {}, enforce_advertised=False
    ).execute_to_queue(
        name, {"command": "echo harmless"}, "true-terminal", _ctx("Manual"), asyncio.Queue()
    )
    core.assert_not_called()
    assert json.loads(result)["reason"] == "ui_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["plugin", "mcp"])
async def test_external_command_parameter_has_no_terminal_exception(
    monkeypatch: pytest.MonkeyPatch, policy: AuthPoliciesConfig, branch: str
) -> None:
    handler = AsyncMock(return_value="unexpected external effect")
    protocol = MagicMock()
    protocol.resolve_tool.return_value = ("synthetic-server", "remote-write")
    protocol.registry.get_server.return_value = None  # 不是 vault-jailed（ADR-0293 提早拒絕）
    monkeypatch.setattr(permission_service, "is_mcp_tool_allowed", lambda *_: True)
    executor = ToolExecutor(
        protocol if branch == "mcp" else None,
        {},
        {},
        {"remote-write": handler} if branch == "plugin" else {},
        enforce_advertised=False,
    )
    result = await executor.execute_to_queue(
        "remote-write", {"command": "echo harmless"}, "external", _ctx("SafeAuto"), asyncio.Queue()
    )
    handler.assert_not_called()
    protocol.execute_tool.assert_not_called()
    assert json.loads(result)["reason"] == "approval_required"


@pytest.mark.asyncio
async def test_approved_invocation_keeps_the_resolved_handler(
    policy: AuthPoliciesConfig, write_probe: tuple[Path, AsyncMock]
) -> None:
    target, writer = write_probe
    registry = dict(tool_registry)
    replacement = AsyncMock(return_value="unapproved replacement")
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        ToolExecutor(None, registry, {}, {}, enforce_advertised=False).execute_to_queue(
            "write", _args("write", False), "stable-handler", _ctx("Manual", True), queue
        )
    )
    try:
        while True:
            event = await asyncio.wait_for(queue.get(), 1)
            assert event is not None
            if event.get("type") == "tool_approval_request":
                break
        registry["write"] = replacement
        pending = next(iter(approval_service._pending))
        await approval_service.resolve(pending, approved=True, resolver="synthetic-owner")
        await task
        replacement.assert_not_called()
        writer.assert_awaited_once()
        assert target.read_bytes() == b"changed"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["execute_command", "bash"])
@pytest.mark.usefixtures("zone_data_root")  # 個人空間在 tmp_path（ADR-0293）
async def test_true_terminal_broker_approval_executes_once(
    monkeypatch: pytest.MonkeyPatch, policy: AuthPoliciesConfig, name: str
) -> None:
    core = AsyncMock(return_value=(0, "synthetic output", 1))
    monkeypatch.setattr(terminal_module, "_execute_core", core)
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        ToolExecutor(None, tool_registry, {}, {}, enforce_advertised=False).execute_to_queue(
            name, {"command": "echo harmless"}, "terminal-approve", _ctx("Manual", True), queue
        )
    )
    try:
        while True:
            event = await asyncio.wait_for(queue.get(), 1)
            assert event is not None
            if event.get("type") == "tool_approval_request":
                break
        core.assert_not_called()
        pending = next(iter(approval_service._pending))
        await approval_service.resolve(pending, approved=True, resolver="synthetic-owner")
        result = await task
        assert "EXIT_CODE: 0" in result
        core.assert_awaited_once()
        await approval_service.resolve(pending, approved=True, resolver="synthetic-owner")
        core.assert_awaited_once()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["execute_command", "bash"])
@pytest.mark.parametrize("reason", ["grant", "blacklist"])
async def test_terminal_denial_precedes_approval_and_execution(
    monkeypatch: pytest.MonkeyPatch, policy: AuthPoliciesConfig, name: str, reason: str
) -> None:
    core = AsyncMock()
    monkeypatch.setattr(terminal_module, "_execute_core", core)
    policy.policies.critical_blacklist.append("^echo blocked-fixture$")
    context = _ctx("FullYOLO", True)
    if reason == "grant":
        context["identity"] = AgentIdentity.for_role(agent_id="SubagentProbe", role="Subagent")
    result = await ToolExecutor(
        None, tool_registry, {}, {}, enforce_advertised=False
    ).execute_to_queue(
        name, {"command": "echo blocked-fixture"}, "terminal-deny", context, asyncio.Queue()
    )
    core.assert_not_called()
    assert json.loads(result)["error_class"] == "permission_denied"
    if reason == "grant":
        assert json.loads(result)["reason"] == "native_tool_rbac"
    else:
        assert "blacklist" in json.loads(result)["reason"]
    assert approval_service.pending_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["request_skill_install", "request_mcp_creation", "evolution_implement_and_verify"]
)
@pytest.mark.parametrize("interactive", [False, True])
async def test_actual_self_approving_handler_retains_its_own_human_gate(
    monkeypatch: pytest.MonkeyPatch,
    policy: AuthPoliciesConfig,
    name: str,
    interactive: bool,
) -> None:
    own_approval = AsyncMock(return_value=(False, {}))
    config = MagicMock()
    config.config_exists.return_value = True
    config.read_config_json.return_value = {
        "skills": {
            "synthetic": {
                "name": "Synthetic",
                "description": "fixture",
                "mcp_config": {"command": "synthetic-never-executed", "args": []},
            }
        }
    }
    monkeypatch.setattr(system_module, "config_utils", config)
    monkeypatch.setattr(system_module, "request_approval", own_approval)
    monkeypatch.setattr(evolution_module, "config_utils", config)
    monkeypatch.setattr(evolution_module, "request_approval", own_approval)
    monkeypatch.setattr(evolution_module.EvolutionPathGuard, "tool_file_exists", lambda *_: True)
    monkeypatch.setattr(evolution_module.EvolutionPathGuard, "read_tool_file", lambda *_: "{}")
    monkeypatch.setattr(evolution_module.resource_manager, "read_file_binary", lambda *_: b"{}")
    build = AsyncMock(return_value="Success")
    monkeypatch.setattr(evolution_module, "write_mcp_implementation", build)
    monkeypatch.setattr(
        evolution_module,
        "_verify_mcp",
        AsyncMock(return_value=evolution_module.VerifyOutcome(True, "verified")),
    )
    args: dict[str, Any] = {"skill_id": "synthetic"}
    # iso-c1 缺口 1：演化建置不再自帶審批——有人在場先跳一般審批卡（核准後才建置、部署卡照舊），
    # 無人在場由 executor 拒絕（approval_required），建置與部署卡都不跑。
    ordinary = name == "evolution_implement_and_verify"
    if ordinary:
        args.update(code="pass", test_tool_name="probe", test_arguments={}, submit_for_deploy=True)
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        ToolExecutor(None, tool_registry, {}, {}, enforce_advertised=False).execute_to_queue(
            name, args, "self-approval", _ctx("Manual", interactive), queue
        )
    )
    try:
        if ordinary and interactive:
            while True:
                event = await asyncio.wait_for(queue.get(), 1)
                assert event is not None, "call ended before the ordinary approval card"
                if event.get("type") == "tool_approval_request":
                    break
            build.assert_not_called()
            pending = next(iter(approval_service._pending))
            await approval_service.resolve(pending, approved=True, resolver="synthetic-owner")
        result = await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert own_approval.await_count == int(interactive)
    if ordinary and not interactive:
        assert json.loads(result)["reason"] == "approval_required"
        build.assert_not_called()
    else:
        assert ("Rejected" if interactive else "interactive session") in result
    config.save_config_json.assert_not_called()
    assert approval_service.pending_count() == 0
