"""Gemini 上游請求契約（model-p1a U3，「甲＋全停」）。

凍結 GeminiAdapter 經 run_react_loop 每一輪交給傳輸層的 URL 與 payload：模型 ID 對照、
轉換器、外部工具歷史、Anthropic 形狀入口、無快取時的逐輪 contents 與 tool_config。
Gemini 顯式快取已全停（ADR-0243），KF-1／KF-2／KF-2d 隨之消失；不帶 cachedContent、
每輪都帶 system_instruction／tools 由 test_gemini_no_explicit_cache.py 專責。
"""

from __future__ import annotations

import pytest
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest
from src.core.exceptions import ErrorCode, ToukaException
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.services.dispatchers.utils import convert_to_openai

from .harness import drive, gemini_text, gemini_tools, shape

_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
_TOOL = {"type": "function", "function": {
    "name": "vault_tools", "description": "檔案",
    "parameters": {"type": "object", "properties": {"action": {"type": "string"}}},
}}
_CORRECTION = (
    "Warning: You mentioned memories or file contents without actually using tools. "
    "Verify first — query memory with `memory_tools` (action=\"query\") "
    "or read files with `read`/`grep` — then answer honestly."
)
_WRAP_UP = (
    "（系統）本次行動的工具輪額度已用盡，最後一輪的工具調用未執行。"
    "請根據目前已取得的結果直接總結收尾，不要再嘗試調用工具。"
)


def _ui(messages=None, model="gemini-2.5-flash", **extra) -> OpenAIChatRequest:
    return OpenAIChatRequest(model=model, tools=[_TOOL], **extra, messages=messages or [
        {"role": "system", "content": "妳是 Sumi。"},
        {"role": "user", "content": "查一下"},
    ])


async def _run(monkeypatch, request, responses, tag, **ctx_extra):
    return await drive(monkeypatch, GeminiAdapter(), request, responses,
                       tag=f"gemini-{tag}", ctx_extra=ctx_extra or None)


def _text(role: str, text: str) -> dict:
    return {"role": role, "parts": [{"text": text}]}


@pytest.mark.parametrize(("requested", "sent"), [
    ("gemini-2.5-flash", "gemini-2.5-flash"),
    ("Gemini-2.5-Pro", "gemini-2.5-pro"),  # 只轉小寫：同一型號的正規化，不是改寫（R-3a）
    ("Models/GEMINI-3-Flash-Preview", "gemini-3-flash-preview"),
    ("gemini-2.5-flash-lite", "gemini-2.5-flash-lite"),
    ("gemini-2.5-flash-image", "gemini-2.5-flash-image"),
    ("gemini-2.0-flash", "gemini-2.0-flash"),
    ("gemini-2.0-flash-thinking-exp", "gemini-2.0-flash-thinking-exp"),
    ("gemini-1.5-pro", "gemini-1.5-pro"),
    ("gemini-2.5-pro-exp-03-25", "gemini-2.5-pro-exp-03-25"),
    ("gemini-3-pro-preview", "gemini-3-pro-preview"),
    ("gemini-3-pro-image-preview", "gemini-3-pro-image-preview"),
    ("gemini-3-flash-preview", "gemini-3-flash-preview"),
    ("gemini-3-deep-think", "gemini-3-deep-think"),
    ("gemini-3.1-pro-preview", "gemini-3.1-pro-preview"),
    ("gemini-3.1-flash", "gemini-3.1-flash"),
    ("gemini-3.1-flash-lite-preview", "gemini-3.1-flash-lite-preview"),
    ("gemini-flash-latest", "gemini-flash-latest"),
    ("gemini-pro-latest", "gemini-pro-latest"),
    ("gemini-exp-1206", "gemini-exp-1206"),  # 以前認不得而報錯，現在照送
    ("gemma-3-27b-it", "gemma-3-27b-it"),
    ("models/gemini-2.5-flash", "gemini-2.5-flash"),  # 只去 models/ 前綴
])
async def test_kf7b_model_id_rewrite_table(monkeypatch, requested, sent):
    """已知故障 KF-7b，見 ADR-0242；3a 已修正（ADR-0247，決定 A13）。

    請求的模型名進 URL 前只去 ``models/`` 前綴並轉小寫，不再經關鍵字比對換成別的模型。"""
    run = await _run(monkeypatch, _ui(model=requested), [gemini_text("好")], "map")
    assert run.snapshots[0]["url"] == _URL.format(sent)
    assert "".join(run.chunks).count(f'"model": "{sent}"') >= 1


