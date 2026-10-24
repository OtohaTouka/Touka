"""P8-A encrypted SQLite transaction/grant repository。"""

from __future__ import annotations

import asyncio
import base64
import importlib
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from pydantic import SecretStr

if TYPE_CHECKING:
    from pathlib import Path


def _modules():
    return (
        importlib.import_module("src.protocols.mcp.oauth.keyring"),
        importlib.import_module("src.protocols.mcp.oauth.models"),
        importlib.import_module("src.protocols.mcp.oauth.store"),
    )


def _key_value() -> str:
    raw = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
    return f"v1:test-key:{raw}"


def _next_key_value() -> str:
    raw = base64.urlsafe_b64encode(bytes(reversed(range(32)))).decode("ascii").rstrip("=")
    return f"v1:next-key:{raw}"


def _slot(models):
    return models.OAuthAuthorizationSlotKey(
        human_principal_id="human-a",
        execution_agent_id="agent-a",
        delegation_digest=None,
        installation_id="server-a",
        installation_generation=3,
        auth_profile_id="profile-a",
    )


def _transaction_payload(models):
    return models.OAuthTransactionPayload(
        pkce_verifier=SecretStr("verifier-canary"),
        requested_scopes=frozenset({"tools.read"}),
        issuer="https://issuer.example.test",
        resource="https://mcp.example.test/mcp",
        redirect_uri="https://chat.example.test/mcp/oauth/callback",
        redirect_route="/mcp/oauth/callback",
        response_mode="form_post",
        profile_generation=1,
        profile_fingerprint="profile-fingerprint",
        auth_profile_id="profile-a",
        human_principal_id="human-a",
        execution_agent_id="agent-a",
        installation_id="server-a",
        installation_generation=3,
        authorization_endpoint="https://issuer.example.test/authorize",
        token_endpoint="https://issuer.example.test/token",
        revocation_endpoint="https://issuer.example.test/revoke",
        code_challenge_methods_supported=("S256",),
        response_modes_supported=("form_post",),
        authorization_response_iss_parameter_supported=True,
        metadata_security_fingerprint="metadata-fingerprint",
    )


def _binding(models):
    return models.OAuthGrantBinding(
        issuer="https://issuer.example.test",
        resource="https://mcp.example.test/mcp",
        client_id="client-a",
        granted_scopes=frozenset({"tools.read"}),
        profile_generation=1,
        profile_fingerprint="profile-fingerprint",
        registration_version=1,
        metadata_security_fingerprint="metadata-fingerprint",
    )


def _tokens(models, *, access: str = "access-canary", refresh: str = "refresh-canary"):
    return models.OAuthTokenSet(
        access_token=SecretStr(access),
        refresh_token=SecretStr(refresh),
        token_type="Bearer",
        expires_at=2000.0,
        granted_scopes=frozenset({"tools.read"}),
    )


async def _store(tmp_path: Path):
    keyring, _models_module, store_module = _modules()
    ring = keyring.OAuthKeyRing(current=keyring.OAuthKey.parse(_key_value()))
    store = store_module.OAuthSqliteStore(tmp_path / "mcp_oauth.sqlite3", ring)
    await store.prepare_schema()
    return store


async def _authorized_store(tmp_path: Path, models, tokens=None):
    store = await _store(tmp_path)
    transaction = await store.create_transaction(
        slot=_slot(models),
        operation_id="operation-a",
        raw_state="authorized-state",
        payload=_transaction_payload(models),
        now=1000.0,
        expires_at=1600.0,
    )
    await store.consume_transaction(
        "authorized-state",
        now=1001.0,
        redirect_route="/mcp/oauth/callback",
    )
    assert await store.commit_authorization(
        transaction_id=transaction.transaction_id,
        expected_slot_version=transaction.slot_version,
        binding=_binding(models),
        tokens=tokens or _tokens(models),
        now=1002.0,
    )
    return store


