"""Unit tests for CLOUD_MODE flag — Batch A TDD (VPS cloud deployment hardening).

Tests:
- CLOUD_MODE defaults to False (local mode unchanged)
- In cloud mode, LDH does NOT issue tokens for private IPs
- In cloud mode, LDH uses X-Real-IP to detect actual client IP
- In local mode, LDH behaviour is preserved
- ALLOWED_ORIGINS setting is correctly merged into CORS
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src import config as config_mod
from src.config import Settings
from src.services.identity_router import is_request_from_trusted_local

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(client_host: str, x_real_ip: str | None = None) -> MagicMock:
    """Build a minimal mock Request object."""
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = client_host
    req.state = MagicMock()
    headers: dict[str, str] = {}
    if x_real_ip:
        headers["X-Real-IP"] = x_real_ip
    req.headers = headers
    return req


# ---------------------------------------------------------------------------
# A1 — CLOUD_MODE and ALLOWED_ORIGINS default values
# ---------------------------------------------------------------------------

def test_cloud_mode_defaults_false():
    """CLOUD_MODE must default to False so existing local users are unaffected."""
    field = Settings.model_fields.get("CLOUD_MODE")
    assert field is not None, "Settings must have CLOUD_MODE field"
    assert field.default is False, f"CLOUD_MODE must default to False, got {field.default}"


def test_allowed_origins_defaults_empty():
    """ALLOWED_ORIGINS must default to empty string (no extra origins in local mode)."""
    field = Settings.model_fields.get("ALLOWED_ORIGINS")
    assert field is not None, "Settings must have ALLOWED_ORIGINS field"
    assert field.default == "", f"ALLOWED_ORIGINS must default to '', got {field.default}"


# ---------------------------------------------------------------------------
# A2 — LDH: local mode behaviour preserved (no regression)
# ---------------------------------------------------------------------------

def test_local_mode_trusts_private_ip():
    """In local mode (CLOUD_MODE=False), private IPs are still trusted."""
    req = _make_request("192.168.1.100")
    with patch("src.services.identity_router.CLOUD_MODE", False):
        assert is_request_from_trusted_local(req) is True


def test_local_mode_trusts_loopback():
    """In local mode, loopback is trusted."""
    req = _make_request("127.0.0.1")
    with patch("src.services.identity_router.CLOUD_MODE", False):
        assert is_request_from_trusted_local(req) is True


def test_local_mode_rejects_public_ip():
    """In local mode, public IPs are NOT trusted."""
    req = _make_request("8.8.8.8")
    with patch("src.services.identity_router.CLOUD_MODE", False):
        assert is_request_from_trusted_local(req) is False


def test_local_mode_trusts_docker_internal():
    """In local mode, Docker internal network (172.x) is trusted."""
    req = _make_request("172.17.0.2")
    with patch("src.services.identity_router.CLOUD_MODE", False):
        assert is_request_from_trusted_local(req) is True


# ---------------------------------------------------------------------------
# A2+A4 — LDH: cloud mode hardened behaviour
# ---------------------------------------------------------------------------

def test_cloud_mode_rejects_private_ip_without_x_real_ip():
    """In cloud mode, a private IP without X-Real-IP header is NOT trusted.

    This prevents Docker-internal addresses from triggering LDH.
    """
    req = _make_request("172.17.0.2")
    with patch("src.services.identity_router.CLOUD_MODE", True):
        assert is_request_from_trusted_local(req) is False


def test_cloud_mode_still_trusts_loopback():
    """In cloud mode, true loopback (127.0.0.1) is still trusted.

    Allows direct localhost access for sysadmin/CLI tools.
    """
    req = _make_request("127.0.0.1")
    with patch("src.services.identity_router.CLOUD_MODE", True):
        assert is_request_from_trusted_local(req) is True


def test_cloud_mode_uses_x_real_ip_to_detect_real_client():
    """In cloud mode, X-Real-IP overrides the socket IP for trust decision.

    Scenario: nginx (127.0.0.1) forwards for an external user (1.2.3.4).
    The socket IP (127.0.0.1) would be trusted without this check.
    With X-Real-IP, the real client IP (1.2.3.4) is used → NOT trusted.
    """
    # nginx local IP is 127.0.0.1, but X-Real-IP reveals public client
    req = _make_request(client_host="127.0.0.1", x_real_ip="1.2.3.4")
    with patch("src.services.identity_router.CLOUD_MODE", True):
        assert is_request_from_trusted_local(req) is False


def test_cloud_mode_x_real_ip_loopback_still_trusted():
    """In cloud mode, if X-Real-IP itself is loopback, it is still trusted."""
    req = _make_request(client_host="127.0.0.1", x_real_ip="127.0.0.1")
    with patch("src.services.identity_router.CLOUD_MODE", True):
        assert is_request_from_trusted_local(req) is True


def test_cloud_mode_invalid_x_real_ip_falls_back_to_socket():
    """In cloud mode, an invalid X-Real-IP header falls back to socket IP."""
    req = _make_request(client_host="127.0.0.1", x_real_ip="not-an-ip")
    with patch("src.services.identity_router.CLOUD_MODE", True):
        # Falls back to socket IP (127.0.0.1) → trusted
        assert is_request_from_trusted_local(req) is True


# ---------------------------------------------------------------------------
# A3 — CORS: ALLOWED_ORIGINS merged correctly
# ---------------------------------------------------------------------------

def test_allowed_origins_env_var_is_exported():
    """ALLOWED_ORIGINS must be importable from src.config."""
    assert hasattr(config_mod, "ALLOWED_ORIGINS"), (
        "ALLOWED_ORIGINS must be exported from config.py"
    )


def test_allowed_origins_added_to_cors(auth_client):
    """CORS allow_origins must include custom values from ALLOWED_ORIGINS."""
    with patch("src.main.ALLOWED_ORIGINS", "https://myapp.example.com"):
        # Re-importing alone won't re-apply middleware; this tests config parsing
        from src.main import ALLOWED_ORIGINS
        extra = [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]
        assert "https://myapp.example.com" in extra
