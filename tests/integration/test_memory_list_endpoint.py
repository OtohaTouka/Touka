"""記憶寶庫 bug 修復 — `/v1/memory/list` 端點整合測試。

證明兩個修復：
1. 數量：不再截斷到 200（用高上限 MEMORY_LIST_LIMIT 跨 agent 完整列示）。
2. 時間：回傳 timestamp 為「毫秒」（後端存秒、邊界轉 ms）。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.observability import MEMORY_LIST_LIMIT
from src.services.identity_router import get_verified_identity


class _FakeFactual:
    """假事實庫：紀錄被要求的 limit，並回傳秒制 created_at 的列。"""

    def __init__(self, rows):
        self.rows = rows
        self.captured_limit = None

    def query_all_facts(self, include_archived: bool = False, limit: int = 200):
        self.captured_limit = limit
        return list(self.rows)[:limit]


class _FakeManager:
    def __init__(self, factual):
        self._factual = factual

    def get_factual_store(self):
        return self._factual


@pytest.fixture
def owner_client():
    app.dependency_overrides[get_verified_identity] = lambda: AgentIdentity.for_role(
        agent_id="owner-test", role="Owner"
    )
    with TestClient(app) as client:
        yield client


def _make_rows(n: int):
    # created_at 以「秒」表示（與 FactualStore 真實儲存一致）
    base = 1772183406  # 2026-02-27（秒）
    return [
        {
            "content": f"記憶 {i}",
            "created_at": base + i,
            "tags": [],
            "agent_id": "Alice" if i % 2 else "Bob",
        }
        for i in range(n)
    ]


def test_memory_list_not_truncated_at_200(owner_client):
    """超過 200 筆時應全數回傳（修復前會被砍到 200）。"""
    rows = _make_rows(250)
    fake = _FakeFactual(rows)
    with patch("memory_server.tools.get_manager", return_value=_FakeManager(fake)):
        resp = owner_client.get("/v1/memory/list")
    assert resp.status_code == 200
    memories = resp.json()["memories"]
    assert len(memories) == 250
    # 端點向 store 要求的是高上限，而非 200
    assert fake.captured_limit == MEMORY_LIST_LIMIT


def test_memory_list_timestamp_in_milliseconds(owner_client):
    """timestamp 應為毫秒（created_at 秒值 ×1000）。"""
    rows = [
        {"content": "c", "created_at": 1780569921, "tags": [], "agent_id": "Alice"}
    ]
    with patch("memory_server.tools.get_manager", return_value=_FakeManager(_FakeFactual(rows))):
        resp = owner_client.get("/v1/memory/list")
    assert resp.status_code == 200
    assert resp.json()["memories"][0]["timestamp"] == 1780569921000
