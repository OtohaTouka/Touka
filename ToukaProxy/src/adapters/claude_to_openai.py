"""Anthropic 響應格式轉為 OpenAI Chat Completion 格式之適配器。"""

import json
from typing import Any

from src.utils.time_utils import get_current_epoch


def convert_anthropic_to_openai(anthropic_response: dict[str, Any]) -> dict[str, Any]:
    """轉換 Anthropic Response 回 OpenAI Chat Completion 格式"""
    content = anthropic_response.get("content", [])

    # 處理 text 或是 tool_calls 還有 thinking block
    text_parts = []
    tool_calls = []

    for block in content:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )
        elif block.get("type") == "thinking":
            # 將 thinking 區塊也保留下來供 Open Web UI 呈現 (可作為 markdown 引用或額外欄位)
            # OpenAI 格式本身並未原生支援 thinking，所以通常轉為 <think> text </think> 附帶在內容中
            thinking_text = block.get("thinking", "")
            text_parts.append(f"<think>\n{thinking_text}\n</think>")

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "\n\n".join(text_parts) if text_parts else None,
    }

    if tool_calls:
        message["tool_calls"] = tool_calls

    anth_usage = anthropic_response.get("usage", {})
    openai_usage = {
        "prompt_tokens": anth_usage.get("input_tokens", 0),
        "completion_tokens": anth_usage.get("output_tokens", 0),
        "total_tokens": anth_usage.get("input_tokens", 0)
        + anth_usage.get("output_tokens", 0),
    }

    return {
        "id": anthropic_response.get("id", "chatcmpl-unknown"),
        "object": "chat.completion",
        "created": int(get_current_epoch()),
        "model": anthropic_response.get("model", "claude-sonnet-4-6"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": "tool_calls"
                if anthropic_response.get("stop_reason") == "tool_use"
                else "stop",
            }
        ],
        "usage": openai_usage,
    }
