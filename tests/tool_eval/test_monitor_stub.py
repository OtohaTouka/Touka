"""長指令 during 監控案例——形狀測試（P0-C stub 已於 P2b 轉正進 SEED_CASES）.

P2b（execute_command 背景執行＋get_command_result 部分輸出/停滯訊號＋stop_command）
已落地，故 long-run-monitor 由 MONITOR_STUB_CASES 移入 SEED_CASES 轉正。本測試改為
驗證它在 SEED 內的期望 episode 形狀（背景跑→見 stalled→收手）。
"""

from __future__ import annotations

from .cases import MONITOR_STUB_CASES, SEED_CASES


def test_monitor_case_promoted_to_seed():
    case = next((c for c in SEED_CASES if c.id == "long-run-monitor"), None)
    assert case is not None, "long-run-monitor 應已轉正進 SEED_CASES"
    # 三工具皆為正確流程 → 全列 expected（避免被記為 wrong_tool）
    assert case.expected_tools == ["execute_command", "get_command_result", "stop_command"]


def test_monitor_case_episode_shape():
    case = next(c for c in SEED_CASES if c.id == "long-run-monitor")
    assert case.recorded_turns is not None
    names = [c.name for turn in case.recorded_turns for c in turn]
    assert names == ["execute_command", "get_command_result", "stop_command"]
    # 背景旗標＋停滯訊號的腳本化中間態
    exec_call = case.recorded_turns[0][0]
    assert exec_call.args.get("background") is True
    assert "stalled" in case.simulated_results["get_command_result"]


def test_monitor_stub_list_now_empty():
    """轉正後 stub 清單清空（保留匯入點向後相容）。"""
    assert MONITOR_STUB_CASES == []
