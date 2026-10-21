"""Integration test for Identity Isolation (Sovereignty Protection)."""

from src.services.resource_manager import resource_manager
from src.services.identity_service import identity_service


def test_vault_path_isolation(mock_identity):
    """驗證不同身分的 Vault 路徑確實隔離，且無法越權存取。"""
    # 1. 建立兩個不同的身分
    alice = mock_identity(agent_id="Alice", role="Owner")
    bob = mock_identity(agent_id="Bob", role="Owner")
    
    # 2. 獲取各自的 vault 路徑
    alice_vault = resource_manager.get_vault_path("soul", alice)
    bob_vault = resource_manager.get_vault_path("soul", bob)
    
    assert alice_vault is not None
    assert bob_vault is not None
    assert "Alice" in str(alice_vault)
    assert "Bob" in str(bob_vault)
    assert alice_vault != bob_vault

def test_identity_object_integrity_in_request(auth_client):
    """驗證請求進入後，Pipeline 確實將 Header 轉換為正確化身物件。"""
    response = auth_client.get("/v1/debug/tools")
    assert response.status_code == 200
    data = response.json()
    assert len(data.get("tools", [])) > 0

def test_unauthorized_agent_access(auth_client, mock_identity):
    """驗證未經授權的 Agent 無法獲得 Owner 權限。"""
    
    # 建立一個「陌生人」身分
    stranger = mock_identity(agent_id="Stranger", role="Base")
    stranger_token = identity_service.issue_token(stranger)
    
    # 清除依賴覆寫
    auth_client.app.dependency_overrides.clear()
    
    # 重要：必須同時更新 Token 與 X-Agent-ID，否則會觸發 Spoofing 攔截
    auth_client.headers.update({
        "Authorization": f"Bearer {stranger_token}",
        "X-Agent-ID": stranger.agent_id
    })
    
    # 嘗試存取需要 Owner 權限的端點
    response = auth_client.get("/v1/debug/tools")
    
    # 權限不足應返回 200 + 空清單 (根據 debug.py 邏輯)
    assert response.status_code == 200
    assert len(response.json().get("tools", [])) == 0
