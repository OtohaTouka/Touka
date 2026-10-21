"""Single-owner process fence for every P8-B authority mutation。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class WorkloadMutationRecoveryRequired(RuntimeError):
    """A committed process fence is still pending exact completion。"""


class WorkloadMutationFenceMismatch(RuntimeError):
    """A caller tried to mutate a fence owned by a different operation。"""


class WorkloadMutationOwnerStore(Protocol):
    """Durable exact-owner seam shared by control, recovery, and rotation。"""

    async def claim_mutation(
        self,
        *,
        operation_id: str,
        mutation_kind: str,
    ) -> None:
        """Claim or resume one exact mutation owner。"""
        ...

    async def complete_mutation(
        self,
        *,
        operation_id: str,
        mutation_kind: str,
    ) -> None:
        """Clear only the same exact mutation owner。"""
        ...


class WorkloadAuthorityMutationCoordinator:
    """Own the sole mutation lock and exact global-fence token in one Proxy。"""

    def __init__(
        self,
        *,
        permits: Any,
        store: WorkloadMutationOwnerStore,
        lock: asyncio.Lock | None = None,
    ) -> None:
        self._permits = permits
        self._store = store
        self._lock = lock or asyncio.Lock()
        self._active_fence_id: str | None = None
        self._pending_fence_id: str | None = None
        self._active_operation: tuple[str, str] | None = None
        self._pending_operation: tuple[str, str] | None = None
        self._active_store_completed = False
        self._pending_store_completed = False

    @property
    def pending_fence_id(self) -> str | None:
        """Return only the opaque token needed for process-local completion。"""
        return self._pending_fence_id

    @property
    def pending_operation_id(self) -> str | None:
        """Return the durable owner bound to the committed process fence。"""
        return self._pending_operation[0] if self._pending_operation is not None else None

    @asynccontextmanager
    async def serialized(self) -> AsyncIterator[None]:
        """Serialize control, recovery, and key-rotation mutations。"""
        async with self._lock:
            yield

    async def begin(self, *, operation_id: str, mutation_kind: str) -> str:
        """Close admission and record the sole uncommitted fence owner。"""
        if (
            not operation_id
            or not mutation_kind
            or self._active_fence_id is not None
            or self._pending_fence_id is not None
        ):
            raise WorkloadMutationRecoveryRequired()
        fence_id = await self._permits.begin_global_revocation()
        try:
            await self._store.claim_mutation(
                operation_id=operation_id,
                mutation_kind=mutation_kind,
            )
        except BaseException:
            await self._permits.abort_global_revocation(fence_id=fence_id)
            raise
        self._active_fence_id = fence_id
        self._active_operation = (operation_id, mutation_kind)
        self._active_store_completed = False
        return fence_id

    def _require_active(self, fence_id: str) -> None:
        if self._active_fence_id != fence_id:
            raise WorkloadMutationFenceMismatch()

    async def abort(self, *, fence_id: str) -> None:
        """Reopen only the exact uncommitted fence。"""
        self._require_active(fence_id)
        operation = self._active_operation
        if operation is None:
            raise WorkloadMutationFenceMismatch()
        if not self._active_store_completed:
            await self._store.complete_mutation(
                operation_id=operation[0],
                mutation_kind=operation[1],
            )
            self._active_store_completed = True
        await self._permits.abort_global_revocation(fence_id=fence_id)
        self._active_fence_id = None
        self._active_operation = None
        self._active_store_completed = False

    async def commit(
        self,
        *,
        fence_id: str,
        new_authority_epoch: int,
    ) -> frozenset[str]:
        """Advance the process fence while keeping admission closed。"""
        self._require_active(fence_id)
        task_ids = await self._permits.commit_global_revocation(
            fence_id=fence_id,
            new_authority_epoch=new_authority_epoch,
        )
        self._active_fence_id = None
        self._pending_fence_id = fence_id
        self._pending_operation = self._active_operation
        self._pending_store_completed = self._active_store_completed
        self._active_operation = None
        self._active_store_completed = False
        return task_ids

    async def complete(self, *, fence_id: str) -> None:
        """Reopen admission only for the exact committed fence owner。"""
        expected = self._pending_fence_id
        operation = self._pending_operation
        if expected is None or operation is None or fence_id != expected:
            raise WorkloadMutationFenceMismatch()
        if not self._pending_store_completed:
            await self._store.complete_mutation(
                operation_id=operation[0],
                mutation_kind=operation[1],
            )
            self._pending_store_completed = True
        await self._permits.complete_global_revocation(fence_id=expected)
        self._pending_fence_id = None
        self._pending_operation = None
        self._pending_store_completed = False
