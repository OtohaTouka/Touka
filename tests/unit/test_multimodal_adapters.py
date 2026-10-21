"""Phase 3：多模態圖片端到端（從 ToukaChat 實際送出的「JSON 字串 content」）。

修復前：前端 ``buildApiMessage`` 以 ``JSON.stringify([...])`` 夾帶多模態，但 adapter 期望
真實 list → 整串 JSON（含 base64）被當文字、圖片靜默失效。修復後：normalize_message_content
於 adapter 入口還原為 list，Claude 走既有 image block、Gemini 新增 inline_data。

純函式測試，不觸碰任何 agent 資料/磁碟。
"""
from __future__ import annotations

import json

from src.adapters.openai_to_claude import convert_openai_to_anthropic
from src.adapters.openai_to_gemini import convert_openai_to_gemini
from src.adapters.schemas import OpenAIChatRequest

_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _stringified_image_msg() -> str:
    """前端 buildApiMessage 對圖片產出的 content（JSON 字串）。"""
    return json.dumps(
        [
            {"type": "text", "text": "這是什麼？"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_PNG_B64}"}},
        ]
    )


def test_claude_preserves_image_from_stringified_content():
    req = OpenAIChatRequest(
        model="claude-4-6",
        messages=[{"role": "user", "content": _stringified_image_msg()}],
    )
    out = convert_openai_to_anthropic(req)
    blocks = [m for m in out.messages if m.role == "user"][-1].content
    types = [b.get("type") for b in blocks]
    assert "text" in types
    assert "image" in types  # 修復前：整串被當 text，無 image
    img = next(b for b in blocks if b.get("type") == "image")
    assert img["source"] == {"type": "base64", "media_type": "image/png", "data": _PNG_B64}


def test_gemini_preserves_image_from_stringified_content():
    req = OpenAIChatRequest(
        model="gemini-2.5-pro",
        messages=[{"role": "user", "content": _stringified_image_msg()}],
    )
    g = convert_openai_to_gemini(req)
    parts = g["contents"][-1]["parts"]
    assert any("text" in p for p in parts)
    inline = [p for p in parts if "inline_data" in p]
    assert inline, "Gemini 應含 inline_data 圖片（修復前被丟棄）"
    assert inline[0]["inline_data"] == {"mime_type": "image/png", "data": _PNG_B64}


def test_gemini_real_list_content_also_works():
    # 外部 OpenAI 相容客戶端直接送真實 list（非字串）也須正確。
    jpeg_uri = f"data:image/jpeg;base64,{_PNG_B64}"
    req = OpenAIChatRequest(
        model="gemini-2.5-pro",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "image_url", "image_url": {"url": jpeg_uri}},
                ],
            }
        ],
    )
    g = convert_openai_to_gemini(req)
    parts = g["contents"][-1]["parts"]
    assert any(p.get("inline_data", {}).get("mime_type") == "image/jpeg" for p in parts)


def test_gemini_drops_remote_url_gracefully_but_keeps_text():
    # 遠端 http URL 無法走 inline_data → 略過但不崩潰、文字保留。
    req = OpenAIChatRequest(
        model="gemini-2.5-pro",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
                ],
            }
        ],
    )
    g = convert_openai_to_gemini(req)
    parts = g["contents"][-1]["parts"]
    assert any(p.get("text") == "describe" for p in parts)
    assert not any("inline_data" in p for p in parts)


def test_gemini_plain_text_unaffected():
    req = OpenAIChatRequest(
        model="gemini-2.5-pro",
        messages=[{"role": "user", "content": "just text"}],
    )
    g = convert_openai_to_gemini(req)
    parts = g["contents"][-1]["parts"]
    assert parts == [{"text": "just text"}]


# ── 複審回歸：role=="tool" 內容**不得**被正規化（否則 str(list) 毀損工具結果）──────────
_TOOL_JSON = json.dumps([{"type": "text", "text": "result"}])  # 工具剛好回 text-block JSON


def test_claude_tool_result_not_normalized():
    req = OpenAIChatRequest(
        model="claude-4-6",
        messages=[
            {"role": "user", "content": "run"},
            {"role": "tool", "tool_call_id": "c1", "content": _TOOL_JSON},
        ],
    )
    out = convert_openai_to_anthropic(req)
    results = [
        b
        for m in out.messages
        if isinstance(m.content, list)
        for b in m.content
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert results, "應有 tool_result block"
    # 原始 JSON 字串（雙引號）保留——若被誤正規化成 list 再 str() 會變 Python repr（單引號）。
    assert results[0]["content"] == _TOOL_JSON


def test_gemini_tool_result_not_normalized():
    req = OpenAIChatRequest(
        model="gemini-2.5-pro",
        messages=[{"role": "tool", "tool_call_id": "c1", "content": _TOOL_JSON}],
    )
    g = convert_openai_to_gemini(req)
    parts = g["contents"][-1]["parts"]
    fr = next(p for p in parts if "functionResponse" in p)
    assert fr["functionResponse"]["response"]["result"] == _TOOL_JSON
