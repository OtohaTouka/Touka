"""P8-A callback parser、PKCE 與 issuer mix-up rules。"""

from __future__ import annotations

import importlib
from urllib.parse import parse_qs, urlsplit

import pytest


def _protocol():
    return importlib.import_module("src.protocols.mcp.oauth.protocol")


def test_form_post_parser_accepts_one_bounded_semantic_outcome():
    module = _protocol()
    policy = module.OAuthCallbackPolicy(
        route="/mcp/oauth/callback",
        redirect_uri="https://chat.example.test/mcp/oauth/callback",
        response_mode="form_post",
    )
    parsed = module.parse_oauth_callback(
        policy=policy,
        method="POST",
        content_type="application/x-www-form-urlencoded",
        raw_query=b"",
        raw_body=b"state=state-a&code=code-a&iss=https%3A%2F%2Fissuer.example.test",
    )

    assert parsed.state == "state-a"
    assert parsed.code == "code-a"
    assert parsed.error is None
    assert parsed.issuer == "https://issuer.example.test"


@pytest.mark.parametrize(
    ("method", "content_type", "query", "body", "kind"),
    [
        ("GET", "", b"", b"state=a&code=b", "method"),
        ("POST", "application/json", b"", b"{}", "content_type"),
        (
            "POST",
            "application/x-www-form-urlencoded",
            b"state=a",
            b"state=a&code=b",
            "mixed_source",
        ),
        ("POST", "application/x-www-form-urlencoded", b"", b"state=a&state=b&code=c", "invalid"),
        ("POST", "application/x-www-form-urlencoded", b"", b"state=a&code=b&error=no", "ambiguous"),
        ("POST", "application/x-www-form-urlencoded", b"", b"state=a", "ambiguous"),
    ],
)
def test_callback_parser_rejects_before_state_lookup(method, content_type, query, body, kind):
    module = _protocol()
    policy = module.OAuthCallbackPolicy(
        route="/mcp/oauth/callback",
        redirect_uri="https://chat.example.test/mcp/oauth/callback",
        response_mode="form_post",
    )
    with pytest.raises(module.OAuthCallbackError) as exc:
        module.parse_oauth_callback(
            policy=policy,
            method=method,
            content_type=content_type,
            raw_query=query,
            raw_body=body,
        )
    assert exc.value.kind.value == kind


def test_callback_parser_enforces_aggregate_field_and_body_bounds():
    module = _protocol()
    policy = module.OAuthCallbackPolicy(
        route="/mcp/oauth/callback",
        redirect_uri="https://chat.example.test/mcp/oauth/callback",
        response_mode="form_post",
    )
    with pytest.raises(module.OAuthCallbackError) as exc:
        module.parse_oauth_callback(
            policy=policy,
            method="POST",
            content_type="application/x-www-form-urlencoded",
            raw_query=b"",
            raw_body=b"state=" + b"a" * 9000 + b"&code=b",
        )
    assert exc.value.kind.value == "too_large"


def test_authorization_url_uses_s256_resource_iss_and_exact_redirect():
    module = _protocol()
    url = module.build_authorization_url(
        authorization_endpoint="https://issuer.example.test/authorize",
        client_id="client-a",
        redirect_uri="https://chat.example.test/mcp/oauth/callback",
        resource="https://mcp.example.test/mcp",
        requested_scopes=frozenset({"tools.read", "resources.read"}),
        state="raw-state",
        code_verifier="verifier-value",
        response_mode="form_post",
    )
    query = parse_qs(urlsplit(url).query)

    assert query["code_challenge_method"] == ["S256"]
    assert query["response_mode"] == ["form_post"]
    assert query["resource"] == ["https://mcp.example.test/mcp"]
    assert query["state"] == ["raw-state"]
    assert query["redirect_uri"] == ["https://chat.example.test/mcp/oauth/callback"]
    assert "verifier-value" not in url


@pytest.mark.parametrize(
    ("supports_iss", "trusted_count", "received", "allowed"),
    [
        (True, 1, "https://issuer.example.test", True),
        (True, 1, None, False),
        (False, 1, None, True),
        (False, 2, None, False),
        (False, 1, "https://issuer.example.test", True),
        (False, 1, "https://ISSUER.example.test", False),
    ],
)
def test_issuer_validation_is_exact_and_conservative(
    supports_iss, trusted_count, received, allowed
):
    module = _protocol()
    result = module.validate_callback_issuer(
        expected="https://issuer.example.test",
        received=received,
        issuer_parameter_supported=supports_iss,
        trusted_issuer_count=trusted_count,
    )
    assert result is allowed
