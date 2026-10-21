"""Single-process deployment gate for P8-B workload OAuth。"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Literal

import filelock

if TYPE_CHECKING:
    from pathlib import Path


class McpWorkloadTopologyError(RuntimeError):
    """The deployment topology cannot safely host the single-process runtime。"""

    def __init__(self) -> None:
        super().__init__("P8-B requires an explicitly local filesystem topology.")


class McpWorkloadTopologyBusyError(McpWorkloadTopologyError):
    """Another Proxy process already owns the P8-B runtime lock。"""

    def __init__(self) -> None:
        RuntimeError.__init__(self, "Another Proxy process owns the P8-B runtime lock.")


class SingleProxyProcessGate:
    """Lifespan-owned exclusive lock; never treated as a recoverable worker lease。"""

    def __init__(
        self,
        *,
        lock_path: Path,
        filesystem_kind: Literal["local", "remote", "unknown"],
    ) -> None:
        self._filesystem_kind = filesystem_kind
        self._lock = filelock.AsyncFileLock(str(lock_path), timeout=0)
        self._acquired = False
        self._boot_id = secrets.token_hex(16)

    @property
    def acquired(self) -> bool:
        """Whether this instance currently owns the OS lock。"""
        return self._acquired

    @property
    def boot_id(self) -> str:
        """Unique in-memory process epoch used to bind ephemeral permits。"""
        return self._boot_id

    async def acquire(self) -> None:
        """Acquire immediately or fail closed without waiting for another process。"""
        if self._filesystem_kind != "local":
            raise McpWorkloadTopologyError()
        if self._acquired:
            return
        try:
            await self._lock.acquire()
        except filelock.Timeout as exc:
            raise McpWorkloadTopologyBusyError() from exc
        self._acquired = True

    async def aclose(self) -> None:
        """Release the lock during lifespan shutdown; safe when never acquired。"""
        if not self._acquired:
            return
        await self._lock.release()
        self._acquired = False
