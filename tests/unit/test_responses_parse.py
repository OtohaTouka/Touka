"""3b-U4b-1：OpenAI Responses 的非串流送出與回應解析（ADR-0248 D1／D5；ADR-0244）。

上游是真的 openai SDK＋httpx2.MockTransport，回非串流的 Response JSON（D1：adapter 預設非串流、
只送 1 次，送出內容沒有 ``stream``）。串流原始事件的彙整、逾時與用量估算在
test_responses_stream.py。

- 解析：reasoning 摘要走思考頻道、function_call 用 call_id（fixture 的 item id 與 call_id 不同）、
  output items 原樣留給下一輪（含加密 reasoning）。
- 用量扣 cached_tokens；停止原因：completed＝end、拒答 part＝refusal、incomplete 依原因對應
  截斷／安全過濾、表外原因＝error。
- 缺金鑰、缺 base URL、400 的錯誤文字直接字串釘住；誠實守衛；終端 chunk；Protocol 成員齊全。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx2
import openai
import pytest
from src.adapters.schemas import OpenAIChatRequest
from src.handlers.adapters import responses_adapter
from src.handlers.adapters.base import ProviderAdapter, RoundError, ToolCall, Turn
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.services.guard_service import guard_service
from src.services.models.model_rules import rules_for

CTX = {"provider": "OpenAI", "base_url": "https://api.openai.test/v1", "api_key": "fake-key"}
USAGE = {"input_tokens": 1200, "input_tokens_details": {"cached_tokens": 1024},
         "output_tokens": 40, "output_tokens_details": {"reasoning_tokens": 12},
         "total_tokens": 1240}
BASE = {"id": "resp_1", "object": "response", "created_at": 1, "model": "gpt-6-astra",
        "status": "in_progress", "output": [], "parallel_tool_calls": True,
        "tool_choice": "auto", "tools": [], "store": False}
MSG = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
       "content": [{"type": "output_text", "text": "檔案說 hi", "annotations": []}]}
REASONING = {"type": "reasoning", "id": "rs_1", "encrypted_content": "gAAAAABfake==",
             "summary": [{"type": "summary_text", "text": "先讀檔"}]}
CALL1 = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "vault_tools",
         "arguments": '{"action":"list"}', "status": "completed"}
CALL2 = {"type": "function_call", "id": "fc_2", "call_id": "call_2", "name": "memory_tools",
         "arguments": '{"action":"query"}', "status": "completed"}


def final(status: str = "completed", **extra: Any) -> dict:
    return {**BASE, "status": status, **extra}


def request(text: str = "hi", **extra: Any) -> OpenAIChatRequest:
    return OpenAIChatRequest(model="gpt-6-astra", messages=[{"role": "user", "content": text}],
                             **extra)


class _Wire:
    def __init__(self, *replies: httpx2.Response) -> None:
        self.replies = list(replies)
        self.requests: list[httpx2.Request] = []

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.requests.append(req)
        return self.replies.pop(0)

    def install(self, monkeypatch) -> _Wire:
        def http_client(**kwargs: Any) -> Any:
            return openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self), **kwargs)

        monkeypatch.setattr(responses_adapter, "DefaultAsyncHttpxClient", http_client)
        return self


async def _round(monkeypatch, *replies: httpx2.Response,
                 ctx: dict | None = None) -> tuple[Any, _Wire, Any]:
    wire = _Wire(*replies).install(monkeypatch)
    adapter = ResponsesAdapter()  # D1 預設：非串流
    state = await adapter.prepare_request(request(), dict(ctx or CTX))
    try:
        items = [item async for item in adapter.send_round(state)]
    finally:
        await adapter.close(state)
    assert len(items) == 1
    return items[0], wire, state


async def test_text_response_posts_once_without_stream(monkeypatch):
    response = final(output=[MSG], usage=USAGE)
    raw, wire, state = await _round(monkeypatch, httpx2.Response(200, json=response))
    assert raw == response
    assert len(wire.requests) == 1 and "stream" not in json.loads(wire.requests[0].content)
    adapter = ResponsesAdapter()
    turn = adapter.parse_round(state, raw)
    assert (turn.visible_text, turn.text_parts, turn.tool_calls) == ("檔案說 hi", ["檔案說 hi"], [])
    assert adapter.extract_usage(state, raw)[:2] == (
        {"prompt_tokens": 176, "cache_read_input_tokens": 1024,
         "cache_creation_input_tokens": 0, "completion_tokens": 40}, 1200)
    assert adapter.extract_final_text(state, raw) == "檔案說 hi"
    assert adapter.stop_reason(state, raw) == "end"
    assert adapter.partial_usage(state) is None  # 非串流：沒有半截可估


async def test_reasoning_and_two_function_calls(monkeypatch):
    response = final(output=[REASONING, CALL1, CALL2], usage=USAGE)
    raw, _, state = await _round(monkeypatch, httpx2.Response(200, json=response))
    turn = ResponsesAdapter().parse_round(state, raw)
    assert turn.reasoning_parts == ["先讀檔"]
    assert turn.tool_calls == [ToolCall(id="call_1", name="vault_tools", args={"action": "list"}),
                               ToolCall(id="call_2", name="memory_tools", args={"action": "query"})]
    assert turn.raw_assistant == [REASONING, CALL1, CALL2]  # 加密 reasoning 原樣留給下一輪
    assert turn.visible_text == ""


def test_empty_output_is_an_empty_turn():
    assert ResponsesAdapter().parse_round(None, final(usage=USAGE)).empty is True


@pytest.mark.parametrize(("reason", "stop"), [
    ("max_output_tokens", "max_tokens"), ("content_filter", "safety"), ("brand_new", "error"),
    (None, "error")])
def test_incomplete_maps_the_reason(reason, stop):
    partial = {**MSG, "status": "incomplete",
               "content": [{"type": "output_text", "text": "截到一", "annotations": []}]}
    raw = final("incomplete", output=[partial], incomplete_details={"reason": reason})
    adapter = ResponsesAdapter()
    assert (adapter.stop_reason(None, raw), adapter.parse_round(None, raw).visible_text) == (
        stop, "截到一")


def test_refusal_part_maps_to_refusal_and_failed_to_error():
    refusal = {**MSG, "content": [{"type": "refusal", "refusal": "不行"}]}
    adapter = ResponsesAdapter()
    raw = final(output=[refusal])
    assert (adapter.stop_reason(None, raw), adapter.extract_final_text(None, raw)) == (
        "refusal", "")
    assert adapter.stop_reason(None, final("failed")) == "error"


async def test_status_error_text_is_pinned(monkeypatch):
    body = b'{"error": {"message": "Invalid input", "type": "invalid_request_error"}}'
    reply = httpx2.Response(400, content=body, headers={"content-type": "application/json"})
    raw, wire, _ = await _round(monkeypatch, reply)
    assert raw == RoundError('OpenAI API Error (400)\n\n`{"error": {"message": "Invalid input", '
                             '"type": "invalid_request_error"}}`')
    assert len(wire.requests) == 1  # 400 不重試（SDK 自己的重試在 U5a 關掉）


@pytest.mark.parametrize(("missing", "text"), [
    ("api_key", "OpenAI Access Denied (401)\n\nMissing `OPENAI_API_KEY` in `.env`."),
    ("base_url", "❌ **OpenAI 連線錯誤**\n\n`缺少 base URL`"),
])
async def test_setup_error_texts_are_pinned(monkeypatch, missing, text):
    ctx = {k: v for k, v in CTX.items() if k != missing}
    raw, wire, _ = await _round(monkeypatch, ctx=ctx)
    assert raw == RoundError(text)
    assert wire.requests == []


async def test_honesty_guard_forces_then_releases_tool_choice():
    adapter = ResponsesAdapter()
    state = await adapter.prepare_request(request(), {})
    claim = {**MSG, "content": [{"type": "output_text", "annotations": [],
                                 "text": "根據過去的記憶系統內容，你上次說喜歡紅茶。"}]}
    turn = Turn(visible_text=claim["content"][0]["text"], raw_assistant=[claim])
    action = adapter.mid_loop_hook(state, turn, set(), True)
    assert action is not None
    assert action.deltas == [guard_service.get_honesty_warning_message()]
    assert state.kwargs["input"][1:] == [
        claim, {"role": "user", "content": guard_service.get_honesty_correction_prompt()}]
    assert state.kwargs["tool_choice"] == "required"
    assert adapter.mid_loop_hook(state, turn, set(), True) is None  # 一次性閂鎖
    await adapter.append_tool_results(state, [("call_1", "memory_tools", "紅茶")])
    assert state.kwargs["tool_choice"] == "auto"


def test_finalize_shape_and_labels():
    raw = final(output=[MSG], usage=USAGE)
    adapter = ResponsesAdapter()
    assert adapter.finalize(None, raw, "r1", {"total_ms": 5}) == {
        "id": "chatcmpl-r1", "object": "chat.completion.chunk", "created": 1,
        "model": "gpt-6-astra",
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
        "telemetry": {"total_ms": 5}}
    assert adapter.telemetry_model(None, request()) == "gpt-6-astra"
    assert adapter.context_breakdown_args(None) == (None, None, None)
    assert adapter.emits_usage_event(None) is True


async def test_every_protocol_member_is_implemented():
    members = {n for n in vars(ProviderAdapter) if not n.startswith("_")}
    members |= set(ProviderAdapter.__annotations__)
    assert sorted(n for n in members | {"resolve_provider_label"}
                  if not hasattr(ResponsesAdapter, n)) == []
    state = await ResponsesAdapter().prepare_request(request(), {"provider": "OpenAI"})
    assert (state.chunk_model, ResponsesAdapter().resolve_provider_label(state)) == (
        "gpt-6-astra", "OpenAI")


async def test_rules_table_stream_switch_and_forced_tool_choice(monkeypatch):
    """3b-U4b-2：規則表接上——stream 關掉就走非串流（D6），不許強制就只靠矯正提示。"""
    real = rules_for
    monkeypatch.setattr(responses_adapter, "rules_for",
                        lambda m: replace(real(m), stream=False, forced_tool_choice=False))
    adapter = ResponsesAdapter(stream=True)
    state = await adapter.prepare_request(request(), {})
    assert (state.stream, state.forced_tool_choice) == (False, False)
    claim = {**MSG, "content": [{"type": "output_text", "annotations": [],
                                 "text": "根據過去的記憶系統內容，你上次說喜歡紅茶。"}]}
    turn = Turn(visible_text=claim["content"][0]["text"], raw_assistant=[claim])
    assert adapter.mid_loop_hook(state, turn, set(), True) is not None  # 仍注入矯正提示
    assert "tool_choice" not in state.kwargs


async def test_responses_rows_stream_by_default():
    for model in ("gpt-6-astra", "gpt-6.1-sol"):
        req = OpenAIChatRequest(model=model, messages=[{"role": "user", "content": "hi"}])
        state = await ResponsesAdapter(stream=True).prepare_request(req, {})
        assert (rules_for(model).uses_responses_api, state.stream) == (True, True)
