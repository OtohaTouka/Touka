"""P8-B Slice 1：不可變 invocation ticket 與 wire binding。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from src.protocols.mcp import canonical
from src.protocols.mcp.canonical import digest_mcp_arguments


def _ticket_type() -> type[Any]:
    ticket_type = getattr(canonical, "McpInvocationAuthorization", None)
    assert ticket_type is not None, "McpInvocationAuthorization seam is missing"
    return ticket_type


def _ticket(**updates: Any) -> Any:
    values: dict[str, Any] = {
        "authorization_id": "authz-1",
        "authority_kind": "workload",
        "workload_principal_id": "heartbeat:Alice",
        "owner_user_id": "owner-1",
        "execution_agent_id": "Alice",
        "origin": "heartbeat",
        "installation_id": "calendar",
        "installation_generation": 3,
        "auth_profile_id": "calendar-workload",
        "profile_generation": 4,
        "target_binding_digest": f"sha256:{'2' * 64}",
        "credential_generation": 5,
        "operation_method": "tools/call",
        "raw_tool_name": "行事曆",
        "arguments_digest": digest_mcp_arguments({"day": "monday"}),
        "required_scopes": frozenset({"calendar.read"}),
        "auth_level": "SafeAuto",
        "policy_digest": f"sha256:{'1' * 64}",
        "request_id": "request-1",
        "issued_at": 100.0,
        "expires_at": 110.0,
    }
    values.update(updates)
    return _ticket_type()(**values)


def test_workload_ticket_is_frozen_and_contains_no_bearer_material() -> None:
    ticket = _ticket()

    with pytest.raises(ValidationError):
        ticket.raw_tool_name = "other"

    serialized = ticket.model_dump_json()
    assert "access_token" not in serialized
    assert "client_secret" not in serialized
    assert "bearer" not in serialized.lower()


@pytest.mark.parametrize("origin", ["foreground", "dispatch"])
def test_workload_ticket_rejects_non_heartbeat_origins(origin: str) -> None:
    with pytest.raises(ValidationError, match="heartbeat"):
        _ticket(origin=origin)


def test_delegation_ticket_requires_exact_owner_and_generation_binding() -> None:
    with pytest.raises(ValidationError, match="(?i)delegation"):
        _ticket(
            authority_kind="delegation",
            workload_principal_id=None,
            human_principal_id="human-1",
            delegation_digest=None,
            delegation_generation=None,
        )

    ticket = _ticket(
        authority_kind="delegation",
        workload_principal_id=None,
        human_principal_id="human-1",
        delegation_digest=f"sha256:{'2' * 64}",
        delegation_generation=7,
        credential_generation=None,
    )
    assert ticket.delegation_generation == 7
    assert ticket.credential_generation is None


def test_ticket_requires_exactly_one_authority_generation() -> None:
    with pytest.raises(ValidationError, match="(?i)credential generation"):
        _ticket(credential_generation=None)

    with pytest.raises(ValidationError, match="(?i)delegation"):
        _ticket(
            authority_kind="delegation",
            workload_principal_id=None,
            human_principal_id="human-1",
            delegation_digest=f"sha256:{'2' * 64}",
            delegation_generation=7,
            credential_generation=5,
        )


def test_ticket_verifies_exact_wire_method_name_and_arguments() -> None:
    ticket = _ticket()

    ticket.verify_wire_attempt(
        method="tools/call",
        tool_name="行事曆",
        arguments={"day": "monday"},
    )

    for attempt in (
        {"method": "tools/list", "tool_name": "行事曆", "arguments": {"day": "monday"}},
        {"method": "tools/call", "tool_name": "行事曆 ", "arguments": {"day": "monday"}},
        {"method": "tools/call", "tool_name": "行事曆", "arguments": {"day": "tuesday"}},
    ):
        with pytest.raises(canonical.McpInvocationBindingError):
            ticket.verify_wire_attempt(**attempt)

    with pytest.raises(canonical.McpInvocationBindingError):
        ticket.verify_wire_attempt(
            method="tools/call",
            tool_name="行事曆",
            arguments={"day": "monday"},
            is_batch=True,
        )


def test_tool_name_catalog_rejects_duplicate_and_unicode_casefold_collisions() -> None:
    canonical.validate_mcp_tool_name_set(("calendar", "weather"))

    with pytest.raises(canonical.McpToolNameCollisionError):
        canonical.validate_mcp_tool_name_set(("calendar", "calendar"))
    with pytest.raises(canonical.McpToolNameCollisionError):
        canonical.validate_mcp_tool_name_set(("Café", "Cafe\u0301"))
    with pytest.raises(canonical.McpToolNameCollisionError):
        canonical.validate_mcp_tool_name_set(("Calendar", "calendar"))
