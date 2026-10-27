# ADR-0267：記憶系統第二批段 4：可信活動時間軸與時間感（continuity v1）

- 狀態：✅ 已接受（維護者 2026-10-15 核准記憶系統第二批計畫，心跳也注入；本 ADR 是第 4 段的 PR，照一般驗收）〔ADR-0285 修訂已知限制最後一條：看板那一行的任務 id 也改用 `safe_ref`〕〔ADR-0286 修訂記憶事件的時機：封存只在 `archive_fact` 回 archived 或 detached 時記〕
- 日期：2026-10-15
- 決策者：維護者
- 影響範圍：ToukaProxy（`utils/async_db.py` 的 `init_db_async`、新 `utils/continuity_events_db.py`、新 `services/continuity_events.py`、`services/continuity_service.py`、`services/context_snapshot_service.py`、`middleware/layers/soul.py`、`routers/heartbeat.py`、`tools/mission_tools.py`、`tools/builtin/memory.py`、`main.py`、`services/agent_reset/primitives.py`、`services/agent_reset/delete_guard.py`、`routers/souls.py`、`services/gdpr_export_service.py`）；ToukaMemory（`protocols.py`、新 `services/event_recorder.py`、`services/manager.py`、`services/lifecycle.py`）
- 相關：ADR-0013（時間 SSOT）、ADR-0030（sqlite3 白名單，照舊）、ADR-0035／0092（滑動視窗觸發點）、ADR-0086（無痕零殘留）、ADR-0104／0113（重置）、ADR-0110（GDPR 匯出）、ADR-0228（原生工具合約與 Guard #45）、ADR-0231（結果不明不寫）、ADR-0245（只留靈魂、`session_bound`）、ADR-0262（刪除靈魂前檢查）、ADR-0263（一步取代），決策都不變。修訂 ADR-0257 決策 3（上一輪的來源順序）與決策 4（心跳不注入）；修訂 ADR-0264 決策 3（記憶體那條上一輪改標 `(server memory)`）；兩份的狀態行都已加反向註記。對話樹時間的「可能來自使用者裝置」標示沿用 ADR-0264

## 背景

- 系統沒有「事情真的做成之後才寫」的紀錄。continuity v0 只有記憶體與對話樹兩個來源；UI 對話由前端整棵寫回，Proxy 重啟後多半說不出上一輪是何時。
- 心跳完全沒有 continuity：醒來不知道人類多久沒來、上次自主跑是何時、最近完成什麼。
- `ToolExecutor` 的成功判斷看字串（沒有 `Error:`／`❌` 就算成功，「⚠️ [重複內容]」也算），不能拿來當「做成了」的依據。

## 決策

### 1. 事件表放 Proxy `ui_sessions.db`，只有中繼資料

- 表 `continuity_events`：`event_id`（主鍵）、`agent_id`（動手的靈魂）、`kind`、`occurred_at`（epoch 毫秒，伺服器時鐘，`get_current_epoch_ms()`）、`session_id`、`subject`（主體 id）、`trust`、`metadata`（JSON）；索引 `(agent_id, kind, occurred_at)`。建表在 `async_db.init_db_async`（字面量 DDL；新模組要匯入 `get_async_db`，反過來會循環匯入）。
- 讀寫只在 `utils/continuity_events_db.py`，只經 `async_db.get_async_db()`；不新增 `import sqlite3`，sqlite3 白名單與 ADR-0030 不變；不碰 `core_memory.db` 的版本升級。
- 只存 id、計數與固定列舉值，不存訊息、記憶、任務描述、目標、驗收結論任何文字。模型寫的任務 id 經 `continuity_events.safe_ref` 只留 `[A-Za-z0-9._:-]{1,32}`，其他換成 `h:` 加 sha256 前 12 碼。
- **trust 寫死 `'system'`**：兩支 INSERT 的 SQL 把它寫成字面量，寫入介面（`insert_event(row)`、`EventRow`）沒有 trust 欄位；欄位留給以後的回填。
- **可信紀錄起點**：`system_settings` 的 `trusted_timeline_epoch` 在第一次建表時寫入（`INSERT OR IGNORE`），之後的啟動不改。重置某個靈魂時另寫 `trusted_timeline_epoch:<靈魂>`；continuity 取兩者較晚的那個。
- 保留期限這批不定（心跳 5 分鐘一次時每個靈魂一年約二十萬筆）。

