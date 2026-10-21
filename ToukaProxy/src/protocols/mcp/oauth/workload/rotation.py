# ruff: noqa: TC002
"""Crash-resumable single-Proxy workload key rotation。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Protocol
from uuid import uuid4

from src.protocols.mcp.oauth.keyring import OAuthKey, OAuthKeyRing
from src.protocols.mcp.oauth.models import OAuthKeyRotationPhase, OAuthKeyRotationRecord
from src.protocols.mcp.oauth.rotation import (
    OAuthKeyRotationError,
    OAuthKeyRotationUnavailableError,
)
from src.protocols.mcp.oauth.workload.models import (
    WorkloadAuditEvent,
    WorkloadMutationKind,
)
from src.protocols.mcp.oauth.workload.mutation import (
    WorkloadAuthorityMutationCoordinator,
    WorkloadMutationRecoveryRequired,
)


class WorkloadRotationProvider(Protocol):
    """Dedicated workload-key persistence seam。"""

    source: str

    def create_rotation_candidate(self) -> OAuthKey:
        """Generate but do not persist one next key。"""
        ...

    async def prepare_rotation(
        self,
        *,
        epoch: int,
        candidate: OAuthKey,
    ) -> OAuthKeyRing:
        """Persist a new-current and old-retiring dual ring。"""
        ...

    async def reload(self) -> OAuthKeyRing:
        """Reload the authoritative persisted ring。"""
        ...

    async def retire(self, *, epoch: int) -> OAuthKeyRing:
        """Remove the retiring key after the store has no references。"""
        ...


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class WorkloadKeyRotationService:
    """Rotate one local encrypted store without P8-A worker-convergence phases。"""

    def __init__(
        self,
        *,
        store: Any,
        provider: WorkloadRotationProvider,
        permits: Any,
        runtime: Any,
        close_all_handles: Any,
        clock: Any,
        drain_timeout: float = 5.0,
        mutation_lock: asyncio.Lock | None = None,
        mutation_coordinator: WorkloadAuthorityMutationCoordinator | None = None,
    ) -> None:
        if drain_timeout <= 0:
            raise ValueError()
        self._store = store
        self._provider = provider
        self._permits = permits
        self._runtime = runtime
        self._close_all_handles = close_all_handles
        self._clock = clock
        self._drain_timeout = drain_timeout
        self._mutation_coordinator = mutation_coordinator or (
            WorkloadAuthorityMutationCoordinator(
                permits=permits,
                store=store,
                lock=mutation_lock,
            )
        )

    @staticmethod
    def _validate_dual(record: OAuthKeyRotationRecord, ring: OAuthKeyRing) -> None:
        if (
            ring.current.kid != record.current_kid
            or record.retiring_kid is None
            or {key.kid for key in ring.retiring} != {record.retiring_kid}
        ):
            raise OAuthKeyRotationError()

    @staticmethod
    def _event(
        *,
        action: str,
        epoch: int,
        outcome: str,
        now: float,
    ) -> WorkloadAuditEvent:
        return WorkloadAuditEvent(
            event_id=str(uuid4()),
            action=action,
            actor_id="system:key-rotation",
            authority_id="workload-keyring",
            authority_generation=epoch,
            request_id=f"workload-key-rotation:{epoch}",
            outcome=outcome,
            created_at=now,
            details_digest=_digest({"action": action, "epoch": epoch}),
        )

    async def status(self) -> OAuthKeyRotationRecord:
        """Return only non-secret durable key IDs and phase。"""
        return await self._store.workload_key_rotation()

    async def _drain(self, task_ids: frozenset[str]) -> None:
        async with asyncio.timeout(self._drain_timeout):
            await self._runtime.cancel_tasks(task_ids)
            await self._close_all_handles()
            if not await self._permits.wait_all_drained(timeout=self._drain_timeout):
                raise OAuthKeyRotationError()

    async def start(self, *, now: float) -> OAuthKeyRotationRecord:
        """Serialize key rotation with every other workload authority mutation。"""
        async with self._mutation_coordinator.serialized():
            return await self._start_unserialized(now=now)

    async def _start_unserialized(self, *, now: float) -> OAuthKeyRotationRecord:
        """Fence, publish a dual ring, re-encrypt all rows, and retire the old key。"""
        if self._provider.source != "managed":
            raise OAuthKeyRotationUnavailableError()
        current = await self.status()
        if current.phase != OAuthKeyRotationPhase.STABLE:
            raise OAuthKeyRotationError()
        if not await self._store.runtime_authority_ready():
            raise OAuthKeyRotationError()
        candidate = self._provider.create_rotation_candidate()
        try:
            fence_id = await self._mutation_coordinator.begin(
                operation_id=str(uuid4()),
                mutation_kind=WorkloadMutationKind.ROTATION.value,
            )
        except WorkloadMutationRecoveryRequired as exc:
            raise OAuthKeyRotationError() from exc
        initial_authority_epoch = await self._store.authority_epoch()
        try:
            prepared, authority_epoch = await self._store.begin_workload_key_rotation(
                new_kid=candidate.kid,
                now=now,
                event=self._event(
                    action="workload-key.rotate.intent",
                    epoch=current.epoch + 1,
                    outcome="pending",
                    now=now,
                ),
            )
        except BaseException:
            current_authority_epoch = await self._store.authority_epoch()
            if current_authority_epoch == initial_authority_epoch:
                await self._mutation_coordinator.abort(fence_id=fence_id)
            elif current_authority_epoch > initial_authority_epoch:
                task_ids = await self._mutation_coordinator.commit(
                    fence_id=fence_id,
                    new_authority_epoch=current_authority_epoch,
                )
                self._runtime.invalidate_all_authorities()
                await self._drain(task_ids)
            raise
        task_ids = await self._mutation_coordinator.commit(
            fence_id=fence_id,
            new_authority_epoch=authority_epoch
        )
        self._runtime.invalidate_all_authorities()
        await self._drain(task_ids)
        ring = await self._provider.prepare_rotation(
            epoch=prepared.epoch,
            candidate=candidate,
        )
        self._validate_dual(prepared, ring)
        self._store.replace_keyring(ring)
        await self._store.set_workload_key_rotation_phase(
            epoch=prepared.epoch,
            expected=OAuthKeyRotationPhase.PREPARE,
            target=OAuthKeyRotationPhase.DUAL_READ_NEW_WRITE,
        )
        return await self._maintain_locked(now=now)

    async def maintain(self, *, now: float | None = None) -> OAuthKeyRotationRecord:
        """Recover any crash cut without auto-authorizing inconsistent state。"""
        async with self._mutation_coordinator.serialized():
            return await self._maintain_locked(now=now)

    async def _resume_rotation_fence_locked(
        self,
        *,
        record: OAuthKeyRotationRecord,
    ) -> None:
        """Rebuild the process-local fence for the exact durable rotation owner。"""
        if self._mutation_coordinator.pending_fence_id is not None:
            return
        owner = await self._store.active_mutation()
        if owner is None:
            if record.phase != OAuthKeyRotationPhase.STABLE:
                raise OAuthKeyRotationError()
            return
        if owner.mutation_kind != WorkloadMutationKind.ROTATION:
            if record.phase == OAuthKeyRotationPhase.STABLE:
                return
            raise OAuthKeyRotationError()
        try:
            fence_id = await self._mutation_coordinator.begin(
                operation_id=owner.operation_id,
                mutation_kind=owner.mutation_kind.value,
            )
            task_ids = await self._mutation_coordinator.commit(
                fence_id=fence_id,
                new_authority_epoch=await self._store.authority_epoch(),
            )
        except WorkloadMutationRecoveryRequired as exc:
            raise OAuthKeyRotationError() from exc
        self._runtime.invalidate_all_authorities()
        await self._drain(task_ids)

    async def _maintain_locked(
        self,
        *,
        now: float | None = None,
    ) -> OAuthKeyRotationRecord:
        """Maintain rotation while the shared mutation lock is already held。"""
        current_now = self._clock() if now is None else now
        record = await self.status()
        await self._resume_rotation_fence_locked(record=record)
        if self._mutation_coordinator.pending_fence_id is not None:
            await self._drain(frozenset())
        ring = await self._provider.reload()
        if record.phase == OAuthKeyRotationPhase.STABLE:
            if (
                ring.current.kid != record.current_kid
                or ring.retiring
                or record.retiring_kid is not None
            ):
                raise OAuthKeyRotationError()
            self._store.replace_keyring(ring)
            if self._mutation_coordinator.pending_fence_id is not None:
                if not await self._store.runtime_authority_ready(
                    mutation_operation_id=(
                        self._mutation_coordinator.pending_operation_id
                    )
                ):
                    raise OAuthKeyRotationError()
                await self._mutation_coordinator.complete(
                    fence_id=self._mutation_coordinator.pending_fence_id
                )
            return record
        if record.phase == OAuthKeyRotationPhase.PREPARE:
            if (
                record.retiring_kid is not None
                and ring.current.kid == record.retiring_kid
                and not ring.retiring
            ):
                await self._store.rollback_workload_key_rotation_prepare(
                    epoch=record.epoch
                )
                self._store.replace_keyring(ring)
                rolled_back = await self.status()
                if self._mutation_coordinator.pending_fence_id is not None:
                    if not await self._store.runtime_authority_ready(
                        mutation_operation_id=(
                            self._mutation_coordinator.pending_operation_id
                        )
                    ):
                        raise OAuthKeyRotationError()
                    await self._mutation_coordinator.complete(
                        fence_id=self._mutation_coordinator.pending_fence_id
                    )
                return rolled_back
            self._validate_dual(record, ring)
            self._store.replace_keyring(ring)
            await self._store.set_workload_key_rotation_phase(
                epoch=record.epoch,
                expected=OAuthKeyRotationPhase.PREPARE,
                target=OAuthKeyRotationPhase.DUAL_READ_NEW_WRITE,
            )
            record = await self.status()
        if record.phase != OAuthKeyRotationPhase.DUAL_READ_NEW_WRITE:
            raise OAuthKeyRotationError()
        retiring_kid = record.retiring_kid
        if retiring_kid is None:
            raise OAuthKeyRotationError()
        if ring.retiring:
            self._validate_dual(record, ring)
            self._store.replace_keyring(ring)
            await self._store.reencrypt_workload_credentials()
            if await self._store.count_workload_key_references(retiring_kid) != 0:
                raise OAuthKeyRotationError()
            ring = await self._provider.retire(epoch=record.epoch)
        if ring.current.kid != record.current_kid or ring.retiring:
            raise OAuthKeyRotationError()
        self._store.replace_keyring(ring)
        if await self._store.count_workload_key_references(retiring_kid) != 0:
            raise OAuthKeyRotationError()
        completed = await self._store.complete_workload_key_rotation(
            epoch=record.epoch,
            now=current_now,
            event=self._event(
                action="workload-key.rotate.commit",
                epoch=record.epoch,
                outcome="success",
                now=current_now,
            ),
        )
        if self._mutation_coordinator.pending_fence_id is not None:
            if not await self._store.runtime_authority_ready(
                mutation_operation_id=(
                    self._mutation_coordinator.pending_operation_id
                )
            ):
                raise OAuthKeyRotationError()
            await self._mutation_coordinator.complete(
                fence_id=self._mutation_coordinator.pending_fence_id
            )
        return completed
