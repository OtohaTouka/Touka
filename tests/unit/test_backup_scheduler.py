"""ADR-0109 — 自動備份排程器（TDD）。

Covers:
  - is_backup_due 純函式：停用/未到時刻/已跑過/隔日/weekly 星期
  - prune_old_auto_backups：只修剪自動備份、保留最新 N、人工備份不受影響
  - create_backup(auto=) 檔名前綴
  - GET/POST /system/backup/schedule（admin 讀寫、非-admin 403、保留 last_run、格式 422）
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime

from fastapi.testclient import TestClient
from src.main import app
from src.models.backup_schedule import BackupScheduleConfig
from src.models.identity import AgentIdentity
from src.routers import system as sysrouter
from src.services import backup_scheduler_service as sched_module
from src.services import system_backup
from src.services.backup_scheduler_service import (
    _TAIPEI as _TPE,  # 與服務同一 tz（tzdata 缺漏時退回固定 UTC+8）
)
from src.services.backup_scheduler_service import (
    backup_scheduler,
    is_backup_due,
    load_schedule_config,
    prune_old_auto_backups,
    save_schedule_config,
)
from src.services.identity_router import get_verified_identity
from src.services.system_backup import AUTO_BACKUP_PREFIX, create_backup


def _epoch(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=_TPE).timestamp()


def _clear():
    app.dependency_overrides.clear()


def _admin():
    return AgentIdentity.for_role(agent_id="Admin", role="admin", is_human=True)


def _normal():
    return AgentIdentity.for_role(agent_id="u", role="normal", is_human=True)


# ── is_backup_due ────────────────────────────────────────────────────────────

def test_due_false_when_disabled():
    cfg = BackupScheduleConfig(enabled=False, time_of_day="03:00")
    assert is_backup_due(cfg, _epoch(2026, 6, 23, 4, 0)) is False


def test_due_false_before_time():
    cfg = BackupScheduleConfig(enabled=True, frequency="daily", time_of_day="03:00")
    assert is_backup_due(cfg, _epoch(2026, 6, 23, 2, 59)) is False


def test_due_true_after_time_first_run():
    cfg = BackupScheduleConfig(enabled=True, frequency="daily", time_of_day="03:00")
    assert is_backup_due(cfg, _epoch(2026, 6, 23, 3, 30)) is True


def test_due_false_already_ran_this_window():
    cfg = BackupScheduleConfig(
        enabled=True, frequency="daily", time_of_day="03:00",
        last_run_epoch=_epoch(2026, 6, 23, 3, 5),
    )
    assert is_backup_due(cfg, _epoch(2026, 6, 23, 3, 30)) is False


def test_due_true_next_day():
    cfg = BackupScheduleConfig(
        enabled=True, frequency="daily", time_of_day="03:00",
        last_run_epoch=_epoch(2026, 6, 23, 3, 5),
    )
    assert is_backup_due(cfg, _epoch(2026, 6, 24, 3, 30)) is True


def test_weekly_only_on_target_weekday():
    now = _epoch(2026, 6, 23, 3, 30)  # whatever weekday this is
    wd = datetime.fromtimestamp(now, _TPE).weekday()
    on = BackupScheduleConfig(enabled=True, frequency="weekly", time_of_day="03:00", weekday=wd)
    off = BackupScheduleConfig(
        enabled=True, frequency="weekly", time_of_day="03:00", weekday=(wd + 1) % 7
    )
    assert is_backup_due(on, now) is True
    assert is_backup_due(off, now) is False


# ── prune_old_auto_backups ───────────────────────────────────────────────────

def test_prune_keeps_latest_n_auto_only(tmp_path):
    for i in range(5):
        f = tmp_path / f"{AUTO_BACKUP_PREFIX}_2026010{i}_0300.zip"
        f.write_bytes(b"x")
        os.utime(f, (1000 + i, 1000 + i))  # 遞增 mtime → i=0 最舊
    humans = []
    for i in range(2):
        f = tmp_path / f"Touka_Backup_2026010{i}_0300.zip"
        f.write_bytes(b"y")
        os.utime(f, (500 + i, 500 + i))
        humans.append(f)

    deleted = prune_old_auto_backups(tmp_path, 3)

    assert len(deleted) == 2  # 5 自動 − 保留 3 = 刪 2 最舊
    assert len(list(tmp_path.glob(f"{AUTO_BACKUP_PREFIX}_*.zip"))) == 3
    for h in humans:  # 人工備份完全不受影響、不計入保留
        assert h.exists()


def test_prune_drops_empty_auto_backups_and_does_not_count_them(tmp_path):
    """0 位元組的自動備份（發布失敗留下的空檔）不算份數，直接刪；人工備份的空檔不動（丙-1）。"""
    real = []
    for i in range(3):
        f = tmp_path / f"{AUTO_BACKUP_PREFIX}_2026010{i}_0300.zip"
        f.write_bytes(b"x")
        os.utime(f, (1000 + i, 1000 + i))
        real.append(f)
    empty = tmp_path / f"{AUTO_BACKUP_PREFIX}_20260109_0300.zip"
    empty.write_bytes(b"")
    os.utime(empty, (2000, 2000))  # newest, so it would push out a real one if it counted
    human_empty = tmp_path / "Touka_Backup_20260109_0300.zip"
    human_empty.write_bytes(b"")

    deleted = prune_old_auto_backups(tmp_path, 3)

    assert deleted == [empty]
    assert all(f.exists() for f in real)
    assert human_empty.exists()


def test_prune_deletes_the_oldest_by_modification_time(tmp_path):
    """修剪看的是修改時間，不是檔名：檔名排序和時間排序故意相反（丙-1 審查補）。"""
    files = []
    for i in range(5):
        f = tmp_path / f"{AUTO_BACKUP_PREFIX}_2026010{i}_0300.zip"
        f.write_bytes(b"x")
        os.utime(f, (2000 - i, 2000 - i))  # 檔名越大越舊
        files.append(f)

    deleted = prune_old_auto_backups(tmp_path, 3)

    assert deleted == [files[4], files[3]]  # oldest first
    assert [f.exists() for f in files] == [True, True, True, False, False]


def test_prune_noop_when_under_retention(tmp_path):
    f = tmp_path / f"{AUTO_BACKUP_PREFIX}_20260101_0300.zip"
    f.write_bytes(b"x")
    assert prune_old_auto_backups(tmp_path, 7) == []
    assert f.exists()


# ── _run_once 失敗仍消耗窗口（複審 #2：不每小時重試洗版） ──────────────────────

async def test_run_once_records_window_even_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.services.backup_scheduler_service.SCHEDULE_CONFIG_FILE",
        tmp_path / "backup_schedule.json",
    )
    monkeypatch.setattr("src.services.backup_scheduler_service.backups_dir", lambda: tmp_path)
    save_schedule_config(BackupScheduleConfig(enabled=True, last_run_epoch=None))

    def _boom(**_k):
        raise RuntimeError  # 模擬備份失敗（磁碟滿/腳本缺）

    monkeypatch.setattr("src.services.backup_scheduler_service.create_backup", _boom)

    await backup_scheduler._run_once(load_schedule_config())  # 不應拋出
    # 失敗仍記錄「本窗口已嘗試」→ is_backup_due 後續為 False，不會每小時重試
    assert load_schedule_config().last_run_epoch is not None


# ── create_backup(auto=) prefix ──────────────────────────────────────────────

def test_create_backup_auto_uses_distinct_prefix(monkeypatch, tmp_path):
    # 在沙盒 workspace 下備份腳本不存在 → 樁住 backup_script 與 subprocess.run。
    stub = tmp_path / "manage_backup.py"
    stub.write_text("# stub")
    monkeypatch.setattr("src.services.system_backup.backup_script", lambda: stub)
    captured = {}

    class _Result:
        stdout = "ok"

    def fake_run(cmd, **_kw):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr("src.services.system_backup.subprocess.run", fake_run)

    create_backup(auto=True)
    assert "--prefix" in captured["cmd"]
    assert AUTO_BACKUP_PREFIX in captured["cmd"]

    captured.clear()
    create_backup()  # 人工：無 prefix（用預設 Touka_Backup）
    assert "--prefix" not in captured["cmd"]


# ── endpoints ────────────────────────────────────────────────────────────────

def test_get_backup_schedule_default(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.services.backup_scheduler_service.SCHEDULE_CONFIG_FILE",
        tmp_path / "backup_schedule.json",
    )
    app.dependency_overrides[get_verified_identity] = _admin
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/system/backup/schedule")
    _clear()
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False  # 預設停用（opt-in）


def test_post_backup_schedule_persists_and_preserves_last_run(tmp_path, monkeypatch):
    cfgfile = tmp_path / "backup_schedule.json"
    monkeypatch.setattr("src.services.backup_scheduler_service.SCHEDULE_CONFIG_FILE", cfgfile)
    save_schedule_config(BackupScheduleConfig(enabled=False, last_run_epoch=12345.0))
    app.dependency_overrides[get_verified_identity] = _admin
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post(
        "/system/backup/schedule",
        json={"enabled": True, "frequency": "weekly", "time_of_day": "04:30",
              "weekday": 2, "retention_count": 5},
    )
    _clear()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["time_of_day"] == "04:30"
    assert body["retention_count"] == 5
    assert body["last_run_epoch"] == 12345.0  # 狀態被保留、未被前端重設
    assert load_schedule_config().enabled is True  # 已持久化


def test_post_backup_schedule_non_admin_403(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.services.backup_scheduler_service.SCHEDULE_CONFIG_FILE", tmp_path / "x.json"
    )
    app.dependency_overrides[get_verified_identity] = _normal
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/system/backup/schedule", json={"enabled": True})
    _clear()
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "AUTH_ADMIN_REQUIRED"


def test_post_backup_schedule_bad_time_422(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.services.backup_scheduler_service.SCHEDULE_CONFIG_FILE", tmp_path / "x.json"
    )
    app.dependency_overrides[get_verified_identity] = _admin
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/system/backup/schedule", json={"time_of_day": "25:99"})
    _clear()
    assert resp.status_code == 422


# ── 批⑤-D E59（ADR-0109）：backup_execution_lock 真併發互斥 ────────────────────


def test_backup_execution_lock_shared_across_consumers():
    """排程器與手動備份/匯入都經**同一個** run_backup_job（它持有 system_backup 唯一的執行鎖）。

    任一消費端改用自己的鎖或繞過 helper → 物件同一性破裂 → 紅。
    """
    assert sysrouter.run_backup_job is system_backup.run_backup_job
    assert sched_module.run_backup_job is system_backup.run_backup_job
    assert not hasattr(sysrouter, "backup_execution_lock")
    assert not hasattr(sched_module, "backup_execution_lock")


async def test_run_backup_concurrent_calls_serialized(monkeypatch):
    """兩併發 run_backup 必序列化（排程＋手動重疊＝備份損壞的根因）。

    既有測試零引用鎖名、無 gather；變異移 system.py:179 的
    `async with backup_execution_lock:` → 臨界區重疊（max==2）→ 紅。
    """
    monkeypatch.setattr(sysrouter, "verify_origin", lambda r: None)
    state = {"now": 0, "max": 0}

    def _slow_backup():  # run_in_threadpool 內執行；sleep 撐開重疊視窗
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)
        state["now"] -= 1
        return {"success": True, "message": "probe"}

    monkeypatch.setattr(sysrouter, "create_backup", _slow_backup)
    admin = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")

    await asyncio.gather(
        sysrouter.run_backup(None, admin),
        sysrouter.run_backup(None, admin),
    )
    assert state["max"] == 1
