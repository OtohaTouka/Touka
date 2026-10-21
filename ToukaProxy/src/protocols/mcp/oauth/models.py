# ruff: noqa: S105, TC002
"""P8-A OAuth durable domain models。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class OAuthInvalidGrantError(ValueError):
    """Authorization server rejected a refresh token as invalid_grant。"""


class OAuthTransactionConflictError(ValueError):
    """Another worker already owns the one live transaction for this slot。"""


class OAuthCallbackRouteError(ValueError):
    """State exists but is bound to a different callback route."""


class OAuthCredentialWireBinding(BaseModel):
    """Secret-free canonical binding for one exact OAuth credential request。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    http_method: Literal["POST"] = "POST"
    grant_type: Literal["client_credentials", "refresh_token"]
    token_endpoint: str = Field(min_length=1)
    resource: str = Field(min_length=1)
    scopes: tuple[str, ...] = ()
    client_auth_method: str = Field(min_length=1)
    form_fields: tuple[str, ...]
    profile_id: str | None = None
    profile_generation: int | None = Field(default=None, ge=1)
    profile_fingerprint: str | None = None
    metadata_security_fingerprint: str = Field(min_length=1)
    credential_id: str | None = None
    credential_generation: int | None = Field(default=None, ge=1)
    grant_id: str | None = None
    grant_version: int | None = Field(default=None, ge=1)
    source_slot_digest: str | None = None
    registration_version: int | None = Field(default=None, ge=1)

    def canonical_bytes(self) -> bytes:
        """Return deterministic secret-free bytes used by permits and audit。"""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        """Return the SHA-256 binding consumed by the final egress permit。"""
        return f"sha256:{hashlib.sha256(self.canonical_bytes()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class OAuthPreparedRefreshRequest:
    """One immutable refresh request shared by permit binding and HTTP send。"""

    endpoint: str
    form: tuple[tuple[str, SecretStr], ...]
    basic_auth: tuple[SecretStr, SecretStr] | None
    binding: OAuthCredentialWireBinding

    def form_data(self) -> dict[str, str]:
        """Materialize the already-bound form only at the outbound boundary。"""
        return {key: value.get_secret_value() for key, value in self.form}

    def basic_auth_values(self) -> tuple[str, str] | None:
        """Materialize Basic credentials only at the outbound boundary。"""
        if self.basic_auth is None:
            return None
        return tuple(value.get_secret_value() for value in self.basic_auth)


class OAuthTransactionStatus(StrEnum):
    """Persisted authorization transaction FSM states。"""

    PENDING = "pending"
    CONSUMING = "consuming"
    CONSUMED_SUCCESS = "consumed_success"
    CONSUMED_ERROR = "consumed_error"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class OAuthGrantStatus(StrEnum):
    """Persisted OAuth grant FSM states。"""

    ACTIVE = "active"
    REFRESHING = "refreshing"
    FROZEN = "frozen"
    EXPIRED = "expired"
    REVOKING = "revoking"
    TOMBSTONED = "tombstoned"


class OAuthKeyRotationPhase(StrEnum):
    """Crash-resumable OAuth key rotation phases."""

    STABLE = "stable"
    PREPARE = "prepare"
    DUAL_READ_NEW_WRITE = "dual-read-new-write"
    STATE_DRAIN = "state-drain"
    WORKER_CONVERGE = "worker-converge"


