# ruff: noqa: TC002
"""P8-B workload OAuth non-secret profiles and exact delegation bindings。"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from src.models.permissions import BasePolicyModel
from src.protocols.mcp.oauth.models import OAuthAuthorizationSlotKey
from src.protocols.mcp.oauth.outbound import validate_oauth_endpoint
from src.protocols.mcp.oauth.workload.target import (
    canonical_mcp_transport_url,
    resource_and_transport_share_origin,
)

_SCOPE_TOKEN_RE = re.compile(r"^[\x21\x23-\x5B\x5D-\x7E]+$")


class WorkloadDelegationStatus(StrEnum):
    """Durable owner-delegation states; only ACTIVE may issue a permit。"""

    ACTIVE = "active"
    TOMBSTONED = "tombstoned"
    RECOVERY_REQUIRED = "recovery_required"
    REVOCATION_PENDING = "revocation_pending"


class WorkloadRevocationStatus(StrEnum):
    """Durable revoke lifecycle; completion requires external drain acknowledgement。"""

    DRAINING = "draining"
    COMPLETED = "completed"
    REVOCATION_PENDING = "revocation_pending"
    RECOVERY_REQUIRED = "recovery_required"


class WorkloadMutationKind(StrEnum):
    """Durable owner classes sharing the sole workload mutation admission gate。"""

    PROFILE = "profile"
    CREDENTIAL = "credential"
    METADATA = "metadata"
    DELEGATION = "delegation"
    SOURCE_DISCONNECT = "source_disconnect"
    ROTATION = "rotation"
    RECOVERY = "recovery"


class WorkloadOAuthProfile(BaseModel):
    """Strict non-secret client-credentials profile stored in JSON。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    enabled: bool = False
    expected_issuer: str
    expected_resource: str
    expected_transport_url: str
    token_endpoint_auth_method: Literal[
        "client_secret_basic",
        "client_secret_post",
    ]
    requested_scopes: frozenset[str] = Field(default_factory=frozenset)
    workload_principal_id: str = Field(min_length=1, max_length=256)
    owner_user_id: str = Field(min_length=1, max_length=256)
    execution_agent_id: str = Field(min_length=1, max_length=256)
    tool_scopes: dict[str, frozenset[str]] = Field(default_factory=dict)
    allow_private_network: bool = False
    private_network_cidrs: tuple[str, ...] = ()
    generation: int = Field(ge=1)
    credential_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    @field_validator("expected_issuer", "expected_resource")
    @classmethod
    def validate_https_binding(cls, value: str) -> str:
        """Keep issuer/resource credential bindings on hardened HTTPS origins。"""
        return validate_oauth_endpoint(value)

    @field_validator("expected_transport_url")
    @classmethod
    def validate_transport_binding(cls, value: str) -> str:
        """Canonicalize the exact credential-bearing MCP transport URL。"""
        return canonical_mcp_transport_url(value)

    @field_validator("requested_scopes")
    @classmethod
    def validate_scopes(cls, value: frozenset[str]) -> frozenset[str]:
        """Accept only RFC-style scope tokens without whitespace aliases。"""
        if any(not _SCOPE_TOKEN_RE.fullmatch(scope) for scope in value):
            raise ValueError()
        return value

    @field_validator("private_network_cidrs")
    @classmethod
    def validate_private_network_cidrs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Accept only unique canonical non-global CIDRs; broad aliases fail closed。"""
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for item in value:
            try:
                network = ipaddress.ip_network(item, strict=True)
            except ValueError as exc:
                raise ValueError() from exc
            if item != str(network) or network.prefixlen == 0 or network.is_global:
                raise ValueError()
            networks.append(network)
        canonical = tuple(
            str(network)
            for network in sorted(
                networks,
                key=lambda item: (item.version, int(item.network_address), item.prefixlen),
            )
        )
        if len(canonical) != len(set(canonical)):
            raise ValueError()
        return canonical

    @model_validator(mode="after")
    def validate_exact_tool_scopes(self) -> WorkloadOAuthProfile:
        """Require explicit non-empty per-tool scopes within the metadata request。"""
        if (
            not self.tool_scopes
            or self.allow_private_network != bool(self.private_network_cidrs)
            or not resource_and_transport_share_origin(
                self.expected_resource,
                self.expected_transport_url,
            )
        ):
            raise ValueError()
        for tool_name, scopes in self.tool_scopes.items():
            if (
                not tool_name
                or len(tool_name) > 1024
                or not scopes
                or not scopes.issubset(self.requested_scopes)
                or any(not _SCOPE_TOKEN_RE.fullmatch(scope) for scope in scopes)
            ):
                raise ValueError()
        return self

    def canonical_bytes(self) -> bytes:
        """Return deterministic bytes for JSON↔SQLite generation/fingerprint checks。"""
        value = {
            "allow_private_network": self.allow_private_network,
            "credential_id": self.credential_id,
            "enabled": self.enabled,
            "expected_issuer": self.expected_issuer,
            "expected_resource": self.expected_resource,
            "expected_transport_url": self.expected_transport_url,
            "generation": self.generation,
            "execution_agent_id": self.execution_agent_id,
            "owner_user_id": self.owner_user_id,
            "private_network_cidrs": list(self.private_network_cidrs),
            "profile_id": self.profile_id,
            "requested_scopes": sorted(self.requested_scopes),
            "token_endpoint_auth_method": self.token_endpoint_auth_method,
            "tool_scopes": {
                tool_name: sorted(scopes)
                for tool_name, scopes in sorted(self.tool_scopes.items())
            },
            "workload_principal_id": self.workload_principal_id,
        }
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def fingerprint(self) -> str:
        """Digest of every security-relevant profile field。"""
        return f"sha256:{hashlib.sha256(self.canonical_bytes()).hexdigest()}"


class WorkloadCredentialPayload(BaseModel):
    """Encrypted-at-rest client credential material; never an API response。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = Field(min_length=1, max_length=64)
    credential_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1)
    client_id: SecretStr
    client_secret: SecretStr


