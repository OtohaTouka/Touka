"""軌B Batch 6 Phase 4：POST /v1/debug/reset 端點——角色門、雙重確認、IDOR 回歸
（送入別的 agent_id 必被拒）。mock orchestrator 避免真實備份/刪除（零副作用）。
"""
from __future__ import annotations

import pytest
import src.routers.debug as debug_router
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.agent_reset.orchestrator import ResetReport
from src.services.identity_router import get_verified_identity

OWNER = AgentIdentity.for_role(agent_id="OwnerUser", role="Owner", is_human=True, capabilities=[])
BASE = AgentIdentity.for_role(agent_id="BaseUser", role="Base", is_human=True, capabilities=[])
# Debugger 是 LDH 對無 token 同網段客戶端自動發放的角色；破壞性重置須排除它（ADR-0104）。
DEBUGGER = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger", capabilities=[])

_OK_BODY = {"confirm": True, "confirm_agent_id": "DebugAgent"}


@pytest.fixture
def owner_client(monkeypatch):
    """注入假 orchestrator（回乾淨報告、絕不真備份/刪除）＋ Owner 身分。"""
    calls: list[str] = []

    async def _fake_reset(candidate: str = "DebugAgent", *, do_backup: bool = True) -> ResetReport:
        calls.append(candidate)
        return ResetReport(agent="DebugAgent", backup={"status": "success"}, clean=True)

    monkeypatch.setattr(debug_router, "reset_debug_agent_data", _fake_reset)
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    with TestClient(app) as client:
        client.reset_calls = calls  # type: ignore[attr-defined]
        yield client
    app.dependency_overrides.clear()


def test_reset_requires_debug_role(monkeypatch):
    """非 Owner/Debugger/admin（Base）→ 拒絕，且 orchestrator 完全不被呼叫。"""

    async def _must_not_run(*a, **k):  # noqa: ANN002,ANN003
        raise AssertionError("orchestrator must not run for unauthorized role")

    monkeypatch.setattr(debug_router, "reset_debug_agent_data", _must_not_run)
    app.dependency_overrides[get_verified_identity] = lambda: BASE
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/debug/reset", json=_OK_BODY)
        assert resp.status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()


def test_reset_rejects_debugger_role(monkeypatch):
    """ADR-0104 R2/R8：Debugger（含 LDH 無 token 自動發放者）→ 拒絕，orchestrator 不被呼叫。

    對齊 admin-gated 的系統備份，並擋掉無認證 LAN 觸發此破壞性端點的路徑。計分卡端點仍允許
    Debugger（不在此測試範圍）。
    """

    async def _must_not_run(*a, **k):  # noqa: ANN002,ANN003
        raise AssertionError("orchestrator must not run for Debugger role")

    monkeypatch.setattr(debug_router, "reset_debug_agent_data", _must_not_run)
    app.dependency_overrides[get_verified_identity] = lambda: DEBUGGER
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/debug/reset", json=_OK_BODY)
        assert resp.status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()


def test_reset_requires_confirm_true(owner_client):
    """confirm=false → 422，且不觸發刪除。"""
    resp = owner_client.post(
        "/v1/debug/reset", json={"confirm": False, "confirm_agent_id": "DebugAgent"}
    )
    assert resp.status_code == 422
    assert owner_client.reset_calls == []


def test_reset_rejects_other_agent_id(owner_client):
    """IDOR 回歸：confirm=true 但 confirm_agent_id 為真實 agent（Jin）→ 422，絕不刪除。"""
    resp = owner_client.post(
        "/v1/debug/reset", json={"confirm": True, "confirm_agent_id": "Jin"}
    )
    assert resp.status_code == 422
    assert owner_client.reset_calls == []  # orchestrator 從未被觸及


def test_reset_rejects_empty_agent_id(owner_client):
    """confirm_agent_id 空字串（誤觸/未填）→ 422。"""
    resp = owner_client.post("/v1/debug/reset", json={"confirm": True})
    assert resp.status_code == 422
    assert owner_client.reset_calls == []


def test_reset_success_returns_report(owner_client):
    """雙重確認齊全 → 200，回傳報告（agent_id=DebugAgent、clean=true）。"""
    resp = owner_client.post("/v1/debug/reset", json=_OK_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "DebugAgent"
    assert body["clean"] is True
    assert owner_client.reset_calls == ["DebugAgent"]  # orchestrator 收到的恆為 DebugAgent