@pytest.mark.asyncio
async def test_transaction_persists_only_state_hmac_and_encrypted_payload(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _store(tmp_path)
    state = "raw-state-canary"
    payload = _transaction_payload(models)

    created = await store.create_transaction(
        slot=_slot(models),
        operation_id="operation-a",
        raw_state=state,
        payload=payload,
        now=1000.0,
        expires_at=1600.0,
    )
    loaded = await store.find_transaction(state, now=1000.0)

    assert created.status == models.OAuthTransactionStatus.PENDING
    assert loaded is not None
    assert loaded.payload.pkce_verifier.get_secret_value() == "verifier-canary"
    raw_db = (tmp_path / "mcp_oauth.sqlite3").read_bytes()
    assert state.encode() not in raw_db
    assert b"verifier-canary" not in raw_db


@pytest.mark.asyncio
async def test_callback_consume_has_exactly_one_cas_winner(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _store(tmp_path)
    await store.create_transaction(
        slot=_slot(models),
        operation_id="operation-a",
        raw_state="single-use-state",
        payload=_transaction_payload(models),
        now=1000.0,
        expires_at=1600.0,
    )

    first, second = await asyncio.gather(
        store.consume_transaction(
            "single-use-state", now=1001.0, redirect_route="/mcp/oauth/callback"
        ),
        store.consume_transaction(
            "single-use-state", now=1001.0, redirect_route="/mcp/oauth/callback"
        ),
    )

    assert sum(item is not None for item in (first, second)) == 1
    assert next(item for item in (first, second) if item is not None).status == (
        models.OAuthTransactionStatus.CONSUMING
    )


@pytest.mark.asyncio
async def test_concurrent_start_persists_only_one_live_transaction_per_slot(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _store(tmp_path)

    results = await asyncio.gather(
        store.create_transaction(
            slot=_slot(models),
            operation_id="operation-a",
            raw_state="state-a",
            payload=_transaction_payload(models),
            now=1000.0,
            expires_at=1600.0,
        ),
        store.create_transaction(
            slot=_slot(models),
            operation_id="operation-b",
            raw_state="state-b",
            payload=_transaction_payload(models),
            now=1000.0,
            expires_at=1600.0,
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(item, models.OAuthTransactionRecord) for item in results) == 1
    assert sum(isinstance(item, models.OAuthTransactionConflictError) for item in results) == 1
    pending = await store.find_pending_for_slot(_slot(models), now=1001.0)
    assert pending is not None
    assert pending.operation_id in {"operation-a", "operation-b"}


@pytest.mark.asyncio
async def test_expiry_boundary_is_invalid_and_route_mismatch_does_not_consume(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _store(tmp_path)
    await store.create_transaction(
        slot=_slot(models),
        operation_id="operation-a",
        raw_state="route-state",
        payload=_transaction_payload(models),
        now=1000.0,
        expires_at=1600.0,
    )

    with pytest.raises(models.OAuthCallbackRouteError):
        await store.consume_transaction(
            "route-state", now=1001.0, redirect_route="/v1/mcp/oauth/callback"
        )
    assert await store.find_transaction("route-state", now=1001.0) is not None
    assert await store.find_transaction("route-state", now=1600.0) is None


@pytest.mark.asyncio
async def test_grant_and_active_pointer_commit_atomically_and_stale_slot_is_fenced(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _store(tmp_path)
    transaction = await store.create_transaction(
        slot=_slot(models),
        operation_id="operation-a",
        raw_state="grant-state",
        payload=_transaction_payload(models),
        now=1000.0,
        expires_at=1600.0,
    )
    consumed = await store.consume_transaction(
        "grant-state", now=1001.0, redirect_route="/mcp/oauth/callback"
    )
    assert consumed is not None

    committed = await store.commit_authorization(
        transaction_id=transaction.transaction_id,
        expected_slot_version=transaction.slot_version,
        binding=_binding(models),
        tokens=_tokens(models),
        now=1002.0,
    )
    active = await store.get_active_grant(_slot(models), now=1002.0)

    assert committed is True
    assert active is not None
    assert active.tokens.access_token.get_secret_value() == "access-canary"
    assert (
        await store.commit_authorization(
            transaction_id=transaction.transaction_id,
            expected_slot_version=transaction.slot_version,
            binding=_binding(models),
            tokens=_tokens(models, access="stale-access"),
            now=1003.0,
        )
        is False
    )
    raw_db = (tmp_path / "mcp_oauth.sqlite3").read_bytes()
    assert b"access-canary" not in raw_db
    assert b"refresh-canary" not in raw_db


@pytest.mark.asyncio
async def test_refresh_lease_is_single_winner_and_stale_response_cannot_rotate(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _store(tmp_path)
    transaction = await store.create_transaction(
        slot=_slot(models),
        operation_id="operation-a",
        raw_state="refresh-state",
        payload=_transaction_payload(models),
        now=1000.0,
        expires_at=1600.0,
    )
    await store.consume_transaction(
        "refresh-state", now=1001.0, redirect_route="/mcp/oauth/callback"
    )
    await store.commit_authorization(
        transaction_id=transaction.transaction_id,
        expected_slot_version=transaction.slot_version,
        binding=_binding(models),
        tokens=_tokens(models),
        now=1002.0,
    )
    active = await store.get_active_grant(_slot(models), now=1002.0)
    assert active is not None

    first, second = await asyncio.gather(
        store.acquire_refresh_lease(
            active.grant_id, active.grant_version, "worker-a", now=1900.0, lease_seconds=30
        ),
        store.acquire_refresh_lease(
            active.grant_id, active.grant_version, "worker-b", now=1900.0, lease_seconds=30
        ),
    )
    lease = next(item for item in (first, second) if item is not None)
    assert sum(item is not None for item in (first, second)) == 1
    assert (
        await store.commit_refresh(
            lease,
            _tokens(models, access="rotated-access", refresh="rotated-refresh"),
            binding=_binding(models),
            now=1901.0,
        )
        is True
    )
    assert (
        await store.commit_refresh(
            lease,
            _tokens(models, access="stale-access", refresh="stale-refresh"),
            binding=_binding(models),
            now=1902.0,
        )
        is False
    )

    rotated = await store.get_active_grant(_slot(models), now=1902.0)
    assert rotated is not None
    assert rotated.grant_version == active.grant_version + 1
    assert rotated.tokens.access_token.get_secret_value() == "rotated-access"


@pytest.mark.asyncio
async def test_refresh_scope_narrowing_creates_new_full_tuple_and_moves_pointer(
    tmp_path,
):
    _keyring, models, _store_module = _modules()
    store = await _authorized_store(tmp_path, models)
    active = await store.get_active_grant(_slot(models), now=1003.0)
    assert active is not None
    lease = await store.acquire_refresh_lease(
        active.grant_id,
        active.grant_version,
        "worker-a",
        now=1900.0,
        lease_seconds=30.0,
    )
    assert lease is not None
    narrowed_binding = active.binding.model_copy(
        update={"granted_scopes": frozenset({"resources.read"})}
    )
    narrowed_tokens = _tokens(
        models,
        access="narrowed-access",
        refresh="narrowed-refresh",
    ).model_copy(update={"granted_scopes": frozenset({"resources.read"})})

    assert await store.commit_refresh(
        lease,
        narrowed_tokens,
        binding=narrowed_binding,
        now=1901.0,
    )
    narrowed = await store.get_active_grant(_slot(models), now=1902.0)

    assert narrowed is not None
    assert narrowed.grant_id != active.grant_id
    assert narrowed.grant_version == 1
    assert narrowed.binding.granted_scopes == frozenset({"resources.read"})
    assert not await store.assert_bearer_usable(
        _slot(models),
        active.grant_id,
        active.grant_version,
        now=1902.0,
    )


@pytest.mark.asyncio
async def test_bearer_fence_rejects_pointer_version_and_expiry_drift(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _authorized_store(tmp_path, models)
    active = await store.get_active_grant(_slot(models), now=1003.0)
    assert active is not None

    assert await store.assert_bearer_usable(
        _slot(models), active.grant_id, active.grant_version, now=1003.0
    )
    assert not await store.assert_bearer_usable(
        _slot(models), active.grant_id, active.grant_version + 1, now=1003.0
    )
    assert not await store.assert_bearer_usable(
        _slot(models), active.grant_id, active.grant_version, now=2000.0
    )


@pytest.mark.asyncio
async def test_refresh_failure_release_is_lease_fenced_and_expiry_aware(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _authorized_store(tmp_path, models)
    active = await store.get_active_grant(_slot(models), now=1003.0)
    assert active is not None
    lease = await store.acquire_refresh_lease(
        active.grant_id,
        active.grant_version,
        "worker-a",
        now=1900.0,
        lease_seconds=30,
    )
    assert lease is not None
    stale = lease.model_copy(update={"fence": "stale"})

    assert not await store.release_refresh_lease(stale, now=1901.0)
    assert await store.release_refresh_lease(lease, now=1901.0)
    snapshot = await store.get_slot_snapshot(_slot(models), now=1901.0)
    assert snapshot.grant_status == models.OAuthGrantStatus.ACTIVE


@pytest.mark.asyncio
async def test_invalid_grant_tombstone_clears_only_exact_active_pointer(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _authorized_store(tmp_path, models)
    active = await store.get_active_grant(_slot(models), now=1003.0)
    assert active is not None
    lease = await store.acquire_refresh_lease(
        active.grant_id,
        active.grant_version,
        "worker-a",
        now=1900.0,
        lease_seconds=30,
    )
    assert lease is not None

    assert await store.tombstone_invalid_grant(lease, now=1901.0)
    assert await store.get_active_grant(_slot(models), now=1901.0) is None
    assert not await store.tombstone_invalid_grant(lease, now=1902.0)


@pytest.mark.asyncio
async def test_durable_clock_watermark_freezes_on_excessive_rollback(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _authorized_store(tmp_path, models)

    assert await store.observe_clock(now=2000.0, max_rollback_seconds=5.0)
    assert await store.observe_clock(now=1995.0, max_rollback_seconds=5.0)
    assert not await store.observe_clock(now=1994.999, max_rollback_seconds=5.0)

    snapshot = await store.get_slot_snapshot(_slot(models), now=2000.0)
    assert snapshot.grant_status == models.OAuthGrantStatus.FROZEN


@pytest.mark.asyncio
async def test_cleanup_recovers_expired_refresh_lease_and_purges_terminal_rows(
    tmp_path,
):
    _keyring, models, _store_module = _modules()
    store = await _authorized_store(tmp_path, models)
    active = await store.get_active_grant(_slot(models), now=1003.0)
    assert active is not None
    lease = await store.acquire_refresh_lease(
        active.grant_id,
        active.grant_version,
        "crashed-worker",
        now=1900.0,
        lease_seconds=30.0,
    )
    assert lease is not None

    result = await store.cleanup(now=1930.0)

    assert result["refresh_leases_recovered"] == 1
    assert result["terminal_transactions_deleted"] == 1
    recovered = await store.get_slot_snapshot(_slot(models), now=1930.0)
    assert recovered.grant_status == models.OAuthGrantStatus.ACTIVE
    replacement = await store.acquire_refresh_lease(
        active.grant_id,
        active.grant_version,
        "replacement-worker",
        now=1930.0,
        lease_seconds=30.0,
    )
    assert replacement is not None


@pytest.mark.asyncio
async def test_metadata_drift_freeze_is_exact_and_blocks_bearer(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _authorized_store(tmp_path, models)
    active = await store.get_active_grant(_slot(models), now=1003.0)
    assert active is not None

    assert await store.freeze_grant(
        active.grant_id,
        active.grant_version,
        now=1004.0,
    )
    assert not await store.assert_bearer_usable(
        _slot(models), active.grant_id, active.grant_version, now=1005.0
    )
    snapshot = await store.get_slot_snapshot(_slot(models), now=1005.0)
    assert snapshot.grant_status == models.OAuthGrantStatus.FROZEN


@pytest.mark.asyncio
async def test_profile_mutation_fence_freezes_grants_and_cancels_pending_before_write(
    tmp_path,
):
    _keyring, models, store_module = _modules()
    store = await _authorized_store(tmp_path, models)
    await store.create_transaction(
        slot=_slot(models),
        operation_id="pending-profile-change",
        raw_state="pending-profile-state",
        payload=_transaction_payload(models),
        now=1100.0,
        expires_at=1700.0,
    )

    epoch = await store.begin_profile_mutation("profile-a", now=1200.0)
    snapshot = await store.get_slot_snapshot(_slot(models), now=1201.0)

    assert snapshot.grant_status == models.OAuthGrantStatus.FROZEN
    assert snapshot.transaction_status is None
    with pytest.raises(store_module.OAuthProfileMutationInProgressError):
        await store.create_transaction(
            slot=_slot(models),
            operation_id="must-fail",
            raw_state="must-fail-state",
            payload=_transaction_payload(models),
            now=1201.0,
            expires_at=1800.0,
        )
    assert not await store.complete_profile_mutation(
        "profile-a",
        epoch + 1,
        applied_fingerprint="stale",
        now=1202.0,
    )
    assert await store.complete_profile_mutation(
        "profile-a",
        epoch,
        applied_fingerprint="new-fingerprint",
        now=1202.0,
    )
    assert await store.list_updating_profile_fences() == []


@pytest.mark.asyncio
async def test_slot_snapshot_is_exact_and_derived_from_persisted_state(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _authorized_store(tmp_path, models)

    active = await store.get_slot_snapshot(_slot(models), now=1003.0)
    other_slot = _slot(models).model_copy(update={"execution_agent_id": "agent-b"})
    missing = await store.get_slot_snapshot(other_slot, now=1003.0)

    assert active.grant_status == models.OAuthGrantStatus.ACTIVE
    assert active.operation_id is None
    assert active.access_expires_at == 2000.0
    assert missing.grant_status is None
    assert missing.transaction_status is None


@pytest.mark.asyncio
async def test_disconnect_is_exact_two_phase_and_cancels_pending_transaction(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _authorized_store(tmp_path, models)
    await store.create_transaction(
        slot=_slot(models),
        operation_id="pending-reconnect",
        raw_state="pending-reconnect-state",
        payload=_transaction_payload(models),
        now=1100.0,
        expires_at=1700.0,
    )

    revoking = await store.begin_disconnect(_slot(models), now=1200.0)
    assert revoking is not None
    assert revoking.status == models.OAuthGrantStatus.REVOKING
    assert await store.complete_disconnect(
        revoking.grant_id,
        expected_grant_version=revoking.grant_version,
        now=1201.0,
    )
    snapshot = await store.get_slot_snapshot(_slot(models), now=1202.0)
    assert snapshot.grant_status is None
    assert snapshot.transaction_status is None


@pytest.mark.asyncio
async def test_stale_disconnect_cannot_clear_a_new_pointer(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _authorized_store(tmp_path, models)
    revoking = await store.begin_disconnect(_slot(models), now=1200.0)
    assert revoking is not None

    assert not await store.complete_disconnect(
        revoking.grant_id,
        expected_grant_version=revoking.grant_version + 1,
        now=1201.0,
    )
    snapshot = await store.get_slot_snapshot(_slot(models), now=1202.0)
    assert snapshot.grant_status == models.OAuthGrantStatus.REVOKING


@pytest.mark.asyncio
async def test_find_pending_for_slot_rejects_ambiguity_and_expires_exact_boundary(tmp_path):
    _keyring, models, _store_module = _modules()
    store = await _store(tmp_path)
    await store.create_transaction(
        slot=_slot(models),
        operation_id="pending-one",
        raw_state="pending-state-one",
        payload=_transaction_payload(models),
        now=1000.0,
        expires_at=1600.0,
    )
    pending = await store.find_pending_for_slot(_slot(models), now=1001.0)
    assert pending is not None
    assert pending.operation_id == "pending-one"
    assert await store.find_pending_for_slot(_slot(models), now=1600.0) is None


@pytest.mark.asyncio
async def test_corrupt_duplicate_live_transactions_fail_loud(tmp_path):
    _keyring, models, store_module = _modules()
    store = await _store(tmp_path)
    await store.create_transaction(
        slot=_slot(models),
        operation_id="pending-one",
        raw_state="pending-state-one",
        payload=_transaction_payload(models),
        now=1000.0,
        expires_at=1600.0,
    )
    async with aiosqlite.connect(tmp_path / "mcp_oauth.sqlite3") as db:
        await db.execute("DROP INDEX oauth_one_live_transaction_per_slot")
        await db.execute(
            "INSERT INTO oauth_transactions "
            "(transaction_id, operation_id, state_kid, state_digest, slot_digest, "
            "slot_version, status, expires_at, redirect_route, encrypted_payload, "
            "created_at) SELECT ?, ?, state_kid, ?, slot_digest, slot_version, status, "
            "expires_at, redirect_route, encrypted_payload, created_at "
            "FROM oauth_transactions LIMIT 1",
            ("corrupt-transaction", "pending-two", "corrupt-state-digest"),
        )
        await db.commit()

    with pytest.raises(store_module.OAuthStoreCorruptionError):
        await store.find_pending_for_slot(_slot(models), now=1001.0)


@pytest.mark.asyncio
async def test_corrupt_transaction_ciphertext_fails_loud(tmp_path):
    _keyring, models, store_module = _modules()
    store = await _store(tmp_path)
    await store.create_transaction(
        slot=_slot(models),
        operation_id="pending-one",
        raw_state="pending-state-one",
        payload=_transaction_payload(models),
        now=1000.0,
        expires_at=1600.0,
    )
    async with aiosqlite.connect(tmp_path / "mcp_oauth.sqlite3") as db:
        await db.execute(
            "UPDATE oauth_transactions SET encrypted_payload = ?",
            ("not-an-encrypted-payload",),
        )
        await db.commit()

    with pytest.raises(store_module.OAuthStoreCorruptionError):
        await store.find_pending_for_slot(_slot(models), now=1001.0)


@pytest.mark.asyncio
async def test_rotation_rekeys_slot_but_drains_raw_state_hmac_by_expiry(tmp_path):
    keyring_module, models, _store_module = _modules()
    store = await _store(tmp_path)
    await store.create_transaction(
        slot=_slot(models),
        operation_id="rotation-pending",
        raw_state="old-state-capability",
        payload=_transaction_payload(models),
        now=1000.0,
        expires_at=1600.0,
    )
    old_key = keyring_module.OAuthKey.parse(_key_value())
    new_key = keyring_module.OAuthKey.parse(_next_key_value())
    store.replace_keyring(keyring_module.OAuthKeyRing(current=new_key, retiring=(old_key,)))

    result = await store.reencrypt_and_rekey_batch(
        retiring_kid=old_key.kid,
        limit=100,
    )

    assert result["payloads_reencrypted"] >= 2
    assert result["slots_rekeyed"] == 1
    references = await store.count_key_references(old_key.kid)
    assert references["state_indexes"] == 1
    assert sum(value for name, value in references.items() if name != "state_indexes") == 0
    assert await store.find_pending_for_slot(_slot(models), now=1001.0) is not None
    assert await store.find_transaction("old-state-capability", now=1001.0) is not None

    await store.cleanup(now=1600.0)
    assert sum((await store.count_key_references(old_key.kid)).values()) == 0
    store.replace_keyring(keyring_module.OAuthKeyRing(current=new_key))
    assert await store.find_pending_for_slot(_slot(models), now=1601.0) is None


@pytest.mark.asyncio
async def test_rotation_rekeys_registration_before_retiring_key_removal(tmp_path):
    keyring_module, _models, store_module = _modules()
    registration_module = importlib.import_module("src.protocols.mcp.oauth.registration")
    old_key = keyring_module.OAuthKey.parse(_key_value())
    new_key = keyring_module.OAuthKey.parse(_next_key_value())
    store = store_module.OAuthSqliteStore(
        tmp_path / "mcp_oauth.sqlite3",
        keyring_module.OAuthKeyRing(current=old_key),
    )
    await store.prepare_schema()
    registration_key = registration_module.OAuthRegistrationKey(
        profile_id="profile-a",
        profile_generation=1,
        installation_id="server-a",
        installation_generation=3,
        issuer="https://issuer.example.test",
    )
    lease = await store.acquire_registration_lease(
        registration_key,
        "worker-a",
        now=1000.0,
        lease_seconds=30.0,
    )
    assert lease is not None
    binding = registration_module.OAuthClientBinding(
        client_id="dynamic-client-a",
        registration_version=1,
    )
    assert await store.commit_registration(lease, binding, now=1001.0)
    store.replace_keyring(keyring_module.OAuthKeyRing(current=new_key, retiring=(old_key,)))

    result = await store.reencrypt_and_rekey_batch(
        retiring_kid=old_key.kid,
        limit=100,
    )

    assert result["registrations_rekeyed"] == 1
    assert sum((await store.count_key_references(old_key.kid)).values()) == 0
    store.replace_keyring(keyring_module.OAuthKeyRing(current=new_key))
    assert await store.get_registration(registration_key, now=1002.0) == binding


_READ_ONLY_QUERIES = (
    "SELECT * FROM oauth_slots ORDER BY slot_digest",
    "SELECT * FROM oauth_grants ORDER BY grant_id",
    "SELECT * FROM oauth_transactions ORDER BY transaction_id",
    "SELECT * FROM oauth_runtime_state ORDER BY name",
)


async def _rows(path: Path) -> list[list[tuple]]:
    async with aiosqlite.connect(str(path)) as db:
        result = []
        for query in _READ_ONLY_QUERIES:
            async with db.execute(query) as cursor:
                result.append([tuple(row) for row in await cursor.fetchall()])
        return result


@pytest.mark.asyncio
async def test_usable_grant_check_for_tool_listing_is_read_only(tmp_path):
    """ADR-0281 T1：組工具清單的授權查詢不寫資料庫、不推進時鐘高水位、不把過期授權改狀態。"""
    _keyring, models, _store_module = _modules()
    store = await _authorized_store(tmp_path, models)
    path = tmp_path / "mcp_oauth.sqlite3"
    other_slot = _slot(models).model_copy(update={"execution_agent_id": "agent-b"})
    before = await _rows(path)

    assert await store.has_usable_grant(_slot(models), now=1500.0) is True
    assert await store.has_usable_grant(_slot(models), now=5000.0) is True  # 有 refresh 可續
    assert await store.has_usable_grant(other_slot, now=1500.0) is False

    assert await _rows(path) == before


@pytest.mark.asyncio
async def test_usable_grant_check_fails_loud_on_a_dangling_pointer(tmp_path):
    _keyring, models, store_module = _modules()
    store = await _authorized_store(tmp_path, models)
    async with aiosqlite.connect(str(tmp_path / "mcp_oauth.sqlite3")) as db:
        await db.execute("PRAGMA foreign_keys = OFF")
        await db.execute("UPDATE oauth_slots SET active_grant_id = ?", ("missing-grant",))
        await db.commit()

    with pytest.raises(store_module.OAuthStoreCorruptionError):
        await store.has_usable_grant(_slot(models), now=1500.0)


@pytest.mark.asyncio
async def test_expired_grant_without_refresh_is_unusable_and_left_untouched(tmp_path):
    """過期又沒有 refresh：回不能用，但不像 get_active_grant 那樣把狀態改成 EXPIRED。"""
    _keyring, models, _store_module = _modules()
    no_refresh = _tokens(models).model_copy(update={"refresh_token": None})
    store = await _authorized_store(tmp_path, models, tokens=no_refresh)
    path = tmp_path / "mcp_oauth.sqlite3"
    before = await _rows(path)

    assert await store.has_usable_grant(_slot(models), now=1500.0) is True
    assert await store.has_usable_grant(_slot(models), now=5000.0) is False

    assert await _rows(path) == before


@pytest.mark.asyncio
async def test_usable_grant_check_never_decrypts_and_reads_in_one_statement(
    tmp_path, monkeypatch
):
    """不解密；指標與授權用同一句讀（不經兩段式的 _find_slot_row，中途提交不被誤判成損毀）。"""
    _keyring, models, _store_module = _modules()
    store = await _authorized_store(tmp_path, models)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError

    monkeypatch.setattr(store._keyring, "decrypt", _forbidden)
    monkeypatch.setattr(store, "_decrypt_grant_row", _forbidden)
    monkeypatch.setattr(store, "_find_slot_row", _forbidden)

    assert await store.has_usable_grant(_slot(models), now=1500.0) is True
