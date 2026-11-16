"""還在跑的子代理（ADR-0318 PR 2）：純資料登記表，給主代理下一輪的 ``<continuity>`` 看.

只記標籤、派它的靈魂、對話與開始時間；不放 task、不做取消（取消由每個子代理自己的停止監看負責）。
子代理開始時登記、結束時（不論成敗或取消）`finish`；記在記憶體，Touka 重啟就清空。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass


@dataclass(frozen=True)
class RunningSubagent:
    """一個還在跑的子代理。``label`` 是模型給的 task_id，顯示前要先中和。"""

    label: str
    parent_agent_id: str
    session_id: str | None
    started_at: float


class SubagentRegistry:
    """子代理登記表（同一個事件迴圈內使用，不需要鎖）。"""

    def __init__(self) -> None:
        self._running: dict[int, RunningSubagent] = {}
        self._keys = itertools.count(1)

    def add(self, entry: RunningSubagent) -> int:
        """登記，回傳之後移除用的鍵。"""
        key = next(self._keys)
        self._running[key] = entry
        return key

    def finish(self, key: int) -> None:
        """結束時移除（重複移除不報錯）。"""
        self._running.pop(key, None)

    def running_in(self, parent_agent_id: str, session_id: str | None) -> list[RunningSubagent]:
        """這個靈魂在這段對話派出、還在跑的子代理（早派的在前；ID 不分大小寫）。

        沒有對話（IDE 等）時不列：分不出是哪個視窗派的。
        """
        if session_id is None:
            return []
        parent = parent_agent_id.casefold()
        return sorted(
            (
                entry
                for entry in self._running.values()
                if entry.parent_agent_id.casefold() == parent and entry.session_id == session_id
            ),
            key=lambda entry: entry.started_at,
        )

    def reset_for_test(self) -> None:
        """測試之間清空（conftest 呼叫）。"""
        self._running.clear()


subagent_registry = SubagentRegistry()
