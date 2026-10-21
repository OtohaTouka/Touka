"""3b-U2：自寫 SSE 解析器與 OpenAI 相容 Chat 彙整器（ADR-0248 附註 14；r2 L12、H1）。

格式案例：CRLF／CR、``:`` 註解行、多行 data、檔尾沒空行、``data:`` 後無空白、BOM、UTF-8 切在
字元中間、CR 與 LF 分在兩段。彙整案例：工具 index 缺號／交錯／整個缺 index／同 index 換 id、
中途 null 不蓋掉已累積內容、只有工具呼叫時 content 是 None、輸出的 tool_calls 不帶 index 且
不掉 type、chunk 帶 ``message`` 拒收。另用 OpenAI 與 DeepSeek 文件的串流範例當獨立 fixture。
3b-U3：Gemini 彙整器（text part 只在 chunk 交界合併、thoughtSignature 放哪裡、未知欄位、完整訊號）
與 Gemini 文件串流範例。期望值全部寫死。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from src.handlers.adapters.sse_stream import (
    ChatStreamAggregator,
    GeminiStreamAggregator,
    StreamShapeError,
    sse_data,
)


async def _parse(*chunks: bytes) -> list[str]:
    async def source() -> Any:
        for chunk in chunks:
            yield chunk

    return [event async for event in sse_data(source())]


# ── 解析器 ──────────────────────────────────────────────────────────────────


async def test_lf_crlf_and_cr_line_endings():
    assert await _parse(b"data: a\n\ndata: b\r\n\r\ndata: c\r\rdata: d\n\n") == ["a", "b", "c", "d"]


async def test_comment_lines_and_other_fields_are_ignored():
    body = b": keep-alive\n\nevent: delta\nid: 7\nretry: 100\ndata: x\n:ping\n\n"
    assert await _parse(body) == ["x"]


async def test_multiline_data_is_joined_with_newlines():
    assert await _parse(b"data: {\"a\":\ndata: 1}\n\n") == ['{"a":\n1}']


async def test_data_without_space_and_field_without_colon():
    assert await _parse(b"data:x\n\ndata\n\n") == ["x", ""]


async def test_last_event_without_trailing_blank_line_is_still_emitted():
    assert await _parse(b"data: a\n\ndata: [DONE]") == ["a", "[DONE]"]
    assert await _parse(b"data: a\n\ndata: b\r") == ["a", "b"]


async def test_bom_at_stream_start_is_stripped():
    assert await _parse(b"\xef\xbb\xbfdata: x\n\n") == ["x"]
    assert await _parse(b"\xef\xbb", b"\xbfdata: x\n\n") == ["x"]  # BOM 被切開也一樣


async def test_utf8_split_inside_a_character():
    body = "data: 紅茶\n\n".encode()
    assert await _parse(body[:7], body[7:]) == ["紅茶"]  # 第 7 個位元組在「紅」的中間


async def test_cr_and_lf_split_across_chunks_is_one_line_end():
    assert await _parse(b"data: a\r", b"\n\r", b"\ndata: b\n\n") == ["a", "b"]
    assert await _parse(b"data: a\r", b"\ndata: b\n\n") == ["a\nb"]  # CRLF 被切開仍是一個行尾


async def test_blank_lines_alone_emit_nothing():
    assert await _parse(b"\n\n\r\n: c\n\n") == []


# ── 彙整器 ──────────────────────────────────────────────────────────────────


def _agg(*chunks: dict) -> dict:
    agg = ChatStreamAggregator()
    for chunk in chunks:
        agg.add(chunk)
    return agg.result()


def _c(delta: dict, **extra: Any) -> dict:
    return {"choices": [{"index": 0, "delta": delta, **extra}]}


def test_text_pieces_and_mid_stream_null():
    result = _agg(_c({"role": "assistant", "content": "紅"}), _c({"content": None}),
                  _c({"content": "茶"}), _c({}, finish_reason="stop"))
    assert result == {"choices": [{"index": 0, "message": {"content": "紅茶", "role": "assistant"},
                                   "finish_reason": "stop"}]}


def test_tool_only_message_has_none_content_and_typed_calls():
    result = _agg(
        _c({"tool_calls": [{"index": 0, "id": "a",
                            "function": {"name": "f", "arguments": '{"x"'}}]}),
        _c({"tool_calls": [{"index": 0, "function": {"arguments": ": 1}"}}]}),
    )
    assert result["choices"][0]["message"] == {"content": None, "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "f", "arguments": '{"x": 1}'}},
    ]}


def test_tool_index_gap_and_interleaving():
    result = _agg(
        _c({"tool_calls": [{"index": 0, "id": "a", "type": "function",
                            "function": {"name": "f", "arguments": "{"}}]}),
        _c({"tool_calls": [{"index": 2, "id": "b", "type": "function",
                            "function": {"name": "g", "arguments": "["}}]}),
        _c({"tool_calls": [{"index": 0, "function": {"arguments": "}"}},
                           {"index": 2, "function": {"arguments": "]"}}]}),
    )
    assert [(c["id"], c["function"]["name"], c["function"]["arguments"])
            for c in result["choices"][0]["message"]["tool_calls"]] == [("a", "f", "{}"),
                                                                        ("b", "g", "[]")]


def test_tool_calls_without_any_index():
    result = _agg(
        _c({"tool_calls": [{"id": "a", "type": "function",
                            "function": {"name": "f", "arguments": "{"}}]}),
        _c({"tool_calls": [{"function": {"arguments": "}"}}]}),
        _c({"tool_calls": [{"id": "b", "type": "function",
                            "function": {"name": "g", "arguments": "{}"}}]}),
    )
    assert [(c["id"], c["function"]["arguments"])
            for c in result["choices"][0]["message"]["tool_calls"]] == [("a", "{}"), ("b", "{}")]


def test_same_index_with_a_new_id_starts_a_new_call():
    result = _agg(
        _c({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "f", "arguments": "{}"}}]}),
        _c({"tool_calls": [{"index": 0, "id": "a", "function": {"arguments": ""}}]}),  # 同 id 照接
        _c({"tool_calls": [{"index": 0, "id": "b", "function": {"name": "g", "arguments": "{}"}}]}),
    )
    assert [c["id"] for c in result["choices"][0]["message"]["tool_calls"]] == ["a", "b"]


def test_usage_chunk_with_empty_choices_and_null_usage_elsewhere():
    result = _agg({"id": "x", "choices": [{"index": 0, "delta": {"content": "好"}}], "usage": None},
                  {"id": "y", "choices": [], "usage": {"prompt_tokens": 3}},
                  {"id": "z", "choices": [], "usage": None})
    assert result == {"id": "x", "choices": [{"index": 0, "message": {"content": "好"}}],
                      "usage": {"prompt_tokens": 3}}


def test_keys_come_only_from_chunks():
    """沒有 index、沒有 finish_reason 的 chunk 彙整後也沒有（golden fixture 的形狀）。"""
    assert _agg({"choices": [{"delta": {"content": "好"}, "finish_reason": None}]}) == {
        "choices": [{"message": {"content": "好"}}]}


def test_non_streaming_message_key_is_rejected():
    with pytest.raises(StreamShapeError):
        _agg({"choices": [{"index": 0, "message": {"content": "好"}}]})


# ── 廠商文件的串流範例原文（獨立 fixture） ──────────────────────────────────

# OpenAI API reference「The chat completion chunk object」的串流範例（id 等原樣）。
_OPENAI_DOC = (
    b'data: {"id":"chatcmpl-123","object":"chat.completion.chunk",'
    b'"created":1694268190,"model":"gpt-4o-mini", "system_fingerprint": "fp_44709d6fcb", '
    b'"choices":[{"index":0,"delta":{"role":"assistant","content":""},"logprobs":null,"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-123","object":"chat.completion.chunk",'
    b'"created":1694268190,"model":"gpt-4o-mini", "system_fingerprint": "fp_44709d6fcb", '
    b'"choices":[{"index":0,"delta":{"content":"Hello"},"logprobs":null,"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-123","object":"chat.completion.chunk",'
    b'"created":1694268190,"model":"gpt-4o-mini", "system_fingerprint": "fp_44709d6fcb", '
    b'"choices":[{"index":0,"delta":{},"logprobs":null,"finish_reason":"stop"}]}\n\n'
    b'data: [DONE]\n\n'
)

# DeepSeek reasoning 模型文件的串流形狀（reasoning_content 先、content 後；usage 在最後）。
_DEEPSEEK_DOC = (
    'data: {"id":"d1","object":"chat.completion.chunk","created":1,"model":"deepseek-reasoner",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":null,"reasoning_content":"9.11"},'
    '"logprobs":null,"finish_reason":null}]}\n\n'
    'data: {"id":"d1","object":"chat.completion.chunk","created":1,"model":"deepseek-reasoner",'
    '"choices":[{"index":0,"delta":{"content":"9.8 更大","reasoning_content":null},'
    '"logprobs":null,"finish_reason":null}]}\n\n'
    'data: {"id":"d1","object":"chat.completion.chunk","created":1,"model":"deepseek-reasoner",'
    '"choices":[{"index":0,"delta":{"content":"","reasoning_content":null},"logprobs":null,'
    '"finish_reason":"stop"}],"usage":{"prompt_tokens":13,"completion_tokens":5,'
    '"total_tokens":18,"prompt_tokens_details":{"cached_tokens":0}}}\n\n'
    "data: [DONE]\n\n"
).encode()


async def _fold(body: bytes) -> dict:
    events = await _parse(body)
    assert events[-1] == "[DONE]"
    return _agg(*[json.loads(e) for e in events[:-1]])


async def test_openai_documentation_example():
    assert await _fold(_OPENAI_DOC) == {
        "id": "chatcmpl-123", "object": "chat.completion.chunk", "created": 1694268190,
        "model": "gpt-4o-mini", "system_fingerprint": "fp_44709d6fcb",
        "choices": [{"index": 0, "logprobs": None,
                     "message": {"role": "assistant", "content": "Hello"},
                     "finish_reason": "stop"}],
    }


async def test_deepseek_reasoning_example():
    result = await _fold(_DEEPSEEK_DOC)
    assert result["choices"][0]["message"] == {
        "role": "assistant", "content": "9.8 更大", "reasoning_content": "9.11"}
    assert result["usage"]["prompt_tokens"] == 13
    assert result["choices"][0]["finish_reason"] == "stop"


# ── Gemini 彙整器（3b-U3；規則見 ADR-0248） ─────────────────────────────

def _gc(*parts: dict, finish: str | None = None, usage: dict | None = None) -> dict:
    candidate: dict = {"content": {"role": "model", "parts": list(parts)}, "index": 0}
    if finish:
        candidate["finishReason"] = finish
    return {"candidates": [candidate], **({"usageMetadata": usage} if usage else {})}


def _gagg(*chunks: dict) -> GeminiStreamAggregator:
    aggregator = GeminiStreamAggregator()
    for chunk in chunks:
        aggregator.add(chunk)
    return aggregator


def _gparts(*chunks: dict) -> list[dict]:
    return _gagg(*chunks).result()["candidates"][0]["content"]["parts"]


def test_text_parts_merge_only_at_chunk_boundaries():
    assert _gparts(_gc({"text": "你"}), _gc({"text": "好"}, {"text": "！"})) == [
        {"text": "你好"}, {"text": "！"}]  # 同一個 chunk 裡的兩個 part 不合併


def test_thought_flag_must_match_to_merge():
    assert _gparts(_gc({"text": "想", "thought": True}), _gc({"text": "完", "thought": True}),
                  _gc({"text": "答"})) == [{"text": "想完", "thought": True}, {"text": "答"}]


def test_signature_follows_its_part_and_closes_it():
    assert _gparts(_gc({"text": "答案"}),
                  _gc({"text": "是紅茶", "thoughtSignature": "sigA"}),
                  _gc({"text": "。"})) == [
        {"text": "答案是紅茶", "thoughtSignature": "sigA"}, {"text": "。"}]  # 已簽章的不再接


def test_signature_only_empty_text_part_merges_or_stays():
    assert _gparts(_gc({"text": "答案是紅茶。"}),
                  _gc({"text": "", "thoughtSignature": "sigB"})) == [
        {"text": "答案是紅茶。", "thoughtSignature": "sigB"}]
    call = {"functionCall": {"name": "vault_tools", "args": {}}}
    assert _gparts(_gc(call), _gc({"text": "", "thoughtSignature": "sigC"})) == [
        call, {"text": "", "thoughtSignature": "sigC"}]  # 前一個不是 text：獨立保留


def test_other_parts_and_unknown_fields_are_kept_verbatim():
    call = {"functionCall": {"name": "f", "args": {"a": 1}}, "thoughtSignature": "sigD"}
    future = {"text": "x", "futureField": {"k": 1}}
    first = {"candidates": [{"content": {"role": "model", "parts": [{"text": "a"}]}, "index": 0,
                             "safetyRatings": [{"p": 1}], "avgLogprobs": -0.5}],
             "usageMetadata": {"promptTokenCount": 3}, "modelVersion": "v1", "responseId": "r1"}
    last = {"candidates": [{"content": {"role": "model", "parts": [future, call]}, "index": 0,
                            "safetyRatings": None, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2},
            "modelVersion": "v2", "responseId": "r1"}
    aggregator = _gagg(first, last)
    assert aggregator.complete is True
    assert aggregator.result() == {
        "candidates": [{"content": {"role": "model", "parts": [{"text": "a"}, future, call]},
                        "index": 0, "safetyRatings": [{"p": 1}], "avgLogprobs": -0.5,
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2},
        "modelVersion": "v1", "responseId": "r1"}


def test_gemini_completion_signals():
    assert _gagg(_gc({"text": "a"}, usage={"promptTokenCount": 1})).complete is False
    second = {"candidates": [{"content": {"parts": [{"text": "b"}]}, "index": 1,
                              "finishReason": "STOP"}]}
    aggregator = _gagg(second, _gc({"text": "a"}))  # 任一 candidate 有 finishReason 就算完整
    assert aggregator.complete is True
    assert [c["index"] for c in aggregator.result()["candidates"]] == [0, 1]
    blocked = {"promptFeedback": {"blockReason": "SAFETY"},
               "usageMetadata": {"promptTokenCount": 4}}
    assert _gagg(blocked).complete is True
    assert _gagg(blocked).result() == blocked



@pytest.mark.parametrize("chunk", [
    [1], {"candidates": ["x"]}, {"candidates": [{"content": {"parts": ["x"]}}]},
], ids=["chunk", "candidate", "part"])
def test_gemini_non_object_shapes_are_rejected(chunk):
    with pytest.raises(StreamShapeError):
        _gagg(chunk)

# Gemini API 文件 streamGenerateContent（alt=sse）範例的形狀：每個事件一份完整回應、行尾 CRLF、
# 最後一個 chunk 是空字串 text＋finishReason。
_GEMINI_DOC = (
    b'data: {"candidates": [{"content": {"parts": [{"text": "The"}],"role": "model"},"index": 0}],'
    b'"usageMetadata": {"promptTokenCount": 8,"totalTokenCount": 8},'
    b'"modelVersion": "gemini-2.0-flash","responseId": "doc1"}\r\n\r\n'
    b'data: {"candidates": [{"content": {"parts": [{"text": " opposite of hot is cold."}],'
    b'"role": "model"},"index": 0}],'
    b'"usageMetadata": {"promptTokenCount": 8,"totalTokenCount": 8},'
    b'"modelVersion": "gemini-2.0-flash","responseId": "doc1"}\r\n\r\n'
    b'data: {"candidates": [{"content": {"parts": [{"text": ""}],"role": "model"},'
    b'"finishReason": "STOP","index": 0}],"usageMetadata": {"promptTokenCount": 8,'
    b'"candidatesTokenCount": 7,"totalTokenCount": 15},'
    b'"modelVersion": "gemini-2.0-flash","responseId": "doc1"}\r\n\r\n'
)


async def test_gemini_documentation_example():
    aggregator = _gagg(*[json.loads(e) for e in await _parse(_GEMINI_DOC)])
    assert aggregator.complete is True
    assert aggregator.result() == {
        "candidates": [{"content": {"parts": [{"text": "The opposite of hot is cold."}],
                                    "role": "model"}, "index": 0, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 7, "totalTokenCount": 15},
        "modelVersion": "gemini-2.0-flash", "responseId": "doc1"}
