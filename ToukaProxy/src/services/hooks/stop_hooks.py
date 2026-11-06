"""Stop hook（ADR-0292）：主代理這一輪回完之後，在背景執行.

只記錄結果、不能讓代理人繼續：exit 2 與 ``decision: "block"`` 都當成記錄（回合結束的約定見
ADR-0146）。在背景執行，回覆不等 hook；子代理的回合不執行。沒有設定 hook 時不讀回覆文字。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from src.services.hooks.background import schedule_background_hook
from src.services.hooks.events import base_payload

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = logging.getLogger(__name__)

STOP_EVENT: Final[str] = "Stop"


def schedule_stop_hooks(ctx: Mapping[str, Any], final_text: Callable[[], str]) -> None:
    """有設定 Stop hook 時在背景執行；``final_text`` 只在要執行時才呼叫。出錯只記 log。"""

    def build_payload() -> dict[str, Any]:
        payload: dict[str, Any] = {**base_payload(ctx), "stop_hook_active": False}
        try:
            payload["last_assistant_message"] = final_text()
        except Exception:
            logger.exception("Stop hook could not read the final reply; sending it without one")
        return payload

    if not ctx.get("subagent_run"):
        schedule_background_hook(STOP_EVENT, build_payload, incognito=bool(ctx.get("incognito")))
