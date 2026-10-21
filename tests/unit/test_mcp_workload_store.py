"""P8-B Slice 2：加密 store、authority epoch 與 transactional audit outbox。"""

from __future__ import annotations

import importlib
import importlib.util
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest
from pydantic import SecretStr
from src.protocols.mcp.oauth.keyring import OAuthKey, OAuthKeyRing
from src.protocols.mcp.oauth.workload.metadata import verify_workload_metadata
from src.protocols.mcp.oauth.workload.models import WorkloadOAuthProfile

if TYPE_CHECKING:
    from pathlib import Path


def _store_module() -> Any:
    module_name = "src.protocols.mcp.oauth.workload.store"
    assert importlib.util.find_spec(module_name) is not None, "workload store seam is missing"
    return importlib.import_module(module_name)


def _models() -> Any:
    return importlib.import_module("src.protocols.mcp.oauth.workload.models")


def _keyring() -> OAuthKeyRing:
    return OAuthKeyRing(current=OAuthKey(kid="workload-k1", material=b"k" * 32))


def _credential() -> Any:
    return _models().WorkloadCredentialPayload(
        profile_id="calendar-workload",
        credential_id="calendar-secret",
        generation=2,
        client_id=SecretStr("client-identifier"),
        client_secret=SecretStr("super-secret-value"),
    )


def _event(event_id: str, action: str = "credential.activate") -> Any:
    return _models().WorkloadAuditEvent(
        event_id=event_id,
        action=action,
        actor_id="human-1",
        authority_id="calendar-secret",
        authority_generation=2,
        request_id="request-1",
        outcome="success",
        created_at=100.0,
        details_digest=f"sha256:{'4' * 64}",
    )


@pytest.mark.asyncio
async def test_credential_is_encrypted_and_audit_intent_is_same_transaction(
    tmp_path: Path,
) -> None:
    store_module = _store_module()
    db_path = tmp_path / "mcp_oauth_workload.sqlite3"
    store = store_module.WorkloadOAuthStore(db_path, _keyring())
    await store.prepare_schema()

    await store.activate_credential(
        _credential(),
        profile_generation=3,
        now=100.0,
        event=_event("event-activate"),
    )

    loaded = await store.get_active_credential(
        profile_id="calendar-workload",
        profile_generation=3,
        credential_id="calendar-secret",
        credential_generation=2,
    )
    assert loaded is not None
    assert loaded.client_secret.get_secret_value() == "super-secret-value"
    assert (
        await store.get_active_profile_credential(
            profile_id="calendar-workload",
            profile_generation=3,
            credential_id="calendar-secret",
        )
        == loaded
    )
    raw_database = db_path.read_bytes()
    assert b"super-secret-value" not in raw_database
    assert b"client-identifier" not in raw_database
    assert [event.event_id for event in await store.pending_audit_events()] == [
        "event-activate"
    ]


@pytest.mark.asyncio
async def test_ciphertext_corruption_fails_loud(tmp_path: Path) -> None:
    store_module = _store_module()
    db_path = tmp_path / "mcp_oauth_workload.sqlite3"
    store = store_module.WorkloadOAuthStore(db_path, _keyring())
    await store.prepare_schema()
    await store.activate_credential(
        _credential(),
        profile_generation=3,
        now=100.0,
        event=_event("event-corrupt"),
    )
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE workload_credentials SET encrypted_payload = ?",
            ('{"version":1,"kid":"workload-k1","nonce":"AA","ciphertext":"AA"}',),
        )
        await db.commit()

    with pytest.raises(store_module.WorkloadStoreCorruptionError):
        await store.get_active_credential(
            profile_id="calendar-workload",
            profile_generation=3,
            credential_id="calendar-secret",
            credential_generation=2,
        )


