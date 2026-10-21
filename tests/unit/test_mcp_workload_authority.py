"""P8-B Slice 4：direct workload ticket authority 與每次 send 重驗。"""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any

import pytest
from pydantic import SecretStr
from src.models.identity import AgentIdentity
from src.protocols.mcp.canonical import McpRequestContext
from src.protocols.mcp.oauth.workload.models import (
    WorkloadCredentialPayload,
    WorkloadMetadataSnapshot,
    WorkloadOAuthProfile,
)
from src.protocols.registry import ProtocolServerConfig


def _authority() -> Any:
    module_name = "src.protocols.mcp.oauth.workload.authority"
    assert importlib.util.find_spec(module_name) is not None, "authority seam is missing"
    return importlib.import_module(module_name)


def _profile(**updates: Any) -> WorkloadOAuthProfile:
    values: dict[str, Any] = {
        "profile_id": "calendar-workload",
        "enabled": True,
        "expected_issuer": "https://issuer.example.test",
        "expected_resource": "https://mcp.example.test",
        "expected_transport_url": "https://mcp.example.test/mcp",
        "token_endpoint_auth_method": "client_secret_basic",
        "requested_scopes": frozenset({"calendar.read"}),
        "workload_principal_id": "heartbeat:Alice",
        "owner_user_id": "owner-1",
        "execution_agent_id": "Alice",
        "tool_scopes": {"calendar.read": frozenset({"calendar.read"})},
        "allow_private_network": False,
        "generation": 6,
        "credential_id": "calendar-secret",
    }
    values.update(updates)
    return WorkloadOAuthProfile(**values)


def _snapshot(profile: WorkloadOAuthProfile) -> WorkloadMetadataSnapshot:
    return WorkloadMetadataSnapshot(
        profile_id=profile.profile_id,
        profile_generation=profile.generation,
        profile_fingerprint=profile.fingerprint,
        issuer=profile.expected_issuer,
        resource=profile.expected_resource,
        token_endpoint="https://issuer.example.test/oauth/token",
        token_endpoint_auth_method=profile.token_endpoint_auth_method,
        allow_private_network=False,
        verified_scopes=profile.requested_scopes,
        metadata_security_fingerprint=f"sha256:{'3' * 64}",
        verified_at=90.0,
    )


def _credential() -> WorkloadCredentialPayload:
    return WorkloadCredentialPayload(
        profile_id="calendar-workload",
        credential_id="calendar-secret",
        generation=2,
        client_id=SecretStr("client-id"),
        client_secret=SecretStr("client-secret"),
    )


def _context() -> McpRequestContext:
    return McpRequestContext(
        request_id="request-1",
        identity=AgentIdentity.for_role("Alice", "Owner"),
        installation_id="calendar",
        auth_level="SafeAuto",
        execution_kind="heartbeat",
        origin="heartbeat",
    )


class _Profiles:
    def __init__(self, profile: WorkloadOAuthProfile) -> None:
        self.profile = profile

    async def get(self, _profile_id: str) -> WorkloadOAuthProfile:
        return self.profile


class _Store:
    def __init__(self, profile: WorkloadOAuthProfile) -> None:
        self.profile = profile

    async def get_metadata_snapshot(self, **_kwargs: Any) -> WorkloadMetadataSnapshot:
        return _snapshot(self.profile)

    async def get_active_profile_credential(
        self,
        **_kwargs: Any,
    ) -> WorkloadCredentialPayload:
        return _credential()

    async def has_blocking_authority_transition(self, **_kwargs: Any) -> bool:
        return False

    async def runtime_authority_ready(self) -> bool:
        return True


class _Owners:
    owner = "owner-1"

    async def active_owner_for_agent(self, _agent_id: str) -> str | None:
        return self.owner


class _Policy:
    digest = f"sha256:{'4' * 64}"

    async def authorize(self, **_kwargs: Any) -> str:
        return self.digest

    async def revalidate(self, **_kwargs: Any) -> str:
        return self.digest


def _config() -> ProtocolServerConfig:
    return ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        auth_mode="oauth_client_credentials",
        auth_profile_id="calendar-workload",
        workload_authority_kind="direct",
    )


def _authorizer(
    *,
    profiles: _Profiles,
    owners: _Owners | None = None,
    policy: _Policy | None = None,
) -> Any:
    return _authority().WorkloadDirectAuthorizer(
        config_lookup=lambda _name: _config(),
        installation_generation_lookup=lambda _name: 4,
        profiles=profiles,
        store=_Store(profiles.profile),
        owners=owners or _Owners(),
        policy=policy or _Policy(),
        feature_enabled=lambda: True,
        clock=lambda: 100.0,
        ticket_ttl=10.0,
    )


@pytest.mark.asyncio
async def test_direct_authorizer_binds_exact_profile_owner_tool_and_credential() -> None:
    profiles = _Profiles(_profile())
    authorizer = _authorizer(profiles=profiles)

    ticket = await authorizer.authorize_operation(
        server_name="calendar",
        request_context=_context(),
        installation_generation=4,
        operation="tools/call",
        selector="calendar.read",
        arguments={"day": 1},
    )
    material = await authorizer.revalidate(ticket, now=105.0)

    assert ticket.owner_user_id == "owner-1"
    assert ticket.profile_generation == 6
    assert ticket.credential_generation == 2
    assert ticket.required_scopes == frozenset({"calendar.read"})
    assert material.credential.credential_id == "calendar-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["owner", "profile", "policy", "tool"])
async def test_direct_authorizer_revalidation_fails_closed(stale: str) -> None:
    profiles = _Profiles(_profile())
    owners = _Owners()
    policy = _Policy()
    authorizer = _authorizer(profiles=profiles, owners=owners, policy=policy)
    ticket = await authorizer.authorize_operation(
        server_name="calendar",
        request_context=_context(),
        installation_generation=4,
        operation="tools/call",
        selector="calendar.read",
        arguments={},
    )
    if stale == "owner":
        owners.owner = "owner-2"
    elif stale == "profile":
        profiles.profile = _profile(generation=7)
    elif stale == "policy":
        policy.digest = f"sha256:{'5' * 64}"
    else:
        profiles.profile = _profile(
            tool_scopes={"calendar.write": frozenset({"calendar.read"})}
        )

    with pytest.raises(_authority().WorkloadAuthorityDenied):
        await authorizer.revalidate(ticket, now=105.0)
