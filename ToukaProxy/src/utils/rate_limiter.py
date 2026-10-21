"""滑動視窗頻率限制器 (Sliding-Window Rate Limiter).

純記憶體實作，無外部依賴，適用於 FastAPI 依賴注入。
單例由各路由模組維護；此類別本身完全可單元測試（SOLID - SRP）。
"""

from __future__ import annotations


class RateLimiter:
    """Sliding-window in-memory rate limiter keyed by arbitrary string (e.g. IP).

    Thread-safety note: FastAPI runs in a single-threaded asyncio event loop,
    so no locking is needed for typical usage. Do NOT use across threads.

    Args:
        max_attempts: Maximum allowed attempts within the window.
        window_sec: Width of the sliding window in seconds.
    """

    def __init__(self, max_attempts: int, window_sec: int) -> None:
        """初始化頻率限制器.

        Args:
            max_attempts: 滑動視窗內允許的最大嘗試次數。
            window_sec: 滑動視窗的寬度（秒）。
        """
        self._max = max_attempts
        self._window = window_sec
        self._attempts: dict[str, list[float]] = {}

    def is_allowed(self, key: str, now: float) -> bool:
        """Return True when key has not yet exceeded max_attempts in the window.

        Evicts stale timestamps as a side-effect; does NOT record the current attempt.
        Call record() separately to count an attempt.

        Args:
            key: Identifying string (e.g. client IP address).
            now: Current epoch timestamp in seconds.
        """
        cutoff = now - self._window
        current = [t for t in self._attempts.get(key, []) if t > cutoff]
        self._attempts[key] = current
        return len(current) < self._max

    def record(self, key: str, now: float) -> None:
        """Record one attempt for key at timestamp now.

        Args:
            key: Identifying string (e.g. client IP address).
            now: Current epoch timestamp in seconds.
        """
        timestamps = self._attempts.get(key, [])
        timestamps.append(now)
        self._attempts[key] = timestamps

    def reset(self, key: str) -> None:
        """Clear all recorded attempts for key (e.g. after a successful auth).

        Args:
            key: Identifying string to reset.
        """
        self._attempts.pop(key, None)
