"""P3：/stats 回應內含本人今日自主配額（daily_count/daily_max/remaining）。"""
from __future__ import annotations

from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.activity_registry import activity_registry
from src.services.autonomy import autonomy_service
from src.services.identity_router import get_verified_identity


def test_get_daily_quota_service_fresh_identity():
    q = autonomy_service.get_daily_quota(
        AgentIdentity.for_role(agent_id="QuotaTester", role="Owner")
    )
    assert q["daily_count"] >= 0
    assert q["daily_max"] >= 1
    assert q["remaining"] == max(0, q["daily_max"] - q["daily_count"])


def test_stats_endpoint_includes_daily_quota():
    app.dependency_overrides[get_verified_identity] = lambda: AgentIdentity.for_role(
        agent_id="QuotaTester", role="admin"
    )
    try:
        with TestClient(app) as client:
            resp = client.get("/v1/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert "daily_quota" in body
        dq = body["daily_quota"]
        assert set(dq.keys()) >= {"daily_count", "daily_max", "remaining"}
        assert dq["remaining"] == max(0, dq["daily_max"] - dq["daily_count"])
        assert isinstance(body.get("background_activity"), dict)
    finally:
        app.dependency_overrides.clear()


def test_stats_endpoint_reflects_background_activity():
    """P4：/stats 的 background_activity 反映登記中的背景活動（心跳脈衝進度）。"""
    activity_registry.reload_all()
    activity_registry.set_activity(
        "bg-sess-1", {"kind": "heartbeat", "current": 2, "total": 5, "agent": "Alice"}
    )
    app.dependency_overrides[get_verified_identity] = lambda: AgentIdentity.for_role(
        agent_id="QuotaTester", role="admin"
    )
    try:
        with TestClient(app) as client:
            resp = client.get("/v1/stats")
        assert resp.status_code == 200
        ba = resp.json().get("background_activity", {})
        assert ba.get("bg-sess-1", {}).get("current") == 2
        assert ba["bg-sess-1"]["total"] == 5
    finally:
        app.dependency_overrides.clear()
        activity_registry.reload_all()
