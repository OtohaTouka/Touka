import json

import pytest
from src.services.autonomy import AutonomyService
from src.services.rhythm_service import RhythmService


@pytest.fixture
def autonomy_infra(tmp_path):
    """建立自主權測試基礎設施 (SSOT)"""
    config_path = tmp_path / "autonomy.json"
    
    config_data = {
        "quota": {"daily_max": 2, "reset_hour": 0},
        # ADR-0146：nudge 續圈已移除，react_loop 只剩工具輪保險絲
        "react_loop": {"max_tool_rounds": 0},
        "self_repair": {
            "enabled": True,
            "max_attempts": 2,
            "repair_prompt": "Repair {attempts}/{max_attempts}"
        }
    }
    config_path.write_text(json.dumps(config_data), encoding="utf-8")
    
    # 建立服務實例
    service = AutonomyService()
    service.config = config_data # 強制注入測試配置
    
    rhythm = RhythmService()
    
    return service, rhythm

def test_daily_quota_exhaustion(autonomy_infra, mock_identity):
    """驗證每日配額耗盡邏輯 (AutonomyService)"""
    service, _ = autonomy_infra
    agent = mock_identity(agent_id="QuotaTest", role="Base")
    
    # 初始應有配額
    assert service.check_quota(agent) is True
    
    # 消耗 1 次
    service.consume_quota(agent, "req_1")
    assert service.check_quota(agent) is True
    
    # 消耗 2 次 (達到上限)
    service.consume_quota(agent, "req_2")
    assert service.check_quota(agent) is False
    
    # 重置後應恢復
    service.reset_quota(agent)
    assert service.check_quota(agent) is True

def test_repair_budget_exhaustion(autonomy_infra):
    """驗證修復預算耗盡邏輯 (AutonomyService)"""
    service, _ = autonomy_infra
    sid = "test_session_123"
    
    # 第 1 次修復
    instr1 = service.get_repair_instruction(sid, 1)
    assert "Repair 1/2" in instr1
    
    # 第 2 次修復
    instr2 = service.get_repair_instruction(sid, 1)
    assert "Repair 2/2" in instr2
    
    # 第 3 次 (超出預算)
    instr3 = service.get_repair_instruction(sid, 1)
    assert instr3 is None
    
    # 重置後應可再次修復
    service.reset_repair_attempts(sid)
    instr_retry = service.get_repair_instruction(sid, 1)
    assert "Repair 1/2" in instr_retry
