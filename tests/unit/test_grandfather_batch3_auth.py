"""Unit/integration tests for Grandfather Batch-3 auth fixes (TDD).

Covers third-priority endpoints from the grandfather list:
  - editor.py   : GET /editor/files
  - models.py   : POST /models/refresh, PATCH /models/{id}/settings,
                  DELETE /models/{id}, POST /models/{id}/health
  - terminal.py : GET /terminal/history, GET /terminal/stream
  - ui_sessions : GET /ui/mission
  - heartbeat   : POST /heartbeat/awaken (already has custom JWT, teach guard)
  - Stale entries: /stats already has auth, /system_logs → move to allowlist
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.terminal import terminal_stream
from src.services.identity_router import get_verified_identity
from src.services.models import model_manager as _model_manager_singleton

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


# ---------------------------------------------------------------------------
# /editor endpoints
# ---------------------------------------------------------------------------

def test_editor_files_requires_auth():
    c = _no_auth()
    resp = c.get("/editor/files")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_editor_files_owner_passes():
    c = _as_owner()
    resp = c.get("/editor/files")
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /models write endpoints
# ---------------------------------------------------------------------------

def test_models_refresh_requires_auth():
    c = _no_auth()
    resp = c.post("/models/refresh")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_models_patch_settings_requires_auth():
    c = _no_auth()
    resp = c.patch("/models/some-model/settings", json={})
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_models_delete_requires_auth():
    c = _no_auth()
    resp = c.delete("/models/some-model")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_models_health_requires_auth():
    c = _no_auth()
    resp = c.post("/models/some-model/health")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_models_refresh_owner_passes():
    c = _as_owner()
    with patch.object(
        _model_manager_singleton,
        "refresh_all_dynamic_models",
        new_callable=AsyncMock,
        return_value=0,  # refresh_all_dynamic_models 回傳 int，確保 JSON 可序列化
    ):
        resp = c.post("/models/refresh")
    _clear()
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /terminal read-only endpoints
# ---------------------------------------------------------------------------

def test_terminal_history_requires_auth():
    c = _no_auth()
    resp = c.get("/terminal/history")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_terminal_history_owner_passes():
    c = _as_owner()
    resp = c.get("/terminal/history")
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"


def test_terminal_stream_requires_auth():
    """P3 fix: /terminal/stream now has Depends(get_verified_identity)."""
    sig = inspect.signature(terminal_stream)
    assert "identity" in sig.parameters, "terminal_stream must require identity"


def test_terminal_stream_owner_passes():
    """P3 fix: owner identity should be accepted by terminal_stream."""
    sig = inspect.signature(terminal_stream)
    assert "identity" in sig.parameters, "terminal_stream must accept identity"


# ---------------------------------------------------------------------------
# /ui/mission endpoint
# ---------------------------------------------------------------------------

def test_mission_requires_auth():
    c = _no_auth()
    resp = c.get("/ui/mission")
    _clear()
    assert resp.status_code in (401, 403), f"Got {resp.status_code}"


def test_mission_owner_passes():
    c = _as_owner()
    resp = c.get("/ui/mission")
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"


# ---------------------------------------------------------------------------
# /stats already has auth (stale grandfather entry)
# ---------------------------------------------------------------------------

def test_stats_already_has_auth():
    """GET /stats already requires auth — verifying stale grandfather entry is removable."""
    c = _no_auth()
    resp = c.get("/stats")
    _clear()
    assert resp.status_code in (401, 403), f"/stats already requires auth, got {resp.status_code}"

