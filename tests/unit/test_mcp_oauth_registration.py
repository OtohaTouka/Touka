"""P8-A CIMD/DCR exact validation and cross-worker registration lease。"""

from __future__ import annotations

import asyncio
import base64
import importlib

import httpx2
import pytest


def _modules():
    return (
        importlib.import_module("src.protocols.mcp.oauth.keyring"),
        importlib.import_module("src.protocols.mcp.oauth.outbound"),
        importlib.import_module("src.protocols.mcp.oauth.profile"),
        importlib.import_module("src.protocols.mcp.oauth.registration"),
        importlib.import_module("src.protocols.mcp.oauth.store"),
    )


def _key_value() -> str:
    encoded = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    return f"v1:registration-test:{encoded}"


def _registration_key(registration_module):
    return registration_module.OAuthRegistrationKey(
        profile_id="profile-a",
        profile_generation=1,
        installation_id="server-a",
        installation_generation=3,
        issuer="https://issuer.example.test",
    )


@pytest.mark.asyncio
async def test_cimd_document_requires_exact_client_redirect_and_public_auth_method():
    _keyring, outbound_module, _profile, registration_module, _store = _modules()

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "client_id": "https://chat.example.test/oauth/client.json",
                "redirect_uris": ["https://proxy.example.test/mcp/oauth/callback"],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler), trust_env=False
    ) as client:
        outbound = outbound_module.OAuthHttpClient(client=client)
        binding = await outbound.fetch_client_metadata_document(
            client_id="https://chat.example.test/oauth/client.json",
            redirect_uri="https://proxy.example.test/mcp/oauth/callback",
            token_endpoint_auth_method="none",
        )

    assert isinstance(binding, registration_module.OAuthClientBinding)
    assert binding.client_id == "https://chat.example.test/oauth/client.json"
    assert binding.registration_version == 1


@pytest.mark.asyncio
async def test_registration_lease_has_one_winner_and_stale_commit_is_fenced(tmp_path):
    keyring_module, _outbound, _profile, registration_module, store_module = _modules()
    ring = keyring_module.OAuthKeyRing(current=keyring_module.OAuthKey.parse(_key_value()))
    store = store_module.OAuthSqliteStore(tmp_path / "oauth.sqlite3", ring)
    await store.prepare_schema()
    key = _registration_key(registration_module)

    first, second = await asyncio.gather(
        store.acquire_registration_lease(key, "worker-a", now=1000.0, lease_seconds=30),
        store.acquire_registration_lease(key, "worker-b", now=1000.0, lease_seconds=30),
    )
    lease = next(item for item in (first, second) if item is not None)
    assert sum(item is not None for item in (first, second)) == 1
    binding = registration_module.OAuthClientBinding(
        client_id="dynamic-client-a",
        token_endpoint_auth_method="none",
        registration_version=1,
    )
    assert await store.commit_registration(lease, binding, now=1001.0)
    assert not await store.commit_registration(lease, binding, now=1002.0)
    loaded = await store.get_registration(key, now=1002.0)
    assert loaded == binding


@pytest.mark.asyncio
async def test_dcr_rejects_registration_management_credentials_and_secret_mismatch():
    _keyring, outbound_module, _profile, _registration, _store = _modules()

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            201,
            headers={"content-type": "application/json"},
            json={
                "client_id": "dynamic-client-a",
                "client_secret": "must-not-be-saved",
                "token_endpoint_auth_method": "none",
                "registration_access_token": "management-secret",
            },
        )

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler), trust_env=False
    ) as client:
        outbound = outbound_module.OAuthHttpClient(client=client)
        with pytest.raises(outbound_module.OAuthUpstreamError):
            await outbound.register_client(
                registration_endpoint="https://issuer.example.test/register",
                redirect_uri="https://proxy.example.test/mcp/oauth/callback",
                token_endpoint_auth_method="none",
            )


def test_client_binding_rejects_auth_method_and_secret_shape_mismatch():
    _keyring, _outbound, _profile, registration_module, _store = _modules()

    with pytest.raises(ValueError):
        registration_module.OAuthClientBinding(
            client_id="public-client",
            token_endpoint_auth_method="none",
            client_secret="must-not-exist",
            registration_version=1,
        )
    with pytest.raises(ValueError):
        registration_module.OAuthClientBinding(
            client_id="confidential-client",
            token_endpoint_auth_method="client_secret_post",
            registration_version=1,
        )


