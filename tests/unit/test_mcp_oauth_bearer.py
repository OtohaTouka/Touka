# ruff: noqa: S105
"""P8-A bearer retrieval, refresh rotation, and exact egress fencing。"""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx2 as httpx
import pytest
from pydantic import SecretStr


def _modules():
    return (
        importlib.import_module("src.protocols.mcp.oauth.models"),
        importlib.import_module("src.protocols.mcp.oauth.profile"),
        importlib.import_module("src.protocols.mcp.oauth.service"),
    )


def _slot(models):
    return models.OAuthAuthorizationSlotKey(
        human_principal_id="human-a",
        execution_agent_id="agent-a",
        installation_id="server-a",
        installation_generation=1,
        auth_profile_id="profile-a",
    )


def _profile(profile_module):
    return profile_module.OAuthProfile(
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


def _metadata(service_module, *, fingerprint="metadata-fingerprint"):
    return service_module.OAuthAuthorizationMetadata(
        issuer="https://issuer.example.test",
        authorization_endpoint="https://issuer.example.test/authorize",
        token_endpoint="https://issuer.example.test/token",
        registration_endpoint=None,
        revocation_endpoint="https://issuer.example.test/revoke",
        code_challenge_methods_supported=("S256",),
        response_modes_supported=("form_post",),
        authorization_response_iss_parameter_supported=True,
        security_fingerprint=fingerprint,
    )


def _grant(
    models,
    *,
    expires_at=2000.0,
    status=None,
    version=3,
    auth_method="none",
    client_secret=None,
):
    profile_module = importlib.import_module("src.protocols.mcp.oauth.profile")
    return models.OAuthGrantRecord(
        grant_id="grant-a",
        slot_digest="slot-digest",
        grant_version=version,
        status=status or models.OAuthGrantStatus.ACTIVE,
        binding=models.OAuthGrantBinding(
            issuer="https://issuer.example.test",
            resource="https://mcp.example.test/mcp",
            client_id="client-a",
            granted_scopes=frozenset({"tools.read"}),
            profile_generation=1,
            profile_fingerprint=profile_module.oauth_profile_fingerprint(_profile(profile_module)),
            registration_version=1,
            metadata_security_fingerprint="metadata-fingerprint",
            revocation_endpoint="https://issuer.example.test/revoke",
            client_secret=(SecretStr(client_secret) if client_secret is not None else None),
            token_endpoint_auth_method=auth_method,
        ),
        tokens=models.OAuthTokenSet(
            access_token=SecretStr("access-canary"),
            refresh_token=SecretStr("refresh-canary"),
            token_type="Bearer",
            expires_at=expires_at,
            granted_scopes=frozenset({"tools.read"}),
        ),
    )


class _Repository:
    def __init__(self, grant):
        self.grant = grant
        self.events: list[str] = []
        self.lease_available = True
        self.commit_result = True

    async def get_active_grant(self, _slot, **_kwargs):
        return self.grant

    async def assert_bearer_usable(self, _slot, grant_id, expected_version, **_kwargs):
        self.events.append(f"fence:{grant_id}:{expected_version}")
        return (
            self.grant is not None
            and self.grant.grant_id == grant_id
            and self.grant.grant_version == expected_version
        )

    async def acquire_refresh_lease(self, grant_id, expected_version, owner, **_kwargs):
        self.events.append(f"lease:{grant_id}:{expected_version}:{owner}")
        if not self.lease_available:
            return None
        self.lease_available = False
        return SimpleNamespace(
            grant_id=grant_id,
            expected_grant_version=expected_version,
            owner=owner,
            fence="lease-fence",
            expires_at=1500.0,
        )

    async def commit_refresh(self, lease, tokens, *, binding, now):
        self.events.append("refresh_committed")
        if not self.commit_result:
            return False
        self.grant = self.grant.model_copy(
            update={
                "grant_version": self.grant.grant_version + 1,
                "binding": binding,
                "tokens": tokens,
            }
        )
        return True

    async def release_refresh_lease(self, _lease, **_kwargs):
        self.events.append("refresh_released")
        return True

    async def tombstone_invalid_grant(self, _lease, **_kwargs):
        self.events.append("refresh_tombstoned")
        self.grant = None
        return True

    async def freeze_grant(self, grant_id, expected_version, **_kwargs):
        self.events.append(f"frozen:{grant_id}:{expected_version}")
        self.grant = self.grant.model_copy(update={"status": self.grant.status.FROZEN})
        return True


class _Outbound:
    def __init__(self, models):
        self.models = models
        self.events: list[str] = []
        self.error = None
        self.returned_scope = None

    async def refresh_prepared_access_token(self, request, **_kwargs):
        self.events.append("refresh")
        assert request.form_data()["refresh_token"] == "refresh-canary"
        if self.error is not None:
            raise self.error
        return self.models.OAuthTokenSet(
            access_token=SecretStr("rotated-access-canary"),
            refresh_token=None,
            token_type="Bearer",
            expires_at=3000.0,
            granted_scopes=frozenset(),
        ), self.returned_scope


@pytest.mark.asyncio
async def test_fresh_grant_is_fenced_before_bearer_is_returned():
    models, profile_module, service_module = _modules()
    repository = _Repository(_grant(models))
    outbound = _Outbound(models)
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=outbound,
        epoch_clock=lambda: 1000.0,
        random_urlsafe=lambda _size: "worker-a",
    )

    credential = await service.get_bearer_credential(
        slot=_slot(models),
        profile=_profile(profile_module),
        metadata=_metadata(service_module),
        resource="https://mcp.example.test/mcp",
    )

    assert credential.authorization_header.get_secret_value() == "Bearer access-canary"
    assert credential.grant_version == 3
    assert repository.events == ["fence:grant-a:3"]
    assert outbound.events == []


