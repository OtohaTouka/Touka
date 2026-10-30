"""Non-default public fields must reach real native adapters and backend seams."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, call, create_autospec

import guard_native_tool_contracts as guard
import pytest
from mcp import types
from memory_server import tools as memory_gateway
from memory_server.factual import FactualStore
from memory_server.models.mission import Task
from memory_server.services.manager import MemoryManager
from memory_server.services.mission_manager import MissionManager
from memory_server.tools import diag
from memory_server.tools.contracts import public_parameters
from src.models.identity import AgentIdentity
from src.services.creation.models import CommitResult, CreationResult, ValidationReport
from src.services.tool_executor import ToolExecutor
from src.tools import editor, mission_tools, subagent_tools
from src.tools.builtin import creation, evolution, memory, system
from src.tools.contracts import PUBLIC_TOOL_CONTRACTS

from tests.support.zone_roots import point_zone_roots

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

TASK_INPUT = {
    "task_id": "task-a",
    "description": "verify bounded change",
    "dependency_task_ids": ["task-before"],
    "verification_cmd": "python -B -m pytest",
    "boundary_rules": ["sandbox only"],
    "review_required": True,
    "review_risk": "high",
    "test_log": "one expected test passed",
    "artifact_refs": ["reports/result.txt"],
}
CASES = [
    (
        "memory_tools",
        {
            "action": "save",
            "content": "specific remembered fact",
            "tags": ["tag"],
            "importance": "4",
            "emotions": ["期待"],
        },
    ),
    (
        "memory_tools",
        {
            "action": "query",
            "query": "specific",
            "limit": 2,
            "tags": ["tag"],
            "importance": "4",
            "emotions": ["期待"],
            "date_start": "2026-09-10",
            "date_end": "2026-09-11",
            "is_recent": True,
            "include_sources": True,
            "include_history": True,
        },
    ),
    ("memory_tools", {"action": "forget", "fact_id_to_archive": "fact-selected"}),
    (
        "memory_tools",
        {
            "action": "manage_dups",
            "operation": "supersede",
            "target_id": "fact-selected",
            "content": "the replacing memory",
            "tags": ["tag"],
            "importance": "4",
            "emotions": ["期待"],
        },
    ),
    ("memory_tools", {"action": "random"}),
    ("memory_tools", {"action": "diagnostics"}),
    (
        "mission_tools",
        {
            "action": "initialize",
            "goal": "ship bounded change",
            "tasks": [TASK_INPUT],
            "target_agent_id": "DispatchAgent",
            "notify_on_complete": True,
        },
    ),
    (
        "mission_tools",
        {
            "action": "update_status",
            "task_id": "task-a",
            "status": "BLOCKED",
            "artifact_refs": ["reports/result.txt"],
            "test_log": "bounded output",
            "blocker_detail": "waiting for input",
            "target_agent_id": "DispatchAgent",
        },
    ),
    ("mission_tools", {"action": "get_board", "target_agent_id": "DispatchAgent"}),
    (
        "mission_tools",
        {"action": "await_dispatch_result", "callback_token": "callback-a", "timeout": 2.5},
    ),
    (
        "mission_tools",
        {"action": "finalize", "verdict": "accepted evidence", "target_agent_id": "DispatchAgent"},
    ),
    (
        "mission_tools",
        {
            "action": "request_review",
            "task_id": "task-a",
            "producer_claims": "specific claim",
            "critic_model": "mock-critic",
            "target_agent_id": "DispatchAgent",
        },
    ),
    (
        "evolution_implement_and_verify",
        {
            "skill_id": "sample-tool",
            "code": "pass",
            "test_tool_name": "ping",
            "test_arguments": {"sample": "value"},
            "dependencies": ["example-package"],
            "submit_for_deploy": True,
            "timeout_override": 3.5,
            "max_storage_mb": 4.5,
        },
    ),
    (
        "create_skill",
        {
            "id": "sample-skill",
            "name": "Sample",
            "description": "specific use",
            "category": "Example",
            "author": "Other Author",
            "system_prompt": "specific prompt",
            "required_tools": ["read"],
            "mcp_servers": ["example-server"],
        },
    ),
    ("ide_open_diff", {"path": "sample.py", "new_content": "updated content"}),
    ("edit_soul", {"action": "write", "type": "soul", "filename": "notes.md", "content": "a" * 60}),
    (
        "edit_soul",
        {
            "action": "str_replace",
            "type": "soul",
            "filename": "notes.md",
            "old_str": "old",
            "new_str": "new",
        },
    ),
    (
        "edit_soul",
        {
            "action": "insert",
            "type": "soul",
            "filename": "notes.md",
            "insert_line": 2,
            "new_str": "new",
        },
    ),
    ("edit_soul", {"action": "undo_edit", "type": "soul", "filename": "notes.md"}),
    ("edit_soul", {"action": "delete", "type": "soul", "filename": "notes.md"}),
    (
        "dispatch_subagent_group",
        {
            "subtasks": [
                {"goal": "bounded subtask", "task_id": "task-a", "sub_model": "mock-submodel"}
            ]
        },
    ),
    ("memory_tools", {"action": "reindex"}),  # appended: CASES[6] stays mission initialize
    (
        "memory_tools",
        {
            "action": "conversations",
            "query": "rain",
            "date_start": "2026-05-13",
            "date_end": "2026-05-14",
            "role": "user",
            "page_size": 3,
            "offset": 1,
            "source": "legacy",
        },
    ),
]


def _field_values(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, item in value.items():
        path = prefix + name
        result[path] = item
        if isinstance(item, dict):
            result.update(_field_values(item, path + "."))
        elif isinstance(item, list) and item and all(isinstance(entry, dict) for entry in item):
            nested = [_field_values(entry) for entry in item]
            for key in set().union(*(entry.keys() for entry in nested)):
                result[f"{path}[].{key}"] = [entry.get(key) for entry in nested]
    return result


async def _execute(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    handler: Callable[..., Any],
    args: dict[str, Any],
) -> str:
    permission = MagicMock()
    permission.get_tool_action_param.return_value = "action"
    permission.evaluate_policy.return_value = SimpleNamespace(effect="ALLOW", reason=None)
    monkeypatch.setattr("src.services.tool_executor.permission_service", permission)
    monkeypatch.setattr("src.services.tool_executor.soul_service.is_toukasoul", lambda _: False)
    monkeypatch.setattr(
        "src.services.tool_executor.session_service.get_session_by_id", AsyncMock(return_value=None)
    )
    executor = ToolExecutor(None, {name: handler}, {}, {}, enforce_advertised=False)
    return await executor.execute_to_queue(
        name,
        args,
        "contract-request",
        {
            "identity": AgentIdentity.for_role(agent_id="DebugAgent", role="Owner"),
            "session_id": "trusted-session",
            "auth_level": "SafeAuto",
            "model": "mock-producer",
        },
        asyncio.Queue(),
    )


async def _memory_observation(
    monkeypatch: pytest.MonkeyPatch, args: dict[str, Any]
) -> dict[str, Any]:
    manager = create_autospec(MemoryManager, instance=True)
    manager.save_memory = AsyncMock(return_value="saved")
    manager.query_memory = AsyncMock(return_value="queried")
    manager.resolve_duplicate.return_value = "archived"
    manager.normalize_agent_id.return_value = "DebugAgent"
    monkeypatch.setattr(memory_gateway, "_global_manager", manager)
    action = args["action"]
    leaf = AsyncMock(return_value="ok")
    if action in {"random", "diagnostics", "reindex", "conversations"}:
        monkeypatch.setitem(memory_gateway.MEMORY_ACTIONS, action, leaf)
    supersede = MagicMock(return_value="superseded")
    if action == "manage_dups":
        monkeypatch.setattr(diag, "supersede_memory", supersede)
    result = await _execute(monkeypatch, "memory_tools", memory.memory_tools, args)
    assert "error_class" not in result and "Error:" not in result
    if action in {"save", "query"}:
        backend = manager.save_memory if action == "save" else manager.query_memory
        backend.assert_awaited_once()
        actual = dict(backend.await_args.kwargs)
        assert actual.pop("identity").agent_id == "DebugAgent"
        return {"action": action, **actual}
    if action == "forget":
        manager.resolve_duplicate.assert_called_once()
        return {
            "action": "forget",
            "fact_id_to_archive": manager.resolve_duplicate.call_args.kwargs["fact_id_to_archive"],
        }
    if action == "manage_dups":
        manager.resolve_duplicate.assert_not_called()
        manager.find_duplicates.assert_not_called()
        supersede.assert_called_once()
        called_manager, who, old_fact_id, content = supersede.call_args.args
        assert called_manager is manager and who.agent_id == "DebugAgent"
        return {
            "action": "manage_dups",
            "operation": "supersede",
            "target_id": old_fact_id,
            "content": content,
            **{
                key: supersede.call_args.kwargs[key]
                for key in ("tags", "importance", "emotions")
            },
        }
    leaf.assert_awaited_once()
    assert leaf.await_args.kwargs["identity"].agent_id == "DebugAgent"
    public = {k: v for k, v in leaf.await_args.kwargs.items() if k not in {"identity", "manager"}}
    return {"action": action, **public}


async def _mission_observation(
    monkeypatch: pytest.MonkeyPatch, args: dict[str, Any]
) -> dict[str, Any]:
    task = Task(id="task-a", description="task", status="DONE")
    state = SimpleNamespace(
        tasks=[task],
        metadata={},
        goal="goal",
        mission_id="mission-a",
        status="DONE",
        final_verdict="accepted evidence",
    )
    manager = MagicMock()
    manager.initialize_mission = AsyncMock(return_value=state)
    manager.save_mission = AsyncMock()
    manager.get_current_mission = AsyncMock(return_value=state)
    manager.update_task = AsyncMock()
    manager.finalize_mission = AsyncMock(return_value=state)
    monkeypatch.setattr("memory_server.services.mission_manager.MissionManager", lambda: manager)
    callbacks = MagicMock()
    callbacks.generate_token.return_value = "generated-token"
    callbacks.wait_for_result = AsyncMock(return_value={"status": "DONE", "task_id": "task-a"})
    monkeypatch.setattr(mission_tools, "get_callback_store", lambda: callbacks)
    monkeypatch.setattr("src.services.subagent_results.read_result", lambda *_: None)
    review = AsyncMock(
        return_value=SimpleNamespace(
            review_status="APPROVED", note="reviewed", evidence_digest="digest"
        )
    )
    monkeypatch.setattr("src.services.review.review_loop.run_review_round", review)
    result = await _execute(monkeypatch, "mission_tools", mission_tools.mission_tools, args)
    assert "error_class" not in result and "Error:" not in result
    action = args["action"]
    if action == "initialize":
        actual = dict(manager.initialize_mission.await_args.kwargs)
        initial = actual.pop("initial_tasks")
        actual_tasks = [Task(**item) for item in initial]
        restored = []
        for item in actual_tasks:
            restored.append(
                {
                    "task_id": item.id,
                    "description": item.description,
                    "dependency_task_ids": item.dependencies,
                    "verification_cmd": item.verification_cmd,
                    "boundary_rules": item.boundary_rules,
                    "review_required": item.review_required,
                    "review_risk": item.review_risk,
                    "test_log": item.test_log,
                    "artifact_refs": item.artifact_refs,
                }
            )
        callbacks.register.assert_called_once_with("generated-token")
        return {
            "action": "initialize",
            "goal": actual["goal"],
            "target_agent_id": actual["target_agent_id"],
            "tasks": restored,
            "notify_on_complete": callbacks.register.call_count == 1,
        }
    if action == "update_status":
        actual = dict(manager.update_task.await_args.kwargs)
        actual.pop("identity")
        actual.pop("artifact_ref")
        actual["status"] = actual["status"].value
        return {"action": "update_status", **actual}
    if action == "get_board":
        return {
            "action": "get_board",
            "target_agent_id": manager.get_current_mission.await_args.args[1],
        }
    if action == "await_dispatch_result":
        return {
            "action": action,
            "callback_token": callbacks.wait_for_result.await_args.args[0],
            "timeout": callbacks.wait_for_result.await_args.kwargs["timeout"],
        }
    if action == "finalize":
        return {
            "action": action,
            "verdict": manager.finalize_mission.await_args.args[1],
            "target_agent_id": manager.finalize_mission.await_args.args[2],
        }
    actual = review.await_args.kwargs
    return {
        "action": "request_review",
        "task_id": actual["task"].id,
        "producer_claims": actual["producer_claims"],
        "critic_model": actual["critic_model"],
        "target_agent_id": manager.get_current_mission.await_args.args[1],
    }


async def _other_observation(
    monkeypatch: pytest.MonkeyPatch, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    if name == "create_skill":
        backend = AsyncMock(
            return_value=CreationResult(
                validation=ValidationReport(ok=True),
                committed=True,
                commit=CommitResult(id="sample-skill"),
            )
        )
        monkeypatch.setattr(creation.creation_api, "create_skill", backend)
        await _execute(monkeypatch, name, creation.create_skill, args)
        spec = backend.await_args.args[0]
        assert spec.is_system is False
        return spec.model_dump(exclude={"is_system"})
    if name == "evolution_implement_and_verify":
        write = AsyncMock(return_value="implementation complete")
        verify = AsyncMock(return_value=evolution.VerifyOutcome(True, "verified"))
        deploy = AsyncMock(return_value="queued for approval")
        monkeypatch.setattr(evolution.EvolutionPathGuard, "tool_file_exists", lambda *_: True)
        monkeypatch.setattr(evolution, "write_mcp_implementation", write)
        monkeypatch.setattr(evolution, "_verify_mcp", verify)
        monkeypatch.setattr(evolution, "request_mcp_creation", deploy)
        await _execute(monkeypatch, name, evolution.evolution_implement_and_verify, args)
        observed = {
            key: write.await_args.kwargs[key] for key in ("skill_id", "code", "dependencies")
        }
        verified = verify.await_args.kwargs
        observed.update(
            test_tool_name=verified["tool_name"],
            test_arguments=verified["arguments"],
            timeout_override=verified["timeout_override"],
            max_storage_mb=verified["max_storage_mb"],
            submit_for_deploy=deploy.await_count == 1,
        )
        return observed
    if name == "ide_open_diff":
        post = AsyncMock(return_value=SimpleNamespace(status_code=200))
        client = MagicMock()
        client.post = post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(
            editor, "_get_ide_config", lambda: {"port": 12345, "authToken": "mock-token"}
        )
        # 建構點只帶 follow_redirects（禁令 22，3a-U2 起明寫）；多帳或漏帶都會 TypeError
        monkeypatch.setattr(editor.httpx, "AsyncClient", lambda *, follow_redirects: client)
        await _execute(monkeypatch, name, editor.ide_open_diff, args)
        actual = post.await_args.kwargs["json"]["params"]["arguments"]
        assert actual["sessionId"] == "trusted-session"
        return {"path": actual["filePath"], "new_content": actual["newContent"]}
    if name == "edit_soul":
        backend = AsyncMock(return_value="edited sandbox soul")
        monkeypatch.setattr(system, "update_my_soul", backend)
        await _execute(monkeypatch, name, system.edit_soul, args)
        actual = dict(backend.await_args.kwargs)
        assert actual.pop("identity").agent_id == "DebugAgent"
        return {
            key: actual[key]
            for key in (
                "action",
                "type",
                "filename",
                "content",
                "old_str",
                "new_str",
                "insert_line",
            )
            if key in actual
        }
    group = AsyncMock(return_value=[{"status": "DONE"}])
    monkeypatch.setattr(subagent_tools, "run_subagent_group", group)
    await _execute(monkeypatch, name, subagent_tools.dispatch_subagent_group, args)
    assert group.await_args.kwargs["concurrency"] == 4
    return {"subtasks": group.await_args.kwargs["subtasks"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "arguments"), CASES)
async def test_every_public_field_reaches_real_adapter_backend(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    arguments: dict[str, Any],
) -> None:
    if name == "memory_tools":
        observed = await _memory_observation(monkeypatch, arguments)
    elif name == "mission_tools":
        observed = await _mission_observation(monkeypatch, arguments)
    else:
        observed = await _other_observation(monkeypatch, name, arguments)
    expected_values = _field_values(arguments)
    actual_values = _field_values(observed)
    assert guard.observation_errors(expected_values, actual_values) == []
    contract = PUBLIC_TOOL_CONTRACTS[name]
    model = contract[arguments["action"]] if isinstance(contract, dict) else contract
    # Contract-generated properties drive coverage. Adding an optional/nested
    # field without a real non-default observation makes this test fail.
    from_schema = guard.schema_paths(public_parameters(model))
    assert (
        guard.coverage_errors(
            {path for path in from_schema if not path.endswith("[]")}, actual_values
        )
        == []
    )


@pytest.mark.asyncio
async def test_manage_dups_resolve_still_reaches_the_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = create_autospec(MemoryManager, instance=True)
    manager.resolve_duplicate.return_value = "archived"
    monkeypatch.setattr(memory_gateway, "_global_manager", manager)
    args = {"action": "manage_dups", "operation": "resolve", "target_id": "fact-selected"}

    result = await _execute(monkeypatch, "memory_tools", memory.memory_tools, args)

    assert "archived" in result
    manager.resolve_duplicate.assert_called_once()
    assert manager.resolve_duplicate.call_args.args[1] == "fact-selected"


def test_real_flow_observation_assertion_rejects_dropped_emotions() -> None:
    expected = _field_values({"action": "query", "emotions": ["期待"]})
    dropped_by_backend = _field_values({"action": "query"})
    assert guard.observation_errors(expected, dropped_by_backend)


def test_field_coverage_rejects_new_nested_optional_without_observation() -> None:
    parameters = public_parameters(PUBLIC_TOOL_CONTRACTS["mission_tools"]["initialize"])
    parameters["properties"]["tasks"]["items"]["properties"]["new_optional"] = {"type": "string"}
    paths = {path for path in guard.schema_paths(parameters) if not path.endswith("[]")}
    observed = _field_values(CASES[6][1])
    errors = guard.coverage_errors(paths, observed)
    assert any("tasks[].new_optional" in error for error in errors)


@pytest.mark.asyncio
async def test_real_mission_manager_persists_review_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = {
        "action": "initialize",
        "goal": "sandbox integration",
        "tasks": [
            {"task_id": "task-before", "description": "dependency"},
            {**TASK_INPUT, "artifact_ref": "reports/legacy.txt"},
        ],
    }
    result = await _execute(monkeypatch, "mission_tools", mission_tools.mission_tools, args)
    assert "Error:" not in result and "error_class" not in result
    state = await MissionManager().get_current_mission(
        AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")
    )
    assert state is not None
    task = next(item for item in state.tasks if item.id == "task-a")
    assert task.review_required and task.review_risk == "high"
    assert task.test_log == TASK_INPUT["test_log"]
    assert task.artifact_refs == TASK_INPUT["artifact_refs"]
    assert task.artifact_ref == "reports/legacy.txt"
    assert task.verification_cmd_locked and task.dependencies == ["task-before"]


@pytest.mark.asyncio
async def test_query_fact_id_can_archive_only_its_own_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    own = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")
    other = AgentIdentity.for_role(agent_id="OtherAgent", role="Owner")
    factual = FactualStore(tmp_path / "factual")
    selected = factual.save_fact(own, "unique fact", emotions=["期待"])["id"]
    unrelated = factual.save_fact(other, "unique fact")["id"]
    manager = MemoryManager.__new__(MemoryManager)
    vector = MagicMock()
    vector.query_vectors.return_value = []
    monkeypatch.setattr(manager, "get_factual_store", lambda: factual)
    monkeypatch.setattr(manager, "get_vector_store", lambda: vector)
    monkeypatch.setattr(memory_gateway, "get_manager", lambda: manager)
    result = await _execute(
        monkeypatch, "memory_tools", memory.memory_tools, {"action": "query", "query": "unique"}
    )
    assert f"[ID: {selected}]" in result and unrelated not in result
    archived = await _execute(
        monkeypatch,
        "memory_tools",
        memory.memory_tools,
        {"action": "forget", "fact_id_to_archive": selected},
    )
    assert "archived" in archived
    await _execute(
        monkeypatch,
        "memory_tools",
        memory.memory_tools,
        {"action": "forget", "fact_id_to_archive": unrelated},
    )
    assert factual.query_facts(own, query="unique") == []
    assert [item["id"] for item in factual.query_facts(other, query="unique")] == [unrelated]
    vector.archive_vector_by_fact_id.assert_called_once_with(own, selected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "expected"),
    [(3.5, 3), (0.5, 1), (True, 17), (False, 17), ("3", 17), (-2, 17), (1000, 17), (None, 17)],
)
async def test_real_verifier_clamps_raw_quota_once_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, requested: Any, expected: int
) -> None:
    original = evolution._clamp_quota
    clamp = MagicMock(wraps=original)
    monkeypatch.setattr(evolution, "_clamp_quota", clamp)
    monkeypatch.setattr(
        evolution.autonomy_service,
        "config",
        {"sandbox_limits": {"execution_timeout_sec": 17, "max_storage_mb": 17}},
    )
    (tmp_path / "zone_data").mkdir()
    point_zone_roots(monkeypatch, tmp_path / "zone_data", tmp_path / "zone_state")
    monkeypatch.setattr(evolution.EvolutionPathGuard, "tool_file_exists", lambda *_: True)
    monkeypatch.setattr(evolution.EvolutionPathGuard, "write_tool_file", MagicMock())
    monkeypatch.setattr(evolution.EvolutionPathGuard, "delete_tool_file", MagicMock())
    monkeypatch.setattr(evolution.EvolutionPathGuard, "is_windows", lambda: False)
    limit_values = evolution.EvolutionPathGuard.rlimit_values
    rlimits = MagicMock(wraps=limit_values)
    monkeypatch.setattr(evolution.EvolutionPathGuard, "rlimit_values", rlimits)
    session = SimpleNamespace(call_tool=AsyncMock(return_value=types.CallToolResult(content=[])))
    connect = AsyncMock(return_value=(SimpleNamespace(session=session), AsyncExitStack()))
    monkeypatch.setattr(evolution, "connect_mcp_server", connect)
    monkeypatch.setattr(evolution, "write_mcp_implementation", AsyncMock(return_value="written"))
    deploy = AsyncMock()
    monkeypatch.setattr(evolution, "request_mcp_creation", deploy)
    await _execute(
        monkeypatch,
        "evolution_implement_and_verify",
        evolution.evolution_implement_and_verify,
        {
            "skill_id": "sample",
            "code": "pass",
            "test_tool_name": "ping",
            "test_arguments": {},
            "timeout_override": requested,
            "max_storage_mb": requested,
        },
    )
    assert clamp.call_count == 2
    assert clamp.call_args_list == [call(requested, 17), call(requested, 17)]
    connect.assert_awaited_once()
    assert connect.call_args.kwargs["read_timeout_seconds"] == expected
    launch = connect.call_args.kwargs["isolated"].launch
    assert launch.timeout == expected
    rlimits.assert_called_once_with(256, expected, expected)
    assert launch.request({}).profile.rlimits == limit_values(256, expected, expected)
    wrapper = evolution.EvolutionPathGuard.write_tool_file.call_args.args[2]
    assert f"MAX_BYTES = {expected} * 1024 * 1024" in wrapper
    session.call_tool.assert_awaited_once_with("ping", {})
    deploy.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("author", [None, ""])
async def test_create_skill_author_omitted_or_empty_uses_identity_metadata(
    monkeypatch: pytest.MonkeyPatch, author: str | None
) -> None:
    args = {"id": "sample", "description": "sample"}
    if author is not None:
        args["author"] = author
    observed = await _other_observation(monkeypatch, "create_skill", args)
    assert observed["author"] == "DebugAgent"


@pytest.mark.asyncio
async def test_manage_duplicates_scan_reaches_read_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = create_autospec(MemoryManager, instance=True)
    manager.normalize_agent_id.return_value = "DebugAgent"
    manager.find_duplicates.return_value = []
    monkeypatch.setattr(memory_gateway, "get_manager", lambda: manager)
    await _execute(
        monkeypatch,
        "memory_tools",
        memory.memory_tools,
        {"action": "manage_dups", "operation": "scan"},
    )
    manager.find_duplicates.assert_called_once()
    assert manager.find_duplicates.call_args.args[0].agent_id == "DebugAgent"
    manager.resolve_duplicate.assert_not_called()
