"""P8-B Slice 4：delegation SQLite mirror 與 authority epoch。"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest
from src.protocols.mcp.oauth.keyring import OAuthKey, OAuthKeyRing
from src.protocols.mcp.oauth.models import OAuthAuthorizationSlotKey
from src.protocols.mcp.oauth.workload.models import (
    WorkloadAuditEvent,
    WorkloadDelegationRecord,
)
from src.protocols.mcp.oauth.workload.store import (
    WorkloadAuthorityConflictError,
    WorkloadOAuthStore,
)

if TYPE_CHECKING:
    from pathlib import Path


def _record(**updates: Any) -> WorkloadDelegationRecord:
    values: dict[str, Any] = {
        "delegation_id": "delegation-1",
        "owner_user_id": "owner-1",
        "source_slot": OAuthAuthorizationSlotKey(
            human_principal_id="owner-1",
            execution_agent_id="Alice",
            delegation_digest=None,
            installation_id="calendar-user-source",
            installation_generation=9,
            auth_profile_id="calendar-user",
        ),
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
    return WorkloadDelegationRecord(**values)


def _event(record: WorkloadDelegationRecord, event_id: str) -> WorkloadAuditEvent:
    return WorkloadAuditEvent(
        event_id=event_id,
        action="delegation.activate",
        actor_id="owner-1",
        authority_id=record.delegation_id,
        authority_generation=record.generation,
        request_id="request-delegation",
        outcome="success",
        created_at=100.0,
        details_digest=record.digest,
    )


def _store(path: Path) -> WorkloadOAuthStore:
    keyring = OAuthKeyRing(current=OAuthKey(kid="workload-k1", material=b"k" * 32))
    return WorkloadOAuthStore(path, keyring)


@pytest.mark.asyncio
async def test_delegation_mirror_is_exact_and_activation_advances_epoch(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "mcp_oauth_workload.sqlite3")
    await store.prepare_schema()
    record = _record()

    epoch = await store.activate_delegation(record, event=_event(record, "activate"))

    assert epoch == 2
    assert (
        await store.get_active_delegation(
            delegation_id=record.delegation_id,
            generation=record.generation,
            digest=record.digest,
            now=150.0,
        )
        == record
    )
    assert (
        await store.get_active_delegation(
            delegation_id=record.delegation_id,
            generation=record.generation + 1,
            digest=record.digest,
            now=150.0,
        )
        is None
    )
@pytest.mark.asyncio
async def test_delegation_expiry_and_tombstone_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path / "mcp_oauth_workload.sqlite3")
    await store.prepare_schema()
    record = _record()
    await store.activate_delegation(record, event=_event(record, "activate"))

    assert (
        await store.get_active_delegation(
            delegation_id=record.delegation_id,
            generation=record.generation,
            digest=record.digest,
            now=record.expires_at,
        )
        is None
    )
    tombstone_event = _event(record, "tombstone").model_copy(
        update={"action": "delegation.tombstone"}
    )
    await store.claim_mutation(
        operation_id="delegation-tombstone-operation",
        mutation_kind="delegation",
    )
    epoch = await store.tombstone_delegation(
        delegation_id=record.delegation_id,
        generation=record.generation,
        digest=record.digest,
        expected_authority_epoch=2,
        authority_key=f"delegation:{record.digest}",
        mutation_operation_id="delegation-tombstone-operation",
        source_slot_digest=None,
        now=160.0,
        event=tombstone_event,
    )

    assert epoch == 3
    assert (
        await store.get_active_delegation(
            delegation_id=record.delegation_id,
            generation=record.generation,
            digest=record.digest,
            now=161.0,
        )
        is None
    )


@pytest.mark.asyncio
async def test_source_disconnect_digest_is_authenticated_by_mirrored_record(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "mcp_oauth_workload.sqlite3")
    await store.prepare_schema()
    record = _record()
    await store.activate_delegation(record, event=_event(record, "activate-source"))
    await store.claim_mutation(
        operation_id="source-disconnect-operation",
        mutation_kind="source_disconnect",
    )

    with pytest.raises(WorkloadAuthorityConflictError):
        await store.tombstone_delegation(
            delegation_id=record.delegation_id,
            generation=record.generation,
            digest=record.digest,
            expected_authority_epoch=2,
            authority_key=f"delegation:{record.digest}",
            mutation_operation_id="source-disconnect-operation",
            source_slot_digest=f"sha256:{'0' * 64}",
            now=160.0,
            event=_event(record, "tombstone-source").model_copy(
                update={"action": "delegation.tombstone"}
            ),
        )

    assert await store.authority_epoch() == 2
    assert (
        await store.get_active_delegation(
            delegation_id=record.delegation_id,
            generation=record.generation,
            digest=record.digest,
            now=150.0,
        )
        == record
    )


@pytest.mark.asyncio
async def test_provable_legacy_source_revocation_is_backfilled(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mcp_oauth_workload.sqlite3"
    store = _store(db_path)
    await store.prepare_schema()
    record = _record()
    await store.activate_delegation(record, event=_event(record, "activate-legacy"))
    await store.claim_mutation(
        operation_id="legacy-source-operation",
        mutation_kind="source_disconnect",
    )
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE workload_delegations SET status = 'tombstoned' "
            "WHERE delegation_id = ? AND generation = ?",
            (record.delegation_id, record.generation),
        )
        await db.execute(
            "INSERT INTO workload_revocations "
            "(authority_key, authority_generation, credential_id, status, "
            "source_slot_digest, mutation_operation_id, updated_at) "
            "VALUES (?, ?, ?, 'revocation_pending', NULL, NULL, 160.0)",
            (
                f"delegation:{record.digest}",
                record.generation,
                record.delegation_id,
            ),
        )
        await db.commit()

    await store.prepare_schema()

    blockers = await store.list_blocking_revocations()
    assert len(blockers) == 1
    assert blockers[0].status == "revocation_pending"
    assert blockers[0].mutation_operation_id == "legacy-source-operation"
    assert blockers[0].source_slot_digest == (
        f"sha256:{hashlib.sha256(record.source_slot.canonical_bytes()).hexdigest()}"
    )
