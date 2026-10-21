"""Dispatcher 共用的請求轉換與原生供應商解析工具。"""

import json
import logging
import re
from typing import Any

from src.adapters import tool_schema
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.core.exceptions import ErrorCode, ToukaException
from src.services.providers import registry

logger = logging.getLogger(__name__)

# KF-9：Anthropic 的 is_error 在 OpenAI／Gemini 都沒有對應欄位，改寫進工具結果的內容。
_TOOL_ERROR_PREFIX = "Tool error: "
_TOOL_BLOCKS = ("tool_use", "tool_result")


def strip_redundant_labels(content: str, alias_name: str) -> str:
    """剔除模型開頭可能自行標註的冗餘標籤 (如 【AgentName】：)"""
    if not content:
        return content

    alias = re.escape(alias_name)
    pattern = (
        rf"^\s*(\*\*|__)?\s*[【\[\(]?\s*{alias}\s*[】\]\)]?\s*(\*\*|__)?\s*[:：\s-]*\n*"
    )

    return re.sub(pattern, "", content, flags=re.IGNORECASE).strip()


def _field(message: Any, name: str) -> Any:
    """AnthropicMessage 物件或 dict 都能讀。"""
    value = getattr(message, name, None)
    return value or (message.get(name) if isinstance(message, dict) else None)


def _system_text(system: Any) -> str:
    """System 字串照用；text 區塊 list 依序合成字串（cache_control 等 Claude 標記不外送）。"""
    if isinstance(system, str):
        return system
    return "\n".join(block.get("text") or "" for block in system
                     if isinstance(block, dict) and block.get("type") == "text")


def _tool_output(block: dict) -> str:
    """tool_result 的內容 → 字串：text 區塊依序合併，圖片等不支援的區塊記警告後略過。"""
    content = block.get("content")
    if isinstance(content, list):
        texts = []
        for part in content:
            kind = part.get("type") if isinstance(part, dict) else type(part).__name__
            if kind == "text":
                texts.append(part.get("text") or "")
            else:
                logger.warning("tool_result 裡不支援的區塊已略過：%s", kind)
        content = "\n".join(texts)
    text = content if isinstance(content, str) else ""
    return _TOOL_ERROR_PREFIX + text if block.get("is_error") else text


def _convert_message(role: str, content: Any) -> list[OpenAIMessage]:
    """一則 Anthropic 訊息 → OpenAI 訊息；沒有工具區塊的原樣搬（KF-9）。

    tool_result 各成一則 role=tool（放最前面，緊接上一則的 tool_calls）；tool_use 變成同一則的
    tool_calls（call id＝tool_use id），這則只留文字（思考區塊只對 Claude 有意義）；使用者訊息裡
    其餘的區塊照原樣放在工具結果後面。
    """
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    uses = [b for b in blocks if b.get("type") == "tool_use"]
    results = [b for b in blocks if b.get("type") == "tool_result"]
    if not uses and not results:
        return [OpenAIMessage(role=role, content=content)]
    messages = [OpenAIMessage(role="tool", tool_call_id=b.get("tool_use_id"),
                              content=_tool_output(b)) for b in results]
    rest = [b for b in blocks if b.get("type") not in _TOOL_BLOCKS]
    if uses:
        text = "\n".join(b.get("text") or "" for b in rest if b.get("type") == "text")
        calls = [{"id": b.get("id"), "type": "function", "function": {
            "name": b.get("name"),
            "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}} for b in uses]
        messages.append(OpenAIMessage(role=role, content=text or None, tool_calls=calls))
    elif rest:
        messages.append(OpenAIMessage(role=role, content=rest))
    return messages


def convert_to_openai(request: Any, degraded: bool, reason: str) -> OpenAIChatRequest:
    """將 Anthropic 形狀請求轉成內部 OpenAI 相容請求（工具紀錄與 list system 見 KF-9）。"""
    openai_messages = []
    if hasattr(request, "system") and request.system:
        openai_messages.append(OpenAIMessage(role="system", content=_system_text(request.system)))

    if degraded:
        warning = f"\n\n⚠️ SYSTEM WARNING: {reason}"
        if openai_messages:
            openai_messages[0].content += warning
        else:
            openai_messages.append(OpenAIMessage(role="system", content=warning))

    for m in request.messages:
        openai_messages.extend(_convert_message(_field(m, "role"), _field(m, "content")))

    # O20：呼叫端明確給的 max_tokens 跟著轉；沒給（只有 AnthropicRequest 的預設值）照舊不帶。
    given = getattr(request, "model_fields_set", ())
    return OpenAIChatRequest(
        model=request.model,
        messages=openai_messages,
        temperature=getattr(request, "temperature", 0.7),
        max_tokens=request.max_tokens if "max_tokens" in given else None,
        # ADR-0111：轉發 top_p/top_k——先前只帶 temperature，原生 Gemini/OpenAI 相容模型與
        # custom/soul 的這兩個參數於 Anthropic→OpenAI 轉換時靜默丟失（預設 UI 路徑）。
        top_p=getattr(request, "top_p", None),
        top_k=getattr(request, "top_k", None),
        # 工具：request.tools（Anthropic schema）轉回 OpenAI（SSOT: tool_schema）。
        # 修正：過去寫死 None → OpenAI 相容/Gemini 模型收不到工具、無法調用。
        tools=tool_schema.anthropic_to_openai_tools(getattr(request, "tools", None)),
    )


def get_provider_info(model_name: str) -> tuple[str, str, str]:
    """依模型名解析外部 OpenAI 相容供應商的 (url, api_key, label)。

    供應商清單與其 URL/金鑰一律由 Provider Registry (SSOT) 衍生；新增供應商＝註冊表加一筆，
    不再於此維護 if 鏈。未知供應商乾淨報 ToukaException（不裸 crash）。
    """
    spec = registry.resolve_provider(model_name)
    if spec is None:
        raise ToukaException(
            ErrorCode.MODEL_NOT_FOUND,
            detail_override=(
                f"無法識別模型供應商：'{model_name}'。"
                "請確保名稱包含 grok, ollama, litellm, deepseek 或 gpt/openai。"
            ),
        )
    # chat_completions_url（非 get_route_url）：回正規化後的完整 chat 端點，避免 handler
    # POST 到基底（DeepSeek/OpenAI 的 .../v1）而非 /chat/completions → API Error。
    return registry.chat_completions_url(spec), registry.get_key(spec), spec.label
