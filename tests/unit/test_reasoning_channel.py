"""T1（ADR-0152）：思考分頻——handler 把思考走獨立 SSE 頻道，不再漏進對話 content。

契約（wire）：思考以 OpenAI 相容 chunk 的 ``delta.reasoning_content`` 發射（DeepSeek
串流慣例；additive、舊前端忽略未知欄位＝零破壞）。correctness 錨：

- openai_handler：上游 ``message.reasoning_content`` 欄位（grok-4.5 實測封包型）與
  content 內嵌 ``<think>`` 型都剝離分頻；歷史（ReAct 續輪）append 的 assistant 訊息
  **不含 reasoning_content 欄位**（DeepSeek 回送即 400）且 content 為乾淨正文。
- claude_handler：native thinking blocks 改走 reasoning 頻道，不再以 ``<thinking>``
  文字塞 content。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.handlers.claude_handler import handle_claude_request
from src.handlers.openai_handler import handle_openai_request
from src.models.identity import AgentIdentity

from tests.helpers.claude_stream import delegate_stream_to_create
from tests.helpers.stream_transport import FakeUpstream, stream_reply


def _parse_sse(chunks: list[str]) -> list[dict]:
    """把 handler 產出的 SSE 行還原為 JSON 事件清單。"""
    events = []
    for raw in chunks:
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
    return events


def _deltas(events: list[dict], key: str) -> str:
    """串接所有 chunk 事件中 delta.<key> 的內容。"""
    out = ""
    for ev in events:
        for choice in ev.get("choices", []):
            val = (choice.get("delta") or {}).get(key)
            if val:
                out += val
    return out


@pytest.fixture
def ctx():
    return {
        "api_key": "fake_key",
        "url": "http://fake-api.com",
        "provider": "OpenAI",
        "custom_connection": False,  # 直接呼叫 handler：明寫不是自訂連線（R-3b ① L4）
        "protocol_manager": MagicMock(),
        "tool_registry": {},
        "memory_tool_registry": {},
        "plugin_registry": {},
        "request_id": "req_reasoning_t1",
        "t0": 0.0,
        "agent_id": "Charlie",
        "identity": AgentIdentity.for_role(agent_id="Charlie", role="Owner", capabilities=["*"]),
        "is_toukasoul": True,
        "session_id": "session_rsn",
        "model": "test-model",
    }


def _openai_request(messages=None):
    request = MagicMock()
    request.model_dump.return_value = {
        "model": "grok-4.5",
        "messages": messages or [{"role": "user", "content": "hi"}],
    }
    request.messages = [MagicMock(content="hi")]
    request.pulse_slots = None
    return request


async def _run(gen) -> list[str]:
    chunks = []
    async for c in gen:
        chunks.append(c)
        if len(chunks) > 60:
            break
    return chunks


# ── openai_handler ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_reasoning_field_split_to_channel(ctx, monkeypatch):
    """上游獨立 reasoning_content 欄位（grok-4.5 型）→ 走 delta.reasoning_content，
    不進 delta.content。"""
    upstream = {
        "choices": [{"message": {
            "role": "assistant",
            "content": "391",
            "reasoning_content": "17*23 = 391, easy.",
        }}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    FakeUpstream([stream_reply(upstream, "semantic")]).install(monkeypatch)  # 3b-U2：串流
    events = _parse_sse(await _run(handle_openai_request(_openai_request(), ctx)))

    assert "17*23 = 391" in _deltas(events, "reasoning_content")
    content = _deltas(events, "content")
    assert "391" in content
    assert "easy" not in content  # 思考不得漏進 content


@pytest.mark.asyncio
async def test_openai_embedded_think_stripped(ctx, monkeypatch):
    """content 內嵌 <think>（R1/本地模型型）→ 剝離分頻，正文乾淨。"""
    upstream = {
        "choices": [{"message": {
            "role": "assistant",
            "content": "<think>secret plan</think>\n\nHello!",
        }}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    FakeUpstream([stream_reply(upstream, "bytes")]).install(monkeypatch)  # 3b-U2：串流
    events = _parse_sse(await _run(handle_openai_request(_openai_request(), ctx)))

    assert "secret plan" in _deltas(events, "reasoning_content")
    content = _deltas(events, "content")
    assert "Hello!" in content
    assert "secret plan" not in content
    assert "<think>" not in content


@pytest.mark.asyncio
async def test_openai_react_history_clean(ctx, monkeypatch):
    """ReAct 續輪：append 回歷史的 assistant 訊息不含 reasoning_content 欄位、
    content 為剝離後正文（DeepSeek 回送 reasoning_content 即 400；think 回灌浪費 token）。"""
    tool_round = {
        "choices": [{"message": {
            "role": "assistant",
            "content": "<think>should call tool</think>calling now",
            "reasoning_content": "field reasoning",
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "vault_tools", "arguments": "{}"},
            }],
        }}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    final_round = {
        "choices": [{"message": {"role": "assistant", "content": "Done!"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    upstream = FakeUpstream([stream_reply(tool_round, "semantic"),
                             stream_reply(final_round, "semantic")]).install(monkeypatch)

    async def mock_execute(name, args, req_id, c, queue, **kwargs):
        await queue.put(None)
        return "Tool Success"

    req_dict = {"model": "grok-4.5", "messages": [{"role": "user", "content": "hi"}]}
    request = _openai_request()
    request.model_dump.return_value = req_dict

    with patch("src.handlers.react_core.ToolExecutor") as MockExecutor:
        MockExecutor.return_value.execute_to_queue = AsyncMock(side_effect=mock_execute)
        events = _parse_sse(await _run(handle_openai_request(request, ctx)))
    resent = [m for m in upstream.sent[1]["messages"] if m["role"] == "assistant"]
    assert "reasoning_content" not in resent[0]  # 第 2 輪送出的位元組裡也沒有（不回送）

    assistant_msgs = [m for m in req_dict["messages"] if m.get("role") == "assistant"]
    assert assistant_msgs, "ReAct 應把 assistant 訊息 append 回歷史"
    appended = assistant_msgs[0]
    assert "reasoning_content" not in appended
    assert "<think>" not in (appended.get("content") or "")
    assert "should call tool" not in (appended.get("content") or "")
    # 兩型思考都進了 reasoning 頻道
    reasoning = _deltas(events, "reasoning_content")
    assert "field reasoning" in reasoning
    assert "should call tool" in reasoning


# ── claude_handler ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claude_thinking_block_to_channel(ctx):
    """native thinking block → delta.reasoning_content；<thinking> 不再塞 content。"""
    think_block = MagicMock()
    think_block.type = "thinking"
    think_block.thinking = "pondering deeply"

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "Answer!"

    response = MagicMock()
    response.content = [think_block, text_block]
    response.usage = MagicMock()
    response.usage.model_dump.return_value = {"input_tokens": 5, "output_tokens": 2}
    response.model_dump.return_value = {
        "id": "msg_1", "model": "claude-test", "role": "assistant",
        "content": [{"type": "text", "text": "Answer!"}],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }

    request = MagicMock()
    request.model = "claude-test"
    request.max_tokens = None  # 真實請求的預設（3a 起轉換器會讀）
    request.pulse_slots = None
    request.model_dump.return_value = {
        "model": "claude-test", "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }
    request.messages = [MagicMock(content="hi", role="user")]

    ctx = dict(ctx)
    ctx["anthropic_client"] = MagicMock()
    ctx["anthropic_client"].messages.create = AsyncMock(return_value=response)
    delegate_stream_to_create(ctx["anthropic_client"])  # 3b-U1：主迴圈走 messages.stream()

    events = _parse_sse(await _run(handle_claude_request(request, ctx)))

    assert "pondering deeply" in _deltas(events, "reasoning_content")
    content = _deltas(events, "content")
    assert "Answer!" in content
    assert "pondering deeply" not in content
    assert "<thinking>" not in content
