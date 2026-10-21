"""漂移修復批 C5（選配 D7＋D8，拍板 P3=納入）。

D7：Gemini 工具呼叫遙測關聯——gemini_handler 過去傳 ``call_id=None``（Gemini API
無原生 id）→ ToolCallEvent（ADR-0122 封套）僅 Gemini 路徑無法逐呼叫關聯（openai
傳 tc.id、claude 傳 block.id）。修法＝以每 run 序號合成穩定 id（``gcall_{n}``）。

D8：``is_toukasoul`` context 預設三家不一致（openai/gemini 預設 True＝fail-open、
claude 預設 False＝fail-closed）。生產鏈路 ctx 皆為 PipelineContext（is_toukasoul
為欄位、恆存在）→ 預設值永不觸發＝零行為變化；統一 False 純防禦（裸 dict 呼叫
不得憑空獲得 ReAct 權）。
"""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from src.handlers import react_core
from src.handlers.gemini_handler import handle_gemini_request
from src.models.identity import AgentIdentity

from tests.helpers.stream_transport import FakeUpstream, gemini_stream_reply


@pytest.mark.asyncio
async def test_gemini_tool_calls_get_synthesized_call_ids():
    tool_resp = {
        "candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": "vault_tools",
                              "args": {"vault": "personal", "action": "list"}}},
            {"functionCall": {"name": "memory_tools", "args": {"action": "query"}}},
        ]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }
    text_resp = {
        "candidates": [{"content": {"role": "model", "parts": [{"text": "Done!"}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
    }
    # 3b-U3：主迴圈走自寫 SSE 串流（共用假傳輸）
    upstream = FakeUpstream([gemini_stream_reply(r, "semantic")
                             for r in (tool_resp, text_resp, text_resp)])

    async def mock_execute(name, args, req_id, c, queue, **kwargs):
        await queue.put({"type": "log", "content": f"[{name} done]"})
        await queue.put(None)
        return "OK"

    ctx = {
        "gemini_api_key": "fake",
        "protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
        "tool_registry": {}, "memory_tool_registry": {}, "plugin_registry": {},
        "request_id": "req_callid", "t0": 0.0,
        "agent_id": "Sumi",
        "identity": AgentIdentity.for_role("Sumi", "Owner", capabilities=["*"]),
        "is_toukasoul": True, "session_id": "sess_callid",
    }
    request = MagicMock()
    request.model = "gemini-2.5-flash"
    request.pulse_slots = None
    request.safety_settings = None
    request.messages = [SimpleNamespace(role="user", content="hi", tool_calls=None)]
    request.tools = []

    with patch.object(httpx.AsyncClient, "send", upstream.send_fn()), patch(
        "src.handlers.react_core.ToolExecutor"
    ) as MockExecutor:
        executor = MockExecutor.return_value
        executor.execute_to_queue = AsyncMock(side_effect=mock_execute)
        chunks = []
        async for c in handle_gemini_request(request, ctx):
            chunks.append(c)
            if len(chunks) > 100:
                break

    call_ids = [c.kwargs.get("call_id") for c in executor.execute_to_queue.call_args_list]
    assert len(call_ids) == 2
    assert all(cid for cid in call_ids), (
        f"Gemini 路徑必須合成非空 call_id（ToolCallEvent 逐呼叫關聯），得到 {call_ids}"
    )
    assert len(set(call_ids)) == len(call_ids), f"call_id 必須唯一：{call_ids}"
    assert len(upstream.requests) == 2
    upstream.assert_all_closed()


def test_is_toukasoul_default_is_fail_closed_in_all_handlers():
    """D8 靜態確認：三 handler 的 is_toukasoul 預設一律 fail-closed（False）。"""
    # E1：openai 的 is_toukasoul 讀取點收進 react_core（E2/E3 後三家皆單源於此）。
    for mod in (react_core,):
        src = inspect.getsource(mod)
        assert 'context.get("is_toukasoul", True)' not in src, f"{mod.__name__} 仍為 fail-open 預設"
        assert 'context.get("is_toukasoul", False)' in src, f"{mod.__name__} 缺 fail-closed 預設"
