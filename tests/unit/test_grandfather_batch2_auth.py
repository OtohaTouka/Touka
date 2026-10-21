"""Unit/integration tests for Grandfather Batch-2 auth fixes (TDD).

Covers second-priority endpoints from the grandfather list:
  - bots.py     : GET /bots, POST /bots
  - custom_models: GET /ui/custom_models, POST/DELETE /ui/custom_models/{id}
  - mcp.py      : GET /mcp/list, POST /mcp/ui/config, GET /mcp/hub,
                  POST /mcp/hub/sync
  - ui_sessions : GET/POST/DELETE /ui/sessions/*, GET/POST /ui/projects/*,
                  GET/POST /ui/settings/*
  - group_settings: verify already has auth (stale grandfather entry)
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity

OWNER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])
ADMIN = AgentIdentity.for_role(agent_id="DebugAgent", role="admin", capabilities=[])


def _override(identity):
    app.dependency_overrides[get_verified_identity] = lambda: identity


def _clear():
    app.dependency_overrides.clear()


def _no_auth():
    _override(None)
    return TestClient(app, raise_server_exceptions=False)


def _as_owner():
    _override(OWNER)
    return TestClient(app, raise_server_exceptions=False)


def _as_admin():
    _override(ADMIN)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# /bots endpoints
# ---------------------------------------------------------------------------

def test_bots_get_requires_auth():
    c = _no_auth()
    resp = c.get("/bots")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_bots_post_requires_auth():
    c = _no_auth()
    resp = c.post("/bots", json={})
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_bots_get_owner_passes():
    c = _as_owner()
    resp = c.get("/bots")
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /ui/custom_models endpoints
# ---------------------------------------------------------------------------

def test_custom_models_list_requires_auth():
    c = _no_auth()
    resp = c.get("/ui/custom_models")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_custom_models_get_single_requires_auth():
    c = _no_auth()
    resp = c.get("/ui/custom_models/some-model")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_custom_models_post_requires_auth():
    c = _no_auth()
    resp = c.post("/ui/custom_models/some-model", json={})
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_custom_models_delete_requires_auth():
    c = _no_auth()
    resp = c.delete("/ui/custom_models/some-model")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_custom_models_owner_passes():
    c = _as_owner()
    resp = c.get("/ui/custom_models")
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /mcp endpoints (excluding /hub/install already fixed)
# ---------------------------------------------------------------------------

def test_mcp_list_requires_auth():
    c = _no_auth()
    resp = c.get("/mcp/list")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_mcp_ui_config_post_requires_auth():
    c = _no_auth()
    resp = c.post("/mcp/ui/config", json={"mcpServers": {}})
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_mcp_ui_config_get_requires_auth():
    c = _no_auth()
    resp = c.get("/mcp/ui/config")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_mcp_hub_requires_auth():
    c = _no_auth()
    resp = c.get("/mcp/hub")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_mcp_hub_sync_requires_auth():
    c = _no_auth()
    resp = c.post("/mcp/hub/sync", json={})
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_mcp_owner_passes_list():
    c = _as_owner()
    resp = c.get("/mcp/list")
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /ui/sessions endpoints
# ---------------------------------------------------------------------------

def test_sessions_list_requires_auth():
    c = _no_auth()
    resp = c.get("/ui/sessions")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_sessions_get_single_requires_auth():
    c = _no_auth()
    resp = c.get("/ui/sessions/some-id")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_sessions_post_requires_auth():
    c = _no_auth()
    resp = c.post("/ui/sessions/some-id", json={})
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_sessions_delete_requires_auth():
    c = _no_auth()
    resp = c.delete("/ui/sessions/some-id")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_sessions_owner_passes():
    c = _as_owner()
    resp = c.get("/ui/sessions")
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /ui/projects endpoints
# ---------------------------------------------------------------------------

def test_projects_list_requires_auth():
    c = _no_auth()
    resp = c.get("/ui/projects/list")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_projects_save_requires_auth():
    c = _no_auth()
    resp = c.post("/ui/projects/save", json={})
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_projects_delete_requires_auth():
    c = _no_auth()
    resp = c.delete("/ui/projects/some-id")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_projects_owner_passes():
    c = _as_owner()
    resp = c.get("/ui/projects/list")
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /ui/settings endpoints
# ---------------------------------------------------------------------------

def test_settings_workspace_get_requires_auth():
    c = _no_auth()
    resp = c.get("/ui/settings/workspace")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_settings_workspace_post_requires_auth():
    c = _no_auth()
    resp = c.post("/ui/settings/workspace", json={})
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_settings_owner_passes():
    c = _as_owner()
    resp = c.get("/ui/settings/workspace")
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /group_settings - should already have auth (stale grandfather entry)
# ---------------------------------------------------------------------------

def test_group_whitelist_already_has_auth():
    """GET /group/whitelist already has auth — verify it returns 403 without identity."""
    c = _no_auth()
    resp = c.get("/group/whitelist")
    _clear()
    assert resp.status_code in (401, 403), f"/group/whitelist should already require auth, got {resp.status_code}"


def test_group_whitelist_post_admin_passes():
    """P1 (owner decision): human admins (role 'admin'), not just RBAC 'Owner', can manage the whitelist."""
    c = _as_admin()
    resp = c.post("/group/whitelist", json={})
    _clear()
    assert resp.status_code not in (401, 403), f"admin must pass whitelist save, got {resp.status_code}"


def test_group_whitelist_post_normal_forbidden():
    """P1: a non-admin/non-Owner identity (normal human) is still rejected (403)."""
    _override(AgentIdentity.for_role(agent_id="someuser", role="normal", capabilities=[]))
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/group/whitelist", json={})
    _clear()
    assert resp.status_code == 403, f"normal must be forbidden, got {resp.status_code}"
