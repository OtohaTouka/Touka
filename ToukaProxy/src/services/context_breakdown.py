"""Context 用量分解 (Context Breakdown) — Phase B / ADR-0092.

從「即將送出的 payload」（system 字串 + tools + 對話訊息）以 B0 統一的 XML
邊界（``prompt_blocks.ALL_BLOCK_TAGS``）切出各區塊，逐塊估 token，並以
authoritative ``input_tokens`` 等比例校正總量，產出 Claude Code ``/context`` 風格
的分類別用量。供 ``token_usage_event`` 攜帶、前端 ContextMeter 渲染。

精度說明：分解為**本地估算**（各家 tokenizer 不一），故各塊加總≠真總數 →
以真 ``input_tokens`` 校正。家對話記憶（獨立 message）目前併入 conversation 計
（Tier-2 再細分）。工具為獨立 ``request.tools`` 欄位、單獨計。**永不拋例外。**
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from src.services.models.context_window_registry import get_context_window
from src.services.prompt_blocks import ALL_BLOCK_TAGS, PERSONA
from src.utils.token_estimator import estimate_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)

# 解析順序：具體區塊先於 persona，使「巢狀於 persona 內」的區塊歸入自己的類別、
# persona 只剩本體（soul 會吸收先前 system 內容，故可能巢狀）。
_PARSE_ORDER: tuple[str, ...] = tuple(t for t in ALL_BLOCK_TAGS if t != PERSONA) + (PERSONA,)


def _system_to_text(system: Any) -> str:
    """把 request.system（str / Anthropic block list / None）攤平成文字。"""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for b in system:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict) and isinstance(b.get("text"), str):
                parts.append(b["text"])
        return "\n".join(parts)
    return str(system)


def _parse_blocks(system_text: str, model: str) -> tuple[dict[str, int], int]:
    """以 XML 邊界切出已知區塊的 token，回 ``(block_tokens, leftover_tokens)``。

    重複 tag 累加同類；切走後的剩餘（裸 system 文字）另計為 leftover。
    """
    blocks: dict[str, int] = {}
    leftover = system_text
    for tag in _PARSE_ORDER:
        pattern = re.compile(rf"<{tag}>(.*?)</{tag}>", re.DOTALL)
        matched = pattern.findall(leftover)
        if matched:
            tok = sum(estimate_tokens(m, model) for m in matched)
            if tok:
                blocks[tag] = blocks.get(tag, 0) + tok
        leftover = pattern.sub("", leftover)
    return blocks, estimate_tokens(leftover, model)


def compute_context_breakdown(
    system: Any,
    tools: Any,
    messages: list[dict[str, Any]] | None,
    model: str,
    input_tokens: int | None,
) -> dict[str, Any]:
    """回傳 ``{usage_pct, window, total_tokens, breakdown:{category:tokens}}``。

    breakdown 各塊為本地估算、以 authoritative ``input_tokens`` 校正總量；
    ``usage_pct = total_tokens / window``（封頂 100）。**永不拋例外。**
    """
    try:
        window = get_context_window(model)
        block_tokens, leftover_tok = _parse_blocks(_system_to_text(system), model)

        tools_tok = (
            estimate_tokens(json.dumps(tools, ensure_ascii=False), model) if tools else 0
        )
        conv_tok = estimate_messages_tokens(messages, model) if messages else 0

        breakdown: dict[str, int] = dict(block_tokens)
        if tools_tok:
            breakdown["tools"] = tools_tok
        if conv_tok:
            breakdown["conversation"] = conv_tok
        if leftover_tok:
            breakdown["system_other"] = leftover_tok

        local_total = sum(breakdown.values())

        # 以 authoritative input_tokens 校正（等比例），使各塊加總 = 真總數。
        authoritative = (
            input_tokens if isinstance(input_tokens, int) and input_tokens > 0 else None
        )
        total = authoritative if authoritative is not None else local_total
        if authoritative is not None and local_total > 0 and total != local_total:
            scale = total / local_total
            breakdown = {k: max(0, round(v * scale)) for k, v in breakdown.items()}

        usage_pct = round(min(100.0, total / window * 100), 1) if window > 0 else 0.0

        return {
            "usage_pct": usage_pct,
            "window": window,
            "total_tokens": total,
            "breakdown": breakdown,
        }
    except Exception:
        logger.debug("compute_context_breakdown failed", exc_info=True)
        return {"usage_pct": 0.0, "window": 0, "total_tokens": 0, "breakdown": {}}
