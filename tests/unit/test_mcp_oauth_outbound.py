"""P8-A OAuth HTTPS/SSRF egress boundary。"""

from __future__ import annotations

import importlib
import ipaddress
from types import SimpleNamespace

import httpx2 as httpx
import pytest


def _outbound():
    return importlib.import_module("src.protocols.mcp.oauth.outbound")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/token",
        "http://issuer.example.test/token",
        "https://user:pass@issuer.example.test/token",
        "https://issuer.example.test/token#fragment",
        "https://[fe80::1%25eth0]/token",
        "https://issuer.example.test:8443/token",
    ],
)
def test_credential_endpoint_requires_unambiguous_https_default_port(url):
    module = _outbound()
    with pytest.raises(module.OAuthOutboundPolicyError):
        module.validate_oauth_endpoint(url)


def test_dns_answer_is_rejected_if_any_address_is_non_public():
    module = _outbound()
    with pytest.raises(module.OAuthOutboundPolicyError):
        module.validate_resolved_addresses(
            (
                ipaddress.ip_address("203.0.113.10"),
                ipaddress.ip_address("127.0.0.1"),
            ),
            allowed_private_cidrs=(),
        )


def test_exact_private_cidr_exception_does_not_allow_sibling_ranges():
    module = _outbound()
    allowed = (ipaddress.ip_network("10.20.0.0/24"),)

    module.validate_resolved_addresses(
        (ipaddress.ip_address("10.20.0.7"),),
        allowed_private_cidrs=allowed,
    )
    with pytest.raises(module.OAuthOutboundPolicyError):
        module.validate_resolved_addresses(
            (ipaddress.ip_address("10.20.1.7"),),
            allowed_private_cidrs=allowed,
        )


@pytest.mark.asyncio
async def test_http_client_passes_request_scoped_private_cidrs_to_transport() -> None:
    module = _outbound()
    seen: list[tuple[str, ...]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["touka_oauth_private_cidrs"])
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"ok": True},
        )

    allowed = (ipaddress.ip_network("10.20.0.0/24"),)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        outbound = module.OAuthHttpClient(client=client)
        result = await outbound.get_json(
            "https://issuer.internal.example/metadata",
            private_network_policy=lambda _url: allowed,
        )

    assert result == {"ok": True}
    assert seen == [("10.20.0.0/24",)]


@pytest.mark.asyncio
async def test_pinned_backend_validates_all_answers_and_connects_only_validated_ip():
    module = _outbound()
    connected: list[str] = []

    class _Resolver:
        async def resolve(self, host: str, port: int):
            assert (host, port) == ("issuer.example.test", 443)
            return (ipaddress.ip_address("93.184.216.34"),)

    class _Backend:
        async def connect_tcp(
            self, host, port, timeout=None, local_address=None, socket_options=None
        ):
            connected.append(host)
            return object()

        async def connect_unix_socket(self, *args, **kwargs):
            message = "unix socket is forbidden"
            raise AssertionError(message)

        async def sleep(self, _seconds):
            return None

    backend = module.PinnedOAuthNetworkBackend(
        resolver=_Resolver(),
        backend=_Backend(),
        allowed_private_cidrs=(),
    )
    result = await backend.connect_tcp("issuer.example.test", 443)

    assert result is not None
    assert connected == ["93.184.216.34"]


@pytest.mark.asyncio
async def test_pinned_backend_never_connects_when_one_dns_answer_is_private():
    module = _outbound()
    connected: list[str] = []

    class _Resolver:
        async def resolve(self, _host: str, _port: int):
            return (
                ipaddress.ip_address("93.184.216.34"),
                ipaddress.ip_address("127.0.0.1"),
            )

    class _Backend:
        async def connect_tcp(self, host, *args, **kwargs):
            connected.append(host)
            return object()

    backend = module.PinnedOAuthNetworkBackend(
        resolver=_Resolver(),
        backend=_Backend(),
        allowed_private_cidrs=(),
    )
    with pytest.raises(module.OAuthOutboundPolicyError):
        await backend.connect_tcp("issuer.example.test", 443)
    assert connected == []


