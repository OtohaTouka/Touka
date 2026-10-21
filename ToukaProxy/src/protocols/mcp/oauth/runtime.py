# ruff: noqa: ANN003, D101, D102, TC002, TC003
"""P8-A runtime composition for API, callback, and MCP transport consumers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from inspect import isawaitable
from typing import Any, Protocol

from src.config import OAUTH_MAX_CLOCK_ROLLBACK_SECONDS
from src.models.mcp_oauth import (
    McpOAuthCallbackOutcome,
    McpOAuthDisconnectResponse,
    McpOAuthStartResponse,
    McpOAuthStatus,
    McpOAuthStatusResponse,
)
from src.protocols.mcp.canonical import McpRequestContext
from src.protocols.mcp.oauth.models import (
    OAuthAuthorizationSlotKey,
    OAuthCredentialWireBinding,
    OAuthTransactionRecord,
)
from src.protocols.mcp.oauth.outbound import validate_oauth_endpoint
from src.protocols.mcp.oauth.profile import (
    OAuthProfile,
    OAuthProfileRepository,
    oauth_profile_fingerprint,
)
from src.protocols.mcp.oauth.protocol import OAuthCallbackPayload, OAuthCallbackPolicy
from src.protocols.mcp.oauth.registration import (
    OAuthClientBinding,
    OAuthRegistrationKey,
)
from src.protocols.mcp.oauth.service import (
    ConnectorAuthService,
    OAuthAuthorizationMetadata,
    OAuthAuthorizationRequiredError,
    OAuthBearerCredential,
    OAuthClockRollbackError,
    OAuthMetadataError,
)
from src.protocols.mcp.oauth.store import OAuthSqliteStore
from src.protocols.mcp.oauth.workload.target import (
    McpOAuthTargetBindingError,
    canonical_mcp_transport_url,
)
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.utils.time_utils import get_current_epoch


class OAuthUnsupportedModeError(ValueError):
    """P8-A only supports interactive oauth_user installations。"""


class OAuthRuntimeBindingError(ValueError):
    """Runtime configuration, slot, or callback binding is unavailable。"""


class OAuthScopeDeniedError(PermissionError):
    """The profile has no exact rule or the grant lacks required scopes。"""


class OAuthMetadataProvider(Protocol):
    async def discover(self, **kwargs) -> OAuthAuthorizationMetadata: ...


class OAuthClientRegistrationProvider(Protocol):
    async def resolve(self, **kwargs) -> OAuthClientBinding: ...


class McpOAuthRuntimeService:
    """Translate verified request facts into the single ConnectorAuthService FSM。"""

    def __init__(
        self,
        *,
        registry: ProtocolRegistry,
        profiles: OAuthProfileRepository,
        store: OAuthSqliteStore,
        connector: ConnectorAuthService,
        metadata_provider: OAuthMetadataProvider,
        client_registration: OAuthClientRegistrationProvider | None = None,
        callback_policies: tuple[OAuthCallbackPolicy, ...],
        primary_callback_route: str,
        feature_enabled: bool,
        delegated_feature_enabled: bool = False,
        authorization_fence: Callable[
            [OAuthAuthorizationSlotKey],
            bool | Awaitable[bool],
        ],
        epoch_clock: Callable[[], float] = get_current_epoch,
    ) -> None:
        self._registry = registry
        self._profiles = profiles
        self._store = store
        self._connector = connector
        self._metadata_provider = metadata_provider
        self._client_registration = client_registration
        self._callback_policies = {policy.route: policy for policy in callback_policies}
        if len(self._callback_policies) != len(callback_policies):
            raise OAuthRuntimeBindingError()
        if primary_callback_route not in self._callback_policies:
            raise OAuthRuntimeBindingError()
        self._primary_callback_route = primary_callback_route
        self._feature_enabled = feature_enabled
        self._delegated_feature_enabled = delegated_feature_enabled
        self._epoch_clock = epoch_clock
        self._authorization_fence = authorization_fence
        self._delegation_disconnect_fence: Callable[
            [OAuthAuthorizationSlotKey, str], AbstractAsyncContextManager[None]
        ] | None = None

    def set_delegation_disconnect_fence(
        self,
        fence: Callable[
            [OAuthAuthorizationSlotKey, str],
            AbstractAsyncContextManager[None],
        ],
    ) -> None:
        """Install the single-process P8-B source-disconnect fence exactly once。"""
        if self._delegation_disconnect_fence is not None:
            raise OAuthRuntimeBindingError()
        self._delegation_disconnect_fence = fence

    def get_callback_policy(self, redirect_route: str) -> OAuthCallbackPolicy:
        return self._callback_policies[redirect_route]

    def _config(self, server_name: str) -> ProtocolServerConfig:
        config = self._registry.get_server(server_name)
        if config is None:
            raise OAuthRuntimeBindingError()
        if config.auth_mode != "oauth_user":
            raise OAuthUnsupportedModeError()
        if not config.enabled:
            raise OAuthAuthorizationRequiredError()
        if not config.url or not config.auth_profile_id:
            raise OAuthRuntimeBindingError()
        validate_oauth_endpoint(config.url)
        return config

    def _delegated_config(self, server_name: str) -> ProtocolServerConfig:
        """Accept only the explicit P8-B owner-delegation installation shape。"""
        config = self._registry.get_server(server_name)
        if (
            config is None
            or not config.enabled
            or config.auth_mode != "oauth_client_credentials"
            or config.workload_authority_kind != "owner_delegation"
            or config.workload_delegation_id is None
            or config.workload_source_installation_id is None
            or not config.url
            or not config.auth_profile_id
        ):
            raise OAuthUnsupportedModeError()
        validate_oauth_endpoint(config.url)
        return config

    def _delegated_source_config(
        self,
        server_name: str,
    ) -> tuple[ProtocolServerConfig, str, ProtocolServerConfig]:
        target = self._delegated_config(server_name)
        source_name = target.workload_source_installation_id
        if source_name is None or source_name == server_name:
            raise OAuthUnsupportedModeError()
        source = self._config(source_name)
        if source.auth_profile_id != target.auth_profile_id:
            raise OAuthUnsupportedModeError()
        try:
            source_url = canonical_mcp_transport_url(source.url or "")
            target_url = canonical_mcp_transport_url(target.url or "")
        except McpOAuthTargetBindingError as exc:
            raise OAuthUnsupportedModeError() from exc
        if source_url != target_url:
            raise OAuthUnsupportedModeError()
        return target, source_name, source

    async def _assert_authorized(self, slot: OAuthAuthorizationSlotKey) -> None:
        allowed = self._authorization_fence(slot)
        if isawaitable(allowed):
            allowed = await allowed
        if not self._feature_enabled or not allowed:
            raise OAuthAuthorizationRequiredError()

    async def _profile(self, config: ProtocolServerConfig) -> OAuthProfile:
        if config.auth_profile_id is None:
            raise OAuthRuntimeBindingError()
        profile = await self._profiles.get(config.auth_profile_id)
        if profile is None:
            raise OAuthRuntimeBindingError()
        return profile

    async def _validated_now(self) -> float:
        """Advance the durable clock fence before any OAuth network egress。"""
        now = self._epoch_clock()
        if not await self._store.observe_clock(
            now=now,
            max_rollback_seconds=OAUTH_MAX_CLOCK_ROLLBACK_SECONDS,
        ):
            raise OAuthClockRollbackError()
        return now

    @staticmethod
    def _slot(
        *,
        context: McpRequestContext,
        installation_generation: int,
        auth_profile_id: str,
    ) -> OAuthAuthorizationSlotKey:
        if context.human_principal_id is None:
            raise OAuthRuntimeBindingError()
        return OAuthAuthorizationSlotKey(
            human_principal_id=context.human_principal_id,
            execution_agent_id=context.identity.agent_id,
            installation_id=context.installation_id,
            installation_generation=installation_generation,
            auth_profile_id=auth_profile_id,
        )

    @staticmethod
    def _all_scopes(profile: OAuthProfile) -> frozenset[str]:
        return frozenset(scope for rule in profile.scope_rules for scope in rule.scopes)

    async def start_authorization(
        self,
        *,
        server_name: str,
        request_context: McpRequestContext,
        installation_generation: int,
        selected_issuer: str | None,
    ) -> McpOAuthStartResponse:
        config = self._config(server_name)
        if config.auth_profile_id is None:
            raise OAuthRuntimeBindingError()
        async with self._profiles.profile_guard(config.auth_profile_id):
            profile = await self._profile(config)
            slot = self._slot(
                context=request_context,
                installation_generation=installation_generation,
                auth_profile_id=profile.profile_id,
            )
            if not profile.enabled:
                raise OAuthAuthorizationRequiredError()
            await self._assert_authorized(slot)
            issuer = selected_issuer
            if issuer is None and len(profile.trusted_issuers) == 1:
                issuer = profile.trusted_issuers[0]
            if issuer is None or issuer not in profile.trusted_issuers:
                raise OAuthMetadataError()
            await self._validated_now()
            resource = str(config.url)
            metadata = await self._metadata_provider.discover(
                resource=resource,
                profile=profile,
                selected_issuer=issuer,
                installation_id=server_name,
            )
            policy = self._callback_policies[self._primary_callback_route]
            if policy.response_mode != profile.callback_binding:
                raise OAuthRuntimeBindingError()
            registration_key = OAuthRegistrationKey(
                profile_id=profile.profile_id,
                profile_generation=profile.generation,
                installation_id=server_name,
                installation_generation=installation_generation,
                issuer=issuer,
            )
            if self._client_registration is not None:
                client_binding = await self._client_registration.resolve(
                    key=registration_key,
                    profile=profile,
                    registration_endpoint=metadata.registration_endpoint,
                    redirect_uri=policy.redirect_uri,
                )
            elif profile.client_id is not None:
                client_binding = OAuthClientBinding(
                    client_id=profile.client_id,
                    token_endpoint_auth_method=profile.token_endpoint_auth_method,
                    registration_version=1,
                )
            else:
                raise OAuthRuntimeBindingError()
            result = await self._connector.start(
                slot=slot,
                profile=profile,
                metadata=metadata,
                resource=resource,
                operation_scopes=self._all_scopes(profile),
                redirect_route=policy.route,
                redirect_uri=policy.redirect_uri,
                client_binding=client_binding,
            )
            return McpOAuthStartResponse.model_validate(result.model_dump())

    async def get_status(
        self,
        *,
        server_name: str,
        request_context: McpRequestContext,
        installation_generation: int,
    ) -> McpOAuthStatusResponse:
        config = self._config(server_name)
        profile = await self._profile(config)
        result = await self._connector.status(
            slot=self._slot(
                context=request_context,
                installation_generation=installation_generation,
                auth_profile_id=profile.profile_id,
            ),
            enabled=self._feature_enabled and profile.enabled,
        )
        if result.status == "auth_required" and len(profile.trusted_issuers) > 1:
            return McpOAuthStatusResponse(
                status=McpOAuthStatus.SELECTION_REQUIRED,
                authorization_servers=list(profile.trusted_issuers),
            )
        return McpOAuthStatusResponse.model_validate(result.model_dump())

    async def disconnect(
        self,
        *,
        server_name: str,
        request_context: McpRequestContext,
        installation_generation: int,
    ) -> McpOAuthDisconnectResponse:
        config = self._config(server_name)
        if config.auth_profile_id is None:
            raise OAuthRuntimeBindingError()
        async with self._profiles.profile_guard(config.auth_profile_id):
            profile = await self._profile(config)
            slot = self._slot(
                context=request_context,
                installation_generation=installation_generation,
                auth_profile_id=profile.profile_id,
            )
            async def disconnect_grant() -> Any:
                return await self._connector.disconnect(
                    slot=slot,
                    private_network_policy=lambda url: profile.allowed_private_cidrs(
                        server_name,
                        url,
                    ),
                )

            if self._delegation_disconnect_fence is None:
                result = await disconnect_grant()
            else:
                async with self._delegation_disconnect_fence(
                    slot,
                    request_context.request_id,
                ):
                    result = await disconnect_grant()
            return McpOAuthDisconnectResponse.model_validate(result.model_dump())

    @asynccontextmanager
    async def bearer_credential_guard(
        self,
        *,
        server_name: str,
        request_context: McpRequestContext,
        installation_generation: int,
        wire_request: Any | None = None,
    ) -> AsyncIterator[OAuthBearerCredential]:
        """Hold the profile fence until the authenticated HTTP exchange completes。"""
        del wire_request
        config = self._config(server_name)
        if config.auth_profile_id is None:
            raise OAuthRuntimeBindingError()
        async with self._profiles.profile_guard(config.auth_profile_id):
            profile = await self._profile(config)
            slot = self._slot(
                context=request_context,
                installation_generation=installation_generation,
                auth_profile_id=profile.profile_id,
            )
            if not profile.enabled:
                raise OAuthAuthorizationRequiredError()
            await self._assert_authorized(slot)
            now = await self._validated_now()
            grant = await self._store.get_active_grant(slot, now=now)
            if grant is None:
                raise OAuthAuthorizationRequiredError()
            metadata = await self._metadata_provider.discover(
                resource=str(config.url),
                profile=profile,
                selected_issuer=grant.binding.issuer,
                installation_id=server_name,
            )
            client_binding = None
            if self._client_registration is not None:
                client_binding = await self._client_registration.resolve(
                    key=OAuthRegistrationKey(
                        profile_id=profile.profile_id,
                        profile_generation=profile.generation,
                        installation_id=server_name,
                        installation_generation=installation_generation,
                        issuer=grant.binding.issuer,
                    ),
                    profile=profile,
                    registration_endpoint=metadata.registration_endpoint,
                    redirect_uri=self._callback_policies[self._primary_callback_route].redirect_uri,
                )
            credential = await self._connector.get_bearer_credential(
                slot=slot,
                profile=profile,
                metadata=metadata,
                resource=str(config.url),
                client_binding=client_binding,
                private_network_policy=lambda url: profile.allowed_private_cidrs(
                    server_name,
                    url,
                ),
                authorization_fence=lambda: self._authorization_fence(slot),
            )
            try:
                await self._assert_authorized(slot)
            except OAuthAuthorizationRequiredError:
                await self._store.freeze_grant(
                    credential.grant_id,
                    credential.grant_version,
                    now=self._epoch_clock(),
                )
                raise
            yield credential.model_copy(
                update={
                    "allowed_private_cidrs": tuple(
                        str(network)
                        for network in profile.allowed_private_cidrs(
                            server_name,
                            str(config.url),
                        )
                    )
                }
            )

    @asynccontextmanager
    async def delegated_bearer_credential_guard(
        self,
        *,
        server_name: str,
        source_slot: OAuthAuthorizationSlotKey,
        expected_grant_id: str,
        minimum_grant_version: int,
        required_scopes: frozenset[str],
        authorization_fence: Callable[[], bool | Awaitable[bool]],
        refresh_egress_guard: Callable[
            [OAuthCredentialWireBinding], AbstractAsyncContextManager[None]
        ] | None = None,
    ) -> AsyncIterator[OAuthBearerCredential]:
        """Refresh one exact P8-A grant headlessly; browser flows are unreachable。"""
        _target_config, source_name, source_config = self._delegated_source_config(
            server_name
        )
        if (
            source_slot.installation_id != source_name
            or source_slot.auth_profile_id != source_config.auth_profile_id
            or source_slot.delegation_digest is not None
            or minimum_grant_version < 1
            or not required_scopes
        ):
            raise OAuthAuthorizationRequiredError()

        async def combined_fence() -> bool:
            delegated = authorization_fence()
            if isawaitable(delegated):
                delegated = await delegated
            if not delegated:
                return False
            current = self._authorization_fence(source_slot)
            if isawaitable(current):
                current = await current
            return self._delegated_feature_enabled and bool(current)

        async with self._profiles.profile_guard(source_slot.auth_profile_id):
            profile = await self._profile(source_config)
            if (
                not profile.enabled
                or profile.generation < 1
                or not await combined_fence()
            ):
                raise OAuthAuthorizationRequiredError()
            now = await self._validated_now()
            grant = await self._store.get_active_grant(source_slot, now=now)
            if not self._delegated_grant_matches(
                grant,
                expected_grant_id=expected_grant_id,
                minimum_grant_version=minimum_grant_version,
                profile_generation=profile.generation,
                required_scopes=required_scopes,
            ):
                raise OAuthAuthorizationRequiredError()
            metadata = await self._metadata_provider.discover(
                resource=str(source_config.url),
                profile=profile,
                selected_issuer=grant.binding.issuer,
                installation_id=source_name,
            )
            client_binding = None
            if self._client_registration is not None:
                client_binding = await self._client_registration.resolve(
                    key=OAuthRegistrationKey(
                        profile_id=profile.profile_id,
                        profile_generation=profile.generation,
                        installation_id=source_name,
                        installation_generation=source_slot.installation_generation,
                        issuer=grant.binding.issuer,
                    ),
                    profile=profile,
                    registration_endpoint=metadata.registration_endpoint,
                    redirect_uri=self._callback_policies[
                        self._primary_callback_route
                    ].redirect_uri,
                )
            credential = await self._connector.get_bearer_credential(
                slot=source_slot,
                profile=profile,
                metadata=metadata,
                resource=str(source_config.url),
                client_binding=client_binding,
                private_network_policy=lambda url: profile.allowed_private_cidrs(
                    source_name,
                    url,
                ),
                authorization_fence=combined_fence,
                refresh_egress_guard=refresh_egress_guard,
            )
            final_now = await self._validated_now()
            current_grant = await self._store.get_active_grant(
                source_slot,
                now=final_now,
            )
            if (
                not await combined_fence()
                or not self._delegated_grant_matches(
                    current_grant,
                    expected_grant_id=expected_grant_id,
                    minimum_grant_version=minimum_grant_version,
                    profile_generation=profile.generation,
                    required_scopes=required_scopes,
                )
                or credential.grant_id != current_grant.grant_id
                or credential.grant_version != current_grant.grant_version
            ):
                await self._store.freeze_grant(
                    credential.grant_id,
                    credential.grant_version,
                    now=final_now,
                )
                raise OAuthAuthorizationRequiredError()
            yield credential.model_copy(
                update={
                    "allowed_private_cidrs": tuple(
                        str(network)
                        for network in profile.allowed_private_cidrs(
                            source_name,
                            str(source_config.url),
                        )
                    )
                }
            )

    @staticmethod
    def _delegated_grant_matches(
        grant: object | None,
        *,
        expected_grant_id: str,
        minimum_grant_version: int,
        profile_generation: int,
        required_scopes: frozenset[str],
    ) -> bool:
        """Pin the slot and grant family while permitting monotonic token refresh。"""
        if grant is None:
            return False
        binding = getattr(grant, "binding", None)
        tokens = getattr(grant, "tokens", None)
        return bool(
            getattr(grant, "grant_id", None) == expected_grant_id
            and getattr(grant, "grant_version", 0) >= minimum_grant_version
            and getattr(binding, "profile_generation", None) == profile_generation
            and required_scopes.issubset(
                getattr(binding, "granted_scopes", frozenset())
            )
            and required_scopes.issubset(
                getattr(tokens, "granted_scopes", frozenset())
            )
        )

    async def get_bearer_credential(
        self,
        *,
        server_name: str,
        request_context: McpRequestContext,
        installation_generation: int,
    ) -> OAuthBearerCredential:
        """Resolve one credential for non-transport diagnostics and tests。"""
        async with self.bearer_credential_guard(
            server_name=server_name,
            request_context=request_context,
            installation_generation=installation_generation,
        ) as credential:
            return credential

    async def authorize_operation(
        self,
        *,
        server_name: str,
        request_context: McpRequestContext,
        installation_generation: int,
        operation: str,
        selector: str,
    ) -> None:
        """Enforce exact profile rule and granted-scope subset before any handle I/O。"""
        if not self._feature_enabled:
            raise OAuthScopeDeniedError()
        config = self._config(server_name)
        profile = await self._profile(config)
        if not profile.enabled:
            raise OAuthScopeDeniedError()
        if operation not in {"tools/call", "resources/read", "prompts/get"}:
            raise OAuthScopeDeniedError()
        required = profile.required_scopes(operation, selector)
        if required is None:
            raise OAuthScopeDeniedError()
        slot = self._slot(
            context=request_context,
            installation_generation=installation_generation,
            auth_profile_id=profile.profile_id,
        )
        try:
            await self._assert_authorized(slot)
        except OAuthAuthorizationRequiredError as exc:
            raise OAuthScopeDeniedError() from exc
        grant = await self._store.get_active_grant(slot, now=self._epoch_clock())
        if grant is None or not required.issubset(grant.binding.granted_scopes):
            raise OAuthScopeDeniedError()

    @staticmethod
    def _slot_from_transaction(
        transaction: OAuthTransactionRecord,
    ) -> OAuthAuthorizationSlotKey:
        payload = transaction.payload
        return OAuthAuthorizationSlotKey(
            human_principal_id=payload.human_principal_id,
            execution_agent_id=payload.execution_agent_id,
            delegation_digest=payload.delegation_digest,
            installation_id=payload.installation_id,
            installation_generation=payload.installation_generation,
            auth_profile_id=payload.auth_profile_id,
        )

    @staticmethod
    def _metadata_from_transaction(
        transaction: OAuthTransactionRecord,
    ) -> OAuthAuthorizationMetadata:
        payload = transaction.payload
        return OAuthAuthorizationMetadata(
            issuer=payload.issuer,
            authorization_endpoint=payload.authorization_endpoint,
            token_endpoint=payload.token_endpoint,
            revocation_endpoint=payload.revocation_endpoint,
            registration_endpoint=None,
            code_challenge_methods_supported=payload.code_challenge_methods_supported,
            response_modes_supported=payload.response_modes_supported,
            authorization_response_iss_parameter_supported=(
                payload.authorization_response_iss_parameter_supported
            ),
            security_fingerprint=payload.metadata_security_fingerprint,
        )

    async def complete_callback(
        self,
        *,
        callback: OAuthCallbackPayload,
        redirect_route: str,
    ) -> McpOAuthCallbackOutcome:
        policy = self.get_callback_policy(redirect_route)
        now = await self._validated_now()
        transaction = await self._store.find_transaction(
            callback.state,
            now=now,
        )
        if transaction is None or transaction.payload.response_mode != policy.response_mode:
            raise OAuthRuntimeBindingError()
        profile_id = transaction.payload.auth_profile_id
        async with self._profiles.profile_guard(profile_id):
            transaction = await self._store.find_transaction(
                callback.state,
                now=now,
            )
            if (
                transaction is None
                or transaction.payload.response_mode != policy.response_mode
                or transaction.payload.auth_profile_id != profile_id
            ):
                raise OAuthRuntimeBindingError()
            profile = await self._profiles.get(profile_id)
            if profile is None:
                raise OAuthRuntimeBindingError()
            slot = self._slot_from_transaction(transaction)
            if not profile.enabled:
                raise OAuthAuthorizationRequiredError()
            await self._assert_authorized(slot)
            current_client_binding = None
            if self._client_registration is not None:
                current_client_binding = await self._client_registration.resolve(
                    key=OAuthRegistrationKey(
                        profile_id=profile.profile_id,
                        profile_generation=profile.generation,
                        installation_id=transaction.payload.installation_id,
                        installation_generation=(transaction.payload.installation_generation),
                        issuer=transaction.payload.issuer,
                    ),
                    profile=profile,
                    registration_endpoint=None,
                    redirect_uri=transaction.payload.redirect_uri,
                )
            elif transaction.payload.client_id is not None:
                current_client_binding = OAuthClientBinding(
                    client_id=transaction.payload.client_id,
                    client_secret=transaction.payload.client_secret,
                    client_secret_expires_at=(transaction.payload.client_secret_expires_at),
                    token_endpoint_auth_method=(transaction.payload.token_endpoint_auth_method),
                    registration_version=transaction.payload.registration_version,
                )
            result = await self._connector.complete_callback(
                callback=callback,
                redirect_route=redirect_route,
                profile=profile,
                metadata=self._metadata_from_transaction(transaction),
                slot=slot,
                authorization_fence=lambda: self._authorization_fence(slot),
                client_binding=current_client_binding,
                private_network_policy=lambda url: profile.allowed_private_cidrs(
                    transaction.payload.installation_id,
                    url,
                ),
            )
            return McpOAuthCallbackOutcome(
                operation_id=result.operation_id,
                status=McpOAuthStatus(result.status),
                client_origin=profile.trusted_public_origin,
            )

    async def list_profiles(self) -> list[OAuthProfile]:
        return await self._profiles.list_profiles()

    async def put_profile(self, profile: OAuthProfile) -> OAuthProfile:
        async with self._profiles.profile_guard(profile.profile_id):
            epoch = await self._store.begin_profile_mutation(
                profile.profile_id,
                now=self._epoch_clock(),
            )
            result = await self._profiles.put(profile)
            if not await self._store.complete_profile_mutation(
                profile.profile_id,
                epoch,
                applied_fingerprint=oauth_profile_fingerprint(result),
                now=self._epoch_clock(),
            ):
                raise OAuthRuntimeBindingError()
            return result

    async def delete_profile(self, profile_id: str) -> None:
        async with self._profiles.profile_guard(profile_id):
            epoch = await self._store.begin_profile_mutation(
                profile_id,
                now=self._epoch_clock(),
            )
            await self._profiles.delete(profile_id)
            if not await self._store.complete_profile_mutation(
                profile_id,
                epoch,
                applied_fingerprint=None,
                now=self._epoch_clock(),
            ):
                raise OAuthRuntimeBindingError()

    async def reconcile_profile_mutations(self) -> None:
        """Resolve crash-left updating intents from the authoritative JSON document。"""
        for profile_id, epoch in await self._store.list_updating_profile_fences():
            async with self._profiles.profile_guard(profile_id):
                profile = await self._profiles.get(profile_id)
                fingerprint = oauth_profile_fingerprint(profile) if profile is not None else None
                if not await self._store.complete_profile_mutation(
                    profile_id,
                    epoch,
                    applied_fingerprint=fingerprint,
                    now=self._epoch_clock(),
                ):
                    raise OAuthRuntimeBindingError()
