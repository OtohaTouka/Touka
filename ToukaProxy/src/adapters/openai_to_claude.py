"""OpenAI 請求格式轉為 Anthropic Messages 請求格式之適配器。"""

import json
import logging
from typing import Any

from src.services.models.model_rules import claude_rules, claude_send_max_tokens
from src.services.prompt_blocks import REQUEST_TAIL_TAGS

from . import tool_schema
from .content_normalize import normalize_message_content
from .schemas import AnthropicMessage, AnthropicRequest, AnthropicThinking, OpenAIChatRequest

logger = logging.getLogger(__name__)

_THINKING_SUFFIX = "-thinking"  # Touka 的模型名尾碼：要求開思考（送出前去掉）


def convert_openai_to_anthropic(request: OpenAIChatRequest) -> AnthropicRequest:
    """轉換 OpenAI Chat Completion 請求為 Anthropic Messages 請求"""
    anthropic_messages = []
    system_texts: list[str] = []

    for msg in request.messages:
        # 兼容字典與物件格式 (因 SoulLayer 注入的歷史紀錄可能是字典)
        role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
        content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
        # ToukaChat 夾帶的多模態 JSON 字串 → list。**排除 role=="tool"**：工具結果是任意文字、
        # 下方 tool 分支會 str(content)，若先被還原成 list 會被 str() 轉成 Python repr 而毀損（複審）。
        if role != "tool":
            content = normalize_message_content(content)
        tool_calls = getattr(msg, "tool_calls", None) or (msg.get("tool_calls") if isinstance(msg, dict) else None)

        if role == "system":
            if isinstance(content, str):
                system_texts.append(content)  # KF-10：多則依序合併，不只留最後一則
            continue

        # 處理 tool_calls (OpenAI -> Anthropic)
        content_blocks = []
        if isinstance(content, str) and content:
            content_blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            # 處理多模態或其他陣列格式的 content
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type")
                    if btype == "text":
                        content_blocks.append({"type": "text", "text": block.get("text")})
                    elif btype == "image_url":
                        image_block = _openai_image_block_to_anthropic(block)
                        if image_block is not None:
                            content_blocks.append(image_block)
                        else:
                            # 失效可見化（ADR-0078）：圖片無法轉換不靜默丟棄。
                            # 不記 URL/資料（可能為巨量 base64 或敏感連結）。
                            logger.warning(
                                "OpenAI image_url block 無法轉為 Anthropic image"
                                "（不支援的 URL 格式或非 base64 inline），已略過。"
                            )
                    elif btype:
                        # 失效可見化：未支援的內容區塊型別不靜默丟棄（與 Gemini 路徑對稱）。
                        logger.warning("不支援的內容區塊型別 '%s'，已略過。", btype)

        # 如果 OpenAI 有給 tool_calls, 轉成 Anthropic 的 tool_use block
        if tool_calls:
            for tool_call in tool_calls:
                func = tool_call.get("function", {}) if isinstance(tool_call, dict) else getattr(tool_call, "function", {})
                args_str = func.get("arguments", "{}") if isinstance(func, dict) else getattr(func, "arguments", {})
                try:
                    args = json.loads(args_str)
                except (json.JSONDecodeError, TypeError):
                    args = {}

                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool_call.get("id") if isinstance(tool_call, dict) else getattr(tool_call, "id", None),
                        "name": func.get("name") if isinstance(func, dict) else getattr(func, "name", None),
                        "input": args,
                    }
                )

        # 處理 user 回傳 tool result (OpenAI role="tool" -> Anthropic role="user" + tool_result block)
        if role == "tool":
            tool_use_id = getattr(msg, "tool_call_id", None) or (msg.get("tool_call_id") if isinstance(msg, dict) else None)
            content_blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": str(content),
                }
            ]
            anthropic_messages.append(
                AnthropicMessage(role="user", content=content_blocks)
            )
            continue

        role_to_use = "assistant" if role == "assistant" else "user"

        # Anthropic 不支援 empty content block，如果都沒東西補個空白文字
        if not content_blocks:
            content_blocks.append({"type": "text", "text": ""})

        anthropic_messages.append(AnthropicMessage(role=role_to_use, content=content_blocks))

    # 合併相鄰的相同角色訊息 (Anthropic 不允許連續的 user 或 assistant)
    merged_messages = _merge_adjacent_roles(anthropic_messages)

    # 處理 Tools 轉換 (OpenAI tools -> Anthropic tools；SSOT: tool_schema)
    anthropic_tools = None
    if request.tools:
        anthropic_tools = tool_schema.openai_to_anthropic_tools(request.tools)

        # [斷點 1] Tools 的最後一項加上 cache_control，以快取 System Prompt 與所有 Tools
        # （openai_to_anthropic_tools 無有效項時回 None，故以 truthy 判斷）
        if anthropic_tools:
            anthropic_tools[-1]["cache_control"] = {"type": "ephemeral"}

    # [斷點 2] 找出倒數一個 User 的訊息並打上 cache_control (保留 1 個配額給 ReAct Tool Results)；
    # 打在請求時才附在尾端的區塊之前，下一輪的歷史前綴才對得上
    user_indices = [i for i, msg in enumerate(merged_messages) if msg.role == "user"]
    if user_indices:
        idx = user_indices[-1]
        content = merged_messages[idx].content
        if isinstance(content, list) and len(content) > 0:
            _mark_history_breakpoint(content)

    # 模型名只轉小寫、去掉 -thinking 尾碼，不再改寫成別的模型；思考方式與 max_tokens
    # 讀每模型規則表（ADR-0247；含 budget 思考的最小值與非串流上限）。
    requested_model = request.model.lower()
    model_name = requested_model.removesuffix(_THINKING_SUFFIX)
    rules = claude_rules(model_name)
    thinking_config = None
    thinking_budget = None
    if requested_model.endswith(_THINKING_SUFFIX):
        thinking_type = "enabled" if rules.thinking == "budget" else "adaptive"
        thinking_config = AnthropicThinking(type=thinking_type)  # budget 用 schema 預設
        if thinking_type == "enabled":
            thinking_budget = thinking_config.budget_tokens

    # system 以字串交給 adapter（KF-5）：和原生字串入口一樣由 adapter 重排並切出穩定前綴
    # 加斷點（ADR-0129／0130）；先包成 list 會讓切分不作用。
    system_prompt = "\n".join(system_texts) or None

    return AnthropicRequest(
        model=model_name,
        max_tokens=claude_send_max_tokens(model_name, request.max_tokens, thinking_budget),
        messages=merged_messages,
        system=system_prompt,
        tools=anthropic_tools,
        # ADR-0111：自來源請求轉發取樣參數（先前硬寫 temperature=1.0、漏 top_p/top_k，
        # 導致 ModelStudio/原生模型對 Claude 設的參數於轉譯期靜默丟失）。
        temperature=request.temperature if request.temperature is not None else 1.0,
        top_p=request.top_p,
        top_k=request.top_k,
        thinking=thinking_config,
    )


