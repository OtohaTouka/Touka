# ruff: noqa: TC002
"""Recovery-aware P8-B admin and owner control-plane orchestration。"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import SecretStr
from src.models.mcp_oauth_workload import (
    WorkloadCredentialStatus,
    WorkloadMetadataStatus,
    WorkloadMutationResponse,
    WorkloadRecoveryItem,
)
from src.protocols.mcp.oauth.models import OAuthAuthorizationSlotKey, OAuthKeyRotationPhase
from src.protocols.mcp.oauth.outbound import validate_oauth_endpoint
from src.protocols.mcp.oauth.workload.metadata import verify_workload_metadata
from src.protocols.mcp.oauth.workload.models import (
    WorkloadAuditEvent,
    WorkloadCredentialPayload,
    WorkloadDelegationRecord,
    WorkloadDelegationStatus,
    WorkloadMetadataSnapshot,
    WorkloadMutationKind,
    WorkloadOAuthProfile,
    WorkloadRevocationRecord,
    WorkloadRevocationStatus,
)
from src.protocols.mcp.oauth.workload.mutation import (
    WorkloadAuthorityMutationCoordinator,
    WorkloadMutationRecoveryRequired,
)
from src.protocols.mcp.oauth.workload.profile import WorkloadProfileRepository
from src.protocols.mcp.oauth.workload.target import (
    McpOAuthTargetBindingError,
    canonical_mcp_transport_url,
    resource_and_transport_share_origin,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from src.protocols.mcp.oauth.workload.delegation import (
        WorkloadDelegationRepository,
    )
    from src.protocols.mcp.oauth.workload.store import WorkloadOAuthStore
    from src.protocols.registry import ProtocolServerConfig


class WorkloadControlDenied(PermissionError):
    """The current admin/owner/source authority did not match the mutation。"""


class WorkloadControlRecoveryRequired(RuntimeError):
    """A durable intent remains fenced and needs exact operator recovery。"""


class WorkloadControlOwners(Protocol):
    """Resolve the current active human owner for an execution agent。"""

    async def active_owner_for_agent(self, agent_id: str) -> str | None:
        """Return the current active owner or None。"""
        ...


class WorkloadSourceGrants(Protocol):
    """Read only the exact active P8-A source authorization slot。"""

    async def get_active_grant(
        self,
        slot: OAuthAuthorizationSlotKey,
        *,
        now: float,
    ) -> Any:
        """Return only the active grant at the exact slot。"""
        ...


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _source_slot_digest(slot: OAuthAuthorizationSlotKey) -> str:
    """Return the canonical, non-secret index for one exact P8-A grant slot。"""
    return f"sha256:{hashlib.sha256(slot.canonical_bytes()).hexdigest()}"


def _well_known_url(origin_url: str, suffix: str) -> str:
    parsed = urlsplit(validate_oauth_endpoint(origin_url))
    path = parsed.path if parsed.path != "/" else ""
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/{suffix}{path}"


class WorkloadOAuthControlService:
    """Coordinate JSON policy, encrypted SQLite mirror, audit, and owner fences。"""

    def __init__(
        self,
        *,
        profiles: WorkloadProfileRepository,
        delegations: WorkloadDelegationRepository,
        store: WorkloadOAuthStore,
        outbound: Any,
        permits: Any,
        runtime: Any,
        owners: WorkloadControlOwners,
        source_grants: WorkloadSourceGrants | None,
        config_lookup: Callable[[str], ProtocolServerConfig | None],
        installation_generation_lookup: Callable[[str], int],
        close_handles: Callable[[str], Awaitable[Any]],
        close_all_handles: Callable[[], Awaitable[Any]] | None = None,
        clock: Callable[[], float],
        revoke_timeout: float = 5.0,
        mutation_lock: asyncio.Lock | None = None,
        mutation_coordinator: WorkloadAuthorityMutationCoordinator | None = None,
    ) -> None:
        if revoke_timeout <= 0:
            raise ValueError()
        self._profiles = profiles
        self._delegations = delegations
        self._store = store
        self._outbound = outbound
        self._permits = permits
        self._runtime = runtime
        self._owners = owners
        self._source_grants = source_grants
        self._config_lookup = config_lookup
        self._installation_generation_lookup = installation_generation_lookup
        self._close_handles = close_handles
        self._close_all_handles = close_all_handles
        self._clock = clock
        self._revoke_timeout = revoke_timeout
        self._mutation_coordinator = mutation_coordinator or (
            WorkloadAuthorityMutationCoordinator(
                permits=permits,
                store=store,
                lock=mutation_lock,
            )
        )
        self._source_authority_lock = asyncio.Lock()
        self._pending_source_disconnects: set[bytes] = set()

    async def _begin_global_fence(
        self,
        *,
        operation_id: str,
        mutation_kind: WorkloadMutationKind,
    ) -> str:
        try:
            return await self._mutation_coordinator.begin(
                operation_id=operation_id,
                mutation_kind=mutation_kind.value,
            )
        except WorkloadMutationRecoveryRequired as exc:
            raise WorkloadControlRecoveryRequired() from exc

    async def _commit_epoch_fence(
        self,
        initial_epoch: int,
        *,
        fence_id: str,
        actor_id: str,
        request_id: str,
        recovery_authority_id: str,
    ) -> bool:
        """Publish one durable epoch advance, invalidate runtime, and drain sends。"""
        current_epoch = await self._store.authority_epoch()
        if current_epoch <= initial_epoch:
            await self._mutation_coordinator.abort(fence_id=fence_id)
            raise WorkloadControlRecoveryRequired()
        tasks = await self._mutation_coordinator.commit(
            fence_id=fence_id,
            new_authority_epoch=current_epoch
        )
        self._runtime.invalidate_all_authorities()
        drained = await self._drain_all_egress(tasks)
        if not drained:
            mutation_operation_id = self._mutation_coordinator.pending_operation_id
            if mutation_operation_id is None:
                raise WorkloadControlRecoveryRequired()
            await self._record_pending_drain(
                authority_key=f"global:{current_epoch}",
                authority_id=recovery_authority_id,
                authority_generation=current_epoch,
                mutation_operation_id=mutation_operation_id,
                actor_id=actor_id,
                request_id=request_id,
            )
        else:
            await self._mutation_coordinator.complete(fence_id=fence_id)
        return drained

    async def _run_epoch_mutation(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        actor_id: str,
        request_id: str,
        recovery_authority_id: str,
        mutation_kind: WorkloadMutationKind,
    ) -> tuple[Any, bool]:
        """Serialize every epoch mutation behind the same permit admission fence。"""
        async with self._mutation_coordinator.serialized():
            if not await self._store.runtime_authority_ready():
                raise WorkloadControlRecoveryRequired()
            fence_id = await self._begin_global_fence(
                operation_id=str(uuid4()),
                mutation_kind=mutation_kind,
            )
            initial_epoch = await self._store.authority_epoch()
            try:
                result = await operation()
            except BaseException:
                current_epoch = await self._store.authority_epoch()
                if current_epoch == initial_epoch:
                    await self._mutation_coordinator.abort(fence_id=fence_id)
                elif current_epoch > initial_epoch:
                    await self._commit_epoch_fence(
                        initial_epoch,
                        fence_id=fence_id,
                        actor_id=actor_id,
                        request_id=request_id,
                        recovery_authority_id=recovery_authority_id,
                    )
                raise
            drained = await self._commit_epoch_fence(
                initial_epoch,
                fence_id=fence_id,
                actor_id=actor_id,
                request_id=request_id,
                recovery_authority_id=recovery_authority_id,
            )
            return result, drained

    async def _record_pending_drain(
        self,
        *,
        authority_key: str,
        authority_id: str,
        authority_generation: int,
        mutation_operation_id: str,
        actor_id: str,
        request_id: str,
    ) -> None:
        """Persist one bounded timeout so restart and admin recovery stay fail-closed。"""
        details = _digest(
            {
                "authority_id": authority_id,
                "generation": authority_generation,
                "status": WorkloadRevocationStatus.REVOCATION_PENDING.value,
            }
        )
        await self._store.record_pending_revocation(
            authority_key=authority_key,
            authority_generation=authority_generation,
            authority_id=authority_id,
            mutation_operation_id=mutation_operation_id,
            now=self._clock(),
            event=self._event(
                action="authority.drain.revocation_pending",
                actor_id=actor_id,
                authority_id=authority_id,
                authority_generation=authority_generation,
                request_id=request_id,
                outcome="failure",
                details_digest=details,
                now=self._clock(),
            ),
        )

    async def _drain_all_egress(self, task_ids: frozenset[str]) -> bool:
        """Cancel and await every local bearer task; timeout never means success。"""
        try:
            async with asyncio.timeout(self._revoke_timeout):
                await self._runtime.cancel_tasks(task_ids)
                if self._close_all_handles is not None:
                    await self._close_all_handles()
                else:
                    await self._close_handles("__all_workload_authorities__")
                return await self._permits.wait_all_drained(
                    timeout=self._revoke_timeout
                )
        except Exception:
            return False

    @staticmethod
    def _event(
        *,
        action: str,
        actor_id: str,
        authority_id: str,
        authority_generation: int,
        request_id: str,
        outcome: str,
        details_digest: str,
        now: float,
    ) -> WorkloadAuditEvent:
        return WorkloadAuditEvent(
            event_id=str(uuid4()),
            action=action,
            actor_id=actor_id,
            authority_id=authority_id,
            authority_generation=authority_generation,
            request_id=request_id,
            outcome=outcome,
            created_at=now,
            details_digest=details_digest,
        )

    async def list_profiles(self) -> list[WorkloadOAuthProfile]:
        """Return all non-secret workload profiles in stable order。"""
        return await self._profiles.list_profiles()

    async def put_profile(
        self,
        profile: WorkloadOAuthProfile,
        *,
        actor_id: str,
        request_id: str,
    ) -> WorkloadOAuthProfile:
        """Globally fence replacement before publishing its new authority epoch。"""
        saved, drained = await self._run_epoch_mutation(
            lambda: self._put_profile_unfenced(
                profile,
                actor_id=actor_id,
                request_id=request_id,
            ),
            actor_id=actor_id,
            request_id=request_id,
            recovery_authority_id=profile.profile_id,
            mutation_kind=WorkloadMutationKind.PROFILE,
        )
        if not drained:
            raise WorkloadControlRecoveryRequired()
        return saved

    async def _put_profile_unfenced(
        self,
        profile: WorkloadOAuthProfile,
        *,
        actor_id: str,
        request_id: str,
    ) -> WorkloadOAuthProfile:
        """Persist intent before JSON and publish only after exact confirmation。"""
        existing = await self._profiles.get(profile.profile_id)
        direction = "expansion" if existing is None else "reduction"
        now = self._clock()
        operation_id = str(uuid4())
        intent = self._event(
            action=f"profile.{direction}.intent",
            actor_id=actor_id,
            authority_id=profile.profile_id,
            authority_generation=profile.generation,
            request_id=request_id,
            outcome="pending",
            details_digest=profile.fingerprint,
            now=now,
        )
        await self._store.begin_authority_transition(
            operation_id=operation_id,
            direction=direction,
            authority_kind="profile",
            authority_id=profile.profile_id,
            authority_generation=profile.generation,
            actor_id=actor_id,
            request_digest=profile.fingerprint,
            now=now,
            event=intent,
        )
        try:
            saved = await self._profiles.put(
                profile,
                version="1.0.0",
                author=actor_id,
            )
        except Exception:
            await self._store.mark_transition_recovery_required(
                operation_id,
                now=self._clock(),
            )
            raise
        result = self._event(
            action=f"profile.{direction}.commit",
            actor_id=actor_id,
            authority_id=profile.profile_id,
            authority_generation=profile.generation,
            request_id=request_id,
            outcome="success",
            details_digest=profile.fingerprint,
            now=self._clock(),
        )
        await self._store.confirm_authority_transition(
            operation_id=operation_id,
            actor_id=actor_id,
            request_digest=profile.fingerprint,
            now=self._clock(),
            event=result,
        )
        invalidate = getattr(self._runtime, "invalidate_all_authorities", None)
        if callable(invalidate):
            invalidate()
        return saved

    async def delete_profile(
        self,
        *,
        profile_id: str,
        expected_generation: int,
        actor_id: str,
        request_id: str,
    ) -> WorkloadMutationResponse:
        """Run profile reduction through the shared epoch/fence coordinator。"""
        result, drained = await self._run_epoch_mutation(
            lambda: self._delete_profile_unfenced(
                profile_id=profile_id,
                expected_generation=expected_generation,
                actor_id=actor_id,
                request_id=request_id,
            ),
            actor_id=actor_id,
            request_id=request_id,
            recovery_authority_id=profile_id,
            mutation_kind=WorkloadMutationKind.PROFILE,
        )
        return result.model_copy(
            update={"status": "completed" if drained else "revocation_pending"}
        )

    async def _delete_profile_unfenced(
        self,
        *,
        profile_id: str,
        expected_generation: int,
        actor_id: str,
        request_id: str,
    ) -> WorkloadMutationResponse:
        """Fence all sends before removing one exact non-secret profile generation。"""
        profile = await self._profiles.get(profile_id)
        if profile is None or profile.generation != expected_generation:
            raise WorkloadControlDenied()
        credential = await self._store.credential_status(
            profile_id=profile_id,
            credential_id=profile.credential_id,
        )
        if credential is not None and credential[1] == "active":
            raise WorkloadControlDenied()
        operation_id = str(uuid4())
        try:
            await self._store.begin_authority_transition(
                operation_id=operation_id,
                direction="reduction",
                authority_kind="profile",
                authority_id=profile_id,
                authority_generation=expected_generation,
                actor_id=actor_id,
                request_digest=profile.fingerprint,
                now=self._clock(),
                event=self._event(
                    action="profile.delete.intent",
                    actor_id=actor_id,
                    authority_id=profile_id,
                    authority_generation=expected_generation,
                    request_id=request_id,
                    outcome="pending",
                    details_digest=profile.fingerprint,
                    now=self._clock(),
                ),
            )
            await self._profiles.delete(
                profile_id,
                expected_generation=expected_generation,
                version="1.0.0",
                author=actor_id,
            )
            await self._store.confirm_authority_transition(
                operation_id=operation_id,
                actor_id=actor_id,
                request_digest=profile.fingerprint,
                now=self._clock(),
                event=self._event(
                    action="profile.delete.commit",
                    actor_id=actor_id,
                    authority_id=profile_id,
                    authority_generation=expected_generation,
                    request_id=request_id,
                    outcome="success",
                    details_digest=profile.fingerprint,
                    now=self._clock(),
                ),
            )
        except BaseException:
            try:
                await self._store.mark_transition_recovery_required(
                    operation_id,
                    now=self._clock(),
                )
            except Exception as recovery_exc:
                raise WorkloadControlRecoveryRequired() from recovery_exc
            raise
        self._runtime.invalidate_all_authorities()
        return WorkloadMutationResponse(
            status="completed",
            generation=expected_generation,
        )

    async def recovery_status(self) -> list[WorkloadRecoveryItem]:
        """Project every durable transition, drain, and rotation blocker。"""
        transitions = await self._store.list_blocking_authority_transitions()
        revocations = await self._store.list_blocking_revocations()
        rotation = await self._store.workload_key_rotation()
        result = [
            WorkloadRecoveryItem(
                kind="transition",
                recovery_id=transition.operation_id,
                authority_id=transition.authority_id,
                generation=transition.authority_generation,
                status=transition.status,
                updated_at=transition.updated_at,
            )
            for transition in transitions
        ]
        result.extend(
            WorkloadRecoveryItem(
                kind="revocation",
                recovery_id=revocation.authority_key,
                authority_id=revocation.authority_id,
                generation=revocation.authority_generation,
                status=revocation.status.value,
                updated_at=revocation.updated_at,
            )
            for revocation in revocations
        )
        if rotation.phase != OAuthKeyRotationPhase.STABLE:
            result.append(
                WorkloadRecoveryItem(
                    kind="rotation",
                    recovery_id="workload-key-rotation",
                    authority_id="workload-keyring",
                    generation=rotation.epoch,
                    status=rotation.phase.value,
                    updated_at=rotation.started_at,
                )
            )
        return sorted(
            result,
            key=lambda item: (item.kind, item.recovery_id, item.generation),
        )

    async def retry_recovery(
        self,
        *,
        actor_id: str,
        request_id: str,
    ) -> list[WorkloadRecoveryItem]:
        """Retry local task-exit acknowledgement and explicitly finalize drains。"""
        async with self._mutation_coordinator.serialized():
            pending = await self._store.list_blocking_revocations()
            owner = await self._store.active_mutation()
            recoverable = [
                revocation
                for revocation in pending
                if revocation.status != WorkloadRevocationStatus.RECOVERY_REQUIRED
                and revocation.mutation_operation_id is not None
                and owner is not None
                and revocation.mutation_operation_id == owner.operation_id
            ]
            if recoverable:
                tasks = await self._resume_revocation_fence_locked(recoverable[0])
                if await self._drain_all_egress(tasks):
                    for revocation in recoverable:
                        mutation_operation_id = revocation.mutation_operation_id
                        if mutation_operation_id is None:
                            raise WorkloadControlRecoveryRequired()
                        await self._complete_revocation_side_effect(revocation)
                        details = _digest(
                            {
                                "authority_id": revocation.authority_id,
                                "generation": revocation.authority_generation,
                                "status": WorkloadRevocationStatus.COMPLETED.value,
                            }
                        )
                        await self._store.finalize_revocation(
                            authority_key=revocation.authority_key,
                            authority_generation=revocation.authority_generation,
                            mutation_operation_id=mutation_operation_id,
                            status=WorkloadRevocationStatus.COMPLETED,
                            now=self._clock(),
                            event=self._event(
                                action="authority.drain.completed",
                                actor_id=actor_id,
                                authority_id=revocation.authority_id,
                                authority_generation=revocation.authority_generation,
                                request_id=request_id,
                                outcome="success",
                                details_digest=details,
                                now=self._clock(),
                            ),
                        )
            if (
                self._mutation_coordinator.pending_fence_id is not None
                and await self._store.runtime_authority_ready(
                    mutation_operation_id=(
                        self._mutation_coordinator.pending_operation_id
                    )
                )
            ):
                await self._mutation_coordinator.complete(
                    fence_id=self._mutation_coordinator.pending_fence_id
                )
        return await self.recovery_status()

    async def _complete_revocation_side_effect(
        self,
        revocation: WorkloadRevocationRecord,
    ) -> None:
        """Finish an exact delegation policy delete before clearing its blocker。"""
        if not revocation.authority_key.startswith("delegation:"):
            return
        record = await self._delegations.get(revocation.authority_id)
        if record is None:
            return
        if (
            record.status != WorkloadDelegationStatus.ACTIVE
            or record.generation != revocation.authority_generation
            or f"delegation:{record.digest}" != revocation.authority_key
            or (
                revocation.source_slot_digest is not None
                and _source_slot_digest(record.source_slot)
                != revocation.source_slot_digest
            )
        ):
            raise WorkloadControlRecoveryRequired()
        try:
            await self._delegations.delete(
                record.delegation_id,
                expected_generation=record.generation,
                version="1.0.0",
                author=record.owner_user_id,
            )
        except Exception as exc:
            raise WorkloadControlRecoveryRequired() from exc

    async def _resume_revocation_fence_locked(
        self,
        revocation: WorkloadRevocationRecord,
    ) -> frozenset[str]:
        """Resume only the process fence named by the durable revocation owner。"""
        operation_id = revocation.mutation_operation_id
        owner = await self._store.active_mutation()
        if (
            operation_id is None
            or owner is None
            or owner.operation_id != operation_id
        ):
            raise WorkloadControlRecoveryRequired()
        pending_fence_id = self._mutation_coordinator.pending_fence_id
        if pending_fence_id is not None:
            if self._mutation_coordinator.pending_operation_id != operation_id:
                raise WorkloadControlRecoveryRequired()
            return frozenset()
        fence_id = await self._begin_global_fence(
            operation_id=owner.operation_id,
            mutation_kind=owner.mutation_kind,
        )
        tasks = await self._mutation_coordinator.commit(
            fence_id=fence_id,
            new_authority_epoch=await self._store.authority_epoch(),
        )
        self._runtime.invalidate_all_authorities()
        return tasks

    async def _transition_resolution_matches(
        self,
        transition: Any,
        *,
        resolution: str,
    ) -> bool:
        if resolution == "confirm_absent":
            if transition.direction != "reduction":
                return False
            if transition.authority_kind == "profile":
                return await self._profiles.get(transition.authority_id) is None
            if transition.authority_kind == "delegation":
                return await self._delegations.get(transition.authority_id) is None
            return False
        if resolution != "confirm_present":
            return False
        if transition.authority_kind == "profile":
            profile = await self._profiles.get(transition.authority_id)
            return (
                profile is not None
                and profile.generation == transition.authority_generation
                and profile.fingerprint == transition.request_digest
            )
        if transition.authority_kind == "delegation":
            delegation = await self._delegations.get(transition.authority_id)
            return (
                delegation is not None
                and delegation.generation == transition.authority_generation
                and delegation.digest == transition.request_digest
                and await self._store.delegation_mirror_matches(delegation)
            )
        return False

    async def reconcile_transition(
        self,
        *,
        operation_id: str,
        resolution: str,
        actor_id: str,
        request_id: str,
    ) -> WorkloadMutationResponse:
        """Finalize only an exact present/absent state chosen by an admin。"""
        async with self._mutation_coordinator.serialized():
            transition = await self._store.get_authority_transition(operation_id)
            if (
                transition is None
                or transition.status not in {"pending_json", "recovery_required"}
                or not await self._transition_resolution_matches(
                    transition,
                    resolution=resolution,
                )
            ):
                raise WorkloadControlDenied()
            existing_fence = self._mutation_coordinator.pending_fence_id
            owns_new_fence = existing_fence is None
            fence_id = existing_fence or await self._begin_global_fence(
                operation_id=operation_id,
                mutation_kind=WorkloadMutationKind.RECOVERY,
            )
            confirmed = False
            try:
                reconciled = await self._store.reconcile_authority_transition(
                    operation_id=operation_id,
                    now=self._clock(),
                    event=self._event(
                        action=f"{transition.authority_kind}.reconcile.{resolution}",
                        actor_id=actor_id,
                        authority_id=transition.authority_id,
                        authority_generation=transition.authority_generation,
                        request_id=request_id,
                        outcome="success",
                        details_digest=transition.request_digest,
                        now=self._clock(),
                    ),
                )
                confirmed = True
                if owns_new_fence:
                    tasks = await self._mutation_coordinator.commit(
                        fence_id=fence_id,
                        new_authority_epoch=await self._store.authority_epoch(),
                    )
                else:
                    tasks = frozenset()
                self._runtime.invalidate_all_authorities()
                drained = await self._drain_all_egress(tasks)
                if drained and await self._store.runtime_authority_ready(
                    mutation_operation_id=(
                        self._mutation_coordinator.pending_operation_id
                    )
                ):
                    await self._mutation_coordinator.complete(fence_id=fence_id)
                elif not drained:
                    await self._record_pending_drain(
                        authority_key=f"reconcile:{operation_id}",
                        authority_id=transition.authority_id,
                        authority_generation=transition.authority_generation,
                        actor_id=actor_id,
                        request_id=request_id,
                    )
            except BaseException:
                if owns_new_fence and not confirmed:
                    await self._mutation_coordinator.abort(fence_id=fence_id)
                raise
        status = "active" if reconciled.status == "active" else "completed"
        if not drained or not await self._store.runtime_authority_ready():
            status = "recovery_required"
        return WorkloadMutationResponse(
            status=status,
            generation=reconciled.authority_generation,
        )

    async def set_credential(
        self,
        *,
        profile_id: str,
        client_id: SecretStr,
        client_secret: SecretStr,
        actor_id: str,
        request_id: str,
    ) -> WorkloadCredentialStatus:
        """Fence credential replacement and wait for every prior send to exit。"""
        status, drained = await self._run_epoch_mutation(
            lambda: self._set_credential_unfenced(
                profile_id=profile_id,
                client_id=client_id,
                client_secret=client_secret,
                actor_id=actor_id,
                request_id=request_id,
            ),
            actor_id=actor_id,
            request_id=request_id,
            recovery_authority_id=profile_id,
            mutation_kind=WorkloadMutationKind.CREDENTIAL,
        )
        if not drained:
            raise WorkloadControlRecoveryRequired()
        return status

    async def _set_credential_unfenced(
        self,
        *,
        profile_id: str,
        client_id: SecretStr,
        client_secret: SecretStr,
        actor_id: str,
        request_id: str,
    ) -> WorkloadCredentialStatus:
        """Return a secret-free projection of the latest credential generation。"""
        """Rotate one write-only client-secret pair without response projection。"""
        profile = await self._profiles.get(profile_id)
        if profile is None:
            raise WorkloadControlDenied()
        current = await self._store.credential_status(
            profile_id=profile_id,
            credential_id=profile.credential_id,
        )
        generation = current[0] + 1 if current is not None else 1
        credential = WorkloadCredentialPayload(
            profile_id=profile_id,
            credential_id=profile.credential_id,
            generation=generation,
            client_id=client_id,
            client_secret=client_secret,
        )
        details = _digest(
            {
                "credential_id": profile.credential_id,
                "generation": generation,
                "profile_generation": profile.generation,
                "profile_id": profile_id,
            }
        )
        event = self._event(
            action="credential.rotate",
            actor_id=actor_id,
            authority_id=profile.credential_id,
            authority_generation=generation,
            request_id=request_id,
            outcome="success",
            details_digest=details,
            now=self._clock(),
        )
        await self._store.rotate_credential(
            credential,
            profile_generation=profile.generation,
            now=self._clock(),
            event=event,
        )
        invalidate = getattr(self._runtime, "invalidate_all_authorities", None)
        if callable(invalidate):
            invalidate()
        return WorkloadCredentialStatus(
            profile_id=profile_id,
            credential_id=profile.credential_id,
            generation=generation,
            status="active",
        )

    async def get_credential_status(
        self,
        profile_id: str,
    ) -> WorkloadCredentialStatus:
        """Return a masked credential status without decrypting secret material。"""
        profile = await self._profiles.get(profile_id)
        if profile is None:
            raise WorkloadControlDenied()
        status = await self._store.credential_status(
            profile_id=profile_id,
            credential_id=profile.credential_id,
        )
        return WorkloadCredentialStatus(
            profile_id=profile_id,
            credential_id=profile.credential_id,
            generation=status[0] if status is not None else None,
            status=status[1] if status is not None else "missing",
        )

    async def revoke_credential(
        self,
        *,
        profile_id: str,
        actor_id: str,
        request_id: str,
    ) -> WorkloadMutationResponse:
        """Serialize revocation with profile, metadata, delegation, and rotation。"""
        async with self._mutation_coordinator.serialized():
            return await self._revoke_credential_unserialized(
                profile_id=profile_id,
                actor_id=actor_id,
                request_id=request_id,
            )

    async def _revoke_credential_unserialized(
        self,
        *,
        profile_id: str,
        actor_id: str,
        request_id: str,
    ) -> WorkloadMutationResponse:
        """Tombstone the active secret, fence every permit, then await local drain。"""
        profile = await self._profiles.get(profile_id)
        if profile is None:
            raise WorkloadControlDenied()
        credential = await self._store.credential_status(
            profile_id=profile_id,
            credential_id=profile.credential_id,
        )
        if credential is None or credential[1] != "active":
            raise WorkloadControlDenied()
        generation = credential[0]
        authority_key = f"credential:{profile.credential_id}"
        if not await self._store.runtime_authority_ready():
            raise WorkloadControlRecoveryRequired()
        mutation_operation_id = str(uuid4())
        fence_id = await self._begin_global_fence(
            operation_id=mutation_operation_id,
            mutation_kind=WorkloadMutationKind.CREDENTIAL,
        )
        try:
            old_epoch = await self._store.authority_epoch()
            new_epoch = await self._store.tombstone_credential(
                credential_id=profile.credential_id,
                credential_generation=generation,
                expected_authority_epoch=old_epoch,
                authority_key=authority_key,
                mutation_operation_id=mutation_operation_id,
                now=self._clock(),
                event=self._event(
                    action="credential.revoke.start",
                    actor_id=actor_id,
                    authority_id=profile.credential_id,
                    authority_generation=generation,
                    request_id=request_id,
                    outcome="pending",
                    details_digest=_digest(
                        {
                            "credential_id": profile.credential_id,
                            "generation": generation,
                            "profile_id": profile_id,
                        }
                    ),
                    now=self._clock(),
                ),
            )
        except Exception:
            await self._mutation_coordinator.abort(fence_id=fence_id)
            raise
        tasks = await self._mutation_coordinator.commit(
            fence_id=fence_id,
            new_authority_epoch=new_epoch
        )
        self._runtime.invalidate_all_authorities()
        drained = await self._drain_all_egress(tasks)
        status = (
            WorkloadRevocationStatus.COMPLETED
            if drained
            else WorkloadRevocationStatus.REVOCATION_PENDING
        )
        await self._store.finalize_revocation(
            authority_key=authority_key,
            authority_generation=generation,
            mutation_operation_id=mutation_operation_id,
            status=status,
            now=self._clock(),
            event=self._event(
                action=f"credential.revoke.{status.value}",
                actor_id=actor_id,
                authority_id=profile.credential_id,
                authority_generation=generation,
                request_id=request_id,
                outcome="success" if drained else "failure",
                details_digest=_digest(
                    {
                        "credential_id": profile.credential_id,
                        "generation": generation,
                        "status": status.value,
                    }
                ),
                now=self._clock(),
            ),
        )
        if drained:
            await self._mutation_coordinator.complete(fence_id=fence_id)
        return WorkloadMutationResponse(status=status.value, generation=generation)

    async def refresh_metadata(
        self,
        *,
        profile_id: str,
        actor_id: str,
        request_id: str,
    ) -> WorkloadMetadataStatus:
        """Fence metadata replacement before its new token endpoint is usable。"""
        snapshot, drained = await self._run_epoch_mutation(
            lambda: self._refresh_metadata_unfenced(
                profile_id=profile_id,
                actor_id=actor_id,
                request_id=request_id,
            ),
            actor_id=actor_id,
            request_id=request_id,
            recovery_authority_id=profile_id,
            mutation_kind=WorkloadMutationKind.METADATA,
        )
        if not drained:
            raise WorkloadControlRecoveryRequired()
        return WorkloadMetadataStatus(
            profile_id=snapshot.profile_id,
            profile_generation=snapshot.profile_generation,
            verified_at=snapshot.verified_at,
        )

    async def _refresh_metadata_unfenced(
        self,
        *,
        profile_id: str,
        actor_id: str,
        request_id: str,
    ) -> WorkloadMetadataSnapshot:
        """Explicit-only RFC metadata refresh; token exchange never calls this path。"""
        profile = await self._profiles.get(profile_id)
        if profile is None:
            raise WorkloadControlDenied()
        networks = tuple(
            ipaddress.ip_network(item, strict=True)
            for item in profile.private_network_cidrs
        )
        private_policy = (lambda _url: networks) if networks else None
        resource = await self._outbound.get_json(
            _well_known_url(profile.expected_resource, "oauth-protected-resource"),
            private_network_policy=private_policy,
        )
        authorization = await self._outbound.get_json(
            _well_known_url(profile.expected_issuer, "oauth-authorization-server"),
            private_network_policy=private_policy,
        )
        snapshot = verify_workload_metadata(
            profile=profile,
            resource_metadata=resource,
            authorization_metadata=authorization,
            verified_at=self._clock(),
        )
        event = self._event(
            action="metadata.refresh",
            actor_id=actor_id,
            authority_id=profile_id,
            authority_generation=profile.generation,
            request_id=request_id,
            outcome="success",
            details_digest=snapshot.metadata_security_fingerprint,
            now=self._clock(),
        )
        await self._store.activate_metadata_snapshot(snapshot, event=event)
        invalidate = getattr(self._runtime, "invalidate_all_authorities", None)
        if callable(invalidate):
            invalidate()
        return snapshot

    async def list_delegations(
        self,
        *,
        owner_user_id: str,
        execution_agent_id: str,
    ) -> list[WorkloadDelegationRecord]:
        """Return delegations only after exact active-owner verification。"""
        if (
            await self._owners.active_owner_for_agent(execution_agent_id)
            != owner_user_id
        ):
            raise WorkloadControlDenied()
        return await self._delegations.list_for_owner_agent(
            owner_user_id=owner_user_id,
            execution_agent_id=execution_agent_id,
        )

    async def put_delegation(
        self,
        *,
        delegation_id: str,
        owner_user_id: str,
        execution_agent_id: str,
        installation_id: str,
        installation_generation: int,
        raw_tool_name: str,
        required_scopes: frozenset[str],
        expires_at: float,
        request_id: str,
    ) -> WorkloadDelegationRecord:
        """Hold the shared P8-A source fence through read and durable publish。"""
        async with self._source_authority_lock:
            record, drained = await self._run_epoch_mutation(
                lambda: self._put_delegation_unfenced(
                    delegation_id=delegation_id,
                    owner_user_id=owner_user_id,
                    execution_agent_id=execution_agent_id,
                    installation_id=installation_id,
                    installation_generation=installation_generation,
                    raw_tool_name=raw_tool_name,
                    required_scopes=required_scopes,
                    expires_at=expires_at,
                    request_id=request_id,
                ),
                actor_id=owner_user_id,
                request_id=request_id,
                recovery_authority_id=delegation_id,
                mutation_kind=WorkloadMutationKind.DELEGATION,
            )
        if not drained:
            raise WorkloadControlRecoveryRequired()
        return record

    async def _put_delegation_unfenced(
        self,
        *,
        delegation_id: str,
        owner_user_id: str,
        execution_agent_id: str,
        installation_id: str,
        installation_generation: int,
        raw_tool_name: str,
        required_scopes: frozenset[str],
        expires_at: float,
        request_id: str,
    ) -> WorkloadDelegationRecord:
        """Build the source slot server-side; admin role never substitutes ownership。"""
        now = self._clock()
        config = self._config_lookup(installation_id)
        owner = await self._owners.active_owner_for_agent(execution_agent_id)
        source_id = (
            config.workload_source_installation_id if config is not None else None
        )
        source_config = self._config_lookup(source_id or "")
        if (
            owner != owner_user_id
            or config is None
            or config.auth_mode != "oauth_client_credentials"
            or config.workload_authority_kind != "owner_delegation"
            or config.workload_delegation_id != delegation_id
            or not config.auth_profile_id
            or source_id is None
            or source_id == installation_id
            or source_config is None
            or not source_config.enabled
            or not source_config.is_remote
            or source_config.auth_mode != "oauth_user"
            or source_config.auth_profile_id != config.auth_profile_id
            or installation_generation
            != self._installation_generation_lookup(installation_id)
            or not config.allows_tool(raw_tool_name)
            or not required_scopes
            or not now < expires_at <= now + 2_592_000
            or self._source_grants is None
        ):
            raise WorkloadControlDenied()
        try:
            if canonical_mcp_transport_url(
                source_config.url or ""
            ) != canonical_mcp_transport_url(config.url or ""):
                raise WorkloadControlDenied()
        except McpOAuthTargetBindingError as exc:
            raise WorkloadControlDenied() from exc
        source_generation = self._installation_generation_lookup(source_id)
        source_slot = OAuthAuthorizationSlotKey(
            human_principal_id=owner_user_id,
            execution_agent_id=execution_agent_id,
            installation_id=source_id,
            installation_generation=source_generation,
            auth_profile_id=config.auth_profile_id,
        )
        grant = await self._source_grants.get_active_grant(source_slot, now=now)
        status = getattr(getattr(grant, "status", None), "value", getattr(grant, "status", None))
        binding = getattr(grant, "binding", None)
        tokens = getattr(grant, "tokens", None)
        if (
            grant is None
            or status not in {"active", "refreshing"}
            or not isinstance(getattr(grant, "grant_id", None), str)
            or not isinstance(getattr(grant, "grant_version", None), int)
            or not isinstance(getattr(binding, "profile_generation", None), int)
            or not isinstance(getattr(binding, "resource", None), str)
            or not resource_and_transport_share_origin(
                binding.resource,
                config.url or "",
            )
            or not required_scopes.issubset(
                getattr(binding, "granted_scopes", frozenset())
            )
            or not required_scopes.issubset(
                getattr(tokens, "granted_scopes", frozenset())
            )
            or str(getattr(tokens, "token_type", "")).lower() != "bearer"
        ):
            raise WorkloadControlDenied()
        source_profile_generation = binding.profile_generation
        source_grant_id = grant.grant_id
        source_grant_version = grant.grant_version
        existing = await self._delegations.get(delegation_id)
        generation = existing.generation + 1 if existing is not None else 1
        record = WorkloadDelegationRecord(
            delegation_id=delegation_id,
            owner_user_id=owner_user_id,
            source_slot=source_slot,
            source_profile_generation=source_profile_generation,
            source_grant_id=source_grant_id,
            source_grant_version=source_grant_version,
            execution_agent_id=execution_agent_id,
            installation_id=installation_id,
            installation_generation=installation_generation,
            raw_tool_name=raw_tool_name,
            required_scopes=required_scopes,
            generation=generation,
            status=WorkloadDelegationStatus.ACTIVE,
            issued_at=now,
            expires_at=expires_at,
        )
        direction = "expansion" if existing is None else "reduction"
        operation_id = str(uuid4())
        await self._store.begin_authority_transition(
            operation_id=operation_id,
            direction=direction,
            authority_kind="delegation",
            authority_id=delegation_id,
            authority_generation=generation,
            actor_id=owner_user_id,
            request_digest=record.digest,
            now=now,
            event=self._event(
                action=f"delegation.{direction}.intent",
                actor_id=owner_user_id,
                authority_id=delegation_id,
                authority_generation=generation,
                request_id=request_id,
                outcome="pending",
                details_digest=record.digest,
                now=now,
            ),
        )
        try:
            await self._delegations.put(
                record,
                version="1.0.0",
                author=owner_user_id,
            )
            await self._store.activate_delegation(
                record,
                event=self._event(
                    action="delegation.activate",
                    actor_id=owner_user_id,
                    authority_id=delegation_id,
                    authority_generation=generation,
                    request_id=request_id,
                    outcome="success",
                    details_digest=record.digest,
                    now=self._clock(),
                ),
            )
        except Exception:
            await self._store.mark_transition_recovery_required(
                operation_id,
                now=self._clock(),
            )
            raise
        await self._store.confirm_authority_transition(
            operation_id=operation_id,
            actor_id=owner_user_id,
            request_digest=record.digest,
            now=self._clock(),
            event=self._event(
                action=f"delegation.{direction}.commit",
                actor_id=owner_user_id,
                authority_id=delegation_id,
                authority_generation=generation,
                request_id=request_id,
                outcome="success",
                details_digest=record.digest,
                now=self._clock(),
            ),
        )
        return record

    async def delete_delegation(
        self,
        *,
        delegation_id: str,
        owner_user_id: str,
        execution_agent_id: str,
        request_id: str,
    ) -> WorkloadMutationResponse:
        """Serialize deletion with every other workload authority mutation。"""
        async with self._mutation_coordinator.serialized():
            return await self._delete_delegation_unserialized(
                delegation_id=delegation_id,
                owner_user_id=owner_user_id,
                execution_agent_id=execution_agent_id,
                request_id=request_id,
                mutation_kind=WorkloadMutationKind.DELEGATION,
                source_slot_digest=None,
            )

    async def _delete_source_delegation(
        self,
        *,
        record: WorkloadDelegationRecord,
        source_slot_digest: str,
        request_id: str,
    ) -> WorkloadMutationResponse:
        """Delete one derived authority under a source-disconnect owner。"""
        async with self._mutation_coordinator.serialized():
            return await self._delete_delegation_unserialized(
                delegation_id=record.delegation_id,
                owner_user_id=record.owner_user_id,
                execution_agent_id=record.execution_agent_id,
                request_id=request_id,
                mutation_kind=WorkloadMutationKind.SOURCE_DISCONNECT,
                source_slot_digest=source_slot_digest,
            )

    async def _delete_delegation_unserialized(
        self,
        *,
        delegation_id: str,
        owner_user_id: str,
        execution_agent_id: str,
        request_id: str,
        mutation_kind: WorkloadMutationKind,
        source_slot_digest: str | None,
    ) -> WorkloadMutationResponse:
        """Fence and remove one exact owner delegation without touching P8-A。"""
        record = await self._delegations.get(delegation_id)
        owner = await self._owners.active_owner_for_agent(execution_agent_id)
        if (
            record is None
            or owner != owner_user_id
            or record.owner_user_id != owner_user_id
            or record.execution_agent_id != execution_agent_id
            or record.status != WorkloadDelegationStatus.ACTIVE
        ):
            raise WorkloadControlDenied()
        authority_key = f"delegation:{record.digest}"
        if not await self._store.runtime_authority_ready():
            raise WorkloadControlRecoveryRequired()
        mutation_operation_id = str(uuid4())
        fence_id = await self._begin_global_fence(
            operation_id=mutation_operation_id,
            mutation_kind=mutation_kind,
        )
        try:
            old_epoch = await self._store.authority_epoch()
            new_epoch = await self._store.tombstone_delegation(
                delegation_id=delegation_id,
                generation=record.generation,
                digest=record.digest,
                expected_authority_epoch=old_epoch,
                authority_key=authority_key,
                mutation_operation_id=mutation_operation_id,
                source_slot_digest=source_slot_digest,
                now=self._clock(),
                event=self._event(
                    action="delegation.revoke.start",
                    actor_id=owner_user_id,
                    authority_id=delegation_id,
                    authority_generation=record.generation,
                    request_id=request_id,
                    outcome="pending",
                    details_digest=record.digest,
                    now=self._clock(),
                ),
            )
        except Exception:
            await self._mutation_coordinator.abort(fence_id=fence_id)
            raise
        tasks = await self._mutation_coordinator.commit(
            fence_id=fence_id,
            new_authority_epoch=new_epoch
        )
        self._runtime.invalidate_all_authorities()
        drained = await self._drain_all_egress(tasks)
        try:
            await self._delegations.delete(
                delegation_id,
                expected_generation=record.generation,
                version="1.0.0",
                author=owner_user_id,
            )
        except Exception as exc:
            raise WorkloadControlRecoveryRequired() from exc
        status = (
            WorkloadRevocationStatus.COMPLETED
            if drained
            else WorkloadRevocationStatus.REVOCATION_PENDING
        )
        await self._store.finalize_revocation(
            authority_key=authority_key,
            authority_generation=record.generation,
            mutation_operation_id=mutation_operation_id,
            status=status,
            now=self._clock(),
            event=self._event(
                action=f"delegation.revoke.{status.value}",
                actor_id=owner_user_id,
                authority_id=delegation_id,
                authority_generation=record.generation,
                request_id=request_id,
                outcome="success" if drained else "failure",
                details_digest=_digest(
                    {
                        "delegation_id": delegation_id,
                        "generation": record.generation,
                        "status": status.value,
                    }
                ),
                now=self._clock(),
            ),
        )
        if drained:
            await self._mutation_coordinator.complete(fence_id=fence_id)
        return WorkloadMutationResponse(
            status=status.value,
            generation=record.generation,
        )

    async def revoke_source_delegations(
        self,
        source_slot: OAuthAuthorizationSlotKey,
        *,
        request_id: str,
    ) -> None:
        """Fence every derived delegation before its exact P8-A grant disconnects。"""
        slot_key = source_slot.canonical_bytes()
        slot_digest = _source_slot_digest(source_slot)
        while True:
            pending = await self._store.list_blocking_revocations(
                source_slot_digest=slot_digest
            )
            if pending:
                async with self._mutation_coordinator.serialized():
                    if any(
                        item.status == WorkloadRevocationStatus.RECOVERY_REQUIRED
                        for item in pending
                    ):
                        raise WorkloadControlRecoveryRequired()
                    tasks = await self._resume_revocation_fence_locked(pending[0])
                    if not await self._drain_all_egress(tasks):
                        self._pending_source_disconnects.add(slot_key)
                        raise WorkloadControlRecoveryRequired()
                    for revocation in pending:
                        operation_id = revocation.mutation_operation_id
                        if (
                            operation_id is None
                            or operation_id != pending[0].mutation_operation_id
                        ):
                            raise WorkloadControlRecoveryRequired()
                        await self._complete_revocation_side_effect(revocation)
                        await self._store.finalize_revocation(
                            authority_key=revocation.authority_key,
                            authority_generation=revocation.authority_generation,
                            mutation_operation_id=operation_id,
                            status=WorkloadRevocationStatus.COMPLETED,
                            now=self._clock(),
                            event=self._event(
                                action="source-delegation.drain.completed",
                                actor_id=source_slot.human_principal_id,
                                authority_id=revocation.authority_id,
                                authority_generation=revocation.authority_generation,
                                request_id=request_id,
                                outcome="success",
                                details_digest=_digest(
                                    {
                                        "authority_id": revocation.authority_id,
                                        "generation": revocation.authority_generation,
                                        "source_slot_digest": slot_digest,
                                    }
                                ),
                                now=self._clock(),
                            ),
                        )
                    pending_fence_id = self._mutation_coordinator.pending_fence_id
                    if (
                        pending_fence_id is None
                        or not await self._store.runtime_authority_ready(
                            mutation_operation_id=pending[0].mutation_operation_id
                        )
                    ):
                        raise WorkloadControlRecoveryRequired()
                    await self._mutation_coordinator.complete(
                        fence_id=pending_fence_id
                    )

            records = await self._delegations.list_for_source_slot(source_slot)
            if not records:
                self._pending_source_disconnects.discard(slot_key)
                return
            result = await self._delete_source_delegation(
                record=records[0],
                source_slot_digest=slot_digest,
                request_id=request_id,
            )
            if result.status != WorkloadRevocationStatus.COMPLETED.value:
                self._pending_source_disconnects.add(slot_key)
                raise WorkloadControlRecoveryRequired()

    @asynccontextmanager
    async def delegation_disconnect_guard(
        self,
        source_slot: OAuthAuthorizationSlotKey,
        request_id: str,
    ) -> AsyncIterator[None]:
        """Keep delegation creation excluded until the P8-A grant is disconnected。"""
        async with self._source_authority_lock:
            await self.revoke_source_delegations(
                source_slot,
                request_id=request_id,
            )
            yield
