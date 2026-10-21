"""Claude model handler（react-core-extraction E3 後＝薄殼）。

本體已抽取：ReAct 迴圈政策 → ``src.handlers.react_core``（單一實作）；
Claude wire 職責（雙 ingress/egress、四斷點 prompt 快取、5s keepalive、
thinking 分頻）→ ``src.handlers.adapters.claude_adapter``。本模組僅保留
入口名（dispatcher factory 與既有呼叫端零改動）。行為凍結由特徵化 golden
套件把關（tests/proxy/characterization/test_golden_claude.py）。
"""

from collections.abc import AsyncGenerator
from typing import Any

from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.react_core import run_react_loop


def handle_claude_request(
    request: Any,
    context: dict[str, Any],
) -> AsyncGenerator[str]:
    """Handle a proxied request to the Anthropic Claude API（主迴圈＝上游串流，ADR-0248 D1）。"""
    return run_react_loop(ClaudeAdapter(stream=True), request, context)