class OAuthKeyRotationRecord(BaseModel):
    """Non-secret persisted key rotation status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: OAuthKeyRotationPhase
    current_kid: str
    retiring_kid: str | None = None
    epoch: int = Field(ge=1)
    started_at: float | None = None
    state_drain_after: float | None = None


class OAuthAuthorizationSlotKey(BaseModel):
    """Verified principal/installation facts used to locate one active grant pointer。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    human_principal_id: str = Field(min_length=1, max_length=256)
    execution_agent_id: str = Field(min_length=1, max_length=256)
    delegation_digest: str | None = Field(default=None, max_length=256)
    installation_id: str = Field(min_length=1, max_length=256)
    installation_generation: int = Field(ge=1)
    auth_profile_id: str = Field(min_length=1, max_length=256)

    def canonical_bytes(self) -> bytes:
        """Return unambiguous length-prefixed canonical bytes。"""
        parts = (
            self.human_principal_id,
            self.execution_agent_id,
            self.delegation_digest or "",
            self.installation_id,
            str(self.installation_generation),
            self.auth_profile_id,
        )
        encoded = [part.encode("utf-8") for part in parts]
        return b"".join(len(part).to_bytes(4, "big") + part for part in encoded)


class OAuthTransactionPayload(BaseModel):
    """Encrypted authorization transaction payload; raw state is intentionally absent。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pkce_verifier: SecretStr
    requested_scopes: frozenset[str]
    issuer: str
    resource: str
    redirect_uri: str
    redirect_route: str
    response_mode: str
    profile_generation: int = Field(ge=1)
    profile_fingerprint: str = Field(min_length=1)
    auth_profile_id: str = Field(min_length=1, max_length=256)
    human_principal_id: str = Field(min_length=1, max_length=256)
    execution_agent_id: str = Field(min_length=1, max_length=256)
    delegation_digest: str | None = Field(default=None, max_length=256)
    installation_id: str = Field(min_length=1, max_length=256)
    installation_generation: int = Field(ge=1)
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str | None = None
    code_challenge_methods_supported: tuple[str, ...]
    response_modes_supported: tuple[str, ...]
    authorization_response_iss_parameter_supported: bool
    metadata_security_fingerprint: str = Field(min_length=1)
    client_id: str | None = None
    client_secret: SecretStr | None = None
    client_secret_expires_at: float | None = None
    token_endpoint_auth_method: str = "none"
    registration_version: int = Field(default=1, ge=1)


class OAuthGrantBinding(BaseModel):
    """Security-relevant full grant tuple excluding the stable slot fields。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    issuer: str
    resource: str
    client_id: str
    grant_family: str = "authorization_code"
    granted_scopes: frozenset[str]
    profile_generation: int = Field(ge=1)
    profile_fingerprint: str = Field(min_length=1)
    registration_version: int = Field(ge=1)
    metadata_security_fingerprint: str = Field(min_length=1)
    revocation_endpoint: str | None = None
    client_secret: SecretStr | None = None
    client_secret_expires_at: float | None = None
    token_endpoint_auth_method: str = "none"


class OAuthTokenSet(BaseModel):
    """Encrypted token material; SecretStr prevents accidental serialization/logging。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    access_token: SecretStr
    refresh_token: SecretStr | None = None
    token_type: str
    expires_at: float
    granted_scopes: frozenset[str]


class OAuthTransactionRecord(BaseModel):
    """Decrypted repository projection, never an API response model。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transaction_id: str
    operation_id: str
    slot_digest: str
    slot_version: int
    status: OAuthTransactionStatus
    expires_at: float
    payload: OAuthTransactionPayload


class OAuthGrantRecord(BaseModel):
    """Decrypted active grant projection used only inside ConnectorAuthService。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: str
    slot_digest: str
    grant_version: int
    status: OAuthGrantStatus
    binding: OAuthGrantBinding
    tokens: OAuthTokenSet


class OAuthRefreshLease(BaseModel):
    """Opaque refresh lease/fence returned to the network winner。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: str
    expected_grant_version: int
    owner: str
    fence: str
    expires_at: float


class OAuthSlotSnapshot(BaseModel):
    """Non-secret persisted projection used to derive the API status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transaction_status: OAuthTransactionStatus | None = None
    operation_id: str | None = None
    transaction_expires_at: float | None = None
    grant_status: OAuthGrantStatus | None = None
    access_expires_at: float | None = None
    has_refresh: bool = False
