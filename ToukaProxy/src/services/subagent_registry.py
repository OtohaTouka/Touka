"""子代理登記表：給主代理下一輪的 ``<continuity>`` 看.

- 還在跑的（ADR-0318 PR 2）：只記標籤、派它的靈魂、對話與開始時間；不放 task、不做取消（取消由每個
  子代理自己的停止監看負責）。子代理開始時登記、結束時（不論成敗或取消）`finish`。
- 最近結束的（ADR-0319 PR 4）：背景派出的子代理結束時記標籤、結果種類（固定值）、結束時間、讀結果的
  憑證與讀過沒有；每段對話只留最新 ``FINISHED_PER_CONVERSATION`` 筆，``FINISHED_KEEP_SECONDS``
  之後清掉。

都只在記憶體，Touka 重啟就清空。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, replace
from typing import Literal

SubagentOutcome = Literal["done", "partial", "failed", "cancelled"]
FINISHED_PER_CONVERSATION = 5
FINISHED_KEEP_SECONDS = 2 * 3600


@dataclass(frozen=True)
class RunningSubagent:
    """一個還在跑的子代理。``label`` 是模型給的 task_id，顯示前要先中和。"""

    label: str
    parent_agent_id: str
    session_id: str | None
    started_at: float


@dataclass(frozen=True)
class FinishedSubagent:
    """一個背景派出、已經結束的子代理；``token`` 是系統產生的讀結果憑證。"""

    label: str
    parent_agent_id: str
    session_id: str
    finished_at: float
    outcome: SubagentOutcome
    token: str
    read: bool = False


class SubagentRegistry:
    """子代理登記表（同一個事件迴圈內使用，不需要鎖）。"""

    def __init__(self) -> None:
        self._running: dict[int, RunningSubagent] = {}
        self._finished: list[FinishedSubagent] = []
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

    def record_finished(self, entry: FinishedSubagent) -> None:
        """背景派出的子代理結束時記下；同一段對話只留最新幾筆，過期的順便清掉。"""
        self._drop_expired(entry.finished_at)
        self._finished.append(entry)
        same = self._finished_for(entry.parent_agent_id, entry.session_id)
        stale = {id(old) for old in same[:-FINISHED_PER_CONVERSATION]}
        self._finished = [kept for kept in self._finished if id(kept) not in stale]

    def mark_read(self, token: str) -> None:
        """這個憑證的結果讀到了（``await_dispatch_result`` 拿到結果時）。"""
        self._finished = [
            replace(entry, read=True) if entry.token == token else entry for entry in self._finished
        ]

    def finished_in(
        self, parent_agent_id: str, session_id: str | None, now_s: float
    ) -> list[FinishedSubagent]:
        """這個靈魂在這段對話最近結束的子代理（新的在前）；沒有對話時不列（同 ``running_in``）。"""
        self._drop_expired(now_s)
        if session_id is None:
            return []
        return self._finished_for(parent_agent_id, session_id)[::-1]

    def _finished_for(self, parent_agent_id: str, session_id: str) -> list[FinishedSubagent]:
        parent = parent_agent_id.casefold()
        return [
            entry
            for entry in self._finished
            if entry.parent_agent_id.casefold() == parent and entry.session_id == session_id
        ]

    def _drop_expired(self, now_s: float) -> None:
        self._finished = [
            entry for entry in self._finished if now_s - entry.finished_at < FINISHED_KEEP_SECONDS
        ]

    def reset_for_test(self) -> None:
        """測試之間清空（conftest 呼叫）。"""
        self._running.clear()
        self._finished.clear()


subagent_registry = SubagentRegistry()
