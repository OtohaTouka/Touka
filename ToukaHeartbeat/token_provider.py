"""One Heartbeat credential resolver, with explicit shared-service readiness."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Literal, Protocol

if __package__:
    from .errors import HeartbeatError
else:
    from errors import HeartbeatError


@dataclass(frozen=True)
class Credential:
    """Internal credential; its value must never appear in status or repr."""

    value: str = field(repr=False)
    source: Literal["shared_setting", "local_fallback"]
    shared_ready: bool

    def accepts(self, candidate: str) -> bool:
        """Reject empty values and compare UTF-8 bytes in constant time."""
        return bool(self.value and candidate) and secrets.compare_digest(
            self.value.encode(), candidate.encode()
        )


class TokenStorage(Protocol):
    """Storage owns the existing env > file > generated resolution chain."""

    @property
    def shared_credential_ready(self) -> bool:
        """Return whether a nonempty local shared setting was supplied."""
        ...

    def get_token(self) -> str:
        """Resolve through the storage-owned precedence chain."""
        ...


class TokenProvider(Protocol):
    """Inject the same readiness and resolution into every consuming boundary."""

    def resolve(self, *, require_shared: bool = False) -> Credential:
        """Return one value/readiness snapshot or a fixed domain error."""
        ...


class StorageTokenProvider:
    """Resolve from one startup-bound storage; do not imply hot rotation."""

    def __init__(self, storage: TokenStorage) -> None:
        self.storage = storage

    def resolve(self, *, require_shared: bool = False) -> Credential:
        """Local fallback authorizes management, but cannot authorize outbound work."""
        ready = self.storage.shared_credential_ready
        if require_shared and not ready:
            raise HeartbeatError("shared_credential_not_ready", phase="credential")
        value = self.storage.get_token()
        if require_shared and not value:
            raise HeartbeatError("shared_credential_not_ready", phase="credential")
        return Credential(value, "shared_setting" if ready else "local_fallback", ready)
