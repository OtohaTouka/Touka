"""Dispatch Callback Store — asyncio.Event 雙向回呼通知（ADR-0051）.

ToukaProxy 在 initialize_mission(notify_on_complete=True) 時為每個 task 生成一個
callback_token，並在此 store 中注冊對應的 asyncio.Event。
ToukaDispatch 完成任務後 POST /v1/internal/dispatch/callback，觸發 deliver_result，
解除 Alice 在 await_dispatch_result 中的等待。

架構特性：
  - SRP：純狀態管理，不含路由或業務邏輯
  - In-memory：server 重啟後 pending token 失效（TTL 300s 內可接受）
  - Thread-safe：asyncio.Event 天生協程安全
  - 站內憑證（``dispatch_subagent``，ADR-0318）：等候逾時不作廢、拿到結果才移除，超過子代理時限加
    寬限才清掉；只收 Touka 內部交回的結果（HTTP 回呼拒絕）。外部派工的憑證照舊：等完就移除。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from src.tools.contracts import SUBAGENT_TIMEOUT_SECONDS
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)

# 站內憑證活多久：子代理最長時限之後，再留一段時間給主代理回來等（ADR-0318）
IN_PROCESS_TOKEN_GRACE_SECONDS = 1800.0
IN_PROCESS_TOKEN_LIFETIME_SECONDS = SUBAGENT_TIMEOUT_SECONDS + IN_PROCESS_TOKEN_GRACE_SECONDS


@dataclass
class _PendingCallback:
    """等待中的回呼憑證，含 asyncio.Event 與結果容器。"""

    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: dict[str, Any] = field(default_factory=dict)
    in_process: bool = False
    created_at: float = 0.0


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

    def register(self, token: str, *, in_process: bool = False) -> None:
        """注冊 token，建立對應的 asyncio.Event 等待槽；``in_process`` 是站內子代理的憑證。"""
        self._drop_expired()
        self._pending[token] = _PendingCallback(
            in_process=in_process, created_at=get_current_epoch()
        )
        logger.debug("Dispatch callback token registered: %s…", token[:8])

    def is_registered(self, token: str) -> bool:
        """回傳 token 是否在 pending 中（供 endpoint auth 驗證使用）。"""
        return token in self._pending

    def is_in_process(self, token: str) -> bool:
        """站內子代理的憑證：HTTP 回呼不能交結果給它。"""
        pending = self._pending.get(token)
        return pending is not None and pending.in_process

    def _drop_expired(self) -> None:
        """清掉超過壽命的站內憑證；外部派工的照舊由等候結束時移除。"""
        cutoff = get_current_epoch() - IN_PROCESS_TOKEN_LIFETIME_SECONDS
        expired = [t for t, cb in self._pending.items() if cb.in_process and cb.created_at < cutoff]
        for token in expired:
            del self._pending[token]
            logger.debug("Expired in-process callback token dropped: %s…", token[:8])

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

        拿到結果就移除 token。外部派工的 token 逾時或等候被取消也移除；站內 token 留著，再等就接著等
        （ADR-0318），超過壽命由 ``_drop_expired`` 清掉。

        Returns:
            交付的 result dict；或 {"status": "TIMEOUT"}；或 {"status": "UNKNOWN_TOKEN"}。
        """
        self._drop_expired()
        if token not in self._pending:
            logger.warning(
                "wait_for_result called with unknown token %s…", token[:8] if token else "empty"
            )
            return {"status": "UNKNOWN_TOKEN"}
        cb = self._pending[token]
        delivered = False
        try:
            await asyncio.wait_for(cb.event.wait(), timeout=timeout)
            delivered = True
        except TimeoutError:
            logger.warning("Dispatch callback timed out: token=%s…", token[:8])
            return {"status": "TIMEOUT"}
        finally:
            if delivered or not cb.in_process:
                self._pending.pop(token, None)
                logger.debug("Dispatch callback token removed: %s…", token[:8])
        return cb.result


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
