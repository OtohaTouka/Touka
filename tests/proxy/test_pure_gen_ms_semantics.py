"""漂移修復批 C4（D5，拍板 P1=選項 B）：`pure_gen_ms` 語意統一＝各輪上游呼叫耗時累加。

漂移現況（同一遙測欄位、三家語意相反）：openai/gemini 以 `if not final_response:
t2 = now` 每輪重置 t2 → gen_time = t3 - t2 只量到「最後回應之後的收尾開銷」≈ 0；
claude 的 t2 在迴圈前 → 量到**整圈含工具執行**。兩種都不是「純生成」，且 e_index
消費 gen_time → 跨供應商不可比。

選項 B：`pure_gen_ms` ＝ Σ每輪（送出 → 回應到達），不含工具執行與本地處理——
欄位名誠實、三家可比。測法＝假時鐘：上游呼叫推進 7s、工具執行推進 5s、其餘 0；
兩輪工具＋一輪收尾文字 → 期望 pure_gen_ms == 3×7000 == 21000（現況 openai/gemini
≈0、claude=31000＝紅）。
"""

import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.handlers import gemini_handler as _gemini_module
from src.handlers import openai_handler as _openai_module
from src.handlers.claude_handler import handle_claude_request
from src.handlers.gemini_handler import handle_gemini_request
from src.handlers.openai_handler import handle_openai_request
from src.models.identity import AgentIdentity

from tests.helpers.claude_stream import delegate_stream_to_create
from tests.helpers.stream_transport import FakeUpstream, gemini_stream_reply, stream_reply

_UPSTREAM_S = 7.0
_TOOL_S = 5.0
_EXPECTED_MS = int(3 * _UPSTREAM_S * 1000)  # 3 次上游呼叫（2 工具輪＋1 收尾）


def _base_ctx(**extra) -> dict:
    ctx = {
        "api_key": "fake",
        "gemini_api_key": "fake",
        "url": "http://fake-api.com",
        "provider": "OpenAI",
        "custom_connection": False,  # 直接呼叫 handler：明寫不是自訂連線（R-3b ① L4）
        "protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
        "tool_registry": {},
        "memory_tool_registry": {},
        "plugin_registry": {},
        "request_id": "req_timing",
        "t0": 1000.0,
        "agent_id": "Sumi",
        "identity": AgentIdentity.for_role("Sumi", "Owner", capabilities=["*"]),
        "is_toukasoul": True,
        "session_id": "sess_timing",
    }
    ctx.update(extra)
    return ctx


def _extract_pure_gen_ms(chunks: list[str]) -> int:
    """自 SSE 輸出撈 touka_status 事件的 telemetry.pure_gen_ms。"""
    for chunk in chunks:
        for line in str(chunk).splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict) and "telemetry" in ev:
                return int(ev["telemetry"].get("pure_gen_ms", -1))
    return -1


def _mock_execute_factory(clock: dict):
    async def mock_execute(name, args, req_id, ctx, queue, **kwargs):
        clock["now"] += _TOOL_S  # 工具執行耗時（不得計入 pure_gen）
        await queue.put({"type": "log", "content": f"[{name} done]"})
        await queue.put(None)
        return "Tool Success"

    return mock_execute


async def _drain(gen) -> list[str]:
    chunks = []
    async for c in gen:
        chunks.append(c)
        if len(chunks) > 300:
            break
    return chunks


