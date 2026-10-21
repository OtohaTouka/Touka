"""P8-A hermetic authorization-code flow against a mock authorization server。"""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from src.models.identity import AgentIdentity
from src.protocols.mcp.canonical import McpRequestContext
from src.protocols.mcp.oauth.keyring import OAuthKey, OAuthKeyRing
from src.protocols.mcp.oauth.outbound import OAuthHttpClient, OAuthMetadataDiscovery
from src.protocols.mcp.oauth.profile import OAuthProfile, OAuthProfileRepository
from src.protocols.mcp.oauth.protocol import OAuthCallbackPayload, OAuthCallbackPolicy
from src.protocols.mcp.oauth.runtime import McpOAuthRuntimeService
from src.protocols.mcp.oauth.service import ConnectorAuthService
from src.protocols.mcp.oauth.store import OAuthSqliteStore
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig


def _key() -> OAuthKey:
    encoded = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    return OAuthKey.parse(f"v1:full-flow:{encoded}")


def _context() -> McpRequestContext:
    return McpRequestContext(
        request_id="request-a",
        identity=AgentIdentity.for_role("soul-a", "Owner"),
        human_principal_id="human-a",
        installation_id="server-a",
        auth_level="Manual",
        origin="foreground",
    )


@pytest.mark.asyncio
async def test_full_authorization_callback_status_and_bearer_flow_is_secret_safe(tmp_path):
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if "oauth-protected-resource" in request.url.path:
            return httpx2.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "resource": "https://mcp.example.test/mcp",
                    "authorization_servers": ["https://issuer.example.test"],
                },
            )
        if "oauth-authorization-server" in request.url.path:
            return httpx2.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "issuer": "https://issuer.example.test",
                    "authorization_endpoint": "https://issuer.example.test/authorize",
                    "token_endpoint": "https://issuer.example.test/token",
                    "revocation_endpoint": "https://issuer.example.test/revoke",
                    "code_challenge_methods_supported": ["S256"],
                    "response_modes_supported": ["form_post"],
                    "authorization_response_iss_parameter_supported": True,
                },
            )
        assert request.url.path == "/token"
        assert b"code=authorization-code-canary" in request.content
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "access_token": "access-token-canary",
                "refresh_token": "refresh-token-canary",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "tools.read",
            },
        )

    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    )
    outbound = OAuthHttpClient(client=client, epoch_clock=lambda: 1000.0)
    store = OAuthSqliteStore(
        tmp_path / "mcp_oauth.sqlite3",
        OAuthKeyRing(current=_key()),
    )
    await store.prepare_schema()
    profiles = OAuthProfileRepository(tmp_path / "mcp_oauth_profiles.json")
    await profiles.put(
        OAuthProfile(
            profile_id="profile-a",
            generation=1,
            enabled=True,
            callback_binding="form_post",
            trusted_public_origin="https://chat.example.test",
            trusted_issuers=("https://issuer.example.test",),
            client_id="client-a",
            scope_rules=(
                {
                    "operation": "tools/call",
                    "selector": "search",
                    "scopes": ["tools.read"],
                },
            ),
        )
    )
    registry = ProtocolRegistry("__missing_full_flow_registry__.json")
    registry.servers = {
        "server-a": ProtocolServerConfig(
            url="https://mcp.example.test/mcp",
            auth_mode="oauth_user",
            auth_profile_id="profile-a",
        )
    }
    runtime = McpOAuthRuntimeService(
        registry=registry,
        profiles=profiles,
        store=store,
        connector=ConnectorAuthService(
            repository=store,
            outbound=outbound,
            epoch_clock=lambda: 1000.0,
        ),
        metadata_provider=OAuthMetadataDiscovery(outbound),
        callback_policies=(
            OAuthCallbackPolicy(
                route="/mcp/oauth/callback",
                redirect_uri="https://proxy.example.test/mcp/oauth/callback",
                response_mode="form_post",
            ),
        ),
        primary_callback_route="/mcp/oauth/callback",
        feature_enabled=True,
        authorization_fence=lambda _slot: True,
        epoch_clock=lambda: 1000.0,
    )

    started = await runtime.start_authorization(
        server_name="server-a",
        request_context=_context(),
        installation_generation=1,
        selected_issuer=None,
    )
    state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    callback = await runtime.complete_callback(
        callback=OAuthCallbackPayload(
            state=state,
            code="authorization-code-canary",
            issuer="https://issuer.example.test",
        ),
        redirect_route="/mcp/oauth/callback",
    )
    status = await runtime.get_status(
        server_name="server-a",
        request_context=_context(),
        installation_generation=1,
    )
    bearer = await runtime.get_bearer_credential(
        server_name="server-a",
        request_context=_context(),
        installation_generation=1,
    )

    assert callback.status.value == "connected"
    assert status.status.value == "connected"
    assert bearer.authorization_header.get_secret_value() == ("Bearer access-token-canary")
    public = started.model_dump_json() + callback.model_dump_json() + status.model_dump_json()
    assert "access-token-canary" not in public
    assert "refresh-token-canary" not in public
    raw_db = (tmp_path / "mcp_oauth.sqlite3").read_bytes()
    assert state.encode() not in raw_db
    assert b"access-token-canary" not in raw_db
    assert b"refresh-token-canary" not in raw_db
    await outbound.aclose()
