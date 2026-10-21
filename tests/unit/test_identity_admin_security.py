"""TDD — DA2: POST /identity must require admin + reject invalid role strings.

The identity map (`identity_map.json`) defines which agents hold which role,
including "admin". Allowing an *Owner* to write it (the previous behaviour) lets
a lower-privilege Owner promote any agent to admin = privilege escalation. The
write is now admin-only, and the free-form `role` field is whitelist-validated.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity

ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True, capabilities=[])
OWNER = AgentIdentity.for_role(agent_id="OwnerAgent", role="Owner", capabilities=[])
# ADR-0245：Guest 退役，低權限樣本改用 Base。
GUEST = AgentIdentity.for_role(agent_id="LowAgent", role="Base", capabilities=[])

# Minimal valid IdentityMapConfig body (BasePolicyModel fields are all optional).
VALID_BODY = {"tag_rules": {}, "agent_overrides": {}}


@pytest.fixture
def admin_client():
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def owner_client():
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def guest_client():
    app.dependency_overrides[get_verified_identity] = lambda: GUEST
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- POST /identity authorization (now admin-only) ----

def test_post_identity_owner_denied(owner_client: TestClient):
    """Owner must NOT be able to rewrite the identity map (escalation fix)."""
    assert owner_client.post("/identity", json=VALID_BODY).status_code == 403


def test_post_identity_guest_denied(guest_client: TestClient):
    assert guest_client.post("/identity", json=VALID_BODY).status_code == 403


def test_post_identity_admin_passes_auth(admin_client: TestClient):
    """admin still passes the auth gate (regression guard)."""
    assert admin_client.post("/identity", json=VALID_BODY).status_code not in (401, 403)


def test_get_identity_owner_still_allowed(owner_client: TestClient):
    """Reading the map stays permissive (Owner/Debugger/admin)."""
    assert owner_client.get("/identity").status_code == 200


# ---- role-field whitelist validation (defense in depth) ----

def test_post_identity_rejects_invalid_role(admin_client: TestClient):
    body = {"tag_rules": {}, "agent_overrides": {"X": {"role": "bogus", "capabilities": []}}}
    assert admin_client.post("/identity", json=body).status_code == 422


def test_post_identity_accepts_valid_role(admin_client: TestClient):
    # ADR-0245：覆寫只能寫給靈魂、DebugAgent、DispatchAgent。
    body = {
        "tag_rules": {},
        "agent_overrides": {"DebugAgent": {"role": "Owner", "capabilities": []}},
    }
    assert admin_client.post("/identity", json=body).status_code not in (401, 403, 422)


# ---- P6-a: identity_map 寫入只接受可指派的 AGENT 角色（拒人類 admin/normal＋內部 System） ----


def test_post_identity_rejects_human_admin_role(admin_client: TestClient):
    """P6-a（刻意行為變更）：admin 是人類角色——不可經 identity_map 指派給 agent
    （否則等同開後門把 agent 提升成 admin）。拆分前 admin ∈ VALID_ROLES 會放行（200），
    拆分後只驗可指派 AGENT 角色集 → 422。"""
    body = {"tag_rules": {}, "agent_overrides": {"X": {"role": "admin", "capabilities": []}}}
    assert admin_client.post("/identity", json=body).status_code == 422


def test_post_identity_rejects_internal_system_role(admin_client: TestClient):
    """System 為內部專用角色，identity_map 不可指派（AGENT_ROLES 含 System 但排除於可指派集）。"""
    body = {"tag_rules": {}, "agent_overrides": {"X": {"role": "System", "capabilities": []}}}
    assert admin_client.post("/identity", json=body).status_code == 422


@pytest.mark.parametrize("role", ["Subagent", "Guest"])
def test_post_identity_rejects_subagent_and_retired_guest(admin_client: TestClient, role: str):
    """ADR-0245：Subagent 只給子代理（同 System 不可指派）；Guest 已退役。"""
    body = {"tag_rules": {}, "agent_overrides": {"X": {"role": role, "capabilities": []}}}
    assert admin_client.post("/identity", json=body).status_code == 422
