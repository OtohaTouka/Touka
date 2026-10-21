"""Unit tests for RhythmService logic（ADR-0146 工具輪預算語意）。

nudge 續圈（get_nudge_prompt/should_stop_react/decrement_slots）已移除——
純文字＝回合結束；本檔改測工具輪保險絲的解析與消耗（詳細矩陣另見
tests/integration/test_tool_loop_budget.py）。
"""

import pytest
from src.services.rhythm_service import HEADLESS_FALLBACK_ROUNDS, RhythmService


@pytest.fixture
def rhythm_svc():
    """建立 RhythmService 實例。"""
    return RhythmService()

def test_get_effective_rounds_priority(rhythm_svc):
    """驗證工具輪上限優先級：顯式 > 全域保險絲 >（無人）保底。"""
    config = {"react_loop": {"max_tool_rounds": 10}}

    # 1. 顯式指定優先
    assert rhythm_svc.get_effective_rounds(config, 15, headless=True) == 15

    # 2. 全域保險絲次之
    assert rhythm_svc.get_effective_rounds(config, None, headless=False) == 10

    # 3. 互動預設不限（0）；無人強制保底
    assert rhythm_svc.get_effective_rounds({}, None, headless=False) == 0
    assert rhythm_svc.get_effective_rounds({}, None, headless=True) == HEADLESS_FALLBACK_ROUNDS

def test_session_round_management(rhythm_svc):
    """驗證工具輪預算的初始化與消耗邏輯。"""
    req_id = "test_req_1"
    rhythm_svc.initialize_session(req_id, 3)

    assert rhythm_svc.get_total_rounds(req_id) == 3
    assert rhythm_svc.consume_round(req_id) is True
    assert rhythm_svc.get_used_rounds(req_id) == 1

    # 扣到零 → 黏著拒絕
    assert rhythm_svc.consume_round(req_id) is True
    assert rhythm_svc.consume_round(req_id) is True
    assert rhythm_svc.consume_round(req_id) is False
    assert rhythm_svc.consume_round(req_id) is False

def test_unlimited_and_uninitialized_never_exhaust(rhythm_svc):
    """0＝不限；未初始化＝防禦性不限（不得把正常請求誤判為耗盡）。"""
    rhythm_svc.initialize_session("unlimited", 0)
    assert all(rhythm_svc.consume_round("unlimited") for _ in range(50))
    assert rhythm_svc.consume_round("never-initialized") is True

def test_interrupt_logic(rhythm_svc):
    """驗證中斷標記邏輯。"""
    session_id = "sess_999"
    assert rhythm_svc.is_interrupted(session_id) is False

    rhythm_svc.interrupt_session(session_id)
    assert rhythm_svc.is_interrupted(session_id) is True

    rhythm_svc.clear_session(session_id)
    assert rhythm_svc.is_interrupted(session_id) is False
