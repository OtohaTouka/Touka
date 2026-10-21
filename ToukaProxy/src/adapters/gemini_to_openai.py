"""Gemini to OpenAI response adapter."""

import json

from src.utils.time_utils import get_current_epoch

# Gemini finishReason → OpenAI finish_reason 對映。
# OpenAI 合法值僅 stop / length / content_filter / tool_calls，
# 未知的 Gemini 值（如 OTHER、BLOCKLIST）一律 fallback 為 stop。
_FINISH_REASON_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
}


def convert_gemini_to_openai(response: dict, model_name: str = "gemini") -> dict:
    """轉換 Gemini Response 回 OpenAI Chat Completion 格式"""
    content_parts = []
    tool_calls = []

    candidates = response.get("candidates", [])
    if candidates:
        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts", [])
        for part in parts:
            if "text" in part:
                content_parts.append(part["text"])
            elif "functionCall" in part:
                # 處理工具呼叫轉換
                func_call = part["functionCall"]
                tool_calls.append(
                    {
                        "id": func_call.get(
                            "name"
                        ),  # Gemini 的 name 其實不唯一，用它充當 ID，以維持與 Open WebUI 互動
                        "type": "function",
                        "function": {
                            "name": func_call.get("name"),
                            "arguments": json.dumps(
                                func_call.get("args", {})
                            ),  # 將字典轉為 JSON 字串
                        },
                    }
                )

        raw_reason = candidate.get("finishReason", "STOP")
        finish_reason = _FINISH_REASON_MAP.get(raw_reason, "stop")
        if finish_reason == "stop" and tool_calls:
            finish_reason = "tool_calls"

    else:
        finish_reason = "stop"

    # Ensure arguments are real JSON string (P17 fix: use json.loads)
    for tc in tool_calls:
        try:
            raw = tc["function"]["arguments"]
            if "{" in raw:
                d = json.loads(raw)
                tc["function"]["arguments"] = json.dumps(d) if isinstance(d, dict) else raw
        except Exception:  # noqa: S110
            pass

    return {
        "id": "chatcmpl-gemini",
        "object": "chat.completion",
        "created": int(get_current_epoch()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "\n".join(content_parts) if content_parts else None,
                    "tool_calls": tool_calls if tool_calls else None,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": response.get("usageMetadata", {}).get(
                "promptTokenCount", 0
            ),
            # 思考 token Google 當輸出計費，一併算進輸出（ADR-0250 A6）
            "completion_tokens": response.get("usageMetadata", {}).get(
                "candidatesTokenCount", 0
            )
            + response.get("usageMetadata", {}).get("thoughtsTokenCount", 0),
            "total_tokens": response.get("usageMetadata", {}).get("totalTokenCount", 0),
        },
    }
