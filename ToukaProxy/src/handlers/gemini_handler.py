"""Gemini model handler（react-core-extraction E2 後＝薄殼）。

本體已抽取：ReAct 迴圈政策 → ``src.handlers.react_core``（單一實作）；
Gemini wire 職責（模型對應/503 退避/Honesty Guard/parts 方言）
→ ``src.handlers.adapters.gemini_adapter``。本模組僅保留入口名（dispatcher
factory 與既有呼叫端零改動）。行為凍結由特徵化 golden 套件把關
（tests/proxy/characterization/test_golden_gemini.py）。
"""

from collections.abc import AsyncGenerator
from typing import Any

from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.react_core import run_react_loop


def handle_gemini_request(
    request: Any,
    context: dict[str, Any],
) -> AsyncGenerator[str]:
    """Handle a Gemini model request with ReAct loop support（主迴圈＝串流，ADR-0248）。"""
    return run_react_loop(GeminiAdapter(stream=True), request, context)