@pytest.mark.asyncio
async def test_openai_pure_gen_ms_accumulates_upstream_only(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "src.handlers.react_core.get_current_epoch", lambda: clock["now"]
    )

    tool_resp = {
        "choices": [{"message": {"content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "vault_tools",
                         "arguments": '{"vault": "personal", "action": "list"}'},
        }]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    text_resp = {
        "choices": [{"message": {"content": "Done!"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    def on_request(_n: int) -> None:
        clock["now"] += _UPSTREAM_S  # 上游生成耗時（3b-U2：主迴圈走串流，每個請求推進一次）

    replies = [stream_reply(r, "semantic") for r in (tool_resp, tool_resp, text_resp)]
    upstream = FakeUpstream(replies, on_request=on_request).install(monkeypatch)

    request = MagicMock()
    request.model = "gpt-4"
    request.pulse_slots = None
    request.model_dump.return_value = {
        "model": "gpt-4", "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }
    request.messages = [SimpleNamespace(role="user", content="hi", tool_calls=None)]

    with patch("src.handlers.react_core.ToolExecutor") as MockExecutor:
        MockExecutor.return_value.execute_to_queue = AsyncMock(
            side_effect=_mock_execute_factory(clock)
        )
        chunks = await _drain(handle_openai_request(request, _base_ctx()))

    assert _extract_pure_gen_ms(chunks) == _EXPECTED_MS
    assert len(upstream.requests) == 3
    upstream.assert_all_closed()


@pytest.mark.asyncio
async def test_gemini_pure_gen_ms_accumulates_upstream_only(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "src.handlers.react_core.get_current_epoch", lambda: clock["now"]
    )

    tool_resp = {
        "candidates": [{"content": {"role": "model", "parts": [{
            "functionCall": {"name": "vault_tools",
                             "args": {"vault": "personal", "action": "list"}},
        }]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }
    text_resp = {
        "candidates": [{"content": {"role": "model", "parts": [{"text": "Done!"}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
    }

    def on_request(_n: int) -> None:
        clock["now"] += _UPSTREAM_S  # 上游生成耗時（3b-U3：主迴圈走串流，每個請求推進一次）

    replies = [gemini_stream_reply(r, "semantic") for r in (tool_resp, tool_resp, text_resp)]
    upstream = FakeUpstream(replies, on_request=on_request).install(monkeypatch)

    request = MagicMock()
    request.model = "gemini-2.5-flash"
    request.pulse_slots = None
    request.safety_settings = None
    request.messages = [SimpleNamespace(role="user", content="hi", tool_calls=None)]
    request.tools = []

    with patch("src.handlers.react_core.ToolExecutor") as MockExecutor:
        MockExecutor.return_value.execute_to_queue = AsyncMock(
            side_effect=_mock_execute_factory(clock)
        )
        chunks = await _drain(handle_gemini_request(request, _base_ctx()))

    assert _extract_pure_gen_ms(chunks) == _EXPECTED_MS
    assert len(upstream.requests) == 3
    upstream.assert_all_closed()


def _claude_block(btype: str, **attrs) -> MagicMock:
    b = MagicMock()
    b.type = btype
    for k, v in attrs.items():
        setattr(b, k, v)
    return b


def _claude_response(blocks: list, usage: dict, dump: dict) -> MagicMock:
    r = MagicMock()
    r.content = blocks
    r.usage = MagicMock()
    r.usage.model_dump.return_value = usage
    r.model_dump.return_value = dump
    return r


@pytest.mark.asyncio
async def test_claude_pure_gen_ms_excludes_tool_execution(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "src.handlers.react_core.get_current_epoch", lambda: clock["now"]
    )

    tool_resp = _claude_response(
        [_claude_block("text", text="Working..."),
         _claude_block("tool_use", id="cl_1", name="vault_tools",
                       input={"vault": "personal", "action": "list"})],
        {"input_tokens": 10, "output_tokens": 5},
        {"id": "m1", "model": "claude-3-5-sonnet", "role": "assistant",
         "content": [{"type": "text", "text": "Working..."}],
         "usage": {"input_tokens": 10, "output_tokens": 5}},
    )
    text_resp = _claude_response(
        [_claude_block("text", text="Done!")],
        {"input_tokens": 5, "output_tokens": 2},
        {"id": "m2", "model": "claude-3-5-sonnet", "role": "assistant",
         "content": [{"type": "text", "text": "Done!"}],
         "usage": {"input_tokens": 5, "output_tokens": 2}},
    )

    async def create_side_effect(**_k):
        clock["now"] += _UPSTREAM_S
        return responses.pop(0) if responses else text_resp

    responses = [tool_resp, tool_resp, text_resp]
    anthropic_client = MagicMock()
    anthropic_client.messages.create = AsyncMock(side_effect=create_side_effect)
    delegate_stream_to_create(anthropic_client)  # 3b-U1：主迴圈走 messages.stream()

    request = MagicMock()
    request.model = "claude-3-5-sonnet"
    request.max_tokens = None  # 真實請求的預設（3a 起轉換器會讀）
    request.pulse_slots = None
    request.model_dump.return_value = {
        "model": "claude-3-5-sonnet", "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }
    request.messages = [SimpleNamespace(role="user", content="hi", tool_calls=None)]

    ctx = _base_ctx(anthropic_client=anthropic_client, model="claude-3-5-sonnet")
    with patch("src.handlers.react_core.ToolExecutor") as MockExecutor:
        MockExecutor.return_value.execute_to_queue = AsyncMock(
            side_effect=_mock_execute_factory(clock)
        )
        chunks = await _drain(handle_claude_request(request, ctx))

    assert _extract_pure_gen_ms(chunks) == _EXPECTED_MS


def test_round_reset_pattern_removed_from_sources():
    """靜態確認：openai/gemini 的「每輪重置 t2」模式已移除（比照禁令 26 範式）。"""
    for mod in (_openai_module, _gemini_module):
        src = inspect.getsource(mod)
        assert "if not final_response:\n                t2 = get_current_epoch()" not in src, mod.__name__
