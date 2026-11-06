"""聊天側剪裁先等畫面的收尾保存（ADR-0302）。

回覆的文字版存好之後，畫面收到終態還會把富版（工具步驟等）整棵存一次。剪裁如果搶先讀、
先存，畫面那次就因版本舊被擋；畫面先存，剪裁就作罷、摘要的模型呼叫白花。所以剪裁在背景
等畫面存好（保存端點存好時叫醒，不反覆讀檔），有上限；同一段對話只留一個在等，新的取代舊的。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

# 收尾保存通常在終態後一秒內就到；頁面關掉就等不到，逾時照舊以文字版剪
CLOSING_SAVE_WAIT_SECONDS = 10.0


@dataclass(eq=False)
class ClosingSave:
    """一輪回覆存好後等著的剪裁：文字版的版本、這一輪的助理節點與送出的人。

    版本含讀的人的身分，所以要用送出的人重讀才比得起來。
    """

    session_id: str
    revision: str
    assistant_node_id: str
    requester: AgentIdentity
    saved: asyncio.Event = field(default_factory=asyncio.Event)
    replaced: bool = False


class ClosingSaveWaits:
    """每段對話最多一個等待者；都在事件迴圈裡改，不需要鎖。"""

    def __init__(self) -> None:
        self._waiting: dict[str, ClosingSave] = {}

    def register(
        self, session_id: str, revision: str, assistant_node_id: str, requester: AgentIdentity
    ) -> ClosingSave:
        """同步登記（呼叫端在送出終態之前，中間不經 await）；舊的被叫醒並放棄。"""
        old = self._waiting.get(session_id)
        if old is not None:
            old.replaced = True
            old.saved.set()
        waiter = ClosingSave(session_id, revision, assistant_node_id, requester)
        self._waiting[session_id] = waiter
        return waiter

    def saved(self, session_id: str) -> None:
        """畫面存好了這段對話：叫醒等著的剪裁。"""
        waiter = self._waiting.get(session_id)
        if waiter is not None:
            waiter.saved.set()

    async def wait(self, waiter: ClosingSave) -> bool:
        """等畫面存好或逾時；被新的一輪取代時回 False。"""
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(waiter.saved.wait(), CLOSING_SAVE_WAIT_SECONDS)
            return not waiter.replaced
        finally:
            self.discard(waiter)

    def discard(self, waiter: ClosingSave) -> None:
        """不等了（等完、不用剪或出錯）：還是它在等就拿掉，已被取代就不動新的。"""
        if self._waiting.get(waiter.session_id) is waiter:
            del self._waiting[waiter.session_id]


closing_saves = ClosingSaveWaits()
