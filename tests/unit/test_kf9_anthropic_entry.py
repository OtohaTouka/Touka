"""3b-U5b：Anthropic 形狀入口接非 Claude 模型時保留工具紀錄（KF-9；ADR-0248 決定 A12）。

（ADR-0248；KF-9 見 ADR-0242。）

IDE 用 Claude 格式（``/v1/messages``）接 Grok、Gemini、OpenAI 等模型時，``convert_to_openai``
把 ``tool_use``／``tool_result`` 轉成 OpenAI 的 ``tool_calls``／``role=tool`` 訊息（call id 就是
tool_use id），list 形狀的 system 合成字串（``cache_control`` 不外送）。Gemini 與 Responses 再由
各自的轉換器接手，所以 functionCall／functionResponse、function_call／function_call_output 也
一一對上。``is_error`` 的結果在內容前加「Tool error: 」（兩家都沒有對應欄位）。

對話涵蓋：平行兩個工具呼叫、其中一個出錯、工具結果後面接使用者文字、第二輪工具。全是純函式。
"""

from __future__ import annotations

import logging
from typing import Any

from src.adapters.openai_to_gemini import convert_openai_to_gemini
from src.adapters.schemas import AnthropicRequest
from src.handlers.adapters.responses_adapter import _input_items
from src.services.dispatchers.utils import convert_to_openai

_SYSTEM = [{"type": "text", "text": "妳是 Sumi。", "cache_control": {"type": "ephemeral"}},
           {"type": "text", "text": "用繁體中文回答。"}]
_SYSTEM_TEXT = "妳是 Sumi。\n用繁體中文回答。"
_IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}


def _use(call_id: str, name: str, args: dict) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}


def _result(call_id: str, content: Any, **extra: Any) -> dict:
    return {"type": "tool_result", "tool_use_id": call_id, "content": content, **extra}


def _conversation() -> AnthropicRequest:
    return AnthropicRequest(model="grok-4", system=_SYSTEM, messages=[
        {"role": "user", "content": "查兩個檔"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "我查查"},
            _use("toolu_1", "vault_tools", {"action": "list"}),
            _use("toolu_2", "vault_tools", {"action": "read", "path": "a.txt"}),
        ]},
        {"role": "user", "content": [
            _result("toolu_1", "a.txt"),
            _result("toolu_2", [{"type": "text", "text": "找不到"}], is_error=True),
            {"type": "text", "text": "再查天氣"},
        ]},
        {"role": "assistant", "content": [_use("toolu_3", "web_search", {"q": "天氣"})]},
        {"role": "user", "content": [_result("toolu_3", [{"type": "text", "text": "晴"}, _IMAGE])]},
    ])


