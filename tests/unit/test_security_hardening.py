"""Unit tests for C1/C2/C3 security hardening (TDD).

C1 — JWT secret must never be a known public default.
C2 — Admin bootstrap password must be randomly generated, never hardcoded.
C3 — X-Agent-ID header must not override identity when authentication fails.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient
from src.core.security import AuthSecretManager
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.proxy import _apply_identity_to_context
from src.services.auth_service import AuthService

# ---------------------------------------------------------------------------
# C1 — AuthSecretManager: no known insecure defaults
# ---------------------------------------------------------------------------

KNOWN_INSECURE_SECRETS = {
    "touka_default_secret_2026",
    "TOUKA_INSECURE_DEFAULT_SECRET_FOR_DEV",
    "",
}


def test_auth_secret_manager_does_not_return_known_insecure_default():
    """AuthSecretManager must never return a publicly known secret."""
    AuthSecretManager._secret = None

    with patch("src.core.security.TOUKA_AUTH_SECRET", ""):
        secret = AuthSecretManager.get_secret()

    assert secret not in KNOWN_INSECURE_SECRETS, (
        f"AuthSecretManager returned a known-insecure secret: {secret!r}"
    )
    AuthSecretManager._secret = None


def test_auth_secret_manager_returns_configured_value_when_set():
    """When TOUKA_AUTH_SECRET is explicitly set, that value is used."""
    strong_secret = "x" * 64
    AuthSecretManager._secret = None

    with patch("src.core.security.TOUKA_AUTH_SECRET", strong_secret):
        secret = AuthSecretManager.get_secret()

    assert secret == strong_secret
    AuthSecretManager._secret = None


def test_auth_secret_manager_auto_generated_secret_is_strong():
    """When no secret is configured, the auto-generated secret must be ≥32 chars."""
    AuthSecretManager._secret = None

    with patch("src.core.security.TOUKA_AUTH_SECRET", ""):
        secret = AuthSecretManager.get_secret()

    assert len(secret) >= 32, f"Auto-generated secret too short: {len(secret)} chars"
    AuthSecretManager._secret = None


def test_verify_secret_strength_passes_for_long_secret():
    """verify_secret_strength() returns True for secrets with 32+ chars."""
    AuthSecretManager._secret = "a" * 32
    assert AuthSecretManager.verify_secret_strength() is True
    AuthSecretManager._secret = None


def test_verify_secret_strength_fails_for_short_secret():
    """verify_secret_strength() returns False for secrets shorter than 32 chars."""
    AuthSecretManager._secret = "short"  # noqa: S105
    assert AuthSecretManager.verify_secret_strength() is False
    AuthSecretManager._secret = None


# ---------------------------------------------------------------------------
# C2 — Admin bootstrap: no hardcoded password
# ---------------------------------------------------------------------------

KNOWN_INSECURE_PASSWORDS = {
    "admin_password_change_me",
    "admin",
    "password",
    "123456",
}


def test_admin_bootstrap_password_is_not_hardcoded():
    """ensure_admin_user() must not create admin with a known-insecure password."""
    svc = AuthService()

    created_users: list = []

    def fake_read_users():
        return []

    def fake_write_users(users):
        created_users.extend(users)

    with (
        patch.object(svc, "_read_users", side_effect=fake_read_users),
        patch.object(svc, "_write_users", side_effect=fake_write_users),
    ):
        svc.ensure_admin_user([])

    assert len(created_users) == 1, "Should have created exactly one admin"
    admin = created_users[0]

    for bad_pw in KNOWN_INSECURE_PASSWORDS:
        assert not svc.verify_password(bad_pw, admin.hashed_password), (
            f"Admin was bootstrapped with known-insecure password: {bad_pw!r}"
        )


def test_admin_bootstrap_password_is_long_enough():
    """Bootstrap admin password must be at least 12 characters when unhashed."""
    svc = AuthService()
    plain_password_holder: list[str] = []

    original_hash = svc.hash_password

    def capturing_hash(pw: str) -> str:
        plain_password_holder.append(pw)
        return original_hash(pw)

    with (
        patch.object(svc, "_read_users", return_value=[]),
        patch.object(svc, "_write_users"),
        patch.object(svc, "hash_password", side_effect=capturing_hash),
    ):
        svc.ensure_admin_user([])

    assert plain_password_holder, "hash_password was never called"
    password = plain_password_holder[0]
    assert len(password) >= 12, f"Bootstrap password too short: {len(password)} chars"


# ---------------------------------------------------------------------------
# C3 — proxy.py: X-Agent-ID header must NOT override identity when auth fails
# ---------------------------------------------------------------------------

def test_proxy_does_not_use_x_agent_id_when_identity_is_none():
    """When authentication fails, headers must never become an identity (ADR-0245: 401).

    Regression test for the header-injection auth bypass:
        An attacker sending 'X-Agent-ID: admin' with no valid token used to be
        pinned to a Guest context; chat now rejects the request outright.
    """
    malicious_headers = {"X-Agent-ID": "admin", "X-Touka-Soul-ID": "SuperAdmin"}
    payload = {"model": "claude-x", "messages": [{"role": "user", "content": "hi"}]}
    with (
        patch("src.services.identity_router.is_request_from_trusted_local", return_value=False),
        patch("src.routers.proxy.process_proxy_request") as process,
    ):
        resp = TestClient(app).post("/v1/chat/completions", json=payload, headers=malicious_headers)

    assert resp.status_code == 401, resp.text
    process.assert_not_called()


def test_proxy_uses_verified_identity_when_auth_succeeds():
    """When identity is valid, ctx should reflect the real agent identity."""
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner", capabilities=["all"])
    ctx: dict = {}
    _apply_identity_to_context(ctx, identity)

    assert ctx["agent_id"] == "Alice"
    assert ctx["role"] == "Owner"
    assert ctx["identity"] is identity
