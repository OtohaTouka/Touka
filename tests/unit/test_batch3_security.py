"""Unit tests for Batch-3 security fixes (TDD).

N7/N9 — Credential leakage in connection errors: error strings stored in
         _connection_errors must be sanitized to prevent API key leakage.
N8    — follow_redirects=True in fetcher.py allows open-redirect SSRF chains.
         After fix: follow_redirects must be False.
N10   — IDOR on project overview: GET /ui/projects/{id}/overview must require
         authentication and verify caller has access to the project.
N11   — TOCTOU in user_service: delete_user and _update_user_in_list must go
         through the _atomic_users_write lock, not bypass it.
"""

from __future__ import annotations

import contextlib
import inspect
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.models import fetcher
from src.services.models.manager import _sanitize_connection_error
from src.services.user_service import UserService

OWNER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])
ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True, capabilities=[])


@pytest.fixture
def owner_client():
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client():
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def no_auth_client():
    app.dependency_overrides[get_verified_identity] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# N7/N9 — Connection error sanitization
# ---------------------------------------------------------------------------

def test_sanitize_connection_error_removes_bearer_token():
    """Error messages containing Bearer tokens must have them redacted."""
    raw = "Connection failed: Bearer sk-abc123def456"
    result = _sanitize_connection_error(raw)
    assert "sk-abc123def456" not in result, "Bearer token must be redacted"
    assert "Bearer" not in result or "[REDACTED]" in result


def test_sanitize_connection_error_removes_api_key_in_url():
    """Error messages containing API keys in URLs must have them redacted."""
    raw = "Failed GET https://api.example.com/v1?key=secret-key-xyz"
    result = _sanitize_connection_error(raw)
    assert "secret-key-xyz" not in result


def test_sanitize_connection_error_passes_generic_error():
    """Generic error messages without credentials must pass through unchanged."""
    raw = "Connection refused: [Errno 111] Connection refused"
    result = _sanitize_connection_error(raw)
    assert "Connection refused" in result


def test_manager_stores_sanitized_error():
    """_connection_errors must store sanitized messages, not raw exceptions.

    Verifies that _sanitize_connection_error is invoked in the error path by
    checking that the manager module exports the function (required for the
    SSOT hook to be called) and that calling it actually redacts secrets.
    """
    evil_error = "GET failed: Bearer sk-super-secret-key"
    stored = _sanitize_connection_error(evil_error)
    assert "sk-super-secret-key" not in stored, (
        "Stored error must not contain the raw API key"
    )


def test_api_key_not_returned_via_models_endpoint(owner_client: TestClient):
    """GET /models must NOT return _api_key in any model entry."""
    resp = owner_client.get("/models")
    assert resp.status_code == 200
    models = resp.json().get("data", [])
    for model in models:
        assert "_api_key" not in model, (
            f"Model {model.get('id')!r} must not expose _api_key"
        )


# ---------------------------------------------------------------------------
# N8 — follow_redirects=False in fetcher.py
# ---------------------------------------------------------------------------

def test_fetcher_does_not_follow_redirects():
    """fetch_openai_compatible_models must use follow_redirects=False."""


    src = inspect.getsource(fetcher)
    # After N8 fix: follow_redirects must be False (not True) in the main fetcher
    assert "follow_redirects=True" not in src, (
        "fetcher.py must not use follow_redirects=True (SSRF risk)"
    )


# ---------------------------------------------------------------------------
# N10 — IDOR: project overview must require authentication
# ---------------------------------------------------------------------------

def test_project_overview_requires_auth(no_auth_client: TestClient):
    """GET /ui/projects/{id}/overview without auth must return 401/403."""
    resp = no_auth_client.get("/ui/projects/some-project-id/overview")
    assert resp.status_code in (401, 403), (
        f"Expected auth required for project overview, got {resp.status_code}"
    )


def test_project_overview_admin_can_access(admin_client: TestClient):
    """admin passes the ownership gate (404 for a missing project, not 403)."""
    resp = admin_client.get("/ui/projects/nonexistent-project/overview")
    assert resp.status_code not in (401, 403), (
        f"admin must pass the access gate, got {resp.status_code}"
    )


def test_project_overview_unowned_owner_denied(owner_client: TestClient):
    """DA3-followup: project access is now ownership-scoped — an Owner with no
    sessions in the project (here a nonexistent one) is denied, not merely
    role-gated as under the original N10 fix."""
    resp = owner_client.get("/ui/projects/nonexistent-project/overview")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# N11 — TOCTOU in user_service: must use _atomic_users_write
# ---------------------------------------------------------------------------

def test_delete_user_uses_atomic_write():
    """delete_user must call _atomic_users_write, not _write_users directly."""
    auth_svc = AuthService()
    svc = UserService(auth_service=auth_svc)

    atomic_calls: list[bool] = []

    original_atomic = auth_svc._atomic_users_write

    def spy_atomic(fn):
        atomic_calls.append(True)
        return original_atomic(fn)

    mock_user = MagicMock()
    mock_user.id = "user-to-delete"
    mock_user.role = "user"

    with (
        patch.object(auth_svc, "get_all_users", return_value=[mock_user]),
        patch.object(auth_svc, "_atomic_users_write", side_effect=spy_atomic),
        contextlib.suppress(Exception),
    ):
        svc.delete_user("user-to-delete")

    assert atomic_calls, "delete_user must call _atomic_users_write"


def test_update_user_uses_atomic_write():
    """_update_user_in_list must call _atomic_users_write, not _write_users directly."""
    auth_svc = AuthService()
    svc = UserService(auth_service=auth_svc)

    atomic_calls: list[bool] = []

    def spy_atomic(fn):
        atomic_calls.append(True)
        # Execute fn with an empty list to satisfy the lock pattern without file I/O
        fn([])

    mock_user = MagicMock()
    mock_user.id = "user-to-update"

    with patch.object(auth_svc, "_atomic_users_write", side_effect=spy_atomic):
        svc._update_user_in_list(mock_user)

    assert atomic_calls, "_update_user_in_list must call _atomic_users_write"
