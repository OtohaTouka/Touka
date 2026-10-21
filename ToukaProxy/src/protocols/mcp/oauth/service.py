# ruff: noqa: ANN001, ANN002, ANN003, ANN201, D101, D102, TC002, TC003, TRY301
"""P8-A authorization-code orchestration; routers and manager depend on this seam。"""

from __future__ import annotations

import asyncio
import hashlib
import random
import secrets
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from inspect import isawaitable
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from src.config import (
    OAUTH_ACCESS_REFRESH_LEEWAY_SECONDS,
    OAUTH_DISCONNECT_REVOCATION_TIMEOUT_SECONDS,
    OAUTH_MAX_CLOCK_ROLLBACK_SECONDS,
    OAUTH_REFRESH_LEASE_SECONDS,
    OAUTH_RETRY_BASE_MS,
    OAUTH_RETRY_MAX_ATTEMPTS,
    OAUTH_RETRY_MAX_MS,
    OAUTH_STATE_TTL_SECONDS,
)
from src.protocols.mcp.oauth.models import (
    OAuthAuthorizationSlotKey,
    OAuthCredentialWireBinding,
    OAuthGrantBinding,
    OAuthGrantRecord,
    OAuthGrantStatus,
    OAuthInvalidGrantError,
    OAuthPreparedRefreshRequest,
    OAuthTokenSet,
    OAuthTransactionConflictError,
    OAuthTransactionPayload,
    OAuthTransactionStatus,
)
from src.protocols.mcp.oauth.profile import (
    IPNetwork,
    OAuthProfile,
    oauth_profile_fingerprint,
    validate_returned_scopes,
)
from src.protocols.mcp.oauth.protocol import (
    OAuthCallbackPayload,
    build_authorization_url,
    validate_callback_issuer,
)
from src.protocols.mcp.oauth.registration import OAuthClientBinding
from src.utils.time_utils import get_current_epoch


class OAuthStateInvalidError(ValueError):
    """Callback state is unknown, expired, consumed or route-mismatched。"""


class OAuthIssuerMismatchError(ValueError):
    """RFC 9207/MCP issuer binding failed after state consumption。"""


class OAuthAuthorizationServerError(ValueError):
    """Authorization server returned an error callback。"""


class OAuthStaleAuthorizationError(ValueError):
    """Token response lost its slot/profile/generation commit fence。"""


class OAuthMetadataError(ValueError):
    """Security metadata lacks mandatory P8-A properties。"""


class OAuthClockRollbackError(ValueError):
    """Durable wall-clock rollback exceeded the configured fail-closed bound。"""


class OAuthAuthorizationRequiredError(ValueError):
    """No exact usable interactive grant exists for this authorization slot。"""


class OAuthAuthorizationMetadata(BaseModel):
    """Presence-aware security projection used by authorization orchestration。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    revocation_endpoint: str | None = None
    code_challenge_methods_supported: tuple[str, ...]
    response_modes_supported: tuple[str, ...]
    authorization_response_iss_parameter_supported: bool
    security_fingerprint: str = Field(min_length=1)


class OAuthStartResult(BaseModel):
    """Browser-safe start result; contains no verifier/token。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: str
    authorization_url: str | None
    expires_at: float
    reused: bool = False


class OAuthCallbackResult(BaseModel):
    """Browser-safe callback outcome。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: str
    status: str


class OAuthStatusResult(BaseModel):
    """Browser-safe persisted-state-derived status projection。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    operation_id: str | None = None
    expires_at: float | None = None


class OAuthDisconnectResult(BaseModel):
    """Browser-safe exact-slot disconnect result。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str


class OAuthBearerCredential(BaseModel):
    """Short-lived in-memory bearer projection with an exact repository fence。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: str
    grant_version: int
    authorization_header: SecretStr
    allowed_private_cidrs: tuple[str, ...] = ()


