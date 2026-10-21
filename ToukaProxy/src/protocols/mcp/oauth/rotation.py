# ruff: noqa: TC002
"""Crash-resumable managed OAuth key rotation orchestration."""

from __future__ import annotations

from typing import Protocol

from src.protocols.mcp.oauth.keyring import OAuthKey, OAuthKeyRing
from src.protocols.mcp.oauth.models import (
    OAuthKeyRotationPhase,
    OAuthKeyRotationRecord,
)
from src.protocols.mcp.oauth.store import OAuthSqliteStore, OAuthStoreCorruptionError


class OAuthKeyRotationError(ValueError):
    """Managed key rotation cannot safely advance."""


class OAuthKeyRotationUnavailableError(OAuthKeyRotationError):
    """In-process rotation is unavailable for this key source."""


class OAuthRotationKeyProvider(Protocol):
    """Durable key writer/reader seam used by the rotation owner."""

    source: str

    def create_rotation_candidate(self) -> OAuthKey:
        """Generate but do not persist the next key."""

    async def prepare_rotation(
        self,
        *,
        epoch: int,
        candidate: OAuthKey,
    ) -> OAuthKeyRing:
        """Atomically persist new-current plus old-retiring."""

    async def reload(self) -> OAuthKeyRing:
        """Reload the authoritative durable ring."""

    async def retire(self, *, epoch: int) -> OAuthKeyRing:
        """Atomically remove the retiring key after all fences pass."""


