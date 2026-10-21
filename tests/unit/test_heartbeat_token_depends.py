"""Unit tests for verify_heartbeat_token() Depends function (TDD).

After the SOLID-SRP fix, ALL heartbeat token validation must live inside
verify_heartbeat_token(), not duplicated in the awaken_agent() body.

Tests verify:
1. Pure validation logic (no HTTP request needed)
2. The awaken_agent body no longer contains inline token validation
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import jwt
import pytest
from src.core.exceptions import ToukaException
from src.core.security import AuthSecretManager
from src.routers import heartbeat
from src.routers.heartbeat import HeartbeatAwakenRequest, awaken_agent, verify_heartbeat_token

HEARTBEAT_TOKEN = "synthetic-shared-credential"  # noqa: S105
SIGNING_KEY = "synthetic-signing-key-for-heartbeat-tests"


@pytest.fixture(autouse=True)
def synthetic_credentials(monkeypatch):
    """Use only owned synthetic credentials, including the JWT signing source."""
    monkeypatch.setattr(heartbeat, "HEARTBEAT_TOKEN", HEARTBEAT_TOKEN)
    monkeypatch.setattr(AuthSecretManager, "get_secret", lambda: SIGNING_KEY)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(header_token: str = "") -> MagicMock:
    """Build a minimal mock Request object."""
    req = MagicMock()
    req.headers.get = lambda k, default="": header_token if k == "X-Heartbeat-Token" else default
    return req


def _make_body(body_token: str = "") -> HeartbeatAwakenRequest:
    """Build a HeartbeatAwakenRequest with the given body token."""
    return HeartbeatAwakenRequest(token=body_token, agent_id="DebugAgent")


def _make_service_jwt(*, sub: str = "heartbeat", type_: str = "service") -> str:
    """Create a valid service-type JWT signed with the current secret."""
    return jwt.encode(
        {"sub": sub, "type": type_},
        AuthSecretManager.get_secret(),
        algorithm="HS256",
    )


# ---------------------------------------------------------------------------
# verify_heartbeat_token: reject cases
# ---------------------------------------------------------------------------


def test_rejects_when_both_header_and_body_empty():
    """No token anywhere → HEARTBEAT_INVALID_TOKEN."""
    with pytest.raises(ToukaException):
        verify_heartbeat_token(request=_make_request(""), req=_make_body(""))


def test_rejects_wrong_plain_token_via_header():
    """Wrong plain token in header → rejected."""
    with pytest.raises(ToukaException):
        verify_heartbeat_token(request=_make_request("wrong-token"), req=_make_body(""))


def test_rejects_wrong_plain_token_via_body():
    """Wrong plain token in body → rejected."""
    with pytest.raises(ToukaException):
        verify_heartbeat_token(request=_make_request(""), req=_make_body("wrong-token"))


def test_rejects_invalid_jwt():
    """Malformed JWT → rejected."""
    with pytest.raises(ToukaException):
        verify_heartbeat_token(request=_make_request("not.a.jwt"), req=_make_body(""))


def test_rejects_jwt_with_wrong_sub():
    """JWT with sub != 'heartbeat' → rejected."""
    bad_jwt = _make_service_jwt(sub="other", type_="service")
    with pytest.raises(ToukaException):
        verify_heartbeat_token(request=_make_request(bad_jwt), req=_make_body(""))


def test_rejects_jwt_with_wrong_type():
    """JWT with type != 'service' → rejected."""
    bad_jwt = _make_service_jwt(sub="heartbeat", type_="access")
    with pytest.raises(ToukaException):
        verify_heartbeat_token(request=_make_request(bad_jwt), req=_make_body(""))


# ---------------------------------------------------------------------------
# verify_heartbeat_token: accept cases
# ---------------------------------------------------------------------------


def test_accepts_valid_plain_token_via_header():
    """Correct HEARTBEAT_TOKEN in header → returns token string."""
    result = verify_heartbeat_token(request=_make_request(HEARTBEAT_TOKEN), req=_make_body(""))
    assert result == HEARTBEAT_TOKEN


def test_accepts_valid_plain_token_via_body():
    """Correct HEARTBEAT_TOKEN in body → returns token string."""
    result = verify_heartbeat_token(request=_make_request(""), req=_make_body(HEARTBEAT_TOKEN))
    assert result == HEARTBEAT_TOKEN


def test_accepts_valid_service_jwt():
    """Valid service JWT (signed with current secret) → accepted."""
    good_jwt = _make_service_jwt()
    result = verify_heartbeat_token(request=_make_request(good_jwt), req=_make_body(""))
    assert result == good_jwt


def test_header_takes_precedence_over_body():
    """When both header and body tokens are present, header wins."""
    good_jwt = _make_service_jwt()
    # body has a wrong token, header has good JWT
    result = verify_heartbeat_token(
        request=_make_request(good_jwt),
        req=_make_body("wrong-body-token"),
    )
    assert result == good_jwt


# ---------------------------------------------------------------------------
# SOLID-SRP check: body must NOT duplicate validation logic
# ---------------------------------------------------------------------------


def test_awaken_agent_body_no_longer_contains_inline_validation():
    """After SOLID fix, awaken_agent body must not re-validate the token.

    The validation keywords (jwt.decode, HEARTBEAT_TOKEN, HEARTBEAT_INVALID_TOKEN)
    must appear in verify_heartbeat_token, NOT in awaken_agent.
    """
    body_src = inspect.getsource(awaken_agent)

    # These SHOULD be in verify_heartbeat_token, not in awaken_agent body
    assert "HEARTBEAT_TOKEN" not in body_src, (
        "SOLID-SRP violation: HEARTBEAT_TOKEN comparison must be in verify_heartbeat_token(), "
        "not duplicated in awaken_agent()"
    )
    assert "jwt.decode" not in body_src, (
        "SOLID-SRP violation: jwt.decode must be in verify_heartbeat_token(), "
        "not duplicated in awaken_agent()"
    )
    assert "HEARTBEAT_INVALID_TOKEN" not in body_src, (
        "SOLID-SRP violation: HEARTBEAT_INVALID_TOKEN check must be in verify_heartbeat_token(), "
        "not duplicated in awaken_agent()"
    )


def test_wrong_header_does_not_fall_back_to_correct_body():
    """A supplied header must not be bypassed with the body token."""
    with pytest.raises(ToukaException):
        verify_heartbeat_token(_make_request("wrong-header"), _make_body(HEARTBEAT_TOKEN))


def test_empty_shared_secret_still_accepts_valid_service_jwt(monkeypatch):
    """The independent service-JWT contract remains supported."""
    monkeypatch.setattr(heartbeat, "HEARTBEAT_TOKEN", "")
    token = _make_service_jwt()
    assert verify_heartbeat_token(_make_request(token), _make_body()) == token


@pytest.mark.parametrize("kind", ["expired", "wrong-signature"])
def test_invalid_service_jwt_is_rejected(kind):
    """Expiry and the real signature verifier both remain enforced."""
    claims = {"sub": "heartbeat", "type": "service"}
    if kind == "expired":
        claims["exp"] = 1
    key = "different-synthetic-signing-key-32plus" if kind == "wrong-signature" else SIGNING_KEY
    token = jwt.encode(claims, key, algorithm="HS256")
    with pytest.raises(ToukaException):
        verify_heartbeat_token(_make_request(token), _make_body())
