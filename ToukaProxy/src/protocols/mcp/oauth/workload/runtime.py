# ruff: noqa: TC002
"""Single-process P8-B runtime for exact tickets and ephemeral access tokens。"""

from __future__ import annotations

import asyncio
import heapq
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from pydantic import SecretStr
from src.protocols.mcp.canonical import (
    ExecutionKind,
    McpInvocationAuthorization,
    McpRequestContext,
    McpTransportBootstrapAuthorization,
    McpWorkloadWireAttempt,
)
from src.protocols.mcp.oauth.models import (
    OAuthAuthorizationSlotKey,
    OAuthCredentialWireBinding,
)
from src.protocols.mcp.oauth.service import OAuthBearerCredential
from src.protocols.mcp.oauth.workload.models import (
    WorkloadAccessToken,
    WorkloadCredentialPayload,
    WorkloadMetadataSnapshot,
)
from src.protocols.mcp.oauth.workload.target import (
    McpOAuthTargetBinding,
    target_allows_transport,
)

MAX_CONSUMED_INVOCATION_TICKETS = 4_096

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from contextlib import AbstractAsyncContextManager


class WorkloadRuntimeDenied(PermissionError):
    """A ticket, durable authority, token, or egress fence failed closed。"""


@dataclass(frozen=True, slots=True)
class WorkloadAuthorityMaterial:
    """Exact durable material needed to exchange and attach one workload bearer。"""

    authority_key: str
    snapshot: WorkloadMetadataSnapshot
    credential: WorkloadCredentialPayload
    target_binding: McpOAuthTargetBinding
    allowed_private_cidrs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkloadDelegatedAuthorityMaterial:
    """Exact P8-A source authority; bearer and refresh tokens stay with P8-A。"""

    authority_key: str
    source_slot: OAuthAuthorizationSlotKey
    source_grant_id: str
    source_grant_version: int
    target_binding: McpOAuthTargetBinding


class WorkloadInvocationAuthorizer(Protocol):
    """Resolve and revalidate ticket authority outside transport mechanics。"""

    async def authorize_operation(
        self,
        **kwargs: Any,
    ) -> McpInvocationAuthorization:
        """Create one immutable exact-operation authorization ticket。"""
        ...

    async def revalidate(
        self,
        ticket: McpInvocationAuthorization,
        *,
        now: float,
    ) -> WorkloadAuthorityMaterial | WorkloadDelegatedAuthorityMaterial:
        """Return current durable material or deny without side effects。"""
        ...


class WorkloadTokenExchange(Protocol):
    """At-most-two-attempt token exchange already wrapped by token permits。"""

    async def exchange(self, **kwargs: Any) -> WorkloadAccessToken:
        """Return an ephemeral validated Bearer token。"""
        ...


class WorkloadSendPermit(Protocol):
    """Opaque send permit projection。"""

    permit_id: str


class WorkloadSendPermits(Protocol):
    """Per-request permit owner used for MCP bearer HTTP sends。"""

    async def issue(self, **kwargs: Any) -> WorkloadSendPermit:
        """Issue one exact boot-local capability。"""
        ...

    async def begin_send(self, permit: WorkloadSendPermit, **kwargs: Any) -> None:
        """Revalidate and consume the capability before network egress。"""
        ...

    async def release(self, permit_id: str) -> None:
        """Cancel unused issuance or release in-flight state after request exit。"""
        ...


class WorkloadEgressAudit(Protocol):
    """Durable secret-free intent acceptance gate before credential egress。"""

    async def accept_egress_intent(self, **kwargs: Any) -> None:
        """Persist an idempotent security event or fail closed。"""
        ...


class DelegatedBearerProvider(Protocol):
    """P8-A remains the sole owner of delegated user bearer and refresh tokens。"""

    def delegated_bearer_credential_guard(
        self,
        **kwargs: Any,
    ) -> AbstractAsyncContextManager[OAuthBearerCredential]:
        """Yield one exact P8-A credential under a live delegation fence。"""
        ...


