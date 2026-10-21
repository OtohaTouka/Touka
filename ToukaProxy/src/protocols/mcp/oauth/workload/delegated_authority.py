# ruff: noqa: TC002
"""Exact owner-delegated P8-A grant authorization for heartbeat MCP calls。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from src.protocols.mcp.canonical import (
    ExecutionKind,
    McpInvocationAuthorization,
    McpRequestContext,
    digest_mcp_arguments,
)
from src.protocols.mcp.oauth.workload.authority import (
    WorkloadOwnerAuthority,
    WorkloadPolicyAuthority,
)
from src.protocols.mcp.oauth.workload.delegation import (
    DelegatedGrantAuthority,
    WorkloadDelegationResolver,
)
from src.protocols.mcp.oauth.workload.models import WorkloadDelegationRecord
from src.protocols.mcp.oauth.workload.runtime import WorkloadDelegatedAuthorityMaterial
from src.protocols.mcp.oauth.workload.target import (
    McpOAuthTargetBinding,
    McpOAuthTargetBindingError,
    build_mcp_oauth_target_binding,
    canonical_mcp_transport_url,
)
from src.protocols.registry import ProtocolServerConfig

if TYPE_CHECKING:
    from collections.abc import Callable


class WorkloadDelegatedAuthorityDenied(PermissionError):
    """Current owner, policy, delegation, mirror, or P8-A grant denied the ticket。"""


class WorkloadDelegationAuthority(Protocol):
    """Current non-secret owner-delegation policy lookup seam。"""

    async def get(self, delegation_id: str) -> WorkloadDelegationRecord | None:
        """Return one current delegation record。"""
        ...


class DelegationResolver(Protocol):
    """Exact JSON＋SQLite＋P8-A authority convergence seam。"""

    async def resolve(self, **kwargs: Any) -> DelegatedGrantAuthority:
        """Resolve one current source authority or fail closed。"""
        ...


class WorkloadDelegatedAuthorizer:
    """Mint tickets backed by an exact owner delegation of one P8-A grant。"""

    def __init__(
        self,
        *,
        config_lookup: Callable[[str], ProtocolServerConfig | None],
        installation_generation_lookup: Callable[[str], int],
        delegations: WorkloadDelegationAuthority,
        resolver: WorkloadDelegationResolver | DelegationResolver,
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
        self._delegations = delegations
        self._resolver = resolver
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
            or config.workload_authority_kind != "owner_delegation"
            or config.workload_delegation_id is None
            or config.workload_source_installation_id is None
            or config.auth_profile_id is None
        ):
            raise WorkloadDelegatedAuthorityDenied()
        return config

    def _source_config(self, target: ProtocolServerConfig) -> ProtocolServerConfig:
        source_id = target.workload_source_installation_id
        source = self._config_lookup(source_id or "")
        try:
            target_url = canonical_mcp_transport_url(target.url or "")
            source_url = canonical_mcp_transport_url(source.url if source else "")
        except McpOAuthTargetBindingError as exc:
            raise WorkloadDelegatedAuthorityDenied() from exc
        if (
            source_id is None
            or source is None
            or not source.enabled
            or not source.is_remote
            or source.auth_mode != "oauth_user"
            or source.auth_profile_id != target.auth_profile_id
            or source_url != target_url
        ):
            raise WorkloadDelegatedAuthorityDenied()
        return source

    @staticmethod
    def _target_binding(
        *,
        config: ProtocolServerConfig,
        authority: DelegatedGrantAuthority,
        installation_id: str,
        installation_generation: int,
    ) -> McpOAuthTargetBinding:
        grant = authority.grant
        try:
            return build_mcp_oauth_target_binding(
                expected_resource=grant.binding.resource,
                expected_transport_url=config.url or "",
                configured_transport_url=config.url,
                profile_id=authority.delegation.source_slot.auth_profile_id,
                profile_generation=grant.binding.profile_generation,
                profile_fingerprint=grant.binding.profile_fingerprint,
                installation_id=installation_id,
                installation_generation=installation_generation,
            )
        except McpOAuthTargetBindingError as exc:
            raise WorkloadDelegatedAuthorityDenied() from exc

    async def _resolve(
        self,
        *,
        config: ProtocolServerConfig,
        owner_user_id: str,
        execution_agent_id: str,
        installation_id: str,
        installation_generation: int,
        raw_tool_name: str,
        now: float,
        expected_ticket: McpInvocationAuthorization | None = None,
    ) -> tuple[WorkloadDelegationRecord, DelegatedGrantAuthority]:
        delegation_id = config.workload_delegation_id
        source_id = config.workload_source_installation_id
        source_config = self._source_config(config)
        if (
            delegation_id is None
            or config.auth_profile_id is None
            or source_id is None
        ):
            raise WorkloadDelegatedAuthorityDenied()
        source_generation = self._installation_generation_lookup(source_id)
        delegation = await self._delegations.get(delegation_id)
        if (
            delegation is None
            or delegation.owner_user_id != owner_user_id
            or delegation.execution_agent_id != execution_agent_id
            or delegation.installation_id != installation_id
            or delegation.installation_generation != installation_generation
            or delegation.raw_tool_name != raw_tool_name
            or delegation.source_slot.installation_id != source_id
            or delegation.source_slot.installation_generation != source_generation
            or delegation.source_slot.auth_profile_id != source_config.auth_profile_id
            or delegation.source_slot.auth_profile_id != config.auth_profile_id
            or delegation.source_profile_generation < 1
            or now < delegation.issued_at
            or now >= delegation.expires_at
        ):
            raise WorkloadDelegatedAuthorityDenied()
        if expected_ticket is not None and (
            delegation.delegation_id != config.workload_delegation_id
            or delegation.digest != expected_ticket.delegation_digest
            or delegation.generation != expected_ticket.delegation_generation
            or delegation.required_scopes != expected_ticket.required_scopes
            or delegation.source_profile_generation
            != expected_ticket.profile_generation
            or delegation.source_slot.auth_profile_id
            != expected_ticket.auth_profile_id
        ):
            raise WorkloadDelegatedAuthorityDenied()
        authority = await self._resolver.resolve(
            delegation_id=delegation.delegation_id,
            delegation_digest=delegation.digest,
            owner_user_id=owner_user_id,
            execution_agent_id=execution_agent_id,
            installation_id=installation_id,
            installation_generation=installation_generation,
            raw_tool_name=raw_tool_name,
            required_scopes=delegation.required_scopes,
            now=now,
        )
        if (
            authority.delegation.digest != delegation.digest
            or authority.grant.grant_id != delegation.source_grant_id
            or authority.grant.grant_version < delegation.source_grant_version
        ):
            raise WorkloadDelegatedAuthorityDenied()
        return delegation, authority

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
        """Create a short-lived ticket only after all delegated authorities agree。"""
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
            raise WorkloadDelegatedAuthorityDenied()
        agent_id = request_context.identity.agent_id
        owner_id = await self._owners.active_owner_for_agent(agent_id)
        if owner_id is None:
            raise WorkloadDelegatedAuthorityDenied()
        now = self._clock()
        delegation, authority = await self._resolve(
            config=config,
            owner_user_id=owner_id,
            execution_agent_id=agent_id,
            installation_id=server_name,
            installation_generation=installation_generation,
            raw_tool_name=selector,
            now=now,
        )
        target_binding = self._target_binding(
            config=config,
            authority=authority,
            installation_id=server_name,
            installation_generation=installation_generation,
        )
        policy_digest = await self._policy.authorize(
            request_context=request_context,
            server_name=server_name,
            raw_tool_name=selector,
            required_scopes=delegation.required_scopes,
        )
        return McpInvocationAuthorization(
            authorization_id=str(uuid4()),
            authority_kind="delegation",
            owner_user_id=owner_id,
            human_principal_id=owner_id,
            delegation_digest=delegation.digest,
            delegation_generation=delegation.generation,
            execution_agent_id=agent_id,
            execution_kind=request_context.execution_kind,
            origin=request_context.origin,
            installation_id=server_name,
            installation_generation=installation_generation,
            auth_profile_id=delegation.source_slot.auth_profile_id,
            profile_generation=delegation.source_profile_generation,
            target_binding_digest=target_binding.digest,
            credential_generation=None,
            operation_method="tools/call",
            raw_tool_name=selector,
            arguments_digest=digest_mcp_arguments(arguments),
            required_scopes=delegation.required_scopes,
            auth_level=request_context.auth_level,
            policy_digest=policy_digest,
            request_id=request_context.request_id,
            issued_at=now,
            expires_at=min(now + self._ticket_ttl, delegation.expires_at),
        )

    async def revalidate(
        self,
        ticket: McpInvocationAuthorization,
        *,
        now: float,
    ) -> WorkloadDelegatedAuthorityMaterial:
        """Recheck owner, policy, delegation mirror, and source grant before send。"""
        config = self._config(ticket.installation_id)
        if (
            ticket.authority_kind != "delegation"
            or ticket.execution_kind != ExecutionKind.HEARTBEAT
            or ticket.origin != "heartbeat"
            or ticket.operation_method != "tools/call"
            or now >= ticket.expires_at
            or config.auth_profile_id != ticket.auth_profile_id
            or not config.allows_tool(ticket.raw_tool_name)
            or self._installation_generation_lookup(ticket.installation_id)
            != ticket.installation_generation
        ):
            raise WorkloadDelegatedAuthorityDenied()
        owner_id = await self._owners.active_owner_for_agent(ticket.execution_agent_id)
        if owner_id != ticket.owner_user_id or owner_id != ticket.human_principal_id:
            raise WorkloadDelegatedAuthorityDenied()
        policy_digest = await self._policy.revalidate(ticket=ticket, now=now)
        if policy_digest != ticket.policy_digest:
            raise WorkloadDelegatedAuthorityDenied()
        delegation, authority = await self._resolve(
            config=config,
            owner_user_id=owner_id,
            execution_agent_id=ticket.execution_agent_id,
            installation_id=ticket.installation_id,
            installation_generation=ticket.installation_generation,
            raw_tool_name=ticket.raw_tool_name,
            now=now,
            expected_ticket=ticket,
        )
        target_binding = self._target_binding(
            config=config,
            authority=authority,
            installation_id=ticket.installation_id,
            installation_generation=ticket.installation_generation,
        )
        if target_binding.digest != ticket.target_binding_digest:
            raise WorkloadDelegatedAuthorityDenied()
        return WorkloadDelegatedAuthorityMaterial(
            authority_key=ticket.handle_principal_key,
            source_slot=delegation.source_slot,
            source_grant_id=authority.grant.grant_id,
            source_grant_version=authority.grant.grant_version,
            target_binding=target_binding,
        )
