"""Cross-platform async mission lock (ADR-0049).

Combines asyncio.Lock (in-process) with AsyncFileLock (cross-process) for safe
read-modify-write access to current_mission.json from multiple processes.

Both ToukaProxy (DispatchAgent) and ToukaDispatch (external AI via MCP) may
write to current_mission.json concurrently; asyncio.Lock alone is insufficient.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from filelock import AsyncFileLock  # noqa: FILELOCK_ASYNC_SSOT

_asyncio_locks: dict[str, asyncio.Lock] = {}
_file_locks: dict[str, AsyncFileLock] = {}
_FILELOCK_TIMEOUT = 30  # seconds; prevents deadlock if a process crashes mid-lock


class MissionLock:
    """Async context manager combining asyncio.Lock (in-process) and AsyncFileLock (cross-process).

    Acquisition order:
      1. asyncio.Lock     — fast, memory-only gate; serialises coroutines in same event loop
      2. AsyncFileLock    — OS-level gate; serialises across processes via async polling

    Both lock objects are cached per agent_id to ensure consistent identity across calls.
    On AsyncFileLock failure the asyncio.Lock is always released (exception safety).

    AsyncFileLock uses thread_local=False by default, ensuring acquire() and release()
    share the same lock_file_fd regardless of which thread pool thread executes them.
    No manual run_in_executor required (ADR-0049).
    """

    def __init__(self, agent_id: str, mission_path: Path) -> None:  # noqa: IDENTITY_SSOT
        """初始化任務鎖，為指定代理人與任務路徑設定進程與執行緒安全鎖。"""
        self._agent_id = agent_id
        if agent_id not in _file_locks:
            _file_locks[agent_id] = AsyncFileLock(
                str(mission_path) + ".lock",
                timeout=_FILELOCK_TIMEOUT,
            )
        self._file_lock = _file_locks[agent_id]

    def _asyncio_lock(self) -> asyncio.Lock:
        if self._agent_id not in _asyncio_locks:
            _asyncio_locks[self._agent_id] = asyncio.Lock()
        return _asyncio_locks[self._agent_id]

    async def __aenter__(self) -> MissionLock:
        await self._asyncio_lock().acquire()
        try:
            await self._file_lock.acquire()
        except Exception:
            self._asyncio_lock().release()
            raise
        return self

    async def __aexit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> bool:
        await self._file_lock.release()
        self._asyncio_lock().release()
        return False


def _reset_mission_lock_cache() -> None:
    """Reset global lock caches (asyncio.Lock + AsyncFileLock). For test isolation only."""
    _asyncio_locks.clear()
    _file_locks.clear()