### 2. 14 種事件與 event_id 規則

| kind | 掛點 | subject | event_id | 鍵 |
|---|---|---|---|---|
| `user_turn` | SoulLayer | 這一輪已存的使用者訊息 id | `user_turn:{靈魂}:{訊息}` | 固定（重新生成不算新訊息） |
| `heartbeat_started`／`heartbeat_finished` | 心跳迴圈開始／finally | `{session}:{t0 毫秒}` | `heartbeat_*:{靈魂}:{run}` | 固定（每個 run 一組） |
| `mission_started` | `mission_tools` initialize | 新看板的 uuid | `mission_started:{靈魂}:{mission}` | 固定 |
| `task_todo`／`task_doing`／`task_blocked`／`task_failed` | `mission_tools` update_status | `{mission}/{task}` | `task_*:{靈魂}:{mission}/{task}:{毫秒}` | **加當下毫秒**（會重複發生） |
| `task_done` | 同上 | 同上 | `task_done:{靈魂}:{mission}/{task}` | 固定（終端） |
| `mission_completed` | update_status 後看板全部完成 | mission uuid | `mission_completed:{靈魂}:{mission}` | 固定（終端） |
| `mission_finalized` | `mission_tools` finalize（帶結論） | mission uuid | `mission_finalized:{靈魂}:{mission}` | 固定（重複驗收只留第一次） |
| `memory_saved` | `MemoryManager.save_memory` | fact id | `memory_saved:{靈魂}:{fact}` | 固定 |
| `memory_archived` | `MemoryManager.resolve_duplicate` | fact id | `memory_archived:{靈魂}:{fact}` | 固定 |
| `memory_superseded` | `lifecycle.supersede_memory` | 舊 fact id | `memory_superseded:{靈魂}:{舊}` | 固定；metadata 帶 `new_fact_id` |

- event_id 一律帶靈魂（多個靈魂回同一則訊息不會只留第一個）。會重複發生的四種任務轉換加毫秒，所以「FAILED → 特權重置 → 再 FAILED」、兩次 BLOCKED 都會留；其餘是終端狀態，或主體天生唯一、不會再發生第二次的事件，用固定鍵。全部 `INSERT OR IGNORE`。
- 同一任務最新一筆已經是同一個狀態（例如 DOING 中補產出、FAILED 補 log）就不記：`insert_event` 對任務事件用單一 `INSERT … SELECT … WHERE` 敘述判斷，在 SQLite 內原子。
- 心跳結束原因是固定列舉：`completed`、`interrupted`、`quota`、`budget`、`no_model`、`error`、`cancelled`；metadata 另帶 `pulses_total`、`pulses_done`、`turns`。跨看板事件的 metadata 帶 `board_owner`。

### 3. 只在事實提交之後記；結果不明不寫

- user_turn：SoulLayer 在 `<continuity>` 組好之後記（它看不到自己這一輪）。條件：`source_message_id`（router 用 CAS 確認已存才設）、不是心跳（`heartbeat_run`；心跳的 ctx 也帶 `source_message_id`）、不是無痕／子代理／派工、`session_bound`（對話屬於這個靈魂）、送訊息的是人類（`ui_requester.is_human`）。
- 心跳：in-flight 標記寫入之後記開始；`finally` 記結束（同步排程、不 await，被取消時也安全），標記沒寫進去的 run 兩筆都不記。禁令 28 的 finally 觸發點與順序不變。
- 任務：`mission_tools` 在 `await manager.…` 成功之後記；非法轉換、BLOCKED 沒填細節、跨看板被拒、`MissionPersistenceError`（結果不明，ADR-0231）都在 await 就丟出，不記，也不另寫 failed。
- 記憶：`save_fact` 回 id 之後（兩種索引結果都算）、`archive_fact` 回 True 之後（ADR-0286 起：回 archived 或 detached 之後；本來就封存、查無都不記）、`supersede_fact` 成功之後。被擋、`save_fact` 回 error、目標錯都不記。
- 不記：`request_review`（審查結果不是狀態轉換）、`get_board`、`await_dispatch_result`；GDPR 匯出、重置、P2 清除、觀測與統計路徑（不是「靈魂存／忘／取代了一筆記憶」）。Memory 的同名 `mission_tools` 被 Proxy 的蓋掉，不掛。

