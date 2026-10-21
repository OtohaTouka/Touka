"""向量記憶重建（re-embed）服務.

從 FactualStore（無損事實 SSOT）讀取某 agent 全部事實，以目前的 embedding 模型
重建其 Qdrant collection（刪舊建新 + 重算向量），保留原 ``created_at`` /
``archived`` / payload。**全程不改 FactualStore（唯讀）**。供維護腳本呼叫。
不升級記憶庫結構：版本落後或無法使用就拒絕（只有 Proxy 啟動時會升級）。
也不寫回事實的 ``embedding_state``：重建後在 Touka 裡跑一次 ``memory_tools(action="reindex")``，
它會先問向量庫，已經有向量的直接標 ready。

破壞性窗口最小化：先把所有事實重算成向量，向量備妥後才刪舊 collection、建新、upsert。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from qdrant_client.models import Distance, PointStruct, VectorParams

from memory_server.factual import FactsSchemaNotReadyError, FactualStore
from memory_server.vector import _format_for_embedding, fact_point_id

if TYPE_CHECKING:
    from pathlib import Path

    from src.models.identity import AgentIdentity

    from memory_server.vector import VectorStore

logger = logging.getLogger(__name__)


def open_factual_store_for_reembed(vault: str | Path) -> FactualStore:
    """離線重建索引用：絕不升級結構；版本落後或無法使用就丟 FactsSchemaNotReadyError。"""
    store = FactualStore(base_dir=vault, allow_schema_upgrade=False)
    status = store.current_schema_status()
    if not status.writable:
        raise FactsSchemaNotReadyError(status)
    return store


def reembed_agent(
    factual: FactualStore,
    vector_store: VectorStore,
    identity: AgentIdentity,
) -> int:
    """重建單一 agent 的向量 collection；回傳重建的向量數。

    讀 FactualStore 全部事實（含 archived，唯讀）→ 先全部重算向量 → 才刪舊
    collection、依目前模型維度建新 collection、批次 upsert，保留原 payload
    （fact_id/content/tags/importance/emotions/created_at/archived）。
    """
    facts = factual.query_facts(identity, query="", include_archived=True)
    name = vector_store.get_collection_name(identity)
    dim = vector_store.model.get_sentence_embedding_dimension()

    # 1. 先把所有事實重算成向量（尚未做任何破壞性操作）。
    points: list[PointStruct] = []
    for f in facts:
        vec = vector_store.get_embedding(
            _format_for_embedding(vector_store.model_name, f["content"], is_query=False)
        )
        points.append(
            PointStruct(
                id=fact_point_id(f["id"]),
                vector=vec,
                payload={
                    "fact_id": f["id"],
                    "content": f["content"],
                    "tags": f.get("tags") or [],
                    "importance": f.get("importance"),
                    "emotions": f.get("emotions") or [],
                    "created_at": f.get("created_at"),
                    "archived": bool(f.get("archived")),
                },
            )
        )

    # 2. 向量備妥後才重建 collection（破壞性窗口最小化、可重跑）。
    if vector_store.client.collection_exists(name):
        vector_store.client.delete_collection(name)
    vector_store.client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )
    if points:
        vector_store.client.upsert(collection_name=name, points=points)

    logger.info("Re-embedded %s: %d vectors (dim=%d)", name, len(points), dim)
    return len(points)
