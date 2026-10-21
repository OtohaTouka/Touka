import json

import pytest
import src.services.autonomy as autonomy_mod
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.autonomy import autonomy_service
from src.services.identity_router import get_verified_identity


@pytest.fixture
def client(tmp_path, monkeypatch):
    """測試用的 TestClient 沙盒化配置"""
    # 模擬配置路徑，避免修改實體系統設定檔
    temp_config = tmp_path / "autonomy.json"
    default_data = {
        "global_config": {
            "max_pulses_per_session": 5
        },
        # ADR-0146：react_loop 新形＝工具輪保險絲（0＝互動不限）
        "react_loop": {"max_tool_rounds": 0},
        "self_repair": {
            "enabled": True,
            "max_attempts": 3,
            "repair_prompt": "Repair {attempts}/{max_attempts}"
        }
    }
    temp_config.write_text(json.dumps(default_data), encoding="utf-8")
    
    monkeypatch.setattr(autonomy_mod, "CONFIG_FILE", temp_config)
    autonomy_service.reload_all()
    
    # 模擬管理員身分認證
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")
    app.dependency_overrides[get_verified_identity] = lambda: identity
    
    with TestClient(app) as c:
        yield c
        
    app.dependency_overrides.clear()
    autonomy_service.reload_all()


def test_get_autonomy_config_populates_defaults(client):
    """驗證 GET /v1/autonomy 會自動填充缺失的 default 值（如 sandbox_limits）"""
    resp = client.get("/v1/autonomy")
    assert resp.status_code == 200
    
    data = resp.json()
    assert "global_config" in data
    assert "react_loop" in data
    assert "self_repair" in data
    assert "sandbox_limits" in data
    
    # 檢查預設值是否正確填充 (Pydantic model defaults)
    assert data["sandbox_limits"]["execution_timeout_sec"] == 30
    assert data["sandbox_limits"]["max_storage_mb"] == 50
    assert data["sandbox_limits"]["max_memory_mb"] == 256

    # ADR-0146：root 遺留鍵已拆除、react_loop 只剩工具輪保險絲
    assert "max_react_slots" not in data
    assert "max_repair_attempts" not in data
    assert data["react_loop"] == {"max_tool_rounds": 0}


def test_post_autonomy_config_success(client):
    """驗證以完整 Pydantic 巢狀結構 POST 時，能成功存檔並讀回"""
    new_data = {
        "global_config": {
            "max_pulses_per_session": 10
        },
        "react_loop": {"max_tool_rounds": 20},
        "self_repair": {
            "enabled": True,
            "max_attempts": 8,
            "repair_prompt": "Repair {attempts}/{max_attempts}"
        },
        "sandbox_limits": {
            "execution_timeout_sec": 45,
            "max_storage_mb": 100,
            "max_memory_mb": 512
        }
    }

    post_resp = client.post("/v1/autonomy", json=new_data)
    assert post_resp.status_code == 200
    assert post_resp.json()["status"] == "success"

    # 再次讀回驗證變更
    get_resp = client.get("/v1/autonomy")
    assert get_resp.status_code == 200

    get_data = get_resp.json()
    assert get_data["global_config"]["max_pulses_per_session"] == 10
    assert get_data["react_loop"]["max_tool_rounds"] == 20
    assert get_data["self_repair"]["max_attempts"] == 8
    assert get_data["sandbox_limits"]["max_memory_mb"] == 512
