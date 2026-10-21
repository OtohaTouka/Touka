# ruff: noqa: D102, TC003
"""P8-A OAuth AES-256-GCM key ring and HMAC indexes。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from asyncio import to_thread
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from filelock import AsyncFileLock  # noqa: FILELOCK_ASYNC_SSOT
from pydantic import BaseModel, ConfigDict

_KID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STATE_DOMAIN = b"touka-mcp-oauth-state-v1\0"
_SLOT_DOMAIN = b"touka-mcp-oauth-slot-v1\0"
_REGISTRATION_DOMAIN = b"touka-mcp-oauth-registration-v1\0"


class OAuthKeyFormatError(ValueError):
    """Key ring configuration is malformed。"""


class OAuthDecryptionError(ValueError):
    """Ciphertext cannot be authenticated with the configured key ring。"""


class OAuthKeyUnavailableError(ValueError):
    """No durable OAuth key is available; ephemeral fallback is forbidden。"""


class OAuthSealedValue(BaseModel):
    """Portable AES-GCM envelope; contains ciphertext only。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = 1
    kid: str
    nonce: str
    ciphertext: str


@dataclass(frozen=True)
class OAuthKey:
    """One validated 256-bit OAuth key。"""

    kid: str
    material: bytes

    @classmethod
    def parse(cls, value: str) -> OAuthKey:
        """Parse ``v1:kid:base64url-32-byte``。"""
        parts = value.split(":")
        if len(parts) != 3 or parts[0] != "v1" or not _KID_RE.fullmatch(parts[1]):
            raise OAuthKeyFormatError()
        encoded = parts[2]
        try:
            material = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (ValueError, binascii.Error) as exc:
            raise OAuthKeyFormatError() from exc
        if len(material) != 32:
            raise OAuthKeyFormatError()
        return cls(kid=parts[1], material=material)

    def serialize(self) -> str:
        """Return the canonical durable v1 representation."""
        encoded = base64.urlsafe_b64encode(self.material).decode("ascii").rstrip("=")
        return f"v1:{self.kid}:{encoded}"


class OAuthKeyRing:
    """Current-write, bounded current/retiring-read OAuth key ring。"""

    def __init__(self, *, current: OAuthKey, retiring: tuple[OAuthKey, ...] = ()) -> None:
        if len(retiring) > 1:
            raise OAuthKeyFormatError()
        keys = (current, *retiring)
        if len({key.kid for key in keys}) != len(keys):
            raise OAuthKeyFormatError()
        self.current = current
        self.retiring = retiring
        self._keys = {key.kid: key for key in keys}

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _decode(value: str) -> bytes:
        try:
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, binascii.Error) as exc:
            raise OAuthDecryptionError() from exc

    def encrypt(self, plaintext: bytes, *, aad: bytes) -> OAuthSealedValue:
        """Encrypt with current key and caller-provided record binding。"""
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self.current.material).encrypt(nonce, plaintext, aad)
        return OAuthSealedValue(
            kid=self.current.kid,
            nonce=self._encode(nonce),
            ciphertext=self._encode(ciphertext),
        )

    def decrypt(self, sealed: OAuthSealedValue, *, aad: bytes) -> bytes:
        """Authenticate/decrypt with the exact envelope kid。"""
        key = self._keys.get(sealed.kid)
        if key is None or sealed.version != 1:
            raise OAuthDecryptionError()
        try:
            return AESGCM(key.material).decrypt(
                self._decode(sealed.nonce),
                self._decode(sealed.ciphertext),
                aad,
            )
        except (InvalidTag, ValueError) as exc:
            raise OAuthDecryptionError() from exc

    @staticmethod
    def _digest(key: OAuthKey, domain: bytes, value: bytes) -> tuple[str, str]:
        digest = hmac.new(key.material, domain + value, hashlib.sha256).hexdigest()
        return key.kid, digest

    def state_digest(self, raw_state: str) -> tuple[str, str]:
        """Return the current kid plus HMAC state index。"""
        return self._digest(self.current, _STATE_DOMAIN, raw_state.encode("utf-8"))

    def state_digest_candidates(self, raw_state: str) -> tuple[tuple[str, str], ...]:
        """Return current then retiring candidate indexes for callback lookup。"""
        return tuple(
            self._digest(key, _STATE_DOMAIN, raw_state.encode("utf-8"))
            for key in (self.current, *self.retiring)
        )

    def slot_digest(self, canonical_slot: bytes) -> tuple[str, str]:
        """Return current HMAC index for a stable authorization slot。"""
        return self._digest(self.current, _SLOT_DOMAIN, canonical_slot)

    def slot_digest_candidates(
        self,
        canonical_slot: bytes,
    ) -> tuple[tuple[str, str], ...]:
        """Return current/retiring slot candidates during rotation。"""
        return tuple(
            self._digest(key, _SLOT_DOMAIN, canonical_slot)
            for key in (self.current, *self.retiring)
        )

    def registration_digest(self, canonical_key: bytes) -> tuple[str, str]:
        """Return the current HMAC index for a DCR registration slot。"""
        return self._digest(self.current, _REGISTRATION_DOMAIN, canonical_key)

    def registration_digest_candidates(
        self,
        canonical_key: bytes,
    ) -> tuple[tuple[str, str], ...]:
        return tuple(
            self._digest(key, _REGISTRATION_DOMAIN, canonical_key)
            for key in (self.current, *self.retiring)
        )