@pytest.mark.parametrize("requested", [
    "", "gemini", "Gemini", "models/", "models/gemini", "gemini-", "gemini 2.5 flash",
    "gemini-2.5-flash/../x", "gemini-2.5-flash:generateContent", "gemini-2.5-flash?alt=sse",
])
async def test_invalid_model_name_raises_readable_error(monkeypatch, requested):
    """空名、只有家族名（舊簡稱，D16）或含 URL 路徑字元：不送出，回看得懂的結構化錯誤。"""
    with pytest.raises(ToukaException) as exc:
        await _run(monkeypatch, _ui(model=requested), [], "invalid")
    assert exc.value.code == ErrorCode.MODEL_NOT_FOUND
    assert exc.value.detail["message"] == (
        f"Invalid Gemini model name: '{requested}'. "
        "Use the full model ID from the model list, e.g. gemini-2.5-flash."
    )


async def test_converter_merges_strips_schema_and_drops_sampling(monkeypatch):
    """多則 system 合併、同角色合併、schema 大寫並剝不支援的鍵、safety off、不送取樣參數；
    generationConfig 只帶輸出上限與停止字串（3b-U3）。"""
    tool = {"type": "function", "function": {"name": "vault_tools", "parameters": {
        "type": "object", "additionalProperties": False, "oneOf": [{"required": ["action"]}],
        "properties": {
            "action": {"type": "string", "const": "list"},
            "paths": {"type": "array", "items": {"type": "string", "anyOf": [{"minLength": 1}]}},
            "opts": {"type": "object", "allOf": [{}], "properties": {"deep": {"type": "boolean"}}},
        },
    }}}
    request = OpenAIChatRequest(
        model="gemini-2.5-flash", tools=[tool], temperature=0.3, top_p=0.9, top_k=40,
        max_tokens=123, stop=["END"], safety_mode="off", session_id="s1", project_id="p1",
        messages=[
            {"role": "system", "content": "第一則"}, {"role": "system", "content": "第二則"},
            {"role": "user", "content": "一"}, {"role": "user", "content": "二"},
            {"role": "assistant", "content": "好"}, {"role": "user", "content": "三"},
        ])
    run = await _run(monkeypatch, request, [gemini_text("好")], "convert")
    assert run.snapshots[0]["payload"] == {
        "contents": [
            {"role": "user", "parts": [{"text": "一"}, {"text": "二"}]},
            _text("model", "好"), _text("user", "三"),
        ],
        "system_instruction": {"parts": [{"text": "第一則\n第二則"}]},
        "tools": [{"functionDeclarations": [{
            "name": "vault_tools", "description": "", "parameters": {
                "type": "OBJECT", "properties": {
                    "action": {"type": "STRING"},
                    "paths": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "opts": {"type": "OBJECT", "properties": {"deep": {"type": "BOOLEAN"}}},
                }},
        }]}],
        "safetySettings": [{"category": f"HARM_CATEGORY_{c}", "threshold": "BLOCK_NONE"} for c in (
            "HARASSMENT", "HATE_SPEECH", "SEXUALLY_EXPLICIT", "DANGEROUS_CONTENT")],
        "generationConfig": {"maxOutputTokens": 123, "stopSequences": ["END"]},
    }


async def test_kf7a_external_tool_history_uses_tool_name(monkeypatch):
    """KF-7a 已修（3b-U3，見 ADR-0242）：外部客戶端帶 role=tool 的歷史時，functionResponse.name
    填對應 functionCall 的工具名，不再填 call id（和 Touka 迴圈內寫回的名稱一致）。"""
    request = _ui([
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_abc", "type": "function",
            "function": {"name": "vault_tools", "arguments": '{"action": "list"}'},
        }]},
        {"role": "tool", "tool_call_id": "call_abc", "content": "a.txt"},
        {"role": "user", "content": "繼續"},
    ])
    run = await _run(monkeypatch, request, [gemini_text("好")], "kf7a")
    assert run.snapshots[0]["payload"]["contents"] == [
        _text("user", "查一下"),
        {"role": "model", "parts": [
            {"functionCall": {"name": "vault_tools", "args": {"action": "list"}}},
        ]},
        {"role": "user", "parts": [  # tool 結果與下一則 user 合併成同一則
            {"functionResponse": {"name": "vault_tools", "response": {"result": "a.txt"}}},
            {"text": "繼續"},
        ]},
    ]


