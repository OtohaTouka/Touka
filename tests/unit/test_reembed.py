"""``reembed_agent`` 單元測試（temp qdrant + mock 掉 embedding）。

驗證：讀 FactualStore（含 archived）→ 重建 collection → 每事實一向量、
保留 created_at/archived/payload、point_id=uuid5(fact_id)；且可重跑（idempotent）。
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from memory_server.services.reembed import reembed_agent
from qdrant_client import QdrantClient

_FACTS = [
    {
        "id": "fact_a",
        "content": "今天天氣很好",
        "tags": ["weather"],
        "importance": "high",
        "emotions": ["joy"],
        "created_at": 100,
        "archived": False,
    },
    {
        "id": "fact_b",
        "content": "archived item",
        "tags": [],
        "importance": None,
        "emotions": None,
        "created_at": 200,
        "archived": True,
    },
]


def _make_vector_store(client: QdrantClient) -> MagicMock:
    vs = MagicMock()
    vs.client = client
    vs.model_name = "intfloat/multilingual-e5-base"
    vs.model.get_sentence_embedding_dimension.return_value = 4
    vs.get_embedding.return_value = [0.1, 0.2, 0.3, 0.4]
    vs.get_collection_name.return_value = "memory_TestAgent"
    return vs


def test_reembed_agent_rebuilds_preserving_payload(tmp_path):
    client = QdrantClient(path=str(tmp_path / "_qdrant_db"))
    vs = _make_vector_store(client)
    factual = MagicMock()
    factual.query_facts.return_value = list(_FACTS)
    identity = MagicMock()

    n = reembed_agent(factual, vs, identity)

    assert n == 2
    # FactualStore 必須以 include_archived=True 讀取（不漏 archived）
    assert factual.query_facts.call_args.kwargs.get("include_archived") is True
    assert client.count("memory_TestAgent").count == 2
    pts = client.query_points(
        "memory_TestAgent", query=[0.1, 0.2, 0.3, 0.4], limit=10
    ).points
    by_fact = {p.payload["fact_id"]: p.payload for p in pts}
    assert by_fact["fact_a"]["created_at"] == 100
    assert by_fact["fact_a"]["archived"] is False
    assert by_fact["fact_a"]["content"] == "今天天氣很好"
    assert by_fact["fact_b"]["created_at"] == 200
    assert by_fact["fact_b"]["archived"] is True
    # point_id 必須是 uuid5(fact_id)（可冪等覆寫）
    expect_id = str(uuid.uuid5(uuid.NAMESPACE_OID, "fact_a"))
    assert any(str(p.id) == expect_id for p in pts)
    client.close()


def test_reembed_agent_idempotent(tmp_path):
    client = QdrantClient(path=str(tmp_path / "_qdrant_db"))
    vs = _make_vector_store(client)
    factual = MagicMock()
    factual.query_facts.return_value = list(_FACTS)
    identity = MagicMock()

    reembed_agent(factual, vs, identity)
    n2 = reembed_agent(factual, vs, identity)

    assert n2 == 2
    assert client.count("memory_TestAgent").count == 2  # 重跑不重複堆積
    client.close()
