# ruff: noqa: TC002
"""Disjoint direct and owner-delegated P8-B authority router。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from src.protocols.mcp.canonical import McpInvocationAuthorization
from src.protocols.mcp.oauth.workload.runtime import (
    WorkloadAuthorityMaterial,
    WorkloadDelegatedAuthorityMaterial,
)
from src.protocols.registry import ProtocolServerConfig

if TYPE_CHECKING:
    from collections.abc import Callable


class WorkloadAuthorityRouteDenied(PermissionError):
    """Installation configuration and immutable ticket authority disagree。"""


class RoutedWorkloadAuthorizer(Protocol):
    """Common direct/delegated authorizer contract。"""

    async def authorize_operation(self, **kwargs: Any) -> McpInvocationAuthorization:
        """Mint one exact ticket。"""
        ...

    async def revalidate(
        self,
        ticket: McpInvocationAuthorization,
        *,
        now: float,
    ) -> WorkloadAuthorityMaterial | WorkloadDelegatedAuthorityMaterial:
        """Revalidate one immutable ticket。"""
        ...


class WorkloadAuthorityRouter:
    """Select exactly one authority branch and reject runtime mode changes。"""

    def __init__(
        self,
        *,
        config_lookup: Callable[[str], ProtocolServerConfig | None],
        direct: RoutedWorkloadAuthorizer,
        delegated: RoutedWorkloadAuthorizer,
    ) -> None:
        self._config_lookup = config_lookup
        self._direct = direct
        self._delegated = delegated

    def _branch(
        self,
        server_name: str,
        authority_kind: str | None = None,
    ) -> RoutedWorkloadAuthorizer:
        config = self._config_lookup(server_name)
        if config is None or config.auth_mode != "oauth_client_credentials":
            raise WorkloadAuthorityRouteDenied()
        if config.workload_authority_kind == "direct":
            if authority_kind not in {None, "workload"}:
                raise WorkloadAuthorityRouteDenied()
            return self._direct
        if config.workload_authority_kind == "owner_delegation":
            if authority_kind not in {None, "delegation"}:
                raise WorkloadAuthorityRouteDenied()
            return self._delegated
        raise WorkloadAuthorityRouteDenied()

    async def authorize_operation(self, **kwargs: Any) -> McpInvocationAuthorization:
        """Route initial authorization by current explicit installation config。"""
        server_name = kwargs.get("server_name")
        if not isinstance(server_name, str):
            raise WorkloadAuthorityRouteDenied()
        return await self._branch(server_name).authorize_operation(**kwargs)

    async def revalidate(
        self,
        ticket: McpInvocationAuthorization,
        *,
        now: float,
    ) -> WorkloadAuthorityMaterial | WorkloadDelegatedAuthorityMaterial:
        """Require the current config to retain the immutable ticket branch。"""
        return await self._branch(
            ticket.installation_id,
            ticket.authority_kind,
        ).revalidate(ticket, now=now)
