"""索引備註與模型載入後的抽查（ADR-0315）.

全在沙盒：tmp_path 的事實庫與 Qdrant 本機資料夾；嵌入模型換成本地假編碼器（不下載、不連網）。
"""

from __future__ import annotations

import hashlib
import math
import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from memory_server import factual as fm
from memory_server import vector as vector_module
from memory_server.services import index_check
from memory_server.services.manager import MemoryManager
from memory_server.services.reembed import reembed_agent
from memory_server.utils import embedding_index as ei
from memory_server.utils.collection_names import collection_name_for, fact_point_id
from memory_server.vector import VectorStore
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from src.models.identity import AgentIdentity

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

ENCODER = "sandbox-encoder"
DIM = 32


class _Embedding(list):
    def tolist(self) -> list[float]:
        return list(self)


class _SandboxEncoder:
    """Deterministic local stand-in for the embedding model; never downloads anything."""

    calls = 0

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def get_sentence_embedding_dimension(self) -> int:
        return DIM

    def encode(self, text: str) -> _Embedding:
        type(self).calls += 1
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return _Embedding((byte - 127.5) / 127.5 for byte in digest)


def _encode(text: str) -> list[float]:
    return _SandboxEncoder().encode(text).tolist()


def _identity(prefix: str = "Check") -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=f"{prefix}{uuid.uuid4().hex[:8]}", role="Owner")


def _cosine(first: list[float], second: list[float]) -> float:
    dot = sum(x * y for x, y in zip(first, second, strict=True))
    return dot / (math.sqrt(sum(x * x for x in first)) * math.sqrt(sum(y * y for y in second)))


def _blend(base: list[float], other: list[float], target: float) -> list[float]:
    """Return a vector whose cosine with ``base`` is ``target`` (other is made orthogonal)."""
    norm_base = math.sqrt(sum(x * x for x in base))
    unit = [x / norm_base for x in base]
    projection = sum(x * y for x, y in zip(other, unit, strict=True))
    orth = [y - projection * x for x, y in zip(unit, other, strict=True)]
    norm_orth = math.sqrt(sum(x * x for x in orth))
    orth = [x / norm_orth for x in orth]
    side = math.sqrt(1 - target * target)
    return [target * x + side * y for x, y in zip(unit, orth, strict=True)]


@pytest.fixture(autouse=True)
def _fresh_state() -> None:
    ei.reset_index_status()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return tmp_path / f"vault-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def store(vault: Path, monkeypatch: pytest.MonkeyPatch) -> fm.FactualStore:
    factual = fm.FactualStore(base_dir=vault)
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", factual)
    return factual


@pytest.fixture
def encoder(monkeypatch: pytest.MonkeyPatch) -> type[_SandboxEncoder]:
    monkeypatch.setattr(vector_module, "SentenceTransformer", _SandboxEncoder)
    monkeypatch.setenv("EMBEDDING_MODEL", ENCODER)
    _SandboxEncoder.calls = 0
    return _SandboxEncoder


@pytest.fixture
def vectors(vault: Path, encoder: type[_SandboxEncoder]) -> Iterator[VectorStore]:
    built = VectorStore(base_dir=vault, model_name=ENCODER)
    yield built
    VectorStore._client_cache.pop(str((vault / "_qdrant_db").absolute())).close()


def _legacy_collection(vectors: VectorStore, identity: AgentIdentity,
                       points: list[tuple[str, list[float]]], size: int = DIM) -> None:
    """An old collection without the marker (as built before ADR-0315)."""
    name = collection_name_for(identity)
    vectors.client.create_collection(
        collection_name=name, vectors_config=VectorParams(size=size, distance=Distance.COSINE)
    )
    vectors.client.upsert(
        collection_name=name,
        points=[
            PointStruct(id=fact_point_id(f"f{index}"), vector=vector, payload={"content": content})
            for index, (content, vector) in enumerate(points)
        ],
    )


class _SyntheticCheckFailure(Exception):
    """A failure raised by the check double below."""


def _load_through_manager() -> VectorStore:
    """The production path: MemoryManager builds the store (under ``zone_data_root``) and hangs
    the check on it."""
    MemoryManager._vector_store_singleton = None
    return MemoryManager().get_vector_store()


def _forget_manager_store(root: Path) -> None:
    client = VectorStore._client_cache.pop(str((root / "vault" / "_qdrant_db").absolute()), None)
    if client is not None:
        client.close()
    MemoryManager._vector_store_singleton = None


# --- the marker ---------------------------------------------------------------


def test_new_collection_records_the_model(
    vectors: VectorStore, store: fm.FactualStore
) -> None:
    identity = _identity()

    assert vectors.save_vector(identity, "f1", "tea at noon")

    config = vectors.client.get_collection(collection_name_for(identity)).config
    assert config.metadata == {ei.INDEX_MODEL_KEY: ENCODER, ei.INDEX_DIM_KEY: DIM}
    entries = ei.read_index_entries(vectors.base_dir)
    assert entries is not None
    assert entries[collection_name_for(identity)].model == ENCODER


def test_reembed_records_the_model(tmp_path: Path) -> None:
    client = QdrantClient(path=str(tmp_path / "q"))
    try:
        vector_store = MagicMock()
        vector_store.client = client
        vector_store.model_name = "intfloat/multilingual-e5-base"
        vector_store.model.get_sentence_embedding_dimension.return_value = 4
        vector_store.get_embedding.return_value = [0.1, 0.2, 0.3, 0.4]
        vector_store.get_collection_name.return_value = "memory_TestAgent"
        factual = MagicMock()
        factual.query_facts.return_value = [{"id": "fact_a", "content": "hello"}]

        reembed_agent(factual, vector_store, _identity())

        metadata = client.get_collection("memory_TestAgent").config.metadata
    finally:
        client.close()
    assert metadata == {
        ei.INDEX_MODEL_KEY: "intfloat/multilingual-e5-base",
        ei.INDEX_DIM_KEY: 4,
    }


