"""UserPromptSubmit hook（ADR-0292）：身分判定之後、組上下文之前執行.

只在真的請求（``ctx.run_hooks``，由 ``process_proxy_request`` 設定）執行；上下文預覽與子代理
不執行。hook 擋下就回 ``HOOK_PROMPT_BLOCKED``（不送模型）；hook 的補充說明當成資料接在
最後一則 user 訊息尾端，只用在這一次請求。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.adapters.content_normalize import append_to_last_user
from src.core.exceptions import ErrorCode, ToukaException
from src.middleware.base import ProxyLayer
from src.services.hooks.events import base_payload, last_user_prompt, wrap_hook_output
from src.services.hooks.service import hook_service

if TYPE_CHECKING:
    from src.models.context import PipelineContext

logger = logging.getLogger(__name__)

PROMPT_EVENT = "UserPromptSubmit"


class HookLayer(ProxyLayer):
    """執行 UserPromptSubmit hook。"""

    async def process(self, request: Any, ctx: PipelineContext) -> None:
        """有設定 hook 時才組事件內容並執行。"""
        if not ctx.get("run_hooks") or ctx.get("subagent_run"):
            return
        if not hook_service.has_hooks(PROMPT_EVENT):
            return
        prompt = last_user_prompt(request)
        if prompt is None:
            return
        decision = await hook_service.dispatch(
            PROMPT_EVENT,
            {**base_payload(ctx), "prompt": prompt},
            incognito=bool(ctx.get("incognito")),
        )
        if decision.blocked:
            logger.info(
                "UserPromptSubmit hook blocked a prompt (session=%s)", ctx.get("session_id")
            )
            raise ToukaException(ErrorCode.HOOK_PROMPT_BLOCKED, reason=decision.reason)
        if decision.context:
            append_to_last_user(request, "\n\n" + wrap_hook_output(PROMPT_EVENT, decision.context))