### 4. recorder：同步、不阻塞、執行緒安全、絕不外丟

- Memory 端只認 `protocols.IContinuityEventRecorder`，經 `services/event_recorder.register_event_recorder` 登記；沒登記（Proxy 沒啟動、單元測試）時 `record_memory_event` 什麼都不做，記錄器出錯也在這裡接住、只記 warning。Memory 不匯入 Proxy（import-linter 與 Guard #21 不變）。
- Proxy 的 `ContinuityEventRecorder`：`main.py` 在 `init_db_async` 之後 `start_recording()` 綁定主事件迴圈並登記給 Memory；`_cleanup_proxy_runtime` 最先執行 `stop_recording()`，關機時先停止接收新事件、再取消背景任務。沒綁定時一律不記。
- 呼叫端只是排一個背景寫入：目前的 running loop 就是綁定的那個時直接 `spawn_logged`；其他情況（工作執行緒，或工作執行緒裡 `asyncio.run` 開的短命迴圈）一律 `call_soon_threadsafe` 排回主迴圈，寫入不會跟著短命迴圈被取消。
- **寫入在主迴圈用一把鎖照呼叫順序一筆一筆寫**：背景寫入如果亂序，「同一任務同一狀態不另記」會拿較晚的那筆比對，把較早的轉換吃掉（例如第一次 FAILED）；實作時全量測試實際碰到過。代價：**關機時還在排隊或寫到一半的事件會被取消、不重試**。
- 寫失敗只記 warning、只試一次、不另寫 failed；`record()` 與各掛點小函式任何一步出錯都接住（禁令 59），呼叫端的回傳逐字不變。
- 只記正式靈魂（`soul_service.is_toukasoul`；人類、DebugAgent、DispatchAgent、子代理臨時 ID、一般模型都不記）。

### 5. 無痕、子代理、派工不記；跨看板記在動手的靈魂名下

- 有 ctx 的掛點直接看 `incognito`、`subagent_run`、`dispatch_run`。記憶事件發生在 Memory、看不到 ctx：子代理與 DispatchAgent 本來就不是靈魂；無痕再由 Proxy 記憶閘道用 `events_scope(kwargs.get("_ctx"))`（ContextVar，`to_thread` 會複製）包住轉發，記憶工具本身的行為不變。
- 有 `can_supervise_dispatch` 的靈魂改 DispatchAgent 看板：事件記在動手的靈魂名下，metadata 標 `board_owner`；DispatchAgent 自己（ToukaDispatch 直接用 MissionManager）不經 `mission_tools`，也不是靈魂，不記。
- 心跳裡做成的事是真的自主工作，照記。

### 6. continuity v1：每行標來源

```
- Current time (Asia/Taipei): … (server clock)
- Since your previous turn in this conversation: 12m (server memory)
- Last message from a human (any conversation): 2d 4h ago, 2026-10-13 12:02 (activity record)
- Last autonomous run: ended 6h 0m ago, …, 3/3 pulses, completed (activity record)
- Recently completed (last 7 days, newest first, max 3): task T3 done …; mission 1a2b3c4d finalized … (activity record)
- Mission board: … details are in the <mission_awareness> block. (mission board)
- Activity records start … (Asia/Taipei); anything earlier was not recorded.
```

- 這段對話上一輪：記憶體（伺服器，標 `(server memory)`，絕不出現前端時間的標示）→ 事件表 user_turn（同一個對話、排除這一輪的訊息 id，重新生成時這一輪已記過）→ 對話樹（標成可能來自使用者裝置的時鐘，絕不寫進事件表）→ 不知道。
- 上次人類訊息：任何對話的 user_turn，排除這一輪。上次自主執行：只算有結束紀錄的 run。最近完成：`task_done`／`mission_completed`／`mission_finalized`，7 天內、最多 3 筆；只顯示 kind、`safe_ref` 過的任務 id、mission uuid 前 8 碼與時間。任務看板照舊讀 MissionManager（SSOT）。
- 可信紀錄起點只在上面三行有「none recorded」時出現（之前沒記不代表沒發生）。
- HEADER 加一句「存進記憶時寫絕對日期，不要寫『N 小時前』」。
- 一次請求只開一條連線讀事件表（`read_snapshot`）。讀不到時三行寫 `unavailable (the activity record could not be read)`、不出現起點行，區塊照樣送出、不丟例外。
- 無痕照舊只有現在時間，不讀事件表、不讀看板、不留記憶體紀錄。

