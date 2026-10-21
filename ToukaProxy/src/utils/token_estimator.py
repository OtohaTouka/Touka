"""Token 估算器 (Token Estimator) — context 用量量測 SSOT.

純函式：以字元啟發式估算文字／訊息的 token 數，供 sliding window 的
context 百分比量測使用。不依賴外部 tokenizer（各家不一致），CJK 與
非 CJK 分開計權以兼顧中英文。估算偏保守（寧高估，避免低估而撐爆窗）。

權威錨點（authoritative anchor）由呼叫端套用：實際送出後，API 回傳的
``usage.input_tokens`` 為真值；本估算器負責「送出前估算」與「無 usage 時
的 fallback」的近似。**永不拋例外，永遠回非負整數。**
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 每 token 的平均字元數（保守取偏小值以略為高估 token，符合「寧高估」原則）。
# CJK：中日韓文字資訊密度高，約 1 字 ≈ 1 token。
# 非 CJK（英數／符號／空白）：約 4 字 ≈ 1 token，保守取 3.5。
_CJK_CHARS_PER_TOKEN = 1.0
_OTHER_CHARS_PER_TOKEN = 3.5

# 每則訊息的結構性 overhead（role 標記、分隔符等）。
_PER_MESSAGE_OVERHEAD_TOKENS = 4

# 最終 fallback：當啟發式本身出錯時，以極粗略的長度估替代。
_FALLBACK_CHARS_PER_TOKEN = 3


def _is_cjk(ch: str) -> bool:
    """判斷字元是否落在 CJK／日韓／全形等高資訊密度範圍。"""
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= code <= 0x4DBF  # CJK Ext-A
        or 0x3000 <= code <= 0x303F  # CJK 標點符號
        or 0x3040 <= code <= 0x30FF  # 平假名／片假名
        or 0xFF00 <= code <= 0xFFEF  # 全形 ASCII／半形片假名
        or 0xAC00 <= code <= 0xD7AF  # 韓文音節
    )


def estimate_tokens(text: str | None, model: str = "") -> int:
    """估算一段文字的 token 數（保守、CJK 感知、永不拋例外）。

    Args:
        text: 待估文字（``None`` 或非字串會被安全處理）。
        model: 模型名稱，目前僅保留供未來各家 tokenizer 微調；現用統一啟發式。

    Returns:
        非負整數估算 token 數。
    """
    _ = model  # 保留參數：未來可依模型族切換 divisor。
    try:
        if not text:
            return 0
        if not isinstance(text, str):
            text = str(text)
        cjk = 0
        other = 0
        for ch in text:
            if _is_cjk(ch):
                cjk += 1
            else:
                other += 1
        est = cjk / _CJK_CHARS_PER_TOKEN + other / _OTHER_CHARS_PER_TOKEN
        return max(0, round(est))
    except Exception:
        # 量測絕不可中斷主流程：退化為極保守的長度估。
        logger.debug("estimate_tokens fell back to length heuristic", exc_info=True)
        try:
            return max(0, len(str(text)) // _FALLBACK_CHARS_PER_TOKEN)
        except Exception:
            return 0


def _coerce_content(content: Any) -> str:
    """把訊息 ``content`` 攤平成可估算的文字。

    支援純字串與 Anthropic 風格的 content block 陣列（text / tool_use /
    tool_result / image…）；非文字 block 以其 JSON 文字長度近似。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    try:
                        parts.append(json.dumps(block, ensure_ascii=False))
                    except Exception:
                        parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def estimate_messages_tokens(
    messages: list[dict[str, Any]] | None, model: str = ""
) -> int:
    """估算一串對話訊息的總 token 數（含每則結構性 overhead）。永不拋例外。"""
    if not messages:
        return 0
    total = 0
    for msg in messages:
        content: Any = None
        if isinstance(msg, dict):
            content = msg.get("content")
        total += estimate_tokens(_coerce_content(content), model)
        total += _PER_MESSAGE_OVERHEAD_TOKENS
    return total
