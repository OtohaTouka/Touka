"""OpenAI 請求格式轉為 Gemini generateContent 請求格式之適配器。"""

import copy
import json
import logging
from typing import Any

from .content_normalize import normalize_message_content

logger = logging.getLogger(__name__)


def _image_url_to_gemini_part(block: dict) -> dict | None:
    """OpenAI ``image_url`` block → Gemini ``inline_data`` part（base64 data URI）。

    Gemini 的 inline 影像僅支援 base64（``inline_data``）；遠端 http(s) URL 需經 Files API，
    本 best-effort 不支援故回 ``None``（呼叫端記 log，失效可見化）。純函式、不拋例外。
    """
    raw = block.get("image_url")
    if isinstance(raw, dict):
        url = raw.get("url")
    elif isinstance(raw, str):
        url = raw
    else:
        url = None
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, sep, data = url.partition(",")
    if not sep or not data:
        return None
    meta = header[len("data:") :].split(";")
    if "base64" not in [p.strip().lower() for p in meta[1:]]:
        return None
    mime = meta[0].strip() or "image/jpeg"
    return {"inline_data": {"mime_type": mime, "data": data}}


def convert_openai_to_gemini(request: Any) -> dict:
    """轉換 OpenAI Chat Completion 請求為 Gemini GenerateContent JSON 格式"""
    gemini_req = {"contents": []}
    system_texts = []
    call_names: dict[str, str] = {}  # KF-7a：外部歷史 tool_call_id → 工具名
    for msg in request.messages:
        # 兼容字典與物件格式 (因 SoulLayer 注入的歷史紀錄可能是字典)
        role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
        content = getattr(msg, "content", None) or (
            msg.get("content") if isinstance(msg, dict) else None
        )
        # ToukaChat 夾帶的多模態 JSON 字串 → list。**排除 role=="tool"**：工具結果是任意
        # 文字、下方 tool 分支會 str(content)，若先被還原成 list 會被 str() 轉成 Python
        # repr 而毀損（複審）。
        if role != "tool":
            content = normalize_message_content(content)
        tool_calls = getattr(msg, "tool_calls", None) or (
            msg.get("tool_calls") if isinstance(msg, dict) else None
        )

        if role == "system":
            if isinstance(content, str):
                system_texts.append(content)
            continue
        parts = []
        # 1. 處理普通文字 / 多模態（文字 + 影像）。
        if isinstance(content, str) and content:
            parts.append({"text": content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    parts.append({"text": block.get("text")})
                elif btype == "image_url":
                    # 之前 Gemini 路徑靜默丟棄圖片（只認 text）；改轉 inline_data。
                    img_part = _image_url_to_gemini_part(block)
                    if img_part is not None:
                        parts.append(img_part)
                    else:
                        logger.warning(
                            "[gemini] image_url 無法轉為 inline_data"
                            "（非 base64 data URI 或遠端 URL），已略過。"
                        )
                elif btype:
                    # 失效可見化：未支援的內容區塊型別不靜默丟棄。
                    logger.warning("[gemini] 不支援的內容區塊型別 '%s'，已略過。", btype)
        # 2. 處理 tool_calls (OpenAI tool_calls -> Gemini functionCall)
        if tool_calls:
            for tool_call in tool_calls:
                func = (
                    tool_call.get("function", {})
                    if isinstance(tool_call, dict)
                    else getattr(tool_call, "function", {})
                )
                args_str = (
                    func.get("arguments", "{}")
                    if isinstance(func, dict)
                    else getattr(func, "arguments", "{}")
                )
                try:
                    args = json.loads(args_str)
                except Exception:
                    args = {}
                fn_name = (
                    func.get("name") if isinstance(func, dict) else getattr(func, "name", None)
                )
                call_id = (tool_call.get("id") if isinstance(tool_call, dict)
                           else getattr(tool_call, "id", None))
                if call_id and fn_name:
                    call_names[call_id] = fn_name
                parts.append({"functionCall": {"name": fn_name, "args": args}})
        # 3. 處理 tool回傳結果
        #    (OpenAI role="tool" -> Gemini role="user/model" 必須是 functionResponse)
        if role == "tool":
            tool_call_id = getattr(msg, "tool_call_id", None) or (
                msg.get("tool_call_id") if isinstance(msg, dict) else None
            )
            name = msg.get("name") if isinstance(msg, dict) else getattr(msg, "name", None)
            parts = [
                {
                    "functionResponse": {  # KF-7a：name 是工具名（對應 functionCall），不是 call id
                        "name": call_names.get(tool_call_id)
                        or (name if isinstance(name, str) else None) or tool_call_id,
                        "response": {"result": str(content)},
                    }
                }
            ]
            role_to_use = "user"
        else:
            role_to_use = "user" if role == "user" else "model"
        if not parts:
            parts.append({"text": " "})
        # Gemini 不允許連續的同角色發言，必須合併
        if gemini_req["contents"] and gemini_req["contents"][-1]["role"] == role_to_use:
            gemini_req["contents"][-1]["parts"].extend(parts)
        else:
            gemini_req["contents"].append({"role": role_to_use, "parts": parts})
    # 4. 處理 System Prompt
    if system_texts:
        gemini_req["system_instruction"] = {
            "parts": [{"text": "\n".join(system_texts)}]
        }
    # 5. 處理 Tools
    if request.tools:
        func_decls = []
        for t in request.tools:
            if t.get("type") == "function":
                func = t.get("function", {})
                # Gemini schema requires uppercase types
                schema_copy = copy.deepcopy(
                    func.get("parameters", {"type": "object", "properties": {}})
                )
                _convert_schema_to_gemini(schema_copy)

                func_decls.append(
                    {
                        "name": func.get("name"),
                        "description": func.get("description", ""),
                        "parameters": schema_copy,
                    }
                )

        # Gemini 的 tools 是一個包含 functionDeclarations 的陣列（無有效項則不送空陣列）
        if func_decls:
            gemini_req["tools"] = [{"functionDeclarations": func_decls}]
    # 6. 處理安全性設定 (Gemini & Grok 2026)
    # 根據 2026 規格，Gemini 3.1+ 支援新的 "OFF" 門檻，但 "BLOCK_NONE" 仍為最廣泛相容之設定
    if getattr(request, "safety_settings", None):
        gemini_req["safetySettings"] = request.safety_settings

    # 若有 safety_mode="off"，則自動為 4 大核心分類添加 BLOCK_NONE 以達成最大程度開放
    if getattr(request, "safety_mode", None) == "off" and not gemini_req.get(
        "safetySettings"
    ):
        categories = [
            "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
        ]
        # 在 2026 模式下，BLOCK_NONE 將放行絕大部分內容，僅保留不可關閉的底線保護
        gemini_req["safetySettings"] = [
            {"category": cat, "threshold": "BLOCK_NONE"} for cat in categories
        ]
    # model-p0（Owner 決定 A）：取樣參數一律不送。3b-U3：generationConfig 只放輸出上限與停止字串。
    max_tokens, stop = getattr(request, "max_tokens", None), getattr(request, "stop", None)
    generation = {"maxOutputTokens": max_tokens if type(max_tokens) is int else None,
                  "stopSequences": [stop] if isinstance(stop, str) else
                  stop if isinstance(stop, list) else None}
    if generation := {k: v for k, v in generation.items() if v}:
        gemini_req["generationConfig"] = generation
    # 7. 強制思考模式 (Tool Choice / Forced Tool)
    # 策略：當使用者指令包含特定關鍵字（如研究、更新、確認）時，強制 Gemini 進入
    # REQUIRED 模式，解決其懶惰慣性。
    # 僅在有工具定義時生效，且不影響其他模型（因為這是 Gemini 特有的轉譯邏輯）。

    return gemini_req


def _convert_schema_to_gemini(schema: dict) -> None:
    """遞迴地將 JSON Schema 中的型別轉換為大寫以符合 Gemini OpenAPI 標準，並移除不相容的欄位"""
    if "type" in schema and isinstance(schema["type"], str):
        schema["type"] = schema["type"].upper()

    # 移除 Gemini API 不支持的欄位。Tier 2 起 canonical schema 帶 oneOf 條件必填
    # （action 判別式）——Gemini OpenAPI 子集不支援 oneOf/anyOf/allOf/const，不剝除
    # 會整包 400；剝除＝優雅降級（條件必填說明仍在描述文字中）。
    unsupported_fields = ["additionalProperties", "oneOf", "anyOf", "allOf", "const"]
    for field in unsupported_fields:
        schema.pop(field, None)

    if "properties" in schema:
        for prop_val in schema["properties"].values():
            if isinstance(prop_val, dict):
                _convert_schema_to_gemini(prop_val)
                # 檢查 prop_val 內是否有名為 'value' 的字典，並遞迴處理
                if "value" in prop_val and isinstance(prop_val["value"], dict):
                    _convert_schema_to_gemini(prop_val["value"])

    if "items" in schema and isinstance(schema["items"], dict):
        _convert_schema_to_gemini(schema["items"])
