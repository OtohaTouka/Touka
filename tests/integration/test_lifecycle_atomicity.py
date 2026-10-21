"""Integration test for lifecycle atomicity (task continuity)."""

from unittest.mock import AsyncMock, patch

import pytest
from src.services.identity_router import get_optional_identity, get_verified_identity

from tests.helpers.claude_stream import delegate_stream_to_create


@pytest.fixture
def mock_anthropic(mock_anthropic):
    """主迴圈走 messages.stream()：本地包裝委派給 create（3b-U1；conftest 不改）。"""
    return delegate_stream_to_create(mock_anthropic)


@pytest.mark.asyncio
async def test_react_loop_continuity_during_token_expiry(
    auth_client, mock_anthropic, mock_tool_response, mock_text_response, mock_identity
):
    """驗證當任務通過 Gate 1 後，即便 Token 在執行中途過期，任務仍能自主完成 (Atomicity)。"""
    # 1. 準備身分——直接 override /v1/messages 實際使用的 get_optional_identity，
    #    模擬「已通過 Gate 1」的錨定身分（ADR-0131 前此測試 override 錯依賴
    #    get_verified_identity，入場實靠「本地壞卡→LDH 降級」碰巧放行；LDH 收斂後
    #    壞卡一律 401，故改為正確注入以單測「錨定後不受 token 過期影響」本身）。
    identity = mock_identity(agent_id="DebugAgent")
    auth_client.app.dependency_overrides[get_verified_identity] = lambda: identity
    auth_client.app.dependency_overrides[get_optional_identity] = lambda: identity
    
    # 2. 準備 2 輪 ReAct 序列
    # 最終回應帶停止關鍵字「【任務完成】」→ should_stop_react 確定性終止迴圈於第 2 輪。
    # （隨機次序測試揭露：原本無停止字、迴圈是否多 nudge 一輪取決於全域 autonomy
    #   slot 狀態；明確的完成訊號讓本測試只驗「迴圈完成任務」而不脆弱耦合於 slot 數。）
    args = {"vault": "personal", "action": "list"}
    tool_call = mock_tool_response("vault_tools", args, call_id="c1")
    final_text = mock_text_response("Task completed despite expiry. 【任務完成】")
    mock_anthropic.messages.create.side_effect = [tool_call, final_text]
    
    # Mock 工具註冊表，防止真實執行
    reg = auth_client.app.state.context["tool_registry"]
    reg["vault_tools"] = AsyncMock(return_value="Mocked File List")
    auth_client.app.state.context["anthropic_client"] = mock_anthropic
    auth_client.app.state.context["is_toukasoul"] = True
    
    # 3. 準備請求
    payload = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Start long task."}],
        "stream": False
    }
    
    # 4. 模擬 Token 驗證 (僅在進入點調用一次)
    # 我們 Mock verify_token 讓它即使在後續被調用也會失敗，但循環應該已經拿到了 identity
    path = "src.services.identity_service.JWTAuthenticationProvider.verify_token"
    with patch(path) as mock_verify:
        mock_verify.return_value = None # 模擬驗證失敗 (如果循環中途又調用的話)
        
        headers = {"X-Agent-ID": "DebugAgent"}
        response = auth_client.post("/v1/messages", json=payload, headers=headers)
        
        # 4. 驗證結果
        assert response.status_code == 200
        # 核心斷言：模型被調用了兩次，代表循環成功完成，不受 Token 驗證狀態影響
        assert mock_anthropic.messages.create.call_count == 2
    
    auth_client.app.dependency_overrides.clear()
