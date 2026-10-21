import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.services.identity_service import identity_service
from src.models.identity import AgentIdentity
from pathlib import Path
import json
from src.services.resource_manager import resource_manager
from src.services.models import model_manager

@pytest.fixture
def debug_client():
    # 獲取 Sandbox 路徑 (由 conftest.py 提供)
    base_path = resource_manager.get_base_path()
    if base_path:
        soul_dir = Path(base_path) / "soul"
        # 建立模擬的靈魂目錄 (SSOT)
        for s_id in ["Alice", "Bob", "Charlie"]:
            (soul_dir / s_id).mkdir(parents=True, exist_ok=True)
            # 建立 soul_service.get_soul_data 實際讀取的 config.json，且須含 is_soul，
            # 否則 is_toukasoul() 回 False、ToukaSoulXXX→XXX 的 ID 糾偏不會觸發
            # （隨機次序下本測試會因此單獨失敗。原 fixture 寫成 soul.json＝讀不到的死檔）。
            (soul_dir / s_id / "config.json").write_text('{"is_soul": true}')

        # 建立模擬的 models.json (SSOT)
        config_dir = Path(base_path) / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        models_json = {
            "models": [
                {"id": "ToukaSoulAlice", "name": "Ember (Alice)", "owned_by": "proxy", "enabled": True}
            ]
        }
        (config_dir / "models.json").write_text(json.dumps(models_json))

        # 強制重載 ModelManager 以讀取沙盒配置
        model_manager.load_initial_config()

    client = TestClient(app)
    # Issue a token for DebugAgent
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
    token = identity_service.issue_token(identity)
    client.headers = {"Authorization": f"Bearer {token}"}
    return client

def test_soul_models_presence(debug_client):
    """驗證靈魂系列模型是否存在且 ID 正確 (SSOT)."""
    response = debug_client.get("/v1/models")
    assert response.status_code == 200
    
    data = response.json()
    models = {m["id"]: m for m in data.get("data", [])}
    
    # 預期 ID 應為簡潔版 (SSOT)
    expected_souls = ["Alice", "Bob", "Charlie"]
    
    for s_id in expected_souls:
        assert s_id in models, f"Soul model {s_id} missing from model list"
        soul = models[s_id]
        assert soul["owned_by"] == "proxy"
        assert soul["enabled"] is True
        assert soul["status"] == "online"
        
        # 驗證名稱是否已從 models.json 成功合併 (若已修復)
        # 這裡先測試 ID 是否正確，名稱合併會在實作後驗證
        assert "Touka" in soul["name"] or s_id in soul["name"]

def test_legacy_id_mapping(debug_client):
    """驗證舊版 ID (ToukaSoulXXX) 的兼容性映射."""
    # 測試透過舊 ID 獲取模型資訊（ToukaSoulAlice → Alice）
    legacy_id = "ToukaSoulAlice"
    response = debug_client.get(f"/v1/models/{legacy_id}")

    # 實作前這裡可能會失敗或回傳 404
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "Alice"  # 應自動糾偏為正式 ID

if __name__ == "__main__":
    pytest.main([__file__])
