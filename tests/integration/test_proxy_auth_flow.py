from unittest.mock import AsyncMock, patch

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.auth_service import AuthService


@pytest.fixture
def client():
    """使用 Context Manager 確保觸發 lifespan 事件 (初始化 app.state.context)"""
    with TestClient(app) as c:
        yield c

@pytest.fixture
def mock_identity_service():
    """Mock identity_service for integration tests"""
    with patch("src.services.identity_router.identity_service") as mock:
        yield mock

@pytest.mark.asyncio
async def test_proxy_messages_auth_flow(client, mock_identity_service):
    """驗證 /v1/messages 的身分流 (帶 Token)"""
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner", capabilities=[])
    mock_identity_service.verify_token.return_value = identity
    
    with patch("src.routers.proxy.process_proxy_request", new_callable=AsyncMock) as mock_process:
        # 使用真實的 Response 對象
        mock_process.return_value = JSONResponse(content={"status": "ok"})
        
        payload = {
            "model": "claude-3-opus-20240229",
            "messages": [{"role": "user", "content": "hello"}]
        }
        response = client.post("/v1/messages", json=payload, headers={"Authorization": "Bearer valid_token"})
        
        assert response.status_code == 200
        mock_identity_service.verify_token.assert_called_with("valid_token")

@pytest.mark.asyncio
async def test_proxy_local_handshake_flow(client, mock_identity_service):
    """驗證本地存取時的自動領卡 (LDH) 流程（修復 3：僅限系統未認領＝首裝）"""
    # ADR-0144：LDH 依賴認領狀態——顯式清空沙箱 users.json＝未認領，
    # 避免繼承其他測試建立的真密碼使用者（已認領→LDH 關閉）造成順序依賴。
    AuthService()._atomic_users_write(lambda users: users.clear())  # noqa: SLF001
    identity = AgentIdentity.for_role(
        agent_id="DebugAgent", role="Debugger", capabilities=["debug"]
    )
    mock_identity_service.issue_token.return_value = "new_generated_token"
    
    with patch("src.services.identity_router.permission_service") as mock_perm, \
         patch("src.routers.proxy.process_proxy_request", new_callable=AsyncMock) as mock_process:
        
        mock_perm.get_role_key.return_value = "Debugger"
        mock_perm.get_capabilities_by_id.return_value = ["debug"]
        mock_process.return_value = JSONResponse(content={"status": "ok"})
        
        payload = {
            "model": "claude-3-opus-20240229",
            "messages": [{"role": "user", "content": "hello"}]
        }
        response = client.post("/v1/messages", json=payload, headers={"X-Agent-ID": "DebugAgent"})
        
        assert response.status_code == 200
        # 核心驗證：標頭中應包含新發放的 Token
        assert response.headers.get("X-New-Auth-Token") == "new_generated_token"

@pytest.mark.asyncio
async def test_proxy_unauthorized_external_blocked(client):
    """驗證非信任來源且無 Token 的請求被攔截（ADR-0245：沒登入不能聊天）"""
    with patch("src.services.identity_router.is_request_from_trusted_local") as mock_trusted, \
         patch("src.routers.proxy.process_proxy_request", new_callable=AsyncMock) as mock_process:
        mock_trusted.return_value = False
        mock_process.return_value = JSONResponse(content={"status": "ok"})

        payload = {
            "model": "claude-3-opus-20240229",
            "messages": [{"role": "user", "content": "hello"}]
        }
        response = client.post("/v1/messages", json=payload)

        assert response.status_code == 401
        mock_process.assert_not_called()
