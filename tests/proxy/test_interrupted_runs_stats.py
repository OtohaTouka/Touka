"""P5c：/stats 回應內含『上次未完成的背景任務』(interrupted_runs)。

沿用 daily_quota / background_activity 的 /stats fold 技巧（免改 openapi/api.ts）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers import stats
from src.services.autonomy import autonomy_service
from src.services.identity_router import get_verified_identity


def test_stats_includes_interrupted_runs():
    """/stats 折入 interrupted_runs，內容反映服務偵測到的未完成背景任務。"""
    sample = [{"session_id": "s1", "agent": "Alice", "total": 5, "started_at": 100.0}]
    app.dependency_overrides[get_verified_identity] = lambda: AgentIdentity.for_role(
        agent_id="QuotaTester", role="admin"
    )
    try:
        with TestClient(app) as client:
            # lifespan 啟動掃描會先重設 _interrupted_runs；於掃描後再注入，確保被讀到
            autonomy_service._interrupted_runs = sample
            resp = client.get("/v1/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert "interrupted_runs" in body
        runs = body["interrupted_runs"]
        assert isinstance(runs, list)
        assert len(runs) == 1
        assert runs[0]["agent"] == "Alice"
        assert runs[0]["total"] == 5
    finally:
        autonomy_service._interrupted_runs = []
        app.dependency_overrides.clear()


def test_stats_interrupted_runs_empty_by_default():
    """無未完成任務時 interrupted_runs 為空清單（鍵恆存在）。"""
    app.dependency_overrides[get_verified_identity] = lambda: AgentIdentity.for_role(
        agent_id="QuotaTester", role="admin"
    )
    try:
        with TestClient(app) as client:
            autonomy_service._interrupted_runs = []
            resp = client.get("/v1/stats")
        assert resp.status_code == 200
        assert resp.json().get("interrupted_runs") == []
    finally:
        app.dependency_overrides.clear()



def test_stats_lists_failed_handovers_only_for_souls_you_can_see(monkeypatch):
    """交接失敗要看得到（ADR-0078、ADR-0307）；別人靈魂的不給。"""
    rows = [
        {"agent": "Mine", "old_session": "a", "new_session": "b", "failures": 3, "updated_at": 1},
        {"agent": "Theirs", "old_session": "c", "new_session": "d", "failures": 3, "updated_at": 2},
    ]
    monkeypatch.setattr(stats, "list_failed_handovers", AsyncMock(return_value=rows))
    monkeypatch.setattr(stats, "can_access_session", lambda _who, agent: agent == "Mine")
    app.dependency_overrides[get_verified_identity] = lambda: AgentIdentity.for_role(
        "owner-1", "normal", is_human=True
    )
    try:
        with TestClient(app) as client:
            resp = client.get("/v1/stats")
        assert resp.status_code == 200
        assert [row["agent"] for row in resp.json()["failed_handovers"]] == ["Mine"]
    finally:
        app.dependency_overrides.clear()


def test_stats_still_answers_when_failed_handovers_cannot_be_read(monkeypatch):
    monkeypatch.setattr(stats, "list_failed_handovers", AsyncMock(side_effect=OSError("db")))
    app.dependency_overrides[get_verified_identity] = lambda: AgentIdentity.for_role(
        agent_id="QuotaTester", role="admin"
    )
    try:
        with TestClient(app) as client:
            resp = client.get("/v1/stats")
        assert resp.status_code == 200
        assert resp.json()["failed_handovers"] == []
    finally:
        app.dependency_overrides.clear()