# --- the check after the model loads ---------------------------------------------


def _prepare(vectors: VectorStore, store: fm.FactualStore,
             points: list[tuple[str, list[float]]], size: int = DIM) -> AgentIdentity:
    identity = _identity()
    store.save_fact(identity, points[0][0] if points else "x")
    _legacy_collection(vectors, identity, points, size)
    ei.refresh_index_status(store)  # 啟動時算好的狀態
    return identity


def _run_check(vectors: VectorStore, store: fm.FactualStore) -> dict[str, object] | None:
    index_check.check_after_model_load(vectors, store)
    status = ei.current_index_status()
    assert status is not None
    return status.notice_fields()


def test_same_model_vectors_match(vectors: VectorStore, store: fm.FactualStore) -> None:
    _prepare(vectors, store, [(text, _encode(text)) for text in ("a", "b", "c")])

    assert _run_check(vectors, store) is None
    assert ei.current_index_status().results[0].verdict is ei.Verdict.MATCH  # type: ignore[union-attr]


def test_other_model_vectors_mismatch(vectors: VectorStore, store: fm.FactualStore) -> None:
    _prepare(vectors, store, [(text, _encode("other " + text)) for text in ("a", "b")])

    fields = _run_check(vectors, store)

    assert fields == {
        "count": 1,
        "basis": "sample",
        "configured_model": ENCODER,
        "index_model": None,
        "index_dim": DIM,
    }


def test_mixed_index_with_one_old_vector_mismatches(
    vectors: VectorStore, store: fm.FactualStore
) -> None:
    points = [(text, _encode(text)) for text in ("a", "b", "c")]
    points[1] = ("b", _encode("written by the old model"))

    fields = _run_check_with(vectors, store, points)

    assert fields is not None
    assert fields["basis"] == "sample"


def _run_check_with(vectors: VectorStore, store: fm.FactualStore,
                    points: list[tuple[str, list[float]]]) -> dict[str, object] | None:
    _prepare(vectors, store, points)
    return _run_check(vectors, store)


def test_in_between_similarity_is_unknown(vectors: VectorStore, store: fm.FactualStore) -> None:
    base = _encode("a")
    middle = _blend(base, _encode("noise"), 0.9)
    assert 0.89 < _cosine(base, middle) < 0.91

    assert _run_check_with(vectors, store, [("a", middle)]) is None
    assert ei.current_index_status().results[0].verdict is ei.Verdict.UNKNOWN  # type: ignore[union-attr]


def test_points_without_content_are_skipped(vectors: VectorStore, store: fm.FactualStore) -> None:
    identity = _identity()
    store.save_fact(identity, "x")
    name = collection_name_for(identity)
    vectors.client.create_collection(
        collection_name=name, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE)
    )
    vectors.client.upsert(
        collection_name=name,
        points=[PointStruct(id=fact_point_id("f"), vector=_encode("x"), payload={})],
    )
    ei.refresh_index_status(store)

    assert _run_check(vectors, store) is None
    assert ei.current_index_status().results[0].verdict is ei.Verdict.UNKNOWN  # type: ignore[union-attr]


def test_actual_length_mismatch(vectors: VectorStore, store: fm.FactualStore) -> None:
    _prepare(vectors, store, [("a", [0.5] * 16)], size=16)

    fields = _run_check(vectors, store)

    assert fields is not None
    assert (fields["basis"], fields["index_dim"]) == ("length", 16)


def test_marked_collections_are_not_sampled(
    vectors: VectorStore, store: fm.FactualStore, encoder: type[_SandboxEncoder]
) -> None:
    identity = _identity()
    store.save_fact(identity, "x")
    vectors.save_vector(identity, "f1", "x")  # 新建的，有備註
    ei.refresh_index_status(store)
    calls = encoder.calls

    assert _run_check(vectors, store) is None
    assert encoder.calls == calls


def test_no_status_means_no_check(vectors: VectorStore, store: fm.FactualStore,
                                  encoder: type[_SandboxEncoder]) -> None:
    identity = _identity()
    store.save_fact(identity, "a")
    _legacy_collection(vectors, identity, [("a", _encode("other"))])
    calls = encoder.calls

    index_check.check_after_model_load(vectors, store)

    assert ei.current_index_status() is None
    assert encoder.calls == calls


def test_manager_runs_the_check_once_when_the_model_loads(
    zone_data_root: Path, store: fm.FactualStore, encoder: type[_SandboxEncoder],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(
        "memory_server.services.manager.check_after_model_load",
        lambda vs, factual: seen.append((vs, factual)),
    )
    built = _load_through_manager()
    try:
        assert seen == []
        _ = built.model
        _ = built.model
        assert seen == [(built, store)]
    finally:
        _forget_manager_store(zone_data_root)


def test_directly_built_store_does_not_check(
    vectors: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[object] = []
    monkeypatch.setattr(
        "memory_server.services.manager.check_after_model_load",
        lambda *args: called.append(args),
    )

    _ = vectors.model

    assert called == []


def test_a_failing_check_does_not_break_loading(
    zone_data_root: Path, store: fm.FactualStore, encoder: type[_SandboxEncoder],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_args: object) -> None:
        raise _SyntheticCheckFailure

    monkeypatch.setattr("memory_server.services.manager.check_after_model_load", broken)
    built = _load_through_manager()
    try:
        assert built.model.get_sentence_embedding_dimension() == DIM
    finally:
        _forget_manager_store(zone_data_root)
