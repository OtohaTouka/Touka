# ruff: noqa: TC002
"""Exact direct-workload authorization and durable revalidation。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from src.protocols.mcp.canonical import (
    ExecutionKind,
    McpInvocationAuthorization,
    McpRequestContext,
    digest_mcp_arguments,
)
from src.protocols.mcp.oauth.workload.models import (
    WorkloadCredentialPayload,
    WorkloadMetadataSnapshot,
    WorkloadOAuthProfile,
)
from src.protocols.mcp.oauth.workload.runtime import WorkloadAuthorityMaterial
from src.protocols.mcp.oauth.workload.target import (
    McpOAuthTargetBinding,
    McpOAuthTargetBindingError,
    build_mcp_oauth_target_binding,
)
from src.protocols.registry import ProtocolServerConfig

if TYPE_CHECKING:
    from collections.abc import Callable


class WorkloadAuthorityDenied(PermissionError):
    """Current owner, policy, profile, credential, or metadata denied the ticket。"""


class WorkloadProfileAuthority(Protocol):
    """Non-secret profile lookup seam。"""

    async def get(self, profile_id: str) -> WorkloadOAuthProfile | None:
        """Return one current profile generation。"""
        ...


class WorkloadDurableAuthority(Protocol):
    """Exact metadata and credential mirror lookup seam。"""

    async def get_metadata_snapshot(self, **kwargs: Any) -> WorkloadMetadataSnapshot | None:
        """Return the exact active metadata snapshot。"""
        ...

    async def get_active_profile_credential(
        self,
        **kwargs: Any,
    ) -> WorkloadCredentialPayload | None:
        """Return the sole active explicitly named credential generation。"""
        ...

    async def has_blocking_authority_transition(self, **kwargs: Any) -> bool:
        """Return whether this profile has an indeterminate control mutation。"""
        ...

    async def runtime_authority_ready(self) -> bool:
        """Return whether global state permits new workload authority。"""
        ...


class WorkloadOwnerAuthority(Protocol):
    """Active human owner lookup seam。"""

    async def active_owner_for_agent(self, agent_id: str) -> str | None:
        """Return the sole active owner or fail closed with None。"""
        ...


class WorkloadPolicyAuthority(Protocol):
    """Current MCP RBAC, auth-level, and PaC digest authority。"""

    async def authorize(self, **kwargs: Any) -> str:
        """Authorize the initial exact operation and return its policy digest。"""
        ...

    async def revalidate(self, **kwargs: Any) -> str:
        """Re-evaluate the immutable ticket against current policy。"""
        ...


class WorkloadDirectAuthorizer:
    """Mint direct client-credentials tickets and re-resolve every durable binding。"""

    def __init__(
        self,
        *,
        config_lookup: Callable[[str], ProtocolServerConfig | None],
        installation_generation_lookup: Callable[[str], int],
        profiles: WorkloadProfileAuthority,
        store: WorkloadDurableAuthority,
        owners: WorkloadOwnerAuthority,
        policy: WorkloadPolicyAuthority,
        feature_enabled: Callable[[], bool],
        clock: Callable[[], float],
        ticket_ttl: float,
    ) -> None:
        if ticket_ttl <= 0:
            raise ValueError()
        self._config_lookup = config_lookup
        self._installation_generation_lookup = installation_generation_lookup
        self._profiles = profiles
        self._store = store
        self._owners = owners
        self._policy = policy
        self._feature_enabled = feature_enabled
        self._clock = clock
        self._ticket_ttl = ticket_ttl

    def _config(self, server_name: str) -> ProtocolServerConfig:
        config = self._config_lookup(server_name)
        if (
            not self._feature_enabled()
            or config is None
            or not config.enabled
            or not config.is_remote
            or config.auth_mode != "oauth_client_credentials"
            or config.workload_authority_kind != "direct"
            or config.auth_profile_id is None
        ):
            raise WorkloadAuthorityDenied()
        return config

    async def _profile(
        self,
        *,
        profile_id: str,
        owner_user_id: str,
        execution_agent_id: str,
        raw_tool_name: str,
    ) -> tuple[WorkloadOAuthProfile, frozenset[str]]:
        profile = await self._profiles.get(profile_id)
        scopes = profile.tool_scopes.get(raw_tool_name) if profile is not None else None
        if (
            profile is None
            or not profile.enabled
            or profile.owner_user_id != owner_user_id
            or profile.execution_agent_id != execution_agent_id
            or scopes is None
            or not scopes
        ):
            raise WorkloadAuthorityDenied()
        return profile, scopes

    async def _material(
        self,
        *,
        ticket: McpInvocationAuthorization,
        profile: WorkloadOAuthProfile,
        config: ProtocolServerConfig,
    ) -> WorkloadAuthorityMaterial:
        try:
            target_binding = self._target_binding(
                ticket=ticket,
                profile=profile,
                config=config,
            )
        except McpOAuthTargetBindingError as exc:
            raise WorkloadAuthorityDenied() from exc
        snapshot = await self._store.get_metadata_snapshot(
            profile_id=profile.profile_id,
            profile_generation=profile.generation,
            profile_fingerprint=profile.fingerprint,
        )
        credential = await self._store.get_active_profile_credential(
            profile_id=profile.profile_id,
            profile_generation=profile.generation,
            credential_id=profile.credential_id,
        )
        if (
            not await self._store.runtime_authority_ready()
            or await self._store.has_blocking_authority_transition(
                authority_kind="profile",
                authority_id=profile.profile_id,
            )
            or snapshot is None
            or credential is None
            or snapshot.profile_generation != ticket.profile_generation
            or snapshot.profile_fingerprint != profile.fingerprint
            or snapshot.private_network_cidrs != profile.private_network_cidrs
            or snapshot.metadata_security_fingerprint == ""
            or credential.generation != ticket.credential_generation
            or credential.profile_id != profile.profile_id
            or credential.credential_id != profile.credential_id
            or not ticket.required_scopes.issubset(snapshot.verified_scopes)
            or target_binding.digest != ticket.target_binding_digest
        ):
            raise WorkloadAuthorityDenied()
        return WorkloadAuthorityMaterial(
            authority_key=ticket.handle_principal_key,
            snapshot=snapshot,
            credential=credential,
            target_binding=target_binding,
            allowed_private_cidrs=profile.private_network_cidrs,
        )

    @staticmethod
    def _target_binding(
        *,
        ticket: McpInvocationAuthorization,
        profile: WorkloadOAuthProfile,
        config: ProtocolServerConfig,
    ) -> McpOAuthTargetBinding:
        return build_mcp_oauth_target_binding(
            expected_resource=profile.expected_resource,
            expected_transport_url=profile.expected_transport_url,
            configured_transport_url=config.url,
            profile_id=profile.profile_id,
            profile_generation=profile.generation,
            profile_fingerprint=profile.fingerprint,
            installation_id=ticket.installation_id,
            installation_generation=ticket.installation_generation,
        )

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
        """Create one short-lived ticket only after all current authorities agree。"""
        config = self._config(server_name)
        if (
            request_context.execution_kind != ExecutionKind.HEARTBEAT
            or request_context.origin != "heartbeat"
            or request_context.installation_id != server_name
            or operation != "tools/call"
            or installation_generation
            != self._installation_generation_lookup(server_name)
            or not config.allows_tool(selector)
        ):
            raise WorkloadAuthorityDenied()
        agent_id = request_context.identity.agent_id
        owner_id = await self._owners.active_owner_for_agent(agent_id)
        if owner_id is None:
            raise WorkloadAuthorityDenied()
        profile, required_scopes = await self._profile(
            profile_id=config.auth_profile_id,
            owner_user_id=owner_id,
            execution_agent_id=agent_id,
            raw_tool_name=selector,
        )
        policy_digest = await self._policy.authorize(
            request_context=request_context,
            server_name=server_name,
            raw_tool_name=selector,
            required_scopes=required_scopes,
        )
        now = self._clock()
        credential = await self._store.get_active_profile_credential(
            profile_id=profile.profile_id,
            profile_generation=profile.generation,
            credential_id=profile.credential_id,
        )
        if credential is None:
            raise WorkloadAuthorityDenied()
        try:
            provisional_target = build_mcp_oauth_target_binding(
                expected_resource=profile.expected_resource,
                expected_transport_url=profile.expected_transport_url,
                configured_transport_url=config.url,
                profile_id=profile.profile_id,
                profile_generation=profile.generation,
                profile_fingerprint=profile.fingerprint,
                installation_id=server_name,
                installation_generation=installation_generation,
            )
        except McpOAuthTargetBindingError as exc:
            raise WorkloadAuthorityDenied() from exc
        ticket = McpInvocationAuthorization(
            authorization_id=str(uuid4()),
            authority_kind="workload",
            workload_principal_id=profile.workload_principal_id,
            owner_user_id=owner_id,
            execution_agent_id=agent_id,
            execution_kind=request_context.execution_kind,
            origin=request_context.origin,
            installation_id=server_name,
            installation_generation=installation_generation,
            auth_profile_id=profile.profile_id,
            profile_generation=profile.generation,
            target_binding_digest=provisional_target.digest,
            credential_generation=credential.generation,
            operation_method="tools/call",
            raw_tool_name=selector,
            arguments_digest=digest_mcp_arguments(arguments),
            required_scopes=required_scopes,
            auth_level=request_context.auth_level,
            policy_digest=policy_digest,
            request_id=request_context.request_id,
            issued_at=now,
            expires_at=now + self._ticket_ttl,
        )
        await self._material(ticket=ticket, profile=profile, config=config)
        return ticket

    async def revalidate(
        self,
        ticket: McpInvocationAuthorization,
        *,
        now: float,
    ) -> WorkloadAuthorityMaterial:
        """Resolve profile, owner, policy, metadata, and credential for every send。"""
        config = self._config(ticket.installation_id)
        if (
            ticket.authority_kind != "workload"
            or ticket.execution_kind != ExecutionKind.HEARTBEAT
            or ticket.origin != "heartbeat"
            or ticket.operation_method != "tools/call"
            or now >= ticket.expires_at
            or config.auth_profile_id != ticket.auth_profile_id
            or not config.allows_tool(ticket.raw_tool_name)
            or self._installation_generation_lookup(ticket.installation_id)
            != ticket.installation_generation
        ):
            raise WorkloadAuthorityDenied()
        owner_id = await self._owners.active_owner_for_agent(ticket.execution_agent_id)
        if owner_id != ticket.owner_user_id:
            raise WorkloadAuthorityDenied()
        profile, required_scopes = await self._profile(
            profile_id=ticket.auth_profile_id,
            owner_user_id=ticket.owner_user_id,
            execution_agent_id=ticket.execution_agent_id,
            raw_tool_name=ticket.raw_tool_name,
        )
        policy_digest = await self._policy.revalidate(ticket=ticket, now=now)
        if (
            profile.generation != ticket.profile_generation
            or profile.workload_principal_id != ticket.workload_principal_id
            or required_scopes != ticket.required_scopes
            or policy_digest != ticket.policy_digest
        ):
            raise WorkloadAuthorityDenied()
        return await self._material(ticket=ticket, profile=profile, config=config)
