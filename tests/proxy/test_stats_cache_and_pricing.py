"""/stats 的快取命中率與價目表資訊（ADR-0311）。

命中率跟著 token 用量的範圍（``can_access_session``）；沒有具體價格的模型跟著模型下架提醒的範圍：
靈魂比照 /stats，Auto 審查員與摘要員只給人類 admin。擁有權判斷全用真的；沙箱沒有 users.json，
只替換 ``AuthService.get_user_by_id``。
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.models.user import User
from src.routers import stats as stats_router
from src.services import telemetry
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.models import lifecycle_alerts
from src.services.models.manager import ModelManager
from src.services.models.routes import CustomRouteDisposition, CustomRouteResolution
from src.services.providers import pricing
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from collections.abc import Iterator

USERS = {
    "user-a": User(id="user-a", email="a@test.com", hashed_password="...", owned_agents=["Alice"]),
    "user-b": User(id="user-b", email="b@test.com", hashed_password="...", owned_agents=["Bob"]),
}
HUMAN_ADMIN = AgentIdentity.for_role("admin-user", "admin", is_human=True)
ADMIN_ASSUMING = AgentIdentity(agent_id="Alice", human_role="admin", is_human=False)
OWNER_AGENT = AgentIdentity.for_role("Alice", "Owner")

_TODAY = 20_402
_ALICE_MODEL = "claude-alice-new"  # 家族最高價
_BOB_MODEL = "grok-bob-new"  # 家族最高價
_REVIEWER_MODEL = "gemini-reviewer-new"  # 家族最高價
_SUMMARY_MODEL = "claude-opus-5-5"  # 有具體價格，不列
_CUSTOM_MODEL = "claude-custom-new"


def _human(user_id: str) -> AgentIdentity:
    return AgentIdentity.for_role(
        user_id, "normal", is_human=True, owned_agents=list(USERS[user_id].owned_agents)
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(telemetry, "get_current_epoch", lambda: _TODAY * 86_400 + 60)
    monkeypatch.setattr(pricing.config_utils, "read_config_json", lambda _name: None)
    monkeypatch.setattr(AuthService, "get_user_by_id", lambda _self, user_id: USERS.get(user_id))
    monkeypatch.setattr(lifecycle_alerts, "soul_service", SimpleNamespace(
        is_toukasoul=lambda agent: agent in {"Alice", "Bob"}))
    monkeypatch.setattr(lifecycle_alerts, "list_active_brains",
                        lambda: {"Alice": _ALICE_MODEL, "Bob": _BOB_MODEL})
    monkeypatch.setattr(lifecycle_alerts, "autonomy_service", SimpleNamespace(
        config=SimpleNamespace(
            get=lambda key: {"model": _REVIEWER_MODEL} if key == "auto_review" else None)))
    monkeypatch.setattr(lifecycle_alerts, "sliding_window_service", SimpleNamespace(
        load_config=lambda: SimpleNamespace(summary_model=_SUMMARY_MODEL)))
    with TestClient(app) as test_client:
        try:
            yield test_client
        finally:
            app.dependency_overrides.clear()


def _stats(client: TestClient, identity: AgentIdentity) -> dict[str, Any]:
    app.dependency_overrides[get_verified_identity] = lambda: identity
    response = client.get("/v1/stats")
    assert response.status_code == 200
    return response.json()


def _usage(read: int, prompt: int) -> dict[str, Any]:
    return {"prompt_tokens": prompt, "total_cost_usd": 0.1, "daily": {str(_TODAY): {
        "prompt_tokens": prompt, "completion_tokens": 0,
        "cache_read_input_tokens": read, "cache_creation_input_tokens": 0,
    }}}


@pytest.fixture
def usage_files() -> Iterator[None]:
    stats_root = Path(resource_manager.get_base_path()) / "stats"
    bodies: dict[str, Any] = {
        "Alice": {"Anthropic": _usage(read=3, prompt=1), "summarizer": _usage(read=0, prompt=0)},
        "Bob": {"Anthropic": _usage(read=1, prompt=1)},
    }
    originals: dict[Path, bytes | None] = {}
    created: list[Path] = []
    for agent, body in bodies.items():
        path = stats_root / agent / "token_usage.json"
        originals[path] = path.read_bytes() if path.exists() else None
        if not path.parent.exists():
            path.parent.mkdir(parents=True)
            created.append(path.parent)
        resource_manager.write_json(path, body, indent=2)
    try:
        yield
    finally:
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        for directory in created:
            with contextlib.suppress(OSError):
                directory.rmdir()


def _only(rates: dict[str, Any]) -> dict[str, Any]:
    """沙箱可能還有別的測試留下的用量檔，只核對這兩個。"""
    return {agent: rates[agent] for agent in ("Alice", "Bob") if agent in rates}


def test_admin_sees_every_souls_rate(client: TestClient, usage_files: None) -> None:
    assert _only(_stats(client, HUMAN_ADMIN)["cache_hit_rates"]) == {
        "Alice": {"Anthropic": 0.75, "summarizer": None},
        "Bob": {"Anthropic": 0.5},
    }


def test_owner_agent_sees_only_its_own_rate(client: TestClient, usage_files: None) -> None:
    assert _stats(client, OWNER_AGENT)["cache_hit_rates"] == {
        "Alice": {"Anthropic": 0.75, "summarizer": None},
    }


@pytest.mark.parametrize(
    "identity", [_human("user-a"), ADMIN_ASSUMING], ids=["user", "admin-assuming"]
)
def test_rates_are_not_wider_than_token_usage(
    client: TestClient, usage_files: None, identity: AgentIdentity
) -> None:
    body = _stats(client, identity)
    assert body["cache_hit_rates"] == {}
    assert body["token_usage"] == {}


def test_a_usage_file_without_labels_does_not_break_stats(client: TestClient) -> None:
    path = Path(resource_manager.get_base_path()) / "stats" / "Alice" / "token_usage.json"
    original = path.read_bytes() if path.exists() else None
    resource_manager.make_dir(path.parent)
    path.write_text('{"total_tokens": 5}', encoding="utf-8")
    try:
        assert _stats(client, OWNER_AGENT)["cache_hit_rates"] == {"Alice": {}}
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)


def test_pricing_carries_the_verified_date(client: TestClient) -> None:
    assert _stats(client, HUMAN_ADMIN)["pricing"]["verified_at"] == pricing.PRICING_VERIFIED_AT


def test_admin_sees_all_models_without_their_own_price(client: TestClient) -> None:
    assert _stats(client, HUMAN_ADMIN)["pricing"]["estimated_models"] == sorted(
        [_ALICE_MODEL, _BOB_MODEL, _REVIEWER_MODEL]
    )


def test_owner_agent_sees_only_its_own_soul_model(client: TestClient) -> None:
    assert _stats(client, OWNER_AGENT)["pricing"]["estimated_models"] == [_ALICE_MODEL]


@pytest.mark.parametrize(
    ("identity", "models"),
    [(_human("user-a"), [_ALICE_MODEL]), (ADMIN_ASSUMING, [_ALICE_MODEL, _BOB_MODEL])],
    ids=["user", "admin-assuming"],
)
def test_reviewer_and_summarizer_stay_with_human_admins(
    client: TestClient, identity: AgentIdentity, models: list[str]
) -> None:
    assert _stats(client, identity)["pricing"]["estimated_models"] == sorted(models)


def test_a_custom_connection_is_not_listed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle_alerts, "list_active_brains", lambda: {"Alice": _CUSTOM_MODEL})
    # 要換類別上的方法：換共用物件的話，還原時會在物件上留一份，後面的測試就換不到了
    monkeypatch.setattr(
        ModelManager, "resolve_custom_route",
        lambda _self, _model: CustomRouteResolution(
            disposition=CustomRouteDisposition.ACTIVE_CUSTOM),
    )
    assert _stats(client, OWNER_AGENT)["pricing"]["estimated_models"] == []


@pytest.mark.parametrize(
    ("config", "expected"),
    [(None, False), ({"pricing": {"x": {"input": 1.0, "output": 2.0}}}, True)],
    ids=["no-override", "override"],
)
def test_pricing_says_when_there_are_overrides(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, config: object, expected: bool
) -> None:
    monkeypatch.setattr(pricing.config_utils, "read_config_json", lambda _name: config)
    assert _stats(client, HUMAN_ADMIN)["pricing"]["has_override"] is expected


def test_a_failure_in_pricing_does_not_break_the_rest(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_identity: AgentIdentity) -> None:
        raise OSError

    monkeypatch.setattr(stats_router, "pricing_status", boom)
    body = _stats(client, HUMAN_ADMIN)
    assert body["pricing"] is None
    assert "token_usage" in body
