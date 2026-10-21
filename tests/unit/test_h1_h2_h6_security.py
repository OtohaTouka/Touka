"""Unit/integration tests for H1/H2/H6 security hardening (TDD).

H1 — Secure Cookie: set_cookie must pass secure=SECURE_COOKIES config value.
H2 — Rate Limiting: /auth/login must return 429 after max attempts exceeded.
H6 — Token Rotation: /auth/refresh must issue AND rotate the refresh token.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.routers.auth import get_auth_service, login_rate_limiter
from src.services.auth_service import AuthService

# ── Helpers ───────────────────────────────────────────────────────────────────

MOCK_TOKENS = {
    "access_token": "fake-access",
    "refresh_token": "fake-refresh",
    "token_type": "bearer",
}


def _make_mock_svc(tokens: dict | None = None, side_effect: Exception | None = None):
    svc = MagicMock()
    if side_effect:
        svc.authenticate_user.side_effect = side_effect
    else:
        svc.authenticate_user.return_value = tokens or MOCK_TOKENS
    return svc


# ---------------------------------------------------------------------------
# H1 — Secure Cookie Flag
# ---------------------------------------------------------------------------

def test_login_sets_secure_false_in_dev_mode():
    """In dev mode (SECURE_COOKIES=False), login cookie must NOT have secure flag."""
    mock_svc = _make_mock_svc()
    app.dependency_overrides[get_auth_service] = lambda: mock_svc

    with TestClient(app) as client, patch("src.routers.auth.SECURE_COOKIES", False):
        resp = client.post(
            "/auth/login",
            json={"email": "test@example.com", "password": "testpass"},
        )

    app.dependency_overrides.clear()
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "secure" not in set_cookie.lower(), (
        "Cookie should NOT have secure flag when SECURE_COOKIES=False"
    )


def test_login_sets_secure_true_in_prod_mode():
    """When SECURE_COOKIES=True, login cookie must have secure flag."""
    mock_svc = _make_mock_svc()
    app.dependency_overrides[get_auth_service] = lambda: mock_svc

    with TestClient(app) as client, patch("src.routers.auth.SECURE_COOKIES", True):
        resp = client.post(
            "/auth/login",
            json={"email": "test@example.com", "password": "testpass"},
        )

    app.dependency_overrides.clear()
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "secure" in set_cookie.lower(), (
        "Cookie MUST have secure flag when SECURE_COOKIES=True"
    )


# ---------------------------------------------------------------------------
# H2 — Rate Limiting on /auth/login
# ---------------------------------------------------------------------------

def test_login_rate_limit_blocks_after_max_attempts():
    """After MAX_ATTEMPTS failed logins from the same IP, /auth/login must return 429."""
    login_rate_limiter.reset("testclient")

    mock_svc = _make_mock_svc(
        side_effect=ToukaException(ErrorCode.AUTH_INVALID_CREDENTIALS)
    )
    app.dependency_overrides[get_auth_service] = lambda: mock_svc

    with TestClient(app) as client:
        max_attempts = login_rate_limiter._max
        for _ in range(max_attempts):
            client.post(
                "/auth/login",
                json={"email": "x@x.com", "password": "wrong"},
            )
        response = client.post(
            "/auth/login",
            json={"email": "x@x.com", "password": "wrong"},
        )

    app.dependency_overrides.clear()
    login_rate_limiter.reset("testclient")

    assert response.status_code == 429, (
        f"Expected 429 after {max_attempts} attempts, got {response.status_code}"
    )


def test_successful_login_resets_rate_limit():
    """A successful login must reset the rate limit counter for that IP."""
    login_rate_limiter.reset("testclient")

    mock_svc = _make_mock_svc()
    app.dependency_overrides[get_auth_service] = lambda: mock_svc

    with TestClient(app) as client:
        resp = client.post(
            "/auth/login",
            json={"email": "ok@x.com", "password": "correct"},
        )

    app.dependency_overrides.clear()
    assert resp.status_code == 200
    # Counter should be 0 after successful login
    assert len(login_rate_limiter._attempts.get("testclient", [])) == 0

    login_rate_limiter.reset("testclient")


# ---------------------------------------------------------------------------
# H6 — Token Rotation on /auth/refresh
# ---------------------------------------------------------------------------

def test_refresh_token_rotation_returns_new_refresh_token():
    """POST /auth/refresh must issue a new refresh token (token rotation)."""
    new_tokens = {"access_token": "new-access", "refresh_token": "new-refresh"}
    mock_svc = MagicMock()
    mock_svc.refresh_access_token.return_value = new_tokens
    app.dependency_overrides[get_auth_service] = lambda: mock_svc

    with TestClient(app) as client:
        client.cookies.set("refresh_token", "old-refresh")
        resp = client.post("/auth/refresh")

    app.dependency_overrides.clear()
    assert resp.status_code == 200
    # Verify the new refresh token is set in cookie (token rotation)
    set_cookie = resp.headers.get("set-cookie", "")
    assert "refresh_token" in set_cookie, (
        "Token rotation: new refresh token must be set in cookie on /auth/refresh"
    )


def test_auth_service_refresh_returns_both_tokens():
    """auth_service.refresh_access_token() must return both access and refresh tokens."""
    svc = AuthService()
    mock_user = MagicMock()
    mock_user.id = "user-abc"
    mock_user.refresh_token_exp = 9_999_999_999
    mock_user.refresh_token_jti = "test-jti"  # noqa: S105
    # P3（認證縫）：撤銷水位欄位須反映真實 User 預設（None），否則 MagicMock 物件
    # 進 max() 比較炸。無水位＝閘不觸發（此測試聚焦 H6 輪替回雙 token）。
    mock_user.password_changed_at = None
    mock_user.tokens_revoked_at = None

    with (
        patch.object(svc, "_read_users", return_value=[mock_user]),
        patch.object(svc, "_write_users"),
        patch("src.services.auth_service.jwt.decode") as mock_decode,
    ):
        mock_decode.return_value = {
            "type": "refresh", "sub": "user-abc", "jti": "test-jti", "iat": 1,
        }
        result = svc.refresh_access_token("any-token")

    assert isinstance(result, dict), "refresh_access_token must return a dict"
    assert "access_token" in result, "Must contain access_token"
    assert "refresh_token" in result, "Must contain new refresh_token (H6 rotation)"
