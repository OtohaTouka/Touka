"""TDD — 軌B 批次1：/v1/debug/scorecard/* 端點（注入假 runner，零 API 花費）。

驗證：角色門（Owner/Debugger/admin）、run 回 job_id、stream SSE 吐出成績單、未知 job 報錯。
"""
from __future__ import annotations

import pytest
import src.routers.debug as debug_router
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from src.services.scorecard.models import RunOutcome

OWNER = AgentIdentity.for_role(agent_id="OwnerUser", role="Owner", is_human=True, capabilities=[])
BASE = AgentIdentity.for_role(agent_id="BaseUser", role="Base", is_human=True, capabilities=[])


async def _fake_runner(_model: str, _scenario) -> RunOutcome:
    return RunOutcome(text="ok", tokens=5, cost_usd=0.001, latency_ms=3)


@pytest.fixture
def owner_client(monkeypatch):
    monkeypatch.setattr(debug_router, "_scorecard_runner", _fake_runner)
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_run_requires_debug_role():
    app.dependency_overrides[get_verified_identity] = lambda: BASE
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/debug/scorecard/run", json={"model": "deepseek-chat"})
        assert resp.status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()


def test_run_returns_job_id(owner_client):
    resp = owner_client.post(
        "/v1/debug/scorecard/run",
        json={"model": "deepseek-chat", "budget_usd": 0.5},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"].startswith("sc_")
    assert body["status"] == "running"
    assert "chat_basic" in body["scenarios"]


def test_run_rejects_nonpositive_budget(owner_client):
    resp = owner_client.post(
        "/v1/debug/scorecard/run",
        json={"model": "deepseek-chat", "budget_usd": 0},
    )
    assert resp.status_code == 422  # pydantic gt=0


def test_stream_emits_result(owner_client):
    run = owner_client.post(
        "/v1/debug/scorecard/run",
        json={"model": "deepseek-chat", "scenario_ids": ["chat_basic"]},
    )
    job_id = run.json()["job_id"]
    stream = owner_client.get(f"/v1/debug/scorecard/stream/{job_id}")
    assert stream.status_code == 200
    assert "result" in stream.text
    assert "deepseek-chat" in stream.text


def test_stream_unknown_job(owner_client):
    resp = owner_client.get("/v1/debug/scorecard/stream/sc_unknown")
    assert 400 <= resp.status_code < 500
