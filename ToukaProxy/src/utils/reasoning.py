"""思考分頻剝離工具（ADR-0152 T1）。

把模型輸出「開頭」的 ``<think>…</think>``／``<thinking>…</thinking>`` 思考區塊自正文
剝離，供 handler 走獨立 reasoning 頻道（SSE ``delta.reasoning_content``）而非漏進對話。

僅處理**開頭位置**的區塊（模型思考前綴的實際型態）；正文中段的字面標籤（如使用者
引用「`<think>` 標籤的用途」）不動——誤剝正文的代價高於漏剝思考。

實際封包佐證（T1-capture）：grok-4.5 傳統端點思考走獨立 ``message.reasoning_content``
欄位（由 handler 直讀）；本工具涵蓋「內嵌標籤」型（DeepSeek-R1 蒸餾、Ollama 本地模型
等）＝防禦縱深，兩型 handler 都接。
"""

from __future__ import annotations

import re

# 開頭的 <think>/<thinking> 區塊（大小寫不敏感；容忍前導空白；懶惰匹配內文）。
_LEADING_THINK_RE = re.compile(
    r"^\s*<(think|thinking)>(.*?)</\1>\s*", re.IGNORECASE | re.DOTALL
)
# 未閉合的開頭標籤（串流截斷／整段皆思考）。
_UNCLOSED_THINK_RE = re.compile(
    r"^\s*<(think|thinking)>(.*)$", re.IGNORECASE | re.DOTALL
)


def split_reasoning(text: str | None) -> tuple[str, str]:
    """自 ``text`` 剝離開頭思考區塊。

    Returns:
        (reasoning, clean)：reasoning＝思考內文（多個開頭區塊以換行併接）、
        clean＝去除思考後的正文。無標籤時 ``("", text)``。
    """
    if not text:
        return "", ""

    reasoning_parts: list[str] = []
    remaining = text
    while True:
        m = _LEADING_THINK_RE.match(remaining)
        if not m:
            break
        reasoning_parts.append(m.group(2).strip())
        remaining = remaining[m.end():]

    if not reasoning_parts:
        m = _UNCLOSED_THINK_RE.match(remaining)
        if m:
            return m.group(2).strip(), ""
        return "", text

    return "\n".join(p for p in reasoning_parts if p), remaining
