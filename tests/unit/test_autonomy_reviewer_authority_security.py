"""審查員設定只有人類 admin 能改（ADR-0246；R-2c ③ #1）。

`POST /v1/autonomy` 原本整份用 `require_privileged`：非 admin 擁有者帶 X-Agent-ID 切成自己的
靈魂（代理角色 Owner）、靈魂本身、Debugger 都能改 `auto_review`——`model` 決定 Auto 的資料送
到哪家廠商（ADR-0246 ④），`headless_terminal=true` 讓所有靈魂無人在場的終端指令改成送審
（ADR-0246 決定 8：要人類決定才開）。現在：

- `auto_review` 有變更時只有人類 admin 能改（`require_admin_human`，admin 代打也不算）；
  其他自主預算欄位照舊 Owner／Debugger／admin。
- 改了 `auto_review.model` 時，新值必須空白或在 reviewer-models 清單內，否則 422。

沙箱：autonomy.json 指到 tmp_path，金鑰只用假值，不連任何供應商。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
import src.routers.autonomy as autonomy_router
import src.services.autonomy as autonomy_mod
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.autonomy import autonomy_service
from src.services.identity_router import get_verified_identity

if TYPE_CHECKING:
    from collections.abc import Iterator

SOUL = "ReviewerAuthSoul"
# identity_router 產生的形狀：非 admin 擁有者帶 X-Agent-ID 切進自己的靈魂＝代理角色 Owner；
# 靈魂本身的 token 也是代理角色 Owner；admin 代打＝human_role admin、is_human False。
WHO: dict[str, AgentIdentity] = {
    "admin": AgentIdentity.for_role("rv-admin", "admin", is_human=True),
    "owner_via_x_agent_id": AgentIdentity.for_role(SOUL, "Owner", is_human=False),
    "soul_itself": AgentIdentity.for_role(SOUL, "Owner", capabilities=["heartbeat_awakening"]),
    "debugger": AgentIdentity.for_role("DebugAgent", "Debugger"),
    "admin_assuming_soul": AgentIdentity(agent_id=SOUL, human_role="admin", is_human=False),
    "owner_human": AgentIdentity.for_role(
        "rv-owner", "normal", is_human=True, owned_agents=[SOUL]
    ),
}
MODELS: list[dict[str, Any]] = [
    {"id": "claude-haiku-5-5", "name": "Claude Haiku 5.5", "owned_by": "anthropic"},
    {"id": "gpt-5", "owned_by": "openai"},  # 沒金鑰
    {"id": SOUL, "owned_by": "anthropic", "is_agent": True},  # 靈魂
    {"id": "claude-old", "owned_by": "anthropic", "enabled": False},  # 停用
]


class _FakeManager:
    def get_models(self) -> list[dict[str, Any]]:
        return [dict(m) for m in MODELS]


class _Caller:
    identity: AgentIdentity = WHO["admin"]


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    config_file = tmp_path / "autonomy.json"
    config_file.write_text(json.dumps({"quota": {"daily_max": 50}}), encoding="utf-8")
    monkeypatch.setattr(autonomy_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(autonomy_router, "model_manager", _FakeManager())
    keys = {"anthropic": "fake-key"}
    monkeypatch.setattr(autonomy_router.registry, "get_key", lambda spec: keys.get(spec.id, ""))
    autonomy_service.reload_all()
    _Caller.identity = WHO["admin"]
    app.dependency_overrides[get_verified_identity] = lambda: _Caller.identity
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.pop(get_verified_identity, None)
    autonomy_service.reload_all()


def _post(client: TestClient, who: str, **sections: Any):
    _Caller.identity = WHO[who]
    return client.post("/v1/autonomy", json={"quota": {"daily_max": 50}, **sections})


def _stored_review() -> dict[str, Any]:
    return dict(autonomy_service.config.get("auto_review") or {})


@pytest.mark.parametrize(
    "change",
    [{"model": "claude-haiku-5-5"}, {"headless_terminal": True}],
    ids=["model", "headless_terminal"],
)
@pytest.mark.parametrize(
    "who", ["owner_via_x_agent_id", "soul_itself", "debugger", "admin_assuming_soul"]
)
def test_only_a_human_admin_may_change_the_reviewer(client, who, change):
    response = _post(client, who, auto_review=change)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "AUTH_ADMIN_REQUIRED"
    assert _stored_review() == {}


def test_human_owner_without_admin_cannot_post_at_all(client):
    response = _post(client, "owner_human", auto_review={"headless_terminal": True})
    assert response.status_code == 403
    assert _stored_review() == {}


@pytest.mark.parametrize(
    "change",
    [{"model": "claude-haiku-5-5"}, {"headless_terminal": True}, {"model": ""}],
    ids=["listed_model", "headless_terminal", "blank_model"],
)
def test_human_admin_may_change_the_reviewer(client, change):
    response = _post(client, "admin", auto_review=change)
    assert response.status_code == 200, response.text
    stored = _stored_review()
    for key, value in change.items():
        assert stored[key] == value


@pytest.mark.parametrize("model", ["gpt-5", SOUL, "claude-old", "not-a-listed-model"])
def test_reviewer_model_outside_the_list_is_rejected(client, model):
    response = _post(client, "admin", auto_review={"model": model})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "VALIDATION_ERROR"
    assert response.json()["detail"]["field"] == "auto_review.model"
    assert _stored_review() == {}


@pytest.mark.parametrize("who", ["owner_via_x_agent_id", "soul_itself", "debugger"])
def test_budget_fields_keep_the_old_authority(client, who):
    """沒動 auto_review（前端整份送回原值）：預算欄位照舊 Owner／Debugger／admin 能改。"""
    assert _post(client, "admin", auto_review={"model": "claude-haiku-5-5"}).status_code == 200
    unchanged = _stored_review()
    response = _post(client, who, quota={"daily_max": 7}, auto_review=unchanged)
    assert response.status_code == 200, response.text
    assert autonomy_service.config["quota"]["daily_max"] == 7
    assert _stored_review() == unchanged


def test_unchanged_stale_model_does_not_block_budget_saves(client, monkeypatch):
    """已存的模型後來沒金鑰：沒改模型的存檔不因它被擋（審查時照樣轉人，不會放行）。"""
    assert _post(client, "admin", auto_review={"model": "claude-haiku-5-5"}).status_code == 200
    monkeypatch.setattr(autonomy_router.registry, "get_key", lambda _spec: "")
    response = _post(client, "admin", quota={"daily_max": 9}, auto_review=_stored_review())
    assert response.status_code == 200, response.text
    assert autonomy_service.config["quota"]["daily_max"] == 9


def test_admin_may_toggle_the_switch_while_an_old_model_lost_its_key(client, monkeypatch):
    """只驗新設定的模型：admin 只改 headless_terminal 時，不因舊模型沒金鑰被擋。"""
    assert _post(client, "admin", auto_review={"model": "claude-haiku-5-5"}).status_code == 200
    monkeypatch.setattr(autonomy_router.registry, "get_key", lambda _spec: "")
    change = {**_stored_review(), "headless_terminal": True}
    response = _post(client, "admin", auto_review=change)
    assert response.status_code == 200, response.text
    assert _stored_review()["headless_terminal"] is True
