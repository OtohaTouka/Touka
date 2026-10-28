"""Stop hook（ADR-0292）：主代理這一輪回完之後，在背景執行.

只記錄結果、不能讓代理人繼續：exit 2 與 ``decision: "block"`` 都當成記錄（回合結束的約定見
ADR-0146）。在背景執行，回覆不等 hook；子代理的回合不執行。沒有設定 hook 時不讀回覆文字。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from src.services.hooks.events import base_payload
from src.services.hooks.service import hook_service
from src.utils.task_utils import spawn_logged

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = logging.getLogger(__name__)

STOP_EVENT: Final[str] = "Stop"


def schedule_stop_hooks(ctx: Mapping[str, Any], final_text: Callable[[], str]) -> None:
    """有設定 Stop hook 時在背景執行；``final_text`` 只在要執行時才呼叫。出錯只記 log。"""
    try:
        if ctx.get("subagent_run") or not hook_service.has_hooks(STOP_EVENT):
            return
        payload: dict[str, Any] = {**base_payload(ctx), "stop_hook_active": False}
    except Exception:
        logger.exception("Stop hooks failed; the reply is not affected")
        return
    try:
        payload["last_assistant_message"] = final_text()
    except Exception:
        logger.exception("Stop hook could not read the final reply; sending it without one")
    spawn_logged(
        hook_service.dispatch(STOP_EVENT, payload, incognito=bool(ctx.get("incognito"))),
        name="hook:Stop",
    )
