"""Negative fixtures for native contracts; no product files are mutated."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

GUARD_PATH = Path(__file__).resolve().parents[2] / "scripts" / "guard_native_tool_contracts.py"


def _guard() -> Any:
    assert GUARD_PATH.is_file(), "The native-contract guard is not implemented yet"
    spec = importlib.util.spec_from_file_location("native_contract_guard_fixture", GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_registered_tool_requires_explicit_classification() -> None:
    guard = _guard()
    assert guard.inventory_errors({"known", "new_tool"}, {"known": "typed"})
    assert not guard.inventory_errors(
        {"known", "new_tool"}, {"known": "typed", "new_tool": "typed"}
    )


@pytest.mark.parametrize(
    "change",
    [
        "def tool(**kwargs): return kwargs.get('new_optional')",
        "def tool(**kwargs): return kwargs['new_optional']",
        "def tool(new_optional=None, **kwargs): return new_optional",
        "def tool(**kwargs): return kwargs.get(dynamic_key)",
        "def tool(**kwargs): return backend(**kwargs)",
    ],
)
def test_new_argument_or_unclassified_dynamic_consumer_changes_fail(change: str) -> None:
    guard = _guard()
    before = guard.consumer_shapes("def tool(**kwargs): return None")
    after = guard.consumer_shapes(change)
    assert guard.consumer_errors(after, before)
    assert not guard.consumer_errors(before, before)


def test_new_action_requires_contract_and_risk_classification() -> None:
    guard = _guard()
    assert guard.action_errors({"save", "resolve"}, {"save"}, {"save"}, set())
    assert not guard.action_errors(
        {"save", "resolve"}, {"save", "resolve"}, {"save", "resolve"}, set()
    )
    assert not guard.action_errors({"save", "export_facts"}, {"save"}, {"save"}, {"export_facts"})


@pytest.mark.parametrize("field", ["emotions", "review_required"])
def test_provider_or_nested_field_loss_fails(field: str) -> None:
    guard = _guard()
    expected = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "object", "properties": {field: {"type": "string"}}},
            }
        },
    }
    broken = {
        "type": "OBJECT",
        "properties": {"tasks": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {}}}},
    }
    assert guard.schema_errors(expected, broken)
    assert not guard.schema_errors(expected, expected)


def test_legacy_memory_schema_cannot_reenter_combined_payload() -> None:
    guard = _guard()
    assert guard.advertisement_errors({"memory_tools", "vault_tools"}, {"vault_tools"})
    assert not guard.advertisement_errors({"memory_tools"}, {"vault_tools"})


def test_reserved_model_fields_fail_schema_check() -> None:
    guard = _guard()
    schema = {
        "type": "object",
        "properties": {"session_id": {"type": "string"}},
        "required": ["session_id"],
    }
    assert guard.reserved_field_errors(schema, {"session_id", "identity"})


def test_new_optional_path_without_backend_observation_fails() -> None:
    guard = _guard()
    assert guard.coverage_errors(
        {"query", "emotions", "tasks[].review_required"}, {"query": "needle"}
    )
    assert not guard.coverage_errors(
        {"query", "emotions"}, {"query": "needle", "emotions": ["happy"]}
    )


def test_backend_dropped_field_does_not_count_as_covered() -> None:
    guard = _guard()
    expected_values = {"query": "needle", "emotions": ["happy"]}
    actual_backend = {"query": "needle"}
    assert guard.observation_errors(expected_values, actual_backend)
    assert not guard.observation_errors(expected_values, dict(expected_values))


def test_source_guard_never_imports_memory_package() -> None:
    guard = _guard()
    source = GUARD_PATH.read_text(encoding="utf-8")
    assert "import memory_server" not in source
    assert "from memory_server" not in source
    assert guard.run_all() == []


@pytest.fixture
def source_fixture(tmp_path: Path) -> Path:
    guard = _guard()
    root = GUARD_PATH.parent.parent
    baseline = json.loads(guard.BASELINE_PATH.read_text(encoding="utf-8"))
    paths = set(baseline["consumers"]) | {
        guard.PROXY_CONTRACTS,
        guard.MEMORY_CONTRACTS,
        "ToukaProxy/config/auth_policies.json.example",
    }
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    assert guard.run_all(tmp_path) == []
    return tmp_path


@pytest.mark.parametrize(
    ("relative", "before", "after"),
    [
        (
            "ToukaProxy/src/tools/builtin/__init__.py",
            '"memory_tools": memory_tools,',
            '"memory_tools": mission_tools,',
        ),
        (
            "ToukaProxy/src/tools/builtin/__init__.py",
            "from .memory import memory_tools as memory_tools",
            "from .mission_tools import memory_tools as memory_tools",
        ),
        (
            "ToukaProxy/src/tools/builtin/__init__.py",
            '"memory_tools": memory_tools,',
            '"memory_tools": memory_tools, "new_tool": memory_tools,',
        ),
        (
            "ToukaProxy/src/tools/builtin/creation.py",
            'skill_id = kwargs.get("id")',
            'skill_id = kwargs.get("new_optional")',
        ),
        (
            "ToukaMemory/src/memory_server/services/mission_manager.py",
            't.get("review_required", False)',
            't.get("new_review_field", False)',
        ),
        (
            "ToukaMemory/src/memory_server/tools/__init__.py",
            '"save": save_memory,',
            '"save": save_memory, "new_action": save_memory,',
        ),
        (
            "ToukaProxy/src/tools/builtin/creation.py",
            'skill_id = kwargs.get("id")',
            "skill_id = kwargs.get(dynamic_key)",
        ),
        (
            "ToukaMemory/src/memory_server/tools/schemas.py",
            "MEMORY_TOOLS_SCHEMA_LIST = [MEMORY_TOOLS_SCHEMA]",
            "MEMORY_TOOLS_SCHEMA_LIST = [MEMORY_TOOLS_SCHEMA, VAULT_TOOLS_SCHEMA]",
        ),
        (
            "ToukaProxy/src/tools/mission_tools.py",
            'if action == "initialize":',
            'if action == "undeclared_action":\n            return "new"\n'
            '        elif action == "initialize":',
        ),
        (
            "ToukaProxy/src/tools/builtin/__init__.py",
            '"memory_tools": memory_tools,',
            '"memory_tools": memory_tools,',
        ),
    ],
)
def test_complete_source_guard_detects_real_wiring_mutations(
    source_fixture: Path,
    relative: str,
    before: str,
    after: str,
) -> None:
    path = source_fixture / relative
    source = path.read_text(encoding="utf-8")
    assert before in source
    path.write_text(source.replace(before, after, 1), encoding="utf-8")
    if before == after:
        path.write_text(
            path.read_text(encoding="utf-8")
            + '\ntool_registry.update({"added_dynamically": memory_tools})\n',
            encoding="utf-8",
        )
    assert _guard().run_all(source_fixture)


def test_primitive_array_item_type_drift_fails() -> None:
    guard = _guard()
    expected = {
        "type": "object",
        "properties": {"emotions": {"type": "array", "items": {"type": "string"}}},
    }
    broken = {
        "type": "object",
        "properties": {"emotions": {"type": "array", "items": {"type": "integer"}}},
    }
    assert guard.schema_errors(expected, broken)


def test_nested_array_item_required_drift_fails() -> None:
    guard = _guard()
    item = {"type": "object", "properties": {"task_id": {"type": "string"}}}
    expected = {
        "type": "object",
        "properties": {"tasks": {"type": "array", "items": {**item, "required": ["task_id"]}}},
    }
    broken = {"type": "object", "properties": {"tasks": {"type": "array", "items": item}}}
    assert guard.schema_errors(expected, broken)


def test_native_guard_and_minimal_pr_workflow_are_wired() -> None:
    root = GUARD_PATH.parent.parent
    assert "guard_native_tool_contracts.py" in (root / "scripts/guard_all.py").read_text(
        encoding="utf-8"
    )
    workflow = root / ".github/workflows/native-tool-contracts.yml"
    assert workflow.is_file()
    text = workflow.read_text(encoding="utf-8")
    steps = yaml.safe_load(text)["jobs"]["contracts"]["steps"]
    runs = [step["run"] for step in steps if "run" in step]
    guard = "uv run --no-sync python -B scripts/guard_native_tool_contracts.py"
    assert any(run.startswith("uv sync --locked --all-packages") for run in runs)
    assert guard in runs
    pytest_runs = [run for run in runs if "python -m pytest tests/" in run]
    assert len(pytest_runs) == 1
    assert runs.index(guard) < runs.index(pytest_runs[0])
    assert "pull_request:" in text and "pull_request_target" not in text
    assert "contents: read" in text and "persist-credentials: false" in text
    assert "secrets." not in text


def test_execution_binding_metadata_follows_actual_registry_reference() -> None:
    guard = _guard()
    source = (GUARD_PATH.parent.parent / guard.BUILTIN_REGISTRY).read_text(encoding="utf-8")
    bindings, errors = guard.execution_binding_metadata(source)
    assert errors == []
    assert bindings["bash"] == {
        "canonical_name": "execute_command",
        "registry_name": "bash",
        "kind": "TERMINAL",
    }
    assert bindings["request_skill_install"]["kind"] == "SELF_APPROVING"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('tool_registry["bash"]', 'tool_registry["write"]'),
        ('"execute_command", tool_registry["bash"]', '"invented", tool_registry["bash"]'),
        ("ToolExecutionClass.TERMINAL", "ToolExecutionClass.ORDINARY"),
    ],
)
def test_execution_binding_rejects_invalid_actual_wiring(before: str, after: str) -> None:
    guard = _guard()
    source = (GUARD_PATH.parent.parent / guard.BUILTIN_REGISTRY).read_text(encoding="utf-8")
    assert before in source
    _bindings, errors = guard.execution_binding_metadata(source.replace(before, after, 1))
    assert errors


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('tool_registry["bash"]', 'tool_registry["write"]'),
        ("ToolExecutionClass.TERMINAL", "ToolExecutionClass.SELF_APPROVING"),
        ("BUILTIN_EXECUTION_BINDINGS = MappingProxyType(", "BUILTIN_EXECUTION_BINDINGS = dict("),
    ],
)
def test_full_guard_rejects_execution_classification_drift(
    source_fixture: Path, before: str, after: str
) -> None:
    guard = _guard()
    path = source_fixture / guard.BUILTIN_REGISTRY
    source = path.read_text(encoding="utf-8")
    assert before in source
    path.write_text(source.replace(before, after, 1), encoding="utf-8")
    assert guard.run_all(source_fixture)


def test_full_guard_rejects_execution_binding_reassignment(source_fixture: Path) -> None:
    guard = _guard()
    path = source_fixture / guard.BUILTIN_REGISTRY
    path.write_text(
        path.read_text(encoding="utf-8") + "\nBUILTIN_EXECUTION_BINDINGS = {}\n", encoding="utf-8"
    )
    assert guard.run_all(source_fixture)


INTERNAL_DECLARATION = 'MEMORY_INTERNAL_ACTIONS = frozenset({"export_facts"})'


def _replace_once(path: Path, before: str, after: str) -> None:
    source = path.read_text(encoding="utf-8")
    assert before in source
    path.write_text(source.replace(before, after, 1), encoding="utf-8")


def test_guard_reads_internal_actions_from_memory_contract_source(source_fixture: Path) -> None:
    guard = _guard()
    _replace_once(
        source_fixture / guard.MEMORY_CONTRACTS,
        INTERNAL_DECLARATION,
        'MEMORY_INTERNAL_ACTIONS = frozenset({"export_facts", "audit_facts"})',
    )
    _replace_once(
        source_fixture / guard.MEMORY_REGISTRY,
        '"export_facts": export_facts,',
        '"export_facts": export_facts,\n    "audit_facts": export_facts,',
    )
    assert guard.run_all(source_fixture) == []


@pytest.mark.parametrize(
    "after",
    [
        'MEMORY_INTERNAL_ACTIONS = frozenset({"export_facts", "save"})',
        "MEMORY_INTERNAL_ACTIONS = frozenset(_INTERNAL)",
        INTERNAL_DECLARATION + '\nMEMORY_INTERNAL_ACTIONS = frozenset({"export_facts", "save"})',
    ],
)
def test_guard_rejects_unsafe_internal_action_declarations(
    source_fixture: Path, after: str
) -> None:
    guard = _guard()
    _replace_once(source_fixture / guard.MEMORY_CONTRACTS, INTERNAL_DECLARATION, after)
    assert guard.run_all(source_fixture)


# --- the trusted source of a superseding memory (memory lifecycle) ---------------------

DIAG = "ToukaMemory/src/memory_server/tools/diag.py"


def test_supersede_source_must_be_declared_trusted(source_fixture: Path, tmp_path: Path) -> None:
    guard = _guard()
    baseline = json.loads(guard.BASELINE_PATH.read_text(encoding="utf-8"))
    [binding] = [
        item
        for item in baseline["typed_consumers"]
        if item["source"] == DIAG and item["function"] == "manage_duplicate_memories"
    ]
    assert "source_context" in binding["trusted"]
    binding["trusted"].remove("source_context")
    stripped = tmp_path / "baseline_without_trusted_source.json"
    stripped.write_text(json.dumps(baseline), encoding="utf-8")

    errors = guard.run_all(source_fixture, stripped)

    assert f"Undeclared public consumer: {DIAG}:manage_duplicate_memories:source_context" in errors


def test_supersede_handler_cannot_forward_keyword_arguments(source_fixture: Path) -> None:
    guard = _guard()
    _replace_once(
        source_fixture / DIAG,
        '    if action == "scan":',
        '    if action == "forward":\n        return supersede_memory(**kwargs)\n'
        '    if action == "scan":',
    )

    errors = guard.run_all(source_fixture)

    assert f"Unclassified dynamic forwarding: {DIAG}:manage_duplicate_memories" in errors
