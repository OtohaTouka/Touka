"""清除連備份：重置時人類勾選後，清掉該靈魂在線上庫殘片、升級備份與快照裡的記憶.

只由 `services/agent_reset/orchestrator.py` 呼叫（不是工具）。本檔只放「清除進行中」旗標：
還原端點與第二個重置看到它就回 409；旗標由清除工作結束時清掉。清除期間也標記目標靈魂，
心跳看到就停（丙-4，ADR-0287）；請求取消後工作還在跑時一樣擋。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from src.services import destructive_ops

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

_IN_PROGRESS = threading.Event()


class ScrubSession:
    """一次清除的旗標持有者：交給工作執行緒之後由工作結束時清；沒交出去由協調器清."""

    def __init__(self, target: AgentIdentity) -> None:
        self.handed_over = False
        self._target = target
        self._ended = False

    def end(self) -> None:
        """清掉旗標、放行目標的心跳（重複呼叫無害）."""
        if not self._ended:
            self._ended = True
            _IN_PROGRESS.clear()
            destructive_ops.release(self._target)


def begin(target: AgentIdentity) -> ScrubSession:
    """預檢通過後設定旗標並標記目標."""
    destructive_ops.hold(target)
    _IN_PROGRESS.set()
    return ScrubSession(target)


def in_progress() -> bool:
    """目前是否有清除正在進行（還原端點、重置協調器讀）."""
    return _IN_PROGRESS.is_set()
