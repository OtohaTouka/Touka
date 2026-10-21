from datetime import UTC, datetime

from ToukaHeartbeat.awaken import HeartbeatSchedule, get_taipei_time_info
from ToukaHeartbeat.storage_service import StorageService


def test_get_taipei_time_info():
    """驗證台北時間轉換邏輯 (Presentation Layer)."""
    # 測試固定時間點: 2026-01-01 00:00:00 UTC
    ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC).timestamp()
    time_str, weekday = get_taipei_time_info(ts)
    
    # 台北應為 2026-01-01 08:00:00 (UTC+8)
    assert time_str == "2026-01-01 08:00:00"
    assert weekday == "週四"

KNOWN_INSECURE_TOKEN = "touka_heartbeat_secret_2026"  # noqa: S105


def test_storage_service_load_save(tmp_path):
    """驗證 StorageService 的持久化功能。

    M6 fix: initial config must contain a RANDOM token, not the hardcoded default.
    """
    config_file = tmp_path / "config.json"
    storage = StorageService(config_file)

    # 測試初始載入 (Bootstrap) — M6 fix: token must be random, not the insecure default
    config = storage.load_config()
    assert config["heartbeat_token"] != KNOWN_INSECURE_TOKEN, (
        "M6 fix: initial heartbeat_token must not be the known-insecure default"
    )
    assert len(config["heartbeat_token"]) >= 16, "Token must be at least 16 chars"
    assert config["schedules"] == []
    
    # 測試寫入與讀取
    test_data = {"schedules": [{"id": "test1", "agent_id": "Alice"}]}
    storage.save_config(test_data)
    
    loaded = storage.load_config()
    assert loaded["schedules"][0]["agent_id"] == "Alice"

def test_heartbeat_schedule_model():
    """驗證排程資料模型的預設值與驗證。"""
    sched = HeartbeatSchedule(agent_id="DebugAgent")
    assert sched.active is True
    assert sched.mode == "interval"
    assert sched.interval_minutes == 60
    assert sched.pulse_count == 1
    
    # 驗證 model_dump
    data = sched.model_dump()
    assert data["agent_id"] == "DebugAgent"
    assert "id" in data
