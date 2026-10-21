"""Strict single-attempt P8-B client-credentials token exchange。"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from pydantic import SecretStr
from src.protocols.mcp.canonical import McpWorkloadWireAttempt
from src.protocols.mcp.oauth import outbound as oauth_outbound
from src.protocols.mcp.oauth.models import OAuthCredentialWireBinding
from src.protocols.mcp.oauth.workload import models as workload_models

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from src.protocols.mcp.canonical import McpInvocationAuthorization

_MAX_ACCESS_TOKEN_CHARS = 65_536
_MAX_TOKEN_LIFETIME_SECONDS = 86_400.0
_CLIENT_AUTH_BASIC = "client_secret_basic"
_CLIENT_AUTH_POST = "client_secret_post"


class WorkloadTokenExchangeDenied(ValueError):
    """Local verified authority is absent or inconsistent; no egress is allowed。"""


class WorkloadTokenResponseError(ValueError):
    """A successful endpoint response violated the workload token contract。"""


class WorkloadTokenExchangeRejected(ValueError):
    """The authorization server rejected the request; retry is forbidden。"""


class WorkloadTokenExchangeRetryable(RuntimeError):
    """A pre-response transport failure or 5xx may be retried with a new permit。"""


class WorkloadTokenTransportError(OSError):
    """The hardened endpoint transport failed before a response was received。"""


class WorkloadTokenEndpointProtocolError(ValueError):
    """The token endpoint response violated the hardened HTTP framing contract。"""


class WorkloadTokenEndpointPolicyError(PermissionError):
    """The endpoint or private-network request lacks an explicit egress policy。"""


@dataclass(frozen=True, slots=True)
class WorkloadTokenEndpointResponse:
    """Bounded JSON response already parsed by the hardened HTTP adapter。"""

    status_code: int
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class WorkloadPreparedTokenRequest:
    """One immutable request whose exact binding is permitted before send。"""

    endpoint: str
    form: tuple[tuple[str, str], ...]
    basic_auth: tuple[str, str] | None
    private_network_cidrs: tuple[str, ...]
    binding: OAuthCredentialWireBinding


class WorkloadTokenEndpoint(Protocol):
    """DIP seam for a redirect-free, SSRF-hardened token endpoint adapter。"""

    async def post_token(
        self,
        *,
        endpoint: str,
        form: dict[str, str],
        basic_auth: tuple[str, str] | None,
        private_network_cidrs: tuple[str, ...],
    ) -> WorkloadTokenEndpointResponse:
        """Post one bounded, redirect-free client-credentials request。"""
        ...


class WorkloadOAuthHttpEndpoint:
    """Production adapter over the shared pinned OAuth HTTP implementation。"""

    def __init__(
        self,
        *,
        outbound: oauth_outbound.OAuthHttpClient,
    ) -> None:
        self._outbound = outbound

    async def post_token(
        self,
        *,
        endpoint: str,
        form: dict[str, str],
        basic_auth: tuple[str, str] | None,
        private_network_cidrs: tuple[str, ...],
    ) -> WorkloadTokenEndpointResponse:
        """Post through pinned DNS/IP transport and preserve retry classification。"""
        private_networks = tuple(
            ipaddress.ip_network(item, strict=True) for item in private_network_cidrs
        )
        private_policy = (
            (lambda _url: private_networks) if private_networks else None
        )
        try:
            payload, status = await self._outbound.post_client_credentials_token(
                token_endpoint=endpoint,
                form=form,
                basic_auth=basic_auth,
                private_network_policy=private_policy,
            )
        except oauth_outbound.OAuthTransportError as exc:
            raise WorkloadTokenTransportError() from exc
        except oauth_outbound.OAuthOutboundPolicyError as exc:
            raise WorkloadTokenEndpointPolicyError() from exc
        except oauth_outbound.OAuthUpstreamError as exc:
            raise WorkloadTokenEndpointProtocolError() from exc
        return WorkloadTokenEndpointResponse(status_code=status, payload=payload)


class WorkloadAttemptPermit(Protocol):
    """Opaque permit fields needed by the exchange coordinator。"""

    permit_id: str
    target_binding_digest: str


class WorkloadExchangePermits(Protocol):
    """Boot-local permit operations used around each token wire attempt。"""

    async def issue(
        self,
        *,
        authority_key: str,
        task_id: str,
        ticket_id: str,
        method: str,
        payload_digest: str,
        target_binding_digest: str,
        attempt_number: int,
        issued_at: float,
        expires_at: float,
    ) -> WorkloadAttemptPermit:
        """Issue a new capability for one exact attempt。"""
        ...

    async def begin_send(
        self,
        permit: WorkloadAttemptPermit,
        *,
        task_id: str,
        ticket_id: str,
        method: str,
        payload_digest: str,
        target_binding_digest: str,
        now: float,
    ) -> None:
        """Revalidate and atomically enter the wire-send state。"""
        ...

    async def release(self, permit_id: str) -> None:
        """Cancel unused issuance or acknowledge only after its request exits。"""
        ...


class WorkloadExchangeAudit(Protocol):
    """Durable secret-free intent gate before each token endpoint attempt。"""

    async def accept_egress_intent(self, **kwargs: Any) -> None:
        """Persist the exact attempt event or fail closed。"""
        ...


class WorkloadTokenExchanger:
    """Validate one exact snapshot and perform exactly one wire attempt。"""

    def __init__(self, *, endpoint: WorkloadTokenEndpoint) -> None:
        self._endpoint = endpoint

    @staticmethod
    def _validate_authority(
        *,
        snapshot: workload_models.WorkloadMetadataSnapshot | None,
        credential: workload_models.WorkloadCredentialPayload,
        requested_scopes: frozenset[str],
    ) -> workload_models.WorkloadMetadataSnapshot:
        if (
            snapshot is None
            or snapshot.profile_id != credential.profile_id
            or not requested_scopes.issubset(snapshot.verified_scopes)
        ):
            raise WorkloadTokenExchangeDenied()
        return snapshot

    @staticmethod
    def _client_auth(
        *,
        snapshot: workload_models.WorkloadMetadataSnapshot,
        credential: workload_models.WorkloadCredentialPayload,
        form: dict[str, str],
    ) -> tuple[str, str] | None:
        client_id = credential.client_id.get_secret_value()
        client_secret = credential.client_secret.get_secret_value()
        if not client_id or not client_secret:
            raise WorkloadTokenExchangeDenied()
        if snapshot.token_endpoint_auth_method == _CLIENT_AUTH_BASIC:
            return (client_id, client_secret)
        if snapshot.token_endpoint_auth_method == _CLIENT_AUTH_POST:
            form["client_id"] = client_id
            form["client_secret"] = client_secret
            return None
        raise WorkloadTokenExchangeDenied()

    @staticmethod
    def _token_from_response(
        payload: Mapping[str, Any],
        *,
        requested_scopes: frozenset[str],
        now: float,
    ) -> workload_models.WorkloadAccessToken:
        access_token = payload.get("access_token")
        token_type = payload.get("token_type")
        expires_in = payload.get("expires_in")
        scope = payload.get("scope")
        if (
            not isinstance(access_token, str)
            or not access_token
            or len(access_token) > _MAX_ACCESS_TOKEN_CHARS
            or "refresh_token" in payload
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or not 0 < float(expires_in) <= _MAX_TOKEN_LIFETIME_SECONDS
            or not isinstance(scope, str)
            or not scope
        ):
            raise WorkloadTokenResponseError()
        granted_scopes = frozenset(scope.split())
        if not granted_scopes or not requested_scopes.issubset(granted_scopes):
            raise WorkloadTokenResponseError()
        return workload_models.WorkloadAccessToken(
            access_token=SecretStr(access_token),
            expires_at=now + float(expires_in),
            granted_scopes=granted_scopes,
        )

    def prepare(
        self,
        *,
        snapshot: workload_models.WorkloadMetadataSnapshot | None,
        credential: workload_models.WorkloadCredentialPayload,
        requested_scopes: frozenset[str],
    ) -> WorkloadPreparedTokenRequest:
        """Freeze the exact HTTP request and its secret-free authority binding。"""
        verified = self._validate_authority(
            snapshot=snapshot,
            credential=credential,
            requested_scopes=requested_scopes,
        )
        form = {
            "grant_type": "client_credentials",
            "resource": verified.resource,
            "scope": " ".join(sorted(requested_scopes)),
        }
        basic_auth = self._client_auth(
            snapshot=verified,
            credential=credential,
            form=form,
        )
        binding = OAuthCredentialWireBinding(
            grant_type="client_credentials",
            token_endpoint=verified.token_endpoint,
            resource=verified.resource,
            scopes=tuple(sorted(requested_scopes)),
            client_auth_method=verified.token_endpoint_auth_method,
            form_fields=tuple(sorted(form)),
            profile_id=verified.profile_id,
            profile_generation=verified.profile_generation,
            profile_fingerprint=verified.profile_fingerprint,
            metadata_security_fingerprint=verified.metadata_security_fingerprint,
            credential_id=credential.credential_id,
            credential_generation=credential.generation,
        )
        return WorkloadPreparedTokenRequest(
            endpoint=verified.token_endpoint,
            form=tuple(sorted(form.items())),
            basic_auth=basic_auth,
            private_network_cidrs=verified.private_network_cidrs,
            binding=binding,
        )

    async def exchange_prepared(
        self,
        prepared: WorkloadPreparedTokenRequest,
        *,
        requested_scopes: frozenset[str],
        now: float,
    ) -> workload_models.WorkloadAccessToken:
        """Send exactly the already-bound request; never rebuild its wire shape。"""
        try:
            response = await self._endpoint.post_token(
                endpoint=prepared.endpoint,
                form=dict(prepared.form),
                basic_auth=prepared.basic_auth,
                private_network_cidrs=prepared.private_network_cidrs,
            )
        except WorkloadTokenTransportError as exc:
            raise WorkloadTokenExchangeRetryable() from exc
        except WorkloadTokenEndpointPolicyError as exc:
            raise WorkloadTokenExchangeDenied() from exc
        except WorkloadTokenEndpointProtocolError as exc:
            raise WorkloadTokenResponseError() from exc
        if 400 <= response.status_code < 500:
            raise WorkloadTokenExchangeRejected()
        if 500 <= response.status_code < 600:
            raise WorkloadTokenExchangeRetryable()
        if response.status_code != 200:
            raise WorkloadTokenResponseError()
        return self._token_from_response(
            response.payload,
            requested_scopes=requested_scopes,
            now=now,
        )

    async def exchange(
        self,
        *,
        snapshot: workload_models.WorkloadMetadataSnapshot | None,
        credential: workload_models.WorkloadCredentialPayload,
        requested_scopes: frozenset[str],
        now: float,
    ) -> workload_models.WorkloadAccessToken:
        """Compatibility wrapper for one locally prepared exact attempt。"""
        prepared = self.prepare(
            snapshot=snapshot,
            credential=credential,
            requested_scopes=requested_scopes,
        )
        return await self.exchange_prepared(
            prepared,
            requested_scopes=requested_scopes,
            now=now,
        )


class WorkloadTokenExchangeCoordinator:
    """Bind an exact heartbeat ticket and at most two separately permitted attempts。"""

    def __init__(
        self,
        *,
        exchanger: WorkloadTokenExchanger,
        permits: WorkloadExchangePermits,
        audit: WorkloadExchangeAudit,
        clock: Callable[[], float],
        attempt_ttl: float,
    ) -> None:
        if attempt_ttl <= 0:
            raise ValueError()
        self._exchanger = exchanger
        self._permits = permits
        self._audit = audit
        self._clock = clock
        self._attempt_ttl = attempt_ttl

    @staticmethod
    def _validate_ticket(
        *,
        ticket: McpInvocationAuthorization,
        snapshot: workload_models.WorkloadMetadataSnapshot,
        credential: workload_models.WorkloadCredentialPayload,
        now: float,
    ) -> None:
        if (
            now >= ticket.expires_at
            or ticket.auth_profile_id != snapshot.profile_id
            or ticket.profile_generation != snapshot.profile_generation
            or ticket.credential_generation != credential.generation
            or credential.profile_id != snapshot.profile_id
            or not ticket.required_scopes.issubset(snapshot.verified_scopes)
        ):
            raise WorkloadTokenExchangeDenied()

    async def _attempt(
        self,
        *,
        attempt_number: int,
        ticket: McpInvocationAuthorization,
        task_id: str,
        snapshot: workload_models.WorkloadMetadataSnapshot,
        credential: workload_models.WorkloadCredentialPayload,
        authority_revalidator: Callable[[], Awaitable[bool]],
    ) -> workload_models.WorkloadAccessToken:
        now = self._clock()
        self._validate_ticket(
            ticket=ticket,
            snapshot=snapshot,
            credential=credential,
            now=now,
        )
        prepared = self._exchanger.prepare(
            snapshot=snapshot,
            credential=credential,
            requested_scopes=ticket.required_scopes,
        )
        wire_attempt = McpWorkloadWireAttempt(
            authorization_id=str(uuid4()),
            egress_kind="token_exchange",
            method=prepared.binding.http_method,
            payload_digest=prepared.binding.digest,
            target_binding_digest=ticket.target_binding_digest,
        )
        await self._audit.accept_egress_intent(
            authorization=ticket,
            egress_kind="token_exchange",
            wire_attempt=wire_attempt,
            attempt_number=attempt_number,
            now=now,
        )
        permit = await self._permits.issue(
            authority_key=ticket.handle_principal_key,
            task_id=task_id,
            ticket_id=wire_attempt.authorization_id,
            egress_kind="token_exchange",
            method=wire_attempt.method,
            payload_digest=wire_attempt.payload_digest,
            target_binding_digest=wire_attempt.target_binding_digest,
            attempt_number=attempt_number,
            issued_at=now,
            expires_at=min(ticket.expires_at, now + self._attempt_ttl),
        )
        try:
            if not await authority_revalidator():
                raise WorkloadTokenExchangeDenied()
            await self._permits.begin_send(
                permit,
                task_id=task_id,
                ticket_id=wire_attempt.authorization_id,
                egress_kind="token_exchange",
                method=wire_attempt.method,
                payload_digest=wire_attempt.payload_digest,
                target_binding_digest=wire_attempt.target_binding_digest,
                now=self._clock(),
            )
            return await self._exchanger.exchange_prepared(
                prepared,
                requested_scopes=ticket.required_scopes,
                now=self._clock(),
            )
        finally:
            await asyncio.shield(self._permits.release(permit.permit_id))

    async def exchange(
        self,
        *,
        ticket: McpInvocationAuthorization,
        task_id: str,
        snapshot: workload_models.WorkloadMetadataSnapshot,
        credential: workload_models.WorkloadCredentialPayload,
        authority_revalidator: Callable[[], Awaitable[bool]],
    ) -> workload_models.WorkloadAccessToken:
        """Retry once only for the exchanger's explicit transient class。"""
        try:
            return await self._attempt(
                attempt_number=1,
                ticket=ticket,
                task_id=task_id,
                snapshot=snapshot,
                credential=credential,
                authority_revalidator=authority_revalidator,
            )
        except WorkloadTokenExchangeRetryable:
            return await self._attempt(
                attempt_number=2,
                ticket=ticket,
                task_id=task_id,
                snapshot=snapshot,
                credential=credential,
                authority_revalidator=authority_revalidator,
            )
