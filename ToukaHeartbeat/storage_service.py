"""Heartbeat-owned configuration persistence with explicit commit outcomes."""

import json
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from pydantic import ValidationError

if __package__:
    from .errors import HeartbeatError
    from .models import HeartbeatConfig
else:
    from errors import HeartbeatError
    from models import HeartbeatConfig

logger = logging.getLogger("ToukaHeartbeat.Storage")


@dataclass(frozen=True)
class WriteReceipt:
    """Visible replace success and separately observed directory durability."""

    committed: bool
    durability: str


class StorageService:
    """Own the single-process lock, configuration validation and atomic writes."""

    def __init__(self, config_file: Path, env_token: str = "") -> None:
        self.config_file = config_file
        self._env_token = env_token.strip()
        self.transaction_lock = RLock()

    def _initial_config(self) -> dict[str, Any]:
        return {
            "api_base_url": "http://touka-proxy:8000",
            "heartbeat_token": secrets.token_hex(16),
            "schedules": [],
        }

    @staticmethod
    def _validate(config: Any) -> dict[str, Any]:
        try:
            HeartbeatConfig.model_validate(config)
        except ValidationError:
            raise HeartbeatError("config_invalid", phase="validate") from None
        return config

    @classmethod
    def _decode(cls, content: bytes) -> dict[str, Any]:
        try:
            parsed = json.loads(content, parse_constant=cls._reject_nonfinite_json)
        except (ValueError, UnicodeError):
            raise HeartbeatError("config_parse_failed", phase="decode") from None
        return cls._validate(parsed)

    @staticmethod
    def _reject_nonfinite_json(_constant: str) -> None:
        raise ValueError

    def read_snapshot(self) -> tuple[dict[str, Any], bytes]:
        """Return validated data and its exact original bytes under one lock."""
        with self.transaction_lock:
            try:
                raw = self.config_file.read_bytes()  # noqa: IO_PRISON_VIOLATION
            except FileNotFoundError:
                if not self._confirmed_missing_entry():
                    raise HeartbeatError("config_read_failed", phase="read") from None
                initial = self._initial_config()
                self.save_config(initial)
                try:
                    raw = self.config_file.read_bytes()  # noqa: IO_PRISON_VIOLATION
                except OSError:
                    raise HeartbeatError("config_read_failed", phase="read") from None
            except OSError:
                raise HeartbeatError("config_read_failed", phase="read") from None
            return self._decode(raw), raw

    def load_config(self) -> dict[str, Any]:
        """Read a validated snapshot or propagate a visible domain failure."""
        return self.read_snapshot()[0]

    def get_token(self) -> str:
        """Resolve env > valid saved token > first local token under the same lock."""
        with self.transaction_lock:
            if self._env_token:
                return self._env_token
            config = self.load_config()
            token = config.get("heartbeat_token", "").strip()
            if token:
                return token
            new_token = secrets.token_hex(16)
            config["heartbeat_token"] = new_token
            self.save_config(config)
            return new_token

    @property
    def shared_credential_ready(self) -> bool:
        """A nonempty local shared setting is available for outbound authentication."""
        return bool(self._env_token)

    def save_config(self, config: dict[str, Any]) -> WriteReceipt:
        """Validate and atomically commit while preserving failure phase evidence."""
        with self.transaction_lock:
            self._validate(config)
            try:
                content = json.dumps(config, indent=4, ensure_ascii=False, allow_nan=False).encode(
                    "utf-8"
                )
            except (TypeError, ValueError, OSError):
                raise HeartbeatError("config_write_failed", phase="encode") from None
            return self._atomic_write(content)

    def restore_bytes(self, content: bytes) -> WriteReceipt:
        """Restore a validated snapshot without regenerating its representation."""
        with self.transaction_lock:
            self._decode(content)
            return self._atomic_write(content)

    def _previous_bytes(self) -> bytes | None:
        try:
            return self.config_file.read_bytes()  # noqa: IO_PRISON_VIOLATION
        except FileNotFoundError:
            if self._confirmed_missing_entry():
                return None
            raise HeartbeatError("config_read_failed", phase="read_before_write") from None
        except OSError:
            raise HeartbeatError("config_read_failed", phase="read_before_write") from None

    def _confirmed_missing_entry(self) -> bool:
        """Missing symlink targets do not authorize replacing an existing entry."""
        try:
            self.config_file.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            raise HeartbeatError("config_read_failed", phase="inspect_entry") from None
        return False

    def _classify_replace_failure(self, previous: bytes | None, content: bytes) -> bool | None:
        try:
            observed = self.config_file.read_bytes()  # noqa: IO_PRISON_VIOLATION
        except FileNotFoundError:
            return False if previous is None else None
        except OSError:
            return None
        if observed == previous == content:
            return None
        if observed == previous:
            return False
        if observed == content:
            return True
        return None

    def _sync_directory(self) -> str:
        """Windows lacks POSIX directory fsync; other I/O failures remain errors."""
        if os.name == "nt":
            return "DIRECTORY_FSYNC_UNSUPPORTED"
        descriptor = os.open(self.config_file.parent, os.O_RDONLY)  # noqa: IO_PRISON_VIOLATION
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return "CONFIRMED"

    def _atomic_write(self, content: bytes) -> WriteReceipt:
        previous = self._previous_bytes()
        temporary: Path | None = None
        phase = "temp_create"
        committed: bool | None = False
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{self.config_file.name}.",
                suffix=".tmp",
                dir=self.config_file.parent,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                phase = "write"
                handle.write(content)
                phase = "flush"
                handle.flush()
                phase = "file_fsync"
                os.fsync(handle.fileno())
                phase = "temp_close"
            phase = "replace"
            temporary.replace(self.config_file)
            committed = True
            temporary = None
            phase = "directory_fsync"
            durability = self._sync_directory()
            return WriteReceipt(committed=True, durability=durability)
        except OSError:
            if phase == "replace":
                committed = self._classify_replace_failure(previous, content)
            raise HeartbeatError(
                "config_write_failed",
                phase=phase,
                committed=committed,
                durability="NOT_CONFIRMED",
            ) from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)  # noqa: IO_PRISON_VIOLATION
                except OSError:
                    logger.warning("Heartbeat-owned temporary file cleanup failed")
