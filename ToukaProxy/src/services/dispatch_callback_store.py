"""Dispatch Callback Store — asyncio.Event 雙向回呼通知（ADR-0051）.

ToukaProxy 在 initialize_mission(notify_on_complete=True) 時為每個 task 生成一個
callback_token，並在此 store 中注冊對應的 asyncio.Event。
ToukaDispatch 完成任務後 POST /v1/internal/dispatch/callback，觸發 deliver_result，
解除 Alice 在 await_dispatch_result 中的等待。

架構特性：
  - SRP：純狀態管理，不含路由或業務邏輯
  - In-memory：server 重啟後 pending token 失效（TTL 300s 內可接受）
  - Thread-safe：asyncio.Event 天生協程安全
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _PendingCallback:
    """等待中的回呼憑證，含 asyncio.Event 與結果容器。"""

    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: dict[str, Any] = field(default_factory=dict)


class DispatchCallbackStore:
    """管理 dispatch 任務的非同步等待憑證（token → asyncio.Event）.

    每個 callback_token 對應一個 _PendingCallback：
    - register(token)：建立等待槽
    - deliver_result(token, result)：交付結果並解除阻擋
    - wait_for_result(token, timeout)：協程等待直到交付或超時
    """

    def __init__(self) -> None:
        self._pending: dict[str, _PendingCallback] = {}

    def generate_token(self) -> str:
        """生成加密強度的隨機 token（URL-safe Base64, 32 bytes entropy）。"""
        return secrets.token_urlsafe(32)

    def register(self, token: str) -> None:
        """注冊 token，建立對應的 asyncio.Event 等待槽。"""
        self._pending[token] = _PendingCallback()
        logger.debug("Dispatch callback token registered: %s…", token[:8])

    def is_registered(self, token: str) -> bool:
        """回傳 token 是否在 pending 中（供 endpoint auth 驗證使用）。"""
        return token in self._pending

    def deliver_result(self, token: str, result: dict[str, Any]) -> bool:
        """交付結果並觸發 asyncio.Event，解除對應的 wait_for_result。

        Returns:
            True：交付成功；False：token 不存在（靜默失敗，不 raise）。
        """
        if token not in self._pending:
            logger.warning("Dispatch callback deliver failed: unknown token %s…", token[:8])
            return False
        cb = self._pending[token]
        cb.result = result
        cb.event.set()
        logger.debug("Dispatch callback delivered: token=%s…", token[:8])
        return True

    async def wait_for_result(
        self, token: str, timeout: float = 300.0
    ) -> dict[str, Any]:
        """協程等待 token 對應的結果，直到交付或超時。

        呼叫後不論成功、超時或未知 token，都會從 pending 中移除 token（TTL）。

        Returns:
            交付的 result dict；或 {"status": "TIMEOUT"}；或 {"status": "UNKNOWN_TOKEN"}。
        """
        if token not in self._pending:
            logger.warning(
                "wait_for_result called with unknown token %s…", token[:8] if token else "empty"
            )
            return {"status": "UNKNOWN_TOKEN"}
        cb = self._pending[token]
        try:
            await asyncio.wait_for(cb.event.wait(), timeout=timeout)
        except TimeoutError:
            logger.warning("Dispatch callback timed out: token=%s…", token[:8])
            return {"status": "TIMEOUT"}
        else:
            return cb.result
        finally:
            self._pending.pop(token, None)
            logger.debug("Dispatch callback token removed: %s…", token[:8])


# ---------------------------------------------------------------------------
# Singleton（可被測試重置）
# ---------------------------------------------------------------------------

_store: DispatchCallbackStore | None = None


def get_callback_store() -> DispatchCallbackStore:
    """回傳模組級 singleton DispatchCallbackStore。"""
    global _store  # noqa: PLW0603
    if _store is None:
        _store = DispatchCallbackStore()
    return _store


def _reset_callback_store() -> None:
    """測試專用：重置 singleton，確保測試間隔離。"""
    global _store  # noqa: PLW0603
    _store = None
