import ast
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# 獲獲取專案根目錄 (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from lint_ssot import SSOTGuard  # noqa: E402
from src.services.models.manager import ModelManager


@pytest.fixture
def mock_config_io():
    with patch("src.services.models.manager.config_utils") as mock:
        yield mock

def test_model_manager_init_uses_config_utils(mock_config_io):
    """驗證 ModelManager 在初始化時是否呼叫了 config_utils 而非原始 open。"""
    # 設定 Mock 回傳值
    mock_config_io.read_config_json.return_value = {"models": []}
    mock_config_io.config_exists.return_value = True
    
    # 初始化 ModelManager
    ModelManager()
    
    # 驗證是否有呼叫 config_utils (預期在重構後通過)
    assert mock_config_io.read_config_json.called
    assert mock_config_io.config_exists.called

def test_model_manager_save_uses_config_utils(mock_config_io):
    """驗證 ModelManager 保存設定時是否呼叫了 config_utils。"""
    manager = ModelManager()
    manager._models_cache = [{"id": "test-model"}]
    
    manager._save_config()
    
    # 驗證是否有呼叫 save_config_json
    mock_config_io.save_config_json.assert_called_with("models.json", {"models": [{"id": "test-model"}]})

def test_model_manager_io_prison_compliance():
    """使用 SSOT Guard 檢查 ModelManager 是否仍有違規 (Meta-test)。"""
    
    target_file = Path("ToukaProxy/src/services/models/manager.py")
    with open(target_file, encoding="utf-8") as f:
        tree = ast.parse(f.read())
        visitor = SSOTGuard(target_file)
        visitor.visit(tree)
    
    # 重構後，這裡不應該有 IO 違規
    violations = [v for v in visitor.violations if v["rule"] == "IO_PRISON_VIOLATION"]
    assert len(violations) == 0, f"Found IO violations: {violations}"
