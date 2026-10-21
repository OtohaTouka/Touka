"""TDD tests for Legacy Stats URL agent_id fix.

Bug: GET /stats/legacy/{agent_id} and POST /stats/legacy/{agent_id}/sync
     ignored the URL {agent_id} path parameter and always used the JWT
     identity instead.

Fix:
  - Both endpoints now accept agent_id: str path parameter.
  - Authorization: is_owner() check (admin/Debugger bypass; human owner;
    same-agent).
  - All service calls and path calculations use the URL agent_id (as
    AgentIdentity.from_str(agent_id)), NOT the JWT caller's identity.

RED tests (fail before fix):
  - Rejection tests: unauth / unrelated user must get 403
  - Regression: response agent_id must match URL param, not JWT agent_id

Happy-path tests (may already pass, confirm fix is non-breaking):
  - Admin / Debugger bypass auth check
"""
from __future__ import annotations

from unittest.mock import create_autospec, patch

import pytest
from fastapi.testclient import TestClient
from memory_server.services.legacy_store import LegacyStore
from memory_server.services.legacy_sync import LegacySyncService
from memory_server.services.manager import MemoryManager
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity

# ---------------------------------------------------------------------------
# Identities – only DebugAgent used as target to avoid touching real data
# ---------------------------------------------------------------------------
DEBUG_AGENT = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger", is_human=False)
ADMIN = AgentIdentity.for_role(agent_id="admin_user", role="admin", is_human=True)
UNRELATED = AgentIdentity.for_role(agent_id="stranger", role="user", is_human=True, owned_agents=[])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _cleanup_overrides():
    """Guarantee dependency overrides are cleared after every test."""
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def mock_memory_manager():
    """Patch MemoryManager with safe defaults; yields the mock instance."""
    mock_mm = create_autospec(MemoryManager, instance=True)
    mock_store = create_autospec(LegacyStore, instance=True)
    mock_store.get_stats.return_value = {"session_count": 2, "message_count": 10}
    mock_store.db_path = "/fake/db.db"
    sync_svc = create_autospec(LegacySyncService, instance=True)
    mock_mm.get_legacy_sync_service.return_value = sync_svc
    sync_svc.get_store.return_value = mock_store
    sync_svc.sync_all_json_to_db.return_value = {
        "synced": 0,
        "errors": 0,
        "total_in_db": {"session_count": 2, "message_count": 10},
    }
    with patch("memory_server.services.manager.MemoryManager", return_value=mock_mm):
        yield mock_mm


# ---------------------------------------------------------------------------
# A. Auth rejection tests  (RED before fix — no auth check exists yet)
# ---------------------------------------------------------------------------

def test_stats_no_auth_rejected():
    """GET /stats/legacy/DebugAgent without JWT must return 401/403."""
    app.dependency_overrides[get_verified_identity] = lambda: None
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/v1/stats/legacy/DebugAgent")
    assert resp.status_code in (401, 403), (
        f"Expected 401/403 for unauthenticated request, got {resp.status_code}"
    )


def test_sync_no_auth_rejected():
    """POST /stats/legacy/DebugAgent/sync without JWT must return 401/403."""
    app.dependency_overrides[get_verified_identity] = lambda: None
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/v1/stats/legacy/DebugAgent/sync")
    assert resp.status_code in (401, 403), (
        f"Expected 401/403 for unauthenticated request, got {resp.status_code}"
    )


def test_stats_unrelated_user_rejected():
    """Human user who does not own DebugAgent must be denied stats access."""
    app.dependency_overrides[get_verified_identity] = lambda: UNRELATED
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/v1/stats/legacy/DebugAgent")
    assert resp.status_code in (401, 403), (
        f"Expected 403 for unrelated user, got {resp.status_code}"
    )