@pytest.mark.asyncio
async def test_tombstone_epoch_and_audit_are_atomic_on_event_conflict(
    tmp_path: Path,
) -> None:
    store_module = _store_module()
    store = store_module.WorkloadOAuthStore(
        tmp_path / "mcp_oauth_workload.sqlite3",
        _keyring(),
    )
    await store.prepare_schema()
    duplicate = _event("same-event")
    await store.activate_credential(
        _credential(),
        profile_generation=3,
        now=100.0,
        event=duplicate,
    )
    await store.claim_mutation(
        operation_id="credential-tombstone-operation",
        mutation_kind="credential",
    )

    with pytest.raises(store_module.WorkloadAuditConflictError):
        await store.tombstone_credential(
            credential_id="calendar-secret",
            credential_generation=2,
            expected_authority_epoch=1,
            mutation_operation_id="credential-tombstone-operation",
            now=101.0,
            event=duplicate,
        )

    assert await store.authority_epoch() == 1
    assert (
        await store.get_active_credential(
            profile_id="calendar-workload",
            profile_generation=3,
            credential_id="calendar-secret",
            credential_generation=2,
        )
        is not None
    )

    await store.tombstone_credential(
        credential_id="calendar-secret",
        credential_generation=2,
        expected_authority_epoch=1,
        mutation_operation_id="credential-tombstone-operation",
        now=102.0,
        event=_event("event-revoke", "credential.tombstone"),
    )
    assert await store.authority_epoch() == 2
    assert (
        await store.get_active_credential(
            profile_id="calendar-workload",
            profile_generation=3,
            credential_id="calendar-secret",
            credential_generation=2,
        )
        is None
    )


@pytest.mark.asyncio
async def test_revocation_draining_record_is_atomic_with_tombstone(tmp_path: Path) -> None:
    store_module = _store_module()
    store = store_module.WorkloadOAuthStore(
        tmp_path / "mcp_oauth_workload.sqlite3",
        _keyring(),
    )
    await store.prepare_schema()
    await store.activate_credential(
        _credential(),
        profile_generation=3,
        now=100.0,
        event=_event("event-activate-revocation"),
    )
    await store.claim_mutation(
        operation_id="credential-revocation-operation",
        mutation_kind="credential",
    )

    new_epoch = await store.tombstone_credential(
        credential_id="calendar-secret",
        credential_generation=2,
        expected_authority_epoch=1,
        authority_key="profile:calendar-workload:3",
        mutation_operation_id="credential-revocation-operation",
        now=101.0,
        event=_event("event-revocation-start", "credential.revoke.start"),
    )

    assert new_epoch == 2
    assert (
        await store.get_revocation_status(
            authority_key="profile:calendar-workload:3",
            authority_generation=2,
        )
        == _models().WorkloadRevocationStatus.DRAINING
    )

    wrong_authority = _event(
        "event-revocation-wrong", "credential.revoke.complete"
    ).model_copy(update={"authority_id": "other-secret"})
    with pytest.raises(store_module.WorkloadAuthorityConflictError):
        await store.finalize_revocation(
            authority_key="profile:calendar-workload:3",
            authority_generation=2,
            mutation_operation_id="credential-revocation-operation",
            status=_models().WorkloadRevocationStatus.COMPLETED,
            now=102.0,
            event=wrong_authority,
        )

    await store.finalize_revocation(
        authority_key="profile:calendar-workload:3",
        authority_generation=2,
        mutation_operation_id="credential-revocation-operation",
        status=_models().WorkloadRevocationStatus.COMPLETED,
        now=103.0,
        event=_event("event-revocation-complete", "credential.revoke.complete"),
    )
    assert (
        await store.get_revocation_status(
            authority_key="profile:calendar-workload:3",
            authority_generation=2,
        )
        == _models().WorkloadRevocationStatus.COMPLETED
    )


@pytest.mark.asyncio
async def test_store_enforces_wal_and_full_synchronous_mode(tmp_path: Path) -> None:
    store_module = _store_module()
    store = store_module.WorkloadOAuthStore(
        tmp_path / "mcp_oauth_workload.sqlite3",
        _keyring(),
    )
    await store.prepare_schema()

    settings = await store.sqlite_durability_settings()
    assert settings == {"journal_mode": "wal", "synchronous": 2}


