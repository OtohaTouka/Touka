"""Unit/integration tests for Grandfather Batch-1 auth fixes (TDD).

Covers first-priority endpoints that were in the grandfather list:
  - system.py  : GET /system/backups, POST /system/backup/run, POST /system/restore/run
  - identity.py: GET /identity, POST /identity
  - permissions: GET /permissions, POST /permissions
  - scheduler  : GET /scheduler/schedules, POST, DELETE, PUT, POST/toggle
  - autonomy   : GET /autonomy, POST /autonomy
  - skills     : GET /skills/list, POST /skills, DELETE /skills/{id}
  - souls      : POST /souls/{soul_id} (update_soul bypass)

Also verifies:
  - /config endpoints already have auth (keep grandfather entries removable)
  - /agents endpoint already has auth (keep grandfather entries removable)
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


# ── Fixtures ──────────────────────────────────────────────────────────────────

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
# /system endpoints
# ---------------------------------------------------------------------------

def test_system_backups_requires_auth():
    c = _no_auth()
    resp = c.get("/system/backups")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_system_backup_run_requires_auth():
    c = _no_auth()
    resp = c.post("/system/backup/run")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_system_restore_requires_auth():
    c = _no_auth()
    resp = c.post("/system/restore/run", json={"backup_file": "x"})
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_system_backups_admin_passes_auth():
    c = _as_admin()
    resp = c.get("/system/backups")
    _clear()
    assert resp.status_code not in (401, 403), f"Admin must pass auth, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /identity endpoints
# ---------------------------------------------------------------------------

def test_identity_get_requires_auth():
    c = _no_auth()
    resp = c.get("/identity")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_identity_post_requires_auth():
    c = _no_auth()
    resp = c.post("/identity", json={})
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_identity_get_owner_passes_auth():
    c = _as_owner()
    resp = c.get("/identity")
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /permissions endpoints
# ---------------------------------------------------------------------------

def test_permissions_get_requires_auth():
    c = _no_auth()
    resp = c.get("/permissions")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_permissions_post_requires_auth():
    c = _no_auth()
    resp = c.post("/permissions", json={})
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_permissions_get_admin_passes_auth():
    c = _as_admin()
    resp = c.get("/permissions")
    _clear()
    assert resp.status_code not in (401, 403), f"Admin must pass, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /scheduler endpoints
# ---------------------------------------------------------------------------

def test_scheduler_get_requires_auth():
    c = _no_auth()
    resp = c.get("/scheduler/schedules")
    _clear()
    # 404 = router not mounted (not exposed); 401/403 = mounted with auth (secured)
    assert resp.status_code in (401, 403, 404), f"Got {resp.status_code}"


def test_scheduler_post_requires_auth():
    c = _no_auth()
    resp = c.post("/scheduler/schedules", json={})
    _clear()
    assert resp.status_code in (401, 403, 404), f"Got {resp.status_code}"


def test_scheduler_delete_requires_auth():
    c = _no_auth()
    resp = c.delete("/scheduler/schedules/some-id")
    _clear()
    assert resp.status_code in (401, 403, 404), f"Got {resp.status_code}"


def test_scheduler_put_requires_auth():
    c = _no_auth()
    resp = c.put("/scheduler/schedules/some-id", json={})
    _clear()
    assert resp.status_code in (401, 403, 404), f"Got {resp.status_code}"


def test_scheduler_toggle_requires_auth():
    c = _no_auth()
    resp = c.post("/scheduler/schedules/some-id/toggle")
    _clear()
    assert resp.status_code in (401, 403, 404), f"Got {resp.status_code}"


def test_scheduler_get_owner_passes_auth():
    c = _as_owner()
    resp = c.get("/scheduler/schedules")
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /autonomy endpoints
# ---------------------------------------------------------------------------

def test_autonomy_get_requires_auth():
    c = _no_auth()
    resp = c.get("/autonomy")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_autonomy_post_requires_auth():
    c = _no_auth()
    resp = c.post("/autonomy", json={})
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_autonomy_get_owner_passes_auth():
    c = _as_owner()
    resp = c.get("/autonomy")
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /skills endpoints
# ---------------------------------------------------------------------------

def test_skills_list_requires_auth():
    c = _no_auth()
    resp = c.get("/skills/list")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_skills_post_requires_auth():
    c = _no_auth()
    resp = c.post("/skills", json={})
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_skills_delete_requires_auth():
    c = _no_auth()
    resp = c.delete("/skills/some-skill")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_skills_list_owner_passes_auth():
    c = _as_owner()
    resp = c.get("/skills/list")
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /souls - update_soul endpoint (POST /souls/{soul_id})
# ---------------------------------------------------------------------------

def test_souls_update_requires_auth():
    """POST /souls/{soul_id} must not silently assign Owner identity."""
    c = _no_auth()
    resp = c.post("/souls/DebugAgent", json={})
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_souls_update_owner_passes_auth():
    c = _as_owner()
    resp = c.post("/souls/DebugAgent", json={})
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"
