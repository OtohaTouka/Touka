"""Integration test for System Degradation (Resilience)."""

import pytest

from tests.helpers.claude_stream import delegate_stream_to_create


@pytest.fixture
def mock_anthropic(mock_anthropic):
    """主迴圈走 messages.stream()：本地包裝委派給 create（3b-U1；conftest 不改）。"""
    return delegate_stream_to_create(mock_anthropic)


def test_tool_list_degraded_status(auth_client):
    """驗證系統降級時，工具清單會顯示警告標籤 (Identity SSOT)。"""
    # 1. 正常狀態
    auth_client.app.state.context["system_degraded"] = False
    resp_normal = auth_client.get("/v1/debug/tools")
    assert "DEGRADED" not in resp_normal.text
    
    # 2. 設置降級狀態 (SSOT)
    auth_client.app.state.context["system_degraded"] = True
    resp_degraded = auth_client.get("/v1/debug/tools")
    
    assert resp_degraded.status_code == 200
    # 只要內容包含 DEGRADED 關鍵字即可
    assert "DEGRADED" in resp_degraded.text.upper()

def test_degraded_prompt_injection(auth_client, mock_anthropic, mock_text_response):
    """驗證系統降級時，提示詞中會被自動注入生存警告。"""
    # 1. 配置 Mock 與狀態
    auth_client.app.state.context["anthropic_client"] = mock_anthropic
    auth_client.app.state.context["system_degraded"] = True
    auth_client.app.state.context["degraded_reason"] = "Database Offline"
    
    mock_anthropic.messages.create.return_value = mock_text_response("Degraded mode acknowledged.")
    
    payload = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Status check"}]
    }
    
    # 2. 發送請求
    response = auth_client.post("/v1/messages", json=payload)
    assert response.status_code == 200
    
    # 3. 檢查模型接收到的 system 參數是否包含警告
    call_args = mock_anthropic.messages.create.call_args
    system_param = call_args.kwargs.get("system", "")
    
    assert "SYSTEM WARNING" in str(system_param)
    assert "DEGRADED MODE" in str(system_param)
    assert "Database Offline" in str(system_param)
