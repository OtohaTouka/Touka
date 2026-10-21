"""虛擬除錯代理處理器 (Human-as-Agent Handler).

定義 HumanModelHandler，用於手動除錯模式下提供虛擬的流式回應，不進行實際的 LLM 推理。
"""

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)


class HumanModelHandler:
    """虛擬除錯 Agent 處理器 (Human-as-Agent)。
    不執行實際推理，僅作為工具執行的中繼站。
    """

    async def generate_response(self, _request: Any, _ctx: dict[str, Any]) -> AsyncGenerator[str]:
        """模擬流式輸出，告知使用者目前處於「手動除錯模式」。
        """
        message = (
            "【Touka 系統訊息】目前已進入「虛擬除錯模式」。\n"
            "請透過下方的「工具調試面版」手動輸入指令，或在此輸入文字以記錄您的思考過程。"
        )

        # 模擬 OpenAI 格式的流式 chunk
        def yield_delta(content: str) -> str:
            """封裝單個流式內容區塊 (chunk) 的文字.

            Args:
                content (str): 要推送的流式文字內容。

            Returns:
                str: 符合 Server-Sent Events (SSE) 協定與 OpenAI 規範的 data 格式字串。
            """
            chunk = {
                "id": f"chatcmpl-debug-{int(get_current_epoch())}",
                "object": "chat.completion.chunk",
                "created": int(get_current_epoch()),
                "model": "DebugAgent",
                "choices": [{"delta": {"content": content}, "index": 0, "finish_reason": None}],
            }
            return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        yield yield_delta(message)
        yield "data: [DONE]\n\n"

    async def generate_tool_calls(
        self, _request: Any, _ctx: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """人類代理不主動產生工具調用。
        """
        return []
