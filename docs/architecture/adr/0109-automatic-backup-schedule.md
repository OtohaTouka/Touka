# ADR-0109：自動備份排程 + 保留策略 — proxy 內輕量排程器、自動備份檔名隔離

- **狀態**：✅ 已接受（2026-06-23 經 TDD 實作完成並全綠驗證：guard 37/37、後端 pytest（含 is_due 純函式/保留修剪/端點）、vitest、smoke 24/24；對抗式複審）
- **日期**：2026-06-23
- **影響範圍**：ToukaProxy（`scripts/manage_backup.py` 檔名+prefix、`services/system_backup.py` auto、`models/backup_schedule.py`、`services/backup_scheduler_service.py`、`routers/system.py` schedule 端點、`main.py` lifespan）＋ ToukaChat（`EnvSettings.tsx` 排程 UI、i18n）
- **關鍵字**：automatic backup, scheduler, retention policy, in-proxy asyncio loop, spawn_logged, 自動/人工備份隔離, 設定頁資料生命週期 軌1 Phase 3, 建基於 Phase 1（BK1-3）/Phase 2（ADR-0108）

## 背景（Context）

Phase 1+2 已有手動建立/還原/刪除/加密下載，但仍靠人記得手動備份。自動排程防「忘了備份就遺失」。

研究（4 探子）關鍵發現：**ToukaProxy 本身已有 in-process 排程基建**（`scheduler_service.py` 的 60s asyncio loop + JSON 狀態、`spawn_logged()` + lifespan `cancel_all_background_tasks()`），且 `create_backup()`、`backups/` 都在同進程。相對地 **ToukaHeartbeat 不適合**——獨立容器（拿不到 proxy 檔案系統）、且耦合 agent-pulse 語意。

## 決策（Decision）

### D1 — 排程器放在 proxy 內（輕量 asyncio service）
`services/backup_scheduler_service.py`：`run_loop()` 每小時醒一次、讀 `config/backup_schedule.json`、純函式 `is_backup_due()` 判斷到期；到期則 `create_backup(auto=True)` → 保留修剪 → 寫回 `last_run_epoch`（best-effort，例外只記 log）。以 `spawn_logged(...)` 於 lifespan 啟動，既有 `cancel_all_background_tasks()` 收尾。**零新相依、零跨容器**。時間走 SSOT `get_current_epoch()`；`datetime.fromtimestamp(epoch, tz)` 為決定性轉換（同 `scheduler_service`，不踩 TIME_SSOT 守衛）。

### D2 — 自動備份檔名隔離 + 保留只計自動備份（擁有者要求）
- `manage_backup.py` 加 `--prefix`（預設 `Touka_Backup`）；順手修檔名筆誤 `%HH%MM`→`%H%M`。
- `system_backup.create_backup(auto=True)` 用 `AUTO_BACKUP_PREFIX = "Touka_AutoBackup"`（SSOT 常數）。
- 保留 `prune_old_auto_backups()` **只 glob `Touka_AutoBackup_*`**，保留最新 N、刪其餘最舊。**人工備份（`Touka_Backup_*`）永不被自動刪、不計入保留份數**。

### D3 — 設定模型 / 端點 / 前端
- `BackupScheduleConfig`（pydantic，`extra="forbid"`）：`enabled`、`frequency`(daily|weekly)、`time_of_day`(HH:MM 正則)、`weekday`(0-6)、`retention_count`(1-100)、`last_run_epoch`(狀態)。持久化 `config/backup_schedule.json`（ResourceManager 原子寫；該檔本身被 `manage_backup` 納入備份）。
- `GET/POST /system/backup/schedule`（admin、POST 加 `verify_origin`；POST 保留既有 `last_run_epoch`，不被前端重設）。
- 前端 EnvSettings 排程區：啟用開關 / 頻率 / 星期(weekly) / `<input type=time>` / 保留份數 / 儲存；提示「自動備份與手動分開、保留只影響自動」。

### D4 — 預設（opt-in）＋ 單實例/重啟
預設 **停用**（全系統備份吃磁碟）、daily 03:00（台北）、保留 7。單實例 Docker → 無併發；`last_run_epoch` 持久化 → 重啟後若過期則開機後補跑、不重複（窗口判斷）。每小時粒度 → 觸發落在排定時刻所在那一小時內。

## 後果（Consequences）

- ✅ 自動備份防資料遺失；自動/人工備份檔名與保留完全隔離（擁有者要求）。
- ✅ 複用既有 in-proxy 排程模式，零新相依、零跨容器、優雅收尾。
- ⚠️ 每小時粒度（非精準到分）；設定改動 ≤1hr 生效（loop 每次醒重讀設定）。
- ⚠️ 自動備份不加密（伺服器端明文，與手動同）；下載才加密（ADR-0108）。

## 對抗式複審
排程到期/保留隔離/端點 admin/CSRF/格式驗證與時間 SSOT 經複審與測試覆蓋。詳見 SECURITY.md「軌1 Phase 3」。

## 替代方案（Rejected）
- **擴充 ToukaHeartbeat 排程器**：跨容器（無法存取 proxy `backups/` 與 `create_backup`）、耦合 agent-pulse 語意 → 否決。
- **新引入 APScheduler 於 proxy**：既有輕量 asyncio loop 模式已足夠，避免多餘相依。
