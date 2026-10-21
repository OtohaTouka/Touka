from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.handlers.gemini_handler import handle_gemini_request
from src.handlers.openai_handler import handle_openai_request
from src.models.identity import AgentIdentity
from src.services.rhythm_service import rhythm_service

from tests.helpers.claude_stream import delegate_stream_to_create
from tests.helpers.stream_transport import FakeUpstream, gemini_stream_reply, stream_reply


@pytest.fixture
def mock_context():
    return {
        "gemini_api_key": "fake_key",
        "api_key": "fake_key",
        "url": "http://fake-api.com",
        "provider": "OpenAI",
        "custom_connection": False,  # 直接呼叫 handler：明寫不是自訂連線（R-3b ① L4）
        "protocol_manager": MagicMock(),
        "tool_registry": {},
        "memory_tool_registry": {},
        "plugin_registry": {},
        "request_id": "test_req_123",
        "t0": 0.0,
        "agent_id": "Charlie",
        "identity": AgentIdentity.for_role(agent_id="Charlie", role="Owner", capabilities=["*"]),
        "is_toukasoul": True,
        "session_id": "session_123",
        "model": "test-model"
    }

@pytest.mark.asyncio
async def test_gemini_handler_tool_call_integration(mock_context):
    """TDD: 驗證 Gemini Handler 是否將正確的 context 物件傳給 ToolExecutor
    """
    request = MagicMock()
    request.model = "gemini-2.5-flash"
    request.messages = []
    request.pulse_slots = None
    request.safety_settings = None  # 假傳輸會真的編 JSON（3b-U3）
    
    # 模擬 Gemini 的 Tool Call 回應，第二輪回傳文字以結束迴圈
    tool_response = {
        "candidates": [{
            "content": {
                "role": "model",
                "parts": [{"functionCall": {"name": "vault_tools", "args": {"vault": "personal", "action": "list"}}}]
            },
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}
    }
    text_response = {
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": "Done!"}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}
    }

    # 3b-U3：主迴圈走自寫 SSE 串流（共用假傳輸，送出內容從 request.content 解析）
    upstream = FakeUpstream([gemini_stream_reply(tool_response, "semantic"),
                             gemini_stream_reply(text_response, "semantic")])

    async def mock_execute(name, args, req_id, ctx, queue, **kwargs):
        await queue.put(None) # Sentinel for completion
        return "Tool Success"

    with pytest.MonkeyPatch.context() as monkeypatch, patch(
        "src.handlers.react_core.ToolExecutor"
    ) as MockExecutor:
        upstream.install(monkeypatch)
        mock_executor_inst = MockExecutor.return_value
        mock_executor_inst.execute_to_queue = AsyncMock(side_effect=mock_execute)

        gen = handle_gemini_request(request, mock_context)
        results = []
        try:
            # 設定超時防止無限迴圈
            async for chunk in gen:
                results.append(chunk)
                if len(results) > 50:
                    break
        except Exception as e:
            pytest.fail(f"Handler crashed: {e}")

        args_list = mock_executor_inst.execute_to_queue.call_args_list
        assert len(args_list) > 0

        # 簽名檢查：(name, args, request_id, ctx, queue)
        call_args = args_list[0][0]
        assert isinstance(call_args[3], dict)
        assert call_args[3]["agent_id"] == "Charlie"

        # 驗證沒有非法關鍵字參數
        call_kwargs = args_list[0][1]
        assert "session_id" not in call_kwargs
        assert len(upstream.requests) == 2
        upstream.assert_all_closed()

@pytest.mark.asyncio
async def test_openai_handler_tool_call_integration(mock_context):
    """TDD: 驗證 OpenAI Handler 是否將正確的 context 物件傳給 ToolExecutor
    """
    request = MagicMock()
    # 模擬真實的 OpenAIChatRequest 結構
    request.model_dump.return_value = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "Hello"}]
    }
    request.messages = [MagicMock(content="Hello")]
    request.pulse_slots = None
    
    # 模擬 OpenAI 的 Tool Call 回應，第二輪回傳文字以結束迴圈
    tool_response = {
        "choices": [{
            "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call_123", "type": "function",
                    "function": {"name": "vault_tools", "arguments": "{\"vault\":\"personal\",\"action\":\"list\"}"}
                }]
            }
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}
    }
    text_response = {
        "choices": [{"message": {"role": "assistant", "content": "Done!"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2}
    }

    # 3b-U2：主迴圈走自寫 SSE 串流（共用假傳輸，送出內容從 request.content 解析）
    upstream = FakeUpstream([stream_reply(tool_response, "semantic"),
                             stream_reply(text_response, "semantic")])

    async def mock_execute(name, args, req_id, ctx, queue, **kwargs):
        await queue.put(None)
        return "Tool Success"

    with pytest.MonkeyPatch.context() as monkeypatch:
        upstream.install(monkeypatch)
        with patch("src.handlers.react_core.ToolExecutor") as MockExecutor:
            mock_executor_inst = MockExecutor.return_value
            mock_executor_inst.execute_to_queue = AsyncMock(side_effect=mock_execute)
            
            gen = handle_openai_request(request, mock_context)
            results = []
            try:
                # 這次應該會在兩輪後結束
                async for chunk in gen:
                    results.append(chunk)
                    if len(results) > 50: break
            except Exception as e:
                pytest.fail(f"OpenAI Handler crashed: {e}")

            args_list = mock_executor_inst.execute_to_queue.call_args_list
            assert len(args_list) > 0
            
            call_args = args_list[0][0]
            assert isinstance(call_args[3], dict)
            assert call_args[3]["agent_id"] == "Charlie"
            
            call_kwargs = args_list[0][1]
            assert "session_id" not in call_kwargs
            assert [body["stream"] for body in upstream.sent] == [True, True]
            upstream.assert_all_closed()


from src.handlers.claude_handler import handle_claude_request


@pytest.mark.asyncio
async def test_claude_handler_tool_call_integration(mock_context):
    """TDD: 驗證 Claude Handler 是否將正確的 context 物件傳給 ToolExecutor
    """
    request = MagicMock()
    request.model = "claude-3-5-sonnet"
    request.max_tokens = None  # 真實請求的預設（3a 起轉換器會讀）
    request.pulse_slots = None
    request.model_dump.return_value = {"model": "claude-3-5-sonnet", "max_tokens": 1024, "messages": []}
    request.messages = [MagicMock(content="Hello", role="user")]
    
    # 模擬 Claude 的 Tool Use 回應
    mock_tool_use = MagicMock()
    mock_tool_use.type = "tool_use"
    mock_tool_use.id = "cl_call_1"
    mock_tool_use.name = "vault_tools"
    mock_tool_use.input = {"vault": "personal", "action": "list"}
    
    mock_text = MagicMock()
    mock_text.type = "text"
    mock_text.text = "Searching..."

    mock_response = MagicMock()
    mock_response.content = [mock_text, mock_tool_use]
    mock_response.usage = MagicMock()
    mock_response.usage.model_dump.return_value = {"input_tokens": 10, "output_tokens": 5}
    mock_response.model_dump.return_value = {
        "id": "msg_123",
        "model": "claude-3-5-sonnet",
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Searching..."},
            {"type": "tool_use", "id": "cl_call_1", "name": "vault_tools", "input": {"vault": "personal", "action": "list"}}
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5}
    }

    # 第二次呼叫回傳純文字
    final_text = MagicMock()
    final_text.type = "text"
    final_text.text = "Done!"
    
    mock_final_response = MagicMock()
    mock_final_response.content = [final_text]
    mock_final_response.usage = MagicMock()
    mock_final_response.usage.model_dump.return_value = {"input_tokens": 5, "output_tokens": 2}
    mock_final_response.model_dump.return_value = {
        "id": "msg_456",
        "model": "claude-3-5-sonnet",
        "role": "assistant",
        "content": [{"type": "text", "text": "Done!"}],
        "usage": {"input_tokens": 5, "output_tokens": 2}
    }

    mock_responses = [mock_response, mock_final_response]

    async def mock_execute(name, args, req_id, ctx, queue, **kwargs):
        await queue.put(None)
        return "Tool Success"

    # Mock Anthropic Client
    anthropic_client = mock_context["anthropic_client"] = MagicMock()
    anthropic_client.messages.create = AsyncMock(side_effect=mock_responses)
    delegate_stream_to_create(anthropic_client)  # 3b-U1：主迴圈走 messages.stream()

    with patch("src.handlers.react_core.ToolExecutor") as MockExecutor:
        mock_executor_inst = MockExecutor.return_value
        mock_executor_inst.execute_to_queue = AsyncMock(side_effect=mock_execute)
        
        gen = handle_claude_request(request, mock_context)
        results = []
        try:
            async for chunk in gen:
                results.append(chunk)
                if len(results) > 50: break
        except Exception as e:
            pytest.fail(f"Claude Handler crashed: {e}")

        args_list = mock_executor_inst.execute_to_queue.call_args_list
        assert len(args_list) > 0
        
        call_args = args_list[0][0]
        # 驗證傳遞的是 context 字典
        assert isinstance(call_args[3], dict)
        assert call_args[3]["agent_id"] == "Charlie"


@pytest.mark.asyncio
async def test_gemini_handler_stops_on_interrupt(mock_context):
    """中斷（#1）：session 已標記中斷時，handler 取得首個回應後即 break，不執行工具。"""
    sid = mock_context["session_id"]
    rhythm_service.interrupt_session(sid)
    try:
        request = MagicMock()
        request.model = "gemini-2.5-flash"
        request.messages = []
        request.pulse_slots = None

        # 回傳一個「工具呼叫」回應——正常情況下會繼續迴圈並執行工具
        tool_response = {
            "candidates": [{
                "content": {"role": "model", "parts": [
                    {"functionCall": {"name": "vault_tools", "args": {"vault": "personal", "action": "list"}}}
                ]}
            }],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
        }

        async def side_effect(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = tool_response
            return resp

        with patch("httpx.AsyncClient.post", side_effect=side_effect), patch(
            "src.handlers.react_core.ToolExecutor"
        ) as MockExecutor:
            mock_exec = MockExecutor.return_value
            mock_exec.execute_to_queue = AsyncMock()

            gen = handle_gemini_request(request, mock_context)
            chunks = []
            async for chunk in gen:
                chunks.append(chunk)
                if len(chunks) > 50:
                    break

            # 中斷 → 工具未被執行（迴圈在執行工具前就 break）
            assert mock_exec.execute_to_queue.call_count == 0
    finally:
        rhythm_service.clear_interrupt(sid)
