"""P8-B Slice 3：ephemeral egress permit 與撤銷 drain。"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from typing import Any

import pytest


def _permit() -> Any:
    module_name = "src.protocols.mcp.oauth.workload.permit"
    assert importlib.util.find_spec(module_name) is not None, "permit seam is missing"
    return importlib.import_module(module_name)


class _RuntimeState:
    def __init__(
        self,
        *,
        epoch: int = 7,
        pending_audit: int = 0,
        authority_ready: bool = True,
    ) -> None:
        self.epoch = epoch
        self.pending_audit = pending_audit
        self.authority_ready = authority_ready

    async def authority_epoch(self) -> int:
        return self.epoch

    async def pending_audit_count(self) -> int:
        return self.pending_audit

    async def runtime_authority_ready(self) -> bool:
        return self.authority_ready


class _BarrierRuntimeState(_RuntimeState):
    def __init__(self) -> None:
        super().__init__()
        self.epoch_reads = 0
        self.begin_snapshot_entered = asyncio.Event()
        self.resume_begin_snapshot = asyncio.Event()

    async def authority_epoch(self) -> int:
        self.epoch_reads += 1
        if self.epoch_reads == 2:
            self.begin_snapshot_entered.set()
            await self.resume_begin_snapshot.wait()
        return self.epoch


def _manager(state: _RuntimeState, *, boot_id: str = "boot-a") -> Any:
    return _permit().WorkloadEgressPermitManager(
        boot_id=boot_id,
        runtime_state=state,
        audit_high_watermark=4,
    )


async def _issue(manager: Any, **updates: Any) -> Any:
    values: dict[str, Any] = {
        "authority_key": "profile:calendar-workload:3",
        "task_id": "task-1",
        "ticket_id": "ticket-1",
        "egress_kind": "mcp_bearer",
        "method": "tools/call",
        "payload_digest": f"sha256:{'a' * 64}",
        "target_binding_digest": f"sha256:{'c' * 64}",
        "attempt_number": 1,
        "issued_at": 100.0,
        "expires_at": 110.0,
    }
    values.update(updates)
    return await manager.issue(**values)


@pytest.mark.asyncio
async def test_permit_is_single_use_and_exactly_bound_to_wire_attempt() -> None:
    state = _RuntimeState()
    manager = _manager(state)
    permit = await _issue(manager)

    await manager.begin_send(
        permit,
        task_id="task-1",
        ticket_id="ticket-1",
        egress_kind="mcp_bearer",
        method="tools/call",
        payload_digest=f"sha256:{'a' * 64}",
        target_binding_digest=f"sha256:{'c' * 64}",
        now=101.0,
    )
    await manager.acknowledge(permit.permit_id)

    with pytest.raises(_permit().WorkloadEgressDenied):
        await manager.begin_send(
            permit,
            task_id="task-1",
            ticket_id="ticket-1",
            egress_kind="mcp_bearer",
            method="tools/call",
            payload_digest=f"sha256:{'a' * 64}",
            target_binding_digest=f"sha256:{'c' * 64}",
            now=102.0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "task-other"),
        ("ticket_id", "ticket-other"),
        ("egress_kind", "transport_bootstrap"),
        ("method", "resources/read"),
        ("payload_digest", f"sha256:{'b' * 64}"),
        ("target_binding_digest", f"sha256:{'d' * 64}"),
    ],
)
async def test_any_wire_binding_mismatch_fails_closed(field: str, value: str) -> None:
    state = _RuntimeState()
    manager = _manager(state)
    permit = await _issue(manager)
    values = {
        "task_id": "task-1",
        "ticket_id": "ticket-1",
        "egress_kind": "mcp_bearer",
        "method": "tools/call",
        "payload_digest": f"sha256:{'a' * 64}",
        "target_binding_digest": f"sha256:{'c' * 64}",
        "now": 101.0,
    }
    values[field] = value

    with pytest.raises(_permit().WorkloadEgressDenied):
        await manager.begin_send(permit, **values)


@pytest.mark.asyncio
async def test_stale_epoch_restart_and_audit_watermark_fail_closed() -> None:
    state = _RuntimeState()
    manager = _manager(state)
    permit = await _issue(manager)
    state.epoch += 1

    with pytest.raises(_permit().WorkloadEgressDenied):
        await manager.begin_send(
            permit,
            task_id="task-1",
            ticket_id="ticket-1",
            egress_kind="mcp_bearer",
            method="tools/call",
            payload_digest=f"sha256:{'a' * 64}",
            target_binding_digest=f"sha256:{'c' * 64}",
            now=101.0,
        )
    with pytest.raises(_permit().WorkloadEgressDenied):
        await _manager(_RuntimeState(), boot_id="boot-b").begin_send(
            permit,
            task_id="task-1",
            ticket_id="ticket-1",
            egress_kind="mcp_bearer",
            method="tools/call",
            payload_digest=f"sha256:{'a' * 64}",
            target_binding_digest=f"sha256:{'c' * 64}",
            now=101.0,
        )

    state.pending_audit = 4
    with pytest.raises(_permit().WorkloadEgressDenied):
        await manager.issue(
            authority_key="profile:other:1",
            task_id="task-2",
            ticket_id="ticket-2",
            egress_kind="mcp_bearer",
            method="tools/call",
            payload_digest=f"sha256:{'c' * 64}",
            target_binding_digest=f"sha256:{'d' * 64}",
            attempt_number=1,
            issued_at=100.0,
            expires_at=110.0,
        )


@pytest.mark.asyncio
async def test_revocation_blocks_new_permits_and_waits_for_send_ack() -> None:
    state = _RuntimeState()
    manager = _manager(state)
    permit = await _issue(manager)
    await manager.begin_send(
        permit,
        task_id="task-1",
        ticket_id="ticket-1",
        egress_kind="mcp_bearer",
        method="tools/call",
        payload_digest=f"sha256:{'a' * 64}",
        target_binding_digest=f"sha256:{'c' * 64}",
        now=101.0,
    )

    await manager.begin_revocation(permit.authority_key)
    active_tasks = await manager.commit_revocation(
        permit.authority_key,
        new_authority_epoch=8,
    )

    assert active_tasks == frozenset({"task-1"})
    assert not await manager.wait_drained(permit.authority_key, timeout=0.01)
    with pytest.raises(_permit().WorkloadEgressDenied):
        await _issue(manager, task_id="task-2", ticket_id="ticket-2")

    await manager.acknowledge(permit.permit_id)
    assert await manager.wait_drained(permit.authority_key, timeout=0.01)


@pytest.mark.asyncio
async def test_release_cancels_an_issued_permit_before_revocation_drain() -> None:
    state = _RuntimeState()
    manager = _manager(state)
    permit = await _issue(manager)

    await manager.release(permit.permit_id)
    await manager.begin_revocation(permit.authority_key)
    active_tasks = await manager.commit_revocation(
        permit.authority_key,
        new_authority_epoch=8,
    )

    assert active_tasks == frozenset()
    assert await manager.wait_drained(permit.authority_key, timeout=0.01)


@pytest.mark.asyncio
async def test_global_revocation_fences_all_authorities_until_epoch_commit() -> None:
    state = _RuntimeState()
    manager = _manager(state)
    sending = await _issue(manager)
    unused = await _issue(
        manager,
        authority_key="profile:mail-workload:1",
        task_id="task-2",
        ticket_id="ticket-2",
    )
    await manager.begin_send(
        sending,
        task_id="task-1",
        ticket_id="ticket-1",
        egress_kind="mcp_bearer",
        method="tools/call",
        payload_digest=f"sha256:{'a' * 64}",
        target_binding_digest=f"sha256:{'c' * 64}",
        now=101.0,
    )

    fence_id = await manager.begin_global_revocation()
    with pytest.raises(_permit().WorkloadEgressDenied):
        await _issue(
            manager,
            authority_key="profile:other-workload:1",
            task_id="task-3",
            ticket_id="ticket-3",
        )
    state.epoch = 8
    active_tasks = await manager.commit_global_revocation(
        fence_id=fence_id,
        new_authority_epoch=8,
    )

    assert active_tasks == frozenset({"task-1"})
    with pytest.raises(_permit().WorkloadEgressDenied):
        await manager.begin_send(
            unused,
            task_id="task-2",
            ticket_id="ticket-2",
            egress_kind="mcp_bearer",
            method="tools/call",
            payload_digest=f"sha256:{'a' * 64}",
            target_binding_digest=f"sha256:{'c' * 64}",
            now=101.0,
        )
    assert not await manager.wait_all_drained(timeout=0.01)
    await manager.release(sending.permit_id)
    await manager.release(unused.permit_id)
    assert await manager.wait_all_drained(timeout=0.01)
    await manager.complete_global_revocation(fence_id=fence_id)
    assert (
        await _issue(
            manager,
            authority_key="profile:other-workload:1",
            task_id="task-3",
            ticket_id="ticket-3",
        )
    ).authority_epoch == 8


@pytest.mark.asyncio
async def test_begin_send_snapshot_is_linearized_before_global_mutation() -> None:
    state = _BarrierRuntimeState()
    manager = _manager(state)
    permit = await _issue(manager)
    begin_task = asyncio.create_task(
        manager.begin_send(
            permit,
            task_id="task-1",
            ticket_id="ticket-1",
            egress_kind="mcp_bearer",
            method="tools/call",
            payload_digest=f"sha256:{'a' * 64}",
            target_binding_digest=f"sha256:{'c' * 64}",
            now=101.0,
        )
    )
    await asyncio.wait_for(state.begin_snapshot_entered.wait(), timeout=0.5)
    revoke_task = asyncio.create_task(manager.begin_global_revocation())
    await asyncio.sleep(0)

    assert revoke_task.done() is False
    state.resume_begin_snapshot.set()
    await asyncio.wait_for(begin_task, timeout=0.5)
    await asyncio.wait_for(revoke_task, timeout=0.5)
    state.epoch = 8
    fence_id = await revoke_task
    assert await manager.commit_global_revocation(
        fence_id=fence_id,
        new_authority_epoch=8,
    ) == frozenset({"task-1"})
    await manager.release(permit.permit_id)
    await manager.complete_global_revocation(fence_id=fence_id)


@pytest.mark.asyncio
async def test_global_revocation_abort_reopens_only_after_durable_failure() -> None:
    state = _RuntimeState()
    manager = _manager(state)

    fence_id = await manager.begin_global_revocation()
    await manager.abort_global_revocation(fence_id=fence_id)

    permit = await _issue(manager)
    assert permit.authority_epoch == 7


@pytest.mark.asyncio
async def test_durable_authority_blocker_denies_issue_and_begin_send() -> None:
    state = _RuntimeState(authority_ready=False)
    manager = _manager(state)

    with pytest.raises(_permit().WorkloadEgressDenied):
        await _issue(manager)

    state.authority_ready = True
    permit = await _issue(manager)
    state.authority_ready = False
    with pytest.raises(_permit().WorkloadEgressDenied):
        await manager.begin_send(
            permit,
            task_id="task-1",
            ticket_id="ticket-1",
            egress_kind="mcp_bearer",
            method="tools/call",
            payload_digest=f"sha256:{'a' * 64}",
            target_binding_digest=f"sha256:{'c' * 64}",
            now=101.0,
        )


@pytest.mark.asyncio
async def test_global_fence_stays_closed_until_exact_completion_token() -> None:
    state = _RuntimeState()
    manager = _manager(state)

    fence_id = await manager.begin_global_revocation()
    assert isinstance(fence_id, str) and fence_id
    state.epoch = 8
    assert await manager.commit_global_revocation(
        fence_id=fence_id,
        new_authority_epoch=8,
    ) == frozenset()

    with pytest.raises(_permit().WorkloadEgressDenied):
        await _issue(manager)
    with pytest.raises(_permit().WorkloadEgressDenied):
        await manager.complete_global_revocation(fence_id="stale-fence")

    await manager.complete_global_revocation(fence_id=fence_id)
    assert (await _issue(manager)).authority_epoch == 8
