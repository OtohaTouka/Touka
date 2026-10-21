# ruff: noqa: TC002
"""P8-B owner-delegation policy repository and exact P8-A authority resolver。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from filelock import AsyncFileLock  # noqa: FILELOCK_ASYNC_SSOT
from src.protocols.mcp.oauth.models import (
    OAuthAuthorizationSlotKey,
    OAuthGrantRecord,
    OAuthGrantStatus,
)
from src.protocols.mcp.oauth.workload.models import (
    WorkloadDelegationPolicy,
    WorkloadDelegationRecord,
    WorkloadDelegationStatus,
)
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class WorkloadDelegationDenied(PermissionError):
    """The exact JSON, SQLite mirror, and P8-A grant authority did not converge。"""


class WorkloadDelegationConflictError(ValueError):
    """A delegation mutation did not satisfy generation compare-and-swap。"""


class WorkloadDelegationDocumentError(ValueError):
    """The non-secret delegation policy document is absent or malformed。"""


class WorkloadDelegationMirror(Protocol):
    """Minimal durable-mirror seam needed by the resolver。"""

    async def get_active_delegation(
        self,
        *,
        delegation_id: str,
        generation: int,
        digest: str,
        now: float,
    ) -> WorkloadDelegationRecord | None:
        """Return only an exact active mirrored delegation row。"""
        ...

    async def has_blocking_authority_transition(self, **kwargs: object) -> bool:
        """Return whether this delegation has an indeterminate policy mutation。"""
        ...

    async def runtime_authority_ready(self) -> bool:
        """Return whether global workload authority is stable and enabled。"""
        ...


class OAuthGrantAuthority(Protocol):
    """P8-A grant lookup seam; callers cannot bypass its active-slot pointer。"""

    async def get_active_grant(
        self,
        slot: OAuthAuthorizationSlotKey,
        *,
        now: float,
    ) -> OAuthGrantRecord | None:
        """Return the active grant at the exact P8-A slot。"""
        ...


class DelegatedGrantAuthority:
    """Resolved immutable pair used to mint one invocation authorization。"""

    __slots__ = ("delegation", "grant")

    def __init__(
        self,
        *,
        delegation: WorkloadDelegationRecord,
        grant: OAuthGrantRecord,
    ) -> None:
        self.delegation = delegation
        self.grant = grant


class WorkloadDelegationRepository:
    """Atomic ResourceManager JSON repository with generation CAS。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = AsyncFileLock(str(self._path) + ".lock", timeout=30)

    async def load(self) -> WorkloadDelegationPolicy:
        """Load the whole versioned policy; malformed existing files fail closed。"""
        exists = await asyncio.to_thread(resource_manager.exists, self._path)
        raw = await asyncio.to_thread(resource_manager.read_json, self._path)
        if raw is None:
            if exists:
                raise WorkloadDelegationDocumentError()
            return WorkloadDelegationPolicy(
                version="1.0.0",
                author="system",
            )
        try:
            return WorkloadDelegationPolicy.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise WorkloadDelegationDocumentError() from exc

    async def get(self, delegation_id: str) -> WorkloadDelegationRecord | None:
        """Return one exact policy record without mutable cache state。"""
        return (await self.load()).delegations.get(delegation_id)

    async def list_for_owner_agent(
        self,
        *,
        owner_user_id: str,
        execution_agent_id: str,
    ) -> list[WorkloadDelegationRecord]:
        """Project only exact-owner records for one effective agent。"""
        document = await self.load()
        return [
            record
            for _key, record in sorted(document.delegations.items())
            if record.owner_user_id == owner_user_id
            and record.execution_agent_id == execution_agent_id
        ]

    async def list_for_source_slot(
        self,
        source_slot: OAuthAuthorizationSlotKey,
    ) -> list[WorkloadDelegationRecord]:
        """Return every active policy record derived from one exact P8-A slot。"""
        document = await self.load()
        return [
            record
            for _key, record in sorted(document.delegations.items())
            if record.source_slot == source_slot
            and record.status == WorkloadDelegationStatus.ACTIVE
        ]

    async def put(
        self,
        delegation: WorkloadDelegationRecord,
        *,
        version: str,
        author: str,
    ) -> WorkloadDelegationRecord:
        """Create generation one or replace using exact generation+1 CAS。"""
        async with self._lock:
            document = await self.load()
            existing = document.delegations.get(delegation.delegation_id)
            if (
                existing is None
                and delegation.generation != 1
                or existing is not None
                and delegation.generation != existing.generation + 1
            ):
                raise WorkloadDelegationConflictError()
            delegations = dict(document.delegations)
            delegations[delegation.delegation_id] = delegation
            updated = document.model_copy(
                update={
                    "version": version,
                    "author": author,
                    "delegations": delegations,
                }
            )
            await asyncio.to_thread(
                resource_manager.write_json,
                self._path,
                updated.model_dump(mode="json"),
                "utf-8",
                2,
            )
        return delegation

    async def delete(
        self,
        delegation_id: str,
        *,
        expected_generation: int,
        version: str,
        author: str,
    ) -> None:
        """Remove only the exact owner-approved generation after durable fencing。"""
        async with self._lock:
            document = await self.load()
            existing = document.delegations.get(delegation_id)
            if existing is None or existing.generation != expected_generation:
                raise WorkloadDelegationConflictError()
            delegations = dict(document.delegations)
            del delegations[delegation_id]
            updated = document.model_copy(
                update={
                    "version": version,
                    "author": author,
                    "delegations": delegations,
                }
            )
            await asyncio.to_thread(
                resource_manager.write_json,
                self._path,
                updated.model_dump(mode="json"),
                "utf-8",
                2,
            )