class OAuthTransactionRepository(Protocol):
    async def find_pending_for_slot(self, slot: OAuthAuthorizationSlotKey, **kwargs): ...
    async def create_transaction(self, **kwargs): ...
    async def cancel_transaction(self, transaction_id: str) -> bool: ...
    async def consume_transaction(self, raw_state: str, **kwargs): ...
    async def mark_transaction_error(self, transaction_id: str) -> None: ...
    async def commit_authorization(self, **kwargs) -> bool: ...
    async def get_slot_snapshot(self, slot: OAuthAuthorizationSlotKey, **kwargs): ...
    async def begin_disconnect(self, slot: OAuthAuthorizationSlotKey, **kwargs): ...
    async def complete_disconnect(self, grant_id: str, **kwargs) -> bool: ...
    async def get_active_grant(self, slot: OAuthAuthorizationSlotKey, **kwargs): ...
    async def assert_bearer_usable(self, slot: OAuthAuthorizationSlotKey, *args, **kwargs): ...
    async def acquire_refresh_lease(self, *args, **kwargs): ...
    async def commit_refresh(self, *args, **kwargs): ...
    async def release_refresh_lease(self, *args, **kwargs): ...
    async def tombstone_invalid_grant(self, *args, **kwargs): ...
    async def freeze_grant(self, *args, **kwargs): ...


class OAuthOutboundClient(Protocol):
    async def exchange_authorization_code(self, **kwargs) -> tuple[OAuthTokenSet, str | None]: ...
    async def revoke_token(self, **kwargs) -> None: ...
    async def refresh_prepared_access_token(
        self,
        request: OAuthPreparedRefreshRequest,
        **kwargs,
    ) -> tuple[OAuthTokenSet, str | None]: ...