### 7. 心跳也注入

- `_execute_single_pulse` 在 `_HEARTBEAT_TOOL_LAYER.process` 之後、`dispatch_request` 之前，只對靈魂（`ctx["is_toukasoul"]`）注入同一個 `<continuity>` 標籤，第二行是 `- Run type: autonomous heartbeat; no human is present in this run.`。沿用同一個標籤，`REQUEST_TAIL_TAGS` 的 Claude 快取斷點、第 1 段的組裝不變式（只出現一次、只在最後一則 user）、context snapshot 都不用改。
- 心跳模式不出現「這段對話上一輪」、不呼叫 `_swap_last_request`（家對話和人類共用 session id，記了會讓人類下一輪的上一輪變成心跳）；自主執行那行標成「Previous autonomous run (before this one)」。
- 既有的時間前綴保留，**nudge 前綴格式逐字不變**（`PULSE_NUDGE_PREFIX`＋時間＋`】`＋那句提示），只把時間來源換成脈衝開頭取一次的 `now_s`，continuity 用同一個值：有補 nudge 的脈衝兩邊時間字串相同。第一個脈衝的初始提示前綴是 `/awaken` 寫入當下，continuity 是送出當下，同一個時鐘、同為台北時間，差幾秒。
- 只改送出的請求；對話樹從 session 讀、`upsert_session` 寫的是樹，不含區塊。審查員的原意在 `/awaken` 就設好，看不到它。

### 8. 重置、刪除前檢查、GDPR

- 重置：活動時間軸屬「記憶」類別。`MEMORY_STORES` 在 `memory` 後面加一列 `StorePurger("timeline", purge_timeline, remaining_timeline)`，不改 `purge_memory`／`remaining_memory`。清除與寫該靈魂的起點在同一個交易。畫面類別文字不改；結果明細多一行 `timeline`。重置期間心跳又寫了事件時，自檢殘留 >0、`clean=False`，照實回報。
- 刪除靈魂：`delete_guard.assert_timeline_cleared` 在記憶檢查之後執行。還有事件時 409 `SOUL_MEMORY_NOT_RESET`（detail `activity_events`），讀不到時 503 `SOUL_MEMORY_CHECK_FAILED`，什麼都不刪；不新增錯誤碼、不改 i18n。同名新靈魂因此不會接手舊時間軸；重置後只留 `system_settings` 的起點鍵，不算事件、不擋刪除。
- GDPR 匯出：每個自有 agent 多 `agents/<id>/activity_timeline.json`（`agent_id`、`count`、`events`），manifest `counts.activity_events` 與 `included` 的 `agent_activity_timeline`，README 多一行；讀不到時列進 `skipped`（`activity_timeline_unavailable`）。

### 9. Guard #45 基準不動

- `mission_tools`、記憶閘道 `memory_tools`、`MemoryManager.save_memory`／`resolve_duplicate` 是有型別 consumer：只加呼叫與匯入、只讀執行期 kwargs `_ctx`（`ToolExecutionContextKwargs`，守衛忽略）、不新增參數、不新增 `**`、不新增 def；`action == "…"` 選擇器不變。沒有型別的 consumer（`find_duplicates`、`purge_agent`、`count_agent_memory`、`MissionManager` 各方法等）不碰。新邏輯都在兩個 tools 目錄以外的新模組。
- 規則摘要：基準列的 consumer 檔新增任何函式都要改基準；兩個 tools 目錄新增任何 `.py` 也要改；有型別函式的新參數必須是輸入模型欄位、任何地方都不能新增 `**`；沒有型別的函式形狀要完全一致，`.get`／`.pop`／`.setdefault("常數")` 增減都算。嚴禁為了不改基準改用守衛看不到的寫法（`getattr`、`itemgetter` 之類）。

