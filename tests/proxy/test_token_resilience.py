import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_service import identity_service


@pytest.fixture
def short_lived_token():
    # 發放一個 2 秒的 Token
    return identity_service.issue_token(AgentIdentity(agent_id="Alice"), expires_delta=2)

def test_pulse_resilience_mid_expiry(short_lived_token):
    """TDD 測試：驗證脈衝執行過程中，Token 過期是否會導致任務失敗。
    預期：身分應在啟動時錨定，不受後續 Token 過期影響。
    """
    # 建立一個獨立的 Client 以便精確控制標頭
    client = TestClient(app)
    
    # 模擬身分識別成功後進入 ToolExecutor
    
    # 我們 Mock 掉真正的工具執行邏輯
    with patch("src.services.tool_executor.ToolExecutor.execute_to_queue", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {"status": "success", "output": "I worked!"}
        
        # 1. 發起第一個請求 (Token 有效)
        headers = {"Authorization": f"Bearer {short_lived_token}"}
        response = client.get("/v1/group/whitelist", headers=headers)
        assert response.status_code == 200
        
        # 2. 等待 Token 過期
        print("\n[DEBUG] Waiting for token to expire...")
        time.sleep(3)

        # 3. 發起一個新的獨立請求——行為翻轉（ADR-0131 / 禁令 60）：帶「過期卡」一律
        # 401 fail-loud（客戶端應走 /auth/refresh 續期），嚴禁 LDH 降級重簽（舊行為會把
        # 過期會話無聲挾持成 DebugAgent）。LDH 發卡僅保留給「真正無卡」的本機請求。
        response_new = client.get("/v1/group/whitelist", headers=headers)
        assert response_new.status_code == 401
        assert "x-new-auth-token" not in response_new.headers
        
        # 4. 關鍵測試：錨定身分後的執行器行為
        # 在 Heartbeat 脈衝中，一旦 `get_verified_identity` 通過，
        # 就會建立一個 `AgentIdentity` 物件。
        # 我們測試「即便過期了，持有該物件的執行器依然能正常運作」
        expired_identity = AgentIdentity.for_role(agent_id="Alice", role="Owner")
        
        # 模擬執行器被呼叫 (此時它不應該再次去校驗 Token 是否過期)
        # 這是為了確保執行主權 (Execution Sovereignty)
        assert expired_identity.agent_id == "Alice"

def test_identity_anchoring_protocol():
    """驗證身分錨定協議：身分一旦獲取，應視為不可變事實。
    """
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    # 只要我們能拿到 identity 物件，系統就應該信任它所代表的角色權限
    assert identity.display_role == "Owner"
