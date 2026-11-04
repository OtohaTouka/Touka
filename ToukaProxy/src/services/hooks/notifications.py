"""Notification hook（ADR-0292）：有事要人注意時通知，只通知、不能改任何事.

matcher 看通知類型。``permission_prompt``、``elicitation_dialog``、``agent_needs_input`` 和
Claude Code 同名；``isolation_level`` 是 Touka 多的（隔離層級改變）。訊息是固定文字，只放工具名稱
或層級名稱，不放參數、問題內容與原因細節。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from src.services.hooks.background import schedule_background_hook
from src.services.hooks.events import base_payload

if TYPE_CHECKING:
    from collections.abc import Mapping

    from src.services.isolation.status import IsolationSnapshot

NOTIFICATION_EVENT: Final[str] = "Notification"
PERMISSION_PROMPT: Final[str] = "permission_prompt"
ELICITATION_DIALOG: Final[str] = "elicitation_dialog"
AGENT_NEEDS_INPUT: Final[str] = "agent_needs_input"
ISOLATION_LEVEL: Final[str] = "isolation_level"


def _notify(ctx: Mapping[str, Any], notification_type: str, message: str) -> None:
    schedule_background_hook(
        NOTIFICATION_EVENT,
        lambda: {**base_payload(ctx), "notification_type": notification_type, "message": message},
        match=(notification_type,),
        incognito=bool(ctx.get("incognito")),
    )


def notify_approval_card(ctx: Mapping[str, Any], tool: str) -> None:
    """審批卡已經跳出。"""
    _notify(ctx, PERMISSION_PROMPT, f"Tool '{tool}' needs your approval.")


def notify_mcp_input(ctx: Mapping[str, Any], tool: str) -> None:
    """MCP 工具要使用者填資料的表單已經跳出。"""
    _notify(ctx, ELICITATION_DIALOG, f"Tool '{tool}' is asking you for input.")


def notify_agent_question(ctx: Mapping[str, Any]) -> None:
    """代理人的問題已經跳出。"""
    _notify(ctx, AGENT_NEEDS_INPUT, "The agent is waiting for your answer.")


class IsolationLevelNotifier:
    """隔離層級改變時通知（第一次也算）；monitor 也會為本機原因或執行器重啟通知，這裡濾掉。"""

    def __init__(self) -> None:
        """還沒通知過任何層級。"""
        self._last_level: str | None = None

    async def __call__(self, snapshot: IsolationSnapshot) -> None:
        """Monitor 的 ``on_level_change`` 回呼；不屬於任何對話，所以 session_id 是空的。"""
        if snapshot.level == self._last_level:
            return
        self._last_level = snapshot.level
        _notify({}, ISOLATION_LEVEL, f"Agent command isolation level is now {snapshot.level}.")