@pytest.mark.asyncio
async def test_verified_metadata_snapshot_is_durable_and_generation_exact(
    tmp_path: Path,
) -> None:
    store = _store_module().WorkloadOAuthStore(
        tmp_path / "mcp_oauth_workload.sqlite3",
        _keyring(),
    )
    await store.prepare_schema()
    profile = WorkloadOAuthProfile(
        profile_id="calendar-workload",
        enabled=True,
        expected_issuer="https://issuer.example.test",
        expected_resource="https://mcp.example.test",
        expected_transport_url="https://mcp.example.test/mcp",
        token_endpoint_auth_method="client_secret_basic",
        requested_scopes=frozenset({"calendar.read"}),
        workload_principal_id="heartbeat:Alice",
        owner_user_id="human-1",
        execution_agent_id="Alice",
        tool_scopes={"calendar.read": frozenset({"calendar.read"})},
        allow_private_network=False,
        generation=3,
        credential_id="calendar-secret",
    )
    snapshot = verify_workload_metadata(
        profile=profile,
        resource_metadata={
            "resource": profile.expected_resource,
            "authorization_servers": [profile.expected_issuer],
            "scopes_supported": ["calendar.read"],
        },
        authorization_metadata={
            "issuer": profile.expected_issuer,
            "token_endpoint": "https://issuer.example.test/oauth/token",
            "token_endpoint_auth_methods_supported": [
                profile.token_endpoint_auth_method
            ],
            "scopes_supported": ["calendar.read"],
        },
        verified_at=100.0,
    )
    event = _models().WorkloadAuditEvent(
        event_id="event-metadata",
        action="metadata.refresh",
        actor_id="human-1",
        authority_id=profile.profile_id,
        authority_generation=profile.generation,
        request_id="request-metadata",
        outcome="success",
        created_at=100.0,
        details_digest=snapshot.metadata_security_fingerprint,
    )

    await store.activate_metadata_snapshot(snapshot, event=event)

    assert (
        await store.get_metadata_snapshot(
            profile_id=profile.profile_id,
            profile_generation=profile.generation,
            profile_fingerprint=profile.fingerprint,
        )
        == snapshot
    )
    assert (
        await store.get_metadata_snapshot(
            profile_id=profile.profile_id,
            profile_generation=profile.generation + 1,
            profile_fingerprint=profile.fingerprint,
        )
        is None
    )


@pytest.mark.asyncio
async def test_revoked_credential_id_can_be_reinstalled_at_next_generation(
    tmp_path: Path,
) -> None:
    store_module = _store_module()
    store = store_module.WorkloadOAuthStore(
        tmp_path / "mcp_oauth_workload.sqlite3",
        _keyring(),
    )
    await store.prepare_schema()
    first = _credential().model_copy(update={"generation": 1})
    await store.rotate_credential(
        first,
        profile_generation=3,
        now=100.0,
        event=_event("event-first").model_copy(
            update={"authority_generation": 1}
        ),
    )
    await store.claim_mutation(
        operation_id="credential-reinstall-operation",
        mutation_kind="credential",
    )
    await store.tombstone_credential(
        credential_id=first.credential_id,
        credential_generation=1,
        expected_authority_epoch=2,
        mutation_operation_id="credential-reinstall-operation",
        now=101.0,
        event=_event("event-first-revoke", "credential.revoke").model_copy(
            update={"authority_generation": 1}
        ),
    )
    await store.complete_mutation(
        operation_id="credential-reinstall-operation",
        mutation_kind="credential",
    )
    second = first.model_copy(
        update={
            "generation": 2,
            "client_secret": SecretStr("replacement-secret"),
        }
    )

    new_epoch = await store.rotate_credential(
        second,
        profile_generation=3,
        now=102.0,
        event=_event("event-second").model_copy(
            update={"authority_generation": 2}
        ),
    )

    assert new_epoch == 4
    loaded = await store.get_active_profile_credential(
        profile_id=second.profile_id,
        profile_generation=3,
        credential_id=second.credential_id,
    )
    assert loaded is not None
    assert loaded.generation == 2
    assert loaded.client_secret.get_secret_value() == "replacement-secret"


