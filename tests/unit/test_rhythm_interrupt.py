"""中斷機制（#1 能真中斷）— rhythm_service 旗標單元測試。

驗證 interrupt/is_interrupted/clear_interrupt/clear_session 的行為，
這是「使用者能真的喊停正在跑的 agent」的底層狀態管理。
"""
from __future__ import annotations

from src.services.rhythm_service import rhythm_service


def test_interrupt_set_check_clear():
    sid = "test-interrupt-A"
    rhythm_service.clear_interrupt(sid)
    assert rhythm_service.is_interrupted(sid) is False
    rhythm_service.interrupt_session(sid)
    assert rhythm_service.is_interrupted(sid) is True
    rhythm_service.clear_interrupt(sid)
    assert rhythm_service.is_interrupted(sid) is False


def test_clear_session_also_clears_interrupt():
    """clear_session 的 docstring 承諾連中斷標記一併清除。"""
    sid = "test-interrupt-B"
    rhythm_service.interrupt_session(sid)
    assert rhythm_service.is_interrupted(sid) is True
    rhythm_service.clear_session(sid)
    assert rhythm_service.is_interrupted(sid) is False


def test_clear_interrupt_idempotent():
    sid = "test-interrupt-never-set"
    rhythm_service.clear_interrupt(sid)  # 未設過也不該拋錯
    assert rhythm_service.is_interrupted(sid) is False


def test_interrupt_is_per_session_isolated():
    a, b = "test-interrupt-iso-A", "test-interrupt-iso-B"
    rhythm_service.clear_interrupt(a)
    rhythm_service.clear_interrupt(b)
    rhythm_service.interrupt_session(a)
    assert rhythm_service.is_interrupted(a) is True
    assert rhythm_service.is_interrupted(b) is False
    rhythm_service.clear_interrupt(a)
