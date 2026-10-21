"""P8-B Slice 3：explicit metadata refresh 與 profile-pinned snapshot。"""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any

import pytest
from src.protocols.mcp.oauth.workload.models import WorkloadOAuthProfile


def _metadata() -> Any:
    module_name = "src.protocols.mcp.oauth.workload.metadata"
    assert importlib.util.find_spec(module_name) is not None, "metadata seam is missing"
    return importlib.import_module(module_name)


def _profile(**updates: Any) -> WorkloadOAuthProfile:
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
    return WorkloadOAuthProfile(**values)


def _resource_metadata(**updates: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "resource": "https://mcp.example.test",
        "authorization_servers": ["https://issuer.example.test"],
        "scopes_supported": ["calendar.read", "calendar.write"],
    }
    values.update(updates)
    return values


def _authorization_metadata(**updates: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "issuer": "https://issuer.example.test",
        "token_endpoint": "https://issuer.example.test/oauth/token",
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
        ],
        "scopes_supported": ["calendar.read", "calendar.write"],
    }
    values.update(updates)
    return values


def test_verified_snapshot_is_pinned_to_profile_generation_and_fingerprint() -> None:
    profile = _profile()
    authorization_metadata = _authorization_metadata()

    snapshot = _metadata().verify_workload_metadata(
        profile=profile,
        resource_metadata=_resource_metadata(),
        authorization_metadata=authorization_metadata,
        verified_at=100.0,
    )

    assert snapshot.profile_id == profile.profile_id
    assert snapshot.profile_generation == profile.generation
    assert snapshot.profile_fingerprint == profile.fingerprint
    assert snapshot.token_endpoint == authorization_metadata["token_endpoint"]
    assert snapshot.token_endpoint_auth_method == profile.token_endpoint_auth_method
    assert snapshot.metadata_security_fingerprint.startswith("sha256:")


def test_verified_snapshot_preserves_exact_private_network_cidrs() -> None:
    profile = _profile(
        allow_private_network=True,
        private_network_cidrs=("10.0.0.0/8",),
    )

    snapshot = _metadata().verify_workload_metadata(
        profile=profile,
        resource_metadata=_resource_metadata(),
        authorization_metadata=_authorization_metadata(),
        verified_at=100.0,
    )

    assert snapshot.private_network_cidrs == ("10.0.0.0/8",)


@pytest.mark.parametrize(
    ("resource_updates", "authorization_updates"),
    [
        ({"resource": "https://other.example.test"}, {}),
        ({"authorization_servers": ["https://other-issuer.example.test"]}, {}),
        ({}, {"issuer": "https://other-issuer.example.test"}),
        ({}, {"token_endpoint": "http://issuer.example.test/token"}),
        ({}, {"token_endpoint_auth_methods_supported": ["private_key_jwt"]}),
        ({"scopes_supported": ["calendar.write"]}, {}),
        ({}, {"scopes_supported": ["calendar.write"]}),
    ],
)
def test_metadata_mismatch_or_unsupported_method_fails_closed(
    resource_updates: dict[str, Any],
    authorization_updates: dict[str, Any],
) -> None:
    with pytest.raises(_metadata().WorkloadMetadataVerificationError):
        _metadata().verify_workload_metadata(
            profile=_profile(),
            resource_metadata=_resource_metadata(**resource_updates),
            authorization_metadata=_authorization_metadata(**authorization_updates),
            verified_at=100.0,
        )


def test_disabled_profile_cannot_produce_verified_metadata() -> None:
    with pytest.raises(_metadata().WorkloadMetadataVerificationError):
        _metadata().verify_workload_metadata(
            profile=_profile(enabled=False),
            resource_metadata=_resource_metadata(),
            authorization_metadata=_authorization_metadata(),
            verified_at=100.0,
        )
