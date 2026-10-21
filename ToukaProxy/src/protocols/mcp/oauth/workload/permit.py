"""Boot-local, single-use P8-B OAuth egress permits and revocation drain。"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOOLS_CALL_METHOD = "tools/call"
_EGRESS_KINDS = {
    "mcp_bearer",
    "transport_bootstrap",
    "token_exchange",
    "delegated_refresh",
}
_BOOTSTRAP_METHODS = {
    "initialize",
    "notifications/initialized",
    "server/discover",
    "tools/list",
    "transport/get",
}


class WorkloadEgressDenied(PermissionError):
    """The exact boot-local permit is absent, stale, mismatched, or consumed。"""


class WorkloadPermitRuntimeState(Protocol):
    """Minimal durable state read before issuance and again before wire egress。"""

    async def authority_epoch(self) -> int:
        """Return the current durable authority invalidation epoch。"""
        ...

    async def pending_audit_count(self) -> int:
        """Return pending security-audit outbox depth。"""
        ...

    async def runtime_authority_ready(self) -> bool:
        """Return whether no durable transition, drain, or rotation blocks egress。"""
        ...


class _PermitState(StrEnum):
    ISSUED = "issued"
    SENDING = "sending"
    ACKNOWLEDGED = "acknowledged"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class WorkloadEgressPermit:
    """Immutable capability for one exact OAuth-bearing wire attempt。"""

    permit_id: str
    boot_id: str
    authority_key: str
    authority_epoch: int
    task_id: str
    ticket_id: str
    egress_kind: str
    method: str
    payload_digest: str
    target_binding_digest: str
    attempt_number: int
    issued_at: float
    expires_at: float


class WorkloadEgressPermitManager:
    """Keep permits only in one Proxy process and drain them during revocation。"""

    def __init__(
        self,
        *,
        boot_id: str,
        runtime_state: WorkloadPermitRuntimeState,
        audit_high_watermark: int,
    ) -> None:
        if not boot_id or audit_high_watermark < 1:
            raise ValueError()
        self._boot_id = boot_id
        self._runtime_state = runtime_state
        self._audit_high_watermark = audit_high_watermark
        self._permits: dict[str, WorkloadEgressPermit] = {}
        self._states: dict[str, _PermitState] = {}
        self._blocked_authorities: set[str] = set()
        self._global_blocked = False
        self._global_fence_id: str | None = None
        self._drained: dict[str, asyncio.Event] = {}
        self._all_drained = asyncio.Event()
        self._all_drained.set()
        self._lock = asyncio.Lock()

    async def _runtime_snapshot(self) -> tuple[int, int, bool]:
        try:
            epoch = await self._runtime_state.authority_epoch()
            pending_audit = await self._runtime_state.pending_audit_count()
            authority_ready = await self._runtime_state.runtime_authority_ready()
        except Exception as exc:
            raise WorkloadEgressDenied() from exc
        if epoch < 1 or pending_audit < 0 or not isinstance(authority_ready, bool):
            raise WorkloadEgressDenied()
        return epoch, pending_audit, authority_ready

    @staticmethod
    def _validate_binding(
        *,
        authority_key: str,
        task_id: str,
        ticket_id: str,
        egress_kind: str,
        method: str,
        payload_digest: str,
        target_binding_digest: str,
        attempt_number: int,
        issued_at: float,
        expires_at: float,
    ) -> None:
        if (
            not authority_key
            or not task_id
            or not ticket_id
            or egress_kind not in _EGRESS_KINDS
            or (
                egress_kind == "mcp_bearer"
                and method != _TOOLS_CALL_METHOD
            )
            or (
                egress_kind == "transport_bootstrap"
                and method not in _BOOTSTRAP_METHODS
            )
            or (
                egress_kind in {"token_exchange", "delegated_refresh"}
                and method != "POST"
            )
            or _DIGEST_RE.fullmatch(payload_digest) is None
            or _DIGEST_RE.fullmatch(target_binding_digest) is None
            or attempt_number not in {1, 2}
            or expires_at <= issued_at
        ):
            raise WorkloadEgressDenied()

    async def issue(
        self,
        *,
        authority_key: str,
        task_id: str,
        ticket_id: str,
        egress_kind: str,
        method: str,
        payload_digest: str,
        target_binding_digest: str,
        attempt_number: int,
        issued_at: float,
        expires_at: float,
    ) -> WorkloadEgressPermit:
        """Issue one attempt capability after checking epoch and audit pressure。"""
        self._validate_binding(
            authority_key=authority_key,
            task_id=task_id,
            ticket_id=ticket_id,
            egress_kind=egress_kind,
            method=method,
            payload_digest=payload_digest,
            target_binding_digest=target_binding_digest,
            attempt_number=attempt_number,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        async with self._lock:
            if self._global_blocked or authority_key in self._blocked_authorities:
                raise WorkloadEgressDenied()
            epoch, pending_audit, authority_ready = await self._runtime_snapshot()
            if not authority_ready or pending_audit >= self._audit_high_watermark:
                raise WorkloadEgressDenied()
            permit = WorkloadEgressPermit(
                permit_id=str(uuid4()),
                boot_id=self._boot_id,
                authority_key=authority_key,
                authority_epoch=epoch,
                task_id=task_id,
                ticket_id=ticket_id,
                egress_kind=egress_kind,
                method=method,
                payload_digest=payload_digest,
                target_binding_digest=target_binding_digest,
                attempt_number=attempt_number,
                issued_at=issued_at,
                expires_at=expires_at,
            )
            self._permits[permit.permit_id] = permit
            self._states[permit.permit_id] = _PermitState.ISSUED
        return permit

    async def begin_send(
        self,
        permit: WorkloadEgressPermit,
        *,
        task_id: str,
        ticket_id: str,
        egress_kind: str,
        method: str,
        payload_digest: str,
        target_binding_digest: str,
        now: float,
    ) -> None:
        """Atomically consume issuance and enter the sole in-flight state。"""
        async with self._lock:
            if self._global_blocked or permit.authority_key in self._blocked_authorities:
                raise WorkloadEgressDenied()
            epoch, pending_audit, authority_ready = await self._runtime_snapshot()
            stored = self._permits.get(permit.permit_id)
            if (
                stored != permit
                or permit.boot_id != self._boot_id
                or self._states.get(permit.permit_id) != _PermitState.ISSUED
                or permit.authority_epoch != epoch
                or not authority_ready
                or pending_audit >= self._audit_high_watermark
                or not permit.issued_at <= now < permit.expires_at
                or permit.task_id != task_id
                or permit.ticket_id != ticket_id
                or permit.egress_kind != egress_kind
                or permit.method != method
                or permit.payload_digest != payload_digest
                or permit.target_binding_digest != target_binding_digest
            ):
                raise WorkloadEgressDenied()
            self._states[permit.permit_id] = _PermitState.SENDING
            self._drained.setdefault(permit.authority_key, asyncio.Event()).clear()
            self._all_drained.clear()

    async def acknowledge(self, permit_id: str) -> None:
        """Acknowledge wire completion or cancellation after the send task exits。"""
        async with self._lock:
            permit = self._permits.get(permit_id)
            if permit is None or self._states.get(permit_id) != _PermitState.SENDING:
                raise WorkloadEgressDenied()
            self._states.pop(permit_id, None)
            self._permits.pop(permit_id, None)
            if not self._authority_has_sending(permit.authority_key):
                self._drained.setdefault(permit.authority_key, asyncio.Event()).set()
            if not self._has_sending():
                self._all_drained.set()

    async def release(self, permit_id: str) -> None:
        """Idempotently cancel unused issuance or acknowledge an exited send。"""
        async with self._lock:
            permit = self._permits.pop(permit_id, None)
            state = self._states.pop(permit_id, None)
            if permit is None or state is None:
                return
            if state not in {_PermitState.ISSUED, _PermitState.SENDING}:
                raise WorkloadEgressDenied()
            if not self._authority_has_sending(permit.authority_key):
                self._drained.setdefault(permit.authority_key, asyncio.Event()).set()
            if not self._has_sending():
                self._all_drained.set()

    def _authority_has_sending(self, authority_key: str) -> bool:
        return any(
            permit.authority_key == authority_key
            and self._states.get(permit_id) == _PermitState.SENDING
            for permit_id, permit in self._permits.items()
        )

    def _has_sending(self) -> bool:
        return any(state == _PermitState.SENDING for state in self._states.values())

    async def begin_global_revocation(self) -> str:
        """Stop every new P8-B permit before one global epoch mutation。"""
        async with self._lock:
            if self._global_blocked or self._global_fence_id is not None:
                raise WorkloadEgressDenied()
            fence_id = str(uuid4())
            self._global_blocked = True
            self._global_fence_id = fence_id
            return fence_id

    def _require_global_fence(self, fence_id: str) -> None:
        if (
            not fence_id
            or not self._global_blocked
            or self._global_fence_id != fence_id
        ):
            raise WorkloadEgressDenied()

    async def abort_global_revocation(self, *, fence_id: str) -> None:
        """Reopen admission only when the durable mutation did not commit。"""
        async with self._lock:
            self._require_global_fence(fence_id)
            self._global_blocked = False
            self._global_fence_id = None

    async def commit_global_revocation(
        self,
        *,
        fence_id: str,
        new_authority_epoch: int,
    ) -> frozenset[str]:
        """Cancel all unused old-epoch permits and return every started task。"""
        async with self._lock:
            self._require_global_fence(fence_id)
            active_tasks: set[str] = set()
            unused: list[str] = []
            for permit_id, permit in self._permits.items():
                if permit.authority_epoch >= new_authority_epoch:
                    raise WorkloadEgressDenied()
                state = self._states.get(permit_id)
                if state == _PermitState.ISSUED:
                    unused.append(permit_id)
                elif state == _PermitState.SENDING:
                    active_tasks.add(permit.task_id)
            for permit_id in unused:
                self._states.pop(permit_id, None)
                self._permits.pop(permit_id, None)
            if active_tasks:
                self._all_drained.clear()
            else:
                self._all_drained.set()
            return frozenset(active_tasks)

    async def complete_global_revocation(self, *, fence_id: str) -> None:
        """Reopen admission only after durable finalize and explicit task drain。"""
        async with self._lock:
            self._require_global_fence(fence_id)
            if self._has_sending():
                raise WorkloadEgressDenied()
            self._global_blocked = False
            self._global_fence_id = None

    async def wait_all_drained(self, *, timeout: float) -> bool:
        """Wait only for explicit exit acknowledgement from every started send。"""
        if timeout < 0:
            raise ValueError()
        async with self._lock:
            if not self._has_sending():
                self._all_drained.set()
        try:
            await asyncio.wait_for(self._all_drained.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def begin_revocation(self, authority_key: str) -> None:
        """Close issuance before the caller performs the durable tombstone。"""
        if not authority_key:
            raise ValueError()
        async with self._lock:
            self._blocked_authorities.add(authority_key)
            self._drained.setdefault(authority_key, asyncio.Event())

    async def abort_revocation(self, authority_key: str) -> None:
        """Reopen issuance only when the durable tombstone transaction failed。"""
        async with self._lock:
            self._blocked_authorities.discard(authority_key)

    async def commit_revocation(
        self,
        authority_key: str,
        *,
        new_authority_epoch: int,
    ) -> frozenset[str]:
        """Cancel unused permits and return in-flight task IDs for cancellation。"""
        async with self._lock:
            if authority_key not in self._blocked_authorities:
                raise WorkloadEgressDenied()
            active_tasks: set[str] = set()
            unused: list[str] = []
            for permit_id, permit in self._permits.items():
                if permit.authority_key != authority_key:
                    continue
                state = self._states.get(permit_id)
                if state == _PermitState.ISSUED:
                    unused.append(permit_id)
                elif state == _PermitState.SENDING:
                    active_tasks.add(permit.task_id)
                if permit.authority_epoch >= new_authority_epoch:
                    raise WorkloadEgressDenied()
            for permit_id in unused:
                self._states.pop(permit_id, None)
                self._permits.pop(permit_id, None)
            event = self._drained.setdefault(authority_key, asyncio.Event())
            if active_tasks:
                event.clear()
            else:
                event.set()
            return frozenset(active_tasks)

    async def wait_drained(self, authority_key: str, *, timeout: float) -> bool:
        """Wait for all started wire attempts to explicitly acknowledge exit。"""
        if timeout < 0:
            raise ValueError()
        async with self._lock:
            event = self._drained.setdefault(authority_key, asyncio.Event())
            if not self._authority_has_sending(authority_key):
                event.set()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True