def claude_stop_sequences(stop: str | list[str] | None) -> list[str] | None:
    """OpenAI ``stop`` → Claude ``stop_sequences``（r55；沒剩任何一項＝None，不送）。

    字串轉成一項；丟掉非字串、空字串與純空白（官方 OpenAI 相容頁：只有非空白的停止字串有效）；
    保序去重。
    """
    items = [stop] if isinstance(stop, str) else stop if isinstance(stop, list) else []
    kept = [item for item in items if isinstance(item, str) and item.strip()]
    return list(dict.fromkeys(kept)) or None


def _split_request_tail(text: Any) -> tuple[str, str]:
    """切出文字尾端連續的請求時區塊：回 (前段, 尾段)。

    每塊是以空行接上的 ``<tag>…</tag>``，tag 屬 REQUEST_TAIL_TAGS；只認貼在最尾端的。
    """
    if not isinstance(text, str):
        return "", ""
    cut = len(text)
    while True:
        stem = text[:cut]
        starts = [stem.rfind(f"\n\n<{t}>") for t in REQUEST_TAIL_TAGS if stem.endswith(f"</{t}>")]
        if max(starts, default=-1) < 0:
            return text[:cut], text[cut:]
        cut = max(starts)


def _is_request_tail(block: dict[str, Any]) -> bool:
    """整塊都是請求時尾端區塊（多模態內容裡 hook 輸出、continuity 各自是一塊）。"""
    if block.get("type") != "text":
        return False
    head, tail = _split_request_tail(block.get("text"))
    return bool(tail) and not head


