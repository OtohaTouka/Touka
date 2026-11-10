"""GET /v1/models/lifecycle-alerts：誰看得到哪些提醒（ADR-0310）。

靈魂的範圍比照 /stats（``can_access_session``）；Auto 審查員與摘要員的設定只給人類 admin
（``is_admin_human``，和 ``require_admin_human`` 同一個條件），其他人照常 200，只是沒有那幾筆。
擁有權判斷全用真的；沙箱沒有 users.json，只替換 ``AuthService.get_user_by_id``。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.models.user import User
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.models import lifecycle_alerts
from src.services.models.manager import ModelManager
from src.services.models.model_lifecycle import ModelLifecycle, ModelLifecycleService
from src.services.models.routes import CustomRouteDisposition, CustomRouteResolution
from src.utils import brain_persistence

if TYPE_CHECKING:
    from collections.abc import Iterator

USERS = {
    "user-a": User(id="user-a", email="a@test.com", hashed_password="...", owned_agents=["Alice"]),
    "user-b": User(id="user-b", email="b@test.com", hashed_password="...", owned_agents=["Bob"]),
}
HUMAN_ADMIN = AgentIdentity.for_role("admin-user", "admin", is_human=True)
ADMIN_ASSUMING = AgentIdentity(agent_id="Alice", human_role="admin", is_human=False)
OWNER_AGENT = AgentIdentity.for_role("Alice", "Owner")

_ALICE_MODEL = "claude-alice-old"
_BOB_MODEL = "claude-bob-old"
_REVIEWER_MODEL = "claude-reviewer-old"
_SUMMARY_MODEL = "claude-summary-old"
_ALL_DEPRECATED = (_ALICE_MODEL, _BOB_MODEL, _REVIEWER_MODEL, _SUMMARY_MODEL)
_ENDPOINT = "/v1/models/lifecycle-alerts"


def _human(user_id: str) -> AgentIdentity:
    return AgentIdentity.for_role(
        user_id, "normal", is_human=True, owned_agents=list(USERS[user_id].owned_agents)
    )


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> ModelLifecycleService:
    fresh = ModelLifecycleService()
    for name in _ALL_DEPRECATED:
        fresh.record(ModelLifecycle(name, "deprecated", None))
    monkeypatch.setattr(lifecycle_alerts, "model_lifecycle_service", fresh)
    return fresh


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, service: ModelLifecycleService) -> SimpleNamespace:
    settings = SimpleNamespace(
        brains={"Alice": _ALICE_MODEL, "Bob": _BOB_MODEL},
        review={"model": _REVIEWER_MODEL},
        summary=_SUMMARY_MODEL,
    )
    monkeypatch.setattr(lifecycle_alerts, "list_active_brains", lambda: dict(settings.brains))
    monkeypatch.setattr(lifecycle_alerts, "autonomy_service",
                        SimpleNamespace(config=SimpleNamespace(
                            get=lambda key: settings.review if key == "auto_review" else None)))
    monkeypatch.setattr(lifecycle_alerts, "sliding_window_service", SimpleNamespace(
        load_config=lambda: SimpleNamespace(summary_model=settings.summary)))
    monkeypatch.setattr(AuthService, "get_user_by_id",
                        lambda _self, user_id: USERS.get(user_id))
    return settings


@pytest.fixture
def client(configured: SimpleNamespace) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        try:
            yield test_client
        finally:
            app.dependency_overrides.clear()


def _alerts(client: TestClient, identity: AgentIdentity) -> dict[str, dict[str, Any]]:
    app.dependency_overrides[get_verified_identity] = lambda: identity
    resp = client.get(_ENDPOINT)
    assert resp.status_code == 200
    return {alert["model"]: alert for alert in resp.json()["alerts"]}


def test_without_credentials_is_401(client: TestClient) -> None:
    def rejected() -> AgentIdentity:  # 測試連線是本機，會走本機降級，所以直接換成驗證失敗
        raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN)

    app.dependency_overrides[get_verified_identity] = rejected
    assert client.get(_ENDPOINT).status_code == 401


def test_route_is_not_swallowed_by_the_model_lookup(client: TestClient) -> None:
    app.dependency_overrides[get_verified_identity] = lambda: HUMAN_ADMIN
    for path in (_ENDPOINT, "/models/lifecycle-alerts"):
        body = client.get(path).json()
        assert "alerts" in body
        assert "checking" in body


def test_normal_user_sees_only_their_own_soul(client: TestClient) -> None:
    alerts = _alerts(client, _human("user-a"))
    assert set(alerts) == {_ALICE_MODEL}
    assert alerts[_ALICE_MODEL]["souls"] == ["Alice"]


def test_souls_agent_sees_only_itself(client: TestClient) -> None:
    assert set(_alerts(client, OWNER_AGENT)) == {_ALICE_MODEL}


def test_human_admin_sees_everything(client: TestClient) -> None:
    alerts = _alerts(client, HUMAN_ADMIN)
    assert set(alerts) == set(_ALL_DEPRECATED)
    assert alerts[_REVIEWER_MODEL]["reviewer"] is True
    assert alerts[_REVIEWER_MODEL]["summarizer"] is False
    assert alerts[_SUMMARY_MODEL]["summarizer"] is True
    assert alerts[_SUMMARY_MODEL]["reviewer"] is False
    assert alerts[_REVIEWER_MODEL]["souls"] == []


@pytest.mark.parametrize("identity", [ADMIN_ASSUMING, OWNER_AGENT], ids=["assuming", "owner-agent"])
def test_reviewer_and_summarizer_need_a_human_admin(
    client: TestClient, identity: AgentIdentity
) -> None:
    alerts = _alerts(client, identity)
    assert _REVIEWER_MODEL not in alerts
    assert _SUMMARY_MODEL not in alerts


def test_shared_model_hides_the_reviewer_flag_from_normal_users(
    client: TestClient, configured: SimpleNamespace
) -> None:
    configured.review = {"model": _ALICE_MODEL}
    configured.summary = _ALICE_MODEL
    alert = _alerts(client, _human("user-a"))[_ALICE_MODEL]
    assert alert["reviewer"] is False
    assert alert["summarizer"] is False


def test_souls_on_the_same_model_are_grouped_and_sorted(
    client: TestClient, configured: SimpleNamespace
) -> None:
    configured.brains = {"Zed": _ALICE_MODEL, "Alice": _ALICE_MODEL}
    assert _alerts(client, HUMAN_ADMIN)[_ALICE_MODEL]["souls"] == ["Alice", "Zed"]


def test_status_and_date_reach_the_response(
    client: TestClient, service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.services.models.model_lifecycle.get_current_epoch", lambda: 1000.0)
    service.record(ModelLifecycle(_ALICE_MODEL, "deprecated", 2000))
    alert = _alerts(client, _human("user-a"))[_ALICE_MODEL]
    assert alert["status"] == "retiring"
    assert alert["retires_at"] == 2000


def test_configured_name_is_normalized(
    client: TestClient, configured: SimpleNamespace
) -> None:
    configured.brains = {"Alice": f"Models/{_ALICE_MODEL.upper()}-thinking"}
    assert set(_alerts(client, _human("user-a"))) == {_ALICE_MODEL}


def test_custom_connection_models_are_skipped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda _self, _m:
                        CustomRouteResolution(disposition=CustomRouteDisposition.ACTIVE_CUSTOM))
    assert _alerts(client, HUMAN_ADMIN) == {}


def test_known_inactive_custom_models_are_skipped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda _self, _m:
                        CustomRouteResolution(
                            disposition=CustomRouteDisposition.KNOWN_CUSTOM_INACTIVE))
    assert _alerts(client, HUMAN_ADMIN) == {}


def test_ambiguous_names_are_skipped_not_an_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ambiguous(_self: ModelManager, _name: str) -> CustomRouteResolution:
        raise ToukaException(ErrorCode.MODEL_NAME_AMBIGUOUS)

    monkeypatch.setattr(ModelManager, "resolve_custom_route", ambiguous)
    assert _alerts(client, HUMAN_ADMIN) == {}


@pytest.mark.parametrize("review", [{"model": ""}, {"model": None}, {}, None, "x"])
def test_unset_reviewer_is_fine(
    client: TestClient, configured: SimpleNamespace, review: Any
) -> None:
    configured.review = review
    assert _REVIEWER_MODEL not in _alerts(client, HUMAN_ADMIN)


def test_model_without_a_warning_is_left_out(
    client: TestClient, configured: SimpleNamespace
) -> None:
    configured.brains = {"Alice": "claude-fine"}
    assert _alerts(client, _human("user-a")) == {}


def test_opening_the_page_triggers_a_check(
    client: TestClient, service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(service, "trigger_check", lambda: calls.append(1))
    _alerts(client, _human("user-a"))
    assert calls == [1]


def test_checking_flag_is_reported(
    client: TestClient, service: ModelLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ModelLifecycleService, "checking", True)
    app.dependency_overrides[get_verified_identity] = lambda: _human("user-a")
    assert client.get(_ENDPOINT).json()["checking"] is True
    assert service.checking is True


def test_startup_triggers_one_check(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr("src.main.model_lifecycle_service",
                        SimpleNamespace(trigger_check=lambda: calls.append(1)))
    with TestClient(app):
        assert calls == [1]


def test_list_active_brains_uses_the_same_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brain_persistence, "_get_settings", lambda: {
        "Plain": "claude-a",
        "Nested": {"active_brain": "claude-b"},
        "NoField": {"other": 1},
        "Empty": "",
        "Legacy": "ToukaSoul-old",
    })
    assert brain_persistence.list_active_brains() == {"Plain": "claude-a", "Nested": "claude-b"}


def test_get_active_brain_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brain_persistence, "_get_settings", lambda: {
        "Plain": "claude-a", "Nested": {"active_brain": "claude-b"}, "Legacy": "ToukaSoul-old",
    })
    assert brain_persistence.get_active_brain(AgentIdentity(agent_id="Plain")) == "claude-a"
    assert brain_persistence.get_active_brain(AgentIdentity(agent_id="Nested")) == "claude-b"
    assert brain_persistence.get_active_brain(AgentIdentity(agent_id="Legacy")) is None
    assert brain_persistence.get_active_brain(AgentIdentity(agent_id="Nobody")) is None