class WorkloadOAuthRuntime:
    """Sole in-process owner of workload access tokens and egress authorization。"""

    def __init__(
        self,
        *,
        authorizer: WorkloadInvocationAuthorizer,
        exchange: WorkloadTokenExchange,
        delegated_bearer_provider: DelegatedBearerProvider | None = None,
        permits: WorkloadSendPermits,
        audit: WorkloadEgressAudit,
        clock: Callable[[], float],
        send_permit_ttl: float,
        token_expiry_skew: float,
        ticket_replay_capacity: int = MAX_CONSUMED_INVOCATION_TICKETS,
    ) -> None:
        if (
            send_permit_ttl <= 0
            or token_expiry_skew < 0
            or ticket_replay_capacity <= 0
        ):
            raise ValueError()
        self._authorizer = authorizer
        self._exchange = exchange
        self._delegated_bearer_provider = delegated_bearer_provider
        self._permits = permits
        self._audit = audit
        self._clock = clock
        self._send_permit_ttl = send_permit_ttl
        self._token_expiry_skew = token_expiry_skew
        self._tokens: dict[str, WorkloadAccessToken] = {}
        self._token_locks: dict[str, asyncio.Lock] = {}
        self._active_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._ticket_replay_capacity = ticket_replay_capacity
        self._consumed_invocation_tickets: dict[str, float] = {}
        self._consumed_invocation_ticket_expiries: list[tuple[float, str]] = []
        self._ticket_send_lock = asyncio.Lock()

    @property
    def cached_token_count(self) -> int:
        """Return only an aggregate; token values never leave this runtime。"""
        return len(self._tokens)

    @property
    def consumed_ticket_count(self) -> int:
        """Return the bounded replay-index cardinality without exposing IDs。"""
        return len(self._consumed_invocation_tickets)

    def _prune_consumed_invocation_tickets(self, *, now: float) -> None:
        """Drop expired replay markers while holding ``_ticket_send_lock``。"""
        while self._consumed_invocation_ticket_expiries:
            expires_at, authorization_id = self._consumed_invocation_ticket_expiries[0]
            if expires_at > now:
                return
            heapq.heappop(self._consumed_invocation_ticket_expiries)
            if self._consumed_invocation_tickets.get(authorization_id) == expires_at:
                self._consumed_invocation_tickets.pop(authorization_id, None)

    def invalidate_authority(self, authority_key: str) -> None:
        """Erase one exact ephemeral token partition after a control-plane fence。"""
        self._tokens.pop(authority_key, None)

    def invalidate_all_authorities(self) -> None:
        """Erase every ephemeral token after a global authority epoch advance。"""
        self._tokens.clear()

    async def authorize_operation(
        self,
        *,
        server_name: str,
        request_context: McpRequestContext,
        installation_generation: int,
        operation: str,
        selector: str,
        arguments: dict[str, Any],
    ) -> McpInvocationAuthorization:
        """Delegate policy resolution, then verify the returned wire binding。"""
        if (
            request_context.execution_kind != ExecutionKind.HEARTBEAT
            or request_context.origin != "heartbeat"
            or operation != "tools/call"
        ):
            raise WorkloadRuntimeDenied()
        ticket = await self._authorizer.authorize_operation(
            server_name=server_name,
            request_context=request_context,
            installation_generation=installation_generation,
            operation=operation,
            selector=selector,
            arguments=arguments,
        )
        if (
            ticket.installation_id != server_name
            or ticket.installation_generation != installation_generation
            or ticket.execution_agent_id != request_context.identity.agent_id
            or ticket.execution_kind != request_context.execution_kind
            or ticket.origin != request_context.origin
            or ticket.request_id != request_context.request_id
        ):
            raise WorkloadRuntimeDenied()
        ticket.verify_wire_attempt(
            method=operation,
            tool_name=selector,
            arguments=arguments,
        )
        return ticket

    async def authorize_transport_bootstrap(
        self,
        *,
        server_name: str,
        authorization: McpInvocationAuthorization,
    ) -> McpTransportBootstrapAuthorization:
        """Mint a distinct non-tool authority for initialize/discover/catalog only。"""
        now = self._clock()
        if authorization.installation_id != server_name or now >= authorization.expires_at:
            raise WorkloadRuntimeDenied()
        material = await self._authorizer.revalidate(authorization, now=now)
        if not isinstance(
            material,
            (WorkloadAuthorityMaterial, WorkloadDelegatedAuthorityMaterial),
        ) or material.authority_key != authorization.handle_principal_key:
            raise WorkloadRuntimeDenied()
        return McpTransportBootstrapAuthorization(
            authorization_id=str(uuid4()),
            parent_authorization=authorization,
            issued_at=now,
            expires_at=min(
                authorization.expires_at,
                now + self._send_permit_ttl,
            ),
        )

    @staticmethod
    def _same_authority(
        ticket: McpInvocationAuthorization,
        first: WorkloadAuthorityMaterial,
        second: WorkloadAuthorityMaterial,
    ) -> bool:
        return first.authority_key == ticket.handle_principal_key and second == first

    async def _access_token(
        self,
        ticket: McpInvocationAuthorization,
        *,
        transport_url: str,
    ) -> tuple[WorkloadAccessToken, WorkloadAuthorityMaterial]:
        authority_key = ticket.handle_principal_key
        lock = self._token_locks.setdefault(authority_key, asyncio.Lock())
        async with lock:
            now = self._clock()
            material = await self._authorizer.revalidate(ticket, now=now)
            if not isinstance(material, WorkloadAuthorityMaterial) or (
                material.authority_key != authority_key
                or material.target_binding.digest != ticket.target_binding_digest
                or not target_allows_transport(
                    material.target_binding,
                    transport_url,
                )
            ):
                raise WorkloadRuntimeDenied()
            token = self._tokens.get(authority_key)
            if (
                token is None
                or token.expires_at <= now + self._token_expiry_skew
                or not ticket.required_scopes.issubset(token.granted_scopes)
            ):
                self._tokens.pop(authority_key, None)

                async def revalidate_exchange_authority() -> bool:
                    current = await self._authorizer.revalidate(
                        ticket,
                        now=self._clock(),
                    )
                    return isinstance(current, WorkloadAuthorityMaterial) and (
                        self._same_authority(ticket, material, current)
                    )

                token = await self._exchange.exchange(
                    ticket=ticket,
                    task_id=ticket.request_id,
                    snapshot=material.snapshot,
                    credential=material.credential,
                    authority_revalidator=revalidate_exchange_authority,
                )
                if (
                    token.expires_at <= self._clock()
                    or not ticket.required_scopes.issubset(token.granted_scopes)
                ):
                    raise WorkloadRuntimeDenied()
                self._tokens[authority_key] = token
            return token, material

    @asynccontextmanager
    async def _send_permit_guard(
        self,
        authorization: McpInvocationAuthorization,
        *,
        authority_revalidator: Callable[[], Awaitable[bool]],
        wire_attempt: McpWorkloadWireAttempt,
    ) -> AsyncIterator[None]:
        """Persist intent, consume one exact permit, and always acknowledge it。"""
        now = self._clock()
        await self._audit.accept_egress_intent(
            authorization=authorization,
            egress_kind=wire_attempt.egress_kind,
            wire_attempt=wire_attempt,
            attempt_number=1,
            now=now,
        )
        permit = await self._permits.issue(
            authority_key=authorization.handle_principal_key,
            task_id=authorization.request_id,
            ticket_id=wire_attempt.authorization_id,
            egress_kind=wire_attempt.egress_kind,
            method=wire_attempt.method,
            payload_digest=wire_attempt.payload_digest,
            target_binding_digest=wire_attempt.target_binding_digest,
            attempt_number=1,
            issued_at=now,
            expires_at=min(
                authorization.expires_at,
                now + self._send_permit_ttl,
            ),
        )
        try:
            if not await authority_revalidator():
                raise WorkloadRuntimeDenied()
            await self._permits.begin_send(
                permit,
                task_id=authorization.request_id,
                ticket_id=wire_attempt.authorization_id,
                egress_kind=wire_attempt.egress_kind,
                method=wire_attempt.method,
                payload_digest=wire_attempt.payload_digest,
                target_binding_digest=wire_attempt.target_binding_digest,
                now=self._clock(),
            )
            yield
        finally:
            await asyncio.shield(self._permits.release(permit.permit_id))

    @staticmethod
    def _same_delegated_authority(
        first: WorkloadDelegatedAuthorityMaterial,
        second: WorkloadDelegatedAuthorityMaterial,
    ) -> bool:
        """Allow only monotonic refreshes of the same exact P8-A grant family。"""
        return (
            second.authority_key == first.authority_key
            and second.source_slot == first.source_slot
            and second.source_grant_id == first.source_grant_id
            and second.source_grant_version >= first.source_grant_version
            and second.target_binding == first.target_binding
        )

    @asynccontextmanager
    async def _delegated_bearer_credential_guard(
        self,
        *,
        server_name: str,
        authorization: McpInvocationAuthorization,
        wire_attempt: McpWorkloadWireAttempt,
        transport_url: str,
    ) -> AsyncIterator[OAuthBearerCredential]:
        provider = self._delegated_bearer_provider
        if provider is None:
            raise WorkloadRuntimeDenied()
        first = await self._authorizer.revalidate(
            authorization,
            now=self._clock(),
        )
        if not isinstance(first, WorkloadDelegatedAuthorityMaterial) or (
            first.authority_key != authorization.handle_principal_key
            or first.target_binding.digest != authorization.target_binding_digest
            or not target_allows_transport(first.target_binding, transport_url)
        ):
            raise WorkloadRuntimeDenied()
        latest = first

        async def authorization_fence() -> bool:
            nonlocal latest
            current = await self._authorizer.revalidate(
                authorization,
                now=self._clock(),
            )
            if not isinstance(current, WorkloadDelegatedAuthorityMaterial) or (
                not self._same_delegated_authority(latest, current)
            ):
                return False
            latest = current
            return True

        @asynccontextmanager
        async def refresh_egress_guard(
            binding: OAuthCredentialWireBinding,
        ) -> AsyncIterator[None]:
            if (
                binding.grant_type != "refresh_token"
                or binding.grant_id != latest.source_grant_id
                or binding.grant_version != latest.source_grant_version
            ):
                raise WorkloadRuntimeDenied()
            refresh_attempt = McpWorkloadWireAttempt(
                authorization_id=str(uuid4()),
                egress_kind="delegated_refresh",
                method=binding.http_method,
                payload_digest=binding.digest,
                target_binding_digest=authorization.target_binding_digest,
            )
            async with self._send_permit_guard(
                authorization,
                authority_revalidator=authorization_fence,
                wire_attempt=refresh_attempt,
            ):
                yield

        async with provider.delegated_bearer_credential_guard(
            server_name=server_name,
            source_slot=first.source_slot,
            expected_grant_id=first.source_grant_id,
            minimum_grant_version=first.source_grant_version,
            required_scopes=authorization.required_scopes,
            authorization_fence=authorization_fence,
            refresh_egress_guard=refresh_egress_guard,
        ) as credential:
            if not await authorization_fence() or (
                credential.grant_id != latest.source_grant_id
                or credential.grant_version != latest.source_grant_version
            ):
                raise WorkloadRuntimeDenied()

            async def final_authority_revalidation() -> bool:
                if not await authorization_fence():
                    return False
                return (
                    credential.grant_id == latest.source_grant_id
                    and credential.grant_version == latest.source_grant_version
                )

            async with self._send_permit_guard(
                authorization,
                authority_revalidator=final_authority_revalidation,
                wire_attempt=wire_attempt,
            ):
                yield credential

    @asynccontextmanager
    async def workload_bearer_credential_guard(
        self,
        *,
        server_name: str,
        authorization: McpInvocationAuthorization,
        wire_attempt: McpWorkloadWireAttempt,
        transport_url: str,
        bootstrap_authorization: McpTransportBootstrapAuthorization | None = None,
    ) -> AsyncIterator[OAuthBearerCredential]:
        """Revalidate twice and hold one send permit for the full HTTP attempt。"""
        task = asyncio.current_task()
        if task is None:
            raise WorkloadRuntimeDenied()
        tasks = self._active_tasks.setdefault(authorization.request_id, set())
        tasks.add(task)
        try:
            if (
                authorization.installation_id != server_name
                or wire_attempt.target_binding_digest
                != authorization.target_binding_digest
            ):
                raise WorkloadRuntimeDenied()
            if wire_attempt.egress_kind == "mcp_bearer":
                if (
                    bootstrap_authorization is not None
                    or wire_attempt.authorization_id
                    != authorization.authorization_id
                    or wire_attempt.method != authorization.operation_method
                    or wire_attempt.raw_tool_name != authorization.raw_tool_name
                    or wire_attempt.arguments_digest
                    != authorization.arguments_digest
                ):
                    raise WorkloadRuntimeDenied()
                async with self._ticket_send_lock:
                    now = self._clock()
                    self._prune_consumed_invocation_tickets(now=now)
                    if (
                        authorization.authorization_id
                        in self._consumed_invocation_tickets
                    ):
                        raise WorkloadRuntimeDenied()
                    if (
                        len(self._consumed_invocation_tickets)
                        >= self._ticket_replay_capacity
                    ):
                        raise WorkloadRuntimeDenied()
                    self._consumed_invocation_tickets[
                        authorization.authorization_id
                    ] = authorization.expires_at
                    heapq.heappush(
                        self._consumed_invocation_ticket_expiries,
                        (authorization.expires_at, authorization.authorization_id),
                    )
            elif wire_attempt.egress_kind == "transport_bootstrap":
                if (
                    bootstrap_authorization is None
                    or bootstrap_authorization.parent_authorization != authorization
                    or wire_attempt.authorization_id
                    != bootstrap_authorization.authorization_id
                    or wire_attempt.method
                    not in bootstrap_authorization.allowed_methods
                    or not bootstrap_authorization.issued_at
                    <= self._clock()
                    < bootstrap_authorization.expires_at
                ):
                    raise WorkloadRuntimeDenied()
            else:
                raise WorkloadRuntimeDenied()
            if authorization.authority_kind == "delegation":
                async with self._delegated_bearer_credential_guard(
                    server_name=server_name,
                    authorization=authorization,
                    wire_attempt=wire_attempt,
                    transport_url=transport_url,
                ) as delegated_credential:
                    yield delegated_credential
                return
            token, first_material = await self._access_token(
                authorization,
                transport_url=transport_url,
            )
            now = self._clock()
            second_material = await self._authorizer.revalidate(authorization, now=now)
            if not isinstance(second_material, WorkloadAuthorityMaterial) or (
                not self._same_authority(
                    authorization,
                    first_material,
                    second_material,
                )
                or not target_allows_transport(
                    second_material.target_binding,
                    transport_url,
                )
            ):
                self._tokens.pop(authorization.handle_principal_key, None)
                raise WorkloadRuntimeDenied()
            credential_generation = authorization.credential_generation
            if credential_generation is None:
                raise WorkloadRuntimeDenied()

            async def final_authority_revalidation() -> bool:
                current = await self._authorizer.revalidate(
                    authorization,
                    now=self._clock(),
                )
                return isinstance(current, WorkloadAuthorityMaterial) and (
                    self._same_authority(
                        authorization,
                        second_material,
                        current,
                    )
                )

            async with self._send_permit_guard(
                authorization,
                authority_revalidator=final_authority_revalidation,
                wire_attempt=wire_attempt,
            ):
                yield OAuthBearerCredential(
                    grant_id=f"workload:{authorization.auth_profile_id}",
                    grant_version=credential_generation,
                    authorization_header=SecretStr(
                        f"Bearer {token.access_token.get_secret_value()}"
                    ),
                    allowed_private_cidrs=second_material.allowed_private_cidrs,
                )
        finally:
            tasks.discard(task)
            if not tasks:
                self._active_tasks.pop(authorization.request_id, None)

    async def cancel_tasks(self, task_ids: frozenset[str]) -> None:
        """Cancel and await every locally registered request before drain checks。"""
        current = asyncio.current_task()
        tasks = {
            task
            for task_id in task_ids
            for task in self._active_tasks.get(task_id, set())
            if task is not current and not task.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        """Erase all ephemeral token projections during lifespan shutdown。"""
        await self.cancel_tasks(frozenset(self._active_tasks))
        self._tokens.clear()
        self._token_locks.clear()
        self._active_tasks.clear()
        self._consumed_invocation_tickets.clear()
        self._consumed_invocation_ticket_expiries.clear()