def _mark_history_breakpoint(blocks: list[dict[str, Any]]) -> None:
    """斷點打在請求時尾端區塊之前的最後一塊；前面沒有別塊就不打。

    文字尾端的請求時區塊先拆成獨立區塊；整塊都是尾端的（多模態內容裡的 hook 輸出、continuity）
    往前跳過。
    """
    last = blocks[-1]
    head, tail = _split_request_tail(last.get("text")) if last.get("type") == "text" else ("", "")
    if tail and head:
        last["text"] = head
        blocks.append({"type": "text", "text": tail})
    target = len(blocks) - 1
    while target >= 0 and _is_request_tail(blocks[target]):
        target -= 1
    if target >= 0:
        blocks[target]["cache_control"] = {"type": "ephemeral"}


def _merge_adjacent_roles(messages: list[AnthropicMessage]) -> list[AnthropicMessage]:
    """將相鄰且同為 user 或 assistant 的 message content 合併"""
    if not messages:
        return []

    merged = [messages[0]]
    for msg in messages[1:]:
        last_msg = merged[-1]
        if msg.role == last_msg.role:
            if isinstance(last_msg.content, list) and isinstance(msg.content, list):
                last_msg.content.extend(msg.content)
            elif isinstance(last_msg.content, str) and isinstance(msg.content, str):
                last_msg.content += "\n" + msg.content
            # 其他情況依需求實作轉換
        else:
            merged.append(msg)
    return merged


def _parse_image_data_uri(url: str) -> tuple[str, str] | None:
    """解析 base64 data URI → ``(media_type, base64_data)``；無法解析時回 ``None``。

    形如 ``data:<media_type>[;param...];base64,<data>``。Anthropic 的 inline 影像來源
    僅支援 base64，故未含 ``;base64`` 參數的 data URI 一律不轉換（回 ``None``）。
    缺 media_type 時預設 ``image/jpeg``（對齊既有 base64→image 慣例）。
    """
    header, sep, data = url.partition(",")
    if not sep or not data:
        return None
    meta = header[len("data:"):]
    parts = meta.split(";")
    if "base64" not in [p.strip().lower() for p in parts[1:]]:
        return None
    media_type = parts[0].strip() or "image/jpeg"
    return media_type, data


def _openai_image_block_to_anthropic(block: dict[str, Any]) -> dict[str, Any] | None:
    """將 OpenAI ``image_url`` 內容 block 轉為 Anthropic image block（OpenAI→Anthropic 影像 SSOT）。

    - data URI(base64)：``{"type":"image","source":{"type":"base64","media_type":...,"data":...}}``
    - http(s) URL：``{"type":"image","source":{"type":"url","url":...}}``
    - 無法解析/不支援：``None``（呼叫端 best-effort 略過）。

    純函式、不拋例外、不產生副作用（SRP）。容忍 ``image_url`` 為物件 ``{"url": ...}``
    或裸字串兩種輸入形態。
    """
    raw = block.get("image_url")
    if isinstance(raw, dict):
        url = raw.get("url")
    elif isinstance(raw, str):
        url = raw
    else:
        url = None

    if not isinstance(url, str) or not url:
        return None

    if url.startswith("data:"):
        parsed = _parse_image_data_uri(url)
        if parsed is None:
            return None
        media_type, data = parsed
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }

    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}

    return None
