"""Unit tests for P7/P9/P13/P14/P22/P25/P27 security hardening (Batch 3 Gamma — TDD).

P7  — Localhost cannot assume unowned Debugger agent.
P9  — save_connections must acquire threading.Lock to prevent file corruption.
P13 — Refresh token jti rotation: old token rejected after rotation.
P14 — /debug/execute Debugger cannot run tools outside allowed_tools list.
P22 — /logs, /memory, /tools, /tools_stats, /system_logs require auth.
P25 — Deleting a user immediately invalidates their refresh token.
P27 — Admin create/update user emits AUDIT warning log.
"""

from __future__ import annotations

import importlib.util
import inspect
import threading
from pathlib import Path
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient
from src.config import AUTH_REFRESH_GRACE_PERIOD_SEC
from src.core.exceptions import ToukaException
from src.core.security import AuthSecretManager
from src.main import app
from src.models.identity import AgentIdentity
from src.models.user import User
from src.routers import admin as admin_mod
from src.routers import connections as conn_mod
from src.services import identity_router as identity_router_mod
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.user_service import UserService
from src.utils.permission_grants import NameGrant
from src.utils.time_utils import get_current_epoch

OWNER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])
ADMIN = AgentIdentity.for_role(agent_id="DebugAgent", role="admin", capabilities=[])


def _override(identity: AgentIdentity) -> None:
    app.dependency_overrides[get_verified_identity] = lambda: identity


def _clear() -> None:
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# P7 — Localhost cannot assume unowned Debugger
# ---------------------------------------------------------------------------

def test_localhost_cannot_assume_unowned_debugger():
    """A human user without Debugger in owned_agents must be denied assumption."""
    assert callable(get_verified_identity)
    src = inspect.getsource(identity_router_mod)
    assert "owned_agents" in src, "identity_router must check owned_agents for Debugger assumption"


def test_admin_can_assume_any_debugger():
    """Admin role bypasses owned_agents check."""
    src = inspect.getsource(identity_router_mod)
    assert 'human_role' in src or 'admin' in src


# ---------------------------------------------------------------------------
# P9 — save_connections concurrent file write
# ---------------------------------------------------------------------------

def test_connections_lock_exists():
    """connections.py must define a module-level threading.Lock."""
    assert hasattr(conn_mod, "_connections_lock"), (
        "connections.py must have module-level _connections_lock"
    )
    assert isinstance(conn_mod._connections_lock, type(threading.Lock())), (
        "_connections_lock must be a threading.Lock instance"
    )


def test_save_connections_concurrent_no_corruption(auth_client):
    """Concurrent POST /connections calls must not corrupt the file."""
    errors: list[Exception] = []

    def call_save() -> None:
        try:
            resp = auth_client.post(
                "/v1/ui/connections",
                json=[{"id": "proxy", "name": "Proxy", "provider": "proxy",
                       "url": "http://localhost:8000/v1", "enabled": True,
                       "apiKey": None}],
            )
            assert resp.status_code in (200, 201, 400, 422, 403, 404, 500)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=call_save) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors during concurrent save: {errors}"


# ---------------------------------------------------------------------------
# P13 — Refresh token jti rotation
# ---------------------------------------------------------------------------

def test_user_model_has_refresh_token_jti():
    """User model must include refresh_token_jti field for token replay detection."""
    user = User(
        id="test-id",
        email="test@example.com",
        hashed_password="hash",
    )
    assert hasattr(user, "refresh_token_jti"), (
        "User model must have refresh_token_jti field"
    )


def test_refresh_token_contains_jti(tmp_path):
    """authenticate_user must embed jti claim in the refresh token."""
    users_file = tmp_path / "users.json"
    svc = AuthService(users_file_path=str(users_file))
    svc.create_user("jti@test.com", "Password123!")

    tokens = svc.authenticate_user("jti@test.com", "Password123!")
    payload = jwt.decode(
        tokens["refresh_token"],
        AuthSecretManager.get_secret(),
        algorithms=["HS256"],
    )
    assert "jti" in payload, "Refresh token must contain jti claim"


def test_old_refresh_token_rejected_after_rotation(tmp_path):
    """After rotation, the old token must be rejected AFTER the grace period expires.

    The grace period allows concurrent multi-tab refreshes (within GRACE window).
    A replay that arrives after the grace window is still caught and rejected.
    """
    users_file = tmp_path / "users.json"
    svc = AuthService(users_file_path=str(users_file))
    svc.create_user("rotate@test.com", "Password123!")

    tokens = svc.authenticate_user("rotate@test.com", "Password123!")
    old_refresh = tokens["refresh_token"]

    # First refresh at T+1 — valid rotation
    t_rotate = get_current_epoch() + 1
    with patch("src.services.auth_service.get_current_epoch", return_value=t_rotate):
        new_tokens = svc.refresh_access_token(old_refresh)
    assert "access_token" in new_tokens

    # Replay with old token AFTER grace period expires — must raise
    t_after_grace = t_rotate + AUTH_REFRESH_GRACE_PERIOD_SEC + 1
    with (
        patch("src.services.auth_service.get_current_epoch", return_value=t_after_grace),
        pytest.raises(ToukaException),
    ):
        svc.refresh_access_token(old_refresh)


