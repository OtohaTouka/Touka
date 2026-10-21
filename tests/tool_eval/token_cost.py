"""工具 schema token 成本量測（P0-D；Claude Code「工具 schema 佔 token 大半」教訓）.

逐工具把 schema 序列化成 JSON 後過 Touka 的 token 估算器 SSOT（ADR-0092，CJK 感知、
永不拋例外），回傳由大到小的 (工具名, tokens) ＋總計。純函式、可決定性。量測是**相對
比較**（多腦 tokenizer 不同）——用於趨勢/排行，非絕對值。
"""

from __future__ import annotations

import json
from typing import Any

from src.utils.token_estimator import estimate_tokens


def _tool_name(schema: dict[str, Any]) -> str:
    """容忍 OpenAI（{function:{name}}）與 Anthropic（{name}）兩種形狀。"""
    fn = schema.get("function") if isinstance(schema.get("function"), dict) else schema
    return fn.get("name") or schema.get("name") or "<unnamed>"


def measure_tool_token_cost(
    tools: list[dict[str, Any]],
) -> tuple[list[tuple[str, int]], int]:
    """逐工具估 token。回傳 (由大到小的 [(name, tokens)], 總計)。"""
    per = [
        (_tool_name(t), estimate_tokens(json.dumps(t, ensure_ascii=False))) for t in tools
    ]
    per.sort(key=lambda x: x[1], reverse=True)
    return per, sum(n for _, n in per)
