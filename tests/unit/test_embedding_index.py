"""嵌入模型和語意索引對不對得上（ADR-0315）：名稱、讀 meta.json、判定、狀態、換模型評估.

全在沙盒：tmp_path 的事實庫與 Qdrant 本機資料夾；不載入嵌入模型、不連網。
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

import pytest
from memory_server import factual as fm
from memory_server.utils import embedding_index as ei
from memory_server.utils.collection_names import (
    QDRANT_DIR_NAME,
    QDRANT_META_FILE,
    collection_name_for,
)
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams
from src.models.identity import AgentIdentity

if TYPE_CHECKING:
    from pathlib import Path

E5 = "intfloat/multilingual-e5-base"
MINILM = "all-MiniLM-L6-v2"
PARAPHRASE = "paraphrase-multilingual-MiniLM-L12-v2"


def _identity(agent_id: str) -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=agent_id, role="Owner")


def _collection(size: int | None, model: str | None = None) -> dict[str, object]:
    vectors: object = {"size": size, "distance": "Cosine"} if size is not None else {}
    metadata = {ei.INDEX_MODEL_KEY: model, ei.INDEX_DIM_KEY: size} if model else None
    return {"vectors": vectors, "metadata": metadata}


def _write_meta(vault: Path, collections: dict[str, dict[str, object]]) -> Path:
    path = vault / QDRANT_DIR_NAME / QDRANT_META_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"collections": collections, "aliases": {}}), encoding="utf-8")
    return path


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return tmp_path / f"vault-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def store(vault: Path) -> fm.FactualStore:
    return fm.FactualStore(base_dir=vault)


@pytest.fixture(autouse=True)
def _fresh_state() -> None:
    ei.reset_index_status()


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch):
    def set_model(model: str | None) -> None:
        if model is None:
            monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        else:
            monkeypatch.setenv("EMBEDDING_MODEL", model)

    return set_model


# --- names -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (MINILM, f"sentence-transformers/{MINILM}"),
        (MINILM, " all-minilm-l6-v2 "),
        (E5, "INTFLOAT/Multilingual-E5-Base"),
    ],
)
def test_same_model_ignores_short_name_case_and_spaces(first: str, second: str) -> None:
    assert ei.same_model(first, second)


def test_different_models_are_not_the_same() -> None:
    assert not ei.same_model(MINILM, PARAPHRASE)
    assert not ei.same_model(E5, "multilingual-e5-base")  # 少了 intfloat/ 是另一個（不存在的）repo


def test_resolve_repo_id_adds_the_sentence_transformers_owner() -> None:
    assert ei.resolve_repo_id(MINILM) == f"sentence-transformers/{MINILM}"
    assert ei.resolve_repo_id(f" {E5} ") == E5


def test_known_dimensions() -> None:
    assert ei.known_dimension(MINILM) == 384
    assert ei.known_dimension(f"sentence-transformers/{PARAPHRASE}") == 384
    assert ei.known_dimension(E5.upper()) == 768
    assert ei.known_dimension("someone/custom-model") is None


# --- reading meta.json ---------------------------------------------------------


def test_reads_size_and_marker_from_a_real_qdrant_folder(tmp_path: Path) -> None:
    folder = tmp_path / QDRANT_DIR_NAME
    client = QdrantClient(path=str(folder))
    try:
        client.create_collection(
            collection_name="memory_A",
            vectors_config=VectorParams(size=4, distance=Distance.COSINE),
            metadata=ei.index_marker(E5, 4),
        )
        client.create_collection(
            collection_name="memory_B",
            vectors_config=VectorParams(size=8, distance=Distance.COSINE),
        )
    finally:
        client.close()

    entries = ei.read_index_entries(tmp_path)

    assert entries is not None
    assert entries["memory_A"] == ei.IndexEntry(name="memory_A", dim=4, model=E5)
    assert entries["memory_B"] == ei.IndexEntry(name="memory_B", dim=8, model=None)


def test_missing_meta_means_no_index_folder(tmp_path: Path) -> None:
    assert ei.read_index_entries(tmp_path) is None


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        json.dumps({"collections": {}}),  # 少了 aliases
        json.dumps({"collections": {"": {}}, "aliases": {}}),
        json.dumps({"collections": {}, "aliases": {}, "extra": 1}),
    ],
)
def test_unreadable_meta_raises(tmp_path: Path, text: str) -> None:
    path = tmp_path / QDRANT_DIR_NAME / QDRANT_META_FILE
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ei.IndexUnreadableError):
        ei.read_index_entries(tmp_path)


def test_named_vectors_and_bad_fields_have_no_size(tmp_path: Path) -> None:
    _write_meta(
        tmp_path,
        {
            "memory_named": {"vectors": {"text": {"size": 4, "distance": "Cosine"}}},
            "memory_bad": {"vectors": {"size": "big"}, "metadata": {ei.INDEX_MODEL_KEY: 3}},
        },
    )

    entries = ei.read_index_entries(tmp_path)

    assert entries is not None
    assert entries["memory_named"] == ei.IndexEntry(name="memory_named", dim=None, model=None)
    assert entries["memory_bad"] == ei.IndexEntry(name="memory_bad", dim=None, model=None)


# --- judging one collection ----------------------------------------------------


@pytest.mark.parametrize(
    ("model", "entry", "expected"),
    [
        (E5, ei.IndexEntry("c", 768, E5.upper()), (ei.Verdict.MATCH, ei.Basis.NAME)),
        (MINILM, ei.IndexEntry("c", 384, f"sentence-transformers/{MINILM}"),
         (ei.Verdict.MATCH, ei.Basis.NAME)),
        (MINILM, ei.IndexEntry("c", 384, PARAPHRASE), (ei.Verdict.MISMATCH, ei.Basis.NAME)),
        (E5, ei.IndexEntry("c", 384, None), (ei.Verdict.MISMATCH, ei.Basis.LENGTH)),
        (MINILM, ei.IndexEntry("c", 768, None), (ei.Verdict.MISMATCH, ei.Basis.LENGTH)),
        (E5, ei.IndexEntry("c", 768, None), (ei.Verdict.MATCH, ei.Basis.LENGTH)),
        (MINILM, ei.IndexEntry("c", 384, None), (ei.Verdict.UNKNOWN, None)),  # 兩個 384
        ("someone/custom", ei.IndexEntry("c", 384, None), (ei.Verdict.UNKNOWN, None)),
        (E5, ei.IndexEntry("c", None, None), (ei.Verdict.UNKNOWN, None)),
    ],
)
def test_judge(model: str, entry: ei.IndexEntry, expected: tuple[object, object]) -> None:
    assert ei.judge(model, entry) == expected


# --- the status the sidebar reads -----------------------------------------------


def test_status_counts_only_agents_that_have_memories(
    vault: Path, store: fm.FactualStore, configured
) -> None:
    configured(E5)
    alice = _identity("Alice")
    store.save_fact(alice, "likes tea")
    _write_meta(
        vault,
        {
            collection_name_for(alice): _collection(384),
            "memory_Gone": _collection(384),  # 刪掉的靈魂留下的舊索引
        },
    )

    status = ei.refresh_index_status(store)

    assert status is not None
    assert status.configured_model == E5
    assert status.notice_fields() == {
        "count": 1,
        "basis": "length",
        "configured_model": E5,
        "index_model": None,
        "index_dim": 384,
    }


def test_status_names_the_model_when_marked(
    vault: Path, store: fm.FactualStore, configured
) -> None:
    configured(MINILM)
    for agent in ("Alice", "Bob"):
        store.save_fact(_identity(agent), "hello")
    _write_meta(
        vault,
        {
            collection_name_for(_identity("Alice")): _collection(768, E5),
            collection_name_for(_identity("Bob")): _collection(768, E5),
        },
    )

    status = ei.refresh_index_status(store)

    assert status is not None
    assert status.notice_fields() == {
        "count": 2,
        "basis": "name",
        "configured_model": MINILM,
        "index_model": E5,
        "index_dim": 768,
    }


def test_matching_status_has_no_notice(vault: Path, store: fm.FactualStore, configured) -> None:
    configured(None)  # 沒設定＝內建英文模型
    alice = _identity("Alice")
    store.save_fact(alice, "hello")
    _write_meta(vault, {collection_name_for(alice): _collection(384, MINILM)})

    status = ei.refresh_index_status(store)

    assert status is not None
    assert status.notice_fields() is None


def test_unknown_only_has_no_notice(vault: Path, store: fm.FactualStore, configured) -> None:
    configured(PARAPHRASE)
    alice = _identity("Alice")
    store.save_fact(alice, "hello")
    _write_meta(vault, {collection_name_for(alice): _collection(384)})

    status = ei.refresh_index_status(store)

    assert status is not None
    assert status.notice_fields() is None


def test_refresh_without_a_store_keeps_the_last_result(
    vault: Path, store: fm.FactualStore, configured
) -> None:
    configured(E5)
    alice = _identity("Alice")
    store.save_fact(alice, "hello")
    _write_meta(vault, {collection_name_for(alice): _collection(384)})
    first = ei.refresh_index_status(store)

    assert ei.refresh_index_status(None) is first
    assert ei.current_index_status() is first


def test_unreadable_meta_keeps_the_last_result_and_logs_once(
    vault: Path, store: fm.FactualStore, configured, caplog: pytest.LogCaptureFixture
) -> None:
    configured(E5)
    alice = _identity("Alice")
    store.save_fact(alice, "hello")
    meta = _write_meta(vault, {collection_name_for(alice): _collection(384)})
    first = ei.refresh_index_status(store)
    meta.write_text("{half written", encoding="utf-8")

    with caplog.at_level("WARNING"):
        assert ei.refresh_index_status(store) is first
        assert ei.refresh_index_status(store) is first

    assert sum("meta.json" in record.getMessage() for record in caplog.records) == 1


def test_first_refresh_with_unreadable_meta_has_no_status(
    vault: Path, store: fm.FactualStore, configured
) -> None:
    configured(E5)
    store.save_fact(_identity("Alice"), "hello")
    path = vault / QDRANT_DIR_NAME / QDRANT_META_FILE
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")

    assert ei.refresh_index_status(store) is None


def test_new_agent_index_shows_up_on_the_next_refresh(
    vault: Path, store: fm.FactualStore, configured
) -> None:
    configured(E5)
    alice = _identity("Alice")
    store.save_fact(alice, "hello")
    _write_meta(vault, {collection_name_for(alice): _collection(768, E5)})
    assert ei.refresh_index_status(store).notice_fields() is None  # type: ignore[union-attr]

    bob = _identity("Bob")
    store.save_fact(bob, "hi")
    _write_meta(
        vault,
        {
            collection_name_for(alice): _collection(768, E5),
            collection_name_for(bob): _collection(384, MINILM),
        },
    )

    fields = ei.refresh_index_status(store).notice_fields()  # type: ignore[union-attr]
    assert fields is not None
    assert fields["count"] == 1


def test_fresh_status_respects_age_and_staleness(
    vault: Path, store: fm.FactualStore, configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured(E5)
    now = [1000.0]
    monkeypatch.setattr(ei, "get_current_epoch", lambda: now[0])
    status = ei.refresh_index_status(store)

    assert ei.fresh_index_status(5) is status
    now[0] += 6
    assert ei.fresh_index_status(5) is None
    now[0] -= 6
    ei.mark_index_status_stale()
    assert ei.fresh_index_status(5) is None
    assert ei.current_index_status() is status


def test_sample_verdicts_fill_in_unknown_collections(
    vault: Path, store: fm.FactualStore, configured
) -> None:
    configured(PARAPHRASE)
    alice = _identity("Alice")
    store.save_fact(alice, "hello")
    name = collection_name_for(alice)
    _write_meta(vault, {name: _collection(384)})
    entry = ei.IndexEntry(name=name, dim=384, model=None)

    ei.record_sample_verdicts(PARAPHRASE, {name: (entry, ei.Verdict.MISMATCH, ei.Basis.SAMPLE)})
    fields = ei.refresh_index_status(store).notice_fields()  # type: ignore[union-attr]

    assert fields == {
        "count": 1,
        "basis": "sample",
        "configured_model": PARAPHRASE,
        "index_model": None,
        "index_dim": 384,
    }


def test_sample_verdicts_are_dropped_when_the_entry_or_model_changes(
    vault: Path, store: fm.FactualStore, configured
) -> None:
    configured(PARAPHRASE)
    alice = _identity("Alice")
    store.save_fact(alice, "hello")
    name = collection_name_for(alice)
    old = ei.IndexEntry(name=name, dim=384, model=None)
    ei.record_sample_verdicts(PARAPHRASE, {name: (old, ei.Verdict.MISMATCH, ei.Basis.SAMPLE)})

    _write_meta(vault, {name: _collection(384, PARAPHRASE)})  # 重建過，多了備註
    assert ei.refresh_index_status(store).notice_fields() is None  # type: ignore[union-attr]

    _write_meta(vault, {name: _collection(384)})
    configured(MINILM)  # 換了設定
    assert ei.refresh_index_status(store).notice_fields() is None  # type: ignore[union-attr]


# --- evaluating a switch (always live) -------------------------------------------


def test_evaluate_without_memories(vault: Path, store: fm.FactualStore) -> None:
    _write_meta(vault, {"memory_Gone": _collection(384, MINILM)})

    assert ei.evaluate_target(E5, store).outcome is ei.TargetOutcome.NO_MEMORIES


def test_evaluate_memories_without_any_index(vault: Path, store: fm.FactualStore) -> None:
    store.save_fact(_identity("Alice"), "hello")
    _write_meta(vault, {"memory_Gone": _collection(384, MINILM)})

    assert ei.evaluate_target(E5, store).outcome is ei.TargetOutcome.NO_INDEX


def test_evaluate_missing_meta_is_unreadable(store: fm.FactualStore) -> None:
    store.save_fact(_identity("Alice"), "hello")

    assert ei.evaluate_target(E5, store).outcome is ei.TargetOutcome.UNREADABLE


def test_evaluate_unreadable_store(vault: Path, store: fm.FactualStore,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> set[str]:
        raise fm.FactsSchemaNotReadyError(fm.FactsSchemaStatus.UNREADABLE)

    monkeypatch.setattr(store, "agent_ids_with_facts", broken)

    assert ei.evaluate_target(E5, store).outcome is ei.TargetOutcome.UNREADABLE


def test_evaluate_all_match(vault: Path, store: fm.FactualStore) -> None:
    alice, bob = _identity("Alice"), _identity("Bob")
    store.save_fact(alice, "hello")
    store.save_fact(bob, "hi")
    _write_meta(
        vault,
        {
            collection_name_for(alice): _collection(768, E5),
            collection_name_for(bob): _collection(768),
        },
    )

    assert ei.evaluate_target(E5, store).outcome is ei.TargetOutcome.MATCH


def test_evaluate_any_mismatch_names_the_index(vault: Path, store: fm.FactualStore) -> None:
    alice, bob = _identity("Alice"), _identity("Bob")
    store.save_fact(alice, "hello")
    store.save_fact(bob, "hi")
    _write_meta(
        vault,
        {
            collection_name_for(alice): _collection(768, E5),
            collection_name_for(bob): _collection(768),
        },
    )

    evaluation = ei.evaluate_target(MINILM, store)

    assert evaluation.outcome is ei.TargetOutcome.MISMATCH
    assert (evaluation.index_model, evaluation.index_dim) == (E5, 768)


def test_evaluate_two_small_models_is_unknown(vault: Path, store: fm.FactualStore) -> None:
    alice = _identity("Alice")
    store.save_fact(alice, "hello")
    _write_meta(vault, {collection_name_for(alice): _collection(384)})

    evaluation = ei.evaluate_target(MINILM, store)

    assert evaluation.outcome is ei.TargetOutcome.UNKNOWN
    assert (evaluation.index_model, evaluation.index_dim) == (None, 384)


def test_evaluate_ignores_sample_results_and_cached_status(
    vault: Path, store: fm.FactualStore, configured
) -> None:
    configured(MINILM)
    alice = _identity("Alice")
    store.save_fact(alice, "hello")
    name = collection_name_for(alice)
    _write_meta(vault, {name: _collection(384)})
    entry = ei.IndexEntry(name=name, dim=384, model=None)
    ei.record_sample_verdicts(MINILM, {name: (entry, ei.Verdict.MATCH, ei.Basis.SAMPLE)})
    ei.refresh_index_status(store)

    assert ei.evaluate_target(MINILM, store).outcome is ei.TargetOutcome.UNKNOWN


def test_agent_ids_with_facts_includes_archived(store: fm.FactualStore) -> None:
    alice, bob = _identity("Alice"), _identity("Bob")
    store.save_fact(alice, "hello")
    saved = store.save_fact(bob, "bye")
    store.archive_fact(bob, saved["id"])

    assert store.agent_ids_with_facts() == {"Alice", "Bob"}


def test_listing_agents_of_an_unreadable_store_does_not_stick(vault: Path) -> None:
    """側欄與啟動檢查碰到讀不到的庫：丟例外，但不把庫標成「讀不到過」（修好就恢復）."""
    vault.mkdir(parents=True)
    db = vault / "core_memory.db"
    db.write_bytes(b"this is not a sqlite database " * 64)
    broken = fm.FactualStore(base_dir=vault)

    with pytest.raises(fm.FactsSchemaNotReadyError):
        broken.agent_ids_with_facts()

    db.unlink()
    fm.FactualStore(base_dir=vault)  # 別的實例建好新庫

    assert broken.current_schema_status() is fm.FactsSchemaStatus.CURRENT
