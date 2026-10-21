"""Encrypted P8-B SQLite store with transactional authority audit outbox。"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import pydantic
from src.protocols.mcp.oauth import keyring as oauth_keyring
from src.protocols.mcp.oauth import models as oauth_models
from src.protocols.mcp.oauth.outbound import (
    OAuthOutboundPolicyError,
    validate_oauth_endpoint,
)
from src.protocols.mcp.oauth.workload import models as workload_models

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class WorkloadStoreCorruptionError(ValueError):
    """Encrypted state or runtime invariants cannot be authenticated。"""


class WorkloadAuditConflictError(ValueError):
    """An audit event ID was replayed with another authority transition。"""


class WorkloadAuthorityConflictError(ValueError):
    """An authority generation or epoch changed before the transaction。"""


class WorkloadCredentialConflictError(ValueError):
    """A credential generation already exists or another generation is active。"""


class WorkloadOAuthStore:
    """Short-transaction encrypted credential and authority-state repository。"""

    def __init__(
        self,
        db_path: Path,
        keyring: oauth_keyring.OAuthKeyRing,
        *,
        initial_authority_epoch: int = 1,
    ) -> None:
        if initial_authority_epoch < 1:
            raise ValueError()
        self._db_path = Path(db_path)
        self._keyring = keyring
        self._initial_authority_epoch = initial_authority_epoch

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(str(self._db_path), timeout=5.0)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA synchronous = FULL")
            yield db
        finally:
            await db.close()

    async def prepare_schema(self) -> None:
        """Create the isolated P8-B store and require WAL durability。"""
        async with self._connection() as db:
            async with db.execute("PRAGMA journal_mode = WAL") as cursor:
                row = await cursor.fetchone()
            if row is None or str(row[0]).lower() != "wal":
                raise WorkloadStoreCorruptionError()
            await db.execute("PRAGMA synchronous = FULL")
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS workload_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS workload_runtime_state ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
                "authority_epoch INTEGER NOT NULL, global_enabled INTEGER NOT NULL, "
                "audit_gate_closed INTEGER NOT NULL, key_rotation_phase TEXT NOT NULL, "
                "current_kid TEXT NOT NULL, retiring_kid TEXT, "
                "key_epoch INTEGER NOT NULL, active_mutation_operation_id TEXT, "
                "active_mutation_kind TEXT)"
            )
            async with db.execute(
                "PRAGMA table_info(workload_runtime_state)"
            ) as cursor:
                runtime_columns = {str(row[1]) for row in await cursor.fetchall()}
            if "key_epoch" not in runtime_columns:
                await db.execute(
                    "ALTER TABLE workload_runtime_state "
                    "ADD COLUMN key_epoch INTEGER NOT NULL DEFAULT 1"
                )
            if "active_mutation_operation_id" not in runtime_columns:
                await db.execute(
                    "ALTER TABLE workload_runtime_state "
                    "ADD COLUMN active_mutation_operation_id TEXT"
                )
            if "active_mutation_kind" not in runtime_columns:
                await db.execute(
                    "ALTER TABLE workload_runtime_state "
                    "ADD COLUMN active_mutation_kind TEXT"
                )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS workload_credentials ("
                "credential_id TEXT NOT NULL, credential_generation INTEGER NOT NULL, "
                "profile_id TEXT NOT NULL, profile_generation INTEGER NOT NULL, "
                "status TEXT NOT NULL, encrypted_payload TEXT NOT NULL, "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
                "PRIMARY KEY(credential_id, credential_generation))"
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS workload_one_active_credential "
                "ON workload_credentials(credential_id) WHERE status = 'active'"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS workload_metadata_snapshots ("
                "profile_id TEXT NOT NULL, profile_generation INTEGER NOT NULL, "
                "profile_fingerprint TEXT NOT NULL, status TEXT NOT NULL, "
                "encrypted_snapshot TEXT NOT NULL, verified_at REAL NOT NULL, "
                "PRIMARY KEY(profile_id, profile_generation))"
            )
            async with db.execute(
                "PRAGMA table_info(workload_metadata_snapshots)"
            ) as cursor:
                metadata_columns = {str(row[1]) for row in await cursor.fetchall()}
            if "encrypted_snapshot" not in metadata_columns:
                await db.execute(
                    "ALTER TABLE workload_metadata_snapshots "
                    "ADD COLUMN encrypted_snapshot TEXT"
                )
                async with db.execute(
                    "SELECT profile_id, profile_generation, profile_fingerprint, "
                    "snapshot_json FROM workload_metadata_snapshots"
                ) as cursor:
                    legacy_rows = await cursor.fetchall()
                for legacy_row in legacy_rows:
                    try:
                        legacy_snapshot = (
                            workload_models.WorkloadMetadataSnapshot.model_validate_json(
                                legacy_row["snapshot_json"]
                            )
                        )
                    except pydantic.ValidationError as exc:
                        raise WorkloadStoreCorruptionError() from exc
                    sealed_snapshot = self._seal_metadata_snapshot(legacy_snapshot)
                    await db.execute(
                        "UPDATE workload_metadata_snapshots "
                        "SET encrypted_snapshot = ?, snapshot_json = '{}' "
                        "WHERE profile_id = ? AND profile_generation = ?",
                        (
                            sealed_snapshot,
                            legacy_row["profile_id"],
                            legacy_row["profile_generation"],
                        ),
                    )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS workload_one_active_metadata_snapshot "
                "ON workload_metadata_snapshots(profile_id) WHERE status = 'active'"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS workload_delegations ("
                "delegation_id TEXT NOT NULL, generation INTEGER NOT NULL, "
                "digest TEXT NOT NULL, status TEXT NOT NULL, record_json TEXT NOT NULL, "
                "issued_at REAL NOT NULL, expires_at REAL NOT NULL, updated_at REAL NOT NULL, "
                "PRIMARY KEY(delegation_id, generation))"
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS workload_one_active_delegation "
                "ON workload_delegations(delegation_id) WHERE status = 'active'"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS workload_audit_outbox ("
                "event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, "
                "status TEXT NOT NULL, created_at REAL NOT NULL, acked_at REAL)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS workload_authority_transitions ("
                "operation_id TEXT PRIMARY KEY, direction TEXT NOT NULL, "
                "authority_kind TEXT NOT NULL, authority_id TEXT NOT NULL, "
                "authority_generation INTEGER NOT NULL, actor_id TEXT NOT NULL, "
                "request_digest TEXT NOT NULL, status TEXT NOT NULL, "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS workload_revocations ("
                "authority_key TEXT NOT NULL, authority_generation INTEGER NOT NULL, "
                "credential_id TEXT NOT NULL, status TEXT NOT NULL, "
                "source_slot_digest TEXT, mutation_operation_id TEXT, "
                "updated_at REAL NOT NULL, "
                "PRIMARY KEY(authority_key, authority_generation))"
            )
            async with db.execute(
                "PRAGMA table_info(workload_revocations)"
            ) as cursor:
                revocation_columns = {str(row[1]) for row in await cursor.fetchall()}
            if "source_slot_digest" not in revocation_columns:
                await db.execute(
                    "ALTER TABLE workload_revocations ADD COLUMN source_slot_digest TEXT"
                )
            if "mutation_operation_id" not in revocation_columns:
                await db.execute(
                    "ALTER TABLE workload_revocations "
                    "ADD COLUMN mutation_operation_id TEXT"
                )
            async with db.execute(
                "SELECT authority_key, authority_generation, credential_id, "
                "source_slot_digest, mutation_operation_id "
                "FROM workload_revocations "
                "WHERE status IN ('draining', 'revocation_pending')"
            ) as cursor:
                legacy_blockers = await cursor.fetchall()
            async with db.execute(
                "SELECT active_mutation_operation_id, active_mutation_kind "
                "FROM workload_runtime_state WHERE singleton = 1"
            ) as cursor:
                legacy_owner_row = await cursor.fetchone()
            if len(legacy_blockers) == 1 and legacy_owner_row is not None:
                blocker = legacy_blockers[0]
                operation_id = legacy_owner_row["active_mutation_operation_id"]
                mutation_kind = legacy_owner_row["active_mutation_kind"]
                source_digest: str | None = None
                try:
                    legacy_owner = workload_models.WorkloadMutationOwner(
                        operation_id=operation_id,
                        mutation_kind=mutation_kind,
                    )
                except (TypeError, ValueError, pydantic.ValidationError):
                    provable = False
                else:
                    operation_id = legacy_owner.operation_id
                    mutation_kind = legacy_owner.mutation_kind.value
                    provable = True
                if (
                    provable
                    and str(mutation_kind)
                    == workload_models.WorkloadMutationKind.SOURCE_DISCONNECT.value
                ):
                    async with db.execute(
                        "SELECT record_json FROM workload_delegations "
                        "WHERE delegation_id = ? AND generation = ?",
                        (blocker["credential_id"], blocker["authority_generation"]),
                    ) as cursor:
                        delegation_row = await cursor.fetchone()
                    if delegation_row is None:
                        provable = False
                    else:
                        try:
                            delegation = (
                                workload_models.WorkloadDelegationRecord.model_validate_json(
                                    delegation_row["record_json"]
                                )
                            )
                        except pydantic.ValidationError:
                            provable = False
                        else:
                            source_digest = (
                                "sha256:"
                                f"{hashlib.sha256(delegation.source_slot.canonical_bytes()).hexdigest()}"
                            )
                if provable:
                    await db.execute(
                        "UPDATE workload_revocations SET source_slot_digest = ?, "
                        "mutation_operation_id = ? "
                        "WHERE authority_key = ? AND authority_generation = ?",
                        (
                            source_digest,
                            str(operation_id),
                            blocker["authority_key"],
                            blocker["authority_generation"],
                        ),
                    )
                elif (
                    str(mutation_kind)
                    == workload_models.WorkloadMutationKind.SOURCE_DISCONNECT.value
                ):
                    await db.execute(
                        "UPDATE workload_revocations SET status = 'recovery_required' "
                        "WHERE authority_key = ? AND authority_generation = ?",
                        (blocker["authority_key"], blocker["authority_generation"]),
                    )
            await db.execute(
                "UPDATE workload_revocations SET status = 'recovery_required' "
                "WHERE status IN ('draining', 'revocation_pending') "
                "AND (mutation_operation_id IS NULL OR NOT EXISTS("
                "SELECT 1 FROM workload_runtime_state "
                "WHERE singleton = 1 AND active_mutation_operation_id = "
                "workload_revocations.mutation_operation_id) OR ("
                "source_slot_digest IS NULL AND EXISTS("
                "SELECT 1 FROM workload_runtime_state WHERE singleton = 1 "
                "AND active_mutation_kind = 'source_disconnect')))"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS workload_revocations_source_blocking "
                "ON workload_revocations(source_slot_digest, status)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO workload_runtime_state "
                "(singleton, authority_epoch, global_enabled, audit_gate_closed, "
                "key_rotation_phase, current_kid, retiring_kid, key_epoch) "
                "VALUES (1, ?, 1, 0, 'stable', ?, ?, 1)",
                (
                    self._initial_authority_epoch,
                    self._keyring.current.kid,
                    self._keyring.retiring[0].kid if self._keyring.retiring else None,
                ),
            )
            await db.execute(
                "INSERT OR IGNORE INTO workload_schema_migrations "
                "(version, applied_at) VALUES (1, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO workload_schema_migrations "
                "(version, applied_at) VALUES (2, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO workload_schema_migrations "
                "(version, applied_at) VALUES (3, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO workload_schema_migrations "
                "(version, applied_at) VALUES (4, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO workload_schema_migrations "
                "(version, applied_at) VALUES (5, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO workload_schema_migrations "
                "(version, applied_at) VALUES (6, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO workload_schema_migrations "
                "(version, applied_at) VALUES (7, 0)"
            )
            await db.commit()

    @staticmethod
    def _credential_aad(credential_id: str, generation: int) -> bytes:
        return f"workload-credential:{credential_id}:{generation}".encode()

    def _seal_credential(
        self,
        credential: workload_models.WorkloadCredentialPayload,
    ) -> str:
        payload = {
            "profile_id": credential.profile_id,
            "credential_id": credential.credential_id,
            "generation": credential.generation,
            "client_id": credential.client_id.get_secret_value(),
            "client_secret": credential.client_secret.get_secret_value(),
        }
        plaintext = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        sealed = self._keyring.encrypt(
            plaintext,
            aad=self._credential_aad(credential.credential_id, credential.generation),
        )
        return sealed.model_dump_json()

    def _open_credential(
        self,
        *,
        credential_id: str,
        generation: int,
        encrypted_payload: str,
    ) -> workload_models.WorkloadCredentialPayload:
        try:
            sealed = oauth_keyring.OAuthSealedValue.model_validate_json(encrypted_payload)
            plaintext = self._keyring.decrypt(
                sealed,
                aad=self._credential_aad(credential_id, generation),
            )
            payload = json.loads(plaintext.decode("utf-8"))
            return workload_models.WorkloadCredentialPayload(
                profile_id=payload["profile_id"],
                credential_id=payload["credential_id"],
                generation=payload["generation"],
                client_id=pydantic.SecretStr(payload["client_id"]),
                client_secret=pydantic.SecretStr(payload["client_secret"]),
            )
        except (
            KeyError,
            TypeError,
            UnicodeError,
            json.JSONDecodeError,
            pydantic.ValidationError,
            oauth_keyring.OAuthDecryptionError,
        ) as exc:
            raise WorkloadStoreCorruptionError() from exc

    @staticmethod
    def _metadata_aad(
        profile_id: str,
        profile_generation: int,
        profile_fingerprint: str,
    ) -> bytes:
        return (
            "workload-metadata:"
            f"{profile_id}:{profile_generation}:{profile_fingerprint}"
        ).encode()

    def _seal_metadata_snapshot(
        self,
        snapshot: workload_models.WorkloadMetadataSnapshot,
    ) -> str:
        plaintext = snapshot.model_dump_json().encode("utf-8")
        sealed = self._keyring.encrypt(
            plaintext,
            aad=self._metadata_aad(
                snapshot.profile_id,
                snapshot.profile_generation,
                snapshot.profile_fingerprint,
            ),
        )
        return sealed.model_dump_json()

    def _open_metadata_snapshot(
        self,
        *,
        profile_id: str,
        profile_generation: int,
        profile_fingerprint: str,
        encrypted_snapshot: str,
    ) -> workload_models.WorkloadMetadataSnapshot:
        try:
            sealed = oauth_keyring.OAuthSealedValue.model_validate_json(
                encrypted_snapshot
            )
            plaintext = self._keyring.decrypt(
                sealed,
                aad=self._metadata_aad(
                    profile_id,
                    profile_generation,
                    profile_fingerprint,
                ),
            )
            snapshot = workload_models.WorkloadMetadataSnapshot.model_validate_json(
                plaintext
            )
            if (
                snapshot.profile_id != profile_id
                or snapshot.profile_generation != profile_generation
                or snapshot.profile_fingerprint != profile_fingerprint
            ):
                raise WorkloadStoreCorruptionError()
            validate_oauth_endpoint(snapshot.issuer)
            validate_oauth_endpoint(snapshot.resource)
            validate_oauth_endpoint(snapshot.token_endpoint)
        except (
            UnicodeError,
            pydantic.ValidationError,
            oauth_keyring.OAuthDecryptionError,
            OAuthOutboundPolicyError,
        ) as exc:
            raise WorkloadStoreCorruptionError() from exc
        return snapshot

    @staticmethod
    async def _insert_audit_event(
        db: aiosqlite.Connection,
        event: workload_models.WorkloadAuditEvent,
    ) -> None:
        try:
            await db.execute(
                "INSERT INTO workload_audit_outbox "
                "(event_id, payload_json, status, created_at, acked_at) "
                "VALUES (?, ?, 'pending', ?, NULL)",
                (event.event_id, event.model_dump_json(), event.created_at),
            )
        except aiosqlite.IntegrityError as exc:
            raise WorkloadAuditConflictError() from exc

    @staticmethod
    def _require_authority(condition: bool) -> None:
        if not condition:
            raise WorkloadAuthorityConflictError()

    async def activate_credential(
        self,
        credential: workload_models.WorkloadCredentialPayload,
        *,
        profile_generation: int,
        now: float,
        event: workload_models.WorkloadAuditEvent,
    ) -> None:
        """Insert one new active generation and its audit intent atomically。"""
        if (
            profile_generation < 1
            or event.authority_id != credential.credential_id
            or event.authority_generation != credential.generation
        ):
            raise WorkloadAuthorityConflictError()
        encrypted_payload = self._seal_credential(credential)
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._insert_audit_event(db, event)
                await db.execute(
                    "INSERT INTO workload_credentials "
                    "(credential_id, credential_generation, profile_id, "
                    "profile_generation, status, encrypted_payload, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                    (
                        credential.credential_id,
                        credential.generation,
                        credential.profile_id,
                        profile_generation,
                        encrypted_payload,
                        now,
                        now,
                    ),
                )
            except WorkloadAuditConflictError:
                await db.rollback()
                raise
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise WorkloadCredentialConflictError() from exc
            await db.commit()

    async def get_active_credential(
        self,
        *,
        profile_id: str,
        profile_generation: int,
        credential_id: str,
        credential_generation: int,
    ) -> workload_models.WorkloadCredentialPayload | None:
        """Return only one exact active profile/credential generation。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT encrypted_payload FROM workload_credentials "
                "WHERE credential_id = ? AND credential_generation = ? "
                "AND profile_id = ? AND profile_generation = ? AND status = 'active'",
                (
                    credential_id,
                    credential_generation,
                    profile_id,
                    profile_generation,
                ),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._open_credential(
            credential_id=credential_id,
            generation=credential_generation,
            encrypted_payload=str(row["encrypted_payload"]),
        )

    async def get_active_profile_credential(
        self,
        *,
        profile_id: str,
        profile_generation: int,
        credential_id: str,
    ) -> workload_models.WorkloadCredentialPayload | None:
        """Resolve the sole active generation for one explicitly named credential。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT credential_generation, encrypted_payload "
                "FROM workload_credentials WHERE profile_id = ? "
                "AND profile_generation = ? AND credential_id = ? "
                "AND status = 'active'",
                (profile_id, profile_generation, credential_id),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        if len(rows) > 1:
            raise WorkloadStoreCorruptionError()
        if not rows:
            return None
        generation = int(rows[0]["credential_generation"])
        return self._open_credential(
            credential_id=credential_id,
            generation=generation,
            encrypted_payload=str(rows[0]["encrypted_payload"]),
        )

    async def credential_status(
        self,
        *,
        profile_id: str,
        credential_id: str,
    ) -> tuple[int, str] | None:
        """Return only the active generation/status without decrypting secret material。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT credential_generation, status FROM workload_credentials "
                "WHERE profile_id = ? AND credential_id = ? "
                "ORDER BY credential_generation DESC LIMIT 1",
                (profile_id, credential_id),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            return None
        return int(row["credential_generation"]), str(row["status"])

    async def rotate_credential(
        self,
        credential: workload_models.WorkloadCredentialPayload,
        *,
        profile_generation: int,
        now: float,
        event: workload_models.WorkloadAuditEvent,
    ) -> int:
        """Atomically tombstone the prior generation, activate next, fence, and audit。"""
        encrypted_payload = self._seal_credential(credential)
        self._require_authority(
            profile_generation >= 1
            and event.authority_id == credential.credential_id
            and event.authority_generation == credential.generation
        )
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT MAX(credential_generation) AS latest_generation, "
                    "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) "
                    "AS active_count FROM workload_credentials "
                    "WHERE credential_id = ?",
                    (credential.credential_id,),
                ) as cursor:
                    history = await cursor.fetchone()
                self._require_authority(history is not None)
                latest_generation = history["latest_generation"]
                active_count = int(history["active_count"] or 0)
                self._require_authority(
                    active_count <= 1
                    and credential.generation
                    == (
                        int(latest_generation) + 1
                        if latest_generation is not None
                        else 1
                    )
                )
                if active_count == 1:
                    await db.execute(
                        "UPDATE workload_credentials SET status = 'tombstoned', "
                        "updated_at = ? WHERE credential_id = ? AND status = 'active'",
                        (now, credential.credential_id),
                    )
                await db.execute(
                    "INSERT INTO workload_credentials "
                    "(credential_id, credential_generation, profile_id, "
                    "profile_generation, status, encrypted_payload, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                    (
                        credential.credential_id,
                        credential.generation,
                        credential.profile_id,
                        profile_generation,
                        encrypted_payload,
                        now,
                        now,
                    ),
                )
                await db.execute(
                    "UPDATE workload_runtime_state SET authority_epoch = authority_epoch + 1 "
                    "WHERE singleton = 1"
                )
                await self._insert_audit_event(db, event)
                async with db.execute(
                    "SELECT authority_epoch FROM workload_runtime_state WHERE singleton = 1"
                ) as cursor:
                    epoch_row = await cursor.fetchone()
                self._require_authority(epoch_row is not None)
            except (WorkloadAuditConflictError, WorkloadAuthorityConflictError):
                await db.rollback()
                raise
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise WorkloadCredentialConflictError() from exc
            await db.commit()
        return int(epoch_row["authority_epoch"])

    async def activate_metadata_snapshot(
        self,
        snapshot: workload_models.WorkloadMetadataSnapshot,
        *,
        event: workload_models.WorkloadAuditEvent,
    ) -> int:
        """Activate one verified snapshot and invalidate older egress permits。"""
        self._require_authority(
            event.authority_id == snapshot.profile_id
            and event.authority_generation == snapshot.profile_generation
            and event.details_digest == snapshot.metadata_security_fingerprint
        )
        encrypted_snapshot = self._seal_metadata_snapshot(snapshot)
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "UPDATE workload_metadata_snapshots SET status = 'tombstoned' "
                    "WHERE profile_id = ? AND status = 'active'",
                    (snapshot.profile_id,),
                )
                await db.execute(
                    "INSERT INTO workload_metadata_snapshots "
                    "(profile_id, profile_generation, profile_fingerprint, status, "
                    "encrypted_snapshot, verified_at) "
                    "VALUES (?, ?, ?, 'active', ?, ?) "
                    "ON CONFLICT(profile_id, profile_generation) DO UPDATE SET "
                    "profile_fingerprint = excluded.profile_fingerprint, "
                    "status = 'active', encrypted_snapshot = excluded.encrypted_snapshot, "
                    "verified_at = excluded.verified_at",
                    (
                        snapshot.profile_id,
                        snapshot.profile_generation,
                        snapshot.profile_fingerprint,
                        encrypted_snapshot,
                        snapshot.verified_at,
                    ),
                )
                await db.execute(
                    "UPDATE workload_runtime_state "
                    "SET authority_epoch = authority_epoch + 1 WHERE singleton = 1"
                )
                await self._insert_audit_event(db, event)
                async with db.execute(
                    "SELECT authority_epoch FROM workload_runtime_state "
                    "WHERE singleton = 1"
                ) as cursor:
                    row = await cursor.fetchone()
                self._require_authority(row is not None)
            except (WorkloadAuditConflictError, WorkloadAuthorityConflictError):
                await db.rollback()
                raise
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise WorkloadAuthorityConflictError() from exc
            await db.commit()
        return int(row["authority_epoch"])

    async def get_metadata_snapshot(
        self,
        *,
        profile_id: str,
        profile_generation: int,
        profile_fingerprint: str,
    ) -> workload_models.WorkloadMetadataSnapshot | None:
        """Return only one exact active verified metadata snapshot。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT encrypted_snapshot FROM workload_metadata_snapshots "
                "WHERE profile_id = ? AND profile_generation = ? "
                "AND profile_fingerprint = ? AND status = 'active'",
                (profile_id, profile_generation, profile_fingerprint),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            return None
        encrypted_snapshot = row["encrypted_snapshot"]
        if not isinstance(encrypted_snapshot, str) or not encrypted_snapshot:
            raise WorkloadStoreCorruptionError()
        return self._open_metadata_snapshot(
            profile_id=profile_id,
            profile_generation=profile_generation,
            profile_fingerprint=profile_fingerprint,
            encrypted_snapshot=encrypted_snapshot,
        )

    async def activate_delegation(
        self,
        delegation: workload_models.WorkloadDelegationRecord,
        *,
        event: workload_models.WorkloadAuditEvent,
    ) -> int:
        """Activate an exact mirror row, advance epoch, and audit atomically。"""
        self._require_authority(
            delegation.status == workload_models.WorkloadDelegationStatus.ACTIVE
            and event.authority_id == delegation.delegation_id
            and event.authority_generation == delegation.generation
            and event.details_digest == delegation.digest
        )
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "UPDATE workload_delegations SET status = 'tombstoned', updated_at = ? "
                    "WHERE delegation_id = ? AND status = 'active'",
                    (event.created_at, delegation.delegation_id),
                )
                await db.execute(
                    "INSERT INTO workload_delegations "
                    "(delegation_id, generation, digest, status, record_json, "
                    "issued_at, expires_at, updated_at) "
                    "VALUES (?, ?, ?, 'active', ?, ?, ?, ?)",
                    (
                        delegation.delegation_id,
                        delegation.generation,
                        delegation.digest,
                        delegation.model_dump_json(),
                        delegation.issued_at,
                        delegation.expires_at,
                        event.created_at,
                    ),
                )
                await db.execute(
                    "UPDATE workload_runtime_state "
                    "SET authority_epoch = authority_epoch + 1 WHERE singleton = 1"
                )
                await self._insert_audit_event(db, event)
                async with db.execute(
                    "SELECT authority_epoch FROM workload_runtime_state WHERE singleton = 1"
                ) as cursor:
                    epoch_row = await cursor.fetchone()
                self._require_authority(epoch_row is not None)
            except (WorkloadAuditConflictError, WorkloadAuthorityConflictError):
                await db.rollback()
                raise
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise WorkloadAuthorityConflictError() from exc
            await db.commit()
        return int(epoch_row["authority_epoch"])

    async def get_active_delegation(
        self,
        *,
        delegation_id: str,
        generation: int,
        digest: str,
        now: float,
    ) -> workload_models.WorkloadDelegationRecord | None:
        """Return only an unexpired mirror matching exact generation and digest。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT record_json, issued_at, expires_at FROM workload_delegations "
                "WHERE delegation_id = ? AND generation = ? AND digest = ? "
                "AND status = 'active' AND issued_at <= ? AND expires_at > ?",
                (delegation_id, generation, digest, now, now),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            delegation = workload_models.WorkloadDelegationRecord.model_validate_json(
                row["record_json"]
            )
        except pydantic.ValidationError as exc:
            raise WorkloadStoreCorruptionError() from exc
        self._require_authority(
            delegation.status == workload_models.WorkloadDelegationStatus.ACTIVE
            and delegation.delegation_id == delegation_id
            and delegation.generation == generation
            and delegation.digest == digest
            and delegation.issued_at == float(row["issued_at"])
            and delegation.expires_at == float(row["expires_at"])
        )
        return delegation

    async def delegation_mirror_matches(
        self,
        delegation: workload_models.WorkloadDelegationRecord,
    ) -> bool:
        """Verify the exact active mirror without applying wall-clock expiry。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT record_json FROM workload_delegations "
                "WHERE delegation_id = ? AND generation = ? AND digest = ? "
                "AND status = 'active'",
                (
                    delegation.delegation_id,
                    delegation.generation,
                    delegation.digest,
                ),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            return False
        try:
            mirrored = workload_models.WorkloadDelegationRecord.model_validate_json(
                row["record_json"]
            )
        except pydantic.ValidationError as exc:
            raise WorkloadStoreCorruptionError() from exc
        return mirrored == delegation and mirrored.digest == delegation.digest

    async def tombstone_delegation(
        self,
        *,
        delegation_id: str,
        generation: int,
        digest: str,
        expected_authority_epoch: int,
        authority_key: str,
        mutation_operation_id: str,
        source_slot_digest: str | None,
        now: float,
        event: workload_models.WorkloadAuditEvent,
    ) -> int:
        """Tombstone one exact delegation and invalidate permits atomically。"""
        self._require_authority(
            event.authority_id == delegation_id
            and event.authority_generation == generation
            and event.details_digest == digest
            and bool(authority_key)
            and bool(mutation_operation_id)
        )
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT authority_epoch, active_mutation_operation_id, "
                    "active_mutation_kind "
                    "FROM workload_runtime_state WHERE singleton = 1"
                ) as cursor:
                    epoch_row = await cursor.fetchone()
                self._require_authority(
                    epoch_row is not None
                    and int(epoch_row["authority_epoch"]) == expected_authority_epoch
                    and str(epoch_row["active_mutation_operation_id"])
                    == mutation_operation_id
                )
                async with db.execute(
                    "SELECT record_json FROM workload_delegations "
                    "WHERE delegation_id = ? AND generation = ? AND digest = ? "
                    "AND status = 'active'",
                    (delegation_id, generation, digest),
                ) as cursor:
                    delegation_row = await cursor.fetchone()
                self._require_authority(delegation_row is not None)
                try:
                    mirrored = workload_models.WorkloadDelegationRecord.model_validate_json(
                        delegation_row["record_json"]
                    )
                except pydantic.ValidationError as exc:
                    raise WorkloadStoreCorruptionError() from exc
                expected_source_slot_digest = (
                    "sha256:"
                    f"{hashlib.sha256(mirrored.source_slot.canonical_bytes()).hexdigest()}"
                )
                if (
                    str(epoch_row["active_mutation_kind"])
                    == workload_models.WorkloadMutationKind.SOURCE_DISCONNECT.value
                ):
                    self._require_authority(
                        source_slot_digest == expected_source_slot_digest
                    )
                else:
                    self._require_authority(source_slot_digest is None)
                result = await db.execute(
                    "UPDATE workload_delegations SET status = 'tombstoned', updated_at = ? "
                    "WHERE delegation_id = ? AND generation = ? AND digest = ? "
                    "AND status = 'active'",
                    (now, delegation_id, generation, digest),
                )
                self._require_authority(result.rowcount == 1)
                new_epoch = expected_authority_epoch + 1
                await db.execute(
                    "UPDATE workload_runtime_state SET authority_epoch = ? "
                    "WHERE singleton = 1",
                    (new_epoch,),
                )
                await db.execute(
                    "INSERT INTO workload_revocations "
                    "(authority_key, authority_generation, credential_id, "
                    "status, source_slot_digest, mutation_operation_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        authority_key,
                        generation,
                        delegation_id,
                        workload_models.WorkloadRevocationStatus.DRAINING.value,
                        source_slot_digest,
                        mutation_operation_id,
                        now,
                    ),
                )
                await self._insert_audit_event(db, event)
            except (
                WorkloadAuditConflictError,
                WorkloadAuthorityConflictError,
                WorkloadStoreCorruptionError,
            ):
                await db.rollback()
                raise
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise WorkloadAuthorityConflictError() from exc
            await db.commit()
        return new_epoch

    async def tombstone_credential(
        self,
        *,
        credential_id: str,
        credential_generation: int,
        expected_authority_epoch: int,
        authority_key: str | None = None,
        mutation_operation_id: str,
        now: float,
        event: workload_models.WorkloadAuditEvent,
    ) -> int:
        """Tombstone, increment epoch, and enqueue audit in one transaction。"""
        self._require_authority(
            event.authority_id == credential_id
            and event.authority_generation == credential_generation
            and (authority_key is None or bool(authority_key))
            and bool(mutation_operation_id)
        )
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT authority_epoch, active_mutation_operation_id "
                    "FROM workload_runtime_state WHERE singleton = 1"
                ) as cursor:
                    epoch_row = await cursor.fetchone()
                self._require_authority(
                    epoch_row is not None
                    and int(epoch_row["authority_epoch"]) == expected_authority_epoch
                    and str(epoch_row["active_mutation_operation_id"])
                    == mutation_operation_id
                )
                result = await db.execute(
                    "UPDATE workload_credentials SET status = 'tombstoned', updated_at = ? "
                    "WHERE credential_id = ? AND credential_generation = ? "
                    "AND status = 'active'",
                    (now, credential_id, credential_generation),
                )
                self._require_authority(result.rowcount == 1)
                new_epoch = expected_authority_epoch + 1
                await db.execute(
                    "UPDATE workload_runtime_state SET authority_epoch = ? "
                    "WHERE singleton = 1",
                    (new_epoch,),
                )
                if authority_key is not None:
                    await db.execute(
                        "INSERT INTO workload_revocations "
                        "(authority_key, authority_generation, credential_id, "
                        "status, source_slot_digest, mutation_operation_id, updated_at) "
                        "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                        (
                            authority_key,
                            credential_generation,
                            credential_id,
                            workload_models.WorkloadRevocationStatus.DRAINING.value,
                            mutation_operation_id,
                            now,
                        ),
                    )
                await self._insert_audit_event(db, event)
            except (WorkloadAuditConflictError, WorkloadAuthorityConflictError):
                await db.rollback()
                raise
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise WorkloadAuthorityConflictError() from exc
            await db.commit()
        return new_epoch

    async def get_revocation_status(
        self,
        *,
        authority_key: str,
        authority_generation: int,
    ) -> workload_models.WorkloadRevocationStatus | None:
        """Return the exact durable revoke outcome for one authority generation。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT status FROM workload_revocations "
                "WHERE authority_key = ? AND authority_generation = ?",
                (authority_key, authority_generation),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            return workload_models.WorkloadRevocationStatus(str(row["status"]))
        except ValueError as exc:
            raise WorkloadStoreCorruptionError() from exc

    async def list_blocking_revocations(
        self,
        *,
        source_slot_digest: str | None = None,
    ) -> list[workload_models.WorkloadRevocationRecord]:
        """Return every durable drain that has not reached explicit completion。"""
        source_filter = (
            " AND source_slot_digest = ?" if source_slot_digest is not None else ""
        )
        parameters: tuple[str, ...] = (
            workload_models.WorkloadRevocationStatus.DRAINING.value,
            workload_models.WorkloadRevocationStatus.REVOCATION_PENDING.value,
            workload_models.WorkloadRevocationStatus.RECOVERY_REQUIRED.value,
        )
        if source_slot_digest is not None:
            parameters += (source_slot_digest,)
        async with (
            self._connection() as db,
            db.execute(
                "SELECT authority_key, authority_generation, credential_id, "
                "status, source_slot_digest, mutation_operation_id, updated_at "
                "FROM workload_revocations WHERE status IN (?, ?, ?)"
                + source_filter
                + " "
                "ORDER BY updated_at, authority_key, authority_generation",
                parameters,
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        try:
            return [
                workload_models.WorkloadRevocationRecord(
                    authority_key=str(row["authority_key"]),
                    authority_id=str(row["credential_id"]),
                    authority_generation=int(row["authority_generation"]),
                    status=workload_models.WorkloadRevocationStatus(
                        str(row["status"])
                    ),
                    source_slot_digest=(
                        str(row["source_slot_digest"])
                        if row["source_slot_digest"] is not None
                        else None
                    ),
                    mutation_operation_id=(
                        str(row["mutation_operation_id"])
                        if row["mutation_operation_id"] is not None
                        else None
                    ),
                    updated_at=float(row["updated_at"]),
                )
                for row in rows
            ]
        except (TypeError, ValueError) as exc:
            raise WorkloadStoreCorruptionError() from exc

    async def record_pending_revocation(
        self,
        *,
        authority_key: str,
        authority_generation: int,
        authority_id: str,
        mutation_operation_id: str,
        source_slot_digest: str | None = None,
        now: float,
        event: workload_models.WorkloadAuditEvent,
    ) -> None:
        """Durably expose a bounded drain timeout without reopening authority。"""
        self._require_authority(
            bool(authority_key)
            and bool(mutation_operation_id)
            and event.authority_id == authority_id
            and event.authority_generation == authority_generation
        )
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT active_mutation_operation_id "
                    "FROM workload_runtime_state WHERE singleton = 1"
                ) as cursor:
                    owner_row = await cursor.fetchone()
                self._require_authority(
                    owner_row is not None
                    and str(owner_row["active_mutation_operation_id"])
                    == mutation_operation_id
                )
                await db.execute(
                    "INSERT INTO workload_revocations "
                    "(authority_key, authority_generation, credential_id, "
                    "status, source_slot_digest, mutation_operation_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(authority_key, authority_generation) DO UPDATE SET "
                    "credential_id = excluded.credential_id, "
                    "status = excluded.status, "
                    "source_slot_digest = excluded.source_slot_digest, "
                    "mutation_operation_id = excluded.mutation_operation_id, "
                    "updated_at = excluded.updated_at",
                    (
                        authority_key,
                        authority_generation,
                        authority_id,
                        workload_models.WorkloadRevocationStatus.REVOCATION_PENDING.value,
                        source_slot_digest,
                        mutation_operation_id,
                        now,
                    ),
                )
                await self._insert_audit_event(db, event)
            except (WorkloadAuditConflictError, WorkloadAuthorityConflictError):
                await db.rollback()
                raise
            await db.commit()

    async def finalize_revocation(
        self,
        *,
        authority_key: str,
        authority_generation: int,
        mutation_operation_id: str,
        status: workload_models.WorkloadRevocationStatus,
        now: float,
        event: workload_models.WorkloadAuditEvent,
    ) -> None:
        """Persist completed or pending only after cancellation/drain resolves。"""
        self._require_authority(
            status
            in {
                workload_models.WorkloadRevocationStatus.COMPLETED,
                workload_models.WorkloadRevocationStatus.REVOCATION_PENDING,
            }
            and event.authority_generation == authority_generation
            and bool(mutation_operation_id)
        )
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT active_mutation_operation_id "
                    "FROM workload_runtime_state WHERE singleton = 1"
                ) as cursor:
                    owner_row = await cursor.fetchone()
                self._require_authority(
                    owner_row is not None
                    and str(owner_row["active_mutation_operation_id"])
                    == mutation_operation_id
                )
                result = await db.execute(
                    "UPDATE workload_revocations SET status = ?, updated_at = ? "
                    "WHERE authority_key = ? AND authority_generation = ? "
                    "AND credential_id = ? AND mutation_operation_id = ? "
                    "AND status IN (?, ?)",
                    (
                        status.value,
                        now,
                        authority_key,
                        authority_generation,
                        event.authority_id,
                        mutation_operation_id,
                        workload_models.WorkloadRevocationStatus.DRAINING.value,
                        workload_models.WorkloadRevocationStatus.REVOCATION_PENDING.value,
                    ),
                )
                self._require_authority(result.rowcount == 1)
                await self._insert_audit_event(db, event)
            except (WorkloadAuditConflictError, WorkloadAuthorityConflictError):
                await db.rollback()
                raise
            await db.commit()

    async def authority_epoch(self) -> int:
        """Return the durable epoch copied into each ephemeral egress permit。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT authority_epoch FROM workload_runtime_state WHERE singleton = 1"
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            raise WorkloadStoreCorruptionError()
        return int(row["authority_epoch"])

    def replace_keyring(self, keyring: oauth_keyring.OAuthKeyRing) -> None:
        """Swap the in-memory ring only after its durable source is verified。"""
        self._keyring = keyring

    async def workload_key_rotation(self) -> oauth_models.OAuthKeyRotationRecord:
        """Return the non-secret single-process workload key state。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT key_rotation_phase, current_kid, retiring_kid, key_epoch "
                "FROM workload_runtime_state WHERE singleton = 1"
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            raise WorkloadStoreCorruptionError()
        try:
            return oauth_models.OAuthKeyRotationRecord(
                phase=oauth_models.OAuthKeyRotationPhase(
                    str(row["key_rotation_phase"])
                ),
                current_kid=str(row["current_kid"]),
                retiring_kid=(
                    str(row["retiring_kid"])
                    if row["retiring_kid"] is not None
                    else None
                ),
                epoch=int(row["key_epoch"]),
            )
        except (TypeError, ValueError, pydantic.ValidationError) as exc:
            raise WorkloadStoreCorruptionError() from exc

    async def claim_mutation(
        self,
        *,
        operation_id: str,
        mutation_kind: workload_models.WorkloadMutationKind | str,
    ) -> None:
        """Claim or idempotently resume the sole exact durable mutation owner。"""
        try:
            owner = workload_models.WorkloadMutationOwner(
                operation_id=operation_id,
                mutation_kind=mutation_kind,
            )
        except (TypeError, ValueError, pydantic.ValidationError) as exc:
            raise WorkloadAuthorityConflictError() from exc
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT active_mutation_operation_id, active_mutation_kind "
                "FROM workload_runtime_state WHERE singleton = 1"
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                raise WorkloadAuthorityConflictError()
            active_id = row["active_mutation_operation_id"]
            active_kind = row["active_mutation_kind"]
            if active_id is None and active_kind is None:
                result = await db.execute(
                    "UPDATE workload_runtime_state SET "
                    "active_mutation_operation_id = ?, active_mutation_kind = ? "
                    "WHERE singleton = 1 AND active_mutation_operation_id IS NULL "
                    "AND active_mutation_kind IS NULL",
                    (owner.operation_id, owner.mutation_kind.value),
                )
                if result.rowcount != 1:
                    await db.rollback()
                    raise WorkloadAuthorityConflictError()
            elif (
                str(active_id) != owner.operation_id
                or str(active_kind) != owner.mutation_kind.value
            ):
                await db.rollback()
                raise WorkloadAuthorityConflictError()
            await db.commit()

    async def active_mutation(
        self,
    ) -> workload_models.WorkloadMutationOwner | None:
        """Return the exact durable mutation owner or fail on a split row。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT active_mutation_operation_id, active_mutation_kind "
                "FROM workload_runtime_state WHERE singleton = 1"
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            raise WorkloadStoreCorruptionError()
        operation_id = row["active_mutation_operation_id"]
        mutation_kind = row["active_mutation_kind"]
        if operation_id is None and mutation_kind is None:
            return None
        if operation_id is None or mutation_kind is None:
            raise WorkloadStoreCorruptionError()
        try:
            return workload_models.WorkloadMutationOwner(
                operation_id=str(operation_id),
                mutation_kind=str(mutation_kind),
            )
        except (TypeError, ValueError, pydantic.ValidationError) as exc:
            raise WorkloadStoreCorruptionError() from exc

    async def complete_mutation(
        self,
        *,
        operation_id: str,
        mutation_kind: workload_models.WorkloadMutationKind | str,
    ) -> None:
        """Clear the durable owner only through exact operation-and-kind CAS。"""
        try:
            owner = workload_models.WorkloadMutationOwner(
                operation_id=operation_id,
                mutation_kind=mutation_kind,
            )
        except (TypeError, ValueError, pydantic.ValidationError) as exc:
            raise WorkloadAuthorityConflictError() from exc
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            result = await db.execute(
                "UPDATE workload_runtime_state SET "
                "active_mutation_operation_id = NULL, active_mutation_kind = NULL "
                "WHERE singleton = 1 AND active_mutation_operation_id = ? "
                "AND active_mutation_kind = ?",
                (owner.operation_id, owner.mutation_kind.value),
            )
            if result.rowcount != 1:
                await db.rollback()
                raise WorkloadAuthorityConflictError()
            await db.commit()

    async def runtime_authority_ready(
        self,
        *,
        mutation_operation_id: str | None = None,
    ) -> bool:
        """Deny while any global flag, transition, drain, or rotation blocks egress。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT global_enabled, audit_gate_closed, key_rotation_phase, "
                "active_mutation_operation_id, active_mutation_kind, "
                "NOT EXISTS(SELECT 1 FROM workload_revocations "
                "WHERE status IN ('draining', 'revocation_pending', "
                "'recovery_required')) AS no_revocations, "
                "NOT EXISTS(SELECT 1 FROM workload_authority_transitions "
                "WHERE status IN ('pending_json', 'recovery_required')) AS no_transitions "
                "FROM workload_runtime_state WHERE singleton = 1"
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            return False
        if mutation_operation_id is None:
            mutation_owner_matches = (
                row["active_mutation_operation_id"] is None
                and row["active_mutation_kind"] is None
            )
        else:
            mutation_owner_matches = (
                row["active_mutation_operation_id"] == mutation_operation_id
                and row["active_mutation_kind"] is not None
            )
        return bool(
            int(row["global_enabled"]) == 1
            and int(row["audit_gate_closed"]) == 0
            and str(row["key_rotation_phase"]) == "stable"
            and int(row["no_revocations"]) == 1
            and int(row["no_transitions"]) == 1
            and mutation_owner_matches
        )

    async def begin_workload_key_rotation(
        self,
        *,
        new_kid: str,
        now: float,
        event: workload_models.WorkloadAuditEvent,
    ) -> tuple[oauth_models.OAuthKeyRotationRecord, int]:
        """Fence egress and persist prepare before publishing the dual keyring。"""
        if not new_kid:
            raise WorkloadAuthorityConflictError()
        self._require_authority(event.created_at == now)
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM workload_runtime_state WHERE singleton = 1"
                ) as cursor:
                    row = await cursor.fetchone()
                self._require_authority(
                    row is not None
                    and str(row["key_rotation_phase"]) == "stable"
                    and str(row["current_kid"]) != new_kid
                )
                key_epoch = int(row["key_epoch"]) + 1
                authority_epoch = int(row["authority_epoch"]) + 1
                self._require_authority(
                    event.authority_id == "workload-keyring"
                    and event.authority_generation == key_epoch
                )
                await db.execute(
                    "UPDATE workload_runtime_state SET key_rotation_phase = 'prepare', "
                    "current_kid = ?, retiring_kid = ?, key_epoch = ?, "
                    "authority_epoch = ? WHERE singleton = 1",
                    (
                        new_kid,
                        str(row["current_kid"]),
                        key_epoch,
                        authority_epoch,
                    ),
                )
                await self._insert_audit_event(db, event)
            except (WorkloadAuditConflictError, WorkloadAuthorityConflictError):
                await db.rollback()
                raise
            await db.commit()
        return (
            oauth_models.OAuthKeyRotationRecord(
                phase=oauth_models.OAuthKeyRotationPhase.PREPARE,
                current_kid=new_kid,
                retiring_kid=str(row["current_kid"]),
                epoch=key_epoch,
            ),
            authority_epoch,
        )

    async def set_workload_key_rotation_phase(
        self,
        *,
        epoch: int,
        expected: oauth_models.OAuthKeyRotationPhase,
        target: oauth_models.OAuthKeyRotationPhase,
    ) -> None:
        """Advance one exact recovery phase without weakening its fence。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            result = await db.execute(
                "UPDATE workload_runtime_state SET key_rotation_phase = ? "
                "WHERE singleton = 1 AND key_epoch = ? AND key_rotation_phase = ?",
                (target.value, epoch, expected.value),
            )
            if result.rowcount != 1:
                await db.rollback()
                raise WorkloadAuthorityConflictError()
            await db.commit()

    async def rollback_workload_key_rotation_prepare(self, *, epoch: int) -> None:
        """Restore the old current ID only when durable key publication never occurred。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            result = await db.execute(
                "UPDATE workload_runtime_state SET key_rotation_phase = 'stable', "
                "current_kid = retiring_kid, retiring_kid = NULL "
                "WHERE singleton = 1 AND key_epoch = ? "
                "AND key_rotation_phase = 'prepare' AND retiring_kid IS NOT NULL",
                (epoch,),
            )
            if result.rowcount != 1:
                await db.rollback()
                raise WorkloadAuthorityConflictError()
            await db.commit()

    async def reencrypt_workload_credentials(self) -> int:
        """Re-encrypt every credential and metadata envelope in one transaction。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT credential_id, credential_generation, encrypted_payload "
                "FROM workload_credentials"
            ) as cursor:
                rows = await cursor.fetchall()
            replacements: list[tuple[str, str, int]] = []
            for row in rows:
                credential_id = str(row["credential_id"])
                generation = int(row["credential_generation"])
                credential = self._open_credential(
                    credential_id=credential_id,
                    generation=generation,
                    encrypted_payload=str(row["encrypted_payload"]),
                )
                replacements.append(
                    (self._seal_credential(credential), credential_id, generation)
                )
            await db.executemany(
                "UPDATE workload_credentials SET encrypted_payload = ? "
                "WHERE credential_id = ? AND credential_generation = ?",
                replacements,
            )
            async with db.execute(
                "SELECT profile_id, profile_generation, profile_fingerprint, "
                "encrypted_snapshot FROM workload_metadata_snapshots"
            ) as cursor:
                metadata_rows = await cursor.fetchall()
            metadata_replacements: list[tuple[str, str, int]] = []
            for row in metadata_rows:
                profile_id = str(row["profile_id"])
                profile_generation = int(row["profile_generation"])
                profile_fingerprint = str(row["profile_fingerprint"])
                snapshot = self._open_metadata_snapshot(
                    profile_id=profile_id,
                    profile_generation=profile_generation,
                    profile_fingerprint=profile_fingerprint,
                    encrypted_snapshot=str(row["encrypted_snapshot"]),
                )
                metadata_replacements.append(
                    (
                        self._seal_metadata_snapshot(snapshot),
                        profile_id,
                        profile_generation,
                    )
                )
            await db.executemany(
                "UPDATE workload_metadata_snapshots SET encrypted_snapshot = ? "
                "WHERE profile_id = ? AND profile_generation = ?",
                metadata_replacements,
            )
            await db.commit()
        return len(replacements) + len(metadata_replacements)

    async def count_workload_key_references(self, kid: str) -> int:
        """Count ciphertext envelopes that still name one retiring key ID。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT encrypted_payload AS envelope FROM workload_credentials "
                "UNION ALL SELECT encrypted_snapshot AS envelope "
                "FROM workload_metadata_snapshots"
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        count = 0
        for row in rows:
            try:
                sealed = oauth_keyring.OAuthSealedValue.model_validate_json(
                    row["envelope"]
                )
            except pydantic.ValidationError as exc:
                raise WorkloadStoreCorruptionError() from exc
            count += int(sealed.kid == kid)
        return count

    async def complete_workload_key_rotation(
        self,
        *,
        epoch: int,
        now: float,
        event: workload_models.WorkloadAuditEvent,
    ) -> oauth_models.OAuthKeyRotationRecord:
        """Commit stable state and its sanitized result event atomically。"""
        self._require_authority(event.created_at == now)
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM workload_runtime_state WHERE singleton = 1"
                ) as cursor:
                    row = await cursor.fetchone()
                self._require_authority(
                    row is not None
                    and int(row["key_epoch"]) == epoch
                    and str(row["key_rotation_phase"]) == "dual-read-new-write"
                    and row["retiring_kid"] is not None
                    and event.authority_id == "workload-keyring"
                    and event.authority_generation == epoch
                )
                await db.execute(
                    "UPDATE workload_runtime_state SET key_rotation_phase = 'stable', "
                    "retiring_kid = NULL WHERE singleton = 1",
                )
                await self._insert_audit_event(db, event)
            except (WorkloadAuditConflictError, WorkloadAuthorityConflictError):
                await db.rollback()
                raise
            await db.commit()
        return oauth_models.OAuthKeyRotationRecord(
            phase=oauth_models.OAuthKeyRotationPhase.STABLE,
            current_kid=str(row["current_kid"]),
            epoch=epoch,
        )

    async def pending_audit_events(self) -> list[workload_models.WorkloadAuditEvent]:
        """Read pending outbox events in deterministic creation order。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT payload_json FROM workload_audit_outbox "
                "WHERE status = 'pending' ORDER BY created_at, event_id"
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        try:
            return [
                workload_models.WorkloadAuditEvent.model_validate_json(row["payload_json"])
                for row in rows
            ]
        except pydantic.ValidationError as exc:
            raise WorkloadStoreCorruptionError() from exc

    async def enqueue_audit_event(
        self,
        event: workload_models.WorkloadAuditEvent,
    ) -> None:
        """Durably accept one standalone security intent before network egress。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._insert_audit_event(db, event)
            except WorkloadAuditConflictError:
                await db.rollback()
                raise
            await db.commit()

    async def pending_audit_count(self) -> int:
        """Return the durable outbox depth used by the fail-closed watermark。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT COUNT(*) FROM workload_audit_outbox WHERE status = 'pending'"
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            raise WorkloadStoreCorruptionError()
        return int(row[0])

    async def acknowledge_audit_event(self, event_id: str, *, acked_at: float) -> None:
        """Idempotently acknowledge one event only after its sink returns success。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            result = await db.execute(
                "UPDATE workload_audit_outbox SET status = 'acked', acked_at = ? "
                "WHERE event_id = ? AND status = 'pending'",
                (acked_at, event_id),
            )
            if result.rowcount == 0:
                async with db.execute(
                    "SELECT status FROM workload_audit_outbox WHERE event_id = ?",
                    (event_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None or str(row["status"]) != "acked":
                    await db.rollback()
                    raise WorkloadAuditConflictError()
            await db.commit()

    @staticmethod
    def _transition_from_row(
        row: aiosqlite.Row,
    ) -> workload_models.WorkloadAuthorityTransition:
        try:
            return workload_models.WorkloadAuthorityTransition(
                operation_id=row["operation_id"],
                direction=row["direction"],
                authority_kind=row["authority_kind"],
                authority_id=row["authority_id"],
                authority_generation=row["authority_generation"],
                actor_id=row["actor_id"],
                request_digest=row["request_digest"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except (KeyError, pydantic.ValidationError) as exc:
            raise WorkloadStoreCorruptionError() from exc

    @staticmethod
    def _transition_matches(
        record: workload_models.WorkloadAuthorityTransition,
        *,
        direction: str,
        authority_kind: str,
        authority_id: str,
        authority_generation: int,
        actor_id: str,
        request_digest: str,
    ) -> bool:
        return (
            record.direction == direction
            and record.authority_kind == authority_kind
            and record.authority_id == authority_id
            and record.authority_generation == authority_generation
            and record.actor_id == actor_id
            and record.request_digest == request_digest
        )

    async def begin_authority_transition(
        self,
        *,
        operation_id: str,
        direction: str,
        authority_kind: str,
        authority_id: str,
        authority_generation: int,
        actor_id: str,
        request_digest: str,
        now: float,
        event: workload_models.WorkloadAuditEvent,
    ) -> workload_models.WorkloadAuthorityTransition:
        """Persist mutation intent; reductions invalidate permits before JSON I/O。"""
        candidate = workload_models.WorkloadAuthorityTransition(
            operation_id=operation_id,
            direction=direction,
            authority_kind=authority_kind,
            authority_id=authority_id,
            authority_generation=authority_generation,
            actor_id=actor_id,
            request_digest=request_digest,
            status="pending_json",
            created_at=now,
            updated_at=now,
        )
        self._require_authority(
            event.authority_id == authority_id
            and event.authority_generation == authority_generation
            and event.actor_id == actor_id
        )
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM workload_authority_transitions WHERE operation_id = ?",
                    (operation_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is not None:
                    existing = self._transition_from_row(row)
                    self._require_authority(
                        self._transition_matches(
                            existing,
                            direction=direction,
                            authority_kind=authority_kind,
                            authority_id=authority_id,
                            authority_generation=authority_generation,
                            actor_id=actor_id,
                            request_digest=request_digest,
                        )
                    )
                    await db.rollback()
                    return existing
                if direction == "reduction":
                    await db.execute(
                        "UPDATE workload_runtime_state "
                        "SET authority_epoch = authority_epoch + 1 WHERE singleton = 1"
                    )
                await db.execute(
                    "INSERT INTO workload_authority_transitions "
                    "(operation_id, direction, authority_kind, authority_id, "
                    "authority_generation, actor_id, request_digest, status, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        candidate.operation_id,
                        candidate.direction,
                        candidate.authority_kind,
                        candidate.authority_id,
                        candidate.authority_generation,
                        candidate.actor_id,
                        candidate.request_digest,
                        candidate.status,
                        candidate.created_at,
                        candidate.updated_at,
                    ),
                )
                await self._insert_audit_event(db, event)
            except (WorkloadAuditConflictError, WorkloadAuthorityConflictError):
                await db.rollback()
                raise
            await db.commit()
        return candidate

    async def mark_transition_recovery_required(
        self,
        operation_id: str,
        *,
        now: float,
    ) -> workload_models.WorkloadAuthorityTransition:
        """Mark an interrupted JSON phase without making expansion usable。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT * FROM workload_authority_transitions WHERE operation_id = ?",
                (operation_id,),
            ) as cursor:
                row = await cursor.fetchone()
            self._require_authority(row is not None)
            record = self._transition_from_row(row)
            self._require_authority(
                record.status in {"pending_json", "recovery_required"}
            )
            if record.status == "pending_json":
                await db.execute(
                    "UPDATE workload_authority_transitions "
                    "SET status = 'recovery_required', updated_at = ? "
                    "WHERE operation_id = ?",
                    (now, operation_id),
                )
            await db.commit()
        return record.model_copy(
            update={"status": "recovery_required", "updated_at": now}
        )

    async def has_blocking_authority_transition(
        self,
        *,
        authority_kind: str,
        authority_id: str,
    ) -> bool:
        """Deny runtime authority while any JSON/SQLite mutation is indeterminate。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT 1 FROM workload_authority_transitions "
                "WHERE authority_kind = ? AND authority_id = ? "
                "AND status IN ('pending_json', 'recovery_required') LIMIT 1",
                (authority_kind, authority_id),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return row is not None

    async def list_blocking_authority_transitions(
        self,
    ) -> list[workload_models.WorkloadAuthorityTransition]:
        """Expose only non-secret indeterminate transitions to the recovery API。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT * FROM workload_authority_transitions "
                "WHERE status IN ('pending_json', 'recovery_required') "
                "ORDER BY created_at, operation_id"
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return [self._transition_from_row(row) for row in rows]

    async def get_authority_transition(
        self,
        operation_id: str,
    ) -> workload_models.WorkloadAuthorityTransition | None:
        """Load one transition by opaque operation ID for explicit recovery。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT * FROM workload_authority_transitions "
                "WHERE operation_id = ?",
                (operation_id,),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return self._transition_from_row(row) if row is not None else None

    async def reconcile_authority_transition(
        self,
        *,
        operation_id: str,
        now: float,
        event: workload_models.WorkloadAuditEvent,
    ) -> workload_models.WorkloadAuthorityTransition:
        """Finalize a server-verified JSON state under an explicit admin audit。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM workload_authority_transitions "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                self._require_authority(row is not None)
                record = self._transition_from_row(row)
                self._require_authority(
                    record.status in {"pending_json", "recovery_required"}
                    and event.authority_id == record.authority_id
                    and event.authority_generation == record.authority_generation
                    and event.details_digest == record.request_digest
                )
                status = "active" if record.direction == "expansion" else "completed"
                if record.direction == "expansion":
                    await db.execute(
                        "UPDATE workload_runtime_state "
                        "SET authority_epoch = authority_epoch + 1 WHERE singleton = 1"
                    )
                await db.execute(
                    "UPDATE workload_authority_transitions "
                    "SET status = ?, updated_at = ? WHERE operation_id = ?",
                    (status, now, operation_id),
                )
                await self._insert_audit_event(db, event)
            except (WorkloadAuditConflictError, WorkloadAuthorityConflictError):
                await db.rollback()
                raise
            await db.commit()
        return record.model_copy(update={"status": status, "updated_at": now})

    async def confirm_authority_transition(
        self,
        *,
        operation_id: str,
        actor_id: str,
        request_digest: str,
        now: float,
        event: workload_models.WorkloadAuditEvent,
    ) -> workload_models.WorkloadAuthorityTransition:
        """Confirm exact JSON replay and publish expansion or finish reduction。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM workload_authority_transitions WHERE operation_id = ?",
                    (operation_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                self._require_authority(row is not None)
                record = self._transition_from_row(row)
                self._require_authority(
                    record.actor_id == actor_id
                    and record.request_digest == request_digest
                    and record.status in {"pending_json", "recovery_required"}
                    and event.actor_id == actor_id
                    and event.authority_id == record.authority_id
                    and event.authority_generation == record.authority_generation
                )
                status = "active" if record.direction == "expansion" else "completed"
                if record.direction == "expansion":
                    await db.execute(
                        "UPDATE workload_runtime_state "
                        "SET authority_epoch = authority_epoch + 1 WHERE singleton = 1"
                    )
                await db.execute(
                    "UPDATE workload_authority_transitions SET status = ?, updated_at = ? "
                    "WHERE operation_id = ?",
                    (status, now, operation_id),
                )
                await self._insert_audit_event(db, event)
            except (WorkloadAuditConflictError, WorkloadAuthorityConflictError):
                await db.rollback()
                raise
            await db.commit()
        return record.model_copy(update={"status": status, "updated_at": now})

    async def sqlite_durability_settings(self) -> dict[str, int | str]:
        """Expose exact SQLite durability pragmas for startup self-checks。"""
        async with self._connection() as db:
            async with db.execute("PRAGMA journal_mode") as cursor:
                journal_row = await cursor.fetchone()
            async with db.execute("PRAGMA synchronous") as cursor:
                sync_row = await cursor.fetchone()
        if journal_row is None or sync_row is None:
            raise WorkloadStoreCorruptionError()
        return {
            "journal_mode": str(journal_row[0]).lower(),
            "synchronous": int(sync_row[0]),
        }