## 和設計不同的地方

- 事件種類是 14 種（設計稿寫 13 是筆誤）。
- 寫入照呼叫順序用鎖逐筆寫（設計沒有）：理由見決策 4。
- recorder 選迴圈：只有目前的 running loop 就是綁定的那個才直接寫，其他一律排回主迴圈（審查時裁定）。
- `mission_tools` 用 `from src.services import continuity_events` 匯入，避開同一個檔另一處改動的相鄰行。
- 記憶體那條上一輪改標 `(server memory)`（見「相關」對 ADR-0264 決策 3 的修訂）；原本要求它不帶任何標示的測試，預期改成帶這個標示，「絕不出現前端時間標示」的斷言保留。
- 刪除前檢查只有活動事件時，409 的 detail 是 `activity_events`，沒有 `facts`／`vectors` 欄位。

## 影響

- **第一次啟動**：`ui_sessions.db` 多一張事件表與索引，並寫可信紀錄起點；不用升級、不備份。
- 靈魂的前景對話與心跳都會看到 `<continuity>` v1（每次請求多一條唯讀連線、5 個索引查詢，區塊約 1,200 字元以內）。**心跳醒來會先看到人類多久沒來、上次自主跑、最近完成，自主行為可能因此不同**（離線測不出品質）。
- 每個事件一條背景寫入連線。重置結果多一行 `timeline`；刪除靈魂多一個「還有活動紀錄」的擋下理由；GDPR 匯出多一個檔。
- 不變：openapi、`errors.json`、i18n、前端、NOQA、AGENTS.md、權限設定檔、`tool_executor.py`、`execution_context.py`、Guard #45 基準。

## 已知限制

- 關機時還在排隊或寫到一半的事件會被取消、不重試；被強制結束的心跳沒有結束紀錄（`/v1/stats` 的「上次未完成」另有顯示）。
- 群聊（`ToukaGroup`）跳過 SoulLayer，人類訊息不記；子代理與派工的動作不算到父靈魂；檔案寫入／刪除事件、換模型事件不做。
- 事件只增不減，保留期限下一批再定。
- 系統快照裡的 `ui_sessions.db` 仍含已重置靈魂的事件（只有中繼資料；清除連備份那段的畫面照實寫「仍保留」）。
- 同一毫秒裡的兩個事件（例如任務完成與看板完成）在 GDPR 匯出裡依寫入順序排。
- 第一個心跳脈衝的初始提示前綴與 continuity 的現在時間差幾秒（見決策 7）。
- 模型寫的任務 id 若剛好是一串英數字詞，照原樣顯示；它和 `<mission_awareness>` 裡模型自己寫的看板內容來源相同，沒有新的提權。

## 決策落點表

