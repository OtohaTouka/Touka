"""ADR-0109：proxy 內輕量自動備份排程器。

複用既有 in-proxy 模式（spawn_logged / lifespan 取消）。每小時醒一次、
讀 config/backup_schedule.json、判斷到期；到期則跑自動全系統備份
（create_backup(auto=True)，檔名前綴 Touka_AutoBackup）並依保留份數修剪「僅自動備份」
（人工備份永不被自動刪、不計入）。

時間走 SSOT get_current_epoch()（非 datetime.now——後者踩 TIME_SSOT 守衛）；
datetime.fromtimestamp(epoch, tz) 為給定 epoch 的決定性轉換、非取「現在」，可用。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.config import get_config_dir
from src.models.backup_schedule import BackupScheduleConfig
from src.services.resource_manager import resource_manager
from src.services.system_backup import (
    AUTO_BACKUP_PREFIX,
    backup_dir,
    create_backup,
    list_snapshot_files,
    run_backup_job,
)
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# 序列化 backup_schedule.json 的 read-modify-write——排程器寫 last_run 與 POST 寫策略不互相覆蓋
# （ADR-0109 複審 #3）。單一事件迴圈、持有極短（僅讀+寫），不會卡住長時間的備份。
_config_lock = asyncio.Lock()

SCHEDULE_CONFIG_FILE = get_config_dir() / "backup_schedule.json"
try:
    _TAIPEI: timezone | ZoneInfo = ZoneInfo("Asia/Taipei")
except (ZoneInfoNotFoundError, ImportError):
    # tzdata 缺漏（部分 Windows/精簡環境）→ 退回固定 UTC+8（台灣無 DST，等價）。
    _TAIPEI = timezone(timedelta(hours=8))


# 備份目錄（SSOT 在 system_backup；與 routers/system._get_backup_dir 同一個）。
backups_dir = backup_dir


def load_schedule_config() -> BackupScheduleConfig:
    """讀取排程設定；缺檔/壞檔回預設（停用）。"""
    data = resource_manager.read_json(SCHEDULE_CONFIG_FILE)
    try:
        return BackupScheduleConfig.model_validate(data or {})
    except Exception:
        logger.warning("backup_schedule.json invalid; falling back to defaults")
        return BackupScheduleConfig()


def save_schedule_config(cfg: BackupScheduleConfig) -> None:
    """持久化排程設定（原子寫，經 ResourceManager）。"""
    resource_manager.make_dir(SCHEDULE_CONFIG_FILE.parent)
    resource_manager.write_json(SCHEDULE_CONFIG_FILE, cfg.model_dump(), indent=2)


async def persist_policy(payload: BackupScheduleConfig) -> BackupScheduleConfig:
    """POST 端點用：在 config 鎖下寫入使用者策略，並保留排程器維護的 last_run_epoch。"""
    async with _config_lock:
        payload.last_run_epoch = load_schedule_config().last_run_epoch
        save_schedule_config(payload)
        return payload


async def _record_last_run(epoch: float) -> None:
    """排程器用：在 config 鎖下只更新 last_run_epoch（重讀策略，避免覆蓋 POST 的策略修改）。"""
    async with _config_lock:
        latest = load_schedule_config()
        latest.last_run_epoch = epoch
        save_schedule_config(latest)


def is_backup_due(cfg: BackupScheduleConfig, now_epoch: float) -> bool:
    """純函式：依設定與當前 epoch 判斷是否該跑自動備份。

    規則：啟用、到了當天 time_of_day、（weekly 時）今天是排定星期、且本窗口（今天該時刻）
    尚未跑過（last_run_epoch 早於本窗口起點）。每小時粒度——觸發落在排定時刻所在那一小時內。
    """
    if not cfg.enabled:
        return False
    now = datetime.fromtimestamp(now_epoch, _TAIPEI)
    hour_s, minute_s = cfg.time_of_day.split(":")
    target_hour, target_minute = int(hour_s), int(minute_s)
    now_total = now.hour * 60 + now.minute
    if now_total < target_hour * 60 + target_minute:
        return False  # 今天還沒到排定時刻
    if cfg.frequency == "weekly" and now.weekday() != cfg.weekday:
        return False  # 不是排定的星期
    if cfg.last_run_epoch is None:
        return True
    window_start = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    last = datetime.fromtimestamp(cfg.last_run_epoch, _TAIPEI)
    return last < window_start  # 本窗口尚未跑過


def prune_old_auto_backups(backup_dir: Path, retention_count: int) -> list[Path]:
    """只修剪自動備份（AUTO_BACKUP_PREFIX）：保留最新 retention_count 份，刪除其餘最舊者。

    人工備份（``Touka_Backup_*``）完全不受影響、亦不計入份數。回傳被刪除的路徑清單。
    """
    autos = sorted(
        (f for f in list_snapshot_files(backup_dir) if f.name.startswith(f"{AUTO_BACKUP_PREFIX}_")),
        key=lambda f: f.stat().st_mtime,
    )  # 最舊在前；只看清單規則內的檔（和清單、清除同一套）
    to_delete = autos[:-retention_count] if retention_count > 0 else list(autos)
    for f in to_delete:
        resource_manager.remove_file(f)
    return to_delete


def _backup_and_prune(retention_count: int) -> int:
    """排程備份＋修剪（在 run_backup_job 的執行緒裡、鎖內執行）；回傳修剪的份數。"""
    create_backup(auto=True)
    return len(prune_old_auto_backups(backups_dir(), retention_count))


class BackupScheduler:
    """每小時醒一次的自動備份排程器（lifespan 以 spawn_logged 啟動，cancel_all 收尾）。

    **最佳努力、cron 式語意**：每小時粒度；若主機在排定時刻處於停機/休眠或時鐘回跳，該窗口會被
    略過（隔次窗口才補；不重複洗版）。備份失敗（磁碟滿/腳本缺）會**記為已嘗試**並 error log——
    本窗口不再每小時重試（避免洗版），等下一窗口再試；管理員應留意錯誤日誌。
    """

    def __init__(self) -> None:
        self.running = False

    async def run_loop(self) -> None:
        """背景循環：每小時讀設定、判斷到期、跑備份＋修剪。"""
        self.running = True
        logger.info("🗓️ [BackupScheduler] loop started.")
        while self.running:
            try:
                cfg = load_schedule_config()
                if is_backup_due(cfg, get_current_epoch()):
                    await self._run_once(cfg)
            except Exception:
                logger.exception("[BackupScheduler] loop error")
            await asyncio.sleep(3600)

    async def _run_once(self, cfg: BackupScheduleConfig) -> None:
        logger.warning("AUDIT: auto-backup starting (retention=%d)", cfg.retention_count)
        try:
            # run_backup_job：與手動備份共用執行鎖，防併發備份（複審 #1/#8）；修剪也在鎖內。
            pruned = await run_backup_job(lambda: _backup_and_prune(cfg.retention_count))
            logger.warning("AUDIT: auto-backup done (pruned %d old auto-backups)", pruned)
        except Exception:
            # 失敗仍記為「本窗口已嘗試」→ 不每小時重試洗版（複審 #2）；管理員看 error log。
            logger.exception("AUDIT: auto-backup FAILED; window consumed, retry next window")
        finally:
            await _record_last_run(get_current_epoch())


# 全系統單例（lifespan 啟動其 run_loop）。
backup_scheduler = BackupScheduler()