class WorkloadProfilePolicy(BasePolicyModel):
    """Versioned non-secret workload profile policy SSOT。"""

    model_config = ConfigDict(extra="forbid")

    version: str
    author: str
    profiles: dict[str, WorkloadOAuthProfile] = Field(default_factory=dict)

    @model_validator(mode="after")
    def keys_match_profile_ids(self) -> WorkloadProfilePolicy:
        """Reject aliases between map keys and profile IDs。"""
        if any(key != profile.profile_id for key, profile in self.profiles.items()):
            raise ValueError()
        return self


class WorkloadAccessToken(BaseModel):
    """Ephemeral opaque bearer returned by a single client-credentials exchange。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    access_token: SecretStr
    token_type: Literal["Bearer"] = Field(default="Bearer")
    expires_at: float
    granted_scopes: frozenset[str]


class WorkloadAuditEvent(BaseModel):
    """Secret-free event persisted transactionally before external audit delivery。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1, max_length=256)
    action: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=256)
    authority_id: str = Field(min_length=1, max_length=256)
    authority_generation: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=256)
    outcome: Literal["pending", "success", "denied", "failure"]
    created_at: float
    details_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class WorkloadRevocationRecord(BaseModel):
    """Secret-free durable drain record used by the recovery control plane。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authority_key: str = Field(min_length=1, max_length=512)
    authority_id: str = Field(min_length=1, max_length=256)
    authority_generation: int = Field(ge=1)
    status: WorkloadRevocationStatus
    source_slot_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    mutation_operation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    updated_at: float

    @model_validator(mode="after")
    def exact_operation_or_manual_recovery(self) -> WorkloadRevocationRecord:
        """Only explicitly quarantined legacy rows may lack an exact owner。"""
        if (
            self.mutation_operation_id is None
            and self.status != WorkloadRevocationStatus.RECOVERY_REQUIRED
        ):
            raise ValueError()
        return self


class WorkloadMutationOwner(BaseModel):
    """Exact durable owner of the single P8-B authority mutation slot。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: str = Field(min_length=1, max_length=256)
    mutation_kind: WorkloadMutationKind


