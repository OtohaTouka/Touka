"""Explicit P8-B metadata verification; never called lazily during token exchange。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.protocols.mcp.oauth.outbound import (
    OAuthOutboundPolicyError,
    validate_oauth_endpoint,
)
from src.protocols.mcp.oauth.workload import models as workload_models


class WorkloadMetadataVerificationError(ValueError):
    """Protected-resource or authorization-server metadata mismatched its profile。"""


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise WorkloadMetadataVerificationError()
    return tuple(value)


def _require(condition: bool) -> None:
    if not condition:
        raise WorkloadMetadataVerificationError()


def verify_workload_metadata(
    *,
    profile: workload_models.WorkloadOAuthProfile,
    resource_metadata: dict[str, Any],
    authorization_metadata: dict[str, Any],
    verified_at: float,
) -> workload_models.WorkloadMetadataSnapshot:
    """Build one immutable snapshot only after every expected binding matches。"""
    try:
        resource = resource_metadata.get("resource")
        issuer = authorization_metadata.get("issuer")
        token_endpoint = authorization_metadata.get("token_endpoint")
        authorization_servers = _string_list(
            resource_metadata.get("authorization_servers")
        )
        auth_methods = _string_list(
            authorization_metadata.get("token_endpoint_auth_methods_supported")
        )
        resource_scopes = frozenset(
            _string_list(resource_metadata.get("scopes_supported"))
        )
        authorization_scopes = frozenset(
            _string_list(authorization_metadata.get("scopes_supported"))
        )
        _require(isinstance(resource, str) and isinstance(issuer, str))
        _require(isinstance(token_endpoint, str))
        validate_oauth_endpoint(resource)
        validate_oauth_endpoint(issuer)
        validate_oauth_endpoint(token_endpoint)
    except (OAuthOutboundPolicyError, WorkloadMetadataVerificationError) as exc:
        raise WorkloadMetadataVerificationError() from exc

    expected_scopes = profile.requested_scopes
    if (
        not profile.enabled
        or resource != profile.expected_resource
        or issuer != profile.expected_issuer
        or authorization_servers != (profile.expected_issuer,)
        or profile.token_endpoint_auth_method not in auth_methods
        or not expected_scopes.issubset(resource_scopes)
        or not expected_scopes.issubset(authorization_scopes)
    ):
        raise WorkloadMetadataVerificationError()

    canonical_metadata = json.dumps(
        {
            "authorization": authorization_metadata,
            "resource": resource_metadata,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    metadata_fingerprint = f"sha256:{hashlib.sha256(canonical_metadata).hexdigest()}"
    return workload_models.WorkloadMetadataSnapshot(
        profile_id=profile.profile_id,
        profile_generation=profile.generation,
        profile_fingerprint=profile.fingerprint,
        issuer=issuer,
        resource=resource,
        token_endpoint=token_endpoint,
        token_endpoint_auth_method=profile.token_endpoint_auth_method,
        allow_private_network=profile.allow_private_network,
        private_network_cidrs=profile.private_network_cidrs,
        verified_scopes=resource_scopes & authorization_scopes,
        metadata_security_fingerprint=metadata_fingerprint,
        verified_at=verified_at,
    )
