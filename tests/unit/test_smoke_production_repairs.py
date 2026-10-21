from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from memory_server import config_utils as memory_config_utils
from memory_server import factual as factual_module
from memory_server.factual import FactualStore
from memory_server.tools import forget_memory
from memory_server.tools.diag import manage_duplicate_memories
from memory_server.vector import VectorStore
from src.models.identity import AgentIdentity
from src.tools.builtin import memory as memory_gateway

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _ExistingDockerMarker:
    def exists(self) -> bool:
        return True


class _SyncResolveManager:
    def __init__(self) -> None:
        self.calls: list[tuple[AgentIdentity, str]] = []

    @staticmethod
    def normalize_agent_id(identity: AgentIdentity) -> str:
        return identity.agent_id

    def resolve_duplicate(
        self,
        identity: AgentIdentity,
        fact_id_to_archive: str,
    ) -> str:
        self.calls.append((identity, fact_id_to_archive))
        return f"Archived {fact_id_to_archive}"


def _docker_path(value: str) -> Path | _ExistingDockerMarker:
    if value == "/.dockerenv":
        return _ExistingDockerMarker()
    return Path(value)


def _parse_source(relative_path: str) -> ast.Module:
    source_path = PROJECT_ROOT / relative_path
    return ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))


def _assigned_literal(module: ast.Module, name: str) -> Any:
    for statement in module.body:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
        ):
            return ast.literal_eval(statement.value)
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in statement.targets
        ):
            return ast.literal_eval(statement.value)
    raise AssertionError(name)


def _is_class_method_reference(
    node: ast.AST,
    *,
    class_name: str,
    method_name: str,
) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == method_name
        and isinstance(node.value, ast.Name)
        and node.value.id == class_name
    )


def _connection_is_closed(connection: factual_module.sqlite3.Connection) -> bool:
    try:
        cursor = connection.execute("SELECT 1")
    except factual_module.sqlite3.ProgrammingError:
        return True
    cursor.close()
    return False


def test_memory_app_data_dir_preserves_explicit_absolute_override_in_docker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured = tmp_path / "explicit-memory-data"
    monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(configured))
    monkeypatch.setattr(memory_config_utils, "Path", _docker_path)

    assert memory_config_utils.get_app_data_dir() == str(configured)


def test_memory_app_data_dir_keeps_docker_default_for_relative_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_APP_DATA_DIR", "relative-memory-data")
    monkeypatch.setattr(memory_config_utils, "Path", _docker_path)

    assert memory_config_utils.get_app_data_dir() == memory_config_utils.DOCKER_APP_DATA_DIR


@pytest.mark.asyncio
async def test_memory_gateway_does_not_forward_execution_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    identity = AgentIdentity(
        agent_id="DebugAgent",
        role="Debugger",
        human_role="admin",
        capabilities=frozenset(),
    )

    async def capture_memory_call(*, action: str, **kwargs: Any) -> str:
        captured.update(action=action, **kwargs)
        return "ok"

    monkeypatch.setattr(memory_gateway, "external_memory_tools", capture_memory_call)

    result = await memory_gateway.memory_tools(
        action="query",
        query="needle",
        identity=identity,
        request_id="request-1",
        agent_id="DebugAgent",
        session_id="session-1",
        project_id="project-1",
        model="test-model",
        is_toukasoul=True,
        auth_level="Manual",
        _hitl_preapproved=True,
        _interactive=True,
        _emit_event=MagicMock(),
        _call_id="call-1",
        _ctx={"request_id": "request-1"},
        resource_proxy=MagicMock(),
    )

    assert result == "ok"
    assert captured == {
        "action": "query",
        "query": "needle",
        "identity": identity,
    }


@pytest.mark.asyncio
async def test_forget_memory_uses_synchronous_manager_contract() -> None:
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
    manager = _SyncResolveManager()

    result = await forget_memory(
        identity=identity,
        manager=manager,
        fact_id_to_archive="fact-1",
    )

    assert result == "Archived fact-1"
    assert manager.calls == [(identity, "fact-1")]


@pytest.mark.asyncio
async def test_duplicate_diagnostics_uses_synchronous_manager_contract() -> None:
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
    manager = _SyncResolveManager()

    result = await manage_duplicate_memories(
        identity=identity,
        manager=manager,
        action="resolve",
        target_id="fact-2",
    )

    assert result == "Archived fact-2"
    assert manager.calls == [(identity, "fact-2")]


def test_factual_store_closes_every_sqlite_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_connect = factual_module.sqlite3.connect
    connections: list[factual_module.sqlite3.Connection] = []

    def tracking_connect(*args: Any, **kwargs: Any) -> factual_module.sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(factual_module.sqlite3, "connect", tracking_connect)
    store = object.__new__(FactualStore)
    store.db_path = tmp_path / "core_memory.db"
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")

    try:
        store._init_db()
        store.save_fact(identity, "lifecycle probe")
        store.query_facts(identity, query="lifecycle")

        assert connections
        assert all(_connection_is_closed(connection) for connection in connections)
    finally:
        for connection in connections:
            connection.close()


def test_factual_store_close_all_connections_is_idempotent_and_fail_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = MagicMock()
    first.close.side_effect = [OSError("first sqlite close failed"), None]
    second = MagicMock()
    active_connections = {first, second}
    monkeypatch.setattr(
        FactualStore,
        "_active_connections",
        active_connections,
        raising=False,
    )

    with pytest.raises(OSError, match="first sqlite close failed"):
        FactualStore.close_all_connections()

    assert active_connections == {first}
    second.close.assert_called_once_with()

    FactualStore.close_all_connections()

    assert first.close.call_count == 2
    assert active_connections == set()


def test_vector_close_all_clients_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    cache = {"client": client}
    monkeypatch.setattr(VectorStore, "_client_cache", cache, raising=False)

    VectorStore.close_all_clients()
    VectorStore.close_all_clients()

    client.close.assert_called_once_with()
    assert cache == {}


def test_vector_close_all_clients_continues_after_failure_and_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = MagicMock()
    first.close.side_effect = OSError("first close failed")
    second = MagicMock()
    cache = {"first": first, "second": second}
    monkeypatch.setattr(VectorStore, "_client_cache", cache, raising=False)

    with pytest.raises(OSError, match="first close failed"):
        VectorStore.close_all_clients()

    first.close.assert_called_once_with()
    second.close.assert_called_once_with()
    assert cache == {}


def test_tool_coverage_marks_side_effectful_tools_as_wired_only() -> None:
    module = _parse_source("tests/smoke/smoke_tool_coverage.py")
    wired_only = _assigned_literal(module, "WIRED_ONLY")

    assert {
        "stop_command",
        "dispatch_subagent",
        "dispatch_subagent_group",
    } <= wired_only.keys()


def test_memory_smoke_closes_vector_clients_in_finally() -> None:
    module = _parse_source("tests/smoke/smoke_memory_tools.py")

    assert any(
        _is_class_method_reference(
            descendant,
            class_name="VectorStore",
            method_name="close_all_clients",
        )
        for try_node in ast.walk(module)
        if isinstance(try_node, ast.Try)
        for statement in try_node.finalbody
        for descendant in ast.walk(statement)
    )


def test_memory_smoke_closes_factual_connections_in_finally() -> None:
    module = _parse_source("tests/smoke/smoke_memory_tools.py")

    assert any(
        _is_class_method_reference(
            descendant,
            class_name="FactualStore",
            method_name="close_all_connections",
        )
        for try_node in ast.walk(module)
        if isinstance(try_node, ast.Try)
        for statement in try_node.finalbody
        for descendant in ast.walk(statement)
    )