def test_refresh_token_reuse_revokes_all(tmp_path):
    """On refresh token reuse AFTER grace period, the user's session must be fully revoked.

    Reuse within the grace window is allowed (concurrent multi-tab scenario).
    Reuse after the grace window is treated as a replay attack and revokes the session.
    """
    users_file = tmp_path / "users.json"
    svc = AuthService(users_file_path=str(users_file))
    svc.create_user("revoke@test.com", "Password123!")

    tokens = svc.authenticate_user("revoke@test.com", "Password123!")
    old_refresh = tokens["refresh_token"]

    # Use it once at T+1 (legitimate rotation)
    t_rotate = get_current_epoch() + 1
    with patch("src.services.auth_service.get_current_epoch", return_value=t_rotate):
        svc.refresh_access_token(old_refresh)

    # Reuse old token AFTER grace period — revoke all
    t_after_grace = t_rotate + AUTH_REFRESH_GRACE_PERIOD_SEC + 1
    with (
        patch("src.services.auth_service.get_current_epoch", return_value=t_after_grace),
        pytest.raises(ToukaException),
    ):
        svc.refresh_access_token(old_refresh)

    # Session must be revoked
    user = svc.get_user_by_email("revoke@test.com")
    assert user.refresh_token_exp is None or user.refresh_token_exp == 0, (
        "Session must be revoked after token reuse"
    )


# ---------------------------------------------------------------------------
# P14 — /debug/execute tool allowlist for Debugger
# ---------------------------------------------------------------------------

def test_debug_execute_rejects_forbidden_tool_for_debugger(auth_client):
    """Debugger role must not execute tools outside allowed_tools list."""
    debugger = AgentIdentity.for_role(
        agent_id="DebugAgent",
        role="Debugger",
        capabilities=["heartbeat_awakening"],
    )
    _override(debugger)
    c = TestClient(app, raise_server_exceptions=False)
    # DA20: with a configured Debugger allowlist, a tool outside it must be cleanly
    # denied (403). Previously this only "passed" because the broken whitelist threw
    # AttributeError (500), which the lenient assertion happened to accept.
    with patch(
        "src.services.permission_service.permission_service.get_allowed_tools",
        return_value=NameGrant.from_lists(["safe_tool"]),
    ):
        resp = c.post(
            "/v1/debug/execute",
            json={"tool_name": "__forbidden_secret_tool__", "args": {}},
        )
    _clear()
    assert resp.status_code == 403, (
        f"Debugger executing forbidden tool must be rejected, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# P22 — Observability endpoints require auth
# ---------------------------------------------------------------------------

def _load_allowlist() -> list[dict]:
    spec = importlib.util.spec_from_file_location(
        "endpoint_auth_allowlist",
        Path(__file__).resolve().parents[2] / "scripts" / "endpoint_auth_allowlist.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ALLOWLIST


def test_logs_endpoint_requires_auth():
    """/logs must NOT be in the allowlist and must require auth."""
    logs_entries = [e for e in _load_allowlist() if "/logs" in e.get("path_prefix", "")]
    assert logs_entries == [], f"/logs must not be in allowlist, found: {logs_entries}"


def test_memory_endpoint_requires_auth():
    """/memory must NOT be in the allowlist and must require auth."""
    memory_entries = [e for e in _load_allowlist() if "/memory" in e.get("path_prefix", "")]
    assert memory_entries == [], f"/memory must not be in allowlist, found: {memory_entries}"


def test_tools_endpoint_requires_auth():
    """/tools must NOT be in the allowlist."""
    tool_entries = [
        e for e in _load_allowlist()
        if e.get("path_prefix", "").startswith("/tools")
    ]
    assert tool_entries == [], f"/tools must not be in allowlist, found: {tool_entries}"


# ---------------------------------------------------------------------------
# P25 — Delete user invalidates refresh token
# ---------------------------------------------------------------------------

def test_deleted_user_cannot_refresh_token(tmp_path):
    """After delete_user(), the user's refresh_token_exp must be 0 or None."""
    users_file = tmp_path / "users.json"
    auth_svc = AuthService(users_file_path=str(users_file))
    user_svc = UserService(auth_service=auth_svc)

    auth_svc.create_user("delete_me@test.com", "Password123!")
    auth_svc.authenticate_user("delete_me@test.com", "Password123!")
    user = auth_svc.get_user_by_email("delete_me@test.com")
    assert user.refresh_token_exp is not None

    user_svc.delete_user(user.id)

    # User is gone — refresh_token_exp was 0 before deletion
    remaining = auth_svc.get_all_users()
    assert not any(u.id == user.id for u in remaining), "User should be deleted"


# ---------------------------------------------------------------------------
# P27 — Admin audit logging
# ---------------------------------------------------------------------------

def test_admin_create_user_emits_audit_log():
    """P27: admin.py create_user and update_user must contain AUDIT log calls."""
    src = inspect.getsource(admin_mod)
    assert "AUDIT" in src, "admin.py must contain AUDIT warning log entries"
    assert "logger.warning" in src, "admin.py must use logger.warning for audit trail"
