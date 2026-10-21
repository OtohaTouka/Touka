"""MCP SDK response-cache store with Touka principal isolation and diagnostics."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.client.caching import CacheEntry, CacheKey

_CACHE_PRINCIPAL: ContextVar[str] = ContextVar(
    "mcp_cache_principal",
    default="system",
)


def _principal_digest(principal: str) -> str:
    """Hash verified principal material so diagnostics/store keys never retain raw IDs."""
    return hashlib.sha256(principal.encode("utf-8")).hexdigest()


class InvalidCacheSizeError(ValueError):
    """Cache entry bound is negative."""

    def __init__(self, max_entries: int) -> None:
        super().__init__(f"max_entries must be >= 0, got {max_entries}")


class PrincipalAwareResponseCacheStore:
    """One bounded cache owner per installation.

    The MCP SDK already owns key construction, TTL/scope resolution and notification
    eviction. Touka only adds the security dimension the SDK's fixed ``partition``
    cannot express for a shared transport: private entries are stored under the
    currently verified request principal, while public entries have no principal arm.
    """

    def __init__(self, *, max_entries: int = 1024) -> None:
        if max_entries < 0:
            raise InvalidCacheSizeError(max_entries)
        self._max_entries = max_entries
        self._entries: dict[tuple[CacheKey, str | None], CacheEntry] = {}
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._evictions = 0

    @contextmanager
    def bind_principal(self, principal: str) -> Iterator[None]:
        """Bind verified principal for SDK store calls in this operation/task."""
        token = _CACHE_PRINCIPAL.set(_principal_digest(principal))
        try:
            yield
        finally:
            _CACHE_PRINCIPAL.reset(token)

    async def get(self, key: CacheKey) -> CacheEntry | None:
        """Return the current principal's private entry or the shared public entry."""
        principal = _CACHE_PRINCIPAL.get()
        storage_key = (key, principal)
        entry = self._entries.get(storage_key)
        if entry is None:
            storage_key = (key, None)
            entry = self._entries.get(storage_key)
        if entry is None:
            self._misses += 1
            return None
        self._entries[storage_key] = self._entries.pop(storage_key)
        self._hits += 1
        return entry

    async def set(self, key: CacheKey, entry: CacheEntry) -> None:
        """Store private entries per principal and public entries once."""
        principal = None if entry.scope == "public" else _CACHE_PRINCIPAL.get()
        storage_key = (key, principal)
        self._entries.pop(storage_key, None)
        self._entries[storage_key] = entry
        self._writes += 1
        if self._max_entries == 0:
            self._entries.clear()
            return
        while len(self._entries) > self._max_entries:
            del self._entries[next(iter(self._entries))]
            self._evictions += 1

    async def delete(self, key: CacheKey) -> None:
        """Evict this SDK key for every principal.

        A change notification has no Touka principal field. Deleting only the
        listener task's principal would leave other private arms stale.
        """
        matching = [storage_key for storage_key in self._entries if storage_key[0] == key]
        for storage_key in matching:
            del self._entries[storage_key]
        self._evictions += len(matching)

    async def clear(self) -> None:
        """Remove every entry owned by this installation store."""
        self._evictions += len(self._entries)
        self._entries.clear()

    def snapshot(self) -> dict[str, int]:
        """Return aggregate counters only; no key, URI, principal or payload."""
        public_entries = sum(principal is None for _key, principal in self._entries)
        return {
            "entries": len(self._entries),
            "public_entries": public_entries,
            "private_entries": len(self._entries) - public_entries,
            "hits": self._hits,
            "misses": self._misses,
            "writes": self._writes,
            "evictions": self._evictions,
            "max_entries": self._max_entries,
        }
