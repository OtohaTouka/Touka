import json
from pathlib import Path
from unittest.mock import patch

import pytest
from src.config import get_app_data_dir, settings
from src.services.agent_service import agent_service
from src.services.resource_manager import resource_manager


@pytest.fixture
def isolation_env(tmp_path, monkeypatch):
    """建立隔離測試環境 (SSOT)"""
    # 設置 settings 屬性，讓 get_app_data_dir 指向 tmp_path
    monkeypatch.setattr(settings, "MEMORY_APP_DATA_DIR", str(tmp_path))
    # 重置 resource_manager 快取，確保它讀取新的 MEMORY_APP_DATA_DIR
    resource_manager.reset_for_test()

    # 建立目錄結構（在 tmp_path 下）
    soul_dir = tmp_path / "soul"
    custom_dir = tmp_path / "custom"

    (soul_dir / "Alice").mkdir(parents=True, exist_ok=True)
    (soul_dir / "Alice" / "config.json").write_text(
        json.dumps({"is_toukasoul": True, "name": "Alice"}), encoding="utf-8"
    )
    (soul_dir / "Bob").mkdir(parents=True, exist_ok=True)
    (soul_dir / "Bob" / "config.json").write_text(
        json.dumps({"is_toukasoul": True, "name": "Bob"}), encoding="utf-8"
    )
    
    girlg_dir = custom_dir / "GirlG"
    girlg_dir.mkdir(parents=True, exist_ok=True)
    (girlg_dir / "config.json").write_text(json.dumps({"name": "GirlG", "is_custom": True}), encoding="utf-8")
    
    # soul_service 使用 memory_server.config_utils.get_app_data_dir（獨立路徑）
    # 透過 mock 讓它也指向 tmp_path
    with patch("src.services.soul_service.config_utils.get_app_data_dir", return_value=str(tmp_path)):
        yield agent_service, resource_manager

def test_soul_identification(isolation_env):
    """Level 1 靈魂應被識別為 is_toukasoul=True, is_custom=False"""
    service, _ = isolation_env
    result = service.identify_agent("Alice")
    assert result is not None
    assert result["is_toukasoul"] is True
    assert result["is_custom"] is False

def test_custom_model_identification(isolation_env):
    """Level 2 自定義模型應被識別為 is_toukasoul=False, is_custom=True"""
    service, _ = isolation_env
    result = service.identify_agent("GirlG")
    assert result is not None
    assert result["is_toukasoul"] is False
    assert result["is_custom"] is True

def test_vault_path_isolation(isolation_env, mock_identity):
    """驗證不同身分的 Vault 路徑隔離"""
    _, rm = isolation_env
    
    alice = mock_identity(agent_id="Alice", role="Owner")
    bob = mock_identity(agent_id="Bob", role="Owner")
    girlg = mock_identity(agent_id="GirlG", role="Base")  # ADR-0245：自訂模型改落 Base
    
    alice_path = rm.get_vault_path("soul", alice)
    bob_path = rm.get_vault_path("soul", bob)
    girlg_personal = rm.get_vault_path("personal", girlg)
    
    # 獲取目前的 app_data
    app_data = get_app_data_dir()
    
    assert str(alice_path).replace("\\", "/").endswith("soul/Alice")
    assert str(bob_path).replace("\\", "/").endswith("soul/Bob")
    assert alice_path != bob_path
    
    # 自定義模型不應有 soul vault
    assert rm.get_vault_path("soul", girlg) is None
    
    # 自定義模型的 personal vault 應在 custom/ 下
    assert "custom" in str(girlg_personal).replace("\\", "/")
    assert "GirlG" in str(girlg_personal).replace("\\", "/")

def test_physical_path_ban_compliance(isolation_env, mock_identity):
    """驗證 ResourceManager 是否符合物理路徑禁令 (回傳路徑而非硬編碼字串)"""
    _, rm = isolation_env
    alice = mock_identity(agent_id="Alice", role="Owner")
    
    path = rm.get_vault_path("soul", alice)
    # 確保是字串或 Path (視 API 定義而定，目前 get_vault_path 回傳 str | None)
    assert path is not None
    
    # 確保包含目前的動態 app_data_dir
    app_data = get_app_data_dir()
    assert str(path).startswith(str(app_data))