@pytest.mark.asyncio
async def test_token_exchange_is_bounded_no_redirect_and_returns_secret_types():
    module = _outbound()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "access_token": "access-canary",
                "refresh_token": "refresh-canary",
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": "tools.read",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        outbound = module.OAuthHttpClient(client=client, epoch_clock=lambda: 1000.0)
        tokens, returned_scope = await outbound.exchange_authorization_code(
            token_endpoint="https://issuer.example.test/token",
            code="code-canary",
            code_verifier="verifier-canary",
            client_id="client-a",
            redirect_uri="https://proxy.example.test/mcp/oauth/callback",
            resource="https://mcp.example.test/mcp",
        )

    assert returned_scope == "tools.read"
    assert tokens.access_token.get_secret_value() == "access-canary"
    assert tokens.refresh_token.get_secret_value() == "refresh-canary"
    assert tokens.expires_at == 1300.0
    body = requests[0].content.decode()
    assert "grant_type=authorization_code" in body
    assert "code_verifier=verifier-canary" in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (
            httpx.Response(302, headers={"location": "https://evil.example/token"}),
            "OAuthUpstreamError",
        ),
        (
            httpx.Response(200, headers={"content-type": "text/html"}, text="no"),
            "OAuthUpstreamError",
        ),
        (
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b"x" * (1024 * 1024 + 1),
            ),
            "OAuthUpstreamError",
        ),
    ],
)
async def test_outbound_rejects_redirect_wrong_content_type_and_oversize(
    response: httpx.Response,
    error_type: str,
):
    module = _outbound()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        outbound = module.OAuthHttpClient(client=client, epoch_clock=lambda: 1000.0)
        with pytest.raises(getattr(module, error_type)):
            await outbound.exchange_authorization_code(
                token_endpoint="https://issuer.example.test/token",
                code="code-canary",
                code_verifier="verifier-canary",
                client_id="client-a",
                redirect_uri="https://proxy.example.test/mcp/oauth/callback",
                resource="https://mcp.example.test/mcp",
            )


@pytest.mark.asyncio
async def test_refresh_maps_only_exact_invalid_grant_without_exposing_body():
    module = _outbound()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"content-type": "application/json"},
            json={
                "error": "invalid_grant",
                "error_description": "refresh-secret-canary",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        outbound = module.OAuthHttpClient(client=client, epoch_clock=lambda: 1000.0)
        with pytest.raises(module.OAuthInvalidGrantError) as caught:
            await outbound.refresh_access_token(
                token_endpoint="https://issuer.example.test/token",
                refresh_token="refresh-secret-canary",
                client_id="client-a",
                resource="https://mcp.example.test/mcp",
            )

    assert "refresh-secret-canary" not in str(caught.value)


@pytest.mark.asyncio
async def test_metadata_discovery_exact_binds_resource_issuer_and_security_projection():
    module = _outbound()
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "oauth-protected-resource" in request.url.path:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "resource": "https://mcp.example.test/mcp",
                    "authorization_servers": ["https://issuer.example.test"],
                },
            )
        return httpx.Response(
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

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        outbound = module.OAuthHttpClient(client=client, epoch_clock=lambda: 1000.0)
        metadata = await module.OAuthMetadataDiscovery(outbound).discover(
            resource="https://mcp.example.test/mcp",
            profile=SimpleNamespace(trusted_issuers=("https://issuer.example.test",)),
            selected_issuer="https://issuer.example.test",
        )

    assert metadata.issuer == "https://issuer.example.test"
    assert metadata.revocation_endpoint == "https://issuer.example.test/revoke"
    assert len(metadata.security_fingerprint) == 64
    assert seen == [
        "https://mcp.example.test/.well-known/oauth-protected-resource/mcp",
        "https://issuer.example.test/.well-known/oauth-authorization-server",
    ]


@pytest.mark.asyncio
async def test_metadata_discovery_rejects_resource_or_issuer_mismatch():
    module = _outbound()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "resource": "https://other.example.test/mcp",
                "authorization_servers": ["https://issuer.example.test"],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        outbound = module.OAuthHttpClient(client=client, epoch_clock=lambda: 1000.0)
        with pytest.raises(module.OAuthMetadataDiscoveryError):
            await module.OAuthMetadataDiscovery(outbound).discover(
                resource="https://mcp.example.test/mcp",
                profile=SimpleNamespace(trusted_issuers=("https://issuer.example.test",)),
                selected_issuer="https://issuer.example.test",
            )
