"""Ask-User Store — AI 詢問使用者的暫停-續跑原語（HAX G10，ADR-0085）.

`ask_user` 工具向使用者發問後，於此 store 註冊一個 token，並在 asyncio.Event 上等待；
前端作答經 POST /ask_user/answer/{token} 觸發 deliver_answer，解除工具的 wait_for_answer，
工具即以答案續跑。仿 dispatch_callback_store（ADR-0051）。

架構特性：SRP（純狀態管理）；in-memory（server 重啟後待答失效，沿用 ADR-0081 限制）；
asyncio.Event 天生協程安全。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _PendingQuestion:
    """等待中的提問憑證，含 asyncio.Event 與答案容器。"""

    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: dict[str, Any] = field(default_factory=dict)


class AskUserStore:
    """管理 ask_user 的非同步等待憑證（token → asyncio.Event）。"""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingQuestion] = {}

    def generate_token(self) -> str:
        """生成加密強度的隨機 token（URL-safe Base64, 32 bytes entropy）。"""
        return secrets.token_urlsafe(32)

    def register(self, token: str) -> None:
        """註冊 token，建立對應的 asyncio.Event 等待槽。"""
        self._pending[token] = _PendingQuestion()
        logger.debug("ask_user token registered: %s…", token[:8])

    def is_registered(self, token: str) -> bool:
        """回傳 token 是否在 pending 中。"""
        return token in self._pending

    def deliver_answer(self, token: str, answers: list[Any]) -> bool:
        """交付答案並觸發 asyncio.Event，解除對應的 wait_for_answer。

        Returns:
            True：交付成功；False：token 不存在（靜默失敗，不 raise）。
        """
        if token not in self._pending:
            logger.warning("ask_user deliver failed: unknown token %s…", token[:8] if token else "")
            return False
        pending = self._pending[token]
        pending.result = {"answers": answers}
        pending.event.set()
        logger.debug("ask_user answer delivered: token=%s…", token[:8])
        return True

    async def wait_for_answer(self, token: str, timeout: float = 600.0) -> dict[str, Any]:
        """協程等待 token 對應的答案，直到交付或超時；結束後移除 token（TTL）。

        Returns:
            {"answers": [...]}；或 {"status": "TIMEOUT"}；或 {"status": "UNKNOWN_TOKEN"}。
        """
        if token not in self._pending:
            logger.warning("wait_for_answer unknown token %s…", token[:8] if token else "empty")
            return {"status": "UNKNOWN_TOKEN"}
        pending = self._pending[token]
        try:
            await asyncio.wait_for(pending.event.wait(), timeout=timeout)
        except TimeoutError:
            logger.warning("ask_user timed out: token=%s…", token[:8])
            return {"status": "TIMEOUT"}
        else:
            return pending.result
        finally:
            self._pending.pop(token, None)


# ---------------------------------------------------------------------------
# Singleton（可被測試重置）
# ---------------------------------------------------------------------------

_store: AskUserStore | None = None


def get_ask_user_store() -> AskUserStore:
    """回傳模組級 singleton AskUserStore。"""
    global _store  # noqa: PLW0603
    if _store is None:
        _store = AskUserStore()
    return _store


def _reset_ask_user_store() -> None:
    """測試專用：重置 singleton，確保測試間隔離。"""
    global _store  # noqa: PLW0603
    _store = None
