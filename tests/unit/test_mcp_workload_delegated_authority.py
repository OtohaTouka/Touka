"""P8-B Slice 5：owner delegation ticket 與 P8-A source-grant 重驗。"""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any

import pytest
from pydantic import SecretStr
from src.models.identity import AgentIdentity
from src.protocols.mcp.canonical import McpRequestContext
from src.protocols.mcp.oauth.models import (
    OAuthAuthorizationSlotKey,
    OAuthGrantBinding,
    OAuthGrantRecord,
    OAuthTokenSet,
)
from src.protocols.mcp.oauth.workload.delegation import DelegatedGrantAuthority
from src.protocols.mcp.oauth.workload.models import WorkloadDelegationRecord
from src.protocols.registry import ProtocolServerConfig


def _authority() -> Any:
    module_name = "src.protocols.mcp.oauth.workload.delegated_authority"
    assert importlib.util.find_spec(module_name) is not None, "delegated authority seam is missing"
    return importlib.import_module(module_name)


def _slot() -> OAuthAuthorizationSlotKey:
    return OAuthAuthorizationSlotKey(
        human_principal_id="owner-1",
        execution_agent_id="Alice",
        installation_id="calendar-user-source",
        installation_generation=9,
        auth_profile_id="calendar-user",
    )


def _delegation(**updates: Any) -> WorkloadDelegationRecord:
    values: dict[str, Any] = {
        "delegation_id": "delegation-1",
        "owner_user_id": "owner-1",
        "source_slot": _slot(),
        "source_profile_generation": 6,
        "source_grant_id": "grant-1",
        "source_grant_version": 2,
        "execution_agent_id": "Alice",
        "installation_id": "calendar",
        "installation_generation": 4,
        "raw_tool_name": "calendar.read",
        "required_scopes": frozenset({"calendar.read"}),
        "generation": 3,
        "status": "active",
        "issued_at": 90.0,
        "expires_at": 200.0,
    }
    values.update(updates)
    return WorkloadDelegationRecord(**values)


def _grant() -> OAuthGrantRecord:
    return OAuthGrantRecord(
        grant_id="grant-1",
        slot_digest="opaque-slot-digest",
        grant_version=2,
        status="active",
        binding=OAuthGrantBinding(
            issuer="https://issuer.example.test",
            resource="https://mcp.example.test",
            client_id="client-1",
            granted_scopes=frozenset({"calendar.read"}),
            profile_generation=6,
            profile_fingerprint="profile-fingerprint",
            registration_version=1,
            metadata_security_fingerprint="metadata-fingerprint",
        ),
        tokens=OAuthTokenSet(
            access_token=SecretStr("access-token"),
            refresh_token=SecretStr("refresh-token"),
            token_type="Bearer",
            expires_at=180.0,
            granted_scopes=frozenset({"calendar.read"}),
        ),
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


class _Delegations:
    def __init__(self, record: WorkloadDelegationRecord | None) -> None:
        self.record = record

    async def get(self, delegation_id: str) -> WorkloadDelegationRecord | None:
        assert delegation_id == "delegation-1"
        return self.record


class _Resolver:
    def __init__(self, delegations: _Delegations) -> None:
        self.delegations = delegations
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, **kwargs: Any) -> DelegatedGrantAuthority:
        self.calls.append(kwargs)
        record = self.delegations.record
        if record is None or kwargs["delegation_digest"] != record.digest:
            raise PermissionError("revoked")
        return DelegatedGrantAuthority(delegation=record, grant=_grant())


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
        auth_profile_id="calendar-user",
        workload_authority_kind="owner_delegation",
        workload_delegation_id="delegation-1",
        workload_source_installation_id="calendar-user-source",
    )


def _source_config() -> ProtocolServerConfig:
    return ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        auth_mode="oauth_user",
        auth_profile_id="calendar-user",
    )


def _authorizer(
    delegations: _Delegations,
    *,
    owners: _Owners | None = None,
    policy: _Policy | None = None,
) -> tuple[Any, _Resolver]:
    resolver = _Resolver(delegations)
    authorizer = _authority().WorkloadDelegatedAuthorizer(
        config_lookup=lambda name: (
            _source_config() if name == "calendar-user-source" else _config()
        ),
        installation_generation_lookup=lambda name: (
            9 if name == "calendar-user-source" else 4
        ),
        delegations=delegations,
        resolver=resolver,
        owners=owners or _Owners(),
        policy=policy or _Policy(),
        feature_enabled=lambda: True,
        clock=lambda: 100.0,
        ticket_ttl=20.0,
    )
    return authorizer, resolver


@pytest.mark.asyncio
async def test_delegated_authorizer_binds_only_exact_p8a_source_authority() -> None:
    delegations = _Delegations(_delegation())
    authorizer, resolver = _authorizer(delegations)

    ticket = await authorizer.authorize_operation(
        server_name="calendar",
        request_context=_context(),
        installation_generation=4,
        operation="tools/call",
        selector="calendar.read",
        arguments={"day": 1},
    )
    material = await authorizer.revalidate(ticket, now=105.0)

    assert ticket.authority_kind == "delegation"
    assert ticket.human_principal_id == "owner-1"
    assert ticket.delegation_generation == 3
    assert ticket.credential_generation is None
    assert ticket.auth_profile_id == "calendar-user"
    assert ticket.profile_generation == 6
    assert material.source_slot == _slot()
    assert material.source_grant_id == "grant-1"
    assert material.source_grant_version == 2
    assert len(resolver.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["owner", "policy", "delegation", "tool"])
async def test_delegated_authorizer_revalidation_fails_closed(stale: str) -> None:
    delegations = _Delegations(_delegation())
    owners = _Owners()
    policy = _Policy()
    authorizer, _resolver = _authorizer(
        delegations,
        owners=owners,
        policy=policy,
    )
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
    elif stale == "policy":
        policy.digest = f"sha256:{'5' * 64}"
    elif stale == "delegation":
        delegations.record = _delegation(generation=4)
    else:
        delegations.record = _delegation(raw_tool_name="calendar.write")

    with pytest.raises((PermissionError, _authority().WorkloadDelegatedAuthorityDenied)):
        await authorizer.revalidate(ticket, now=105.0)