class OAuthKeyRingProvider:
    """Single owner for managed/external key configuration and bootstrap locking。"""

    def __init__(
        self,
        *,
        source: Literal["managed", "external"] | str,
        current_value: str,
        retiring_json: str,
        lock_path: Path,
        bootstrap_enabled: bool,
        persist: Callable[[str, str], bool],
        reload_current: Callable[[], str] | None = None,
        persist_many: Callable[[dict[str, str]], bool] | None = None,
        reload_values: Callable[[], dict[str, str]] | None = None,
        current_name: str = "TOUKA_MCP_OAUTH_KEY",
        retiring_name: str = "TOUKA_MCP_OAUTH_RETIRED_KEYS",
        rotation_phase_name: str = "TOUKA_MCP_OAUTH_ROTATION_PHASE",
        key_epoch_name: str = "TOUKA_MCP_OAUTH_KEY_EPOCH",
    ) -> None:
        self._source = source
        self._current_value = current_value
        self._retiring_json = retiring_json
        self._lock = AsyncFileLock(str(lock_path), timeout=30)
        self._bootstrap_enabled = bootstrap_enabled
        self._persist = persist
        self._persist_many = persist_many
        self._reload_values = reload_values
        self._reload_current = reload_current or (lambda: self._current_value)
        self._current_name = current_name
        self._retiring_name = retiring_name
        self._rotation_phase_name = rotation_phase_name
        self._key_epoch_name = key_epoch_name

    @property
    def source(self) -> str:
        """Return the configured key source mode."""
        return self._source

    @staticmethod
    def _parse_retiring(value: str) -> tuple[OAuthKey, ...]:
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise OAuthKeyFormatError() from exc
        if (
            not isinstance(raw, list)
            or len(raw) > 1
            or any(not isinstance(item, str) for item in raw)
        ):
            raise OAuthKeyFormatError()
        return tuple(OAuthKey.parse(item) for item in raw)

    @staticmethod
    def _generate() -> str:
        kid = f"managed-{secrets.token_hex(8)}"
        encoded = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        return f"v1:{kid}:{encoded.rstrip('=')}"

    def create_rotation_candidate(self) -> OAuthKey:
        """Generate but do not persist a new managed key."""
        if self._source != "managed":
            raise OAuthKeyUnavailableError()
        return OAuthKey.parse(self._generate())

    def _authoritative_values(self) -> tuple[str, str]:
        if self._reload_values is None:
            return self._current_value, self._retiring_json
        values = self._reload_values()
        return (
            values.get(self._current_name, ""),
            values.get(self._retiring_name, "[]"),
        )

    @staticmethod
    def _ring(current_value: str, retiring_json: str) -> OAuthKeyRing:
        if not current_value:
            raise OAuthKeyUnavailableError()
        return OAuthKeyRing(
            current=OAuthKey.parse(current_value),
            retiring=OAuthKeyRingProvider._parse_retiring(retiring_json),
        )

    async def reload(self) -> OAuthKeyRing:
        """Reload the authoritative durable key ring without bootstrapping."""
        current_value, retiring_json = self._authoritative_values()
        ring = self._ring(current_value, retiring_json)
        self._current_value = current_value
        self._retiring_json = retiring_json
        return ring

    async def prepare_rotation(
        self,
        *,
        epoch: int,
        candidate: OAuthKey,
    ) -> OAuthKeyRing:
        """Atomically publish new-current and old-retiring in managed mode."""
        if self._source != "managed" or self._persist_many is None:
            raise OAuthKeyUnavailableError()
        async with self._lock:
            current_value, retiring_json = self._authoritative_values()
            current_ring = self._ring(current_value, retiring_json)
            if current_ring.retiring or candidate.kid == current_ring.current.kid:
                raise OAuthKeyFormatError()
            new_value = candidate.serialize()
            new_retiring_json = json.dumps(
                [current_ring.current.serialize()],
                separators=(",", ":"),
            )
            updates = {
                self._current_name: new_value,
                self._retiring_name: new_retiring_json,
                self._rotation_phase_name: "dual-read-new-write",
                self._key_epoch_name: str(epoch),
            }
            persisted = await to_thread(self._persist_many, updates)
            if not persisted:
                raise OAuthKeyUnavailableError()
            self._current_value = new_value
            self._retiring_json = new_retiring_json
            return OAuthKeyRing(current=candidate, retiring=(current_ring.current,))

    async def retire(self, *, epoch: int) -> OAuthKeyRing:
        """Atomically remove one retiring key after repository convergence."""
        if self._source != "managed" or self._persist_many is None:
            raise OAuthKeyUnavailableError()
        async with self._lock:
            current_value, retiring_json = self._authoritative_values()
            ring = self._ring(current_value, retiring_json)
            if len(ring.retiring) != 1:
                raise OAuthKeyFormatError()
            updates = {
                self._retiring_name: "[]",
                self._rotation_phase_name: "stable",
                self._key_epoch_name: str(epoch),
            }
            persisted = await to_thread(self._persist_many, updates)
            if not persisted:
                raise OAuthKeyUnavailableError()
            self._retiring_json = "[]"
            return OAuthKeyRing(current=ring.current)

    async def load(self) -> OAuthKeyRing:
        """Load a durable ring; managed bootstrap is serialized and must persist。"""
        if self._source not in {"managed", "external"}:
            raise OAuthKeyFormatError()
        current_value = self._current_value
        if not current_value and self._source == "managed":
            if not self._bootstrap_enabled:
                raise OAuthKeyUnavailableError()
            async with self._lock:
                current_value = self._reload_current()
                if not current_value:
                    candidate = self._generate()
                    persisted = await to_thread(
                        self._persist,
                        self._current_name,
                        candidate,
                    )
                    if not persisted:
                        raise OAuthKeyUnavailableError()
                    current_value = candidate
        if not current_value:
            raise OAuthKeyUnavailableError()
        self._current_value = current_value
        return self._ring(current_value, self._retiring_json)
