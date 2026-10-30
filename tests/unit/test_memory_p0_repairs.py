"""Memory repairs: random recall, diagnostics, duplicate scan and the retired JSONL import.

Every store is sandboxed: a tmp FactualStore, a synthetic legacy DB under the session
sandbox, and a local Qdrant directory in tmp. No embedding model is loaded.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from memory_server import factual as factual_module
from memory_server import tools as memory_gateway
from memory_server.factual import FactualStore
from memory_server.services import manager as manager_module
from memory_server.services.legacy_store import LegacyStore
from memory_server.services.manager import MemoryManager
from memory_server.vector import VectorStore
from qdrant_client.models import Distance, PointStruct, VectorParams
from src.models.identity import AgentIdentity
from src.services import gdpr_export_service
from src.services.resource_manager import resource_manager
from src.tools.builtin import memory as proxy_memory_gateway

from tests.support.zone_roots import point_zone_roots


class _SyntheticStoreFailure(Exception):
    """A storage failure raised by the test doubles below."""


def _identity(prefix: str = "MemProbe") -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=f"{prefix}{uuid.uuid4().hex[:8]}", role="Owner")


def _raise_store_failure(*_args: Any, **_kwargs: Any) -> Any:
    raise _SyntheticStoreFailure


def _seed_legacy(identity: AgentIdentity, contents: list[str]) -> None:
    directory = Path(resource_manager.get_vault_path("legacy", identity))
    directory.mkdir(parents=True, exist_ok=True)
    store = LegacyStore(str(directory / "legacy_history.db"))
    store.upsert_session(
        session_id="legacy-session",
        identity=identity,
        filename="legacy-session.json",
        title="legacy",
        created_at=1700000000000,
        updated_at=1700000000000,
    )
    store.upsert_messages(
        [
            {
                "id": f"legacy-{index}",
                "session_id": "legacy-session",
                "role": "user",
                "content": content,
                "timestamp": 1700000000000,
            }
            for index, content in enumerate(contents)
        ]
    )


def _plant_jsonl(vault: Path, agent: str, name: str = "facts.jsonl") -> Path:
    path = vault / agent / "fact" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"id": "old-1", "content": "an old fact"}\n', encoding="utf-8")
    return path


@pytest.fixture
def factual_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FactualStore:
    store = FactualStore(base_dir=tmp_path / "vault")
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", store)
    return store


@pytest.fixture
def vector_stub(monkeypatch: pytest.MonkeyPatch) -> Any:
    vector = create_autospec(VectorStore, instance=True)
    vector.save_vector.return_value = True
    vector.query_vectors.return_value = []
    vector.agent_point_count.return_value = 0
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", vector)
    return vector


@pytest.fixture
def manager(
    monkeypatch: pytest.MonkeyPatch, factual_store: FactualStore, vector_stub: Any
) -> MemoryManager:
    real = MemoryManager()
    monkeypatch.setattr(memory_gateway, "_global_manager", real)
    return real


# --- random recall ---------------------------------------------------------


@pytest.mark.asyncio
async def test_random_recall_fact_and_legacy(
    manager: MemoryManager, factual_store: FactualStore
) -> None:
    identity = _identity()
    factual_store.save_fact(identity, "the fact about green tea")
    _seed_legacy(identity, ["the legacy line about rain"])

    result = await memory_gateway.memory_tools(action="random", identity=identity)

    assert "the fact about green tea" in result
    assert "the legacy line about rain" in result


@pytest.mark.asyncio
async def test_random_recall_all_sources_failed_is_structured_error(
    manager: MemoryManager, factual_store: FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    monkeypatch.setattr(factual_store, "query_facts", _raise_store_failure)
    monkeypatch.setattr(LegacyStore, "get_random_messages", _raise_store_failure)

    result = await proxy_memory_gateway.memory_tools(action="random", identity=identity)

    assert json.loads(result)["error_class"] == "internal_error"


@pytest.mark.asyncio
async def test_random_recall_failed_source_with_nothing_else_is_not_empty_memory(
    manager: MemoryManager, factual_store: FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    monkeypatch.setattr(factual_store, "query_facts", _raise_store_failure)

    result = await proxy_memory_gateway.memory_tools(action="random", identity=identity)

    assert json.loads(result)["error_class"] == "internal_error"


@pytest.mark.asyncio
async def test_random_recall_keeps_working_half_when_other_half_fails(
    manager: MemoryManager, factual_store: FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    _seed_legacy(identity, ["only the legacy half answered"])
    monkeypatch.setattr(factual_store, "query_facts", _raise_store_failure)

    result = await memory_gateway.memory_tools(action="random", identity=identity)

    assert "only the legacy half answered" in result


# --- diagnostics -----------------------------------------------------------


@pytest.mark.asyncio
async def test_diag_vector_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, factual_store: FactualStore
) -> None:
    identity = _identity()
    vector_dir = tmp_path / "vectors"
    store = VectorStore(base_dir=vector_dir, model_name="sandbox-without-model")
    client_key = str((vector_dir / "_qdrant_db").absolute())
    try:
        name = store.get_collection_name(identity)
        store.client.create_collection(
            collection_name=name, vectors_config=VectorParams(size=3, distance=Distance.COSINE)
        )
        store.client.upsert(
            collection_name=name,
            points=[
                PointStruct(id=index, vector=[1.0, 0.5, float(index)], payload={})
                for index in (1, 2)
            ],
        )
        monkeypatch.setattr(MemoryManager, "_vector_store_singleton", store)
        monkeypatch.setattr(memory_gateway, "_global_manager", MemoryManager())

        result = await memory_gateway.memory_tools(action="diagnostics", identity=identity)
    finally:
        VectorStore._client_cache.pop(client_key).close()

    assert "(Vector Count): 2" in result
    assert "Check Failed" not in result


@pytest.mark.asyncio
async def test_diag_lists_unmigrated_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manager: MemoryManager
) -> None:
    identity = _identity()
    point_zone_roots(monkeypatch, tmp_path, tmp_path / "zone_state")
    _plant_jsonl(tmp_path / "vault", identity.agent_id)

    result = await memory_gateway.memory_tools(action="diagnostics", identity=identity)

    assert "facts.jsonl" in result


@pytest.mark.asyncio
async def test_diag_without_unmigrated_jsonl_has_no_legacy_facts_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manager: MemoryManager
) -> None:
    identity = _identity()
    point_zone_roots(monkeypatch, tmp_path, tmp_path / "zone_state")
    _plant_jsonl(tmp_path / "vault", identity.agent_id, "facts.jsonl.bak")

    result = await memory_gateway.memory_tools(action="diagnostics", identity=identity)

    assert "facts.jsonl" not in result


# --- duplicate scan --------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_dups_scan_real_manager(
    manager: MemoryManager, factual_store: FactualStore, vector_stub: Any
) -> None:
    identity = _identity()
    first = factual_store.save_fact(identity, "I like green tea")["id"]
    second = factual_store.save_fact(identity, "I really like green tea")["id"]
    factual_store.save_fact(identity, "The meeting is on Friday")

    def similar(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if "green tea" not in kwargs["query"]:
            return []
        other = second if kwargs["query"] == "I like green tea" else first
        return [
            {"fact_id": other, "content": "near duplicate", "_score": 0.97},
            {"fact_id": "unrelated", "content": "far away", "_score": 0.40},
        ]

    vector_stub.query_vectors.side_effect = similar

    result = await memory_gateway.memory_tools(action="manage_dups", identity=identity)

    assert "Detected 1 Potential Duplicate Pair" in result
    assert first in result and second in result
    assert "unrelated" not in result
    for call in vector_stub.query_vectors.call_args_list:
        assert call.kwargs["identity"] is identity


@pytest.mark.asyncio
async def test_manage_dups_scan_is_capped(
    manager: MemoryManager,
    factual_store: FactualStore,
    vector_stub: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = manager_module.MAX_DUPLICATE_SCAN_FACTS
    assert isinstance(cap, int) and cap > 0
    monkeypatch.setattr(manager_module, "MAX_DUPLICATE_SCAN_FACTS", 2)
    identity = _identity()
    for index in range(5):
        factual_store.save_fact(identity, f"distinct fact {index}")

    result = await memory_gateway.memory_tools(action="manage_dups", identity=identity)

    assert "No significant duplicates" in result
    assert vector_stub.query_vectors.call_count == 2


# --- retired JSONL import --------------------------------------------------


def test_jsonl_present_is_left_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "vault"
    jsonl = _plant_jsonl(vault, "LegacyAgent")
    payload = jsonl.read_bytes()
    mtime = jsonl.stat().st_mtime_ns
    reads: list[str] = []
    original_read = resource_manager.read_file_text

    def recording_read(path: Any, *args: Any, **kwargs: Any) -> str:
        reads.append(str(path))
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(resource_manager, "read_file_text", recording_read)

    store = FactualStore(base_dir=vault)

    assert str(jsonl) not in reads
    assert jsonl.read_bytes() == payload
    assert jsonl.stat().st_mtime_ns == mtime
    assert not jsonl.with_name("facts.jsonl.bak").exists()
    assert store.query_all_facts(include_archived=True) == []


def test_find_unmigrated_jsonl_agents_only_counts_unrenamed(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _plant_jsonl(vault, "Alice")
    _plant_jsonl(vault, "Bob", "facts.jsonl.bak")
    nested = vault / "Carol" / "nested" / "fact" / "facts.jsonl"
    nested.parent.mkdir(parents=True)
    nested.write_text("{}\n", encoding="utf-8")

    assert factual_module.find_unmigrated_jsonl_agents(vault) == ["Alice"]
    assert factual_module.find_unmigrated_jsonl_agents(tmp_path / "missing") == []


# --- every action against the real manager ---------------------------------


@pytest.mark.asyncio
async def test_real_manager_runs_every_memory_action(
    manager: MemoryManager, factual_store: FactualStore, vector_stub: Any
) -> None:
    identity = _identity()
    saved = await memory_gateway.memory_tools(
        action="save", identity=identity, content="sandbox fact one"
    )
    fact_id = saved.split("ID: ", 1)[1].rstrip(").")

    async def run(action: str, **arguments: Any) -> Any:
        return await memory_gateway.memory_tools(action=action, identity=identity, **arguments)

    legacy_dir = Path(resource_manager.get_vault_path("legacy", identity))
    assert not legacy_dir.exists()
    results = {
        "save": saved,
        "conversations": await run("conversations", source="legacy"),
        "query": await run("query", query="sandbox"),
        "random": await run("random"),
        "diagnostics": await run("diagnostics"),
        "manage_dups": await run("manage_dups"),
        "reindex": await run("reindex"),
        "export_facts": await run("export_facts"),
        "forget": await run("forget", fact_id_to_archive=fact_id),
    }

    assert set(results) == set(memory_gateway.MEMORY_ACTIONS)
    for action, text in results.items():
        assert isinstance(text, str), action
        for marker in ("Error", "Check Failed", "❌"):
            assert marker not in text, (action, text)
    assert "sandbox fact one" in results["random"]
    assert json.loads(results["export_facts"])["count"] == 1
    assert "共 0 則" in results["conversations"]
    assert "沒有符合的舊對話" in results["conversations"]
    assert not legacy_dir.exists()


# --- gateway and registry --------------------------------------------------


@pytest.mark.asyncio
async def test_memory_gateway_errors_are_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    async def explode(**_kwargs: Any) -> str:
        raise _SyntheticStoreFailure

    monkeypatch.setattr(proxy_memory_gateway, "external_memory_tools", explode)
    failed = await proxy_memory_gateway.memory_tools(action="query", identity=_identity())
    assert json.loads(failed)["error_class"] == "internal_error"

    monkeypatch.setattr(proxy_memory_gateway, "external_memory_tools", None)
    missing = await proxy_memory_gateway.memory_tools(action="query", identity=_identity())
    assert json.loads(missing)["error_class"] == "internal_error"


@pytest.mark.asyncio
async def test_gdpr_export_treats_structured_memory_error_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def explode(**_kwargs: Any) -> str:
        raise _SyntheticStoreFailure

    monkeypatch.setattr(proxy_memory_gateway, "external_memory_tools", explode)

    assert await gdpr_export_service._fetch_agent_facts(_identity()) is None


def test_direct_memory_tool_names_removed() -> None:
    assert "random_recall" not in memory_gateway.memory_tool_registry
    assert "memory_diagnostics" not in memory_gateway.memory_tool_registry
    assert {"random", "diagnostics"} <= set(memory_gateway.MEMORY_ACTIONS)
