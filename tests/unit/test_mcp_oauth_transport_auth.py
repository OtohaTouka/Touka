"""P8-A MCP transport bearer injection stays request-time and secret-free。"""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager

import httpx2
import pytest
from pydantic import SecretStr


@pytest.mark.asyncio
async def test_async_auth_fetches_and_fences_bearer_for_every_http_request():
    auth_module = importlib.import_module("src.protocols.mcp.oauth.auth")
    service_module = importlib.import_module("src.protocols.mcp.oauth.service")
    calls = 0
    seen: list[str] = []
    provider_paths: list[str] = []

    active = False

    @asynccontextmanager
    async def provider(*, wire_request: httpx2.Request):
        nonlocal calls
        nonlocal active
        calls += 1
        provider_paths.append(wire_request.url.path)
        active = True
        try:
            yield service_module.OAuthBearerCredential(
                grant_id="grant-a",
                grant_version=calls,
                authorization_header=SecretStr(f"Bearer access-{calls}"),
                allowed_private_cidrs=("10.20.0.0/16",),
            )
        finally:
            active = False

    async def handler(request: httpx2.Request) -> httpx2.Response:
        assert active is True
        assert request.extensions["touka_oauth_private_cidrs"] == ("10.20.0.0/16",)
        seen.append(request.headers["authorization"])
        return httpx2.Response(200, json={"ok": True})

    async with httpx2.AsyncClient(
        auth=auth_module.McpOAuthBearerAuth(provider),
        transport=httpx2.MockTransport(handler),
        trust_env=False,
    ) as client:
        await client.get("https://mcp.example.test/one")
        await client.get("https://mcp.example.test/two")

    assert calls == 2
    assert seen == ["Bearer access-1", "Bearer access-2"]
    assert provider_paths == ["/one", "/two"]
    assert active is False


@pytest.mark.asyncio
async def test_auth_provider_failure_prevents_any_network_egress():
    auth_module = importlib.import_module("src.protocols.mcp.oauth.auth")
    sent = False

    @asynccontextmanager
    async def provider(*, wire_request: httpx2.Request):
        assert wire_request.url.path == "/mcp"
        raise RuntimeError("auth-required")
        yield

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal sent
        sent = True
        return httpx2.Response(200)

    async with httpx2.AsyncClient(
        auth=auth_module.McpOAuthBearerAuth(provider),
        transport=httpx2.MockTransport(handler),
        trust_env=False,
    ) as client:
        with pytest.raises(RuntimeError, match="auth-required"):
            await client.get("https://mcp.example.test/mcp")

    assert sent is False