@pytest.mark.asyncio
async def test_refresh_preserves_omitted_scope_and_refresh_token_then_fences_new_version():
    models, profile_module, service_module = _modules()
    repository = _Repository(_grant(models, expires_at=1050.0))
    outbound = _Outbound(models)
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=outbound,
        epoch_clock=lambda: 1000.0,
        random_urlsafe=lambda _size: "worker-a",
    )

    credential = await service.get_bearer_credential(
        slot=_slot(models),
        profile=_profile(profile_module),
        metadata=_metadata(service_module),
        resource="https://mcp.example.test/mcp",
    )

    assert credential.authorization_header.get_secret_value() == ("Bearer rotated-access-canary")
    assert credential.grant_version == 4
    assert repository.grant.tokens.refresh_token.get_secret_value() == "refresh-canary"
    assert repository.grant.tokens.granted_scopes == frozenset({"tools.read"})
    assert repository.events[-1] == "fence:grant-a:4"
    assert outbound.events == ["refresh"]


@pytest.mark.asyncio
async def test_refresh_network_is_held_inside_explicit_egress_guard():
    models, profile_module, service_module = _modules()
    repository = _Repository(_grant(models, expires_at=1050.0))
    outbound = _Outbound(models)
    trace: list[str] = []
    original_refresh = outbound.refresh_prepared_access_token

    async def traced_refresh(request, **kwargs):
        trace.append("network")
        return await original_refresh(request, **kwargs)

    outbound.refresh_prepared_access_token = traced_refresh
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=outbound,
        epoch_clock=lambda: 1000.0,
        random_urlsafe=lambda _size: "worker-a",
    )

    bindings = []

    @asynccontextmanager
    async def refresh_guard(binding):
        bindings.append(binding)
        trace.append("permit-enter")
        try:
            yield
        finally:
            trace.append("permit-exit")

    await service.get_bearer_credential(
        slot=_slot(models),
        profile=_profile(profile_module),
        metadata=_metadata(service_module),
        resource="https://mcp.example.test/mcp",
        refresh_egress_guard=refresh_guard,
    )

    assert trace == ["permit-enter", "network", "permit-exit"]
    assert len(bindings) == 1
    assert bindings[0].http_method == "POST"
    assert bindings[0].grant_type == "refresh_token"
    assert bindings[0].token_endpoint == "https://issuer.example.test/token"
    assert bindings[0].resource == "https://mcp.example.test/mcp"
    assert bindings[0].client_auth_method == "none"
    assert bindings[0].grant_id == "grant-a"
    assert bindings[0].grant_version == 3
    assert bindings[0].digest.startswith("sha256:")
    assert "refresh-canary" not in bindings[0].model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auth_method", "expected_form_fields", "expects_basic_header"),
    [
        (
            "client_secret_basic",
            ("grant_type", "refresh_token", "resource"),
            True,
        ),
        (
            "client_secret_post",
            (
                "client_id",
                "client_secret",
                "grant_type",
                "refresh_token",
                "resource",
            ),
            False,
        ),
    ],
)
async def test_delegated_refresh_permit_matches_actual_client_auth_wire_shape(
    auth_method,
    expected_form_fields,
    expects_basic_header,
):
    models, profile_module, service_module = _modules()
    repository = _Repository(
        _grant(
            models,
            expires_at=1050.0,
            auth_method=auth_method,
            client_secret="client-secret-canary",
        )
    )
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["form_fields"] = tuple(
            sorted(parse_qs(request.content.decode(), keep_blank_values=True))
        )
        observed["has_basic_header"] = "authorization" in request.headers
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "access_token": "rotated-access-canary",
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": "tools.read",
            },
        )

    bindings = []

    @asynccontextmanager
    async def refresh_guard(binding):
        bindings.append(binding)
        yield

    outbound_module = importlib.import_module("src.protocols.mcp.oauth.outbound")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        service = service_module.ConnectorAuthService(
            repository=repository,
            outbound=outbound_module.OAuthHttpClient(
                client=client,
                epoch_clock=lambda: 1000.0,
            ),
            epoch_clock=lambda: 1000.0,
            random_urlsafe=lambda _size: "worker-a",
        )
        await service.get_bearer_credential(
            slot=_slot(models),
            profile=_profile(profile_module),
            metadata=_metadata(service_module),
            resource="https://mcp.example.test/mcp",
            refresh_egress_guard=refresh_guard,
        )

    assert len(bindings) == 1
    assert bindings[0].client_auth_method == auth_method
    assert bindings[0].form_fields == expected_form_fields
    assert observed == {
        "form_fields": expected_form_fields,
        "has_basic_header": expects_basic_header,
    }
    assert "client-secret-canary" not in bindings[0].model_dump_json()


