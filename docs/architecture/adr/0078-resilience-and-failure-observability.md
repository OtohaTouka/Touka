# ADR-0078: 失效可觀察性與韌性加固（原子寫入 + 失效可見化 + 當機復原可見化 + 韌性小修 + 降級橫幅 + 心跳重新觸發）

**狀態 (Status):** ✅ 已接受（實作位置修訂 by [ADR-0156](0156-react-core-extraction.md)：openai httpx 錯誤處理實作移出 handler 至 adapters；行為不變）
**日期 (Date):** 2026-06-09
**決策者 (Deciders):** 維護者

> 🔧 **心跳重新觸發的「強制 SafeAuto」已被 [ADR-0246](0246-model-layer-stage-2b-2c.md)（Manual／Auto／YOLO 三模式）修訂**：改為照心跳 `safe` 對應的 Auto（無人在場不跑終端、不叫審查員），永不升級 YOLO；擁有者守衛、`can_awaken`、pulse 夾 1..5 與每日配額不變。

## 上下文 (Context)

對抗式韌性稽核針對「極端情況下系統是否看得見、復原得了」提出四問：① bash/工具當機、網路/API 錯誤介面看得到嗎？② 讀到壞磁碟/壞檔有錯誤嗎？③ Touka 被誤關能回原迴圈嗎？④ 還有哪些極端情況？三路證據式稽核（含檔路徑/行號）發現多處破口：

- **資料完整性**：`ResourceManager.write_file_text` 為**非原子**寫入（寫一半當機即損毀；下次 `read_json` 靜默回 `None` → 配額/成本默默歸零）。
- **失效可見化**：工具例外只顯示籠統「Execution failed」；OpenAI 網路斷線與串流中斷**靜默**；degraded（記憶系統未載入）只有 Claude handler 注入警告。
- **當機復原**：對話歷史保存於 SQLite，但**進行中的背景多脈衝迴圈**當機後**默默消失、不復原**（autonomy_log 有記 active，但無人讀來偵測）。
- **韌性破口**：`upsert_session` 無鎖（併發 stale-read 丟欄位）、bash 孫進程於逾時/停止後成孤兒、fire-and-forget `asyncio.create_task` 吞例外。

## 決策方案 (Decision)

分四階段 + 兩 bonus 補強，**重用既有基礎建設**（resource_manager / autonomy_log / activity_registry / `/stats` fold / SystemStatusContext / sonner toast）。每階段獨立 TDD、各自 guard 31/31、人類負責 commit。

### P5a — 原子寫入 + 壞檔可見（資料完整性，最高優先）
`ResourceManager.write_file_text` 改**原子寫入**：同目錄 `tempfile.mkstemp` → 寫入 + `flush` + `os.fsync` → `os.replace`（同檔系統原子置換）；失敗清暫存並拋出。**一處改、全體 JSON 寫入者受惠**（autonomy_log / token_usage / mission / sessions-JSON / schedules）。`read_json` 對 `JSONDecodeError` 改記**明確 corrupt 告警**（有別於單純讀取失敗，可被 health/degraded 偵測）。

### P5b — 失效可見化
`tool_executor` 例外事件帶真錯誤（`{ClassName}: {msg[:200]}`，full trace 仍 `logger.exception`）；`openai_handler` 以 `try/except httpx.HTTPError` 包 `client.post` → 明確「連線錯誤」+ `[DONE]`；**degraded 警告三家一致**（OpenAI 注入 system message、Gemini 注入 system_instruction、Claude 既有）；前端 `useChatStream` onError 於非 `AbortError` 時 `toast.error(ERRORS.NETWORK_CONNECTION_ERROR.msg)`（守 ERROR_SSOT）。

### P5c — 當機復原可見化
`AutonomyService.mark_heartbeat_in_flight` / `clear_heartbeat_in_flight` 於背景迴圈起點落盤 `heartbeat_in_flight={session_id, agent, total, started_at}`、`finally` 清除（以 `_autonomy_lock` 串行、不覆蓋既有配額欄位）；`scan_and_clear_interrupted()` 於 `main.py` lifespan 掃描各 agent autonomy_log 殘留標記 → 收集「上次未完成」存記憶體並清 stale；折進 `/stats` 的 `interrupted_runs`，前端 `InterruptedRunsNotice` 顯示「⚠️ 上次有 N 個未完成的背景任務」。範圍＝**偵測 + 顯示 + 清 stale**（非自動續跑）。