@pytest.mark.asyncio
async def test_metadata_snapshot_is_encrypted_and_index_tamper_fails_loud(
    tmp_path: Path,
) -> None:
    store_module = _store_module()
    db_path = tmp_path / "mcp_oauth_workload.sqlite3"
    store = store_module.WorkloadOAuthStore(db_path, _keyring())
    await store.prepare_schema()
    profile = WorkloadOAuthProfile(
        profile_id="calendar-workload",
        enabled=True,
        expected_issuer="https://issuer.example.test",
        expected_resource="https://mcp.example.test",
        expected_transport_url="https://mcp.example.test/mcp",
        token_endpoint_auth_method="client_secret_basic",
        requested_scopes=frozenset({"calendar.read"}),
        workload_principal_id="heartbeat:Alice",
        owner_user_id="human-1",
        execution_agent_id="Alice",
        tool_scopes={"calendar.read": frozenset({"calendar.read"})},
        allow_private_network=False,
        generation=3,
        credential_id="calendar-secret",
    )
    snapshot = verify_workload_metadata(
        profile=profile,
        resource_metadata={
            "resource": profile.expected_resource,
            "authorization_servers": [profile.expected_issuer],
            "scopes_supported": ["calendar.read"],
        },
        authorization_metadata={
            "issuer": profile.expected_issuer,
            "token_endpoint": "https://issuer.example.test/oauth/token",
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            "scopes_supported": ["calendar.read"],
        },
        verified_at=100.0,
    )
    await store.activate_metadata_snapshot(
        snapshot,
        event=_models().WorkloadAuditEvent(
            event_id="event-encrypted-metadata",
            action="metadata.refresh",
            actor_id="human-1",
            authority_id=profile.profile_id,
            authority_generation=profile.generation,
            request_id="request-metadata",
            outcome="success",
            created_at=100.0,
            details_digest=snapshot.metadata_security_fingerprint,
        ),
    )

    raw_database = db_path.read_bytes()
    assert b"issuer.example.test/oauth/token" not in raw_database
    tampered_fingerprint = f"sha256:{'9' * 64}"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE workload_metadata_snapshots SET profile_fingerprint = ?",
            (tampered_fingerprint,),
        )
        await db.commit()

    with pytest.raises(store_module.WorkloadStoreCorruptionError):
        await store.get_metadata_snapshot(
            profile_id=profile.profile_id,
            profile_generation=profile.generation,
            profile_fingerprint=tampered_fingerprint,
        )

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE workload_metadata_snapshots "
            "SET profile_fingerprint = ?, encrypted_snapshot = ?",
            (profile.fingerprint, '{"ciphertext":"tampered"}'),
        )
        await db.commit()
    with pytest.raises(store_module.WorkloadStoreCorruptionError):
        await store.get_metadata_snapshot(
            profile_id=profile.profile_id,
            profile_generation=profile.generation,
            profile_fingerprint=profile.fingerprint,
        )


@pytest.mark.asyncio
async def test_runtime_authority_ready_includes_transition_and_revocation_blockers(
    tmp_path: Path,
) -> None:
    store = _store_module().WorkloadOAuthStore(
        tmp_path / "mcp_oauth_workload.sqlite3",
        _keyring(),
    )
    await store.prepare_schema()
    models = _models()
    transition_digest = f"sha256:{'7' * 64}"

    assert await store.runtime_authority_ready()
    await store.begin_authority_transition(
        operation_id="operation-pending",
        direction="expansion",
        authority_kind="profile",
        authority_id="calendar-workload",
        authority_generation=1,
        actor_id="admin-1",
        request_digest=transition_digest,
        now=100.0,
        event=models.WorkloadAuditEvent(
            event_id="event-transition-pending",
            action="profile.expansion.intent",
            actor_id="admin-1",
            authority_id="calendar-workload",
            authority_generation=1,
            request_id="request-transition",
            outcome="pending",
            created_at=100.0,
            details_digest=transition_digest,
        ),
    )
    assert not await store.runtime_authority_ready()


    await store.confirm_authority_transition(
        operation_id="operation-pending",
        actor_id="admin-1",
        request_digest=transition_digest,
        now=101.0,
        event=models.WorkloadAuditEvent(
            event_id="event-transition-complete",
            action="profile.expansion.commit",
            actor_id="admin-1",
            authority_id="calendar-workload",
            authority_generation=1,
            request_id="request-transition",
            outcome="success",
            created_at=101.0,
            details_digest=transition_digest,
        ),
    )
    assert await store.runtime_authority_ready()

    await store.claim_mutation(
        operation_id="pending-drain-operation",
        mutation_kind="recovery",
    )
    await store.record_pending_revocation(
        authority_key="global:2",
        authority_generation=2,
        authority_id="calendar-workload",
        mutation_operation_id="pending-drain-operation",
        now=102.0,
        event=models.WorkloadAuditEvent(
            event_id="event-revocation-pending",
            action="authority.drain.revocation_pending",
            actor_id="admin-1",
            authority_id="calendar-workload",
            authority_generation=2,
            request_id="request-revocation",
            outcome="failure",
            created_at=102.0,
            details_digest=f"sha256:{'8' * 64}",
        ),
    )
    assert not await store.runtime_authority_ready()


