# ruff: noqa: TC002
"""Production adapters for current owner, RBAC, auth-level, and PaC authority。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any, Protocol

from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext
from src.protocols.mcp.canonical import McpInvocationAuthorization, McpRequestContext

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


class UserAuthoritySource(Protocol):
    """Read-only users.json authority seam。"""

    def get_all_users(self) -> list[Any]:
        """Return current strongly validated users。"""
        ...


class WorkloadCurrentPolicyDenied(PermissionError):
    """Current identity, RBAC, headless auth-level, or PaC did not allow egress。"""


class CurrentOwnerAuthority:
    """Resolve one active explicit owner without an admin shortcut。"""

    def __init__(self, *, auth_service: UserAuthoritySource) -> None:
        self._auth_service = auth_service

    async def active_owner_for_agent(self, agent_id: str) -> str | None:
        """Fail closed for zero, suspended, or ambiguous active owners。"""
        users = await asyncio.to_thread(self._auth_service.get_all_users)
        owners = [
            user.id
            for user in users
            if bool(user.is_active) and agent_id in user.owned_agents
        ]
        return owners[0] if len(owners) == 1 else None


class CurrentWorkloadPolicyAuthority:
    """Canonical current-state policy evaluator shared by both authority branches。"""

    def __init__(
        self,
        *,
        identity_lookup: Callable[[str], AgentIdentity],
        mcp_tool_allowed: Callable[[str, str, AgentIdentity], bool],
        policy_evaluate: Callable[[PolicyContext], Any],
        policy_snapshot: Callable[[], Mapping[str, Any]],
    ) -> None:
        self._identity_lookup = identity_lookup
        self._mcp_tool_allowed = mcp_tool_allowed
        self._policy_evaluate = policy_evaluate
        self._policy_snapshot = policy_snapshot

    def _evaluate(
        self,
        *,
        execution_agent_id: str,
        auth_level: str,
        server_name: str,
        raw_tool_name: str,
        required_scopes: frozenset[str],
    ) -> str:
        identity = self._identity_lookup(execution_agent_id)
        if (
            identity.agent_id != execution_agent_id
            or not self._mcp_tool_allowed(server_name, raw_tool_name, identity)
        ):
            raise WorkloadCurrentPolicyDenied()
        context = PolicyContext(
            actor=identity,
            resource=raw_tool_name,
            action="tool_call",
            environment={
                "auth_level": auth_level,
                "mcp_server": server_name,
                "request_origin": "heartbeat",
            },
        )
        decision = self._policy_evaluate(context)
        if getattr(decision, "effect", None) != "ALLOW":
            raise WorkloadCurrentPolicyDenied()
        evidence = {
            "auth_level": auth_level,
            "decision": {
                "effect": decision.effect,
                "metadata": getattr(decision, "metadata", {}),
                "reason": getattr(decision, "reason", None),
            },
            "identity": identity.model_dump(mode="json"),
            "policy": self._policy_snapshot(),
            "required_scopes": sorted(required_scopes),
            "server_name": server_name,
            "tool_name": raw_tool_name,
        }
        encoded = json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    async def authorize(
        self,
        *,
        request_context: McpRequestContext,
        server_name: str,
        raw_tool_name: str,
        required_scopes: frozenset[str],
    ) -> str:
        """Authorize an initial exact heartbeat operation。"""
        if request_context.origin != "heartbeat":
            raise WorkloadCurrentPolicyDenied()
        return self._evaluate(
            execution_agent_id=request_context.identity.agent_id,
            auth_level=request_context.auth_level,
            server_name=server_name,
            raw_tool_name=raw_tool_name,
            required_scopes=required_scopes,
        )

    async def revalidate(
        self,
        *,
        ticket: McpInvocationAuthorization,
        now: float,
    ) -> str:
        """Re-evaluate current RBAC/PaC; time is checked by the authorizer。"""
        if now >= ticket.expires_at or ticket.origin != "heartbeat":
            raise WorkloadCurrentPolicyDenied()
        return self._evaluate(
            execution_agent_id=ticket.execution_agent_id,
            auth_level=ticket.auth_level,
            server_name=ticket.installation_id,
            raw_tool_name=ticket.raw_tool_name,
            required_scopes=ticket.required_scopes,
        )
