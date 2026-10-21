"""P8-B Slice 2：同步 security audit sink 與 durable outbox ack。"""

from __future__ import annotations

import importlib
import importlib.util
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from src.protocols.mcp.canonical import (
    McpInvocationAuthorization,
    McpWorkloadWireAttempt,
    digest_mcp_arguments,
)
from src.protocols.mcp.oauth.keyring import OAuthKey, OAuthKeyRing
from src.protocols.mcp.oauth.workload.models import (
    WorkloadAuditEvent,
    WorkloadCredentialPayload,
)
from src.protocols.mcp.oauth.workload.store import WorkloadOAuthStore
from src.services import audit_service as audit_module

if TYPE_CHECKING:
    from pathlib import Path


def _event(event_id: str) -> WorkloadAuditEvent:
    return WorkloadAuditEvent(
        event_id=event_id,
        action="credential.activate",
        actor_id="human-1",
        authority_id="credential-1",
        authority_generation=1,
        request_id="request-1",
        outcome="success",
        created_at=100.0,
        details_digest=f"sha256:{'5' * 64}",
    )


def _security_event() -> Any:
    event_type = getattr(audit_module, "SecurityAuditEvent", None)
    assert event_type is not None, "SecurityAuditEvent seam is missing"
    return event_type(
        event_id="security-1",
        category="mcp_oauth_workload",
        action="credential.activate",
        actor_id="human-1",
        authority_id="credential-1",
        authority_generation=1,
        request_id="request-1",
        outcome="success",
        created_at=100.0,
        details_digest=f"sha256:{'5' * 64}",
    )


def test_security_event_sink_is_synchronous_and_injectable() -> None:
    received: list[Any] = []

    audit_module.audit_service.emit_security_event(
        _security_event(),
        sink=received.append,
    )

    assert [event.event_id for event in received] == ["security-1"]


def test_security_event_sink_failure_propagates_to_outbox_owner() -> None:
    def _fail(_event: Any) -> None:
        raise OSError

    with pytest.raises(OSError):
        audit_module.audit_service.emit_security_event(_security_event(), sink=_fail)


@pytest.mark.asyncio
async def test_outbox_ack_happens_only_after_sink_success(tmp_path: Path) -> None:
    module_name = "src.protocols.mcp.oauth.workload.audit"
    assert importlib.util.find_spec(module_name) is not None, "outbox drainer seam is missing"
    workload_audit = importlib.import_module(module_name)
    store = WorkloadOAuthStore(
        tmp_path / "workload.sqlite3",
        OAuthKeyRing(current=OAuthKey(kid="workload-k1", material=b"k" * 32)),
    )
    await store.prepare_schema()
    await store.activate_credential(
        WorkloadCredentialPayload(
            profile_id="profile-1",
            credential_id="credential-1",
            generation=1,
            client_id=SecretStr("client-id"),
            client_secret=SecretStr("client-secret"),
        ),
        profile_generation=1,
        now=100.0,
        event=_event("outbox-1"),
    )
    attempts = 0

    def _sink(_event: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError

    drainer = workload_audit.WorkloadAuditOutboxDrainer(
        store=store,
        sink=_sink,
        clock=lambda: 101.0,
    )
    with pytest.raises(OSError):
        await drainer.drain_once()
    assert await store.pending_audit_count() == 1

    assert await drainer.drain_once() == 1
    assert await store.pending_audit_count() == 0


@pytest.mark.asyncio
async def test_egress_intent_is_durable_and_secret_free_before_permit(tmp_path: Path) -> None:
    workload_audit = importlib.import_module("src.protocols.mcp.oauth.workload.audit")
    store = WorkloadOAuthStore(
        tmp_path / "workload.sqlite3",
        OAuthKeyRing(current=OAuthKey(kid="workload-k1", material=b"k" * 32)),
    )
    await store.prepare_schema()
    accepter = workload_audit.WorkloadEgressAuditAccepter(store=store)
    ticket = McpInvocationAuthorization(
        authorization_id="authz-1",
        authority_kind="workload",
        workload_principal_id="heartbeat:Alice",
        owner_user_id="owner-1",
        execution_agent_id="Alice",
        origin="heartbeat",
        installation_id="calendar",
        installation_generation=4,
        auth_profile_id="calendar-workload",
        profile_generation=6,
        target_binding_digest=f"sha256:{'3' * 64}",
        credential_generation=2,
        raw_tool_name="calendar.read",
        arguments_digest=digest_mcp_arguments({}),
        required_scopes=frozenset({"calendar.read"}),
        auth_level="SafeAuto",
        policy_digest=f"sha256:{'1' * 64}",
        request_id="request-1",
        issued_at=100.0,
        expires_at=200.0,
    )

    await accepter.accept_egress_intent(
        authorization=ticket,
        egress_kind="mcp_bearer",
        wire_attempt=McpWorkloadWireAttempt(
            authorization_id=ticket.authorization_id,
            egress_kind="mcp_bearer",
            method="tools/call",
            payload_digest=f"sha256:{'2' * 64}",
            target_binding_digest=ticket.target_binding_digest,
            raw_tool_name="calendar.read",
            arguments_digest=ticket.arguments_digest,
        ),
        attempt_number=1,
        now=150.0,
    )

    events = await store.pending_audit_events()
    assert len(events) == 1
    assert events[0].authority_id == "heartbeat:Alice"
    assert events[0].authority_generation == 2
    assert events[0].outcome == "pending"
    assert "secret" not in events[0].model_dump_json().lower()
