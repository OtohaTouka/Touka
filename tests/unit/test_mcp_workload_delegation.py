"""P8-B Slice 4：delegation policy SSOT 與 exact P8-A grant resolver。"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from src.protocols.mcp.oauth.models import (
    OAuthAuthorizationSlotKey,
    OAuthGrantBinding,
    OAuthGrantRecord,
    OAuthTokenSet,
)

if TYPE_CHECKING:
    from pathlib import Path


def _delegation() -> Any:
    module_name = "src.protocols.mcp.oauth.workload.delegation"
    assert importlib.util.find_spec(module_name) is not None, "delegation seam is missing"
    return importlib.import_module(module_name)


def _models() -> Any:
    return importlib.import_module("src.protocols.mcp.oauth.workload.models")


def _slot() -> OAuthAuthorizationSlotKey:
    return OAuthAuthorizationSlotKey(
        human_principal_id="owner-1",
        execution_agent_id="Alice",
        delegation_digest=None,
        installation_id="calendar-user-source",
        installation_generation=9,
        auth_profile_id="calendar-user",
    )


def _record(**updates: Any) -> Any:
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
        "generation": 1,
        "status": "active",
        "issued_at": 100.0,
        "expires_at": 200.0,
    }
    values.update(updates)
    return _models().WorkloadDelegationRecord(**values)


def _grant(**updates: Any) -> OAuthGrantRecord:
    values: dict[str, Any] = {
        "grant_id": "grant-1",
        "slot_digest": "opaque-keyed-slot-digest",
        "grant_version": 2,
        "status": "active",
        "binding": OAuthGrantBinding(
            issuer="https://issuer.example.test",
            resource="https://mcp.example.test",
            client_id="client-1",
            granted_scopes=frozenset({"calendar.read"}),
            profile_generation=6,
            profile_fingerprint="profile-fingerprint",
            registration_version=1,
            metadata_security_fingerprint="metadata-fingerprint",
        ),
        "tokens": OAuthTokenSet(
            access_token=SecretStr("access-token"),
            refresh_token=SecretStr("refresh-token"),
            token_type="Bearer",
            expires_at=180.0,
            granted_scopes=frozenset({"calendar.read"}),
        ),
    }
    values.update(updates)
    return OAuthGrantRecord(**values)


class _Mirror:
    def __init__(self, record: Any | None) -> None:
        self.record = record

    async def get_active_delegation(self, **_kwargs: Any) -> Any | None:
        return self.record

    async def has_blocking_authority_transition(self, **_kwargs: Any) -> bool:
        return False

    async def runtime_authority_ready(self) -> bool:
        return True


class _Grants:
    def __init__(self, grant: OAuthGrantRecord | None) -> None:
        self.grant = grant

    async def get_active_grant(
        self,
        slot: OAuthAuthorizationSlotKey,
        *,
        now: float,
    ) -> OAuthGrantRecord | None:
        assert slot == _slot()
        assert now == 150.0
        return self.grant


@pytest.mark.asyncio
async def test_repository_round_trip_is_versioned_policy_ssot(tmp_path: Path) -> None:
    repository = _delegation().WorkloadDelegationRepository(
        tmp_path / "mcp_oauth_delegations.json"
    )
    record = _record()

    await repository.put(
        record,
        version="1.0.0",
        author="owner-1",
    )
    document = await repository.load()

    assert document.version == "1.0.0"
    assert document.author == "owner-1"
    assert document.delegations == {record.delegation_id: record}


@pytest.mark.asyncio
async def test_same_generation_delegation_contention_has_exactly_one_winner(
    tmp_path: Path,
) -> None:
    module = _delegation()
    path = tmp_path / "mcp_oauth_delegations.json"
    first = module.WorkloadDelegationRepository(path)
    second = module.WorkloadDelegationRepository(path)
    candidates = (_record(), _record(expires_at=201.0))

    results = await asyncio.gather(
        first.put(candidates[0], version="1.0.0", author="owner-1"),
        second.put(candidates[1], version="1.0.0", author="owner-2"),
        return_exceptions=True,
    )

    winners = [result for result in results if not isinstance(result, BaseException)]
    conflicts = [
        result
        for result in results
        if isinstance(result, module.WorkloadDelegationConflictError)
    ]
    assert len(winners) == 1
    assert len(conflicts) == 1
    assert await first.get("delegation-1") == winners[0]


@pytest.mark.asyncio
async def test_resolver_requires_exact_json_mirror_and_source_grant() -> None:
    record = _record()
    resolver = _delegation().WorkloadDelegationResolver(
        policy_lookup=lambda _delegation_id: record,
        mirror=_Mirror(record),
        grants=_Grants(_grant()),
    )

    authority = await resolver.resolve(
        delegation_id=record.delegation_id,
        delegation_digest=record.digest,
        owner_user_id="owner-1",
        execution_agent_id="Alice",
        installation_id="calendar",
        installation_generation=4,
        raw_tool_name="calendar.read",
        required_scopes=frozenset({"calendar.read"}),
        now=150.0,
    )

    assert authority.delegation == record
    assert authority.grant.grant_id == "grant-1"


@pytest.mark.asyncio
async def test_resolver_accepts_monotonic_refresh_of_same_source_grant_family() -> None:
    record = _record()
    resolver = _delegation().WorkloadDelegationResolver(
        policy_lookup=lambda _delegation_id: record,
        mirror=_Mirror(record),
        grants=_Grants(_grant(grant_version=3)),
    )

    authority = await resolver.resolve(
        delegation_id=record.delegation_id,
        delegation_digest=record.digest,
        owner_user_id="owner-1",
        execution_agent_id="Alice",
        installation_id="calendar",
        installation_generation=4,
        raw_tool_name="calendar.read",
        required_scopes=frozenset({"calendar.read"}),
        now=150.0,
    )

    assert authority.grant.grant_version == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["expired", "mirror", "grant", "tool"])
async def test_resolver_fails_closed_for_any_stale_binding(failure: str) -> None:
    record = _record(expires_at=140.0) if failure == "expired" else _record()
    mirror_record = None if failure == "mirror" else record
    grant = _grant(grant_id="grant-other") if failure == "grant" else _grant()
    resolver = _delegation().WorkloadDelegationResolver(
        policy_lookup=lambda _delegation_id: record,
        mirror=_Mirror(mirror_record),
        grants=_Grants(grant),
    )

    with pytest.raises(_delegation().WorkloadDelegationDenied):
        await resolver.resolve(
            delegation_id=record.delegation_id,
            delegation_digest=record.digest,
            owner_user_id="owner-1",
            execution_agent_id="Alice",
            installation_id="calendar",
            installation_generation=4,
            raw_tool_name=("calendar.write" if failure == "tool" else "calendar.read"),
            required_scopes=frozenset({"calendar.read"}),
            now=150.0,
        )
