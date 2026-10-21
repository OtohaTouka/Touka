"""Provider Adapters（react-core-extraction，拍板 K4）。

每供應商一個 adapter：只擁有 wire 職責（request 整備／傳輸／回合解析／歷史寫回／
終端包裝）；ReAct 迴圈政策一律在 `src.handlers.react_core`（單一實作）。
"""

from .base import ProviderAdapter, RoundError, ToolCall, Turn
from .openai_adapter import OpenAIAdapter

__all__ = ["OpenAIAdapter", "ProviderAdapter", "RoundError", "ToolCall", "Turn"]
