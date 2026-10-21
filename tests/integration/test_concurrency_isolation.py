"""Integration test for concurrency identity isolation."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from src.main import app
from src.services.identity_service import identity_service

from tests.helpers.claude_stream import delegate_stream_to_create


@pytest.fixture
def mock_anthropic(mock_anthropic):
    """主迴圈走 messages.stream()：本地包裝委派給 create（3b-U1；conftest 不改）。"""
    return delegate_stream_to_create(mock_anthropic)


@pytest.mark.asyncio
async def test_concurrent_identity_isolation(mock_identity, mock_anthropic, mock_text_response):
    """驗證在高併發請求下，不同 Agent 的 Identity Context 不會發生竄位 (Bleeding)。"""
    # 1. 準備身分
    agent_a = mock_identity(agent_id="Agent_A", role="Owner")
    agent_b = mock_identity(agent_id="Agent_B", role="Base")
    token_a = identity_service.issue_token(agent_a)
    token_b = identity_service.issue_token(agent_b)
    
    # 2. Mock 模型回傳 (確保每個請求都有回應)
    mock_anthropic.messages.create.return_value = mock_text_response("Isolated Response")
    
    # 確保 app.state.context 已初始化 (防禦性處理)
    if not hasattr(app.state, "context"):
        app.state.context = {"tool_registry": {}}
        
    app.state.context["anthropic_client"] = mock_anthropic
    
    async def make_request(agent_id, token):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            headers = {
                "Authorization": f"Bearer {token}",
                "X-Agent-ID": agent_id
            }
            payload = {
                "model": "claude-3-5-sonnet",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": f"I am {agent_id}"}]
            }
            response = await ac.post("/v1/messages", json=payload, headers=headers)
            return agent_id, response.status_code, response.json()

    # 3. 同時發送 20 個交替身分的請求
    tasks = []
    for i in range(20):
        if i % 2 == 0:
            tasks.append(make_request("Agent_A", token_a))
        else:
            tasks.append(make_request("Agent_B", token_b))
            
    results = await asyncio.gather(*tasks)
    
    # 4. 驗證所有請求都成功且身分正確
    for _agent_id, status, _body in results:
        assert status == 200
        
    assert len(results) == 20
    assert mock_anthropic.messages.create.call_count == 20  # 3b-U1：確實走到模型