class WorkloadDelegationResolver:
    """Require exact agreement across policy, mirror, and the P8-A grant pointer。"""

    def __init__(
        self,
        *,
        policy_lookup: Callable[
            [str],
            WorkloadDelegationRecord | None | Awaitable[WorkloadDelegationRecord | None],
        ],
        mirror: WorkloadDelegationMirror,
        grants: OAuthGrantAuthority,
    ) -> None:
        self._policy_lookup = policy_lookup
        self._mirror = mirror
        self._grants = grants

    async def resolve(
        self,
        *,
        delegation_id: str,
        delegation_digest: str,
        owner_user_id: str,
        execution_agent_id: str,
        installation_id: str,
        installation_generation: int,
        raw_tool_name: str,
        required_scopes: frozenset[str],
        now: float,
    ) -> DelegatedGrantAuthority:
        """Resolve one active authority without widening tool, scope, or generation。"""
        delegation = self._policy_lookup(delegation_id)
        if isawaitable(delegation):
            delegation = await delegation
        if delegation is None or not self._policy_matches(
            delegation,
            delegation_id=delegation_id,
            delegation_digest=delegation_digest,
            owner_user_id=owner_user_id,
            execution_agent_id=execution_agent_id,
            installation_id=installation_id,
            installation_generation=installation_generation,
            raw_tool_name=raw_tool_name,
            required_scopes=required_scopes,
            now=now,
        ):
            raise WorkloadDelegationDenied()

        if (
            not await self._mirror.runtime_authority_ready()
            or await self._mirror.has_blocking_authority_transition(
                authority_kind="delegation",
                authority_id=delegation_id,
            )
        ):
            raise WorkloadDelegationDenied()

        mirror = await self._mirror.get_active_delegation(
            delegation_id=delegation.delegation_id,
            generation=delegation.generation,
            digest=delegation.digest,
            now=now,
        )
        if mirror is None or not hmac.compare_digest(
            mirror.canonical_bytes(),
            delegation.canonical_bytes(),
        ):
            raise WorkloadDelegationDenied()

        grant = await self._grants.get_active_grant(delegation.source_slot, now=now)
        if grant is None or not self._grant_matches(delegation, grant):
            raise WorkloadDelegationDenied()
        return DelegatedGrantAuthority(delegation=delegation, grant=grant)

    @staticmethod
    def _policy_matches(
        delegation: WorkloadDelegationRecord,
        *,
        delegation_id: str,
        delegation_digest: str,
        owner_user_id: str,
        execution_agent_id: str,
        installation_id: str,
        installation_generation: int,
        raw_tool_name: str,
        required_scopes: frozenset[str],
        now: float,
    ) -> bool:
        digest_matches = hmac.compare_digest(delegation.digest, delegation_digest)
        tool_matches = hmac.compare_digest(
            delegation.raw_tool_name.encode("utf-8"),
            raw_tool_name.encode("utf-8"),
        )
        return (
            delegation.status == WorkloadDelegationStatus.ACTIVE
            and delegation.issued_at <= now < delegation.expires_at
            and delegation.delegation_id == delegation_id
            and digest_matches
            and delegation.owner_user_id == owner_user_id
            and delegation.execution_agent_id == execution_agent_id
            and delegation.installation_id == installation_id
            and delegation.installation_generation == installation_generation
            and tool_matches
            and delegation.required_scopes == required_scopes
        )

    @staticmethod
    def _grant_matches(
        delegation: WorkloadDelegationRecord,
        grant: OAuthGrantRecord,
    ) -> bool:
        return (
            grant.status in {OAuthGrantStatus.ACTIVE, OAuthGrantStatus.REFRESHING}
            and grant.grant_id == delegation.source_grant_id
            and grant.grant_version >= delegation.source_grant_version
            and grant.binding.profile_generation == delegation.source_profile_generation
            and delegation.required_scopes.issubset(grant.binding.granted_scopes)
            and delegation.required_scopes.issubset(grant.tokens.granted_scopes)
            and grant.tokens.token_type.lower() == "bearer"
        )


def delegation_document_fingerprint(document: WorkloadDelegationPolicy) -> str:
    """Hash a canonical non-secret policy snapshot for audit bindings。"""
    encoded = document.model_dump_json(
        exclude_none=True,
        by_alias=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
