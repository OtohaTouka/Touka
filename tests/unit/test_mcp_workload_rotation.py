"""P8-B single-Proxy workload key rotation and crash recovery。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from src.protocols.mcp.oauth.keyring import OAuthKey, OAuthKeyRing
from src.protocols.mcp.oauth.workload.models import (
    WorkloadAuditEvent,
    WorkloadCredentialPayload,
    WorkloadMutationKind,
)
from src.protocols.mcp.oauth.workload.rotation import WorkloadKeyRotationService
from src.protocols.mcp.oauth.workload.store import WorkloadOAuthStore

if TYPE_CHECKING:
    from pathlib import Path


class _Provider:
    source = "managed"

    def __init__(self) -> None:
        self.old = OAuthKey(kid="workload-k1", material=b"k" * 32)
        self.new = OAuthKey(kid="workload-k2", material=b"n" * 32)
        self.ring = OAuthKeyRing(current=self.old)

    def create_rotation_candidate(self) -> OAuthKey:
        return self.new

    async def prepare_rotation(self, *, epoch: int, candidate: OAuthKey) -> OAuthKeyRing:
        assert epoch == 2
        assert candidate == self.new
        self.ring = OAuthKeyRing(current=self.new, retiring=(self.old,))
        return self.ring

    async def reload(self) -> OAuthKeyRing:
        return self.ring

    async def retire(self, *, epoch: int) -> OAuthKeyRing:
        assert epoch == 2
        self.ring = OAuthKeyRing(current=self.new)
        return self.ring


class _BarrierProvider(_Provider):
    def __init__(self) -> None:
        super().__init__()
        self.first_reload_entered = asyncio.Event()
        self.release_first_reload = asyncio.Event()
        self.reload_calls = 0
        self.active_reloads = 0
        self.max_active_reloads = 0

    async def reload(self) -> OAuthKeyRing:
        self.reload_calls += 1
        self.active_reloads += 1
        self.max_active_reloads = max(
            self.max_active_reloads,
            self.active_reloads,
        )
        try:
            if self.reload_calls == 1:
                self.first_reload_entered.set()
                await self.release_first_reload.wait()
            return self.ring
        finally:
            self.active_reloads -= 1


class _Permits:
    async def begin_global_revocation(self) -> str:
        return "rotation-fence"

    async def abort_global_revocation(self, **_kwargs: Any) -> None:
        return None

    async def commit_global_revocation(self, **_kwargs: Any) -> frozenset[str]:
        return frozenset()

    async def complete_global_revocation(self, **_kwargs: Any) -> None:
        return None

    async def wait_all_drained(self, *, timeout: float) -> bool:
        return timeout > 0


class _Runtime:
    async def cancel_tasks(self, task_ids: frozenset[str]) -> None:
        assert task_ids == frozenset()

    def invalidate_all_authorities(self) -> None:
        return None


def _event() -> WorkloadAuditEvent:
    return WorkloadAuditEvent(
        event_id="credential-event",
        action="credential.activate",
        actor_id="admin-1",
        authority_id="credential-a",
        authority_generation=1,
        request_id="request-a",
        outcome="success",
        created_at=90.0,
        details_digest=f"sha256:{'a' * 64}",
    )


async def _store(tmp_path: Path, provider: _Provider) -> WorkloadOAuthStore:
    store = WorkloadOAuthStore(
        tmp_path / "mcp_oauth_workload.sqlite3",
        provider.ring,
    )
    await store.prepare_schema()
    await store.activate_credential(
        WorkloadCredentialPayload(
            profile_id="profile-a",
            credential_id="credential-a",
            generation=1,
            client_id=SecretStr("client-id"),
            client_secret=SecretStr("client-secret"),
        ),
        profile_generation=1,
        now=90.0,
        event=_event(),
    )
    return store


def _service(store: WorkloadOAuthStore, provider: _Provider) -> WorkloadKeyRotationService:
    async def close_all() -> None:
        return None

    return WorkloadKeyRotationService(
        store=store,
        provider=provider,
        permits=_Permits(),
        runtime=_Runtime(),
        close_all_handles=close_all,
        clock=lambda: 100.0,
    )


@pytest.mark.asyncio
async def test_rotation_reencrypts_every_secret_before_retiring_old_key(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    store = await _store(tmp_path, provider)

    result = await _service(store, provider).start(now=100.0)

    assert result.phase == "stable"
    assert result.current_kid == "workload-k2"
    assert result.retiring_kid is None
    assert await store.count_workload_key_references("workload-k1") == 0
    credential = await store.get_active_profile_credential(
        profile_id="profile-a",
        profile_generation=1,
        credential_id="credential-a",
    )
    assert credential is not None
    assert credential.client_secret.get_secret_value() == "client-secret"


@pytest.mark.asyncio
async def test_prepare_crash_with_unpublished_key_rolls_back_denying_ambiguity(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    store = await _store(tmp_path, provider)
    await store.claim_mutation(
        operation_id="rotation-operation",
        mutation_kind=WorkloadMutationKind.ROTATION.value,
    )
    prepared, _epoch = await store.begin_workload_key_rotation(
        new_kid=provider.new.kid,
        now=100.0,
        event=WorkloadAuditEvent(
            event_id="rotation-intent",
            action="workload-key.rotate.intent",
            actor_id="system:key-rotation",
            authority_id="workload-keyring",
            authority_generation=2,
            request_id="rotation-2",
            outcome="pending",
            created_at=100.0,
            details_digest=f"sha256:{'b' * 64}",
        ),
    )

    assert prepared.phase == "prepare"
    recovered = await _service(store, provider).maintain(now=101.0)

    assert recovered.phase == "stable"
    assert recovered.current_kid == "workload-k1"
    assert await store.runtime_authority_ready()
    assert await store.active_mutation() is None


@pytest.mark.asyncio
async def test_public_maintain_calls_are_serialized(tmp_path: Path) -> None:
    provider = _BarrierProvider()
    store = await _store(tmp_path, provider)
    service = _service(store, provider)

    first = asyncio.create_task(service.maintain(now=101.0))
    await provider.first_reload_entered.wait()
    second = asyncio.create_task(service.maintain(now=102.0))
    completed, _pending = await asyncio.wait({second}, timeout=0.1)
    try:
        assert completed == set()
    finally:
        provider.release_first_reload.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.phase == "stable"
    assert second_result.phase == "stable"
    assert provider.max_active_reloads == 1


@pytest.mark.asyncio
async def test_stable_rotation_does_not_hijack_foreign_recovery_owner(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    store = await _store(tmp_path, provider)
    await store.claim_mutation(
        operation_id="profile-recovery-operation",
        mutation_kind=WorkloadMutationKind.PROFILE.value,
    )

    result = await _service(store, provider).maintain(now=101.0)

    assert result.phase == "stable"
    owner = await store.active_mutation()
    assert owner is not None
    assert owner.operation_id == "profile-recovery-operation"
    assert not await store.runtime_authority_ready()
