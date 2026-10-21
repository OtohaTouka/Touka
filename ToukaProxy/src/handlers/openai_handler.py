"""OpenAI-compatible model handler（react-core-extraction E1 後＝薄殼）。

本體已抽取：ReAct 迴圈政策 → ``src.handlers.react_core``（單一實作）；
OpenAI wire 職責 → ``src.handlers.adapters.openai_adapter``。本模組僅保留
入口名（dispatcher factory 與既有呼叫端零改動）。行為凍結由特徵化 golden
套件把關（tests/proxy/characterization/test_golden_openai.py）。
"""

from collections.abc import AsyncGenerator
from typing import Any

from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.handlers.react_core import run_react_loop


def handle_openai_request(
    request: Any,
    context: dict[str, Any],
) -> AsyncGenerator[str]:
    """Handle an OpenAI-compatible request with ReAct loop support（主迴圈＝串流，ADR-0248）。"""
    return run_react_loop(OpenAIAdapter(stream=True), request, context)


def handle_responses_request(
    request: Any,
    context: dict[str, Any],
) -> AsyncGenerator[str]:
    """官方 OpenAI 的 Responses 路由（主迴圈明確打開串流，ADR-0248 D1）。"""
    return run_react_loop(ResponsesAdapter(stream=True), request, context)
