# ruff: noqa: TC002
"""Browser-safe P8-B workload OAuth control-plane contracts。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class WorkloadCredentialSetRequest(BaseModel):
    """One-shot secret input; neither field may appear in any response model。"""

    model_config = ConfigDict(extra="forbid")

    client_id: SecretStr = Field(json_schema_extra={"writeOnly": True})
    client_secret: SecretStr = Field(json_schema_extra={"writeOnly": True})


class WorkloadCredentialStatus(BaseModel):
    """Masked credential projection with no client identifier or secret。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    credential_id: str
    generation: int | None = None
    status: Literal["active", "missing", "tombstoned"]


class WorkloadMetadataStatus(BaseModel):
    """Minimal admin projection; endpoint, CIDRs, scopes, and fingerprints stay private。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    profile_generation: int = Field(ge=1)
    verified_at: float
    status: Literal["active"] = "active"


class WorkloadRecoveryItem(BaseModel):
    """Unified non-secret view of transition, revocation, and rotation blockers。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["transition", "revocation", "rotation"]
    recovery_id: str
    authority_id: str
    generation: int = Field(ge=1)
    status: Literal[
        "pending_json",
        "recovery_required",
        "draining",
        "revocation_pending",
        "prepare",
        "dual-read-new-write",
        "state-drain",
        "worker-converge",
    ]
    updated_at: float | None = None


class WorkloadTransitionReconcileRequest(BaseModel):
    """Explicit admin decision; authority facts remain server-derived。"""

    model_config = ConfigDict(extra="forbid")

    resolution: Literal["confirm_present", "confirm_absent"]


class WorkloadDelegationPutRequest(BaseModel):
    """Owner input; owner, agent, source slot, and generation are server-derived。"""

    model_config = ConfigDict(extra="forbid")

    installation_id: str = Field(min_length=1, max_length=256)
    installation_generation: int = Field(ge=1)
    raw_tool_name: str = Field(min_length=1, max_length=1024)
    required_scopes: frozenset[str] = Field(min_length=1)
    expires_at: float


class WorkloadDelegationView(BaseModel):
    """Owner-safe projection without P8-A grant or source-slot internals。"""

    model_config = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

    delegation_id: str
    execution_agent_id: str
    installation_id: str
    installation_generation: int = Field(ge=1)
    raw_tool_name: str
    required_scopes: frozenset[str]
    generation: int = Field(ge=1)
    status: Literal["active", "tombstoned"]
    issued_at: float
    expires_at: float


class WorkloadMutationResponse(BaseModel):
    """Stable status for bounded revocation and recovery-aware operations。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal[
        "active",
        "completed",
        "revocation_pending",
        "recovery_required",
    ]
    generation: int
