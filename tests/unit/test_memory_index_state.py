"""The fact store is the record; the semantic index can fail and be repaired by hand.

Every store is sandboxed: a tmp FactualStore, and either an autospec vector stub or a local
Qdrant directory in tmp with a deterministic stand-in encoder. No embedding model is loaded.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, create_autospec

import pytest
from memory_server import config_utils
from memory_server import factual as fm
from memory_server import tools as memory_gateway
from memory_server import vector as vector_module
from memory_server.services.manager import MemoryManager
from memory_server.services.reembed import reembed_agent
from memory_server.tools.contracts import MEMORY_ACTION_CONTRACTS
from memory_server.vector import VectorStore
from pydantic import ValidationError
from qdrant_client import QdrantClient
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext
from src.models.safety import AuthPoliciesConfig
from src.tools.builtin import memory as proxy_memory_gateway

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

sqlite = fm.sqlite3

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_EXAMPLE = PROJECT_ROOT / "ToukaProxy" / "config" / "auth_policies.json.example"
POLICY_TRACKED = PROJECT_ROOT / "ToukaProxy" / "config" / "auth_policies.json"
SMOKE_FACT_ID = re.compile(r"ID:\s*([^)]+)")  # same parser as tests/smoke/smoke_memory_tools.py
INDEX_LATER = "語意索引稍後補上"
SEMANTIC_DOWN = "語意搜尋暫時無法使用"  # the query note (lifecycle.SEMANTIC_DOWN_NOTE)
DEDUP_SKIPPED = "語意查重暫時無法使用"  # the save / supersede note
OLD_CREATE = (
    "CREATE TABLE facts (id TEXT PRIMARY KEY, agent_id TEXT, content TEXT, tags TEXT,"
    " importance TEXT DEFAULT NULL, emotions TEXT DEFAULT NULL, created_at INTEGER,"
    " updated_at INTEGER, archived INTEGER DEFAULT 0)"
)
OLD_INSERT = (
    "INSERT INTO facts (id, agent_id, content, tags, created_at, updated_at, archived)"
    " VALUES (?, ?, ?, '[]', 1700000000, 1700000000, 0)"
)


class _Embedding(list[float]):
    def tolist(self) -> list[float]:
        return list(self)


class _SandboxEncoder:
    """Deterministic local stand-in for the embedding model; never downloads anything."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def get_sentence_embedding_dimension(self) -> int:
        return 32

    def encode(self, text: str) -> _Embedding:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return _Embedding((byte - 127.5) / 127.5 for byte in digest)


class _SyntheticVectorFailure(Exception):
    """A vector store failure raised by the doubles below."""


def _identity(prefix: str = "IndexProbe") -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=f"{prefix}{uuid.uuid4().hex[:8]}", role="Owner")


def _fact_id(reply: str) -> str:
    match = SMOKE_FACT_ID.search(reply)
    assert match is not None, reply
    return match.group(1)


def _row(store: fm.FactualStore, fact_id: str) -> dict[str, Any]:
    conn = sqlite.connect(store.db_path)
    conn.row_factory = sqlite.Row
    try:
        row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None, fact_id
    return dict(row)


def _state(store: fm.FactualStore, fact_id: str) -> str:
    return _row(store, fact_id)["embedding_state"]


