"""插話的 UserPromptSubmit hook（ADR-0292）.

插話是使用者在 AI 工作中送出的話，加入佇列前一樣經過 UserPromptSubmit hook：擋下就回
``HOOK_PROMPT_BLOCKED``、不加入。hook 的補充說明不採用（插話文字原樣顯示、也存在回覆裡，不混進
hook 的輸出）。事件內容多一個 ``interjection: true``。hook 服務自己出錯時記 log、照常加入。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from src.core.exceptions import ErrorCode, ToukaException
from src.services.hooks.service import hook_service

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

PROMPT_EVENT: Final[str] = "UserPromptSubmit"


async def check_interjection(
    session_id: str, fields: Mapping[str, Any], text: str, *, incognito: bool
) -> bool:
    """有設定 hook 時執行，擋下就丟 ``HOOK_PROMPT_BLOCKED``；回傳有沒有等過 hook。

    ``fields`` 是串流建立時記下的共用欄位（``hooks.events.base_payload``），只取代理人；
    對話與是否無痕以這次請求驗過的為準。
    """
    try:
        if not hook_service.has_hooks(PROMPT_EVENT):
            return False
        payload = {
            "session_id": session_id,
            "agent_id": fields.get("agent_id"),
            "incognito": incognito,
            "prompt": text,
            "interjection": True,
        }
        decision = await hook_service.dispatch(PROMPT_EVENT, payload, incognito=incognito)
    except Exception:
        logger.exception("UserPromptSubmit hooks failed for an interjection; adding it anyway")
        return True
    if decision.blocked:
        logger.info("UserPromptSubmit hook blocked an interjection (session=%s)", session_id)
        raise ToukaException(ErrorCode.HOOK_PROMPT_BLOCKED, reason=decision.block_reason)
    return True
