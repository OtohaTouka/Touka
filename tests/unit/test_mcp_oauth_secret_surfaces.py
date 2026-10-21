"""P8-A secret canaries never cross Touka-controlled serialization surfaces."""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr, ValidationError
from src.models.mcp_oauth import McpOAuthCallbackOutcome, McpOAuthStatus
from src.protocols.mcp.oauth.models import OAuthTokenSet
from src.protocols.mcp.oauth.profile import OAuthProfile
from src.protocols.mcp.oauth.service import OAuthBearerCredential
from src.routers.mcp_oauth import _callback_page

SECRET_CANARIES = (
    "access-surface-canary-1f31",
    "refresh-surface-canary-8c42",
    "code-surface-canary-26aa",
    "verifier-surface-canary-d90e",
    "client-secret-surface-canary-c184",
    "state-surface-canary-77bf",
)


def _assert_no_canary(rendered: str) -> None:
    assert all(canary not in rendered for canary in SECRET_CANARIES)


def test_secret_models_mask_tokens_in_repr_dump_and_json() -> None:
    tokens = OAuthTokenSet(
        access_token=SecretStr(SECRET_CANARIES[0]),
        refresh_token=SecretStr(SECRET_CANARIES[1]),
        token_type="Bearer",
        expires_at=2000.0,
        granted_scopes=frozenset({"tools.read"}),
    )
    credential = OAuthBearerCredential(
        grant_id="opaque-grant",
        grant_version=1,
        authorization_header=SecretStr(f"Bearer {SECRET_CANARIES[0]}"),
    )

    _assert_no_canary(repr(tokens))
    _assert_no_canary(tokens.model_dump_json())
    _assert_no_canary(repr(tokens.model_dump()))
    _assert_no_canary(repr(credential))
    _assert_no_canary(credential.model_dump_json())


def test_non_secret_profile_rejects_secret_fields_instead_of_ignoring_them() -> None:
    with pytest.raises(ValidationError):
        OAuthProfile.model_validate(
            {
                "profile_id": "profile-a",
                "generation": 1,
                "trusted_public_origin": "https://chat.example.test",
                "trusted_issuers": ["https://issuer.example.test"],
                "client_id": "client-a",
                "client_secret": SECRET_CANARIES[4],
            }
        )


def test_callback_page_clears_current_entry_and_posts_only_opaque_outcome() -> None:
    response = _callback_page(
        McpOAuthCallbackOutcome(
            operation_id="opaque-operation",
            status=McpOAuthStatus.CONNECTED,
            client_origin="https://chat.example.test",
        )
    )
    html = response.body.decode("utf-8")

    _assert_no_canary(html)
    assert 'history.replaceState(null,"",location.pathname)' in html
    assert "localStorage" not in html
    assert "sessionStorage" not in html
    assert "console." not in html
    assert "operationId" in html
    assert "authorization" not in html.lower()
    assert "token" not in html.lower()
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_sanitized_error_payload_never_needs_upstream_oauth_detail() -> None:
    payload = {
        "error": {
            "code": "MCP_OAUTH_UPSTREAM",
            "message": "OAuth upstream request failed",
        }
    }
    rendered = json.dumps(payload, separators=(",", ":"))

    _assert_no_canary(rendered)