class WorkloadAuthorityTransition(BaseModel):
    """Crash-resumable JSON↔SQLite authority mutation journal record。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: str = Field(min_length=1, max_length=256)
    direction: Literal["expansion", "reduction"]
    authority_kind: Literal["profile", "credential", "delegation", "global"]
    authority_id: str = Field(min_length=1, max_length=256)
    authority_generation: int = Field(ge=1)
    actor_id: str = Field(min_length=1, max_length=256)
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal["pending_json", "recovery_required", "active", "completed"]
    created_at: float
    updated_at: float


class WorkloadMetadataSnapshot(BaseModel):
    """Verified derived OAuth metadata pinned to one exact profile generation。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = Field(min_length=1, max_length=64)
    profile_generation: int = Field(ge=1)
    profile_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    issuer: str
    resource: str
    token_endpoint: str
    token_endpoint_auth_method: Literal[
        "client_secret_basic",
        "client_secret_post",
    ]
    allow_private_network: bool
    private_network_cidrs: tuple[str, ...] = ()
    verified_scopes: frozenset[str] = Field(default_factory=frozenset)
    metadata_security_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verified_at: float

    @model_validator(mode="after")
    def private_network_binding_is_consistent(self) -> WorkloadMetadataSnapshot:
        """Reject metadata snapshots whose private-network switch and CIDRs drift。"""
        if self.allow_private_network != bool(self.private_network_cidrs):
            raise ValueError()
        return self


class WorkloadDelegationRecord(BaseModel):
    """Exact owner delegation of one existing P8-A grant to one heartbeat tool。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    delegation_id: str = Field(min_length=1, max_length=256)
    owner_user_id: str = Field(min_length=1, max_length=256)
    source_slot: OAuthAuthorizationSlotKey
    source_profile_generation: int = Field(ge=1)
    source_grant_id: str = Field(min_length=1, max_length=256)
    source_grant_version: int = Field(ge=1)
    execution_agent_id: str = Field(min_length=1, max_length=256)
    installation_id: str = Field(min_length=1, max_length=256)
    installation_generation: int = Field(ge=1)
    raw_tool_name: str = Field(min_length=1, max_length=1024)
    required_scopes: frozenset[str] = Field(default_factory=frozenset)
    generation: int = Field(ge=1)
    status: WorkloadDelegationStatus
    issued_at: float
    expires_at: float

    @field_validator("required_scopes")
    @classmethod
    def validate_delegated_scopes(cls, value: frozenset[str]) -> frozenset[str]:
        """Reject malformed delegated scopes before durable activation。"""
        if not value or any(not _SCOPE_TOKEN_RE.fullmatch(scope) for scope in value):
            raise ValueError()
        return value

    @model_validator(mode="after")
    def validate_exact_source_slot(self) -> WorkloadDelegationRecord:
        """Forbid chaining and require a distinct interactive source installation。"""
        slot = self.source_slot
        if (
            slot.delegation_digest is not None
            or slot.human_principal_id != self.owner_user_id
            or slot.execution_agent_id != self.execution_agent_id
            or slot.installation_id == self.installation_id
        ):
            msg = (
                "Delegation source slot must match owner and agent while naming a "
                "distinct interactive installation."
            )
            raise ValueError(msg)
        if self.expires_at <= self.issued_at:
            msg = "Delegation expiry must be after issuance."
            raise ValueError(msg)
        return self

    def canonical_bytes(self) -> bytes:
        """Return deterministic binding bytes excluding only the derived digest。"""
        value = self.model_dump(mode="json")
        value["required_scopes"] = sorted(self.required_scopes)
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        """Stable delegation identity used in invocation tickets and audit rows。"""
        return f"sha256:{hashlib.sha256(self.canonical_bytes()).hexdigest()}"


class WorkloadDelegationPolicy(BasePolicyModel):
    """Versioned non-secret delegation policy stored outside runtime state。"""

    model_config = ConfigDict(extra="forbid")

    version: str
    author: str
    delegations: dict[str, WorkloadDelegationRecord] = Field(default_factory=dict)

    @model_validator(mode="after")
    def keys_match_delegation_ids(self) -> WorkloadDelegationPolicy:
        """Reject aliases between the map key and signed delegation body。"""
        if any(
            key != delegation.delegation_id
            for key, delegation in self.delegations.items()
        ):
            raise ValueError()
        return self
