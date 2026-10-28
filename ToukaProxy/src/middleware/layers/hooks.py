"""UserPromptSubmit hook（ADR-0292）：身分判定之後、組上下文之前執行.

只在真的請求（``ctx.run_hooks``，由 ``process_proxy_request`` 設定）執行；上下文預覽與子代理
不執行；最後一則 user 訊息只有工具結果（工具回合）時也不執行。hook 擋下就回
``HOOK_PROMPT_BLOCKED``（不送模型）；hook 的補充說明當成資料接在最後一則 user 訊息尾端，只用在
這一次請求。hook 服務自己出錯時記 log、當成沒有 hook 結果（不擋、不讓請求失敗）。

這時對話的模式還沒決定（``ToolLayer`` 才決定），事件內容不帶 ``permission_mode``（ctx 裡就算
有值也不是這次對話的模式）。

hook 執行時串流還沒登記，使用者按停止只會設這個對話的中斷旗標（``rhythm_service``）；hook 回來
後看到旗標就回 ``PROMPT_STOPPED``、不送模型。擋下優先：還連著的用戶端要靠它把那一輪拿掉。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.adapters.content_normalize import append_to_last_user
from src.core.exceptions import ErrorCode, ToukaException
from src.middleware.base import ProxyLayer
from src.services.hooks.events import base_payload, last_user_prompt, wrap_hook_output
from src.services.hooks.service import hook_service
from src.services.rhythm_service import rhythm_service
from src.utils.auth_mode import request_is_incognito

if TYPE_CHECKING:
    from src.models.context import PipelineContext
    from src.services.hooks.service import HookDecision

logger = logging.getLogger(__name__)

PROMPT_EVENT = "UserPromptSubmit"
# 擋下理由放進 403 回應時的上限（多個 hook 的理由合併後可能很長）
MAX_REASON_CHARS = 4_000


class HookLayer(ProxyLayer):
    """執行 UserPromptSubmit hook。"""

    async def process(self, request: Any, ctx: PipelineContext) -> None:
        """有設定 hook 時才組事件內容並執行。"""
        if not ctx.get("run_hooks") or ctx.get("subagent_run"):
            return
        decision = await self._decide(request, ctx)
        if decision is None:
            return
        if decision.blocked:
            logger.info(
                "UserPromptSubmit hook blocked a prompt (session=%s)", ctx.get("session_id")
            )
            raise ToukaException(
                ErrorCode.HOOK_PROMPT_BLOCKED, reason=decision.reason[:MAX_REASON_CHARS]
            )
        session_id = ctx.get("session_id")
        if decision.ran and session_id and rhythm_service.is_interrupted(session_id):
            logger.info("Prompt stopped by the user while hooks ran (session=%s)", session_id)
            raise ToukaException(ErrorCode.PROMPT_STOPPED)
        if decision.context:
            append_to_last_user(request, "\n\n" + wrap_hook_output(PROMPT_EVENT, decision.context))

    @staticmethod
    async def _decide(request: Any, ctx: PipelineContext) -> HookDecision | None:
        """沒有 hook、不是使用者送出的訊息或 hook 服務出錯時回 None。"""
        try:
            if not hook_service.has_hooks(PROMPT_EVENT):
                return None
            prompt = last_user_prompt(request)
            if prompt is None:
                return None
            incognito = bool(ctx.get("incognito")) or request_is_incognito(request)
            payload = {**base_payload(ctx), "incognito": incognito, "prompt": prompt}
            payload.pop("permission_mode", None)
            return await hook_service.dispatch(PROMPT_EVENT, payload, incognito=incognito)
        except Exception:
            logger.exception("UserPromptSubmit hooks failed; continuing without them")
            return None
