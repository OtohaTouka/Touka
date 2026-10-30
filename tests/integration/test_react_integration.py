"""Integration test for the ReAct loop."""

from unittest.mock import AsyncMock

import pytest

from tests.helpers.claude_stream import delegate_stream_to_create


@pytest.fixture
def mock_anthropic(mock_anthropic):
    """主迴圈走 messages.stream()：本地包裝委派給 create（3b-U1；conftest 不改）。"""
    return delegate_stream_to_create(mock_anthropic)


@pytest.mark.asyncio
async def test_full_react_loop_with_tool(
    auth_client, mock_anthropic, mock_tool_response, mock_text_response, zone_data_root
):
    """驗證全鏈路 ReAct 循環：接收請求 -> 觸發工具 -> 返回結果。"""
    agent_id = "ReactAgent"
    # 1. 準備 Mock 序列 (2 正常 + 最多 3 輪 nudge 緩衝)
    tool_call = mock_tool_response(
        "vault_tools",
        {"vault": "personal", "action": "list"},
        call_id="call_1",
    )
    final_text = mock_text_response("Here is the list of files.")

    mock_anthropic.messages.create.side_effect = [
        tool_call, final_text, final_text, final_text, final_text,
    ]

    # 2. Mock Tool Registry
    mock_vault_tool = AsyncMock(return_value="File: a.txt, b.txt")
    auth_client.app.state.context["tool_registry"]["vault_tools"] = mock_vault_tool
    auth_client.app.state.context["anthropic_client"] = mock_anthropic

    # 重要：開啟靈魂模式以觸發 ReAct 循環
    auth_client.app.state.context["is_toukasoul"] = True

    # 3. 發送請求
    payload = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "List my files."}],
        "stream": True,
    }

    # 4. 執行請求
    # data 根是這個測試自己的空資料夾（zone_data_root），其他測試的 soul 殘留影響不到
    headers = {"X-Touka-Soul-ID": agent_id}
    response = auth_client.post("/v1/messages", json=payload, headers=headers)

    assert response.status_code == 200
    assert "list of files" in response.text.lower()

    # 5. 斷言驗證：至少 2 次 (tool + final)，至多 5 次 (含 nudge)
    call_count = mock_anthropic.messages.create.call_count
    assert 2 <= call_count <= 5, f"Unexpected call count: {call_count}"
