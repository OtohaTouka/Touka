"""TDD — observability 端點權限收緊（2026-07-07 權限稽查跟進）。

稽查發現：``/memory/list``（跨 agent 事實庫全量、記憶寶庫資料源）與四個日誌端點
（``proxy_payload.log`` 含實際請求 payload）只要「任何已驗證身分」（含 Guest 級
agent token）即可讀——繞過工具層 Gate-1 的 vault 範圍管制；``/events`` 連身分驗證
都沒有。本檔釘死：
(1) 五端點掛 Owner/Debugger/admin 三元組閘（與 debug.py 對齊），低權角色 403；
    人類 normal 角色一併拒絕（跨 agent 資料非成員面）；
(2) ``/events`` 至少需已驗證身分（角色不限；無身分 403）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity

ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True)
NORMAL = AgentIdentity.for_role(agent_id="NormalUser", role="normal", is_human=True)

_TIGHTENED = [
    "/v1/memory/list",
    "/v1/logs",
    "/v1/system_logs",
    "/v1/logs/recent",
    "/v1/logs/proxy",
]


def _client_for(identity: AgentIdentity | None) -> TestClient:
    app.dependency_overrides[get_verified_identity] = lambda: identity
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", _TIGHTENED)
def test_low_role_denied_403(path):
    """低權身分（含人類 normal）一律 403——防跨 agent 記憶／payload 洩漏。"""
    with _client_for(NORMAL) as c:
        assert c.get(path).status_code == 403


@pytest.mark.parametrize("path", _TIGHTENED)
def test_admin_allowed(path):
    """人類 admin（Debug／監控 UI 的實際使用者）必須放行。"""
    with _client_for(ADMIN) as c:
        assert c.get(path).status_code == 200


def test_events_requires_verified_identity():
    """/events 無已驗證身分 → 403（先前完全無驗證）。"""
    with _client_for(None) as c:
        assert c.get("/v1/events").status_code == 403


def test_events_any_role_ok():
    """/events 只要有已驗證身分即可（低敏感、角色不限）。"""
    with _client_for(NORMAL) as c:
        r = c.get("/v1/events")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
