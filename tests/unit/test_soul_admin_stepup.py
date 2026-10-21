"""C3 — admin 步升驗證 ＋ DELETE /v1/souls/{id}（soul-crud-repair 提案 C3；實作前＝紅）。

D2＝每請求內嵌 admin 密碼步升（verify_password＋AUDIT），D3＝人類編輯靈魂一律步升、
agent 自我存取不變，D4＝刪除冪等清全 SSOT 面（三目錄＋models/registry/brain/owned_agents/
identity_map override），D5＝DebugAgent 不可刪。直接對應使用者痛點：「建立失敗留下的孤兒
資料，連清都清不掉」——DELETE 冪等清孤兒、清完 id 可重用。全走 conftest 沙箱、不碰真實 data/。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from src.config import get_config_dir
from src.main import app
from src.models.identity import AgentIdentity
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.models import model_manager
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager
from src.services.user_service import UserService

ADMIN_PW = "StepUpPass123!"


def _app_data() -> Path:
    return Path(resource_manager.get_base_path())


def _config_dir() -> Path:
    return Path(get_config_dir())


@pytest.fixture(autouse=True)
def registry_snapshot():
    """models.json／agents_registry.json 逐測試快照還原（會話沙箱共享），缺檔以空 dict 佈置。"""
    cfg = _config_dir()
    files = [cfg / "models.json", cfg / "agents_registry.json"]
    original = {f: (f.read_text(encoding="utf-8") if f.exists() else None) for f in files}
    for f in files:
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("{}", encoding="utf-8")
    yield
    for f, content in original.items():
        if content is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(content, encoding="utf-8")


@pytest.fixture(autouse=True)
def selectable_test_brain(registry_snapshot):
    """建立靈魂會先驗主要模型（H9）：前置資料用的 claude-test-brain 種成清單裡可選的一般模型."""
    model_manager.set_models(
        [*model_manager._models_cache, {"id": "claude-test-brain", "owned_by": "anthropic"}]
    )


def _override(identity: AgentIdentity) -> TestClient:
    app.dependency_overrides[get_verified_identity] = lambda: identity
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def stepup(request):
    """真實 admin 使用者（沙箱 users.json）＋以其 id 為 agent_id 的 is_human admin 身分。

    email 以測試名稱去重——沙箱 users.json 跨測試共享（conftest 不清），固定 email 會在
    第二個測試撞 VALIDATION_ERROR「Email already registered」。
    """
    auth = AuthService()
    user = auth.create_user(f"stepup-{request.node.name}@test.com", ADMIN_PW, role="admin")
    identity = AgentIdentity.for_role(user.id, "admin", is_human=True, owned_agents=[])
    app.dependency_overrides[get_verified_identity] = lambda: identity
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, user
    app.dependency_overrides.clear()


def _create_soul(client: TestClient, agent_id: str) -> None:
    r = client.post(
        "/v1/souls",
        json={
            "agent_id": agent_id,
            "name": agent_id,
            "persona": f"我是 {agent_id}",
            "config": {"base_model": "claude-test-brain"},
        },
    )
    assert r.status_code == 200, r.text


# ── DELETE：happy path＋孤兒＋id 可重用（使用者痛點）─────────────────────────────


def test_delete_soul_removes_all_surfaces_and_id_reusable(stepup):
    client, _user = stepup
    _create_soul(client, "DelSoulA")

    r = client.request("DELETE", "/v1/souls/DelSoulA", json={"admin_password": ADMIN_PW})
    assert r.status_code == 200, r.text

    assert not (_app_data() / "soul" / "DelSoulA").exists()
    models = json.loads((_config_dir() / "models.json").read_text(encoding="utf-8"))
    assert not any(m.get("id") == "ToukaSoulDelSoulA" for m in models.get("models", []))
    registry = json.loads((_config_dir() / "agents_registry.json").read_text(encoding="utf-8"))
    assert "DelSoulA" not in registry
    brains_path = _app_data() / "vault" / "brain_settings.json"
    if brains_path.exists():
        assert "DelSoulA" not in json.loads(brains_path.read_text(encoding="utf-8"))

    # 清完同 id 可重建（＝孤兒清除後不再撞 SOUL_ALREADY_EXISTS）
    _create_soul(client, "DelSoulA")


def test_delete_orphan_soul_dir_succeeds(stepup):
    """只剩一個空孤兒目錄（建立失敗殘留形態）→ DELETE 冪等清除，200。"""
    client, _user = stepup
    (_app_data() / "soul" / "OrphanSoul").mkdir(parents=True)

    r = client.request("DELETE", "/v1/souls/OrphanSoul", json={"admin_password": ADMIN_PW})
    assert r.status_code == 200, r.text
    assert not (_app_data() / "soul" / "OrphanSoul").exists()


def test_delete_purges_owned_agents_and_identity_override(stepup):
    """完整靈魂全面清除：owned_agents 殘留＋identity_map override 也一併清乾淨。"""
    client, _user = stepup
    _create_soul(client, "DelSoulB")

    # 佈置：某使用者 owned_agents 含此靈魂＋identity_map 有 override
    auth = AuthService()
    owner_user = auth.create_user("owns-b-unique@test.com", "OwnerPass123!", role="normal")
    UserService(auth_service=auth).add_owned_agent(
        owner_user.id, AgentIdentity(agent_id="DelSoulB")
    )
    current = permission_service.identity_map.model_dump()
    current.setdefault("agent_overrides", {})["DelSoulB"] = {"role": "Owner", "capabilities": []}
    permission_service.update_identity(current)

    r = client.request("DELETE", "/v1/souls/DelSoulB", json={"admin_password": ADMIN_PW})
    assert r.status_code == 200, r.text

    assert "DelSoulB" not in AuthService().get_user_by_id(owner_user.id).owned_agents
    assert "DelSoulB" not in permission_service.identity_map.model_dump().get("agent_overrides", {})


# ── DELETE 授權：步升密碼／角色／身分／DebugAgent ────────────────────────────────


def test_delete_wrong_password_denied_and_nothing_removed(stepup):
    client, _user = stepup
    _create_soul(client, "KeepSoul")

    r = client.request("DELETE", "/v1/souls/KeepSoul", json={"admin_password": "WRONG"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "AUTH_REAUTH_FAILED"
    assert (_app_data() / "soul" / "KeepSoul").exists()  # 拒絕時零刪除


def test_delete_missing_password_rejected(stepup):
    client, _user = stepup
    _create_soul(client, "KeepSoul2")
    r = client.request("DELETE", "/v1/souls/KeepSoul2", json={})
    assert r.status_code in (403, 422)
    assert (_app_data() / "soul" / "KeepSoul2").exists()


def test_delete_non_admin_human_denied():
    """一般人類（normal）即使帶密碼也不得刪除。"""
    auth = AuthService()
    user = auth.create_user("normal-h@test.com", ADMIN_PW, role="normal")
    ident = AgentIdentity.for_role(user.id, "normal", is_human=True)
    (_app_data() / "soul" / "NSoul").mkdir(parents=True)
    with _override(ident) as c:
        r = c.request("DELETE", "/v1/souls/NSoul", json={"admin_password": ADMIN_PW})
    app.dependency_overrides.clear()
    assert r.status_code == 403
    assert (_app_data() / "soul" / "NSoul").exists()


def test_delete_agent_token_denied():
    """非人類 agent（含 assumed-admin 代打）無密碼可打＝一律不得刪除靈魂資料。"""
    agent = AgentIdentity.for_role("SomeAgent", "Owner", is_human=False)
    (_app_data() / "soul" / "ASoul").mkdir(parents=True)
    with _override(agent) as c:
        r = c.request("DELETE", "/v1/souls/ASoul", json={"admin_password": ADMIN_PW})
    app.dependency_overrides.clear()
    assert r.status_code == 403
    assert (_app_data() / "soul" / "ASoul").exists()


def test_delete_debugagent_forbidden(stepup):
    """D5：DebugAgent 是系統除錯身分，不可刪除（即使 admin 步升成功）。"""
    client, _user = stepup
    (_app_data() / "soul" / "DebugAgent").mkdir(parents=True)
    r = client.request("DELETE", "/v1/souls/DebugAgent", json={"admin_password": ADMIN_PW})
    assert r.status_code == 403
    assert (_app_data() / "soul" / "DebugAgent").exists()


def test_delete_rejects_path_escape_soul_id(stepup):
    client, _user = stepup
    sentinel = _app_data() / "legacy" / "sentinel_keep"
    sentinel.mkdir(parents=True)
    r = client.request(
        "DELETE", "/v1/souls/..%2F..%2Flegacy", json={"admin_password": ADMIN_PW}
    )
    assert r.status_code >= 400
    assert sentinel.exists()  # 逃逸路徑不得刪到監獄外的東西


# ── UPDATE：人類編輯一律步升（D3）；agent 自我存取不變 ──────────────────────────


def test_update_human_requires_stepup_password(stepup):
    client, _user = stepup
    _create_soul(client, "UpdSoul")

    r_no = client.post("/v1/souls/UpdSoul", json={"persona": "改人格"})
    assert r_no.status_code in (403, 422)  # 人類無密碼不得改

    r_ok = client.post(
        "/v1/souls/UpdSoul", json={"persona": "改人格", "admin_password": ADMIN_PW}
    )
    assert r_ok.status_code == 200, r_ok.text
    assert (_app_data() / "soul" / "UpdSoul" / "soul.md").read_text(encoding="utf-8") == "改人格"


def test_update_wrong_password_denied(stepup):
    client, _user = stepup
    _create_soul(client, "UpdSoul2")
    r = client.post(
        "/v1/souls/UpdSoul2", json={"persona": "x", "admin_password": "WRONG"}
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "AUTH_REAUTH_FAILED"


def test_update_agent_self_access_unchanged():
    """回歸鎖：agent 對自身靈魂的存取路徑（非人類）不受步升影響（無需密碼）。"""
    selfid = AgentIdentity.for_role("SelfSoulX", "Owner", is_human=False)
    app.dependency_overrides[get_verified_identity] = lambda: selfid
    (_app_data() / "soul" / "SelfSoulX").mkdir(parents=True)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/v1/souls/SelfSoulX", json={"persona": "mine"})
    app.dependency_overrides.clear()
    assert r.status_code != 403  # 自我存取過閘（無密碼）