def test_sync_unrelated_user_rejected():
    """Human user who does not own DebugAgent must be denied sync."""
    app.dependency_overrides[get_verified_identity] = lambda: UNRELATED
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/v1/stats/legacy/DebugAgent/sync")
    assert resp.status_code in (401, 403), (
        f"Expected 403 for unrelated user, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# B. Happy-path auth tests  (confirm admin / Debugger bypass works)
# ---------------------------------------------------------------------------

def test_stats_debugger_passes_auth(mock_memory_manager):
    """Debugger role bypasses ownership check for stats."""
    app.dependency_overrides[get_verified_identity] = lambda: DEBUG_AGENT
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/v1/stats/legacy/DebugAgent")
    assert resp.status_code not in (401, 403), (
        f"Debugger must not be rejected, got {resp.status_code}"
    )


def test_sync_debugger_passes_auth(mock_memory_manager):
    """Debugger role bypasses ownership check for sync."""
    app.dependency_overrides[get_verified_identity] = lambda: DEBUG_AGENT
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/v1/stats/legacy/DebugAgent/sync")
    assert resp.status_code not in (401, 403), (
        f"Debugger must not be rejected, got {resp.status_code}"
    )


def test_stats_admin_passes_auth(mock_memory_manager):
    """Admin (is_human=True, role=admin) bypasses ownership check for stats."""
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/v1/stats/legacy/DebugAgent")
    assert resp.status_code not in (401, 403), (
        f"Admin must not be rejected, got {resp.status_code}"
    )


def test_sync_admin_passes_auth(mock_memory_manager):
    """Admin bypasses ownership check for sync."""
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/v1/stats/legacy/DebugAgent/sync")
    assert resp.status_code not in (401, 403), (
        f"Admin must not be rejected, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# C. Core regression tests  (RED before fix — URL agent_id currently ignored)
# ---------------------------------------------------------------------------

def test_stats_response_agent_id_matches_url(mock_memory_manager):
    """response['agent_id'] must equal the URL path param, not the caller's ID.

    Before fix: identity.agent_id ('admin_user') was used → response showed
                'admin_user' even when the URL requested 'DebugAgent'.
    After fix:  URL param 'DebugAgent' must appear in the response.
    """
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/v1/stats/legacy/DebugAgent")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("agent_id") == "DebugAgent", (
        f"response agent_id must be 'DebugAgent' (URL param), "
        f"got '{data.get('agent_id')}' — JWT identity must NOT bleed into response"
    )


def test_service_called_with_url_agent_id_not_caller(mock_memory_manager):
    """MemoryManager.get_store() must receive AgentIdentity for the URL agent.

    Before fix: get_store() was called with the JWT identity (admin_user).
    After fix:  get_store() must be called with AgentIdentity(agent_id='DebugAgent').
    """
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/v1/stats/legacy/DebugAgent")
    assert resp.status_code == 200

    sync_svc = mock_memory_manager.get_legacy_sync_service.return_value
    calls = sync_svc.get_store.call_args_list
    assert len(calls) == 1, f"get_store must be called exactly once, got {len(calls)}"
    used_identity: AgentIdentity = calls[0][0][0]
    assert used_identity.agent_id == "DebugAgent", (
        f"get_store must be called with agent_id='DebugAgent', "
        f"got '{used_identity.agent_id}'"
    )


def test_sync_service_called_with_url_agent_id(mock_memory_manager):
    """sync_all_json_to_db() must receive AgentIdentity for the URL agent.

    Before fix: sync_all_json_to_db() was called with the JWT identity.
    After fix:  must be called with AgentIdentity(agent_id='DebugAgent').
    """
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/v1/stats/legacy/DebugAgent/sync")
    assert resp.status_code == 200

    sync_svc = mock_memory_manager.get_legacy_sync_service.return_value
    calls = sync_svc.sync_all_json_to_db.call_args_list
    assert len(calls) == 1, f"sync_all_json_to_db must be called exactly once, got {len(calls)}"
    used_identity: AgentIdentity = calls[0][0][0]
    assert used_identity.agent_id == "DebugAgent", (
        f"sync_all_json_to_db must be called with agent_id='DebugAgent', "
        f"got '{used_identity.agent_id}'"
    )
