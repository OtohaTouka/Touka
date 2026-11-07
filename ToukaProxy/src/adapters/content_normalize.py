"""多模態內容正規化（SSOT）。

ToukaChat 前端把多模態訊息以 **JSON 字串**夾帶於 ``content``
（``buildApiMessage``：``JSON.stringify([{type:"text"...},{type:"image_url"...}])``），
但各供應商 adapter（Claude/Gemini/OpenAI passthrough）期望 ``content`` 為**真實 list**。

若不還原，整串 JSON（含 base64 圖片資料）會被當成單一文字區塊送給模型——圖片靜默失效。
此模組在 adapter 入口把「看起來是多模態區塊陣列的 JSON 字串」還原為 list，集中一處（SSOT），
讓三條供應商路徑共用同一份解析語意。
"""

from __future__ import annotations

import json
from typing import Any

# 前端 buildApiMessage 實際產出的多模態區塊型別（SSOT；新增型別時同步此集合）。
# 刻意只認這兩種：唯有「整段都是已知多模態區塊」才還原為 list，否則一律當純文字保留——
# 失敗安全（fail-safe），避免使用者剛好貼上一段 typed-dict JSON 卻被誤判、靜默丟失原文。
_KNOWN_BLOCK_TYPES = frozenset({"text", "image_url"})


def normalize_message_content(content: Any) -> Any:
    """若 ``content`` 是「多模態區塊陣列」的 JSON 字串 → 還原為 list；否則原樣回傳。

    僅在嚴格條件下解析（純防禦、永不拋例外）：去空白後同時以 ``[`` 起頭、``]`` 結尾、
    ``json.loads`` 成功、結果為**非空 list 且每個元素皆為 ``type`` 屬已知多模態型別的 dict**。
    任一元素型別不在 :data:`_KNOWN_BLOCK_TYPES` → 視為純文字原樣回傳（避免把使用者貼的
    ``[1,2,3]``、Markdown 清單、或 typed-dict JSON 誤判為多模態而丟失原文）。
    """
    if not isinstance(content, str):
        return content
    s = content.strip()
    if not (s.startswith("[") and s.endswith("]")):
        return content
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return content
    if not isinstance(parsed, list) or not parsed:
        return content
    if all(isinstance(block, dict) and block.get("type") in _KNOWN_BLOCK_TYPES for block in parsed):
        return parsed
    return content


def append_text_part(content: Any, text: str) -> Any:
    """在訊息 ``content`` 尾端附一段文字，回傳新內容；不認得的形狀回 ``None``（不附加）。

    多模態 JSON 字串先還原成 list 再加一個 text part（直接串字串會讓轉接層認不出圖片）；
    不改動傳入的 list。``<continuity>`` 與 ``<ide_context>`` 共用。
    """
    content = normalize_message_content(content)
    if isinstance(content, str):
        return content + text
    if isinstance(content, list):
        return [*content, {"type": "text", "text": text}]
    return None


def append_to_last_user(request: Any, text: str) -> bool:
    """接在請求最後一則訊息（須是 user）的尾端；不是 user 就不附（不改動歷史訊息）。

    ``<continuity>`` 與 hook 輸出共用；回傳有沒有附上。
    """
    messages = getattr(request, "messages", None)
    if not messages:
        return False
    last = messages[-1]
    is_dict = isinstance(last, dict)
    if (last.get("role") if is_dict else getattr(last, "role", None)) != "user":
        return False
    updated = append_text_part(
        last.get("content") if is_dict else getattr(last, "content", None), text
    )
    if updated is None:
        return False
    if is_dict:
        last["content"] = updated
    else:
        last.content = updated
    return True


def _field(message: Any, name: str) -> Any:
    return message.get(name) if isinstance(message, dict) else getattr(message, name, None)


def _is_blank_assistant(message: Any) -> bool:
    """沒有文字、也沒有工具呼叫或其他區塊的助理訊息（例如模型一個字都沒出就失敗的那一輪）。"""
    if _field(message, "role") != "assistant" or _field(message, "tool_calls"):
        return False
    content = normalize_message_content(_field(message, "content"))
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        return all(
            isinstance(block, dict)
            and block.get("type") == "text"
            and not str(block.get("text") or "").strip()
            for block in content
        )
    return False


def _as_blocks(content: Any) -> list[Any] | None:
    content = normalize_message_content(content)
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return list(content) if isinstance(content, list) else None


def _merged_user_content(first: Any, second: Any) -> Any:
    """兩則 user 的內容接成一則；都是純文字就用空行接，有區塊就接成區塊陣列。認不得回 None。"""
    first, second = normalize_message_content(first), normalize_message_content(second)
    if isinstance(first, str) and isinstance(second, str):
        return f"{first}\n\n{second}"
    head, tail = _as_blocks(first), _as_blocks(second)
    return None if head is None or tail is None else [*head, *tail]


def _with_content(message: Any, content: Any) -> Any:
    if isinstance(message, dict):
        return {**message, "content": content}
    return message.model_copy(update={"content": content})


def drop_blank_assistant_turns(messages: list[Any]) -> list[Any]:
    """拿掉空白的助理訊息；因此接在一起的前後兩則 user 合併成一則（ADR-0231 決定 6）。

    Anthropic 規定文字區塊至少 1 個字，其他家文件沒寫（2026-11-06 查），所以在分流到各家
    之前一起處理。原本就相鄰的 user 不動；回傳新串列，不改動傳入的訊息。
    """
    result: list[Any] = []
    dropped = False
    for message in messages:
        if _is_blank_assistant(message):
            dropped = True
            continue
        if dropped and result and _field(result[-1], "role") == _field(message, "role") == "user":
            merged = _merged_user_content(_field(result[-1], "content"), _field(message, "content"))
            if merged is not None:
                result[-1] = _with_content(result[-1], merged)
                dropped = False
                continue
        result.append(message)
        dropped = False
    return result


def message_text(content: Any) -> str:
    """訊息內容裡的文字（多模態 JSON 字串先還原；圖片等非文字區塊略過）。"""
    content = normalize_message_content(content)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if isinstance(part, str))