@pytest.mark.asyncio
async def test_dynamic_authority_revocation_freezes_grant_before_bearer_egress():
    models, profile_module, service_module = _modules()
    repository = _Repository(_grant(models))
    outbound = _Outbound(models)
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=outbound,
        epoch_clock=lambda: 1000.0,
    )

    with pytest.raises(service_module.OAuthAuthorizationRequiredError):
        await service.get_bearer_credential(
            slot=_slot(models),
            profile=_profile(profile_module),
            metadata=_metadata(service_module),
            resource="https://mcp.example.test/mcp",
            authorization_fence=lambda: False,
        )

    assert repository.events == ["frozen:grant-a:3"]
    assert outbound.events == []


@pytest.mark.asyncio
async def test_dynamic_authority_is_rechecked_immediately_before_refresh_egress():
    models, profile_module, service_module = _modules()
    repository = _Repository(_grant(models, expires_at=1050.0))
    outbound = _Outbound(models)
    decisions = iter((True, False))
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=outbound,
        epoch_clock=lambda: 1000.0,
        random_urlsafe=lambda _size: "worker-a",
    )

    with pytest.raises(service_module.OAuthAuthorizationRequiredError):
        await service.get_bearer_credential(
            slot=_slot(models),
            profile=_profile(profile_module),
            metadata=_metadata(service_module),
            resource="https://mcp.example.test/mcp",
            authorization_fence=lambda: next(decisions),
        )

    assert outbound.events == []
    assert "frozen:grant-a:3" in repository.events
    assert "refresh_released" in repository.events


@pytest.mark.asyncio
async def test_metadata_drift_freezes_exact_grant_before_credential_egress():
    models, profile_module, service_module = _modules()
    repository = _Repository(_grant(models))
    outbound = _Outbound(models)
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=outbound,
        epoch_clock=lambda: 1000.0,
    )

    with pytest.raises(service_module.OAuthAuthorizationRequiredError):
        await service.get_bearer_credential(
            slot=_slot(models),
            profile=_profile(profile_module),
            metadata=_metadata(service_module, fingerprint="drifted"),
            resource="https://mcp.example.test/mcp",
        )

    assert repository.events == ["frozen:grant-a:3"]
    assert outbound.events == []


@pytest.mark.asyncio
async def test_invalid_grant_tombstones_pointer_and_never_returns_old_access_token():
    models, profile_module, service_module = _modules()
    outbound_module = importlib.import_module("src.protocols.mcp.oauth.outbound")
    repository = _Repository(_grant(models, expires_at=900.0))
    outbound = _Outbound(models)
    outbound.error = outbound_module.OAuthInvalidGrantError()
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=outbound,
        epoch_clock=lambda: 1000.0,
        random_urlsafe=lambda _size: "worker-a",
    )

    with pytest.raises(service_module.OAuthAuthorizationRequiredError):
        await service.get_bearer_credential(
            slot=_slot(models),
            profile=_profile(profile_module),
            metadata=_metadata(service_module),
            resource="https://mcp.example.test/mcp",
        )

    assert repository.events[-1] == "refresh_tombstoned"


@pytest.mark.asyncio
async def test_durable_clock_rollback_blocks_before_any_credential_egress():
    models, profile_module, service_module = _modules()
    repository = _Repository(_grant(models))
    outbound = _Outbound(models)

    async def reject_clock(**_kwargs):
        return False

    repository.observe_clock = reject_clock
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=outbound,
        epoch_clock=lambda: 900.0,
    )

    with pytest.raises(service_module.OAuthClockRollbackError):
        await service.get_bearer_credential(
            slot=_slot(models),
            profile=_profile(profile_module),
            metadata=_metadata(service_module),
            resource="https://mcp.example.test/mcp",
        )

    assert outbound.events == []