class ConnectorAuthService:
    """Single FSM owner for interactive MCP authorization-code grants。"""

    def __init__(
        self,
        *,
        repository: OAuthTransactionRepository,
        outbound: OAuthOutboundClient,
        epoch_clock: Callable[[], float] = get_current_epoch,
        random_urlsafe: Callable[[int], str] = secrets.token_urlsafe,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_jitter: Callable[[], float] = random.random,
    ) -> None:
        self._repository = repository
        self._outbound = outbound
        self._epoch_clock = epoch_clock
        self._random_urlsafe = random_urlsafe
        self._sleeper = sleeper
        self._retry_jitter = retry_jitter

    async def _validated_now(self) -> float:
        now = self._epoch_clock()
        observer = getattr(self._repository, "observe_clock", None)
        if observer is not None and not await observer(
            now=now,
            max_rollback_seconds=OAUTH_MAX_CLOCK_ROLLBACK_SECONDS,
        ):
            raise OAuthClockRollbackError()
        return now

    @staticmethod
    def _grant_matches_current_security_state(
        grant,
        *,
        profile: OAuthProfile,
        metadata: OAuthAuthorizationMetadata,
        resource: str,
    ) -> bool:
        binding = grant.binding
        return (
            profile.enabled
            and binding.resource == resource
            and binding.issuer == metadata.issuer
            and (profile.client_id is None or binding.client_id == profile.client_id)
            and binding.profile_generation == profile.generation
            and binding.profile_fingerprint == oauth_profile_fingerprint(profile)
            and binding.metadata_security_fingerprint == metadata.security_fingerprint
            and grant.tokens.granted_scopes == binding.granted_scopes
        )

    async def _fenced_credential(
        self,
        *,
        slot: OAuthAuthorizationSlotKey,
        grant,
        now: float,
        authorization_fence: Callable[[], bool | Awaitable[bool]] | None = None,
    ) -> OAuthBearerCredential:
        await self._assert_dynamic_authority(
            grant=grant,
            now=now,
            authorization_fence=authorization_fence,
        )
        if now >= grant.tokens.expires_at or not await self._repository.assert_bearer_usable(
            slot,
            grant.grant_id,
            grant.grant_version,
            now=now,
        ):
            raise OAuthAuthorizationRequiredError()
        return OAuthBearerCredential(
            grant_id=grant.grant_id,
            grant_version=grant.grant_version,
            authorization_header=SecretStr(
                f"Bearer {grant.tokens.access_token.get_secret_value()}"
            ),
        )

    async def _assert_dynamic_authority(
        self,
        *,
        grant,
        now: float,
        authorization_fence: Callable[[], bool | Awaitable[bool]] | None,
    ) -> None:
        if authorization_fence is None:
            return
        allowed = authorization_fence()
        if isawaitable(allowed):
            allowed = await allowed
        if allowed:
            return
        await self._repository.freeze_grant(
            grant.grant_id,
            grant.grant_version,
            now=now,
        )
        raise OAuthAuthorizationRequiredError()

    @staticmethod
    def _prepare_refresh_request(
        *,
        grant: OAuthGrantRecord,
        metadata: OAuthAuthorizationMetadata,
        profile: OAuthProfile,
        slot: OAuthAuthorizationSlotKey,
        refresh_token: SecretStr,
    ) -> OAuthPreparedRefreshRequest:
        """Freeze one exact refresh form/header shape before permit issuance。"""
        method = grant.binding.token_endpoint_auth_method
        secret = grant.binding.client_secret
        form = {
            "grant_type": SecretStr("refresh_token"),
            "refresh_token": refresh_token,
            "resource": SecretStr(grant.binding.resource),
        }
        basic_auth: tuple[SecretStr, SecretStr] | None = None
        if method == "none" and secret is None:
            form["client_id"] = SecretStr(grant.binding.client_id)
        elif method == "client_secret_basic" and secret is not None:
            basic_auth = (SecretStr(grant.binding.client_id), secret)
        elif method == "client_secret_post" and secret is not None:
            form["client_id"] = SecretStr(grant.binding.client_id)
            form["client_secret"] = secret
        else:
            raise OAuthAuthorizationRequiredError()

        binding = OAuthCredentialWireBinding(
            grant_type="refresh_token",
            token_endpoint=metadata.token_endpoint,
            resource=grant.binding.resource,
            scopes=tuple(sorted(grant.binding.granted_scopes)),
            client_auth_method=method,
            form_fields=tuple(sorted(form)),
            profile_id=profile.profile_id,
            profile_generation=profile.generation,
            profile_fingerprint=oauth_profile_fingerprint(profile),
            metadata_security_fingerprint=metadata.security_fingerprint,
            grant_id=grant.grant_id,
            grant_version=grant.grant_version,
            source_slot_digest=(f"sha256:{hashlib.sha256(slot.canonical_bytes()).hexdigest()}"),
            registration_version=grant.binding.registration_version,
        )
        return OAuthPreparedRefreshRequest(
            endpoint=metadata.token_endpoint,
            form=tuple(sorted(form.items())),
            basic_auth=basic_auth,
            binding=binding,
        )

    async def get_bearer_credential(
        self,
        *,
        slot: OAuthAuthorizationSlotKey,
        profile: OAuthProfile,
        metadata: OAuthAuthorizationMetadata,
        resource: str,
        client_binding: OAuthClientBinding | None = None,
        private_network_policy: Callable[[str], tuple[IPNetwork, ...]] | None = None,
        authorization_fence: Callable[[], bool | Awaitable[bool]] | None = None,
        refresh_egress_guard: Callable[
            [OAuthCredentialWireBinding], AbstractAsyncContextManager[None]
        ]
        | None = None,
    ) -> OAuthBearerCredential:
        """Resolve, refresh, and fence one exact bearer immediately before egress。"""
        now = await self._validated_now()
        grant = await self._repository.get_active_grant(slot, now=now)
        if grant is None:
            raise OAuthAuthorizationRequiredError()
        await self._assert_dynamic_authority(
            grant=grant,
            now=now,
            authorization_fence=authorization_fence,
        )
        client_binding_matches = client_binding is None or (
            client_binding.client_id == grant.binding.client_id
            and client_binding.registration_version == grant.binding.registration_version
            and client_binding.token_endpoint_auth_method
            == grant.binding.token_endpoint_auth_method
            and client_binding.client_secret == grant.binding.client_secret
            and (
                client_binding.client_secret_expires_at is None
                or now < client_binding.client_secret_expires_at
            )
        )
        if not client_binding_matches or not self._grant_matches_current_security_state(
            grant,
            profile=profile,
            metadata=metadata,
            resource=resource,
        ):
            await self._repository.freeze_grant(
                grant.grant_id,
                grant.grant_version,
                now=now,
            )
            raise OAuthAuthorizationRequiredError()
        if now < grant.tokens.expires_at - OAUTH_ACCESS_REFRESH_LEEWAY_SECONDS:
            return await self._fenced_credential(
                slot=slot,
                grant=grant,
                now=now,
                authorization_fence=authorization_fence,
            )
        refresh_token = grant.tokens.refresh_token
        if refresh_token is None:
            return await self._fenced_credential(
                slot=slot,
                grant=grant,
                now=now,
                authorization_fence=authorization_fence,
            )
        lease = await self._repository.acquire_refresh_lease(
            grant.grant_id,
            grant.grant_version,
            self._random_urlsafe(16),
            now=now,
            lease_seconds=OAUTH_REFRESH_LEASE_SECONDS,
        )
        if lease is None:
            return await self._fenced_credential(
                slot=slot,
                grant=grant,
                now=now,
                authorization_fence=authorization_fence,
            )
        try:
            await self._validated_now()
            await self._assert_dynamic_authority(
                grant=grant,
                now=self._epoch_clock(),
                authorization_fence=authorization_fence,
            )
            prepared_refresh = self._prepare_refresh_request(
                grant=grant,
                metadata=metadata,
                profile=profile,
                slot=slot,
                refresh_token=refresh_token,
            )
            if refresh_egress_guard is None:
                refreshed, returned_scope = await self._outbound.refresh_prepared_access_token(
                    prepared_refresh,
                    private_network_policy=private_network_policy,
                )
            else:
                async with refresh_egress_guard(prepared_refresh.binding):
                    refreshed, returned_scope = await self._outbound.refresh_prepared_access_token(
                        prepared_refresh,
                        private_network_policy=private_network_policy,
                    )
            granted_scopes = validate_returned_scopes(
                returned_scope,
                grant.binding.granted_scopes,
                refresh=True,
            )
            refreshed = refreshed.model_copy(
                update={
                    "refresh_token": refreshed.refresh_token or refresh_token,
                    "granted_scopes": granted_scopes,
                }
            )
            refreshed_binding = grant.binding.model_copy(update={"granted_scopes": granted_scopes})
            commit_now = await self._validated_now()
            if not await self._repository.commit_refresh(
                lease,
                refreshed,
                binding=refreshed_binding,
                now=commit_now,
            ):
                raise OAuthStaleAuthorizationError()
        except OAuthInvalidGrantError as exc:
            await self._repository.tombstone_invalid_grant(
                lease,
                now=self._epoch_clock(),
            )
            raise OAuthAuthorizationRequiredError() from exc
        except Exception:
            await self._repository.release_refresh_lease(
                lease,
                now=self._epoch_clock(),
            )
            raise
        rotated_now = await self._validated_now()
        rotated = await self._repository.get_active_grant(slot, now=rotated_now)
        if rotated is None:
            raise OAuthAuthorizationRequiredError()
        return await self._fenced_credential(
            slot=slot,
            grant=rotated,
            now=rotated_now,
            authorization_fence=authorization_fence,
        )

    @staticmethod
    def _validate_metadata(profile: OAuthProfile, metadata: OAuthAuthorizationMetadata) -> None:
        if (
            metadata.issuer not in profile.trusted_issuers
            or "S256" not in metadata.code_challenge_methods_supported
            or profile.callback_binding not in metadata.response_modes_supported
        ):
            raise OAuthMetadataError()

    @staticmethod
    def _pending_matches_current_binding(
        pending,
        *,
        profile: OAuthProfile,
        metadata: OAuthAuthorizationMetadata,
        resource: str,
        operation_scopes: frozenset[str],
        redirect_route: str,
        redirect_uri: str,
        client_binding: OAuthClientBinding,
    ) -> bool:
        payload = getattr(pending, "payload", None)
        if payload is None:
            return True
        return (
            payload.requested_scopes == operation_scopes
            and payload.issuer == metadata.issuer
            and payload.resource == resource
            and payload.redirect_uri == redirect_uri
            and payload.redirect_route == redirect_route
            and payload.response_mode == profile.callback_binding
            and payload.profile_generation == profile.generation
            and payload.profile_fingerprint == oauth_profile_fingerprint(profile)
            and payload.metadata_security_fingerprint == metadata.security_fingerprint
            and payload.client_id == client_binding.client_id
            and payload.client_secret == client_binding.client_secret
            and payload.client_secret_expires_at == client_binding.client_secret_expires_at
            and payload.token_endpoint_auth_method == client_binding.token_endpoint_auth_method
            and payload.registration_version == client_binding.registration_version
        )

    async def start(
        self,
        *,
        slot: OAuthAuthorizationSlotKey,
        profile: OAuthProfile,
        metadata: OAuthAuthorizationMetadata,
        resource: str,
        operation_scopes: frozenset[str],
        redirect_route: str,
        redirect_uri: str,
        client_binding: OAuthClientBinding | None = None,
    ) -> OAuthStartResult:
        """Persist transaction before returning its one-time authorization URL。"""
        if not profile.enabled:
            raise OAuthMetadataError()
        self._validate_metadata(profile, metadata)
        now = await self._validated_now()
        if client_binding is None:
            if profile.client_id is None:
                raise OAuthMetadataError()
            client_binding = OAuthClientBinding(
                client_id=profile.client_id,
                token_endpoint_auth_method=profile.token_endpoint_auth_method,
                registration_version=1,
            )
        pending = await self._repository.find_pending_for_slot(slot, now=now)
        if pending is not None:
            if self._pending_matches_current_binding(
                pending,
                profile=profile,
                metadata=metadata,
                resource=resource,
                operation_scopes=operation_scopes,
                redirect_route=redirect_route,
                redirect_uri=redirect_uri,
                client_binding=client_binding,
            ):
                return OAuthStartResult(
                    operation_id=pending.operation_id,
                    authorization_url=None,
                    expires_at=pending.expires_at,
                    reused=True,
                )
            if not await self._repository.cancel_transaction(pending.transaction_id):
                raise OAuthStaleAuthorizationError()
        state = self._random_urlsafe(32)
        verifier = self._random_urlsafe(48)
        operation_id = self._random_urlsafe(16)
        expires_at = now + OAUTH_STATE_TTL_SECONDS
        payload = OAuthTransactionPayload(
            pkce_verifier=verifier,
            requested_scopes=operation_scopes,
            issuer=metadata.issuer,
            resource=resource,
            redirect_uri=redirect_uri,
            redirect_route=redirect_route,
            response_mode=profile.callback_binding,
            profile_generation=profile.generation,
            profile_fingerprint=oauth_profile_fingerprint(profile),
            auth_profile_id=profile.profile_id,
            human_principal_id=slot.human_principal_id,
            execution_agent_id=slot.execution_agent_id,
            delegation_digest=slot.delegation_digest,
            installation_id=slot.installation_id,
            installation_generation=slot.installation_generation,
            authorization_endpoint=metadata.authorization_endpoint,
            token_endpoint=metadata.token_endpoint,
            revocation_endpoint=metadata.revocation_endpoint,
            code_challenge_methods_supported=metadata.code_challenge_methods_supported,
            response_modes_supported=metadata.response_modes_supported,
            authorization_response_iss_parameter_supported=(
                metadata.authorization_response_iss_parameter_supported
            ),
            metadata_security_fingerprint=metadata.security_fingerprint,
            client_id=client_binding.client_id,
            client_secret=client_binding.client_secret,
            client_secret_expires_at=client_binding.client_secret_expires_at,
            token_endpoint_auth_method=client_binding.token_endpoint_auth_method,
            registration_version=client_binding.registration_version,
        )
        try:
            await self._repository.create_transaction(
                slot=slot,
                operation_id=operation_id,
                raw_state=state,
                payload=payload,
                now=now,
                expires_at=expires_at,
            )
        except OAuthTransactionConflictError as exc:
            winner = await self._repository.find_pending_for_slot(slot, now=now)
            if winner is None:
                raise OAuthStaleAuthorizationError() from exc
            return OAuthStartResult(
                operation_id=winner.operation_id,
                authorization_url=None,
                expires_at=winner.expires_at,
                reused=True,
            )
        return OAuthStartResult(
            operation_id=operation_id,
            authorization_url=build_authorization_url(
                authorization_endpoint=metadata.authorization_endpoint,
                client_id=client_binding.client_id,
                redirect_uri=redirect_uri,
                resource=resource,
                requested_scopes=operation_scopes,
                state=state,
                code_verifier=verifier,
                response_mode=profile.callback_binding,
            ),
            expires_at=expires_at,
        )

    async def complete_callback(
        self,
        *,
        callback: OAuthCallbackPayload,
        redirect_route: str,
        profile: OAuthProfile,
        metadata: OAuthAuthorizationMetadata,
        slot: OAuthAuthorizationSlotKey,
        authorization_fence: Callable[[], bool | Awaitable[bool]] | None = None,
        client_binding: OAuthClientBinding | None = None,
        private_network_policy: Callable[[str], tuple[IPNetwork, ...]] | None = None,
    ) -> OAuthCallbackResult:
        """Consume state before semantic validation or token I/O, then CAS commit。"""
        callback_now = await self._validated_now()
        transaction = await self._repository.consume_transaction(
            callback.state,
            now=callback_now,
            redirect_route=redirect_route,
            expected_slot=slot,
        )
        if transaction is None:
            raise OAuthStateInvalidError()
        try:
            if (
                transaction.payload.auth_profile_id != profile.profile_id
                or transaction.payload.profile_generation != profile.generation
                or transaction.payload.profile_fingerprint != oauth_profile_fingerprint(profile)
                or transaction.payload.metadata_security_fingerprint
                != metadata.security_fingerprint
                or transaction.payload.token_endpoint != metadata.token_endpoint
            ):
                raise OAuthStaleAuthorizationError()
            if authorization_fence is not None:
                allowed = authorization_fence()
                if isawaitable(allowed):
                    allowed = await allowed
                if not allowed:
                    raise OAuthStaleAuthorizationError()
            if callback.error is not None:
                raise OAuthAuthorizationServerError()
            if not validate_callback_issuer(
                expected=transaction.payload.issuer,
                received=callback.issuer,
                issuer_parameter_supported=(
                    metadata.authorization_response_iss_parameter_supported
                ),
                trusted_issuer_count=len(profile.trusted_issuers),
            ):
                raise OAuthIssuerMismatchError()
            if callback.code is None:
                raise OAuthAuthorizationServerError()
            effective_client_id = transaction.payload.client_id or profile.client_id
            if effective_client_id is None:
                raise OAuthStaleAuthorizationError()
            if client_binding is not None and (
                client_binding.client_id != effective_client_id
                or client_binding.registration_version != transaction.payload.registration_version
                or client_binding.token_endpoint_auth_method
                != transaction.payload.token_endpoint_auth_method
            ):
                raise OAuthStaleAuthorizationError()
            exchange_now = await self._validated_now()
            if (
                transaction.payload.client_secret_expires_at is not None
                and exchange_now >= transaction.payload.client_secret_expires_at
            ):
                raise OAuthStaleAuthorizationError()
            tokens, returned_scope = await self._outbound.exchange_authorization_code(
                token_endpoint=metadata.token_endpoint,
                code=callback.code,
                code_verifier=transaction.payload.pkce_verifier.get_secret_value(),
                client_id=effective_client_id,
                redirect_uri=transaction.payload.redirect_uri,
                resource=transaction.payload.resource,
                client_secret=(
                    transaction.payload.client_secret.get_secret_value()
                    if transaction.payload.client_secret is not None
                    else None
                ),
                token_endpoint_auth_method=(transaction.payload.token_endpoint_auth_method),
                private_network_policy=private_network_policy,
            )
            granted_scopes = validate_returned_scopes(
                returned_scope,
                transaction.payload.requested_scopes,
                refresh=False,
            )
            tokens = tokens.model_copy(update={"granted_scopes": granted_scopes})
            binding = OAuthGrantBinding(
                issuer=transaction.payload.issuer,
                resource=transaction.payload.resource,
                client_id=effective_client_id,
                granted_scopes=granted_scopes,
                profile_generation=profile.generation,
                profile_fingerprint=transaction.payload.profile_fingerprint,
                registration_version=transaction.payload.registration_version,
                metadata_security_fingerprint=metadata.security_fingerprint,
                revocation_endpoint=metadata.revocation_endpoint,
                client_secret=transaction.payload.client_secret,
                client_secret_expires_at=(transaction.payload.client_secret_expires_at),
                token_endpoint_auth_method=(transaction.payload.token_endpoint_auth_method),
            )
            commit_now = await self._validated_now()
            committed = await self._repository.commit_authorization(
                transaction_id=transaction.transaction_id,
                expected_slot_version=transaction.slot_version,
                binding=binding,
                tokens=tokens,
                now=commit_now,
            )
            if not committed:
                raise OAuthStaleAuthorizationError()
        except Exception:
            await self._repository.mark_transaction_error(transaction.transaction_id)
            raise
        return OAuthCallbackResult(
            operation_id=transaction.operation_id,
            status="connected",
        )

    async def status(
        self,
        *,
        slot: OAuthAuthorizationSlotKey,
        enabled: bool,
    ) -> OAuthStatusResult:
        """Map the exact durable snapshot to the API status vocabulary。"""
        if not enabled:
            return OAuthStatusResult(status="disabled")
        now = await self._validated_now()
        snapshot = await self._repository.get_slot_snapshot(
            slot,
            now=now,
        )
        if snapshot.transaction_status == OAuthTransactionStatus.PENDING:
            return OAuthStatusResult(
                status="awaiting_user",
                operation_id=snapshot.operation_id,
                expires_at=snapshot.transaction_expires_at,
            )
        if snapshot.transaction_status == OAuthTransactionStatus.CONSUMING:
            return OAuthStatusResult(
                status="exchanging",
                operation_id=snapshot.operation_id,
                expires_at=snapshot.transaction_expires_at,
            )
        statuses = {
            OAuthGrantStatus.ACTIVE: "connected",
            OAuthGrantStatus.REFRESHING: "refreshing",
            OAuthGrantStatus.REVOKING: "revoking",
            OAuthGrantStatus.FROZEN: "frozen",
            OAuthGrantStatus.EXPIRED: "expired",
        }
        status = statuses.get(snapshot.grant_status, "auth_required")
        return OAuthStatusResult(status=status, expires_at=snapshot.access_expires_at)

    async def disconnect(
        self,
        *,
        slot: OAuthAuthorizationSlotKey,
        private_network_policy: Callable[[str], tuple[IPNetwork, ...]] | None = None,
    ) -> OAuthDisconnectResult:
        """Revoke bounded upstream state, then tombstone only the exact local grant。"""
        now = await self._validated_now()
        grant = await self._repository.begin_disconnect(slot, now=now)
        if grant is None:
            return OAuthDisconnectResult(status="auth_required")
        try:
            if grant.binding.revocation_endpoint is not None:
                await self._validated_now()
                token = grant.tokens.refresh_token or grant.tokens.access_token
                async with asyncio.timeout(OAUTH_DISCONNECT_REVOCATION_TIMEOUT_SECONDS):
                    for attempt in range(OAUTH_RETRY_MAX_ATTEMPTS):
                        try:
                            await self._outbound.revoke_token(
                                revocation_endpoint=(grant.binding.revocation_endpoint),
                                token=token.get_secret_value(),
                                client_id=grant.binding.client_id,
                                private_network_policy=private_network_policy,
                            )
                            break
                        except Exception:
                            if attempt + 1 >= OAUTH_RETRY_MAX_ATTEMPTS:
                                raise
                            jitter = max(0.0, min(1.0, self._retry_jitter()))
                            delay_ms = min(
                                OAUTH_RETRY_MAX_MS,
                                OAUTH_RETRY_BASE_MS * (2**attempt),
                            )
                            await self._sleeper(delay_ms * (0.5 + 0.5 * jitter) / 1000)
        finally:
            complete_now = await self._validated_now()
            completed = await self._repository.complete_disconnect(
                grant.grant_id,
                expected_grant_version=grant.grant_version,
                now=complete_now,
            )
        if not completed:
            raise OAuthStaleAuthorizationError()
        return OAuthDisconnectResult(status="auth_required")
