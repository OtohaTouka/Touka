"""語意索引狀態：事實庫（SQLite）是正本，向量庫只是可以重建的索引.

- 存記憶：事實先寫進事實庫；向量寫不進去（含建 collection 失敗、取不到向量庫）只標
  ``failed``，事實不刪。
- 語意結果：一律回查事實庫，只留這個身分、未封存的事實；封存時向量庫失敗也以事實庫為準，
  那筆標 ``failed``，下次補索引時把向量的封存補上。
- 手動補索引（``memory_tools(action="reindex")``）：只補呼叫者自己的事實；向量庫已經有的
  直接標 ``ready``，沒有的才重算；逐筆隔離錯誤，沿用原本的 ``created_at``，一次最多
  ``MAX_REINDEX_FACTS`` 筆。不自動觸發；換嵌入模型的全量重建仍是離線工具。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from memory_server.config_utils import get_embedding_model_name
from memory_server.factual import EmbeddingState

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

    from memory_server.factual import FactualStore
    from memory_server.vector import VectorStore

logger = logging.getLogger(__name__)

MAX_REINDEX_FACTS = 100  # 一次手動補索引最多處理幾筆（沒有向量的每筆要算一次）
_PENDING_STATES = (EmbeddingState.UNVERIFIED, EmbeddingState.FAILED)


@dataclass(frozen=True)
class ReindexReport:
    """一次補索引的結果；remaining 是做完後仍待補（未核對＋失敗）的筆數.

    archived_synced／archived_failed：已封存、但當時向量沒跟著封存的事實，這次補好／仍失敗。
    """

    checked: int
    already_indexed: int
    embedded: int
    failed: int
    remaining: int
    archived_synced: int = 0
    archived_failed: int = 0


def _embed(vector: VectorStore, identity: AgentIdentity, fact: dict[str, Any]) -> bool:
    """寫一筆事實的向量；任何錯誤（含建 collection 失敗）都只算這一筆失敗."""
    try:
        return bool(
            vector.save_vector(
                identity,
                fact["id"],
                fact["content"],
                fact.get("tags"),
                fact.get("importance"),
                fact.get("emotions"),
                created_at=fact.get("created_at"),
            )
        )
    except Exception:
        logger.warning("[Index] could not write the vector for %s", fact["id"], exc_info=True)
        return False


def index_new_fact(
    factual: FactualStore,
    vector: VectorStore | None,
    identity: AgentIdentity,
    fact: dict[str, Any],
) -> bool:
    """剛存好的事實寫進語意索引並記下狀態；回是否已進索引。事實無論如何都留著."""
    indexed = vector is not None and _embed(vector, identity, fact)
    state = EmbeddingState.READY if indexed else EmbeddingState.FAILED
    try:
        factual.set_embedding_state(
            identity, [fact["id"]], state, get_embedding_model_name() if indexed else None
        )
    except Exception:  # 沒記到狀態時仍是未核對，補索引照樣會處理
        logger.warning("[Index] could not record %s for %s", state.value, fact["id"], exc_info=True)
    return indexed


def archive_in_index(
    factual: FactualStore, vector: VectorStore | None, identity: AgentIdentity, fact_id: str
) -> bool:
    """事實庫封存後，向量也封存；沒成功（取不到、丟錯或回 False）就把那筆標 failed 等補索引."""
    archived = False
    if vector is not None:
        try:
            archived = bool(vector.archive_vector_by_fact_id(identity, fact_id))
        except Exception:  # 事實庫已封存為準；語意結果一律回查事實庫，舊向量不會再出現
            logger.warning("[Index] could not archive the vector of %s", fact_id, exc_info=True)
    if not archived:
        try:
            factual.set_embedding_state(identity, [fact_id], EmbeddingState.FAILED, None)
        except Exception:
            logger.warning("[Index] could not mark %s for reindex", fact_id, exc_info=True)
    return archived


def _sync_archived(
    vector: VectorStore, identity: AgentIdentity, fact_ids: list[str]
) -> tuple[list[str], list[str]]:
    """補上已封存事實的向量封存：沒有向量的不用補；回（已同步, 仍失敗）."""
    if not fact_ids:
        return [], []
    try:
        present = vector.indexed_fact_ids(identity, fact_ids)
    except Exception:  # 問不到就這次不動，下次再試
        logger.warning("[Index] cannot list vectors of archived facts", exc_info=True)
        return [], list(fact_ids)
    synced = [fact_id for fact_id in fact_ids if fact_id not in present]
    failed: list[str] = []
    for fact_id in fact_ids:
        if fact_id in present:
            try:
                done = bool(vector.archive_vector_by_fact_id(identity, fact_id))
            except Exception:
                logger.warning("[Index] could not archive the vector of %s", fact_id, exc_info=True)
                done = False
            (synced if done else failed).append(fact_id)
    return synced, failed


def live_semantic_hits(
    factual: FactualStore, identity: AgentIdentity, hits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """語意結果回查事實庫：只留這個身分、未封存的事實，內容以事實庫為準（保留相似度）."""
    fact_ids = [hit["fact_id"] for hit in hits if hit.get("fact_id")]
    if not fact_ids:
        return []
    live = factual.get_active_facts(identity, fact_ids)
    return [
        live[hit["fact_id"]] | {"fact_id": hit["fact_id"], "_score": hit.get("_score", 0.0)}
        for hit in hits
        if hit.get("fact_id") in live
    ]


def reindex_facts(
    factual: FactualStore, vector: VectorStore, identity: AgentIdentity
) -> ReindexReport:
    """手動補索引：先問向量庫哪些已經有，有的標 ready，沒有的才重算向量."""
    pending = factual.list_pending_index(identity, MAX_REINDEX_FACTS)
    fact_ids = [fact["id"] for fact in pending]
    try:
        present = vector.indexed_fact_ids(identity, fact_ids)
    except Exception:  # 查不到就全部重寫：同一筆的 point id 固定，重寫不會重複
        logger.warning("[Index] cannot list existing vectors; re-embedding all", exc_info=True)
        present = set()
    already = [fact_id for fact_id in fact_ids if fact_id in present]
    ready = list(already)
    failed: list[str] = []
    for fact in pending:
        if fact["id"] not in already:
            (ready if _embed(vector, identity, fact) else failed).append(fact["id"])
    if ready:
        factual.set_embedding_state(
            identity, ready, EmbeddingState.READY, get_embedding_model_name()
        )
    if failed:
        factual.set_embedding_state(identity, failed, EmbeddingState.FAILED, None)
    counts = factual.count_embedding_states(identity)
    synced, unsynced = _sync_archived(
        vector, identity, factual.list_archive_pending(identity, MAX_REINDEX_FACTS)
    )
    if synced:  # 封存的事實不再查向量：不記模型
        factual.set_embedding_state(identity, synced, EmbeddingState.READY, None)
    if unsynced:
        factual.set_embedding_state(identity, unsynced, EmbeddingState.FAILED, None)
    return ReindexReport(
        checked=len(pending),
        already_indexed=len(already),
        embedded=len(ready) - len(already),
        failed=len(failed),
        remaining=sum(counts.get(state.value, 0) for state in _PENDING_STATES),
        archived_synced=len(synced),
        archived_failed=len(unsynced),
    )