@pytest.mark.asyncio
async def test_registration_service_prefers_cimd_then_uses_named_dcr_fallback(tmp_path):
    keyring_module, outbound_module, profile_module, registration_module, store_module = _modules()
    seen: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        if request.url.path == "/oauth/client.json":
            return httpx2.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "client_id": "https://chat.example.test/oauth/client.json",
                    "redirect_uris": ["https://proxy.example.test/mcp/oauth/callback"],
                    "grant_types": ["authorization_code"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            )
        return httpx2.Response(
            201,
            headers={"content-type": "application/json"},
            json={
                "client_id": "dynamic-client-a",
                "token_endpoint_auth_method": "none",
            },
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), trust_env=False)
    outbound = outbound_module.OAuthHttpClient(client=client)
    store = store_module.OAuthSqliteStore(
        tmp_path / "oauth.sqlite3",
        keyring_module.OAuthKeyRing(current=keyring_module.OAuthKey.parse(_key_value())),
    )
    await store.prepare_schema()
    service = registration_module.OAuthClientRegistrationService(
        repository=store,
        outbound=outbound,
        random_urlsafe=lambda _size: "worker-a",
        epoch_clock=lambda: 1000.0,
    )
    cimd_profile = profile_module.OAuthProfile(
        profile_id="profile-a",
        generation=1,
        enabled=True,
        trusted_public_origin="https://chat.example.test",
        trusted_issuers=("https://issuer.example.test",),
        client_id="https://chat.example.test/oauth/client.json",
    )
    dcr_profile = cimd_profile.model_copy(
        update={
            "client_id": None,
            "dcr_enabled": True,
            "dcr_trusted_issuers": ("https://issuer.example.test",),
        }
    )

    cimd = await service.resolve(
        key=_registration_key(registration_module),
        profile=cimd_profile,
        registration_endpoint="https://issuer.example.test/register",
        redirect_uri="https://proxy.example.test/mcp/oauth/callback",
    )
    dcr = await service.resolve(
        key=_registration_key(registration_module),
        profile=dcr_profile,
        registration_endpoint="https://issuer.example.test/register",
        redirect_uri="https://proxy.example.test/mcp/oauth/callback",
    )

    assert cimd.client_id.endswith("client.json")
    assert dcr.client_id == "dynamic-client-a"
    assert seen == ["/oauth/client.json", "/register"]
    await outbound.aclose()


@pytest.mark.asyncio
async def test_expired_client_secret_can_acquire_fenced_replacement_lease(tmp_path):
    keyring_module, _outbound, _profile, registration_module, store_module = _modules()
    store = store_module.OAuthSqliteStore(
        tmp_path / "oauth.sqlite3",
        keyring_module.OAuthKeyRing(current=keyring_module.OAuthKey.parse(_key_value())),
    )
    await store.prepare_schema()
    key = _registration_key(registration_module)
    first = await store.acquire_registration_lease(
        key,
        "worker-a",
        now=1000.0,
        lease_seconds=30.0,
    )
    assert first is not None
    expired = registration_module.OAuthClientBinding(
        client_id="dynamic-client-a",
        token_endpoint_auth_method="client_secret_post",
        client_secret="expired-secret",
        client_secret_expires_at=1010.0,
        registration_version=1,
    )
    assert await store.commit_registration(first, expired, now=1001.0)
    assert await store.get_registration(key, now=1010.0) is None

    replacement = await store.acquire_registration_lease(
        key,
        "worker-b",
        now=1010.0,
        lease_seconds=30.0,
    )

    assert replacement is not None
    assert replacement.expected_version == 1
    fresh = registration_module.OAuthClientBinding(
        client_id="dynamic-client-b",
        token_endpoint_auth_method="client_secret_post",
        client_secret="fresh-secret",
        client_secret_expires_at=2010.0,
        registration_version=2,
    )
    assert await store.commit_registration(replacement, fresh, now=1011.0)
    assert await store.get_registration(key, now=1012.0) == fresh
