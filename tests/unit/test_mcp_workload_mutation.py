"""P8-B round-2：single process mutation coordinator contract。"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

_FOREIGN_MUTATION_ERROR = "foreign mutation"


class _Permits:
    def __init__(self) -> None:
        self.next_id = 0
        self.committed: list[tuple[str, int]] = []
        self.completed: list[str] = []
        self.aborted: list[str] = []

    async def begin_global_revocation(self) -> str:
        self.next_id += 1
        return f"fence-{self.next_id}"

    async def abort_global_revocation(self, *, fence_id: str) -> None:
        self.aborted.append(fence_id)

    async def commit_global_revocation(
        self,
        *,
        fence_id: str,
        new_authority_epoch: int,
    ) -> frozenset[str]:
        self.committed.append((fence_id, new_authority_epoch))
        return frozenset({"task-1"})

    async def complete_global_revocation(self, *, fence_id: str) -> None:
        self.completed.append(fence_id)


class _MutationStore:
    def __init__(self) -> None:
        self.active: tuple[str, str] | None = None
        self.completed: list[tuple[str, str]] = []

    async def claim_mutation(
        self,
        *,
        operation_id: str,
        mutation_kind: str,
    ) -> None:
        if self.active not in {None, (operation_id, mutation_kind)}:
            raise RuntimeError(_FOREIGN_MUTATION_ERROR)
        self.active = (operation_id, mutation_kind)

    async def complete_mutation(
        self,
        *,
        operation_id: str,
        mutation_kind: str,
    ) -> None:
        if self.active != (operation_id, mutation_kind):
            raise RuntimeError(_FOREIGN_MUTATION_ERROR)
        self.completed.append((operation_id, mutation_kind))
        self.active = None


def _mutation() -> Any:
    return importlib.import_module("src.protocols.mcp.oauth.workload.mutation")


@pytest.mark.asyncio
async def test_one_coordinator_owns_the_exact_pending_fence_until_completion() -> None:
    permits = _Permits()
    store = _MutationStore()
    coordinator = _mutation().WorkloadAuthorityMutationCoordinator(
        permits=permits,
        store=store,
    )

    fence_id = await coordinator.begin(
        operation_id="operation-1",
        mutation_kind="profile",
    )
    tasks = await coordinator.commit(fence_id=fence_id, new_authority_epoch=8)

    assert tasks == frozenset({"task-1"})
    assert coordinator.pending_fence_id == "fence-1"
    with pytest.raises(_mutation().WorkloadMutationRecoveryRequired):
        await coordinator.begin(
            operation_id="operation-2",
            mutation_kind="credential",
        )

    with pytest.raises(TypeError):
        await coordinator.complete()
    with pytest.raises(_mutation().WorkloadMutationFenceMismatch):
        await coordinator.complete(fence_id="foreign-fence")

    await coordinator.complete(fence_id=fence_id)
    assert coordinator.pending_fence_id is None
    assert permits.completed == ["fence-1"]
    assert store.completed == [("operation-1", "profile")]
    with pytest.raises(_mutation().WorkloadMutationFenceMismatch):
        await coordinator.complete(fence_id=fence_id)


@pytest.mark.asyncio
async def test_uncommitted_fence_can_only_be_aborted_by_exact_owner() -> None:
    permits = _Permits()
    store = _MutationStore()
    coordinator = _mutation().WorkloadAuthorityMutationCoordinator(
        permits=permits,
        store=store,
    )
    fence_id = await coordinator.begin(
        operation_id="operation-1",
        mutation_kind="delegation",
    )

    with pytest.raises(_mutation().WorkloadMutationFenceMismatch):
        await coordinator.abort(fence_id="wrong-fence")

    await coordinator.abort(fence_id=fence_id)
    assert permits.aborted == ["fence-1"]
    assert store.completed == [("operation-1", "delegation")]
