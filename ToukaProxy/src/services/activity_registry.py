"""背景活動登記表 (Activity Registry).

以 session_id 追蹤「背景正在跑什麼」（如心跳多脈衝的 current/total），讓 UI 能看到
背景 run 的進度——背景脈衝是 detached BackgroundTask、無 SSE，否則完全不可見。

記憶體內、單例；活動結束（finally）即清除。經由 /stats 的 background_activity 對外輸出。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ActivityRegistry:
    """以會話為單位登記背景活動的單例。

    Attributes:
        _activities (dict[str, dict[str, Any]]): session_id -> 活動描述 dict。
    """

    def __init__(self) -> None:
        """初始化登記表。"""
        self._activities: dict[str, dict[str, Any]] = {}

    def set_activity(self, session_id: str | None, activity: dict[str, Any]) -> None:
        """登記/更新某會話的背景活動（session_id 為空則略過）。

        Args:
            session_id (str | None): 會話識別碼。
            activity (dict[str, Any]): 活動描述（如 {kind, current, total, agent}）。
        """
        if not session_id:
            return
        self._activities[session_id] = activity

    def clear(self, session_id: str | None) -> None:
        """清除某會話的背景活動。"""
        if session_id:
            self._activities.pop(session_id, None)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """回傳目前所有背景活動的快照（淺拷貝，供端點輸出）。"""
        return dict(self._activities)

    def reload_all(self) -> None:
        """清空所有登記（SSOT：供測試環境重置）。"""
        self._activities.clear()


# 全系統單例
activity_registry = ActivityRegistry()
