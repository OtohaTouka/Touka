"""P8-B Slice 1：workload profile、credential 與 delegation domain keys。"""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any

import pytest
from pydantic import ValidationError
from src.protocols.mcp.canonical import McpInvocationAuthorization, digest_mcp_arguments
from src.protocols.mcp.oauth.models import OAuthAuthorizationSlotKey


def _models() -> Any:
    module_name = "src.protocols.mcp.oauth.workload.models"
    assert importlib.util.find_spec(module_name) is not None, "workload model seam is missing"
    return importlib.import_module(module_name)


def _profile(**updates: Any) -> Any:
    values: dict[str, Any] = {
        "profile_id": "calendar-workload",
        "enabled": True,
        "expected_issuer": "https://issuer.example.test",
        "expected_resource": "https://mcp.example.test",
        "expected_transport_url": "https://mcp.example.test/mcp",
        "token_endpoint_auth_method": "client_secret_basic",
        "requested_scopes": frozenset({"calendar.read"}),
        "workload_principal_id": "heartbeat:Alice",
        "owner_user_id": "human-1",
        "execution_agent_id": "Alice",
        "tool_scopes": {"calendar.read": frozenset({"calendar.read"})},
        "allow_private_network": False,
        "generation": 3,
        "credential_id": "calendar-secret",
    }
    values.update(updates)
    return _models().WorkloadOAuthProfile(**values)


def _source_slot(**updates: Any) -> OAuthAuthorizationSlotKey:
    values: dict[str, Any] = {
        "human_principal_id": "human-1",
        "execution_agent_id": "Alice",
        "delegation_digest": None,
        "installation_id": "calendar-user-source",
        "installation_generation": 9,
        "auth_profile_id": "calendar-user",
    }
    values.update(updates)
    return OAuthAuthorizationSlotKey(**values)


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
def test_profile_accepts_only_the_two_approved_client_secret_methods(method: str) -> None:
    profile = _profile(token_endpoint_auth_method=method)
    assert profile.token_endpoint_auth_method == method

    with pytest.raises(ValidationError):
        _profile(token_endpoint_auth_method="private_key_jwt")


def test_profile_canonical_key_changes_for_security_relevant_fields() -> None:
    baseline = _profile().canonical_bytes()

    for changed in (
        _profile(generation=4),
        _profile(expected_resource="https://mcp.example.test/resource"),
        _profile(expected_transport_url="https://mcp.example.test/other-mcp"),
        _profile(token_endpoint_auth_method="client_secret_post"),
        _profile(
            requested_scopes=frozenset({"calendar.write"}),
            tool_scopes={"calendar.read": frozenset({"calendar.write"})},
        ),
    ):
        assert changed.canonical_bytes() != baseline


def test_profile_requires_exact_canonical_transport_on_resource_origin() -> None:
    profile = _profile(expected_transport_url="https://MCP.example.test:443/mcp")
    assert profile.expected_transport_url == "https://mcp.example.test/mcp"

    with pytest.raises(ValidationError):
        _models().WorkloadOAuthProfile.model_validate(
            _profile().model_dump(exclude={"expected_transport_url"})
        )
    with pytest.raises(ValidationError):
        _profile(expected_transport_url="https://other.example.test/mcp")


@pytest.mark.parametrize(
    "transport_url",
    [
        "http://mcp.example.test/mcp",
        "https://user:pass@mcp.example.test/mcp",
        "https://mcp.example.test/mcp?token=secret",
        "https://mcp.example.test/mcp#fragment",
    ],
)
def test_profile_rejects_ambiguous_transport_url(transport_url: str) -> None:
    with pytest.raises(ValidationError):
        _profile(expected_transport_url=transport_url)


def test_private_network_profile_requires_exact_non_global_cidrs() -> None:
    with pytest.raises(ValidationError):
        _profile(allow_private_network=True)
    with pytest.raises(ValidationError):
        _profile(private_network_cidrs=("10.0.0.0/8",))
    with pytest.raises(ValidationError):
        _profile(
            allow_private_network=True,
            private_network_cidrs=("0.0.0.0/0",),
        )
    with pytest.raises(ValidationError):
        _profile(
            allow_private_network=True,
            private_network_cidrs=("10.0.0.1/8",),
        )

    profile = _profile(
        allow_private_network=True,
        private_network_cidrs=("10.0.0.0/8", "fd00::/8"),
    )
    assert profile.private_network_cidrs == ("10.0.0.0/8", "fd00::/8")
    assert profile.canonical_bytes() != _profile().canonical_bytes()


def test_delegation_binds_exact_p8a_slot_grant_tool_and_generations() -> None:
    models = _models()
    record = models.WorkloadDelegationRecord(
        delegation_id="delegation-1",
        owner_user_id="human-1",
        source_slot=_source_slot(),
        source_profile_generation=6,
        source_grant_id="grant-1",
        source_grant_version=2,
        execution_agent_id="Alice",
        installation_id="calendar",
        installation_generation=4,
        raw_tool_name="行事曆",
        required_scopes=frozenset({"calendar.read"}),
        generation=1,
        status="active",
        issued_at=100.0,
        expires_at=200.0,
    )

    assert record.digest.startswith("sha256:")
    assert record.source_slot.delegation_digest is None

    with pytest.raises(ValidationError, match="source slot"):
        models.WorkloadDelegationRecord(
            **record.model_dump(exclude={"source_slot"}),
            source_slot=_source_slot(delegation_digest=f"sha256:{'3' * 64}"),
        )
    with pytest.raises(ValidationError):
        models.WorkloadDelegationRecord(
            **record.model_dump(exclude={"required_scopes"}),
            required_scopes=frozenset(),
        )


def test_handle_principal_key_isolates_all_authority_generations() -> None:
    values: dict[str, Any] = {
        "authorization_id": "authz-1",
        "authority_kind": "workload",
        "workload_principal_id": "heartbeat:Alice",
        "owner_user_id": "human-1",
        "execution_agent_id": "Alice",
        "origin": "heartbeat",
        "installation_id": "calendar",
        "installation_generation": 4,
        "auth_profile_id": "calendar-workload",
        "profile_generation": 6,
        "target_binding_digest": f"sha256:{'2' * 64}",
        "credential_generation": 2,
        "raw_tool_name": "行事曆",
        "arguments_digest": digest_mcp_arguments({}),
        "required_scopes": frozenset({"calendar.read"}),
        "auth_level": "SafeAuto",
        "policy_digest": f"sha256:{'1' * 64}",
        "request_id": "request-1",
        "issued_at": 100.0,
        "expires_at": 110.0,
    }
    baseline = McpInvocationAuthorization(**values).handle_principal_key

    for field, value in (
        ("installation_generation", 5),
        ("profile_generation", 7),
        ("credential_generation", 3),
        ("raw_tool_name", "另一個工具"),
    ):
        changed = dict(values)
        changed[field] = value
        assert McpInvocationAuthorization(**changed).handle_principal_key != baseline
