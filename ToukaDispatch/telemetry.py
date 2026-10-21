"""ToukaDispatch 相容窗遙測；只保留匿名聚合，不記 bearer 或 caller 名稱。"""

from __future__ import annotations

import threading
from typing import Any

from memory_server.config_utils import get_current_epoch


class DispatchTelemetry:
    """記錄 transport 使用量與最近成功應用認證，供 SSE sunset 決策。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._anonymous_connections = {"modern": 0, "legacy": 0}
        self._handshakes = {"internal": 0, "external": 0}
        self._last_used: dict[str, dict[str, Any]] = {
            "modern": {
                "at": None,
                "protocol_version": None,
                "caller_type": None,
            },
            "legacy": {
                "at": None,
                "protocol_version": None,
                "caller_type": None,
            },
        }

    def record_anonymous_connection(self, transport: str) -> None:
        """記錄未綁應用身分的 transport 連線／request。"""
        if transport not in self._anonymous_connections:
            return
        with self._lock:
            self._anonymous_connections[transport] += 1

    def record_handshake(
        self,
        *,
        caller_type: str,
        transport: str,
        protocol_version: str | None,
    ) -> None:
        """記錄成功 handshake 的衍生分面，不記 caller_name。"""
        if caller_type not in self._handshakes:
            return
        with self._lock:
            self._handshakes[caller_type] += 1
            self._set_last_used_unlocked(
                caller_type=caller_type,
                transport=transport,
                protocol_version=protocol_version,
            )

    def record_tool_use(
        self,
        *,
        caller_type: str,
        transport: str,
        protocol_version: str | None,
    ) -> None:
        """記錄通過逐 request 驗證後的最近使用時間。"""
        with self._lock:
            self._set_last_used_unlocked(
                caller_type=caller_type,
                transport=transport,
                protocol_version=protocol_version,
            )

    def _set_last_used_unlocked(
        self,
        *,
        caller_type: str,
        transport: str,
        protocol_version: str | None,
    ) -> None:
        if transport not in self._last_used:
            return
        self._last_used[transport] = {
            "at": get_current_epoch(),
            "protocol_version": protocol_version,
            "caller_type": caller_type,
        }

    def snapshot(self) -> dict[str, Any]:
        """回傳可公開於 loopback health 的匿名聚合快照。"""
        with self._lock:
            return {
                "anonymous_connections": dict(self._anonymous_connections),
                "handshakes": dict(self._handshakes),
                "last_used": {
                    transport: dict(value)
                    for transport, value in self._last_used.items()
                },
            }
