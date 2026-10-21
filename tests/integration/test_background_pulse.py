import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.models.context import PipelineContext
from src.models.session_persistence import SavedResult
from src.routers.heartbeat import _execute_single_pulse
from src.utils.async_db import SessionSnapshot
from src.utils.history_manager import TreeMessageManager


@pytest.fixture
def pulse_deps():
    """Mock dependencies for pulse execution"""
    with (
        patch(
            "src.utils.async_db.SessionService.read_execution_snapshot", new_callable=AsyncMock
        ) as mock_get,
        patch(
            "src.utils.async_db.SessionService.conditional_save_session", new_callable=AsyncMock
        ) as mock_save,
        patch("src.routers.heartbeat.dispatch_request") as mock_dispatch,
        patch("src.routers.heartbeat.autonomy_service") as mock_autonomy,
        patch("src.routers.heartbeat.rhythm_service") as mock_rhythm,
        patch("src.routers.heartbeat.get_active_brain", return_value="claude-x"),
    ):
        # ADR-0250 A3：沒有可用模型的脈衝直接結束；這裡測的是脈衝流程，先給主要模型

        tree = TreeMessageManager.append_node(
            TreeMessageManager.create_empty_tree(), "user", "hello"
        )
        mock_get.return_value = SessionSnapshot({
            "id": "test_sid",
            "title": "Test Session",
            "messages": tree,
        }, "rev")
        mock_save.return_value = SavedResult(status="saved", representation="full", revision="next")
        
        # 模擬 dispatch_request 回傳一個具有 body_iterator 的對象 (SSE 格式)
        async def mock_iter():
            data = {"choices": [{"delta": {"content": "I am alive"}}]}
            yield f"data: {json.dumps(data)}\n\n"
            yield "data: [DONE]\n\n"
        
        mock_response = MagicMock()
        mock_response.body_iterator = mock_iter()
        mock_dispatch.return_value = mock_response
        
        yield mock_get, mock_save, mock_dispatch, mock_autonomy, mock_rhythm

@pytest.mark.asyncio
async def test_execute_single_pulse_logic(pulse_deps, mock_identity):
    """驗證單次脈衝的核心邏輯流程"""
    mock_get, mock_save, mock_dispatch, mock_autonomy, mock_rhythm = pulse_deps
    
    identity = mock_identity(agent_id="Alice", role="Owner")
    ctx = PipelineContext(request_id="test_req", identity=identity)
    
    await _execute_single_pulse(
        final_session_id="test_sid",
        identity=identity,
        model_override=None,
        ctx=ctx,
        pulse_slots=5,
        t0=123456789.0,
        init_prompt="Wake up"
    )
    
    mock_get.assert_called()
    mock_dispatch.assert_called()
    mock_save.assert_awaited()

@pytest.mark.asyncio
async def test_pulse_role_correction(pulse_deps, mock_identity):
    """驗證脈衝是否正確修正角色序列 (避免連續 Assistant)"""
    mock_get, mock_save, mock_dispatch, _, _ = pulse_deps
    identity = mock_identity(agent_id="Alice", role="Owner")
    ctx = PipelineContext(request_id="test_req", identity=identity)
    
    tree = TreeMessageManager.append_node(TreeMessageManager.create_empty_tree(), "user", "hi")
    tree = TreeMessageManager.append_node(tree, "assistant", "hello")
    mock_get.return_value = SessionSnapshot({
        "id": "test_sid",
        "title": "Test Session",
        "messages": tree,
    }, "rev")
    
    await _execute_single_pulse(
        final_session_id="test_sid",
        identity=identity,
        model_override=None,
        ctx=ctx,
        pulse_slots=5,
        t0=123456789.0,
        init_prompt="Wake up"
    )
    
    assert mock_save.await_count >= 1
    assert mock_save.await_args_list[0].args[:2] == ("test_sid", "rev")
    sent = mock_dispatch.call_args.kwargs["request"].messages
    assert [message.role for message in sent][-3:] == ["user", "assistant", "user"]