| 決策 | 檔案 | 位置 | 測試／守衛 |
|---|---|---|---|
| 1 事件表、起點 | `ToukaProxy/src/utils/async_db.py`；新 `utils/continuity_events_db.py` | `init_db_async`、`TRUSTED_TIMELINE_EPOCH_KEY`；`EventRow`、`insert_event`、`count_agent_events`、`purge_agent_events`、`list_agent_events`、`read_snapshot`、`agent_epoch_key` | `test_continuity_events_db.py` |
| 2 event_id、安全 id | 新 `services/continuity_events.py` | `EVENT_KINDS`、`REPEATABLE_KINDS`、`event_id`、`safe_ref` | `test_continuity_event_recorder.py` |
| 3 掛點 | `middleware/layers/soul.py`；`routers/heartbeat.py`；`tools/mission_tools.py`；ToukaMemory `services/manager.py`、`services/lifecycle.py` | `SoulLayer.process`；`_process_heartbeat_loop`；initialize／update_status／finalize；`save_memory`、`resolve_duplicate`；`supersede_memory` | `test_continuity_event_hooks.py` |
| 4 recorder | `services/continuity_events.py`；`main.py`；ToukaMemory `protocols.py`、新 `services/event_recorder.py` | `ContinuityEventRecorder`、`_write`、`start_recording`／`stop_recording`；`_lifespan_runtime`、`_cleanup_proxy_runtime`；`IContinuityEventRecorder`；`register_event_recorder`、`record_memory_event` | `test_continuity_event_recorder.py`、`guard_module_boundaries.py` |
| 5 抑制與跨看板 | `services/continuity_events.py`；`tools/builtin/memory.py` | `events_scope`、`record_user_turn`、`record_*`；閘道 `memory_tools` | `test_continuity_event_recorder.py`、`test_continuity_event_hooks.py` |
| 6 continuity v1 | `services/continuity_service.py`；`services/context_snapshot_service.py` | `HEADER`、`_previous_turn_line`、`_read_timeline`、`_human_line`、`_run_line`、`_recent_line`、`_records_start_line`、`build_continuity_block`、`inject_continuity`；CONTINUITY 說明 | `test_continuity_block.py` |
| 7 心跳注入 | `routers/heartbeat.py` | `_execute_single_pulse`（`now_s`、`inject_continuity`） | `test_continuity_not_persisted.py`、`test_interjection_notes.py` |
| 8 重置、刪除、GDPR | `services/agent_reset/primitives.py`；`services/agent_reset/delete_guard.py`；`routers/souls.py`；`services/gdpr_export_service.py` | `purge_timeline`、`remaining_timeline`、`MEMORY_STORES`；`assert_timeline_cleared`；`delete_soul`；`_fetch_agent_timeline`、`_collect`、`_readme` | `test_reset_primitives.py`、`test_per_agent_reset.py`、`test_soul_delete_memory_guard.py`、`test_gdpr_export.py` |
| 9 Guard #45 | — | 基準不動 | Guard #45、`test_guard_native_tool_contracts.py` |

## 驗證

本機 Windows、3.12 venv（`uv run --locked`）、fnm Node 24.21.0。自動測試都在 conftest 沙盒或 pytest `tmp_path` 裡：事件表在 tmp 的 `ui_sessions.db`，事實庫是 tmp 檔，向量庫用 autospec 替身或 tmp 本機 Qdrant，上游是假的串流。不讀 `data/`、不載入嵌入模型、不打付費 API，也沒有啟動 Touka。

- 新測試：`test_continuity_events_db.py` 11、`test_continuity_event_recorder.py` 57、`test_continuity_event_hooks.py` 25；既有檔加測或改寫：`test_continuity_block.py`（v1 案例 12 個測試函式）、`test_continuity_not_persisted.py` 5→7、`test_interjection_notes.py`（心跳那條比對前去掉區塊）、`test_reset_primitives.py`、`test_per_agent_reset.py`、`test_gdpr_export.py`、`test_soul_delete_memory_guard.py` 9→12。先紅後綠：事件表與掛點的新測試先因模組不存在無法收集、GDPR 2 個紅；continuity 與心跳把產品檔換回實作前的版本時 14 個紅；刪除前檢查 3 個紅。
- 突變：47 個，每個只改一處，跑對應的測試；全部變紅（7 天時間窗與無痕讀取兩個第一次倖存，補強測試後變紅）。
- 全量（最後 merge dev `a732d6ff` 後）：pytest 9845 passed／7 skipped／6 failed（6 個是本機 Docker Desktop 的 `test_local_compose_bindings`，以 CI 為準；7 個跳過與 ADR-0251 的基準相同）；程式 `b353409d` 時 smoke 24／24。`guard_adr_integrity.py`（0001..0267 連續、修訂雙向一致）、`guard_noqa_registry.py`、`lint_ssot.py`、`guard_native_tool_contracts.py`、`guard_module_boundaries.py`、文件契約測試、ruff（改到的檔沒有新增問題）通過。`test_main_loop_retry.py` 在本機偶爾因計時失敗，基準 commit 上同樣會發生（5 次 1 次），與本段無關。

## 回復

- 不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。程式可以單獨撤；Guard #45 基準沒有動，不用一起撤。
- 撤回後事件表與 `system_settings` 的起點鍵留在 `ui_sessions.db`，沒有程式會讀寫它們；不影響其他表。要清掉時在 Proxy 停機時手動 `DROP TABLE continuity_events`。
