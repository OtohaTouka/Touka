import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity

@pytest.fixture
def client():
    """使用 Context Manager 確保觸發 lifespan 事件 (初始化 app.state.context)"""
    with TestClient(app) as c:
        yield c

@pytest.fixture
def mock_identity_service():
    """Mock identity_service for integration tests"""
    with patch("src.services.identity_router.identity_service") as mock:
        identity = AgentIdentity.for_role(
            agent_id="DebugAgent", role="Debugger", capabilities=["debug"]
        )
        mock.verify_token.return_value = identity
        yield mock

@pytest.mark.asyncio
async def test_toukagroup_visibility_in_models(client, mock_identity_service):
    """驗證 ToukaGroup 是否出現在模型清單中"""
    
    # 模擬 ModelManager 返回的模型清單
    mock_models = [
        {
            "id": "ToukaGroup",
            "name": "🌍 靈魂群聊 (ToukaGroup)",
            "owned_by": "proxy",
            "enabled": True,
            "status": "online"
        }
    ]
    
    with patch("src.services.models.manager.ModelManager.get_models", return_value=mock_models):
        response = client.get("/v1/models", headers={"Authorization": "Bearer valid_token"})
        assert response.status_code == 200
        
        data = response.json()
        models = data.get("data", [])
        model_ids = [m["id"] for m in models]
        
        assert "ToukaGroup" in model_ids

@pytest.mark.asyncio
async def test_toukagroup_alias_dispatch_flow(client, mock_identity_service):
    """驗證 ToukaGroup 的別名解析與分發流 (使用 OpenAI 格式測試)"""
    
    mock_registry = {"Alice": ["Ember"]}
    mock_whitelist = {"ToukaSoul": {"Ember": "Alice"}}
    
    async def mock_gen(*args, **kwargs):
        yield 'data: {"choices": [{"delta": {"content": "Hello from Alice"}, "index": 0}]}\n\n'
        yield "data: [DONE]\n\n"

    with patch("src.services.dispatchers.group.get_stream_generator", side_effect=mock_gen), \
         patch("src.services.dispatchers.group.get_active_brain", return_value="gemini-1.5-flash"), \
         patch("src.services.dispatchers.group.record_working_brain"), \
         patch("src.services.models.manager.ModelManager.get_model_ids", return_value=["Alice"]), \
         patch("src.services.group_router.soul_service") as mock_souls, \
         patch("src.services.resource_manager.resource_manager.read_json") as mock_read:
        
        mock_souls.is_toukasoul.return_value = True  # Alice is a mock soul (ADR-0245)
        mock_read.side_effect = [mock_registry, mock_whitelist]
        
        payload = {
            "model": "ToukaGroup",
            "messages": [{"role": "user", "content": "你好 @Ember"}],
            "stream": False
        }

        response = client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer valid_token"})

        assert response.status_code == 200
        content = response.json()
        text = content["choices"][0]["message"]["content"]

        assert "【Ember】" in text
        assert "Hello from Alice" in text

@pytest.mark.asyncio
async def test_toukagroup_invalid_alias_graceful_fail(client, mock_identity_service):
    """驗證 ToukaGroup 遇到無效別名時的優雅錯誤回傳"""
    
    mock_registry = {"Alice": ["Ember"]}
    mock_whitelist = {"ToukaSoul": {"Ember": "Alice"}}

    with patch("src.services.models.manager.ModelManager.get_model_ids", return_value=["Alice"]), \
         patch("src.services.resource_manager.resource_manager.read_json") as mock_read:
        
        mock_read.side_effect = [mock_registry, mock_whitelist]
        
        payload = {
            "model": "ToukaGroup",
            "messages": [{"role": "user", "content": "你好 @NoSuchAgent"}],
            "stream": False
        }
        
        response = client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer valid_token"})
        
        assert response.status_code == 200
        content = response.json()
        text = content["choices"][0]["message"]["content"]
        
        assert "❌" in text
        assert "NoSuchAgent" in text
