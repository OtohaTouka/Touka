"""ADR-0239 安全網：角色閘「掛在 Depends」與「在函式內呼叫」的 HTTP 優先序。

純重構（Authorization Gateway）前後必須完全一致：

- **Depends 形式**：角色閘是路由依賴，FastAPI 先解依賴再驗 body → 無權呼叫者送出
  格式錯誤（但 JSON 合法）的 body 時得到 **403**（錯誤碼依閘家族而定），不是 422。
- **函式內呼叫形式**（debug scorecard run／reset）：body 先驗證，失敗即 **422**，
  根本走不到函式內的角色閘；body 合法時才由閘回 403。

另釘 skills ``_require_owner_role``（Owner/admin，排除 Debugger）兩個安裝端點的閘：
被拒角色 403＋AUTH_PERMISSION_DENIED；放行角色只驗證閘通過（安裝服務以 mock 取代，
不做真實下載／安裝）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from src.services.skill_service import skill_service

if TYPE_CHECKING:
    from collections.abc import Iterator

OWNER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])
BASE = AgentIdentity.for_role(agent_id="DebugAgent", role="Base", capabilities=[])
DEBUGGER = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger", capabilities=[])
HUMAN_ADMIN = AgentIdentity.for_role(
    agent_id="AdminUser", role="admin", is_human=True, capabilities=[]
)
HUMAN_NORMAL = AgentIdentity.for_role(
    agent_id="NormalUser", role="normal", is_human=True, capabilities=[]
)

_PD = "AUTH_PERMISSION_DENIED"
_ADM = "AUTH_ADMIN_REQUIRED"

# 合法 JSON、但不符合 body 模型（陣列而非物件）——避開 JSON 解析錯誤那條更早的 422 路徑。
_SCHEMA_INVALID_BODY: list[int] = [1]


def _client_as(identity: AgentIdentity | None) -> TestClient:
    app.dependency_overrides[get_verified_identity] = lambda: identity
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def as_identity() -> Iterator[object]:
    """回傳 ``factory(identity) -> TestClient``；結束時移除本檔加的覆寫。"""

    def _factory(identity: AgentIdentity | None) -> TestClient:
        return _client_as(identity)

    yield _factory
    app.dependency_overrides.pop(get_verified_identity, None)


def _error_code(resp: object) -> str:
    return resp.json()["detail"]["code"]


# ── Depends 形式：角色閘先於 body 驗證 → 403 ──────────────────────────────────


@pytest.mark.parametrize(
    ("path", "identity", "expected_code"),
    [
        # admin-only（_require_admin_only）：Owner 非 admin
        pytest.param("/v1/identity", OWNER, _ADM, id="identity-admin-only"),
        # admin-only（ui_sessions._require_admin_role）
        pytest.param("/v1/ui/settings/workspace", OWNER, _ADM, id="ui-workspace-admin"),
        # 三元組（autonomy._require_autonomy_role）
        pytest.param("/v1/autonomy", BASE, _PD, id="autonomy-privileged"),
        # 已認證（feedback._require_authenticated）：未認證 None
        pytest.param("/v1/feedback/submit", None, _PD, id="feedback-authenticated"),
    ],
)
def test_depends_gate_wins_over_invalid_body(as_identity, path, identity, expected_code):
    """無權呼叫者＋格式錯誤 body：Depends 閘先執行 → 403，不是 422。"""
    resp = as_identity(identity).post(path, json=_SCHEMA_INVALID_BODY)
    assert resp.status_code == 403
    assert _error_code(resp) == expected_code


# ── 函式內呼叫形式：body 驗證先於角色閘 → 422 ─────────────────────────────────


@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param("/v1/debug/scorecard/run", {}, id="scorecard-missing-model"),
        pytest.param("/v1/debug/reset", {"confirm": [1]}, id="reset-bad-confirm"),
        pytest.param("/v1/debug/reset", _SCHEMA_INVALID_BODY, id="reset-array-body"),
    ],
)
def test_inline_gate_loses_to_invalid_body(as_identity, path, body):
    """無權呼叫者＋格式錯誤 body：inline 閘在函式內，body 驗證先失敗 → 422。"""
    resp = as_identity(BASE).post(path, json=body)
    assert resp.status_code == 422


@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param("/v1/debug/scorecard/run", {"model": "m"}, id="scorecard-valid-body"),
        pytest.param(
            "/v1/debug/reset",
            {"confirm": True, "confirm_agent_id": "DebugAgent"},
            id="reset-valid-body",
        ),
    ],
)
def test_inline_gate_denies_once_body_is_valid(as_identity, path, body):
    """body 合法時才輪到函式內的角色閘：Base → 403 AUTH_PERMISSION_DENIED。"""
    resp = as_identity(BASE).post(path, json=body)
    assert resp.status_code == 403
    assert _error_code(resp) == _PD


def test_reset_gate_excludes_debugger(as_identity):
    """reset 的 {Owner,admin} 閘排除 Debugger（合法 body 下仍 403）。"""
    resp = as_identity(DEBUGGER).post(
        "/v1/debug/reset", json={"confirm": True, "confirm_agent_id": "DebugAgent"}
    )
    assert resp.status_code == 403
    assert _error_code(resp) == _PD


# ── skills._require_owner_role：兩個安裝端點 ──────────────────────────────────

_SKILL_INSTALL_CASES = [
    pytest.param(
        "/v1/skills/install",
        {"url": "https://example.invalid/x.skill"},
        "install_skill_from_url",
        id="install-url",
    ),
    pytest.param(
        "/v1/skills/catalog/install",
        {"catalog_id": "demo"},
        "install_skill_from_catalog",
        id="install-catalog",
    ),
]


@pytest.fixture
def mocked_installers(monkeypatch) -> dict[str, AsyncMock]:
    """以 AsyncMock 取代安裝服務：任何情況都不做真實下載／寫入。"""
    mocks = {
        "install_skill_from_url": AsyncMock(return_value="mock-skill"),
        "install_skill_from_catalog": AsyncMock(return_value="mock-skill"),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(skill_service, name, mock)
    return mocks


@pytest.mark.parametrize(("path", "body", "service_method"), _SKILL_INSTALL_CASES)
@pytest.mark.parametrize(
    "identity",
    [
        pytest.param(DEBUGGER, id="debugger"),
        pytest.param(BASE, id="base"),
        pytest.param(HUMAN_NORMAL, id="human-normal"),
        pytest.param(None, id="unauthenticated"),
    ],
)
def test_skill_install_owner_gate_denies(
    as_identity, mocked_installers, path, body, service_method, identity
):
    """Debugger／Base／一般人類／未認證 → 403 AUTH_PERMISSION_DENIED，且服務未被呼叫。"""
    resp = as_identity(identity).post(path, json=body)
    assert resp.status_code == 403
    assert _error_code(resp) == _PD
    mocked_installers[service_method].assert_not_awaited()


@pytest.mark.parametrize(("path", "body", "service_method"), _SKILL_INSTALL_CASES)
@pytest.mark.parametrize(
    "identity",
    [
        pytest.param(OWNER, id="owner"),
        pytest.param(HUMAN_ADMIN, id="human-admin"),
    ],
)
def test_skill_install_owner_gate_allows(
    as_identity, mocked_installers, path, body, service_method, identity
):
    """Owner／人類 admin 通過閘（只驗證閘；安裝服務為 mock）。"""
    resp = as_identity(identity).post(path, json=body)
    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "skill_id": "mock-skill"}
    mocked_installers[service_method].assert_awaited_once()
