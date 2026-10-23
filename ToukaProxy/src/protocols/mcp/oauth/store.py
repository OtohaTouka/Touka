# ruff: noqa: TC002
"""P8-A encrypted SQLite repository for OAuth transactions and grants。"""

from __future__ import annotations

import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
from pydantic import SecretStr
from src.protocols.mcp.oauth.keyring import OAuthKeyRing, OAuthSealedValue
from src.protocols.mcp.oauth.models import (
    OAuthAuthorizationSlotKey,
    OAuthCallbackRouteError,
    OAuthGrantBinding,
    OAuthGrantRecord,
    OAuthGrantStatus,
    OAuthKeyRotationPhase,
    OAuthKeyRotationRecord,
    OAuthRefreshLease,
    OAuthSlotSnapshot,
    OAuthTokenSet,
    OAuthTransactionConflictError,
    OAuthTransactionPayload,
    OAuthTransactionRecord,
    OAuthTransactionStatus,
)
from src.protocols.mcp.oauth.registration import (
    OAuthClientBinding,
    OAuthRegistrationKey,
    OAuthRegistrationLease,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


class OAuthStoreCorruptionError(ValueError):
    """Durable HMAC index is ambiguous or otherwise corrupt。"""


class OAuthProfileMutationInProgressError(ValueError):
    """Profile is fenced for a cross-process mutation。"""


class OAuthSqliteStore:
    """Short-transaction, encrypted OAuth persistence boundary。"""

    def __init__(
        self,
        db_path: Path,
        keyring: OAuthKeyRing,
        *,
        initial_rotation_phase: OAuthKeyRotationPhase | str = (OAuthKeyRotationPhase.STABLE),
        initial_key_epoch: int = 1,
    ) -> None:
        phase = OAuthKeyRotationPhase(initial_rotation_phase)
        if initial_key_epoch < 1:
            raise ValueError()
        if (
            phase
            not in {
                OAuthKeyRotationPhase.STABLE,
                OAuthKeyRotationPhase.PREPARE,
            }
            and len(keyring.retiring) != 1
        ):
            raise OAuthStoreCorruptionError()
        self._db_path = Path(db_path)
        self._keyring = keyring
        self._initial_rotation_phase = phase
        self._initial_key_epoch = initial_key_epoch

    def replace_keyring(self, keyring: OAuthKeyRing) -> None:
        """Atomically replace the process-local read/write key ring."""
        self._keyring = keyring

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(str(self._db_path), timeout=5.0)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("PRAGMA foreign_keys = ON")
            yield db
        finally:
            await db.close()

    async def prepare_schema(self) -> None:
        """Create the versioned P8-A schema without touching application data。"""
        async with self._connection() as db:
            async with db.execute("PRAGMA journal_mode = WAL") as cursor:
                journal_row = await cursor.fetchone()
            journal_mode = str(journal_row[0]).lower() if journal_row else ""
            if journal_mode not in {"wal", "delete"}:
                raise OAuthStoreCorruptionError()
            if journal_mode != "wal":
                logger.warning(
                    "OAuth SQLite WAL unavailable; explicit fallback mode=%s",
                    journal_mode,
                )
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS oauth_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS oauth_slots ("
                "slot_digest TEXT PRIMARY KEY, slot_kid TEXT NOT NULL, "
                "profile_id TEXT NOT NULL, version INTEGER NOT NULL, "
                "active_grant_id TEXT, encrypted_slot_key TEXT)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS oauth_transactions ("
                "transaction_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, "
                "state_kid TEXT NOT NULL, state_digest TEXT NOT NULL, "
                "slot_digest TEXT NOT NULL, slot_version INTEGER NOT NULL, "
                "status TEXT NOT NULL, expires_at REAL NOT NULL, "
                "redirect_route TEXT NOT NULL, encrypted_payload TEXT NOT NULL, "
                "created_at REAL NOT NULL, UNIQUE(state_kid, state_digest), "
                "FOREIGN KEY(slot_digest) REFERENCES oauth_slots(slot_digest))"
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS oauth_one_live_transaction_per_slot "
                "ON oauth_transactions(slot_digest) WHERE status IN "
                "('pending', 'consuming')"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS oauth_grants ("
                "grant_id TEXT PRIMARY KEY, slot_digest TEXT NOT NULL, "
                "grant_version INTEGER NOT NULL, status TEXT NOT NULL, "
                "encrypted_payload TEXT NOT NULL, access_expires_at REAL NOT NULL, "
                "has_refresh INTEGER NOT NULL, lease_owner TEXT, lease_fence TEXT, "
                "lease_expires_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
                "FOREIGN KEY(slot_digest) REFERENCES oauth_slots(slot_digest))"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS oauth_registrations ("
                "registration_key TEXT PRIMARY KEY, version INTEGER NOT NULL, "
                "status TEXT NOT NULL, lease_owner TEXT, lease_fence TEXT, "
                "lease_expires_at REAL, encrypted_payload TEXT, updated_at REAL NOT NULL, "
                "key_kid TEXT, encrypted_registration_key TEXT)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS oauth_profile_fences ("
                "profile_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL, "
                "status TEXT NOT NULL, applied_fingerprint TEXT)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS oauth_runtime_state ("
                "name TEXT PRIMARY KEY, numeric_value REAL NOT NULL)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS oauth_key_rotation ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
                "phase TEXT NOT NULL, current_kid TEXT NOT NULL, "
                "retiring_kid TEXT, epoch INTEGER NOT NULL, started_at REAL, "
                "state_drain_after REAL)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS oauth_worker_epochs ("
                "worker_id TEXT PRIMARY KEY, key_epoch INTEGER NOT NULL, "
                "heartbeat_at REAL NOT NULL)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS oauth_rotation_workers ("
                "epoch INTEGER NOT NULL, worker_id TEXT NOT NULL, loaded INTEGER NOT NULL, "
                "PRIMARY KEY(epoch, worker_id))"
            )
            await db.execute(
                "INSERT OR IGNORE INTO oauth_key_rotation "
                "(singleton, phase, current_kid, retiring_kid, epoch, started_at, "
                "state_drain_after) VALUES (1, ?, ?, ?, ?, ?, ?)",
                (
                    self._initial_rotation_phase.value,
                    self._keyring.current.kid,
                    (self._keyring.retiring[0].kid if self._keyring.retiring else None),
                    self._initial_key_epoch,
                    (0.0 if self._initial_rotation_phase != OAuthKeyRotationPhase.STABLE else None),
                    (0.0 if self._initial_rotation_phase != OAuthKeyRotationPhase.STABLE else None),
                ),
            )
            await db.execute(
                "INSERT OR IGNORE INTO oauth_schema_migrations (version, applied_at) VALUES (1, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO oauth_schema_migrations (version, applied_at) VALUES (2, 0)"
            )
            async with db.execute("PRAGMA table_info(oauth_slots)") as cursor:
                slot_columns = {str(row[1]) for row in await cursor.fetchall()}
            if "encrypted_slot_key" not in slot_columns:
                await db.execute("ALTER TABLE oauth_slots ADD COLUMN encrypted_slot_key TEXT")
            async with db.execute("PRAGMA table_info(oauth_registrations)") as cursor:
                registration_columns = {str(row[1]) for row in await cursor.fetchall()}
            if "key_kid" not in registration_columns:
                await db.execute("ALTER TABLE oauth_registrations ADD COLUMN key_kid TEXT")
            if "encrypted_registration_key" not in registration_columns:
                await db.execute(
                    "ALTER TABLE oauth_registrations ADD COLUMN encrypted_registration_key TEXT"
                )
            await db.execute(
                "INSERT OR IGNORE INTO oauth_schema_migrations (version, applied_at) VALUES (3, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO oauth_schema_migrations (version, applied_at) VALUES (4, 0)"
            )
            await db.commit()

    async def observe_clock(
        self,
        *,
        now: float,
        max_rollback_seconds: float,
    ) -> bool:
        """Persist a high-watermark and fail closed on excessive wall-clock rollback。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT numeric_value FROM oauth_runtime_state WHERE name = ?",
                ("clock_high_watermark",),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.execute(
                    "INSERT INTO oauth_runtime_state (name, numeric_value) VALUES (?, ?)",
                    ("clock_high_watermark", now),
                )
                await db.commit()
                return True
            high_watermark = float(row["numeric_value"])
            if now < high_watermark - max_rollback_seconds:
                await db.execute(
                    "UPDATE oauth_transactions SET status = ? WHERE status = ?",
                    (
                        OAuthTransactionStatus.CANCELLED.value,
                        OAuthTransactionStatus.PENDING.value,
                    ),
                )
                await db.execute(
                    "UPDATE oauth_grants SET status = ?, lease_owner = NULL, "
                    "lease_fence = NULL, lease_expires_at = NULL, updated_at = ? "
                    "WHERE status IN (?, ?)",
                    (
                        OAuthGrantStatus.FROZEN.value,
                        now,
                        OAuthGrantStatus.ACTIVE.value,
                        OAuthGrantStatus.REFRESHING.value,
                    ),
                )
                await db.execute(
                    "UPDATE oauth_registrations SET status = ?, lease_owner = NULL, "
                    "lease_fence = NULL, lease_expires_at = NULL, updated_at = ? "
                    "WHERE status IN (?, ?)",
                    ("frozen", now, "active", "registering"),
                )
                await db.commit()
                return False
            if now > high_watermark:
                await db.execute(
                    "UPDATE oauth_runtime_state SET numeric_value = ? WHERE name = ?",
                    (now, "clock_high_watermark"),
                )
            await db.commit()
            return True

    async def cleanup(self, *, now: float) -> dict[str, int]:
        """Recover expired leases and purge terminal encrypted payload rows。"""
        terminal_statuses = (
            OAuthTransactionStatus.CONSUMED_SUCCESS.value,
            OAuthTransactionStatus.CONSUMED_ERROR.value,
            OAuthTransactionStatus.EXPIRED.value,
            OAuthTransactionStatus.CANCELLED.value,
        )
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE oauth_transactions SET status = ? WHERE status = ? AND expires_at <= ?",
                (
                    OAuthTransactionStatus.EXPIRED.value,
                    OAuthTransactionStatus.PENDING.value,
                    now,
                ),
            )
            await db.execute(
                "UPDATE oauth_transactions SET status = ? WHERE status = ? AND expires_at <= ?",
                (
                    OAuthTransactionStatus.CONSUMED_ERROR.value,
                    OAuthTransactionStatus.CONSUMING.value,
                    now,
                ),
            )
            recovered = await db.execute(
                "UPDATE oauth_grants SET status = CASE WHEN has_refresh = 1 "
                "OR access_expires_at > ? THEN ? ELSE ? END, lease_owner = NULL, "
                "lease_fence = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE status = ? AND lease_expires_at <= ?",
                (
                    now,
                    OAuthGrantStatus.ACTIVE.value,
                    OAuthGrantStatus.EXPIRED.value,
                    now,
                    OAuthGrantStatus.REFRESHING.value,
                    now,
                ),
            )
            await db.execute(
                "UPDATE oauth_grants SET status = ?, updated_at = ? "
                "WHERE status = ? AND has_refresh = 0 AND access_expires_at <= ?",
                (
                    OAuthGrantStatus.EXPIRED.value,
                    now,
                    OAuthGrantStatus.ACTIVE.value,
                    now,
                ),
            )
            deleted_transactions = await db.execute(
                "DELETE FROM oauth_transactions WHERE status IN (?, ?, ?, ?)",
                terminal_statuses,
            )
            deleted_grants = await db.execute(
                "DELETE FROM oauth_grants WHERE status = ?",
                (OAuthGrantStatus.TOMBSTONED.value,),
            )
            await db.commit()
            return {
                "refresh_leases_recovered": recovered.rowcount,
                "terminal_transactions_deleted": deleted_transactions.rowcount,
                "tombstoned_grants_deleted": deleted_grants.rowcount,
            }

    @staticmethod
    def _sealed_json(sealed: OAuthSealedValue) -> str:
        return sealed.model_dump_json()

    @staticmethod
    def _transaction_bytes(payload: OAuthTransactionPayload) -> bytes:
        data = payload.model_dump(mode="json")
        data["pkce_verifier"] = payload.pkce_verifier.get_secret_value()
        data["requested_scopes"] = sorted(payload.requested_scopes)
        data["client_secret"] = (
            payload.client_secret.get_secret_value() if payload.client_secret is not None else None
        )
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _transaction_from_bytes(payload: bytes) -> OAuthTransactionPayload:
        data = json.loads(payload)
        data["pkce_verifier"] = SecretStr(data["pkce_verifier"])
        if data.get("client_secret") is not None:
            data["client_secret"] = SecretStr(data["client_secret"])
        return OAuthTransactionPayload.model_validate(data)

    @staticmethod
    def _grant_bytes(binding: OAuthGrantBinding, tokens: OAuthTokenSet) -> bytes:
        data: dict[str, Any] = {
            "binding": binding.model_dump(mode="json"),
            "tokens": tokens.model_dump(mode="json"),
        }
        data["binding"]["granted_scopes"] = sorted(binding.granted_scopes)
        data["binding"]["client_secret"] = (
            binding.client_secret.get_secret_value() if binding.client_secret is not None else None
        )
        data["tokens"]["access_token"] = tokens.access_token.get_secret_value()
        data["tokens"]["refresh_token"] = (
            tokens.refresh_token.get_secret_value() if tokens.refresh_token else None
        )
        data["tokens"]["granted_scopes"] = sorted(tokens.granted_scopes)
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _grant_from_bytes(payload: bytes) -> tuple[OAuthGrantBinding, OAuthTokenSet]:
        data = json.loads(payload)
        token_data = data["tokens"]
        token_data["access_token"] = SecretStr(token_data["access_token"])
        if token_data.get("refresh_token") is not None:
            token_data["refresh_token"] = SecretStr(token_data["refresh_token"])
        binding_data = data["binding"]
        if binding_data.get("client_secret") is not None:
            binding_data["client_secret"] = SecretStr(binding_data["client_secret"])
        return (
            OAuthGrantBinding.model_validate(binding_data),
            OAuthTokenSet.model_validate(token_data),
        )

    @staticmethod
    def _registration_bytes(binding: OAuthClientBinding) -> bytes:
        data = binding.model_dump(mode="json")
        data["client_secret"] = (
            binding.client_secret.get_secret_value() if binding.client_secret is not None else None
        )
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()

    @staticmethod
    def _registration_from_bytes(payload: bytes) -> OAuthClientBinding:
        data = json.loads(payload)
        if data.get("client_secret") is not None:
            data["client_secret"] = SecretStr(data["client_secret"])
        return OAuthClientBinding.model_validate(data)

    @staticmethod
    def _slot_key_bytes(slot: OAuthAuthorizationSlotKey) -> bytes:
        return slot.model_dump_json().encode("utf-8")

    @staticmethod
    def _slot_key_from_bytes(payload: bytes) -> OAuthAuthorizationSlotKey:
        return OAuthAuthorizationSlotKey.model_validate_json(payload)

    @staticmethod
    def _registration_key_bytes(key: OAuthRegistrationKey) -> bytes:
        return key.model_dump_json().encode("utf-8")

    @staticmethod
    def _registration_key_from_bytes(payload: bytes) -> OAuthRegistrationKey:
        return OAuthRegistrationKey.model_validate_json(payload)

    async def _find_slot_row(
        self,
        db: aiosqlite.Connection,
        slot: OAuthAuthorizationSlotKey,
    ) -> aiosqlite.Row | None:
        for _kid, digest in self._keyring.slot_digest_candidates(slot.canonical_bytes()):
            async with db.execute(
                "SELECT slot_digest, slot_kid, profile_id, version, active_grant_id "
                "FROM oauth_slots WHERE slot_digest = ?",
                (digest,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                return row
        return None

    def _decrypt_transaction_row(self, row: aiosqlite.Row) -> OAuthTransactionRecord:
        transaction_id = str(row["transaction_id"])
        try:
            sealed = OAuthSealedValue.model_validate_json(row["encrypted_payload"])
            payload = self._transaction_from_bytes(
                self._keyring.decrypt(
                    sealed,
                    aad=f"transaction:{transaction_id}".encode(),
                )
            )
        except Exception as exc:
            raise OAuthStoreCorruptionError() from exc
        return OAuthTransactionRecord(
            transaction_id=transaction_id,
            operation_id=row["operation_id"],
            slot_digest=row["slot_digest"],
            slot_version=row["slot_version"],
            status=row["status"],
            expires_at=row["expires_at"],
            payload=payload,
        )

    def _decrypt_grant_row(self, row: aiosqlite.Row) -> OAuthGrantRecord:
        grant_id = str(row["grant_id"])
        try:
            sealed = OAuthSealedValue.model_validate_json(row["encrypted_payload"])
            binding, tokens = self._grant_from_bytes(
                self._keyring.decrypt(sealed, aad=f"grant:{grant_id}".encode())
            )
        except Exception as exc:
            raise OAuthStoreCorruptionError() from exc
        return OAuthGrantRecord(
            grant_id=grant_id,
            slot_digest=row["slot_digest"],
            grant_version=row["grant_version"],
            status=row["status"],
            binding=binding,
            tokens=tokens,
        )

    async def create_transaction(
        self,
        *,
        slot: OAuthAuthorizationSlotKey,
        operation_id: str,
        raw_state: str,
        payload: OAuthTransactionPayload,
        now: float,
        expires_at: float,
    ) -> OAuthTransactionRecord:
        """Persist one pending transaction; raw state is never stored。"""
        transaction_id = secrets.token_urlsafe(16)
        state_kid, state_digest = self._keyring.state_digest(raw_state)
        sealed = self._keyring.encrypt(
            self._transaction_bytes(payload),
            aad=f"transaction:{transaction_id}".encode(),
        )
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT status FROM oauth_profile_fences WHERE profile_id = ?",
                (slot.auth_profile_id,),
            ) as cursor:
                profile_fence = await cursor.fetchone()
            if profile_fence is not None and profile_fence["status"] == "updating":
                await db.rollback()
                raise OAuthProfileMutationInProgressError()
            slot_row = await self._find_slot_row(db, slot)
            if slot_row is None:
                slot_kid, slot_digest = self._keyring.slot_digest(slot.canonical_bytes())
                slot_version = 1
                sealed_slot_key = self._keyring.encrypt(
                    self._slot_key_bytes(slot),
                    aad=f"slot:{slot_digest}".encode(),
                )
                await db.execute(
                    "INSERT INTO oauth_slots "
                    "(slot_digest, slot_kid, profile_id, version, active_grant_id, "
                    "encrypted_slot_key) VALUES (?, ?, ?, ?, NULL, ?)",
                    (
                        slot_digest,
                        slot_kid,
                        slot.auth_profile_id,
                        slot_version,
                        self._sealed_json(sealed_slot_key),
                    ),
                )
            else:
                slot_digest = str(slot_row["slot_digest"])
                slot_version = int(slot_row["version"])
            try:
                await db.execute(
                    "INSERT INTO oauth_transactions "
                    "(transaction_id, operation_id, state_kid, state_digest, "
                    "slot_digest, slot_version, status, expires_at, redirect_route, "
                    "encrypted_payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        transaction_id,
                        operation_id,
                        state_kid,
                        state_digest,
                        slot_digest,
                        slot_version,
                        OAuthTransactionStatus.PENDING.value,
                        expires_at,
                        payload.redirect_route,
                        self._sealed_json(sealed),
                        now,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise OAuthTransactionConflictError() from exc
            await db.commit()
        return OAuthTransactionRecord(
            transaction_id=transaction_id,
            operation_id=operation_id,
            slot_digest=slot_digest,
            slot_version=slot_version,
            status=OAuthTransactionStatus.PENDING,
            expires_at=expires_at,
            payload=payload,
        )

    async def _find_transaction_row(
        self,
        db: aiosqlite.Connection,
        raw_state: str,
    ) -> aiosqlite.Row | None:
        match: aiosqlite.Row | None = None
        for kid, digest in self._keyring.state_digest_candidates(raw_state):
            async with db.execute(
                "SELECT transaction_id, operation_id, slot_digest, slot_version, status, "
                "expires_at, redirect_route, encrypted_payload FROM oauth_transactions "
                "WHERE state_kid = ? AND state_digest = ?",
                (kid, digest),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                if match is not None:
                    raise OAuthStoreCorruptionError()
                match = row
        return match

    async def find_transaction(
        self,
        raw_state: str,
        *,
        now: float,
    ) -> OAuthTransactionRecord | None:
        """Lookup one still-pending transaction and lazily expire exact boundary。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._find_transaction_row(db, raw_state)
            if row is None or row["status"] != OAuthTransactionStatus.PENDING.value:
                await db.rollback()
                return None
            if now >= float(row["expires_at"]):
                await db.execute(
                    "UPDATE oauth_transactions SET status = ? "
                    "WHERE transaction_id = ? AND status = ?",
                    (
                        OAuthTransactionStatus.EXPIRED.value,
                        row["transaction_id"],
                        OAuthTransactionStatus.PENDING.value,
                    ),
                )
                await db.commit()
                return None
            await db.rollback()
            return self._decrypt_transaction_row(row)

    async def find_pending_for_slot(
        self,
        slot: OAuthAuthorizationSlotKey,
        *,
        now: float,
    ) -> OAuthTransactionRecord | None:
        """Find the sole pending transaction for an exact slot without raw state。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            slot_row = await self._find_slot_row(db, slot)
            if slot_row is None:
                await db.rollback()
                return None
            async with db.execute(
                "SELECT transaction_id, operation_id, slot_digest, slot_version, status, "
                "expires_at, redirect_route, encrypted_payload FROM oauth_transactions "
                "WHERE slot_digest = ? AND status = ? ORDER BY created_at DESC",
                (
                    slot_row["slot_digest"],
                    OAuthTransactionStatus.PENDING.value,
                ),
            ) as cursor:
                rows = await cursor.fetchall()
            if len(rows) > 1:
                await db.rollback()
                raise OAuthStoreCorruptionError()
            if not rows:
                await db.rollback()
                return None
            row = rows[0]
            if now >= float(row["expires_at"]):
                await db.execute(
                    "UPDATE oauth_transactions SET status = ? WHERE transaction_id = ? "
                    "AND status = ?",
                    (
                        OAuthTransactionStatus.EXPIRED.value,
                        row["transaction_id"],
                        OAuthTransactionStatus.PENDING.value,
                    ),
                )
                await db.commit()
                return None
            await db.rollback()
            return self._decrypt_transaction_row(row)

    async def consume_transaction(
        self,
        raw_state: str,
        *,
        now: float,
        redirect_route: str,
        expected_slot: OAuthAuthorizationSlotKey | None = None,
    ) -> OAuthTransactionRecord | None:
        """CAS pending→consuming only for the exact route and unexpired state。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._find_transaction_row(db, raw_state)
            if row is None or row["status"] != OAuthTransactionStatus.PENDING.value:
                await db.rollback()
                return None
            if expected_slot is not None:
                expected_digests = {
                    digest
                    for _kid, digest in self._keyring.slot_digest_candidates(
                        expected_slot.canonical_bytes()
                    )
                }
                if row["slot_digest"] not in expected_digests:
                    await db.rollback()
                    return None
            if now >= float(row["expires_at"]):
                await db.execute(
                    "UPDATE oauth_transactions SET status = ? "
                    "WHERE transaction_id = ? AND status = ?",
                    (
                        OAuthTransactionStatus.EXPIRED.value,
                        row["transaction_id"],
                        OAuthTransactionStatus.PENDING.value,
                    ),
                )
                await db.commit()
                return None
            if row["redirect_route"] != redirect_route:
                await db.rollback()
                raise OAuthCallbackRouteError()
            cursor = await db.execute(
                "UPDATE oauth_transactions SET status = ? WHERE transaction_id = ? AND status = ?",
                (
                    OAuthTransactionStatus.CONSUMING.value,
                    row["transaction_id"],
                    OAuthTransactionStatus.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            await db.commit()
            return self._decrypt_transaction_row(row).model_copy(
                update={"status": OAuthTransactionStatus.CONSUMING}
            )

    async def cancel_transaction(self, transaction_id: str) -> bool:
        """Cancel only one still-pending immutable authorization transaction。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE oauth_transactions SET status = ? WHERE transaction_id = ? AND status = ?",
                (
                    OAuthTransactionStatus.CANCELLED.value,
                    transaction_id,
                    OAuthTransactionStatus.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
            return True

    async def commit_authorization(
        self,
        *,
        transaction_id: str,
        expected_slot_version: int,
        binding: OAuthGrantBinding,
        tokens: OAuthTokenSet,
        now: float,
    ) -> bool:
        """Atomically commit grant, move pointer, and terminalize transaction。"""
        grant_id = secrets.token_urlsafe(16)
        sealed = self._keyring.encrypt(
            self._grant_bytes(binding, tokens),
            aad=f"grant:{grant_id}".encode(),
        )
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT slot_digest, slot_version, status FROM oauth_transactions "
                "WHERE transaction_id = ?",
                (transaction_id,),
            ) as cursor:
                transaction = await cursor.fetchone()
            if (
                transaction is None
                or transaction["status"] != OAuthTransactionStatus.CONSUMING.value
                or int(transaction["slot_version"]) != expected_slot_version
            ):
                await db.rollback()
                return False
            slot_digest = str(transaction["slot_digest"])
            async with db.execute(
                "SELECT profile_id, version, active_grant_id FROM oauth_slots "
                "WHERE slot_digest = ?",
                (slot_digest,),
            ) as cursor:
                slot = await cursor.fetchone()
            if slot is None or int(slot["version"]) != expected_slot_version:
                await db.rollback()
                return False
            async with db.execute(
                "SELECT status FROM oauth_profile_fences WHERE profile_id = ?",
                (slot["profile_id"],),
            ) as cursor:
                profile_fence = await cursor.fetchone()
            if profile_fence is not None and profile_fence["status"] == "updating":
                await db.rollback()
                return False
            old_grant = slot["active_grant_id"]
            if old_grant is not None:
                await db.execute(
                    "UPDATE oauth_grants SET status = ?, updated_at = ? "
                    "WHERE grant_id = ? AND status != ?",
                    (
                        OAuthGrantStatus.TOMBSTONED.value,
                        now,
                        old_grant,
                        OAuthGrantStatus.TOMBSTONED.value,
                    ),
                )
            await db.execute(
                "INSERT INTO oauth_grants "
                "(grant_id, slot_digest, grant_version, status, encrypted_payload, "
                "access_expires_at, has_refresh, lease_owner, lease_fence, lease_expires_at, "
                "created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                (
                    grant_id,
                    slot_digest,
                    OAuthGrantStatus.ACTIVE.value,
                    self._sealed_json(sealed),
                    tokens.expires_at,
                    int(tokens.refresh_token is not None),
                    now,
                    now,
                ),
            )
            slot_cursor = await db.execute(
                "UPDATE oauth_slots SET active_grant_id = ?, version = version + 1 "
                "WHERE slot_digest = ? AND version = ?",
                (grant_id, slot_digest, expected_slot_version),
            )
            transaction_cursor = await db.execute(
                "UPDATE oauth_transactions SET status = ? WHERE transaction_id = ? AND status = ?",
                (
                    OAuthTransactionStatus.CONSUMED_SUCCESS.value,
                    transaction_id,
                    OAuthTransactionStatus.CONSUMING.value,
                ),
            )
            if slot_cursor.rowcount != 1 or transaction_cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
            return True

    async def mark_transaction_error(self, transaction_id: str) -> None:
        """Terminalize a consumed transaction without persisting upstream detail。"""
        async with self._connection() as db:
            await db.execute(
                "UPDATE oauth_transactions SET status = ? WHERE transaction_id = ? AND status = ?",
                (
                    OAuthTransactionStatus.CONSUMED_ERROR.value,
                    transaction_id,
                    OAuthTransactionStatus.CONSUMING.value,
                ),
            )
            await db.commit()

    async def has_usable_grant(self, slot: OAuthAuthorizationSlotKey, *, now: float) -> bool:
        """組工具清單用的只讀查詢（ADR-0281 T1）：不開寫入交易、不改狀態、不解密。

        指標指向不存在的授權算損毀，照 ADR-0206 明確丟出。
        """
        async with self._connection() as db:
            slot_row = await self._find_slot_row(db, slot)
            if slot_row is None or slot_row["active_grant_id"] is None:
                return False
            async with db.execute(
                "SELECT status, access_expires_at, has_refresh FROM oauth_grants "
                "WHERE grant_id = ? AND slot_digest = ?",
                (slot_row["active_grant_id"], slot_row["slot_digest"]),
            ) as cursor:
                grant = await cursor.fetchone()
        if grant is None:
            raise OAuthStoreCorruptionError()
        usable_statuses = {OAuthGrantStatus.ACTIVE.value, OAuthGrantStatus.REFRESHING.value}
        if grant["status"] not in usable_statuses:
            return False
        return now < float(grant["access_expires_at"]) or bool(grant["has_refresh"])

    async def get_active_grant(
        self,
        slot: OAuthAuthorizationSlotKey,
        *,
        now: float,
    ) -> OAuthGrantRecord | None:
        """Return only the exact active, unexpired pointer target。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            slot_row = await self._find_slot_row(db, slot)
            if slot_row is None or slot_row["active_grant_id"] is None:
                await db.rollback()
                return None
            async with db.execute(
                "SELECT grant_id, slot_digest, grant_version, status, encrypted_payload, "
                "access_expires_at, has_refresh FROM oauth_grants WHERE grant_id = ?",
                (slot_row["active_grant_id"],),
            ) as cursor:
                grant = await cursor.fetchone()
            if grant is None or grant["slot_digest"] != slot_row["slot_digest"]:
                await db.rollback()
                return None
            if grant["status"] not in {
                OAuthGrantStatus.ACTIVE.value,
                OAuthGrantStatus.REFRESHING.value,
            }:
                await db.rollback()
                return None
            if now >= float(grant["access_expires_at"]) and not bool(grant["has_refresh"]):
                await db.execute(
                    "UPDATE oauth_grants SET status = ?, updated_at = ? "
                    "WHERE grant_id = ? AND grant_version = ?",
                    (
                        OAuthGrantStatus.EXPIRED.value,
                        now,
                        grant["grant_id"],
                        grant["grant_version"],
                    ),
                )
                await db.commit()
                return None
            await db.rollback()
            return self._decrypt_grant_row(grant)

    async def acquire_refresh_lease(
        self,
        grant_id: str,
        expected_version: int,
        owner: str,
        *,
        now: float,
        lease_seconds: float,
    ) -> OAuthRefreshLease | None:
        """Acquire one cross-worker refresh lease with a response fence。"""
        fence = secrets.token_urlsafe(16)
        expires_at = now + lease_seconds
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE oauth_grants SET status = ?, lease_owner = ?, lease_fence = ?, "
                "lease_expires_at = ?, updated_at = ? WHERE grant_id = ? AND grant_version = ? "
                "AND has_refresh = 1 AND (status = ? OR (status = ? AND lease_expires_at <= ?))",
                (
                    OAuthGrantStatus.REFRESHING.value,
                    owner,
                    fence,
                    expires_at,
                    now,
                    grant_id,
                    expected_version,
                    OAuthGrantStatus.ACTIVE.value,
                    OAuthGrantStatus.REFRESHING.value,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            await db.commit()
        return OAuthRefreshLease(
            grant_id=grant_id,
            expected_grant_version=expected_version,
            owner=owner,
            fence=fence,
            expires_at=expires_at,
        )

    async def commit_refresh(
        self,
        lease: OAuthRefreshLease,
        tokens: OAuthTokenSet,
        *,
        binding: OAuthGrantBinding,
        now: float,
    ) -> bool:
        """Rotate tokens only if lease owner/fence/version still match。"""
        if tokens.granted_scopes != binding.granted_scopes:
            return False
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT slot_digest, encrypted_payload FROM oauth_grants WHERE grant_id = ? "
                "AND grant_version = ? AND status = ? AND lease_owner = ? "
                "AND lease_fence = ? AND lease_expires_at > ?",
                (
                    lease.grant_id,
                    lease.expected_grant_version,
                    OAuthGrantStatus.REFRESHING.value,
                    lease.owner,
                    lease.fence,
                    now,
                ),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                return False
            old_sealed = OAuthSealedValue.model_validate_json(row["encrypted_payload"])
            old_binding, _old_tokens = self._grant_from_bytes(
                self._keyring.decrypt(
                    old_sealed,
                    aad=f"grant:{lease.grant_id}".encode(),
                )
            )
            if old_binding.granted_scopes != binding.granted_scopes:
                replacement_id = secrets.token_urlsafe(16)
                replacement = self._keyring.encrypt(
                    self._grant_bytes(binding, tokens),
                    aad=f"grant:{replacement_id}".encode(),
                )
                await db.execute(
                    "INSERT INTO oauth_grants "
                    "(grant_id, slot_digest, grant_version, status, encrypted_payload, "
                    "access_expires_at, has_refresh, lease_owner, lease_fence, "
                    "lease_expires_at, created_at, updated_at) "
                    "VALUES (?, ?, 1, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                    (
                        replacement_id,
                        row["slot_digest"],
                        OAuthGrantStatus.ACTIVE.value,
                        self._sealed_json(replacement),
                        tokens.expires_at,
                        int(tokens.refresh_token is not None),
                        now,
                        now,
                    ),
                )
                pointer = await db.execute(
                    "UPDATE oauth_slots SET active_grant_id = ?, version = version + 1 "
                    "WHERE slot_digest = ? AND active_grant_id = ?",
                    (replacement_id, row["slot_digest"], lease.grant_id),
                )
                if pointer.rowcount != 1:
                    await db.rollback()
                    return False
                tombstone = await db.execute(
                    "UPDATE oauth_grants SET status = ?, lease_owner = NULL, "
                    "lease_fence = NULL, lease_expires_at = NULL, updated_at = ? "
                    "WHERE grant_id = ? AND grant_version = ? AND status = ? "
                    "AND lease_owner = ? AND lease_fence = ?",
                    (
                        OAuthGrantStatus.TOMBSTONED.value,
                        now,
                        lease.grant_id,
                        lease.expected_grant_version,
                        OAuthGrantStatus.REFRESHING.value,
                        lease.owner,
                        lease.fence,
                    ),
                )
                if tombstone.rowcount != 1:
                    await db.rollback()
                    return False
                await db.commit()
                return True
            sealed = self._keyring.encrypt(
                self._grant_bytes(binding, tokens),
                aad=f"grant:{lease.grant_id}".encode(),
            )
            cursor = await db.execute(
                "UPDATE oauth_grants SET grant_version = grant_version + 1, status = ?, "
                "encrypted_payload = ?, access_expires_at = ?, has_refresh = ?, "
                "lease_owner = NULL, lease_fence = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE grant_id = ? AND grant_version = ? AND status = ? "
                "AND lease_owner = ? AND lease_fence = ?",
                (
                    OAuthGrantStatus.ACTIVE.value,
                    self._sealed_json(sealed),
                    tokens.expires_at,
                    int(tokens.refresh_token is not None),
                    now,
                    lease.grant_id,
                    lease.expected_grant_version,
                    OAuthGrantStatus.REFRESHING.value,
                    lease.owner,
                    lease.fence,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
            return True

    async def assert_bearer_usable(
        self,
        slot: OAuthAuthorizationSlotKey,
        grant_id: str,
        expected_version: int,
        *,
        now: float,
    ) -> bool:
        """Fence the exact active pointer/version immediately before bearer egress。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            slot_row = await self._find_slot_row(db, slot)
            if slot_row is None or slot_row["active_grant_id"] != grant_id:
                await db.rollback()
                return False
            async with db.execute(
                "SELECT 1 FROM oauth_grants WHERE grant_id = ? AND slot_digest = ? "
                "AND grant_version = ? AND status IN (?, ?) AND access_expires_at > ?",
                (
                    grant_id,
                    slot_row["slot_digest"],
                    expected_version,
                    OAuthGrantStatus.ACTIVE.value,
                    OAuthGrantStatus.REFRESHING.value,
                    now,
                ),
            ) as cursor:
                usable = await cursor.fetchone()
            await db.rollback()
            return usable is not None

    async def release_refresh_lease(
        self,
        lease: OAuthRefreshLease,
        *,
        now: float,
    ) -> bool:
        """Release only the still-live exact lease after a transient failure。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE oauth_grants SET status = CASE WHEN access_expires_at > ? "
                "THEN ? ELSE ? END, lease_owner = NULL, lease_fence = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE grant_id = ? "
                "AND grant_version = ? AND status = ? AND lease_owner = ? "
                "AND lease_fence = ? AND lease_expires_at > ?",
                (
                    now,
                    OAuthGrantStatus.ACTIVE.value,
                    OAuthGrantStatus.EXPIRED.value,
                    now,
                    lease.grant_id,
                    lease.expected_grant_version,
                    OAuthGrantStatus.REFRESHING.value,
                    lease.owner,
                    lease.fence,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
            return True

    async def tombstone_invalid_grant(
        self,
        lease: OAuthRefreshLease,
        *,
        now: float,
    ) -> bool:
        """Clear only the active pointer governed by a still-live invalid_grant lease。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT slot_digest FROM oauth_grants WHERE grant_id = ? "
                "AND grant_version = ? AND status = ? AND lease_owner = ? "
                "AND lease_fence = ? AND lease_expires_at > ?",
                (
                    lease.grant_id,
                    lease.expected_grant_version,
                    OAuthGrantStatus.REFRESHING.value,
                    lease.owner,
                    lease.fence,
                    now,
                ),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                return False
            slot_digest = str(row["slot_digest"])
            grant_cursor = await db.execute(
                "UPDATE oauth_grants SET status = ?, lease_owner = NULL, "
                "lease_fence = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE grant_id = ? AND grant_version = ? AND status = ? "
                "AND lease_owner = ? AND lease_fence = ?",
                (
                    OAuthGrantStatus.TOMBSTONED.value,
                    now,
                    lease.grant_id,
                    lease.expected_grant_version,
                    OAuthGrantStatus.REFRESHING.value,
                    lease.owner,
                    lease.fence,
                ),
            )
            slot_cursor = await db.execute(
                "UPDATE oauth_slots SET active_grant_id = NULL, version = version + 1 "
                "WHERE slot_digest = ? AND active_grant_id = ?",
                (slot_digest, lease.grant_id),
            )
            if grant_cursor.rowcount != 1 or slot_cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
            return True

    async def freeze_grant(
        self,
        grant_id: str,
        expected_version: int,
        *,
        now: float,
    ) -> bool:
        """Freeze only a still-pointed active/refreshing grant after security drift。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE oauth_grants SET status = ?, lease_owner = NULL, "
                "lease_fence = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE grant_id = ? AND grant_version = ? AND status IN (?, ?) "
                "AND EXISTS (SELECT 1 FROM oauth_slots WHERE "
                "oauth_slots.slot_digest = oauth_grants.slot_digest "
                "AND oauth_slots.active_grant_id = oauth_grants.grant_id)",
                (
                    OAuthGrantStatus.FROZEN.value,
                    now,
                    grant_id,
                    expected_version,
                    OAuthGrantStatus.ACTIVE.value,
                    OAuthGrantStatus.REFRESHING.value,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
            return True

    async def begin_profile_mutation(
        self,
        profile_id: str,
        *,
        now: float,
    ) -> int:
        """Persist updating intent, then freeze/cancel all profile-bound state atomically。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT epoch FROM oauth_profile_fences WHERE profile_id = ?",
                (profile_id,),
            ) as cursor:
                row = await cursor.fetchone()
            epoch = int(row["epoch"]) + 1 if row is not None else 1
            await db.execute(
                "INSERT INTO oauth_profile_fences "
                "(profile_id, epoch, status, applied_fingerprint) VALUES (?, ?, ?, NULL) "
                "ON CONFLICT(profile_id) DO UPDATE SET epoch = excluded.epoch, "
                "status = excluded.status, applied_fingerprint = NULL",
                (profile_id, epoch, "updating"),
            )
            await db.execute(
                "UPDATE oauth_transactions SET status = ? WHERE status = ? "
                "AND slot_digest IN (SELECT slot_digest FROM oauth_slots "
                "WHERE profile_id = ?)",
                (
                    OAuthTransactionStatus.CANCELLED.value,
                    OAuthTransactionStatus.PENDING.value,
                    profile_id,
                ),
            )
            await db.execute(
                "UPDATE oauth_grants SET status = ?, lease_owner = NULL, "
                "lease_fence = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE status IN (?, ?) AND grant_id IN "
                "(SELECT active_grant_id FROM oauth_slots WHERE profile_id = ? "
                "AND active_grant_id IS NOT NULL)",
                (
                    OAuthGrantStatus.FROZEN.value,
                    now,
                    OAuthGrantStatus.ACTIVE.value,
                    OAuthGrantStatus.REFRESHING.value,
                    profile_id,
                ),
            )
            await db.commit()
            return epoch

    async def list_updating_profile_fences(self) -> list[tuple[str, int]]:
        """Return deterministic orphaned mutation intents for startup recovery。"""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT profile_id, epoch FROM oauth_profile_fences "
                "WHERE status = ? ORDER BY profile_id",
                ("updating",),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return [(str(row["profile_id"]), int(row["epoch"])) for row in rows]

    async def complete_profile_mutation(
        self,
        profile_id: str,
        expected_epoch: int,
        *,
        applied_fingerprint: str | None,
        now: float,
    ) -> bool:
        """Complete only the exact persisted mutation epoch after JSON commit。"""
        del now
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE oauth_profile_fences SET status = ?, applied_fingerprint = ? "
                "WHERE profile_id = ? AND epoch = ? AND status = ?",
                (
                    "stable",
                    applied_fingerprint,
                    profile_id,
                    expected_epoch,
                    "updating",
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
            return True

    async def _find_registration_row(
        self,
        db: aiosqlite.Connection,
        key: OAuthRegistrationKey,
    ) -> aiosqlite.Row | None:
        for _kid, digest in self._keyring.registration_digest_candidates(key.canonical_bytes()):
            async with db.execute(
                "SELECT registration_key, version, status, lease_owner, lease_fence, "
                "lease_expires_at, encrypted_payload FROM oauth_registrations "
                "WHERE registration_key = ?",
                (digest,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                return row
        return None

    def _decrypt_registration_row(self, row: aiosqlite.Row) -> OAuthClientBinding:
        key_digest = str(row["registration_key"])
        try:
            sealed = OAuthSealedValue.model_validate_json(row["encrypted_payload"])
            return self._registration_from_bytes(
                self._keyring.decrypt(
                    sealed,
                    aad=f"registration:{key_digest}".encode(),
                )
            )
        except Exception as exc:
            raise OAuthStoreCorruptionError() from exc

    async def get_registration(
        self,
        key: OAuthRegistrationKey,
        *,
        now: float,
    ) -> OAuthClientBinding | None:
        """Return only one exact active, non-expired client registration。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._find_registration_row(db, key)
            if row is None or row["status"] != "active":
                await db.rollback()
                return None
            binding = self._decrypt_registration_row(row)
            if (
                binding.client_secret_expires_at is not None
                and now >= binding.client_secret_expires_at
            ):
                await db.execute(
                    "UPDATE oauth_registrations SET status = ?, updated_at = ? "
                    "WHERE registration_key = ? AND version = ? AND status = ?",
                    (
                        "frozen",
                        now,
                        row["registration_key"],
                        row["version"],
                        "active",
                    ),
                )
                await db.commit()
                return None
            await db.rollback()
            return binding

    async def acquire_registration_lease(
        self,
        key: OAuthRegistrationKey,
        owner: str,
        *,
        now: float,
        lease_seconds: float,
    ) -> OAuthRegistrationLease | None:
        """Acquire one DCR network-attempt lease across workers。"""
        fence = secrets.token_urlsafe(16)
        expires_at = now + lease_seconds
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._find_registration_row(db, key)
            if row is None:
                key_kid, key_digest = self._keyring.registration_digest(key.canonical_bytes())
                expected_version = 0
                sealed_registration_key = self._keyring.encrypt(
                    self._registration_key_bytes(key),
                    aad=f"registration-key:{key_digest}".encode(),
                )
                await db.execute(
                    "INSERT INTO oauth_registrations "
                    "(registration_key, version, status, lease_owner, lease_fence, "
                    "lease_expires_at, encrypted_payload, updated_at, key_kid, "
                    "encrypted_registration_key) "
                    "VALUES (?, 0, ?, ?, ?, ?, NULL, ?, ?, ?)",
                    (
                        key_digest,
                        "registering",
                        owner,
                        fence,
                        expires_at,
                        now,
                        key_kid,
                        self._sealed_json(sealed_registration_key),
                    ),
                )
            elif row["status"] == "registering" and float(row["lease_expires_at"]) <= now:
                key_digest = str(row["registration_key"])
                expected_version = int(row["version"])
                cursor = await db.execute(
                    "UPDATE oauth_registrations SET lease_owner = ?, lease_fence = ?, "
                    "lease_expires_at = ?, updated_at = ? WHERE registration_key = ? "
                    "AND version = ? AND status = ? AND lease_expires_at <= ?",
                    (
                        owner,
                        fence,
                        expires_at,
                        now,
                        key_digest,
                        expected_version,
                        "registering",
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return None
            elif row["status"] == "frozen":
                key_digest = str(row["registration_key"])
                expected_version = int(row["version"])
                cursor = await db.execute(
                    "UPDATE oauth_registrations SET status = ?, lease_owner = ?, "
                    "lease_fence = ?, lease_expires_at = ?, encrypted_payload = NULL, "
                    "updated_at = ? WHERE registration_key = ? AND version = ? "
                    "AND status = ?",
                    (
                        "registering",
                        owner,
                        fence,
                        expires_at,
                        now,
                        key_digest,
                        expected_version,
                        "frozen",
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return None
            else:
                await db.rollback()
                return None
            await db.commit()
        return OAuthRegistrationLease(
            key_digest=key_digest,
            expected_version=expected_version,
            owner=owner,
            fence=fence,
            expires_at=expires_at,
        )

    async def commit_registration(
        self,
        lease: OAuthRegistrationLease,
        binding: OAuthClientBinding,
        *,
        now: float,
    ) -> bool:
        """Commit only the live DCR attempt fence; stale remote responses are dropped。"""
        if binding.registration_version != lease.expected_version + 1:
            return False
        sealed = self._keyring.encrypt(
            self._registration_bytes(binding),
            aad=f"registration:{lease.key_digest}".encode(),
        )
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE oauth_registrations SET version = version + 1, status = ?, "
                "lease_owner = NULL, lease_fence = NULL, lease_expires_at = NULL, "
                "encrypted_payload = ?, updated_at = ? WHERE registration_key = ? "
                "AND version = ? AND status = ? AND lease_owner = ? AND lease_fence = ? "
                "AND lease_expires_at > ?",
                (
                    "active",
                    self._sealed_json(sealed),
                    now,
                    lease.key_digest,
                    lease.expected_version,
                    "registering",
                    lease.owner,
                    lease.fence,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
            return True

    async def _reencrypt_column_batch(
        self,
        db: aiosqlite.Connection,
        *,
        table: str,
        primary_key: str,
        encrypted_column: str,
        aad_prefix: str,
        retiring_kid: str,
        limit: int,
    ) -> int:
        if limit <= 0:
            return 0
        query = (
            f"SELECT {primary_key}, {encrypted_column} FROM {table} "  # noqa: S608
            f"WHERE {encrypted_column} IS NOT NULL "
            f"AND json_extract({encrypted_column}, '$.kid') = ? LIMIT ?"
        )
        async with db.execute(query, (retiring_kid, limit)) as cursor:
            rows = await cursor.fetchall()
        rewritten = 0
        for row in rows:
            record_id = str(row[primary_key])
            old_json = str(row[encrypted_column])
            sealed = OAuthSealedValue.model_validate_json(old_json)
            plaintext = self._keyring.decrypt(
                sealed,
                aad=f"{aad_prefix}:{record_id}".encode(),
            )
            replacement = self._sealed_json(
                self._keyring.encrypt(
                    plaintext,
                    aad=f"{aad_prefix}:{record_id}".encode(),
                )
            )
            update = (
                f"UPDATE {table} SET {encrypted_column} = ? "  # noqa: S608
                f"WHERE {primary_key} = ? AND {encrypted_column} = ?"
            )
            result = await db.execute(update, (replacement, record_id, old_json))
            if result.rowcount != 1:
                raise OAuthStoreCorruptionError()
            rewritten += 1
        return rewritten

    async def _rekey_slot_batch(
        self,
        db: aiosqlite.Connection,
        *,
        retiring_kid: str,
        limit: int,
    ) -> int:
        if limit <= 0:
            return 0
        async with db.execute(
            "SELECT slot_digest, slot_kid, profile_id, version, active_grant_id, "
            "encrypted_slot_key FROM oauth_slots WHERE slot_kid = ? LIMIT ?",
            (retiring_kid, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        rewritten = 0
        for row in rows:
            old_digest = str(row["slot_digest"])
            if row["encrypted_slot_key"] is None:
                raise OAuthStoreCorruptionError()
            sealed = OAuthSealedValue.model_validate_json(row["encrypted_slot_key"])
            slot = self._slot_key_from_bytes(
                self._keyring.decrypt(
                    sealed,
                    aad=f"slot:{old_digest}".encode(),
                )
            )
            new_kid, new_digest = self._keyring.slot_digest(slot.canonical_bytes())
            if new_kid == retiring_kid or new_digest == old_digest:
                raise OAuthStoreCorruptionError()
            encrypted_slot_key = self._sealed_json(
                self._keyring.encrypt(
                    self._slot_key_bytes(slot),
                    aad=f"slot:{new_digest}".encode(),
                )
            )
            try:
                await db.execute(
                    "INSERT INTO oauth_slots "
                    "(slot_digest, slot_kid, profile_id, version, active_grant_id, "
                    "encrypted_slot_key) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        new_digest,
                        new_kid,
                        row["profile_id"],
                        row["version"],
                        row["active_grant_id"],
                        encrypted_slot_key,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                raise OAuthStoreCorruptionError() from exc
            await db.execute(
                "UPDATE oauth_transactions SET slot_digest = ? WHERE slot_digest = ?",
                (new_digest, old_digest),
            )
            await db.execute(
                "UPDATE oauth_grants SET slot_digest = ? WHERE slot_digest = ?",
                (new_digest, old_digest),
            )
            deleted = await db.execute(
                "DELETE FROM oauth_slots WHERE slot_digest = ? AND slot_kid = ?",
                (old_digest, retiring_kid),
            )
            if deleted.rowcount != 1:
                raise OAuthStoreCorruptionError()
            rewritten += 1
        return rewritten

    async def _rekey_registration_batch(
        self,
        db: aiosqlite.Connection,
        *,
        retiring_kid: str,
        limit: int,
    ) -> int:
        if limit <= 0:
            return 0
        async with db.execute(
            "SELECT registration_key, version, status, encrypted_payload, "
            "encrypted_registration_key FROM oauth_registrations "
            "WHERE key_kid = ? LIMIT ?",
            (retiring_kid, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        rewritten = 0
        for row in rows:
            old_digest = str(row["registration_key"])
            if row["encrypted_registration_key"] is None:
                raise OAuthStoreCorruptionError()
            key_sealed = OAuthSealedValue.model_validate_json(row["encrypted_registration_key"])
            key = self._registration_key_from_bytes(
                self._keyring.decrypt(
                    key_sealed,
                    aad=f"registration-key:{old_digest}".encode(),
                )
            )
            new_kid, new_digest = self._keyring.registration_digest(key.canonical_bytes())
            if new_kid == retiring_kid or new_digest == old_digest:
                raise OAuthStoreCorruptionError()
            new_key_payload = self._sealed_json(
                self._keyring.encrypt(
                    self._registration_key_bytes(key),
                    aad=f"registration-key:{new_digest}".encode(),
                )
            )
            new_binding_payload: str | None = None
            if row["encrypted_payload"] is not None:
                binding_sealed = OAuthSealedValue.model_validate_json(row["encrypted_payload"])
                binding_plaintext = self._keyring.decrypt(
                    binding_sealed,
                    aad=f"registration:{old_digest}".encode(),
                )
                new_binding_payload = self._sealed_json(
                    self._keyring.encrypt(
                        binding_plaintext,
                        aad=f"registration:{new_digest}".encode(),
                    )
                )
            try:
                result = await db.execute(
                    "UPDATE oauth_registrations SET registration_key = ?, key_kid = ?, "
                    "encrypted_registration_key = ?, encrypted_payload = ? "
                    "WHERE registration_key = ? AND key_kid = ? AND version = ? "
                    "AND status = ?",
                    (
                        new_digest,
                        new_kid,
                        new_key_payload,
                        new_binding_payload,
                        old_digest,
                        retiring_kid,
                        row["version"],
                        row["status"],
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                raise OAuthStoreCorruptionError() from exc
            if result.rowcount != 1:
                raise OAuthStoreCorruptionError()
            rewritten += 1
        return rewritten

    async def reencrypt_and_rekey_batch(
        self,
        *,
        retiring_kid: str,
        limit: int,
    ) -> dict[str, int]:
        """CAS-reencrypt payloads and rekey durable locators in one short batch."""
        if limit < 1:
            raise ValueError()
        payload_specs = (
            ("oauth_transactions", "transaction_id", "encrypted_payload", "transaction"),
            ("oauth_grants", "grant_id", "encrypted_payload", "grant"),
            ("oauth_slots", "slot_digest", "encrypted_slot_key", "slot"),
            (
                "oauth_registrations",
                "registration_key",
                "encrypted_payload",
                "registration",
            ),
            (
                "oauth_registrations",
                "registration_key",
                "encrypted_registration_key",
                "registration-key",
            ),
        )
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            remaining = limit
            payloads_reencrypted = 0
            for table, primary_key, column, aad_prefix in payload_specs:
                count = await self._reencrypt_column_batch(
                    db,
                    table=table,
                    primary_key=primary_key,
                    encrypted_column=column,
                    aad_prefix=aad_prefix,
                    retiring_kid=retiring_kid,
                    limit=remaining,
                )
                payloads_reencrypted += count
                remaining -= count
                if remaining == 0:
                    break
            slots_rekeyed = await self._rekey_slot_batch(
                db,
                retiring_kid=retiring_kid,
                limit=remaining,
            )
            remaining -= slots_rekeyed
            registrations_rekeyed = await self._rekey_registration_batch(
                db,
                retiring_kid=retiring_kid,
                limit=remaining,
            )
            await db.commit()
        return {
            "payloads_reencrypted": payloads_reencrypted,
            "slots_rekeyed": slots_rekeyed,
            "registrations_rekeyed": registrations_rekeyed,
        }

    async def count_key_references(self, kid: str) -> dict[str, int]:
        """Count every durable reference that still requires one key id."""
        index_queries = {
            "state_indexes": ("SELECT COUNT(*) FROM oauth_transactions WHERE state_kid = ?"),
            "slot_indexes": "SELECT COUNT(*) FROM oauth_slots WHERE slot_kid = ?",
            "registration_indexes": ("SELECT COUNT(*) FROM oauth_registrations WHERE key_kid = ?"),
        }
        envelope_queries = {
            "transaction_payloads": (
                "oauth_transactions",
                "encrypted_payload",
            ),
            "grant_payloads": ("oauth_grants", "encrypted_payload"),
            "slot_payloads": ("oauth_slots", "encrypted_slot_key"),
            "registration_payloads": (
                "oauth_registrations",
                "encrypted_payload",
            ),
            "registration_key_payloads": (
                "oauth_registrations",
                "encrypted_registration_key",
            ),
        }
        counts: dict[str, int] = {}
        async with self._connection() as db:
            for name, query in index_queries.items():
                async with db.execute(query, (kid,)) as cursor:
                    row = await cursor.fetchone()
                counts[name] = int(row[0])
            for name, (table, column) in envelope_queries.items():
                query = (
                    f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL "  # noqa: S608
                    f"AND json_extract({column}, '$.kid') = ?"
                )
                async with db.execute(query, (kid,)) as cursor:
                    row = await cursor.fetchone()
                counts[name] = int(row[0])
        return counts

    @staticmethod
    def _rotation_from_row(row: aiosqlite.Row) -> OAuthKeyRotationRecord:
        return OAuthKeyRotationRecord(
            phase=row["phase"],
            current_kid=row["current_kid"],
            retiring_kid=row["retiring_kid"],
            epoch=row["epoch"],
            started_at=row["started_at"],
            state_drain_after=row["state_drain_after"],
        )

    async def get_key_rotation(self) -> OAuthKeyRotationRecord:
        """Load the singleton persisted rotation state."""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT phase, current_kid, retiring_kid, epoch, started_at, "
                "state_drain_after FROM oauth_key_rotation WHERE singleton = 1"
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            raise OAuthStoreCorruptionError()
        return self._rotation_from_row(row)

    async def begin_key_rotation(
        self,
        *,
        new_kid: str,
        worker_id: str,
        now: float,
        state_ttl_seconds: float,
        worker_liveness_seconds: float,
    ) -> OAuthKeyRotationRecord:
        """CAS stable to prepare and snapshot workers with a current lease."""
        if worker_liveness_seconds <= 0:
            raise ValueError()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT phase, current_kid, retiring_kid, epoch, started_at, "
                "state_drain_after FROM oauth_key_rotation WHERE singleton = 1"
            ) as cursor:
                row = await cursor.fetchone()
            if (
                row is None
                or row["phase"] != OAuthKeyRotationPhase.STABLE.value
                or row["retiring_kid"] is not None
                or row["current_kid"] != self._keyring.current.kid
                or new_kid == row["current_kid"]
            ):
                await db.rollback()
                raise OAuthStoreCorruptionError()
            epoch = int(row["epoch"]) + 1
            retiring_kid = str(row["current_kid"])
            result = await db.execute(
                "UPDATE oauth_key_rotation SET phase = ?, current_kid = ?, "
                "retiring_kid = ?, epoch = ?, started_at = ?, state_drain_after = ? "
                "WHERE singleton = 1 AND phase = ? AND epoch = ?",
                (
                    OAuthKeyRotationPhase.PREPARE.value,
                    new_kid,
                    retiring_kid,
                    epoch,
                    now,
                    now + state_ttl_seconds,
                    OAuthKeyRotationPhase.STABLE.value,
                    row["epoch"],
                ),
            )
            if result.rowcount != 1:
                await db.rollback()
                raise OAuthStoreCorruptionError()
            await db.execute(
                "DELETE FROM oauth_worker_epochs WHERE heartbeat_at < ?",
                (now - worker_liveness_seconds,),
            )
            await db.execute(
                "INSERT INTO oauth_worker_epochs (worker_id, key_epoch, heartbeat_at) "
                "VALUES (?, ?, ?) ON CONFLICT(worker_id) DO UPDATE SET "
                "heartbeat_at = excluded.heartbeat_at",
                (worker_id, row["epoch"], now),
            )
            await db.execute(
                "INSERT OR IGNORE INTO oauth_rotation_workers (epoch, worker_id, loaded) "
                "SELECT ?, worker_id, 0 FROM oauth_worker_epochs",
                (epoch,),
            )
            await db.commit()
        return OAuthKeyRotationRecord(
            phase=OAuthKeyRotationPhase.PREPARE,
            current_kid=new_kid,
            retiring_kid=retiring_kid,
            epoch=epoch,
            started_at=now,
            state_drain_after=now + state_ttl_seconds,
        )

    async def set_key_rotation_phase(
        self,
        *,
        epoch: int,
        expected: OAuthKeyRotationPhase,
        target: OAuthKeyRotationPhase,
    ) -> bool:
        """Advance one exact persisted rotation phase by CAS."""
        async with self._connection() as db:
            result = await db.execute(
                "UPDATE oauth_key_rotation SET phase = ? WHERE singleton = 1 "
                "AND epoch = ? AND phase = ?",
                (target.value, epoch, expected.value),
            )
            await db.commit()
        return result.rowcount == 1

    async def rollback_key_rotation_prepare(self, *, epoch: int) -> bool:
        """Rollback only the pre-persistence prepare phase."""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT retiring_kid FROM oauth_key_rotation WHERE singleton = 1 "
                "AND epoch = ? AND phase = ?",
                (epoch, OAuthKeyRotationPhase.PREPARE.value),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or row["retiring_kid"] is None:
                await db.rollback()
                return False
            await db.execute(
                "UPDATE oauth_key_rotation SET phase = ?, current_kid = ?, "
                "retiring_kid = NULL, epoch = epoch - 1, started_at = NULL, "
                "state_drain_after = NULL WHERE singleton = 1 AND epoch = ? "
                "AND phase = ?",
                (
                    OAuthKeyRotationPhase.STABLE.value,
                    row["retiring_kid"],
                    epoch,
                    OAuthKeyRotationPhase.PREPARE.value,
                ),
            )
            await db.execute(
                "DELETE FROM oauth_rotation_workers WHERE epoch = ?",
                (epoch,),
            )
            await db.commit()
            return True

    async def report_worker_epoch(
        self,
        worker_id: str,
        *,
        epoch: int,
        now: float,
    ) -> None:
        """Heartbeat one worker and join any in-progress convergence set."""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "INSERT INTO oauth_worker_epochs (worker_id, key_epoch, heartbeat_at) "
                "VALUES (?, ?, ?) ON CONFLICT(worker_id) DO UPDATE SET "
                "key_epoch = excluded.key_epoch, heartbeat_at = excluded.heartbeat_at",
                (worker_id, epoch, now),
            )
            async with db.execute(
                "SELECT phase, epoch FROM oauth_key_rotation WHERE singleton = 1"
            ) as cursor:
                rotation = await cursor.fetchone()
            if rotation is not None and rotation["phase"] != OAuthKeyRotationPhase.STABLE.value:
                rotation_epoch = int(rotation["epoch"])
                await db.execute(
                    "INSERT INTO oauth_rotation_workers (epoch, worker_id, loaded) "
                    "VALUES (?, ?, ?) ON CONFLICT(epoch, worker_id) DO UPDATE SET "
                    "loaded = excluded.loaded",
                    (rotation_epoch, worker_id, int(epoch == rotation_epoch)),
                )
            await db.commit()

    async def key_rotation_workers_converged(self, *, epoch: int) -> bool:
        """Require every worker captured or joining during rotation to load the epoch."""
        async with (
            self._connection() as db,
            db.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN loaded = 1 THEN 1 ELSE 0 END) AS loaded "
                "FROM oauth_rotation_workers WHERE epoch = ?",
                (epoch,),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        total = int(row["total"]) if row is not None else 0
        loaded = int(row["loaded"] or 0) if row is not None else 0
        return total > 0 and loaded == total

    async def complete_key_rotation(self, *, epoch: int) -> bool:
        """CAS worker-converge to stable after the retiring key is removed."""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            result = await db.execute(
                "UPDATE oauth_key_rotation SET phase = ?, retiring_kid = NULL, "
                "started_at = NULL, state_drain_after = NULL WHERE singleton = 1 "
                "AND epoch = ? AND phase = ?",
                (
                    OAuthKeyRotationPhase.STABLE.value,
                    epoch,
                    OAuthKeyRotationPhase.WORKER_CONVERGE.value,
                ),
            )
            if result.rowcount == 1:
                await db.execute(
                    "DELETE FROM oauth_rotation_workers WHERE epoch = ?",
                    (epoch,),
                )
            await db.commit()
        return result.rowcount == 1

    async def get_slot_snapshot(
        self,
        slot: OAuthAuthorizationSlotKey,
        *,
        now: float,
    ) -> OAuthSlotSnapshot:
        """Derive one non-secret status projection from exact persisted slot state。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            slot_row = await self._find_slot_row(db, slot)
            if slot_row is None:
                await db.rollback()
                return OAuthSlotSnapshot()
            slot_digest = str(slot_row["slot_digest"])
            async with db.execute(
                "SELECT operation_id, status, expires_at FROM oauth_transactions "
                "WHERE slot_digest = ? AND status IN (?, ?) ORDER BY created_at DESC",
                (
                    slot_digest,
                    OAuthTransactionStatus.PENDING.value,
                    OAuthTransactionStatus.CONSUMING.value,
                ),
            ) as cursor:
                transactions = await cursor.fetchall()
            if len(transactions) > 1:
                await db.rollback()
                raise OAuthStoreCorruptionError()
            transaction = transactions[0] if transactions else None
            if (
                transaction is not None
                and transaction["status"] == OAuthTransactionStatus.PENDING.value
                and now >= float(transaction["expires_at"])
            ):
                await db.execute(
                    "UPDATE oauth_transactions SET status = ? WHERE slot_digest = ? "
                    "AND status = ? AND expires_at <= ?",
                    (
                        OAuthTransactionStatus.EXPIRED.value,
                        slot_digest,
                        OAuthTransactionStatus.PENDING.value,
                        now,
                    ),
                )
                transaction = None
            grant = None
            if slot_row["active_grant_id"] is not None:
                async with db.execute(
                    "SELECT status, access_expires_at, has_refresh FROM oauth_grants "
                    "WHERE grant_id = ? AND slot_digest = ?",
                    (slot_row["active_grant_id"], slot_digest),
                ) as cursor:
                    grant = await cursor.fetchone()
                if grant is None:
                    await db.rollback()
                    raise OAuthStoreCorruptionError()
                if (
                    grant["status"] == OAuthGrantStatus.ACTIVE.value
                    and now >= float(grant["access_expires_at"])
                    and not bool(grant["has_refresh"])
                ):
                    await db.execute(
                        "UPDATE oauth_grants SET status = ?, updated_at = ? "
                        "WHERE grant_id = ? AND status = ?",
                        (
                            OAuthGrantStatus.EXPIRED.value,
                            now,
                            slot_row["active_grant_id"],
                            OAuthGrantStatus.ACTIVE.value,
                        ),
                    )
                    grant = dict(grant)
                    grant["status"] = OAuthGrantStatus.EXPIRED.value
            await db.commit()
            return OAuthSlotSnapshot(
                transaction_status=(
                    OAuthTransactionStatus(transaction["status"])
                    if transaction is not None
                    else None
                ),
                operation_id=(transaction["operation_id"] if transaction is not None else None),
                transaction_expires_at=(
                    transaction["expires_at"] if transaction is not None else None
                ),
                grant_status=(OAuthGrantStatus(grant["status"]) if grant is not None else None),
                access_expires_at=(grant["access_expires_at"] if grant is not None else None),
                has_refresh=bool(grant["has_refresh"]) if grant is not None else False,
            )

    async def begin_disconnect(
        self,
        slot: OAuthAuthorizationSlotKey,
        *,
        now: float,
    ) -> OAuthGrantRecord | None:
        """Cancel pending auth and CAS the exact active pointer target to revoking。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            slot_row = await self._find_slot_row(db, slot)
            if slot_row is None:
                await db.rollback()
                return None
            slot_digest = str(slot_row["slot_digest"])
            await db.execute(
                "UPDATE oauth_transactions SET status = ? WHERE slot_digest = ? AND status = ?",
                (
                    OAuthTransactionStatus.CANCELLED.value,
                    slot_digest,
                    OAuthTransactionStatus.PENDING.value,
                ),
            )
            grant_id = slot_row["active_grant_id"]
            if grant_id is None:
                await db.commit()
                return None
            async with db.execute(
                "SELECT grant_id, slot_digest, grant_version, status, encrypted_payload, "
                "access_expires_at, has_refresh FROM oauth_grants WHERE grant_id = ? "
                "AND slot_digest = ?",
                (grant_id, slot_digest),
            ) as cursor:
                grant = await cursor.fetchone()
            if grant is None:
                await db.rollback()
                raise OAuthStoreCorruptionError()
            allowed = {
                OAuthGrantStatus.ACTIVE.value,
                OAuthGrantStatus.REFRESHING.value,
                OAuthGrantStatus.FROZEN.value,
                OAuthGrantStatus.EXPIRED.value,
            }
            if grant["status"] == OAuthGrantStatus.REVOKING.value:
                await db.commit()
                return self._decrypt_grant_row(grant)
            if grant["status"] not in allowed:
                await db.rollback()
                return None
            cursor = await db.execute(
                "UPDATE oauth_grants SET status = ?, lease_owner = NULL, "
                "lease_fence = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE grant_id = ? AND grant_version = ? AND status = ?",
                (
                    OAuthGrantStatus.REVOKING.value,
                    now,
                    grant_id,
                    grant["grant_version"],
                    grant["status"],
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            await db.commit()
            return self._decrypt_grant_row(grant).model_copy(
                update={"status": OAuthGrantStatus.REVOKING}
            )

    async def complete_disconnect(
        self,
        grant_id: str,
        *,
        expected_grant_version: int,
        now: float,
    ) -> bool:
        """Tombstone only the still-pointed revoking grant using exact CAS。"""
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT slot_digest FROM oauth_grants WHERE grant_id = ? "
                "AND grant_version = ? AND status = ?",
                (
                    grant_id,
                    expected_grant_version,
                    OAuthGrantStatus.REVOKING.value,
                ),
            ) as cursor:
                grant = await cursor.fetchone()
            if grant is None:
                await db.rollback()
                return False
            slot_digest = str(grant["slot_digest"])
            grant_cursor = await db.execute(
                "UPDATE oauth_grants SET status = ?, updated_at = ? WHERE grant_id = ? "
                "AND grant_version = ? AND status = ?",
                (
                    OAuthGrantStatus.TOMBSTONED.value,
                    now,
                    grant_id,
                    expected_grant_version,
                    OAuthGrantStatus.REVOKING.value,
                ),
            )
            slot_cursor = await db.execute(
                "UPDATE oauth_slots SET active_grant_id = NULL, version = version + 1 "
                "WHERE slot_digest = ? AND active_grant_id = ?",
                (slot_digest, grant_id),
            )
            if grant_cursor.rowcount != 1 or slot_cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
            return True