def _call(call_id: str, name: str, arguments: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


_LIST, _READ, _SEARCH = '{"action": "list"}', '{"action": "read", "path": "a.txt"}', '{"q": "天氣"}'


def _messages(degraded: bool = False) -> list[dict]:
    request = convert_to_openai(_conversation(), degraded, "記憶離線")
    return [message.model_dump(exclude_none=True) for message in request.messages]


def test_tool_blocks_become_tool_calls_and_tool_messages(caplog):
    with caplog.at_level(logging.WARNING):
        messages = _messages()
    assert messages == [
        {"role": "system", "content": _SYSTEM_TEXT},
        {"role": "user", "content": "查兩個檔"},
        {"role": "assistant", "content": "我查查", "tool_calls": [
            _call("toolu_1", "vault_tools", _LIST), _call("toolu_2", "vault_tools", _READ)]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "a.txt"},
        {"role": "tool", "tool_call_id": "toolu_2", "content": "Tool error: 找不到"},
        {"role": "user", "content": [{"type": "text", "text": "再查天氣"}]},
        {"role": "assistant", "tool_calls": [_call("toolu_3", "web_search", _SEARCH)]},
        {"role": "tool", "tool_call_id": "toolu_3", "content": "晴"},
    ]
    assert "tool_result 裡不支援的區塊已略過：image" in caplog.text  # 不靜默丟棄


def test_degraded_warning_follows_the_system_text():
    assert _messages(degraded=True)[0] == {
        "role": "system", "content": f"{_SYSTEM_TEXT}\n\n⚠️ SYSTEM WARNING: 記憶離線"}


def test_gemini_pairs_function_calls_with_their_responses():
    payload = convert_openai_to_gemini(convert_to_openai(_conversation(), False, ""))
    assert payload["system_instruction"] == {"parts": [{"text": _SYSTEM_TEXT}]}

    def response(name: str, result: str) -> dict:
        return {"functionResponse": {"name": name, "response": {"result": result}}}

    assert payload["contents"] == [
        {"role": "user", "parts": [{"text": "查兩個檔"}]},
        {"role": "model", "parts": [
            {"text": "我查查"},
            {"functionCall": {"name": "vault_tools", "args": {"action": "list"}}},
            {"functionCall": {"name": "vault_tools", "args": {"action": "read", "path": "a.txt"}}},
        ]},
        {"role": "user", "parts": [
            response("vault_tools", "a.txt"), response("vault_tools", "Tool error: 找不到"),
            {"text": "再查天氣"},
        ]},
        {"role": "model", "parts": [
            {"functionCall": {"name": "web_search", "args": {"q": "天氣"}}}]},
        {"role": "user", "parts": [response("web_search", "晴")]},
    ]


def test_responses_answers_each_call_by_its_id():
    instructions, items = _input_items(_messages())

    def call(call_id: str, name: str, arguments: str) -> dict:
        return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}

    def output(call_id: str, text: str) -> dict:
        return {"type": "function_call_output", "call_id": call_id, "output": text}

    assert instructions == _SYSTEM_TEXT
    assert items == [
        {"role": "user", "content": "查兩個檔"},
        {"role": "assistant", "content": "我查查"},
        call("toolu_1", "vault_tools", _LIST), call("toolu_2", "vault_tools", _READ),
        output("toolu_1", "a.txt"), output("toolu_2", "Tool error: 找不到"),
        {"role": "user", "content": [{"type": "input_text", "text": "再查天氣"}]},
        call("toolu_3", "web_search", _SEARCH), output("toolu_3", "晴"),
    ]


def test_given_max_tokens_is_carried_over():
    """O20：呼叫端給的 max_tokens 跟著轉過去（IDE／Chat 的 Claude 格式都帶 4096）。"""
    body = {"model": "gemini-2.5-flash", "max_tokens": 4096, "messages": [
        {"role": "user", "content": "hi"}]}
    request = convert_to_openai(AnthropicRequest.model_validate(body), False, "")
    assert request.max_tokens == 4096
    assert convert_openai_to_gemini(request)["generationConfig"] == {"maxOutputTokens": 4096}


def test_missing_max_tokens_stays_unset():
    """沒給 max_tokens 時照舊不帶（不把 AnthropicRequest 的預設 8192 當成呼叫端的值）。"""
    body = {"model": "grok-4", "messages": [{"role": "user", "content": "hi"}]}
    request = convert_to_openai(AnthropicRequest.model_validate(body), False, "")
    assert request.max_tokens is None
    assert "generationConfig" not in convert_openai_to_gemini(request)


def test_messages_without_tool_blocks_are_unchanged():
    request = convert_to_openai(AnthropicRequest(model="grok-4", system="妳是 Sumi。", messages=[
        {"role": "user", "content": [{"type": "text", "text": "看圖"}, _IMAGE]},
        {"role": "assistant", "content": [{"type": "text", "text": "好"}]},
        {"role": "user", "content": "謝謝"},
    ]), False, "")
    assert [message.model_dump(exclude_none=True) for message in request.messages] == [
        {"role": "system", "content": "妳是 Sumi。"},
        {"role": "user", "content": [{"type": "text", "text": "看圖"}, _IMAGE]},
        {"role": "assistant", "content": [{"type": "text", "text": "好"}]},
        {"role": "user", "content": "謝謝"},
    ]
