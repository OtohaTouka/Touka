"""Durable P8-B audit outbox delivery。"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from src.protocols.mcp.oauth.workload.models import WorkloadAuditEvent
from src.services import audit_service

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.protocols.mcp.canonical import (
        McpInvocationAuthorization,
        McpWorkloadWireAttempt,
    )


class WorkloadAuditIntentStore(Protocol):
    """Standalone transactional audit insertion seam。"""

    async def enqueue_audit_event(self, event: WorkloadAuditEvent) -> None:
        """Commit one pending event or fail closed。"""
        ...


class WorkloadEgressAuditAccepter:
    """Convert an immutable ticket into a secret-free durable egress intent。"""

    def __init__(self, *, store: WorkloadAuditIntentStore) -> None:
        self._store = store

    async def accept_egress_intent(
        self,
        *,
        authorization: McpInvocationAuthorization,
        egress_kind: str,
        wire_attempt: McpWorkloadWireAttempt | None = None,
        attempt_number: int,
        now: float,
    ) -> None:
        """Persist before permit issuance; a store failure blocks all egress。"""
        allowed_attempts = {
            "token_exchange": {1, 2},
            "mcp_bearer": {1},
            "transport_bootstrap": {1},
            "delegated_refresh": {1},
        }
        if attempt_number not in allowed_attempts.get(egress_kind, set()):
            raise ValueError()
        if wire_attempt is None or wire_attempt.egress_kind != egress_kind:
            raise ValueError()
        authority_id = (
            authorization.workload_principal_id
            if authorization.authority_kind == "workload"
            else authorization.delegation_digest
        )
        authority_generation = (
            authorization.credential_generation
            if authorization.authority_kind == "workload"
            else authorization.delegation_generation
        )
        if authority_id is None or authority_generation is None:
            raise ValueError()
        details = json.dumps(
            {
                "attempt_number": attempt_number,
                "authorization_id": authorization.authorization_id,
                "egress_kind": egress_kind,
                "installation_id": authorization.installation_id,
                "operation_method": authorization.operation_method,
                "raw_tool_name": authorization.raw_tool_name,
                "ticket_digest": authorization.handle_principal_key,
                "wire_authorization_id": (
                    wire_attempt.authorization_id
                ),
                "wire_method": wire_attempt.method,
                "wire_payload_digest": wire_attempt.payload_digest,
                "wire_target_binding_digest": (
                    wire_attempt.target_binding_digest
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        await self._store.enqueue_audit_event(
            WorkloadAuditEvent(
                event_id=str(uuid4()),
                action=f"{egress_kind}.intent",
                actor_id=authorization.execution_agent_id,
                authority_id=authority_id,
                authority_generation=authority_generation,
                request_id=authorization.request_id,
                outcome="pending",
                created_at=now,
                details_digest=f"sha256:{hashlib.sha256(details).hexdigest()}",
            )
        )


class WorkloadAuditStore(Protocol):
    """Minimal durable outbox contract used by the delivery loop。"""

    async def pending_audit_events(self) -> list[WorkloadAuditEvent]:
        """Return pending events in deterministic order。"""
        ...

    async def acknowledge_audit_event(self, event_id: str, *, acked_at: float) -> None:
        """Idempotently mark one delivered event acknowledged。"""
        ...


class WorkloadAuditOutboxDrainer:
    """Deliver synchronously, then acknowledge; sink failures leave rows pending。"""

    def __init__(
        self,
        *,
        store: WorkloadAuditStore,
        sink: Callable[[audit_service.SecurityAuditEvent], None],
        clock: Callable[[], float],
    ) -> None:
        self._store = store
        self._sink = sink
        self._clock = clock

    async def drain_once(self) -> int:
        """Drain the current bounded snapshot and propagate the first sink failure。"""
        delivered = 0
        for event in await self._store.pending_audit_events():
            security_event = audit_service.SecurityAuditEvent(
                event_id=event.event_id,
                category="mcp_oauth_workload",
                action=event.action,
                actor_id=event.actor_id,
                authority_id=event.authority_id,
                authority_generation=event.authority_generation,
                request_id=event.request_id,
                outcome=event.outcome,
                created_at=event.created_at,
                details_digest=event.details_digest,
            )
            self._sink(security_event)
            await self._store.acknowledge_audit_event(
                event.event_id,
                acked_at=self._clock(),
            )
            delivered += 1
        return delivered