async def test_kf9_anthropic_entry_keeps_tool_blocks_on_gemini(monkeypatch):
    """KF-9 已修正（ADR-0248，3b-U5b）。

    原 ``test_kf9_anthropic_entry_loses_tool_blocks_on_gemini``。
    Anthropic 形狀入口接 Gemini：``convert_to_openai`` 先把 tool_use／tool_result 轉成 OpenAI 的
    tool_calls／tool 訊息，Gemini 轉換器再轉成 functionCall／functionResponse（name 是工具名）；
    list 形狀的 system 合成字串，system_instruction 帶得過去。"""
    native = AnthropicRequest(
        model="gemini-2.5-flash", system=[{"type": "text", "text": "妳是 Sumi。"}],
        tools=[{"name": "vault_tools", "description": "檔案", "input_schema": {"type": "object"}}],
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "查一下"}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "我查查"},
                {"type": "tool_use", "id": "toolu_1", "name": "vault_tools",
                 "input": {"action": "list"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"},
            ]},
        ])
    run = await _run(monkeypatch, convert_to_openai(native, False, ""), [gemini_text("好")], "kf9")
    payload = run.snapshots[0]["payload"]
    assert sorted(payload) == ["contents", "system_instruction", "tools"]
    assert payload["system_instruction"] == {"parts": [{"text": "妳是 Sumi。"}]}
    assert payload["contents"] == [
        _text("user", "查一下"),
        {"role": "model", "parts": [
            {"text": "我查查"},
            {"functionCall": {"name": "vault_tools", "args": {"action": "list"}}},
        ]},
        {"role": "user", "parts": [
            {"functionResponse": {"name": "vault_tools", "response": {"result": "a.txt"}}},
        ]},
    ]


async def test_no_cache_contents_accumulate_past_recache_threshold(monkeypatch):
    """無快取：每個工具輪 contents 加 2；超過 10 則（舊的重建門檻）也照樣累加、較早的
    對話都在、保留 system_instruction／tools，從不帶 cachedContent 或 tool_config。"""
    history = [{"role": "system", "content": "妳是 Sumi。"}, {"role": "user", "content": "前情"},
               {"role": "assistant", "content": "好"}, {"role": "user", "content": "查一下"}]
    tool_rounds = [gemini_tools(("vault_tools", {"action": str(i)})) for i in range(5)]
    run = await _run(monkeypatch, _ui(history), [*tool_rounds, gemini_text("查好了。")], "accum")
    shapes = [shape(s) for s in run.snapshots]
    assert [len(s["contents"]) for s in shapes] == [3, 5, 7, 9, 11, 13]
    assert {(tuple(s["keys"]), s["tool_config"]) for s in shapes} == {
        (("contents", "system_instruction", "tools"), None)}
    assert shapes[-1]["contents"] == ["user:text", "model:text", "user:text"] + [
        "model:functionCall", "user:functionResponse"] * 5
    last = run.snapshots[-1]["payload"]
    assert last["contents"][:3] == [_text("user", "前情"), _text("model", "好"),
                                    _text("user", "查一下")]
    assert last["system_instruction"] == {"parts": [{"text": "妳是 Sumi。"}]}


async def test_no_cache_honesty_correction_forces_any_then_auto(monkeypatch):
    run = await _run(monkeypatch, _ui(), [
        gemini_text("根據過去的記憶系統，你喜歡紅茶。"),
        gemini_tools(("vault_tools", {"action": "read"})),
        gemini_text("查到了。"),
    ], "honesty")
    shapes = [shape(s) for s in run.snapshots]
    assert [s["tool_config"] for s in shapes] == [None, "ANY", "AUTO"]
    assert [s["contents"] for s in shapes] == [
        ["user:text"],
        ["user:text", "model:text", "user:text"],
        ["user:text", "model:text", "user:text", "model:functionCall", "user:functionResponse"],
    ]
    assert run.snapshots[1]["payload"]["contents"][-1] == _text("user", _CORRECTION)
    assert "誠實度警報" in "".join(run.chunks)


async def test_no_cache_wrap_up_round_sets_none(monkeypatch):
    """額度耗盡：收尾輪 tool_config NONE＋denied functionResponse＋WRAP_UP 文字（同一則 user）。"""
    run = await _run(monkeypatch, _ui(), [
        gemini_tools(("vault_tools", {"action": "list"})),
        gemini_tools(("vault_tools", {"action": "read"})),
        gemini_text("總結。"),
    ], "wrap", enable_autonomy=True, pulse_rounds=1)
    assert [shape(s)["tool_config"] for s in run.snapshots] == [None, None, "NONE"]
    assert run.snapshots[2]["payload"]["contents"][-2:] == [
        {"role": "model", "parts": [
            {"functionCall": {"name": "vault_tools", "args": {"action": "read"}}},
        ]},
        {"role": "user", "parts": [
            {"functionResponse": {"name": "vault_tools",
                                  "response": {"result": "（未執行：工具輪額度已盡（1 輪））"}}},
            {"text": _WRAP_UP},
        ]},
    ]