def _write(db: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = sqlite.connect(db)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _user_version(db: Path) -> int:
    conn = sqlite.connect(db)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _point(vector: VectorStore, identity: AgentIdentity, fact_id: str) -> dict[str, Any] | None:
    name = vector.get_collection_name(identity)
    if not vector.client.collection_exists(name):
        return None
    records = vector.client.retrieve(
        collection_name=name, ids=[str(uuid.uuid5(uuid.NAMESPACE_OID, fact_id))]
    )
    return records[0].payload if records else None


def _make_v0_vault(vault: Path, agent: str, contents: list[str]) -> Path:
    db = vault / "core_memory.db"
    vault.mkdir(parents=True, exist_ok=True)
    conn = sqlite.connect(db)
    try:
        conn.execute(OLD_CREATE)
        for index, content in enumerate(contents):
            conn.execute(OLD_INSERT, (f"fact_old{index}", agent, content))
        conn.commit()
    finally:
        conn.close()
    return db


def _no_space(monkeypatch: pytest.MonkeyPatch) -> None:
    full_disk = SimpleNamespace(total=10**12, used=10**12, free=0)
    monkeypatch.setattr(fm.shutil, "disk_usage", lambda _path: full_disk)


def _vector_stub(monkeypatch: pytest.MonkeyPatch) -> Any:
    stub = create_autospec(VectorStore, instance=True)
    stub.query_vectors.return_value = []
    stub.save_vector.return_value = True
    stub.agent_point_count.return_value = 0
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", stub)
    return stub


def _vector_unavailable(_self: MemoryManager) -> VectorStore:
    raise _SyntheticVectorFailure


async def _memory(action: str, identity: AgentIdentity, **arguments: Any) -> str:
    return await memory_gateway.memory_tools(action=action, identity=identity, **arguments)


def _spy_embeddings(
    monkeypatch: pytest.MonkeyPatch, vector: VectorStore, fail_on: str | None = None
) -> list[str]:
    seen: list[str] = []
    original: Callable[[str], list[float]] = vector.get_embedding

    def spy(text: str) -> list[float]:
        seen.append(text)
        if text == fail_on:
            raise _SyntheticVectorFailure
        return original(text)

    monkeypatch.setattr(vector, "get_embedding", spy)
    return seen


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> MemoryManager:
    real = MemoryManager()
    monkeypatch.setattr(memory_gateway, "_global_manager", real)
    return real


@pytest.fixture
def store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manager: MemoryManager
) -> fm.FactualStore:
    factual = fm.FactualStore(base_dir=tmp_path / "vault")
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", factual)
    return factual


@pytest.fixture
def vector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manager: MemoryManager
) -> Iterator[VectorStore]:
    monkeypatch.setattr(vector_module, "SentenceTransformer", _SandboxEncoder)
    directory = tmp_path / "vectors"
    real = VectorStore(base_dir=directory, model_name="sandbox-encoder")
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", real)
    yield real
    VectorStore._client_cache.pop(str((directory / "_qdrant_db").absolute())).close()


# --- saving: the fact survives an index failure ------------------------------


