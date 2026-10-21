"""P8-A browser-safe MCP OAuth API contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class McpOAuthStatus(StrEnum):
    """Derived status projection; this is not a second persisted FSM."""

    DISABLED = "disabled"
    AUTH_REQUIRED = "auth_required"
    DISCOVERING = "discovering"
    SELECTION_REQUIRED = "selection_required"
    AWAITING_USER = "awaiting_user"
    EXCHANGING = "exchanging"
    CONNECTED = "connected"
    REFRESHING = "refreshing"
    REVOKING = "revoking"
    FROZEN = "frozen"
    EXPIRED = "expired"
    ERROR = "error"


class McpOAuthStartRequest(BaseModel):
    """Safe AS selection; scopes/resource remain server-side derived facts."""

    model_config = ConfigDict(extra="forbid")

    authorization_server: str | None = Field(default=None, max_length=2048)


class McpOAuthStartResponse(BaseModel):
    """One browser launch instruction with no verifier or token material."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    authorization_url: str | None
    expires_at: float
    reused: bool = False


class McpOAuthStatusResponse(BaseModel):
    """Persisted-state-derived UI projection."""

    model_config = ConfigDict(extra="forbid")

    status: McpOAuthStatus
    operation_id: str | None = None
    expires_at: float | None = None
    authorization_servers: list[str] = Field(default_factory=list)


class McpOAuthDisconnectResponse(BaseModel):
    """Exact-slot disconnect result."""

    model_config = ConfigDict(extra="forbid")

    status: McpOAuthStatus


class McpOAuthCallbackOutcome(BaseModel):
    """Internal browser-page projection; contains no OAuth wire secret."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: str
    status: McpOAuthStatus
    client_origin: str