### P5d — 韌性小修
- `upsert_session` 拆「公開上鎖 wrapper + `_upsert_session_unlocked` 核心」（`append_message_to_session` 已持鎖 → 改呼叫 unlocked 版，避免非可重入 `asyncio.Lock` 死鎖），以 `_get_session_lock` 串行化防併發 stale-read 丟欄位。
- `process_registry.terminate_process_tree`（POSIX `os.killpg`／Windows `taskkill /F /T`，回退 `proc.kill()`）+ bash 子進程以新行程群組啟動（POSIX `start_new_session`／Windows `CREATE_NEW_PROCESS_GROUP`）；逾時與 `terminate_session` 皆連坐殺孫進程，杜絕孤兒。
- `utils/task_utils.spawn_logged(coro, name)` 以強參考 + `add_done_callback` 記錄 fire-and-forget 例外；套用 handover 生成（`ui_sessions`）與模型初始化（`main.py`）。

### Bonus 1 — 降級模式側欄常駐橫幅
`SystemStatusContext` 既已輪詢 `/v1/memory`（已暴露 `system_degraded` / `degraded_reason`）→ **後端零改**，僅解析該欄位進 `SystemStatus`；`DegradedBanner`（`bg-destructive`、`role="alert"`）掛於 `RightSidebar` 分頁列與內容之間，降級期間跨分頁常駐、正常時不顯示。

### Bonus 2 — 心跳一鍵重新觸發
新端點 `POST /v1/heartbeat/retrigger`：以**人類擁有者**身分驗證（`get_verified_identity` + `resource_manager.is_owner`，非 service token）、`can_awaken` 能力守衛、**強制 SafeAuto**（永不升級 FullYOLO）、pulse 夾 1..5、不新建會話 → 派發 `_process_heartbeat_loop` 延續既有歷史（`_execute_single_pulse` 於上一則為 assistant 時自動補 user nudge 續跑）；`AutonomyService.clear_interrupted_run` 重觸後自清單移除（免下次輪詢復現）。前端 `InterruptedRunsNotice` 每筆加「重新觸發」按鈕。

## 後果與影響 (Consequences)

- **資料不再寫到一半損毀**（原子置換）；壞檔有明確告警而非靜默歸零。
- **失效不再無聲**：工具/網路/API 錯誤與 degraded 皆在介面或 agent 回覆中明示。
- **背景任務當機不再默默消失**：重啟後可見「上次 N 個未完成」並可一鍵接續。
- **併發寫入不丟欄位**；**bash 孫進程不殘留**；**fire-and-forget 失敗有 log**。
- **新端點安全邊界**：UI 重新觸發強制 SafeAuto、僅擁有者可觸發、受每日配額與 pulse 上限約束（詳見 `SECURITY.md`）。
- **NOQA**：`terminate_process_tree` taskkill 行 `# noqa: S603, S607`（registry S603 10→11、「其他」15→16）、retrigger 端點 `Depends` `# noqa: B008`（132→133）。
- **API 合約**：新增 `/v1/heartbeat/retrigger` → 已重生 `openapi.json` + `ToukaChat/src/types/api.ts`（純加性，Guard #2/#17 綠）。

## Action Items
1. [x] P5a 原子寫入 + corrupt 告警（`resource_manager`）。
2. [x] P5b 失效可見化（`tool_executor` / `openai_handler` / 三家 degraded / `useChatStream`）。
3. [x] P5c 當機復原可見化（`autonomy` 標記/掃描、`main.py` lifespan、`/stats` fold、`InterruptedRunsNotice`）。
4. [x] P5d 韌性小修（upsert 鎖、`terminate_process_tree`、`spawn_logged`）。
5. [x] Bonus：降級側欄橫幅（`SystemStatusContext` + `DegradedBanner`）。
6. [x] Bonus：心跳重新觸發端點 + UI 按鈕（強制 SafeAuto、擁有者守衛、`clear_interrupted_run`）。
7. [x] TDD 全綠（後端 pytest 1506、前端 vitest 360）、guard 31/31、NOQA registry 同步、openapi/api.ts 重生。
8. [ ]（未來增強）背景迴圈**完整自動續跑**（含跨 pulse 狀態續接）；degraded 改由 `/v1/health` 暴露以省一次 `/v1/memory` 輪詢。
