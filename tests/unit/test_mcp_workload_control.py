"""P8-B admin/owner control-plane keeps secrets write-only and mirrors authority。"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from src.protocols.mcp.oauth.keyring import OAuthKey, OAuthKeyRing
from src.protocols.mcp.oauth.models import OAuthAuthorizationSlotKey
from src.protocols.mcp.oauth.workload.control import (
    WorkloadControlDenied,
    WorkloadControlRecoveryRequired,
    WorkloadOAuthControlService,
)
from src.protocols.mcp.oauth.workload.delegation import WorkloadDelegationRepository
from src.protocols.mcp.oauth.workload.models import WorkloadOAuthProfile
from src.protocols.mcp.oauth.workload.permit import (
    WorkloadEgressDenied,
    WorkloadEgressPermitManager,
)
from src.protocols.mcp.oauth.workload.profile import WorkloadProfileRepository
from src.protocols.mcp.oauth.workload.store import WorkloadOAuthStore
from src.protocols.registry import ProtocolServerConfig

if TYPE_CHECKING:
    from pathlib import Path


def _profile() -> WorkloadOAuthProfile:
    return WorkloadOAuthProfile(
        profile_id="calendar-workload",
        enabled=True,
        expected_issuer="https://issuer.example.test",
        expected_resource="https://mcp.example.test",
        expected_transport_url="https://mcp.example.test/mcp",
        token_endpoint_auth_method="client_secret_basic",
        requested_scopes=frozenset({"calendar.read"}),
        workload_principal_id="heartbeat:Alice",
        owner_user_id="owner-1",
        execution_agent_id="Alice",
        tool_scopes={"calendar.read": frozenset({"calendar.read"})},
        allow_private_network=False,
        generation=1,
        credential_id="calendar-secret",
    )


class _Outbound:
    async def get_json(self, url: str, **_kwargs: Any) -> dict[str, Any]:
        if "oauth-protected-resource" in url:
            return {
                "resource": "https://mcp.example.test",
                "authorization_servers": ["https://issuer.example.test"],
                "scopes_supported": ["calendar.read"],
            }
        return {
            "issuer": "https://issuer.example.test",
            "token_endpoint": "https://issuer.example.test/token",
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            "scopes_supported": ["calendar.read"],
        }


class _Permits:
    async def begin_global_revocation(self) -> str:
        return "fake-global-fence"

    async def abort_global_revocation(self, **_kwargs: Any) -> None:
        return None

    async def commit_global_revocation(self, **_kwargs: Any) -> frozenset[str]:
        return frozenset()

    async def complete_global_revocation(self, **_kwargs: Any) -> None:
        return None

    async def wait_all_drained(self, *, timeout: float) -> bool:
        return timeout >= 0

    async def begin_revocation(self, _authority_key: str) -> None:
        return None

    async def abort_revocation(self, _authority_key: str) -> None:
        return None

    async def commit_revocation(self, _authority_key: str, **_kwargs: Any) -> frozenset[str]:
        return frozenset()

    async def wait_drained(self, _authority_key: str, *, timeout: float) -> bool:
        return timeout >= 0


class _Runtime:
    async def cancel_tasks(self, _task_ids: frozenset[str]) -> None:
        return None

    def invalidate_authority(self, _authority_key: str) -> None:
        return None

    def invalidate_all_authorities(self) -> None:
        return None


class _Owners:
    async def active_owner_for_agent(self, agent_id: str) -> str | None:
        return "owner-1" if agent_id == "Alice" else None


class _Grants:
    async def get_active_grant(self, slot: Any, *, now: float) -> Any:
        assert now == 100.0
        assert slot.installation_id == "calendar-user-source"
        assert slot.installation_generation == 9
        return SimpleNamespace(
            grant_id="grant-1",
            grant_version=2,
            binding=SimpleNamespace(
                profile_generation=6,
                resource="https://mcp.example.test",
                granted_scopes=frozenset({"calendar.read"}),
            ),
            tokens=SimpleNamespace(
                token_type="Bearer",
                granted_scopes=frozenset({"calendar.read"}),
            ),
            status="active",
        )


async def _control(
    tmp_path: Path,
    *,
    source_grants: Any | None = None,
) -> tuple[WorkloadOAuthControlService, WorkloadOAuthStore]:
    store = WorkloadOAuthStore(
        tmp_path / "mcp_oauth_workload.sqlite3",
        OAuthKeyRing(current=OAuthKey(kid="workload-k1", material=b"k" * 32)),
    )
    await store.prepare_schema()
    config = ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        auth_mode="oauth_client_credentials",
        auth_profile_id="calendar-user",
        workload_authority_kind="owner_delegation",
        workload_delegation_id="delegation-1",
        workload_source_installation_id="calendar-user-source",
    )
    secondary_config = config.model_copy(
        update={"workload_delegation_id": "delegation-2"}
    )
    source_config = ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        auth_mode="oauth_user",
        auth_profile_id="calendar-user",
    )
    control = WorkloadOAuthControlService(
        profiles=WorkloadProfileRepository(
            tmp_path / "mcp_oauth_workload_profiles.json"
        ),
        delegations=WorkloadDelegationRepository(
            tmp_path / "mcp_oauth_workload_delegations.json"
        ),
        store=store,
        outbound=_Outbound(),
        permits=_Permits(),
        runtime=_Runtime(),
        owners=_Owners(),
        source_grants=source_grants or _Grants(),
        config_lookup=lambda name: (
            config
            if name == "calendar"
            else secondary_config
            if name == "calendar-secondary"
            else source_config
            if name == "calendar-user-source"
            else None
        ),
        installation_generation_lookup=lambda name: (
            9 if name == "calendar-user-source" else 4
        ),
        close_handles=lambda _authority_key: _async_none(),
        clock=lambda: 100.0,
    )
    return control, store


async def _async_none() -> None:
    return None


class _ControlledRuntime:
    def __init__(self, *, release_on_cancel: bool) -> None:
        self.release_on_cancel = release_on_cancel
        self.cancel_called = asyncio.Event()
        self.allow_release = asyncio.Event()
        self.permits: WorkloadEgressPermitManager | None = None
        self.permit_id: str | None = None

    async def cancel_tasks(self, task_ids: frozenset[str]) -> None:
        if not task_ids:
            return
        self.cancel_called.set()
        if self.release_on_cancel:
            await self.allow_release.wait()
            assert self.permits is not None
            assert self.permit_id is not None
            await self.permits.release(self.permit_id)

    def invalidate_all_authorities(self) -> None:
        return None


async def _control_with_real_permits(
    tmp_path: Path,
    *,
    release_on_cancel: bool,
    revoke_timeout: float = 1.0,
) -> tuple[
    WorkloadOAuthControlService,
    WorkloadOAuthStore,
    WorkloadEgressPermitManager,
    _ControlledRuntime,
]:
    store = WorkloadOAuthStore(
        tmp_path / "mcp_oauth_workload.sqlite3",
        OAuthKeyRing(current=OAuthKey(kid="workload-k1", material=b"k" * 32)),
    )
    await store.prepare_schema()
    runtime = _ControlledRuntime(release_on_cancel=release_on_cancel)
    permits = WorkloadEgressPermitManager(
        boot_id="control-test",
        runtime_state=store,
        audit_high_watermark=100,
    )
    runtime.permits = permits
    config = ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        auth_mode="oauth_client_credentials",
        auth_profile_id="calendar-user",
        workload_authority_kind="owner_delegation",
        workload_delegation_id="delegation-1",
        workload_source_installation_id="calendar-user-source",
    )
    secondary_config = config.model_copy(
        update={"workload_delegation_id": "delegation-2"}
    )
    source_config = ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        auth_mode="oauth_user",
        auth_profile_id="calendar-user",
    )
    control = WorkloadOAuthControlService(
        profiles=WorkloadProfileRepository(
            tmp_path / "mcp_oauth_workload_profiles.json"
        ),
        delegations=WorkloadDelegationRepository(
            tmp_path / "mcp_oauth_workload_delegations.json"
        ),
        store=store,
        outbound=_Outbound(),
        permits=permits,
        runtime=runtime,
        owners=_Owners(),
        source_grants=_Grants(),
        config_lookup=lambda name: (
            config
            if name == "calendar"
            else secondary_config
            if name == "calendar-secondary"
            else source_config
            if name == "calendar-user-source"
            else None
        ),
        installation_generation_lookup=lambda name: (
            9 if name == "calendar-user-source" else 4
        ),
        close_handles=lambda _authority_key: _async_none(),
        clock=lambda: 100.0,
        revoke_timeout=revoke_timeout,
    )
    return control, store, permits, runtime


async def _start_real_send(
    store: WorkloadOAuthStore,
    permits: WorkloadEgressPermitManager,
    runtime: _ControlledRuntime,
) -> None:
    permit = await permits.issue(
        authority_key="profile:calendar-workload:1",
        task_id="send-task",
        ticket_id="send-ticket",
        egress_kind="mcp_bearer",
        method="tools/call",
        payload_digest=f"sha256:{'a' * 64}",
        target_binding_digest=f"sha256:{'b' * 64}",
        attempt_number=1,
        issued_at=90.0,
        expires_at=110.0,
    )
    await permits.begin_send(
        permit,
        task_id="send-task",
        ticket_id="send-ticket",
        egress_kind="mcp_bearer",
        method="tools/call",
        payload_digest=f"sha256:{'a' * 64}",
        target_binding_digest=f"sha256:{'b' * 64}",
        now=100.0,
    )
    assert permit.authority_epoch == await store.authority_epoch()
    runtime.permit_id = permit.permit_id


@pytest.mark.asyncio
async def test_admin_profile_credential_and_explicit_metadata_flow(tmp_path: Path) -> None:
    control, store = await _control(tmp_path)
    profile = await control.put_profile(
        _profile(),
        actor_id="admin-1",
        request_id="profile-request",
    )
    credential = await control.set_credential(
        profile_id=profile.profile_id,
        client_id=SecretStr("client-id-canary"),
        client_secret=SecretStr("client-secret-canary"),
        actor_id="admin-1",
        request_id="credential-request",
    )
    snapshot = await control.refresh_metadata(
        profile_id=profile.profile_id,
        actor_id="admin-1",
        request_id="metadata-request",
    )

    assert credential.model_dump() == {
        "profile_id": "calendar-workload",
        "credential_id": "calendar-secret",
        "generation": 1,
        "status": "active",
    }
    assert "canary" not in credential.model_dump_json()
    assert snapshot.model_dump() == {
        "profile_id": profile.profile_id,
        "profile_generation": profile.generation,
        "verified_at": 100.0,
        "status": "active",
    }
    assert await store.get_active_profile_credential(
        profile_id=profile.profile_id,
        profile_generation=profile.generation,
        credential_id=profile.credential_id,
    ) is not None


@pytest.mark.asyncio
async def test_owner_delegation_is_server_derived_and_mirrored(tmp_path: Path) -> None:
    control, store = await _control(tmp_path)
    record = await control.put_delegation(
        delegation_id="delegation-1",
        owner_user_id="owner-1",
        execution_agent_id="Alice",
        installation_id="calendar",
        installation_generation=4,
        raw_tool_name="calendar.read",
        required_scopes=frozenset({"calendar.read"}),
        expires_at=200.0,
        request_id="delegation-request",
    )

    assert record.source_slot == OAuthAuthorizationSlotKey(
        human_principal_id="owner-1",
        execution_agent_id="Alice",
        installation_id="calendar-user-source",
        installation_generation=9,
        auth_profile_id="calendar-user",
    )
    assert await store.get_active_delegation(
        delegation_id=record.delegation_id,
        generation=record.generation,
        digest=record.digest,
        now=150.0,
    ) == record


@pytest.mark.asyncio
async def test_credential_revoke_tombstones_before_completed_response(
    tmp_path: Path,
) -> None:
    control, store = await _control(tmp_path)
    profile = await control.put_profile(
        _profile(),
        actor_id="admin-1",
        request_id="profile-request",
    )
    await control.set_credential(
        profile_id=profile.profile_id,
        client_id=SecretStr("client-id-canary"),
        client_secret=SecretStr("client-secret-canary"),
        actor_id="admin-1",
        request_id="credential-request",
    )

    result = await control.revoke_credential(
        profile_id=profile.profile_id,
        actor_id="admin-1",
        request_id="revoke-request",
    )

    assert result.model_dump() == {"status": "completed", "generation": 1}
    assert (
        await store.get_active_profile_credential(
            profile_id=profile.profile_id,
            profile_generation=profile.generation,
            credential_id=profile.credential_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_owner_delegation_delete_preserves_source_grant(
    tmp_path: Path,
) -> None:
    control, store = await _control(tmp_path)
    record = await control.put_delegation(
        delegation_id="delegation-1",
        owner_user_id="owner-1",
        execution_agent_id="Alice",
        installation_id="calendar",
        installation_generation=4,
        raw_tool_name="calendar.read",
        required_scopes=frozenset({"calendar.read"}),
        expires_at=200.0,
        request_id="delegation-request",
    )

    result = await control.delete_delegation(
        delegation_id=record.delegation_id,
        owner_user_id="owner-1",
        execution_agent_id="Alice",
        request_id="delete-request",
    )

    assert result.model_dump() == {"status": "completed", "generation": 1}
    assert await control.list_delegations(
        owner_user_id="owner-1",
        execution_agent_id="Alice",
    ) == []
    assert await store.get_active_delegation(
        delegation_id=record.delegation_id,
        generation=record.generation,
        digest=record.digest,
        now=150.0,
    ) is None


@pytest.mark.asyncio
async def test_source_disconnect_removes_derived_delegation_before_p8a_grant(
    tmp_path: Path,
) -> None:
    control, _store = await _control(tmp_path)
    record = await control.put_delegation(
        delegation_id="delegation-1",
        owner_user_id="owner-1",
        execution_agent_id="Alice",
        installation_id="calendar",
        installation_generation=4,
        raw_tool_name="calendar.read",
        required_scopes=frozenset({"calendar.read"}),
        expires_at=200.0,
        request_id="delegation-request",
    )

    await control.revoke_source_delegations(
        record.source_slot,
        request_id="disconnect-request",
    )

    assert await control.list_delegations(
        owner_user_id="owner-1",
        execution_agent_id="Alice",
    ) == []


# Only catches a hang: the guard test below reads event order, not elapsed time.
DEADLOCK_GUARD_SECONDS = 30.0


class _WatchedLock(asyncio.Lock):
    """An asyncio.Lock that records when someone has to wait for it."""

    def __init__(self) -> None:
        super().__init__()
        self.contended = asyncio.Event()
        self.acquires = 0

    async def acquire(self) -> bool:
        self.acquires += 1
        if self.locked():
            self.contended.set()
        return await super().acquire()


@pytest.mark.asyncio
async def test_source_disconnect_guard_blocks_delegation_creation_until_disconnect_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _MutableGrants(_Grants):
        active = True

        async def get_active_grant(self, slot: Any, *, now: float) -> Any:
            if not self.active:
                return None
            return await super().get_active_grant(slot, now=now)

    grants = _MutableGrants()
    control, _store = await _control(tmp_path, source_grants=grants)
    guard = _WatchedLock()
    monkeypatch.setattr(control, "_source_authority_lock", guard)
    record = await control.put_delegation(
        delegation_id="delegation-1",
        owner_user_id="owner-1",
        execution_agent_id="Alice",
        installation_id="calendar",
        installation_generation=4,
        raw_tool_name="calendar.read",
        required_scopes=frozenset({"calendar.read"}),
        expires_at=200.0,
        request_id="delegation-create",
    )
    assert guard.acquires == 1  # creation goes through this very lock
    disconnect_entered = asyncio.Event()
    finish_disconnect = asyncio.Event()

    async def disconnect() -> None:
        async with control.delegation_disconnect_guard(
            record.source_slot,
            "disconnect-request",
        ):
            disconnect_entered.set()
            await finish_disconnect.wait()

    disconnect_task = asyncio.create_task(disconnect())
    await asyncio.wait_for(disconnect_entered.wait(), timeout=DEADLOCK_GUARD_SECONDS)
    assert guard.contended.is_set() is False
    create_task = asyncio.create_task(
        control.put_delegation(
            delegation_id="delegation-1",
            owner_user_id="owner-1",
            execution_agent_id="Alice",
            installation_id="calendar",
            installation_generation=4,
            raw_tool_name="calendar.read",
            required_scopes=frozenset({"calendar.read"}),
            expires_at=200.0,
            request_id="delegation-race",
        )
    )
    # The creation either queues behind the held guard or, if the guard let it through,
    # finishes while the source grant is still active. Whichever happens first decides.
    contended = asyncio.create_task(guard.contended.wait())
    first, _ = await asyncio.wait(
        {create_task, contended},
        timeout=DEADLOCK_GUARD_SECONDS,
        return_when=asyncio.FIRST_COMPLETED,
    )

    assert first == {contended}
    assert create_task.done() is False
    grants.active = False
    finish_disconnect.set()
    await asyncio.wait_for(disconnect_task, timeout=DEADLOCK_GUARD_SECONDS)
    with pytest.raises(WorkloadControlDenied):
        await asyncio.wait_for(create_task, timeout=DEADLOCK_GUARD_SECONDS)
    assert await control.list_delegations(
        owner_user_id="owner-1",
        execution_agent_id="Alice",
    ) == []


@pytest.mark.asyncio
async def test_source_disconnect_timeout_restart_retries_from_durable_row(
    tmp_path: Path,
) -> None:
    control, store, permits, runtime = await _control_with_real_permits(
        tmp_path,
        release_on_cancel=False,
        revoke_timeout=0.001,
    )
    record = await control.put_delegation(
        delegation_id="delegation-1",
        owner_user_id="owner-1",
        execution_agent_id="Alice",
        installation_id="calendar",
        installation_generation=4,
        raw_tool_name="calendar.read",
        required_scopes=frozenset({"calendar.read"}),
        expires_at=200.0,
        request_id="delegation-create",
    )
    await _start_real_send(store, permits, runtime)

    with pytest.raises(WorkloadControlRecoveryRequired):
        await control.revoke_source_delegations(
            record.source_slot,
            request_id="disconnect-timeout",
        )

    source_slot_digest = (
        f"sha256:{hashlib.sha256(record.source_slot.canonical_bytes()).hexdigest()}"
    )
    pending = await store.list_blocking_revocations(
        source_slot_digest=source_slot_digest
    )
    assert len(pending) == 1
    assert pending[0].source_slot_digest == source_slot_digest
    assert pending[0].mutation_operation_id

    restarted, restarted_store, _permits, _runtime = (
        await _control_with_real_permits(
            tmp_path,
            release_on_cancel=True,
            revoke_timeout=0.1,
        )
    )
    yielded = False
    async with restarted.delegation_disconnect_guard(
        record.source_slot,
        "disconnect-restart",
    ):
        yielded = True

    assert yielded
    assert await restarted_store.list_blocking_revocations(
        source_slot_digest=source_slot_digest
    ) == []
    assert await restarted_store.active_mutation() is None
    assert await restarted_store.runtime_authority_ready()


@pytest.mark.asyncio
async def test_source_disconnect_restart_finishes_every_derived_delegation(
    tmp_path: Path,
) -> None:
    control, store, permits, runtime = await _control_with_real_permits(
        tmp_path,
        release_on_cancel=False,
        revoke_timeout=0.001,
    )
    first = await control.put_delegation(
        delegation_id="delegation-1",
        owner_user_id="owner-1",
        execution_agent_id="Alice",
        installation_id="calendar",
        installation_generation=4,
        raw_tool_name="calendar.read",
        required_scopes=frozenset({"calendar.read"}),
        expires_at=200.0,
        request_id="delegation-create-1",
    )
    second = await control.put_delegation(
        delegation_id="delegation-2",
        owner_user_id="owner-1",
        execution_agent_id="Alice",
        installation_id="calendar-secondary",
        installation_generation=4,
        raw_tool_name="calendar.read",
        required_scopes=frozenset({"calendar.read"}),
        expires_at=200.0,
        request_id="delegation-create-2",
    )
    assert second.source_slot == first.source_slot
    await _start_real_send(store, permits, runtime)

    with pytest.raises(WorkloadControlRecoveryRequired):
        await control.revoke_source_delegations(
            first.source_slot,
            request_id="disconnect-timeout",
        )

    restarted, restarted_store, _permits, _runtime = (
        await _control_with_real_permits(
            tmp_path,
            release_on_cancel=True,
            revoke_timeout=0.1,
        )
    )
    async with restarted.delegation_disconnect_guard(
        first.source_slot,
        "disconnect-restart",
    ):
        pass

    assert await restarted.list_delegations(
        owner_user_id="owner-1",
        execution_agent_id="Alice",
    ) == []
    assert await restarted_store.list_blocking_revocations(
        source_slot_digest=(
            f"sha256:{hashlib.sha256(first.source_slot.canonical_bytes()).hexdigest()}"
        )
    ) == []


@pytest.mark.asyncio
async def test_profile_replacement_waits_for_real_started_send_exit(
    tmp_path: Path,
) -> None:
    control, store, permits, runtime = await _control_with_real_permits(
        tmp_path,
        release_on_cancel=True,
    )
    await control.put_profile(
        _profile(),
        actor_id="admin-1",
        request_id="profile-create",
    )
    await _start_real_send(store, permits, runtime)

    replacement_task = asyncio.create_task(
        control.put_profile(
            _profile().model_copy(update={"generation": 2}),
            actor_id="admin-1",
            request_id="profile-replace",
        )
    )
    await asyncio.wait_for(runtime.cancel_called.wait(), timeout=0.5)

    assert replacement_task.done() is False
    runtime.allow_release.set()
    replacement = await asyncio.wait_for(replacement_task, timeout=0.5)
    assert replacement.generation == 2


@pytest.mark.asyncio
async def test_profile_replacement_timeout_is_visible_and_retry_finalizes(
    tmp_path: Path,
) -> None:
    control, store, permits, runtime = await _control_with_real_permits(
        tmp_path,
        release_on_cancel=False,
        revoke_timeout=0.001,
    )
    await control.put_profile(
        _profile(),
        actor_id="admin-1",
        request_id="profile-create",
    )
    await _start_real_send(store, permits, runtime)

    with pytest.raises(WorkloadControlRecoveryRequired):
        await control.put_profile(
            _profile().model_copy(update={"generation": 2}),
            actor_id="admin-1",
            request_id="profile-replace",
        )

    recovery = await control.recovery_status()
    assert [(item.kind, item.authority_id, item.status) for item in recovery] == [
        ("revocation", "calendar-workload", "revocation_pending")
    ]

    assert runtime.permit_id is not None
    await permits.release(runtime.permit_id)
    assert await control.retry_recovery(
        actor_id="admin-1",
        request_id="profile-retry",
    ) == []


@pytest.mark.asyncio
async def test_cancellation_after_revoke_commit_retains_durable_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control, store = await _control(tmp_path)
    profile = await control.put_profile(
        _profile(),
        actor_id="admin-1",
        request_id="profile-create",
    )
    await control.set_credential(
        profile_id=profile.profile_id,
        client_id=SecretStr("client-id"),
        client_secret=SecretStr("client-secret"),
        actor_id="admin-1",
        request_id="credential-create",
    )

    async def cancel_drain(_task_ids: frozenset[str]) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(control._runtime, "cancel_tasks", cancel_drain)
    with pytest.raises(asyncio.CancelledError):
        await control.revoke_credential(
            profile_id=profile.profile_id,
            actor_id="admin-1",
            request_id="credential-revoke",
        )

    blockers = await store.list_blocking_revocations()
    assert len(blockers) == 1
    assert blockers[0].status == "draining"
    assert blockers[0].mutation_operation_id
    owner = await store.active_mutation()
    assert owner is not None
    assert owner.operation_id == blockers[0].mutation_operation_id
    assert not await store.runtime_authority_ready()


@pytest.mark.asyncio
async def test_retry_recovery_finishes_cancelled_delegation_document_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control, store = await _control(tmp_path)
    record = await control.put_delegation(
        delegation_id="delegation-1",
        owner_user_id="owner-1",
        execution_agent_id="Alice",
        installation_id="calendar",
        installation_generation=4,
        raw_tool_name="calendar.read",
        required_scopes=frozenset({"calendar.read"}),
        expires_at=200.0,
        request_id="delegation-create",
    )

    async def cancel_drain(_task_ids: frozenset[str]) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(control._runtime, "cancel_tasks", cancel_drain)
    with pytest.raises(asyncio.CancelledError):
        await control.delete_delegation(
            delegation_id=record.delegation_id,
            owner_user_id="owner-1",
            execution_agent_id="Alice",
            request_id="delegation-delete",
        )

    async def finish_drain(_task_ids: frozenset[str]) -> None:
        return None

    monkeypatch.setattr(control._runtime, "cancel_tasks", finish_drain)
    assert await control.retry_recovery(
        actor_id="admin-1",
        request_id="delegation-retry",
    ) == []
    assert await control.list_delegations(
        owner_user_id="owner-1",
        execution_agent_id="Alice",
    ) == []
    assert await store.list_blocking_revocations() == []
    assert await store.active_mutation() is None


@pytest.mark.asyncio
async def test_profile_delete_failure_after_epoch_advance_keeps_egress_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control, store, permits, _runtime = await _control_with_real_permits(
        tmp_path,
        release_on_cancel=False,
    )
    await control.put_profile(
        _profile(),
        actor_id="admin-1",
        request_id="profile-create",
    )
    initial_epoch = await store.authority_epoch()

    class _DeleteFailure(Exception):
        pass

    async def fail_delete(*_args: Any, **_kwargs: Any) -> None:
        raise _DeleteFailure()

    monkeypatch.setattr(control._profiles, "delete", fail_delete)
    with pytest.raises(_DeleteFailure):
        await control.delete_profile(
            profile_id="calendar-workload",
            expected_generation=1,
            actor_id="admin-1",
            request_id="profile-delete",
        )

    assert await store.authority_epoch() == initial_epoch + 1
    assert [
        (item.kind, item.authority_id, item.status)
        for item in await control.recovery_status()
    ] == [("transition", "calendar-workload", "recovery_required")]
    with pytest.raises(WorkloadEgressDenied):
        await permits.issue(
            authority_key="profile:other-workload:1",
            task_id="task-after-failure",
            ticket_id="ticket-after-failure",
            egress_kind="mcp_bearer",
            method="tools/call",
            payload_digest=f"sha256:{'c' * 64}",
            target_binding_digest=f"sha256:{'b' * 64}",
            attempt_number=1,
            issued_at=100.0,
            expires_at=110.0,
        )


@pytest.mark.asyncio
async def test_admin_can_explicitly_reconcile_exact_present_profile_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control, _store, permits, _runtime = await _control_with_real_permits(
        tmp_path,
        release_on_cancel=False,
    )
    await control.put_profile(
        _profile(),
        actor_id="admin-1",
        request_id="profile-create",
    )

    async def fail_delete(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("delete-failed")

    monkeypatch.setattr(control._profiles, "delete", fail_delete)
    with pytest.raises(RuntimeError, match="delete-failed"):
        await control.delete_profile(
            profile_id="calendar-workload",
            expected_generation=1,
            actor_id="admin-1",
            request_id="profile-delete",
        )
    monkeypatch.undo()
    [blocker] = await control.recovery_status()

    result = await control.reconcile_transition(
        operation_id=blocker.recovery_id,
        resolution="confirm_present",
        actor_id="admin-2",
        request_id="profile-reconcile",
    )

    assert result.status == "completed"
    assert result.generation == 1
    assert await control.recovery_status() == []
    permit = await permits.issue(
        authority_key="profile:calendar-workload:1",
        task_id="task-after-reconcile",
        ticket_id="ticket-after-reconcile",
        egress_kind="mcp_bearer",
        method="tools/call",
        payload_digest=f"sha256:{'d' * 64}",
        target_binding_digest=f"sha256:{'b' * 64}",
        attempt_number=1,
        issued_at=100.0,
        expires_at=110.0,
    )
    await permits.release(permit.permit_id)
