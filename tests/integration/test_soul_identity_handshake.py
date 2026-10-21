import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.services.auth_service import AuthService
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager


def test_soul_access_identity_handshake_local_default(global_test_sandbox):
    """測試在本地環境下，未提供 X-Agent-ID 時應自動升級為 DebugAgent 並可存取靈魂。"""
    
    # 1. 準備：在沙盒中建立一個靈魂目錄
    app_data_dir = Path(global_test_sandbox)
    soul_dir = app_data_dir / "soul" / "Alice"
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "soul.md").write_text("# Alice's Soul", encoding="utf-8")
    (soul_dir / "config.json").write_text(json.dumps({"is_toukasoul": True}), encoding="utf-8")
    
    # 重置單例
    resource_manager.reset_for_test()
    
    # 2. Mock 權限服務：確保 DebugAgent 擁有 Debugger 角色且獲准存取
    # 這裡我們直接對單例進行 Monkeypatch
    original_get_role = permission_service.get_role_key
    
    def mock_get_role(identity, **kwargs):
        agent_id = identity.agent_id if hasattr(identity, 'agent_id') else str(identity)
        if agent_id == "DebugAgent":
            return "Debugger"
        return "Base"
        
    permission_service.get_role_key = mock_get_role
    
    # 3. 執行：使用 TestClient 發送本地請求，不帶 X-Agent-ID
    # 修復 3（ADR-0144）：LDH 僅限「系統未認領」——顯式清空沙箱 users.json（TestClient
    # lifespan 會補回「暫時密碼 admin」＝仍未認領），避免繼承其他測試建立的真密碼
    # 使用者（已認領→LDH 關閉→401）造成隨機次序 flaky。
    AuthService()._atomic_users_write(lambda users: users.clear())  # noqa: SLF001
    with TestClient(app) as client:
        # 模擬本地 Host
        headers = {"Host": "localhost"}
        response = client.get("/v1/souls", headers=headers)
        
        # 4. 驗證
        assert response.status_code == 200
        data = response.json()
        soul_ids = [s["id"] for s in data.get("souls", [])]
        
        print(f"\n[Handshake Test] Found souls: {soul_ids}")
        
        # 恢復
        permission_service.get_role_key = original_get_role
        
        assert "Alice" in soul_ids, f"Should be able to see Alice's soul (Found: {soul_ids})"

if __name__ == "__main__":
    pytest.main([__file__])