class OAuthKeyRotationService:
    """Drive persisted phases without ever discarding an uncertain key."""

    def __init__(
        self,
        *,
        store: OAuthSqliteStore,
        provider: OAuthRotationKeyProvider,
        worker_id: str,
        state_ttl_seconds: float,
        worker_liveness_seconds: float,
        batch_size: int,
    ) -> None:
        if not worker_id or state_ttl_seconds < 0 or worker_liveness_seconds <= 0 or batch_size < 1:
            raise ValueError()
        self._store = store
        self._provider = provider
        self._worker_id = worker_id
        self._state_ttl_seconds = state_ttl_seconds
        self._worker_liveness_seconds = worker_liveness_seconds
        self._batch_size = batch_size

    @staticmethod
    def _validate_dual_ring(
        record: OAuthKeyRotationRecord,
        ring: OAuthKeyRing,
    ) -> None:
        retiring_ids = {key.kid for key in ring.retiring}
        if (
            ring.current.kid != record.current_kid
            or record.retiring_kid is None
            or retiring_ids != {record.retiring_kid}
        ):
            raise OAuthKeyRotationError()

    async def start(self, *, now: float) -> OAuthKeyRotationRecord:
        """Persist prepare before atomically publishing the dual-read ring."""
        if self._provider.source != "managed":
            raise OAuthKeyRotationUnavailableError()
        current = await self._store.get_key_rotation()
        if current.phase != OAuthKeyRotationPhase.STABLE:
            raise OAuthKeyRotationError()
        candidate = self._provider.create_rotation_candidate()
        prepared = await self._store.begin_key_rotation(
            new_kid=candidate.kid,
            worker_id=self._worker_id,
            now=now,
            state_ttl_seconds=self._state_ttl_seconds,
            worker_liveness_seconds=self._worker_liveness_seconds,
        )
        try:
            ring = await self._provider.prepare_rotation(
                epoch=prepared.epoch,
                candidate=candidate,
            )
        except Exception:
            try:
                ring = await self._provider.reload()
            except Exception as reload_error:
                raise OAuthKeyRotationError() from reload_error
            if ring.current.kid == prepared.retiring_kid and not ring.retiring:
                await self._store.rollback_key_rotation_prepare(epoch=prepared.epoch)
                raise
            self._validate_dual_ring(prepared, ring)
        else:
            self._validate_dual_ring(prepared, ring)
        self._store.replace_keyring(ring)
        advanced = await self._store.set_key_rotation_phase(
            epoch=prepared.epoch,
            expected=OAuthKeyRotationPhase.PREPARE,
            target=OAuthKeyRotationPhase.DUAL_READ_NEW_WRITE,
        )
        if not advanced:
            raise OAuthStoreCorruptionError()
        await self._store.report_worker_epoch(
            self._worker_id,
            epoch=prepared.epoch,
            now=now,
        )
        return await self._store.get_key_rotation()

    async def status(self) -> OAuthKeyRotationRecord:
        """Return non-secret persisted rotation state."""
        return await self._store.get_key_rotation()

    async def _load_dual_ring(
        self,
        record: OAuthKeyRotationRecord,
        *,
        now: float,
    ) -> OAuthKeyRing:
        ring = await self._provider.reload()
        self._validate_dual_ring(record, ring)
        self._store.replace_keyring(ring)
        await self._store.report_worker_epoch(
            self._worker_id,
            epoch=record.epoch,
            now=now,
        )
        return ring

    async def maintain(self, *, now: float) -> OAuthKeyRotationRecord:
        """Resume one rotation and advance every currently satisfied phase."""
        record = await self._store.get_key_rotation()
        if record.phase == OAuthKeyRotationPhase.STABLE:
            await self._store.report_worker_epoch(
                self._worker_id,
                epoch=record.epoch,
                now=now,
            )
            return record
        if self._provider.source != "managed":
            raise OAuthKeyRotationUnavailableError()
        if record.phase == OAuthKeyRotationPhase.PREPARE:
            ring = await self._provider.reload()
            if ring.current.kid == record.retiring_kid and not ring.retiring:
                if not await self._store.rollback_key_rotation_prepare(epoch=record.epoch):
                    raise OAuthStoreCorruptionError()
                self._store.replace_keyring(ring)
                recovered = await self._store.get_key_rotation()
                await self._store.report_worker_epoch(
                    self._worker_id,
                    epoch=recovered.epoch,
                    now=now,
                )
                return recovered
            self._validate_dual_ring(record, ring)
            self._store.replace_keyring(ring)
            if not await self._store.set_key_rotation_phase(
                epoch=record.epoch,
                expected=OAuthKeyRotationPhase.PREPARE,
                target=OAuthKeyRotationPhase.DUAL_READ_NEW_WRITE,
            ):
                raise OAuthStoreCorruptionError()
            record = await self._store.get_key_rotation()
        await self._load_dual_ring(record, now=now)
        retiring_kid = record.retiring_kid
        if retiring_kid is None:
            raise OAuthStoreCorruptionError()

        if record.phase == OAuthKeyRotationPhase.DUAL_READ_NEW_WRITE:
            await self._store.reencrypt_and_rekey_batch(
                retiring_kid=retiring_kid,
                limit=self._batch_size,
            )
            references = await self._store.count_key_references(retiring_kid)
            non_state = sum(count for name, count in references.items() if name != "state_indexes")
            if non_state == 0:
                if not await self._store.set_key_rotation_phase(
                    epoch=record.epoch,
                    expected=OAuthKeyRotationPhase.DUAL_READ_NEW_WRITE,
                    target=OAuthKeyRotationPhase.STATE_DRAIN,
                ):
                    raise OAuthStoreCorruptionError()
                record = await self._store.get_key_rotation()

        if record.phase == OAuthKeyRotationPhase.STATE_DRAIN:
            await self._store.cleanup(now=now)
            references = await self._store.count_key_references(retiring_kid)
            if (
                record.state_drain_after is not None
                and now >= record.state_drain_after
                and sum(references.values()) == 0
            ):
                if not await self._store.set_key_rotation_phase(
                    epoch=record.epoch,
                    expected=OAuthKeyRotationPhase.STATE_DRAIN,
                    target=OAuthKeyRotationPhase.WORKER_CONVERGE,
                ):
                    raise OAuthStoreCorruptionError()
                record = await self._store.get_key_rotation()

        if record.phase == OAuthKeyRotationPhase.WORKER_CONVERGE:
            references = await self._store.count_key_references(retiring_kid)
            if sum(references.values()) != 0:
                raise OAuthStoreCorruptionError()
            if await self._store.key_rotation_workers_converged(epoch=record.epoch):
                ring = await self._provider.retire(epoch=record.epoch)
                if ring.current.kid != record.current_kid or ring.retiring:
                    raise OAuthKeyRotationError()
                self._store.replace_keyring(ring)
                if not await self._store.complete_key_rotation(epoch=record.epoch):
                    raise OAuthStoreCorruptionError()
                record = await self._store.get_key_rotation()
        return record
