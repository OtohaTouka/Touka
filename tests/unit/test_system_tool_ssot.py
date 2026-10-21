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
from src.tools.builtin.system import agent_control, update_my_soul


@pytest.fixture
def debug_identity() -> AgentIdentity:
    return AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")

@pytest.mark.asyncio
async def test_update_my_soul_read(debug_identity):
    """測試讀取靈魂檔案是否導向至 soul_io。"""
    mock_content = "1: line one\n2: line two"
    with patch("src.utils.soul_io.read_soul_file") as mock_read:
        mock_read.return_value = mock_content
        
        result = await update_my_soul(
            identity=debug_identity,
            type="soul",
            action="read"
        )
        
        assert "line one" in result
        mock_read.assert_called_once_with(debug_identity, "soul.md")

@pytest.mark.asyncio
async def test_update_my_soul_write_protection(debug_identity):
    """測試寫入保護（內容過短）。"""
    result = await update_my_soul(
        identity=debug_identity,
        type="soul",
        action="write",
        content="too short"
    )
    assert "內容過短" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("total_rounds", "used_rounds", "expected_remaining"),
    [(8, 3, 5), (2, 5, 0)],
)
async def test_agent_control_status_uses_rhythm_tool_round_budget(
    debug_identity: AgentIdentity,
    total_rounds: int,
    used_rounds: int,
    expected_remaining: int,
) -> None:
    with (
        patch("src.tools.builtin.system.get_active_brain", return_value="smoke-brain"),
        patch(
            "src.tools.builtin.system.rhythm_service.get_total_rounds",
            return_value=total_rounds,
        ) as get_total_rounds,
        patch(
            "src.tools.builtin.system.rhythm_service.get_used_rounds",
            return_value=used_rounds,
        ) as get_used_rounds,
    ):
        result = await agent_control(
            action="get_status",
            identity=debug_identity,
            request_id="status-probe",
        )

    assert f"Remaining Tool Rounds: {expected_remaining}" in result
    assert "Autonomy Slots" not in result
    get_total_rounds.assert_called_once_with("status-probe")
    get_used_rounds.assert_called_once_with("status-probe")

def test_system_tool_ssot_compliance():
    """Meta-test: 確保 system.py 中不再包含禁用的 IO 呼叫。"""
    
    file_path = Path("ToukaProxy/src/tools/builtin/system.py")
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    visitor = SSOTGuard(file_path)
    visitor.visit(tree)
    
    # 這裡預期 violations 為空
    violations = [v for v in visitor.violations if v['rule'] == 'IO_PRISON_VIOLATION']
    assert len(violations) == 0, f"Found IO violations in system.py: {violations}"