@pytest.mark.asyncio
async def test_fact_survives_vector_failure(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _vector_stub(monkeypatch)
    stub.save_vector.return_value = False
    identity = _identity()

    reply = await _memory("save", identity, content="tea at noon")

    fact_id = _fact_id(reply)
    assert INDEX_LATER in reply
    assert [fact["id"] for fact in store.query_facts(identity)] == [fact_id]
    assert _state(store, fact_id) == "failed"


@pytest.mark.asyncio
async def test_fact_survives_ensure_collection_failure(
    store: fm.FactualStore, vector: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise _SyntheticVectorFailure

    monkeypatch.setattr(vector.client, "create_collection", refuse)
    identity = _identity()

    reply = await _memory("save", identity, content="the vault key is blue")

    fact_id = _fact_id(reply)
    assert INDEX_LATER in reply
    assert [fact["id"] for fact in store.query_facts(identity)] == [fact_id]
    assert _state(store, fact_id) == "failed"
    assert _point(vector, identity, fact_id) is None


@pytest.mark.asyncio
async def test_fact_survives_when_the_vector_store_cannot_open(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MemoryManager, "get_vector_store", _vector_unavailable)
    identity = _identity()

    reply = await _memory("save", identity, content="the meeting moved to friday")

    fact_id = _fact_id(reply)
    assert INDEX_LATER in reply
    assert DEDUP_SKIPPED in reply  # the semantic duplicate check could not run either
    assert _state(store, fact_id) == "failed"


@pytest.mark.asyncio
async def test_indexed_fact_is_marked_ready_in_seconds(
    store: fm.FactualStore, vector: VectorStore
) -> None:
    identity = _identity()

    reply = await _memory("save", identity, content="sunflowers on the balcony")

    fact_id = _fact_id(reply)
    assert INDEX_LATER not in reply
    row = _row(store, fact_id)
    assert row["embedding_state"] == "ready"
    assert row["embedding_model"] == config_utils.get_embedding_model_name()
    now = int(config_utils.get_current_epoch())
    assert now - 120 <= row["embedding_updated_at"] <= now + 1
    payload = _point(vector, identity, fact_id)
    assert payload is not None
    assert payload["created_at"] == row["created_at"]


# --- archive and semantic results: SQLite wins ------------------------------


@pytest.mark.asyncio
async def test_archive_sqlite_wins(
    store: fm.FactualStore, vector: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    fact_id = _fact_id(await _memory("save", identity, content="the old address"))

    def broken(*_args: Any, **_kwargs: Any) -> bool:
        raise _SyntheticVectorFailure

    monkeypatch.setattr(vector, "archive_vector_by_fact_id", broken)

    archived = await _memory("forget", identity, fact_id_to_archive=fact_id)
    found = await _memory("query", identity, query="the old address")

    assert "archived" in archived
    assert _row(store, fact_id)["archived"] == 1
    assert _point(vector, identity, fact_id)["archived"] is False  # the index is stale
    assert "the old address" not in found


@pytest.mark.asyncio
async def test_semantic_results_rechecked_against_sqlite(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, other = _identity(), _identity("Other")
    live = store.save_fact(identity, "live fact text")["id"]
    archived = store.save_fact(identity, "archived fact text")["id"]
    store.archive_fact(identity, archived)
    foreign = store.save_fact(other, "foreign fact text")["id"]
    stub = _vector_stub(monkeypatch)
    stub.query_vectors.return_value = [
        {"fact_id": live, "content": "stale payload text", "created_at": 1, "_score": 0.9},
        {"fact_id": archived, "content": "archived fact text", "created_at": 1, "_score": 0.8},
        {"fact_id": "fact_missing", "content": "missing fact text", "created_at": 1, "_score": 0.7},
        {"fact_id": foreign, "content": "foreign fact text", "created_at": 1, "_score": 0.6},
    ]

    found = await _memory("query", identity, query="zzz unmatched words", limit=10)

    assert f"[ID: {live}]" in found
    assert "live fact text" in found
    for leaked in ("stale payload", "archived fact", "missing fact", "foreign fact"):
        assert leaked not in found
    for leaked_id in (archived, "fact_missing", foreign):
        assert leaked_id not in found


# --- duplicate checks --------------------------------------------------------


@pytest.mark.asyncio
async def test_save_dedupe_ignores_archived_or_missing_semantic_hits(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    archived = store.save_fact(identity, "an old preference")["id"]
    store.archive_fact(identity, archived)
    stub = _vector_stub(monkeypatch)
    stub.query_vectors.return_value = [
        {"fact_id": archived, "content": "an old preference", "_score": 0.99},
        {"fact_id": "fact_missing", "content": "gone", "_score": 0.98},
    ]

    reply = await _memory("save", identity, content="a new preference")

    assert "重複" not in reply
    assert _fact_id(reply) not in {archived, "fact_missing"}
    assert len(store.query_facts(identity)) == 1


@pytest.mark.asyncio
async def test_save_dedupe_still_blocks_a_live_semantic_hit(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    live = store.save_fact(identity, "I prefer window seats")["id"]
    stub = _vector_stub(monkeypatch)
    stub.query_vectors.return_value = [
        {"fact_id": live, "content": "I prefer window seats", "_score": 0.97}
    ]

    reply = await _memory("save", identity, content="I like window seats")

    assert "重複" in reply
    assert f"(ID: {live})" in reply
    assert len(store.query_facts(identity)) == 1


@pytest.mark.asyncio
async def test_exact_duplicate_is_caught_by_sqlite_without_the_vector_store(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MemoryManager, "get_vector_store", _vector_unavailable)
    identity, other = _identity(), _identity("Other")
    first = _fact_id(await _memory("save", identity, content="lunch is at one"))

    again = await _memory("save", identity, content="  lunch is at one  ")

    assert "重複" in again
    assert f"(ID: {first})" in again
    assert len(store.query_facts(identity, include_archived=True)) == 1

    store.archive_fact(identity, first)
    after_archive = _fact_id(await _memory("save", identity, content="lunch is at one"))
    someone_else = await _memory("save", other, content="lunch is at one")

    assert after_archive != first
    assert "重複" not in someone_else


@pytest.mark.asyncio
async def test_duplicate_scan_skips_archived_candidates(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    store.save_fact(identity, "I like green tea")
    archived = store.save_fact(identity, "I really like green tea")["id"]
    store.archive_fact(identity, archived)
    stub = _vector_stub(monkeypatch)

    def similar(**kwargs: Any) -> list[dict[str, Any]]:
        if kwargs["query"] != "I like green tea":
            return []
        return [{"fact_id": archived, "content": "I really like green tea", "_score": 0.97}]

    stub.query_vectors.side_effect = similar

    result = await _memory("manage_dups", identity)

    assert "No significant duplicates" in result
    assert archived not in result


# --- semantic search down: said, never passed off as "nothing found" ---------


def _break_semantic_search(monkeypatch: pytest.MonkeyPatch, vector: VectorStore) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise _SyntheticVectorFailure

    monkeypatch.setattr(vector.client, "query_points", broken)


@pytest.mark.asyncio
async def test_query_vectors_raises_when_the_search_fails(
    store: fm.FactualStore, vector: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    await _memory("save", identity, content="the spare key is under the mat")
    _break_semantic_search(monkeypatch, vector)

    with pytest.raises(vector_module.SemanticSearchUnavailableError) as raised:
        vector.query_vectors(identity, "where is the spare key")

    assert isinstance(raised.value.__cause__, _SyntheticVectorFailure)


def test_query_vectors_raises_when_the_index_cannot_be_read(
    vector: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> bool:
        raise _SyntheticVectorFailure

    monkeypatch.setattr(vector.client, "collection_exists", broken)

    with pytest.raises(vector_module.SemanticSearchUnavailableError):
        vector.query_vectors(_identity(), "anything at all")


def test_query_vectors_without_a_collection_is_still_empty(vector: VectorStore) -> None:
    assert vector.query_vectors(_identity(), "nothing was saved yet") == []


@pytest.mark.asyncio
async def test_query_says_so_when_the_semantic_search_fails(
    store: fm.FactualStore, vector: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    await _memory("save", identity, content="the spare key is under the mat")
    _break_semantic_search(monkeypatch, vector)

    found = await _memory("query", identity, query="where is the spare key")

    assert SEMANTIC_DOWN in found


@pytest.mark.asyncio
async def test_save_keeps_the_fact_and_says_the_semantic_check_was_skipped(
    store: fm.FactualStore, vector: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    first = await _memory("save", identity, content="the spare key is under the mat")
    _break_semantic_search(monkeypatch, vector)

    reply = await _memory("save", identity, content="the spare key moved to the drawer")

    fact_id = _fact_id(reply)
    assert DEDUP_SKIPPED not in first
    assert reply.startswith("Memory successfully saved (ID: ")
    assert DEDUP_SKIPPED in reply
    assert _state(store, fact_id) == "ready"  # only the search broke; indexing still works


@pytest.mark.asyncio
async def test_supersede_says_the_semantic_check_was_skipped(
    store: fm.FactualStore, vector: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    old = _fact_id(await _memory("save", identity, content="I live in Taipei"))
    _break_semantic_search(monkeypatch, vector)

    reply = await _memory(
        "manage_dups", identity, operation="supersede", target_id=old, content="I moved to Tainan"
    )

    assert "已用新記憶取代" in reply
    assert DEDUP_SKIPPED in reply


@pytest.mark.asyncio
async def test_duplicate_scan_reports_a_broken_semantic_search(
    store: fm.FactualStore, vector: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    await _memory("save", identity, content="I like green tea")
    _break_semantic_search(monkeypatch, vector)

    result = await proxy_memory_gateway.memory_tools(action="manage_dups", identity=identity)

    assert json.loads(result)["error_class"] == "upstream_error"
    assert "No significant duplicates" not in result


@pytest.mark.asyncio
async def test_duplicate_scan_reports_an_unavailable_vector_store(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    store.save_fact(identity, "I like green tea")
    monkeypatch.setattr(MemoryManager, "get_vector_store", _vector_unavailable)

    result = await proxy_memory_gateway.memory_tools(action="manage_dups", identity=identity)

    assert json.loads(result)["error_class"] == "upstream_error"


@pytest.mark.asyncio
async def test_duplicate_scan_with_nothing_saved_needs_no_vector_store(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MemoryManager, "get_vector_store", _vector_unavailable)

    result = await proxy_memory_gateway.memory_tools(action="manage_dups", identity=_identity())

    assert "No significant duplicates" in result


# --- forget: what happened, said as it is -----------------------------------


def _clock(monkeypatch: pytest.MonkeyPatch, epoch: int) -> None:
    monkeypatch.setattr(fm, "get_current_epoch", lambda: epoch)


def test_archive_fact_reports_what_happened(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, other = _identity(), _identity("Other")
    live = store.save_fact(identity, "a live memory")["id"]

    assert store.archive_fact(other, live) is fm.ArchiveResult.NOT_FOUND
    _clock(monkeypatch, 3_000_000_000)
    assert store.archive_fact(identity, live) is fm.ArchiveResult.ARCHIVED
    _clock(monkeypatch, 3_000_000_100)
    assert store.archive_fact(identity, live) is fm.ArchiveResult.ALREADY_ARCHIVED
    assert store.archive_fact(identity, "fact_missing") is fm.ArchiveResult.NOT_FOUND
    assert _row(store, live)["archived"] == 1
    assert _row(store, live)["updated_at"] == 3_000_000_000  # nothing changed the second time


def test_archive_fact_takes_a_superseded_version_out_of_the_history(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    old = store.save_fact(identity, "I live in Taipei")["id"]
    store.supersede_fact(identity, old, "I moved to Tainan")
    _clock(monkeypatch, 3_000_000_000)

    assert store.archive_fact(identity, old) is fm.ArchiveResult.DETACHED
    assert _row(store, old)["superseded_by"] is None
    assert _row(store, old)["updated_at"] == 3_000_000_000
    _clock(monkeypatch, 3_000_000_100)
    assert store.archive_fact(identity, old) is fm.ArchiveResult.ALREADY_ARCHIVED
    assert _row(store, old)["updated_at"] == 3_000_000_000


@pytest.mark.asyncio
async def test_forgetting_twice_is_a_success_that_changes_nothing(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _vector_stub(monkeypatch)
    stub.archive_vector_by_fact_id.return_value = True
    identity = _identity()
    fact_id = store.save_fact(identity, "a memory to forget")["id"]

    first = await _memory("forget", identity, fact_id_to_archive=fact_id)
    second = await _memory("forget", identity, fact_id_to_archive=fact_id)

    assert first == f"✅ Memory {fact_id} has been archived."
    assert "already archived" in second
    assert not second.startswith("❌")
    assert stub.archive_vector_by_fact_id.call_count == 1


@pytest.mark.asyncio
async def test_forget_reports_a_database_error_instead_of_not_found(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vector_stub(monkeypatch)
    identity = _identity()
    fact_id = store.save_fact(identity, "a memory")["id"]
    _write(
        store.db_path,
        "CREATE TRIGGER broken_disk BEFORE UPDATE OF archived ON facts"
        " BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END",
    )

    reply = await proxy_memory_gateway.memory_tools(
        action="forget", identity=identity, fact_id_to_archive=fact_id
    )

    assert json.loads(reply)["error_class"] == "internal_error"
    assert "not found" not in reply
    assert "disk I/O" not in reply
    assert _row(store, fact_id)["archived"] == 0


# --- manual repair: reindex --------------------------------------------------


@pytest.mark.asyncio
async def test_reindex_missing_repairs(
    store: fm.FactualStore, vector: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    indexed = store.save_fact(identity, "already in the index")["id"]
    assert vector.save_vector(identity, indexed, "already in the index")
    failed = store.save_fact(identity, "write failed earlier")["id"]
    missing = store.save_fact(identity, "never reached the index")["id"]
    _write(store.db_path, "UPDATE facts SET embedding_state = 'failed' WHERE id = ?", (failed,))
    for fact_id in (failed, missing):
        _write(store.db_path, "UPDATE facts SET created_at = 1600000000 WHERE id = ?", (fact_id,))
    embedded = _spy_embeddings(monkeypatch, vector)

    reply = await _memory("reindex", identity)

    assert {fact_id: _state(store, fact_id) for fact_id in (indexed, failed, missing)} == {
        indexed: "ready",
        failed: "ready",
        missing: "ready",
    }
    assert sorted(embedded) == ["never reached the index", "write failed earlier"]
    for fact_id in (failed, missing):
        assert _point(vector, identity, fact_id)["created_at"] == 1600000000
    assert "3 fact(s)" in reply
    assert "1 already indexed" in reply
    assert "2 re-embedded" in reply
    assert "0 still waiting" in reply


@pytest.mark.asyncio
async def test_reindex_only_touches_the_callers_facts(
    store: fm.FactualStore, vector: VectorStore
) -> None:
    identity, other = _identity(), _identity("Other")
    mine = store.save_fact(identity, "my pending fact")["id"]
    theirs = store.save_fact(other, "their pending fact")["id"]
    _write(store.db_path, "UPDATE facts SET embedding_state = 'failed' WHERE id = ?", (theirs,))

    await _memory("reindex", identity)
    store.set_embedding_state(identity, [theirs], "ready", "sandbox-encoder")

    assert _state(store, mine) == "ready"
    assert _state(store, theirs) == "failed"
    assert _point(vector, identity, theirs) is None
    assert _point(vector, other, theirs) is None


@pytest.mark.asyncio
async def test_reindex_isolates_one_bad_fact(
    store: fm.FactualStore, vector: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    good = store.save_fact(identity, "a fact that embeds")["id"]
    bad = store.save_fact(identity, "a fact whose embedding breaks")["id"]
    _spy_embeddings(monkeypatch, vector, fail_on="a fact whose embedding breaks")

    reply = await _memory("reindex", identity)

    assert _state(store, good) == "ready"
    assert _state(store, bad) == "failed"
    assert "1 re-embedded" in reply
    assert "1 could not be indexed" in reply
    assert "1 still waiting" in reply


@pytest.mark.asyncio
async def test_reindex_is_capped(
    store: fm.FactualStore, vector: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("memory_server.services.index_state.MAX_REINDEX_FACTS", 2)
    identity = _identity()
    ids = [store.save_fact(identity, f"capped fact number {index}")["id"] for index in range(3)]

    reply = await _memory("reindex", identity)

    assert sorted(_state(store, fact_id) for fact_id in ids) == ["ready", "ready", "unverified"]
    assert "2 fact(s)" in reply
    assert "1 still waiting" in reply


@pytest.mark.asyncio
async def test_unverified_backfill_on_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    vector: VectorStore,
) -> None:
    identity = _identity("Upgraded")
    contents = ["remembered before the upgrade", "also remembered before"]
    db = _make_v0_vault(tmp_path / "old_vault", identity.agent_id, contents)
    upgraded = fm.FactualStore(base_dir=tmp_path / "old_vault")
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", upgraded)
    for index, content in enumerate(contents):
        assert vector.save_vector(identity, f"fact_old{index}", content)  # indexed back then
    embedded = _spy_embeddings(monkeypatch, vector)

    assert _user_version(db) == fm._target_version()
    assert {_state(upgraded, f"fact_old{index}") for index in range(2)} == {"unverified"}
    diagnostics = await _memory("diagnostics", identity)
    assert "unverified 2" in diagnostics
    assert "failed" not in diagnostics

    reply = await _memory("reindex", identity)

    assert {_state(upgraded, f"fact_old{index}") for index in range(2)} == {"ready"}
    assert embedded == []  # confirmed in the vector store, not recomputed
    assert "2 already indexed" in reply


@pytest.mark.asyncio
async def test_reindex_refuses_a_store_that_was_not_upgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manager: MemoryManager
) -> None:
    identity = _identity("Outdated")
    db = _make_v0_vault(tmp_path / "old_vault", identity.agent_id, ["kept at v0"])
    _no_space(monkeypatch)
    stuck = fm.FactualStore(base_dir=tmp_path / "old_vault")
    assert stuck.current_schema_status() is fm.FactsSchemaStatus.NO_SPACE
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", stuck)
    stub = _vector_stub(monkeypatch)

    result = await proxy_memory_gateway.memory_tools(action="reindex", identity=identity)

    assert json.loads(result)["error_class"] == "internal_error"
    assert _user_version(db) == 0
    stub.save_vector.assert_not_called()
    with pytest.raises(fm.FactsSchemaNotReadyError):
        stuck.list_pending_index(identity, 10)
    with pytest.raises(fm.FactsSchemaNotReadyError):
        stuck.set_embedding_state(identity, ["fact_old0"], "ready", "sandbox-encoder")


# --- diagnostics: counts only ------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostics_shows_index_state_counts(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    states = {
        "ready one": "ready",
        "ready two": "ready",
        "failed one": "failed",
        "unverified one": "unverified",
    }
    for content, state in states.items():
        fact_id = store.save_fact(identity, content)["id"]
        _write(store.db_path, "UPDATE facts SET embedding_state = ? WHERE id = ?", (state, fact_id))
    archived = store.save_fact(identity, "archived and failed")["id"]
    _write(store.db_path, "UPDATE facts SET embedding_state = 'failed' WHERE id = ?", (archived,))
    store.archive_fact(identity, archived)
    stub = _vector_stub(monkeypatch)

    result = await _memory("diagnostics", identity)

    assert "ready 2" in result
    assert "failed 1" in result
    assert "unverified 1" in result
    assert "reindex" in result
    assert [call[0] for call in stub.method_calls] == ["agent_point_count"]  # no spot checks


@pytest.mark.asyncio
async def test_diagnostics_counts_a_store_left_at_v0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manager: MemoryManager
) -> None:
    identity = _identity("Outdated")
    _make_v0_vault(tmp_path / "old_vault", identity.agent_id, ["kept at v0"])
    _no_space(monkeypatch)
    stuck = fm.FactualStore(base_dir=tmp_path / "old_vault")
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", stuck)
    _vector_stub(monkeypatch)

    result = await _memory("diagnostics", identity)

    assert "unverified 1" in result


# --- the new action: contract, risk and the offline rebuild ----------------------


def test_reindex_contract_takes_no_target() -> None:
    model = MEMORY_ACTION_CONTRACTS["reindex"]

    assert set(model.model_fields) == {"action"}
    with pytest.raises(ValidationError):
        model.model_validate({"action": "reindex", "agent_id": "SomeoneElse"})


def test_reindex_left_out_of_a_private_policy_is_dangerous() -> None:
    text = POLICY_EXAMPLE.read_text(encoding="utf-8")
    example = AuthPoliciesConfig.model_validate_json(text)
    raw = json.loads(text)
    raw["tool_risk"]["tools"]["memory_tools"]["actions"].pop("reindex", None)
    private = AuthPoliciesConfig.model_validate(raw)
    identity = _identity()

    def effect(policy: AuthPoliciesConfig, level: str) -> str:
        context = PolicyContext(
            actor=identity,
            resource="memory_tools",
            action="tool_call",
            environment={"auth_level": level, "tool_action": "reindex"},
        )
        return policy.evaluate(context).effect

    assert example.tool_risk.class_for("memory_tools", "reindex") == "safe"
    assert effect(example, "Auto") == "ALLOW"
    assert private.tool_risk.class_for("memory_tools", "reindex") == "dangerous"
    assert effect(private, "Manual") == "CONFIRM"
    assert effect(private, "Auto") == "REVIEW"


def test_tracked_policy_file_marks_reindex_safe() -> None:
    # Reads only the one classification; nothing else from the file is shown on failure.
    actions = json.loads(POLICY_TRACKED.read_text(encoding="utf-8"))["tool_risk"]["tools"][
        "memory_tools"
    ]["actions"]
    reindex_risk = actions.get("reindex")

    assert reindex_risk == "safe", "memory_tools.reindex is not classified as safe"


def test_reembed_leaves_embedding_state_alone(store: fm.FactualStore, tmp_path: Path) -> None:
    identity = _identity()
    fact_id = store.save_fact(identity, "rebuilt offline")["id"]
    _write(store.db_path, "UPDATE facts SET embedding_state = 'failed' WHERE id = ?", (fact_id,))
    client = QdrantClient(path=str(tmp_path / "offline_qdrant"))
    offline = MagicMock()
    offline.client = client
    offline.model_name = "sandbox-encoder"
    offline.model.get_sentence_embedding_dimension.return_value = 4
    offline.get_embedding.return_value = [0.1, 0.2, 0.3, 0.4]
    offline.get_collection_name.return_value = "memory_offline"
    try:
        assert reembed_agent(store, offline, identity) == 1
    finally:
        client.close()

    assert _state(store, fact_id) == "failed"


# --- archived facts whose index payload was left behind -------------------------------


@pytest.mark.asyncio
async def test_reindex_syncs_archived_payloads_left_behind(
    store: fm.FactualStore, vector: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    fact_id = _fact_id(await _memory("save", identity, content="the old office address"))
    working = vector.archive_vector_by_fact_id

    def broken(*_args: Any, **_kwargs: Any) -> bool:
        raise _SyntheticVectorFailure

    monkeypatch.setattr(vector, "archive_vector_by_fact_id", broken)
    await _memory("forget", identity, fact_id_to_archive=fact_id)
    assert _state(store, fact_id) == "failed"
    assert _point(vector, identity, fact_id)["archived"] is False
    monkeypatch.setattr(vector, "archive_vector_by_fact_id", working)

    reply = await _memory("reindex", identity)

    assert _state(store, fact_id) == "ready"
    assert _point(vector, identity, fact_id)["archived"] is True
    assert "1 archived" in reply
    assert "0 still waiting" in reply  # the active part of the report is unchanged


@pytest.mark.asyncio
async def test_archive_that_returns_false_is_also_marked(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _vector_stub(monkeypatch)
    stub.archive_vector_by_fact_id.return_value = False
    identity = _identity()
    fact_id = _fact_id(await _memory("save", identity, content="a quietly failing archive"))

    await _memory("forget", identity, fact_id_to_archive=fact_id)

    assert _state(store, fact_id) == "failed"


@pytest.mark.asyncio
async def test_archive_without_a_vector_store_is_marked(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vector_stub(monkeypatch)
    identity = _identity()
    fact_id = _fact_id(await _memory("save", identity, content="archived while index is down"))
    monkeypatch.setattr(MemoryManager, "get_vector_store", _vector_unavailable)

    reply = await _memory("forget", identity, fact_id_to_archive=fact_id)

    assert "archived" in reply
    assert _state(store, fact_id) == "failed"


@pytest.mark.asyncio
async def test_reindex_marks_archived_facts_without_a_vector_ready_without_writing(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _vector_stub(monkeypatch)
    stub.indexed_fact_ids.return_value = set()
    identity = _identity()
    fact_id = store.save_fact(identity, "never reached the index")["id"]
    store.archive_fact(identity, fact_id)
    _write(store.db_path, "UPDATE facts SET embedding_state = 'failed' WHERE id = ?", (fact_id,))

    await _memory("reindex", identity)

    assert _state(store, fact_id) == "ready"
    stub.archive_vector_by_fact_id.assert_not_called()


@pytest.mark.asyncio
async def test_reindex_keeps_an_archived_fact_failed_when_the_sync_fails(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _vector_stub(monkeypatch)
    identity = _identity()
    fact_id = store.save_fact(identity, "the index refuses the archive")["id"]
    store.archive_fact(identity, fact_id)
    _write(store.db_path, "UPDATE facts SET embedding_state = 'failed' WHERE id = ?", (fact_id,))
    stub.indexed_fact_ids.return_value = {fact_id}
    stub.archive_vector_by_fact_id.return_value = False

    reply = await _memory("reindex", identity)

    assert _state(store, fact_id) == "failed"
    assert "1 could not be synced" in reply


@pytest.mark.asyncio
async def test_reindex_archive_sync_only_touches_the_callers_facts(
    store: fm.FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _vector_stub(monkeypatch)
    stub.indexed_fact_ids.return_value = set()
    identity, other = _identity(), _identity("Other")
    theirs = store.save_fact(other, "their archived fact")["id"]
    store.archive_fact(other, theirs)
    _write(store.db_path, "UPDATE facts SET embedding_state = 'failed' WHERE id = ?", (theirs,))

    await _memory("reindex", identity)

    assert _state(store, theirs) == "failed"