@pytest.mark.asyncio
async def test_legacy_unowned_revocation_migrates_to_manual_recovery(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mcp_oauth_workload.sqlite3"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE workload_revocations ("
            "authority_key TEXT NOT NULL, authority_generation INTEGER NOT NULL, "
            "credential_id TEXT NOT NULL, status TEXT NOT NULL, "
            "updated_at REAL NOT NULL, "
            "PRIMARY KEY(authority_key, authority_generation))"
        )
        await db.execute(
            "INSERT INTO workload_revocations "
            "(authority_key, authority_generation, credential_id, status, updated_at) "
            "VALUES ('legacy:authority', 2, 'legacy-id', 'revocation_pending', 90.0)"
        )
        await db.commit()

    store = _store_module().WorkloadOAuthStore(db_path, _keyring())
    await store.prepare_schema()

    blockers = await store.list_blocking_revocations()
    assert len(blockers) == 1
    assert blockers[0].status == _models().WorkloadRevocationStatus.RECOVERY_REQUIRED
    assert blockers[0].source_slot_digest is None
    assert blockers[0].mutation_operation_id is None
    assert not await store.runtime_authority_ready()


@pytest.mark.asyncio
async def test_durable_mutation_owner_is_exact_and_blocks_unowned_egress(
    tmp_path: Path,
) -> None:
    store_module = _store_module()
    store = store_module.WorkloadOAuthStore(
        tmp_path / "mcp_oauth_workload.sqlite3",
        _keyring(),
    )
    await store.prepare_schema()

    await store.claim_mutation(
        operation_id="operation-profile-1",
        mutation_kind="profile",
    )

    owner = await store.active_mutation()
    assert owner is not None
    assert owner.operation_id == "operation-profile-1"
    assert owner.mutation_kind == "profile"
    assert not await store.runtime_authority_ready()
    assert await store.runtime_authority_ready(
        mutation_operation_id="operation-profile-1"
    )

    await store.claim_mutation(
        operation_id="operation-profile-1",
        mutation_kind="profile",
    )
    with pytest.raises(store_module.WorkloadAuthorityConflictError):
        await store.claim_mutation(
            operation_id="operation-foreign",
            mutation_kind="credential",
        )
    with pytest.raises(store_module.WorkloadAuthorityConflictError):
        await store.complete_mutation(
            operation_id="operation-foreign",
            mutation_kind="profile",
        )

    await store.complete_mutation(
        operation_id="operation-profile-1",
        mutation_kind="profile",
    )
    assert await store.active_mutation() is None
    assert await store.runtime_authority_ready()


@pytest.mark.asyncio
async def test_same_generation_metadata_refresh_replaces_active_snapshot(
    tmp_path: Path,
) -> None:
    store = _store_module().WorkloadOAuthStore(
        tmp_path / "mcp_oauth_workload.sqlite3",
        _keyring(),
    )
    await store.prepare_schema()
    profile = WorkloadOAuthProfile(
        profile_id="calendar-workload",
        enabled=True,
        expected_issuer="https://issuer.example.test",
        expected_resource="https://mcp.example.test",
        expected_transport_url="https://mcp.example.test/mcp",
        token_endpoint_auth_method="client_secret_basic",
        requested_scopes=frozenset({"calendar.read"}),
        workload_principal_id="heartbeat:Alice",
        owner_user_id="human-1",
        execution_agent_id="Alice",
        tool_scopes={"calendar.read": frozenset({"calendar.read"})},
        allow_private_network=False,
        generation=3,
        credential_id="calendar-secret",
    )

    def snapshot(token_endpoint: str, verified_at: float) -> Any:
        return verify_workload_metadata(
            profile=profile,
            resource_metadata={
                "resource": profile.expected_resource,
                "authorization_servers": [profile.expected_issuer],
                "scopes_supported": ["calendar.read"],
            },
            authorization_metadata={
                "issuer": profile.expected_issuer,
                "token_endpoint": token_endpoint,
                "token_endpoint_auth_methods_supported": [
                    profile.token_endpoint_auth_method
                ],
                "scopes_supported": ["calendar.read"],
            },
            verified_at=verified_at,
        )

    first = snapshot("https://issuer.example.test/oauth/token-v1", 100.0)
    second = snapshot("https://issuer.example.test/oauth/token-v2", 101.0)
    for index, candidate in enumerate((first, second), start=1):
        await store.activate_metadata_snapshot(
            candidate,
            event=_models().WorkloadAuditEvent(
                event_id=f"event-metadata-{index}",
                action="metadata.refresh",
                actor_id="human-1",
                authority_id=profile.profile_id,
                authority_generation=profile.generation,
                request_id=f"request-metadata-{index}",
                outcome="success",
                created_at=candidate.verified_at,
                details_digest=candidate.metadata_security_fingerprint,
            ),
        )

    assert await store.get_metadata_snapshot(
        profile_id=profile.profile_id,
        profile_generation=profile.generation,
        profile_fingerprint=profile.fingerprint,
    ) == second
