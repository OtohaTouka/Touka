# ruff: noqa: ANN001, ANN002, ANN003, ANN201, D101, D102, S105, TC002
"""P8-A immutable CIMD/DCR client registration domain models。"""

from __future__ import annotations

import secrets
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from src.config import OAUTH_DCR_LEASE_SECONDS, OAUTH_MAX_CLOCK_ROLLBACK_SECONDS
from src.protocols.mcp.oauth.profile import OAuthProfile
from src.utils.time_utils import get_current_epoch


class OAuthRegistrationError(ValueError):
    """Client metadata or DCR lifecycle cannot produce an authoritative binding。"""


class OAuthRegistrationClockRollbackError(OAuthRegistrationError):
    """DCR is unavailable after an excessive durable clock rollback。"""


class OAuthRegistrationKey(BaseModel):
    """Stable DCR slot excluding returned client material。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = Field(min_length=1, max_length=64)
    profile_generation: int = Field(ge=1)
    installation_id: str = Field(min_length=1, max_length=256)
    installation_generation: int = Field(ge=1)
    issuer: str

    def canonical_bytes(self) -> bytes:
        parts = (
            self.profile_id,
            str(self.profile_generation),
            self.installation_id,
            str(self.installation_generation),
            self.issuer,
        )
        encoded = (part.encode("utf-8") for part in parts)
        return b"".join(len(part).to_bytes(4, "big") + part for part in encoded)


class OAuthClientBinding(BaseModel):
    """Encrypted exact client binding from pre-registration, CIMD, or DCR。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_id: str = Field(min_length=1, max_length=2048)
    token_endpoint_auth_method: Literal["none", "client_secret_post"] = "none"
    client_secret: SecretStr | None = None
    client_secret_expires_at: float | None = None
    registration_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_secret_shape(self) -> OAuthClientBinding:
        """Bind public/confidential auth methods to an exact secret shape."""
        if self.token_endpoint_auth_method == "none":
            if self.client_secret is not None or self.client_secret_expires_at is not None:
                raise ValueError()
            return self
        if self.client_secret is None or not self.client_secret.get_secret_value():
            raise ValueError()
        return self


class OAuthRegistrationLease(BaseModel):
    """Cross-worker DCR lease with attempt fencing。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_digest: str
    expected_version: int = Field(ge=0)
    owner: str
    fence: str
    expires_at: float


class OAuthRegistrationRepository(Protocol):
    async def get_registration(self, key: OAuthRegistrationKey, **kwargs): ...
    async def acquire_registration_lease(self, *args, **kwargs): ...
    async def commit_registration(self, *args, **kwargs) -> bool: ...


class OAuthRegistrationOutbound(Protocol):
    async def fetch_client_metadata_document(self, **kwargs) -> OAuthClientBinding: ...
    async def register_client(self, **kwargs) -> OAuthClientBinding: ...


class OAuthClientRegistrationService:
    """Resolve pre-registration/CIMD first, then bounded named-issuer DCR fallback。"""

    def __init__(
        self,
        *,
        repository: OAuthRegistrationRepository,
        outbound: OAuthRegistrationOutbound,
        random_urlsafe=secrets.token_urlsafe,
        epoch_clock=get_current_epoch,
    ) -> None:
        self._repository = repository
        self._outbound = outbound
        self._random_urlsafe = random_urlsafe
        self._epoch_clock = epoch_clock

    async def _validated_now(self) -> float:
        now = self._epoch_clock()
        observer = getattr(self._repository, "observe_clock", None)
        if observer is not None and not await observer(
            now=now,
            max_rollback_seconds=OAUTH_MAX_CLOCK_ROLLBACK_SECONDS,
        ):
            raise OAuthRegistrationClockRollbackError()
        return now

    async def resolve(
        self,
        *,
        key: OAuthRegistrationKey,
        profile: OAuthProfile,
        registration_endpoint: str | None,
        redirect_uri: str,
    ) -> OAuthClientBinding:
        client_id = profile.client_id
        if client_id is not None:
            if urlsplit(client_id).scheme == "https":
                return await self._outbound.fetch_client_metadata_document(
                    client_id=client_id,
                    redirect_uri=redirect_uri,
                    token_endpoint_auth_method=profile.token_endpoint_auth_method,
                    private_network_policy=lambda url: profile.allowed_private_cidrs(
                        key.installation_id,
                        url,
                    ),
                )
            return OAuthClientBinding(
                client_id=client_id,
                token_endpoint_auth_method=profile.token_endpoint_auth_method,
                registration_version=1,
            )
        if not profile.dcr_enabled or key.issuer not in profile.dcr_trusted_issuers:
            raise OAuthRegistrationError()
        now = await self._validated_now()
        existing = await self._repository.get_registration(key, now=now)
        if existing is not None:
            return existing
        if registration_endpoint is None:
            raise OAuthRegistrationError()
        lease = await self._repository.acquire_registration_lease(
            key,
            self._random_urlsafe(16),
            now=now,
            lease_seconds=OAUTH_DCR_LEASE_SECONDS,
        )
        if lease is None:
            raise OAuthRegistrationError()
        await self._validated_now()
        binding = await self._outbound.register_client(
            registration_endpoint=registration_endpoint,
            redirect_uri=redirect_uri,
            token_endpoint_auth_method=profile.token_endpoint_auth_method,
            private_network_policy=lambda url: profile.allowed_private_cidrs(
                key.installation_id,
                url,
            ),
        )
        binding = binding.model_copy(update={"registration_version": lease.expected_version + 1})
        if (
            binding.client_secret_expires_at is not None
            and await self._validated_now() >= binding.client_secret_expires_at
        ):
            raise OAuthRegistrationError()
        commit_now = await self._validated_now()
        if not await self._repository.commit_registration(
            lease,
            binding,
            now=commit_now,
        ):
            raise OAuthRegistrationError()
        authoritative_now = await self._validated_now()
        authoritative = await self._repository.get_registration(
            key,
            now=authoritative_now,
        )
        if authoritative is None:
            raise OAuthRegistrationError()
        return authoritative
