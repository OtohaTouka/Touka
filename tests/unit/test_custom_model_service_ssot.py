import ast
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# 獲取專案根目錄 (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from lint_ssot import SSOTGuard  # noqa: E402
from src.models.identity import AgentIdentity
from src.services.custom_model_service import CustomModelService


@pytest.fixture
def custom_service():
    return CustomModelService()

@pytest.fixture
def debug_identity():
    return AgentIdentity.for_role(agent_id="TestCustomModel", role="Base")

def test_get_model_data_delegation(custom_service, debug_identity):
    """測試讀取模型資料是否導向至 custom_io。"""
    mock_prompt = "System Prompt"
    mock_config = '{"name": "Test"}'
    
    with patch("src.services.custom_model_service.custom_io.read_custom_file") as mock_read:
        # 第一遍讀取 prompt, 第二遍讀取 config
        mock_read.side_effect = [mock_prompt, mock_config]
        
        prompt, config = custom_service.get_model_data(debug_identity)
        
        assert prompt == mock_prompt
        assert config["name"] == "Test"
        assert mock_read.call_count == 2

def test_save_model_delegation(custom_service, debug_identity):
    """測試儲存模型是否導向至 custom_io。"""
    with patch("src.services.custom_model_service.custom_io.write_custom_file") as mock_write:
        mock_write.return_value = True
        
        success = custom_service.save_model(
            identity=debug_identity,
            name="New Name",
            description="desc",
            system_prompt="prompt",
            config={}
        )
        
        assert success is True
        # 應寫入 prompt 與 config
        assert mock_write.call_count == 2
        # 驗證第二次寫入的是 config.json
        args, _ = mock_write.call_args
        assert args[1] == "config.json"

def test_custom_model_service_ssot_compliance():
    """Meta-test: 確保 custom_model_service.py 中不再包含禁用的 IO 呼叫。"""
    
    file_path = Path("ToukaProxy/src/services/custom_model_service.py")
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    visitor = SSOTGuard(file_path)
    visitor.visit(tree)
    
    violations = [v for v in visitor.violations if v['rule'] == 'IO_PRISON_VIOLATION']
    assert len(violations) == 0, f"Found IO violations in custom_model_service.py: {violations}"
