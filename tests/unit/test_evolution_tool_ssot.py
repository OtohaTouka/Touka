import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 獲取專案根目錄 (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from lint_ssot import SSOTGuard  # noqa: E402
from src.tools.builtin.evolution import propose_new_capability, write_mcp_implementation


@pytest.mark.asyncio
async def test_propose_new_capability_delegation():
    """測試提案能力是否正確導向至 EvolutionPathGuard。"""
    skill_id = "test_skill"
    name = "Test Skill"
    
    with patch("src.tools.builtin.evolution.EvolutionPathGuard.mkdir_tool") as mock_mkdir, \
         patch("src.tools.builtin.evolution.EvolutionPathGuard.write_tool_file") as mock_write:
        
        result = await propose_new_capability(
            skill_id=skill_id,
            name=name,
            description="test",
            schema={}
        )
        
        assert "proposed" in result
        mock_mkdir.assert_called_once_with(skill_id)
        mock_write.assert_called_once()
        # 驗證寫入的是 spec.json
        args, _ = mock_write.call_args
        assert args[0] == skill_id
        assert args[1] == "spec.json"

@pytest.mark.asyncio
async def test_write_mcp_implementation_delegation():
    """測試實作寫入是否正確導向至 EvolutionPathGuard。"""
    skill_id = "test_skill"
    code = "print('hello')"
    
    with patch("src.tools.builtin.evolution.EvolutionPathGuard.write_tool_file") as mock_write, \
         patch("src.tools.builtin.evolution.EvolutionPathGuard.tool_file_exists") as mock_exists, \
         patch("src.tools.builtin.evolution.EvolutionPathGuard.run_sandbox_command") as mock_run:
        
        mock_exists.return_value = False
        mock_run.return_value = MagicMock(stdout="Success", stderr="", returncode=0)
        
        result = await write_mcp_implementation(
            skill_id=skill_id,
            code=code,
            dependencies=["requests"]
        )
        
        assert "Development Success" in result
        # 應寫入 server.py 與 requirements.txt
        assert mock_write.call_count == 2
        # 應執行 venv 建立與 pip 安裝
        assert mock_run.call_count == 2

def test_evolution_tool_ssot_compliance():
    """Meta-test: 確保 evolution.py 中不再包含禁用的 IO 呼叫。"""
    
    file_path = Path("ToukaProxy/src/tools/builtin/evolution.py")
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    visitor = SSOTGuard(file_path)
    visitor.visit(tree)
    
    violations = [v for v in visitor.violations if v['rule'] == 'IO_PRISON_VIOLATION']
    assert len(violations) == 0, f"Found IO violations in evolution.py: {violations}"
