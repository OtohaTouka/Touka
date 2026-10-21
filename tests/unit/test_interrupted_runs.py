"""P5c：當機復原可見化 — heartbeat in-flight 標記的落盤/清除與啟動掃描。

當機/被強制關閉時，背景多脈衝迴圈的 in-flight 標記會殘留在 autonomy_log.json，
供下次啟動掃描偵測「上次有未跑完的背景任務」（偵測＋顯示＋清 stale，非自動續跑）。
"""
from __future__ import annotations

import pytest
from src.models.identity import AgentIdentity
from src.services.autonomy import AutonomyService
from src.services.resource_manager import resource_manager


@pytest.fixture
def patched_data_dir(tmp_path, monkeypatch):
    """將 app data dir 導向 tmp，使 stats 路徑落在隔離沙箱（不碰真實資料）。"""
    monkeypatch.setattr(
        "src.services.resource_manager.get_app_data_dir", lambda: str(tmp_path)
    )
    return tmp_path


def _identity(agent_id: str) -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=agent_id, role="Owner")


def test_mark_then_clear_in_flight(patched_data_dir):
    """落盤標記後可讀到；清除後標記消失。"""
    svc = AutonomyService()
    identity = _identity("MarkerAgent")
    svc.mark_heartbeat_in_flight(
        identity,
        {"session_id": "s1", "agent": "MarkerAgent", "total": 3, "started_at": 100.0},
    )
    log_file = patched_data_dir / "stats" / "MarkerAgent" / "autonomy_log.json"
    data = resource_manager.read_json(log_file)
    assert data["heartbeat_in_flight"]["total"] == 3
    assert data["heartbeat_in_flight"]["session_id"] == "s1"

    svc.clear_heartbeat_in_flight(identity)
    data2 = resource_manager.read_json(log_file)
    assert "heartbeat_in_flight" not in data2


def test_mark_preserves_existing_quota_fields(patched_data_dir):
    """落盤標記不可覆蓋既有配額欄位（daily_count 等）。"""
    svc = AutonomyService()
    identity = _identity("MixAgent")
    log_file = patched_data_dir / "stats" / "MixAgent" / "autonomy_log.json"
    resource_manager.write_json(
        log_file, {"daily_count": 7, "last_used_time": "2026-06-09T10:00:00"}
    )
    svc.mark_heartbeat_in_flight(identity, {"session_id": "s9", "total": 1})
    data = resource_manager.read_json(log_file)
    assert data["daily_count"] == 7
    assert data["heartbeat_in_flight"]["session_id"] == "s9"


def test_clear_preserves_other_fields(patched_data_dir):
    """清除標記只移除 heartbeat_in_flight，其餘欄位保留。"""
    svc = AutonomyService()
    identity = _identity("KeepAgent")
    log_file = patched_data_dir / "stats" / "KeepAgent" / "autonomy_log.json"
    resource_manager.write_json(
        log_file,
        {"daily_count": 4, "heartbeat_in_flight": {"session_id": "sx", "total": 2}},
    )
    svc.clear_heartbeat_in_flight(identity)
    data = resource_manager.read_json(log_file)
    assert "heartbeat_in_flight" not in data
    assert data["daily_count"] == 4


def test_scan_detects_and_clears_stale_markers(patched_data_dir):
    """啟動掃描：收集殘留標記、清除 stale、結果存入服務供 /stats 讀取。"""
    stats_root = patched_data_dir / "stats"
    # 兩個有殘留標記（=上次當機未跑完）、一個乾淨
    resource_manager.write_json(
        stats_root / "Alice" / "autonomy_log.json",
        {
            "daily_count": 2,
            "heartbeat_in_flight": {"session_id": "sa", "total": 5, "agent": "Alice"},
        },
    )
    resource_manager.write_json(
        stats_root / "Bob" / "autonomy_log.json",
        {"heartbeat_in_flight": {"session_id": "sb", "total": 2}},  # 無 agent 欄位
    )
    resource_manager.write_json(
        stats_root / "Carol" / "autonomy_log.json",
        {"daily_count": 1},
    )
    svc = AutonomyService()
    found = svc.scan_and_clear_interrupted()

    assert len(found) == 2
    agents = {e.get("agent") for e in found}
    assert agents == {"Alice", "Bob"}  # Bob 缺 agent → 由目錄名補上

    # stale 標記已清除，但其他欄位保留
    a = resource_manager.read_json(stats_root / "Alice" / "autonomy_log.json")
    assert "heartbeat_in_flight" not in a
    assert a["daily_count"] == 2

    # 結果存入服務、供 /stats 讀取
    assert svc.get_interrupted_runs() == found


def test_scan_empty_when_no_markers(patched_data_dir):
    """無殘留標記時回空清單。"""
    resource_manager.write_json(
        patched_data_dir / "stats" / "Solo" / "autonomy_log.json", {"daily_count": 0}
    )
    svc = AutonomyService()
    assert svc.scan_and_clear_interrupted() == []
    assert svc.get_interrupted_runs() == []


def test_scan_handles_missing_stats_root(patched_data_dir):
    """stats 根目錄不存在 → 回空、不拋例外。"""
    svc = AutonomyService()
    assert svc.scan_and_clear_interrupted() == []
    assert svc.get_interrupted_runs() == []
