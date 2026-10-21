# ADR-0264：記憶系統第二批段 T4：對話時間戳、摘要時間範圍、剪下的對話直接歸檔進舊對話庫

- 狀態：✅ 已接受（Owner 2026-10-15 核准記憶系統第二批計畫，含「舊對話日期改成訊息的實際時間」；舊對話歸檔的修正經技術主管併入本段）〔ADR-0267 修訂決策 3：記憶體那條上一輪改標 `(server memory)`，仍絕不加對話紀錄的註記〕
- 日期：2026-10-15
- 決策者：Owner
- 影響範圍：ToukaChat（`lib/stream_tree.ts`、`lib/tree_history.ts`、`lib/time_utils.ts`、`lib/export-utils.ts`、`lib/providers/StreamRegistryProvider.tsx`）；ToukaProxy（`services/sliding_window_service.py`、`services/dispatcher.py`、`services/dialogue_summarizer.py`、`services/continuity_service.py`、`middleware/layers/context.py`、`adapters/content_normalize.py`、`utils/time_utils.py`、`routers/ui_sessions.py` 匯出端點、`routers/stats.py` 舊對話統計）；ToukaMemory（`services/legacy_store.py`、`services/legacy_sync.py`）
- 相關：ADR-0035／ADR-0092（滑動視窗、替換式摘要）、ADR-0154（前景代打成靈魂）、ADR-0231（讀回核對）、ADR-0253（舊對話同步照實回報）、ADR-0256（記憶來源）、ADR-0257（continuity、歷史資料界線）、ADR-0258（舊對話時間一律毫秒），決策都不變；解決 ADR-0256 已知限制「剪掉還沒同步的訊息，查回原文已不在」與 ADR-0257 已知限制「IDE 資訊區塊帶圖會壞」

## 背景

1. **前端節點不帶時間**：`buildUserTurnTree`／`appendAssistantNode` 建的節點沒有時間；完成時前端整棵寫回（`_apply_session_changes` 整欄取代），dispatcher 為這輪助理節點蓋的後端時間也被換掉。舊對話的日期因此是「匯出或同步當下」，摘要沒有時間範圍。
2. **continuity 讀的是對話樹節點的 `timestamp`**：記憶體沒有上一輪紀錄時，往回找對話樹上帶時間的節點。對話樹是前端整棵寫回的，伺服器分不出哪個時間是後端寫的、哪個是使用者裝置的時鐘。
3. **系統自己的舊對話寫入在正式環境一律失敗**：`resource_manager.is_protected_path` 把 `<資料根>/legacy/` 整個當受保護（`write_file_text`、`write_json`、`move_file` 等寫入口都先檢查），而 `get_vault_path("legacy")` 正好回 `<資料根>/legacy/<id>`。依 git 歷史推定：
   - 前端「切分存入舊對話」`POST /v1/ui/sessions/legacy/export`：2026-05-04 改走 `resource_manager.write_json`（同一個 commit 加了保護）起，一定回 SYSTEM_ERROR，前端不剪。
   - 設定頁手動同步：2026-05-05 把 `shutil.move` 改成 `resource_manager.move_file` 起，搬到 `backup/` 被擋；資料庫其實已寫入，但每個檔都算錯、端點回 SYNC_FAILED、JSON 留在原地。ADR-0253 遺留的「同步搬完卻回失敗」很可能就是這個原因。
   - 滑動視窗剪裁的 ACID-A（寫 `legacy/<id>/<id>_sw_<ts>.json`）：2026-05-21 加入時就失敗，每次回 False；正式環境的長對話從沒剪成、前情摘要從沒寫過。
   - 單元測試都用 `tmp_path` 或把 resource_manager 換成假的，所以沒抓到。
4. **聊天側剪裁的身分是人類**：ToukaChat 走的 UI 路徑把 `ctx["ui_requester"]`（pipeline 換身分之前存的人類請求者）傳給 `_maybe_trim_after_chat`；若剪成，歸檔與摘要記帳都會落在人類名下，靈魂找不到。實際上因第 3 點從沒剪成，沒有落錯的資料。
5. **IDE 資訊區塊**：`ContextLayer._append_context` 直接接在字串後面，ToukaChat 的多模態 JSON 字串會被串壞、圖片失效。

## 決策

1. **前端自己蓋時間（毫秒，經前端時間 SSOT）**：使用者訊息在送出時蓋（同一次送出只取一次，保存重試時重算的樹相同）；助理回覆在 `persistDone` 蓋完成時間（`stampNodeTime`）。節點已有時間（含後端寫的、重整後接回的）一律不動。伺服器不補，因為會和 ADR-0231 的讀回核對衝突。`MessageNode` 加 `timestamp?`。
2. **舊對話日期是訊息實際時間**：`formatToOpenWebUI` 讀節點 `timestamp`（秒或毫秒都認，`toEpochMs` 和後端 `legacy_time` 同一條 1e11 規則），沒有才照舊用 `createdAt` 或匯出當下。
3. **continuity 照實標示來源**：從對話樹算出的「距上一輪」加註 `(time from the conversation record; may come from the user's device clock)`（常數 `time_utils.CONVERSATION_RECORD_TIME_NOTE`）；記憶體那條是伺服器時鐘，不加〔ADR-0267 修訂：每行標來源，記憶體那條改標 `(server memory)`〕。樹上的秒值（匯入的 Open WebUI 檔）經 `to_epoch_ms` 換成毫秒。不寫任何可信事件（事件表是之後的段落）。
4. **摘要**：送摘要員的每行前面帶台北時間（`legacy_time.format_taipei`，沒有就 `unknown time`）；系統提示加「有可靠時間戳時，把明天／昨天／下週這類相對時間換成絕對日期，不確定保留原話」；回傳的摘要第一行是系統寫的時間範圍（最早～最晚、幾則沒有時間、何時摘要，附同一句來源說明），存在 `context_summary` 文字裡，不改資料表與合約。替換式維持（ADR-0092 不改）。
5. **系統歸檔直接寫舊對話資料庫**：新入口 `LegacySyncService.archive_payload`，滑動視窗剪裁、匯出端點、手動同步三條路共用；底下 `LegacyStore.archive` 一個交易寫完 sessions（標題等中繼資料照 upsert）、訊息（只新增：`ON CONFLICT(id) DO NOTHING`，已歸檔的訊息不被同 id 的新內容蓋掉；其他限制照樣失敗並整筆 rollback）與已處理紀錄。系統不在 `legacy/` 寫檔或搬檔。
6. **剪裁的原子性照舊**：先歸檔成功才剪（ACID-A → 摘要 → ACID-B）；歸檔失敗、或一則都沒寫進去，就不剪、不摘要、不動 session。
7. **同步不搬檔**：舊對話庫新表 `processed_files(filename, content_sha256, processed_at)`，以（檔名, 內容 SHA-256）記已處理，重跑跳過；歸檔失敗不留紀錄、下次重做。沒有 id 的訊息改用決定性 id（session、位置、內容的雜湊），同一份內容重跑不多一份。同步回應多 `skipped`。
8. **「剪下的對話自動同步進舊對話庫」改由歸檔本身達成**：已核准的做法原本是剪裁成功後再 spawn 一次同步；現在剪下就直接寫進資料庫，不另跑同步。
9. **設定頁統計語意改變**：`archived_files` 改成「已處理紀錄數」（原本數 `backup/` 的檔）；`pending_files` 是目錄裡（檔名, 雜湊）還沒處理的 JSON 數。前端欄位與顯示不變。
10. **匯出端點**：回應改成 `{"status": "success", "archived": n}`，不再回實體路徑（加嚴）；`filename` 只當資料庫裡的標籤，不當路徑；沒有可歸檔的訊息回錯誤（否則前端會把那段當作存好而剪掉）。
11. **聊天側剪裁的身分用家對話所屬的靈魂**：`_maybe_trim_after_chat(session_id)` 不再收請求者身分，從 session 的 `home_for_agent` 解析靈魂（判定與設家相同：正式靈魂、ID 不需正規化；身分建法與心跳相同 `identity_service.get_identity`）；解析不到不剪、記 warning，絕不退回人類。
12. **IDE 資訊區塊比照 continuity**：多模態 JSON 字串先還原成 list 再加一段文字（共用 `content_normalize.append_text_part`）。
13. **不變的**：`resource_manager` 的保護一行不改，代理人的檔案工具照樣碰不到 `legacy/`；新寫入口只有上面三個呼叫點，兩個 tools 目錄不得引用（靜態測試釘住）。禁令 28、57 的觸發點、非阻塞、per-session 鎖、只限家對話、排除無痕都不動。Guard #45 基準不動（`legacy_vault`、`recall`、`manager` 都沒改）。

## 後果

- Owner 目前沒有資料：沒有東西遺失，不做搬移。
- 「切分存入舊對話」開始真的會歸檔並剪掉前段；長對話開始真的會被滑動視窗剪裁與摘要（`sliding_window.json.example` 預設 `enabled=false`、`summary_budget_pct=0`，要使用者開才會剪、才會摘要）。
- 剪下的內容只在舊對話資料庫：模型經 `memory_tools(action="random")` 隨機回想讀得到，記憶來源核對查得到「原文仍在」；檔案動詞 `scope="legacy"` 讀的是 legacy 目錄的 JSON，看不到剪下的內容（以前在正式環境也從沒寫成過）。
- 給模型看的「距上一輪」若來自對話樹會多一句來源說明；摘要多一行時間範圍，送摘要員的每行多約 10 個 token。
- 舊對話庫多一張表，只加不改；舊庫第一次寫入時才建，只讀時不建檔、不建表。

## 已知限制

- 這輪助理節點的後端完成時間會被前端完成時間取代（前端整棵寫回；兩者都是完成時間，差網路延遲）。要保留得讓串流終態事件帶後端時間（改合約），這次不做。
- 改用決定性 id 後，舊版用隨機 id 匯入過、且目錄裡還留著的檔，第一次重跑時沒有 id 的訊息會多一份（Owner 無資料，不處理）。
- `backup/` 目錄不再使用；裡面若有舊檔，不再計入統計。
- 剪下的節點內容若不是字串，歸檔會失敗，那個 session 就一直不剪（安全的失敗方向；目前沒有會產生這種節點的路徑）。
- 同步最後讀統計失敗時，整次仍回 SYNC_FAILED（檔已寫入並記為已處理）。

## 驗證

- 前端：`stream_tree.timestamp.test.ts`（送出時間、預設用前端時鐘、不碰既有時間、`stampNodeTime` 對照表、`toEpochMs` 對照表）、`export-utils.timestamp.test.ts`（毫秒、秒、`createdAt`、沒有時間）、`StreamRegistryProvider.persistence.test.tsx`（送出與完成時間、心跳節點時間不變）；共用契約 fixture `tests/fixtures/reliability_session_stream.json` 的 user／assistant 節點加 `timestamp: 1000`。
- 後端：`test_legacy_archive.py`（交易原子性、只新增不覆寫、`upsert_messages` 行為不變、只讀不建表、同步不搬檔與冪等對照表、決定性 id、失敗可重跑、匯出端點與設定頁同步／統計端點在沙盒真實 ResourceManager 上跑、保護沒放寬、工具碰不到新入口）；`test_sliding_window_chat_trigger.py`（家對話用靈魂剪、解析不到不剪並警告的對照表、沙盒真實 ResourceManager 跑「剪裁 → 歸檔 → 手動同步 → 重跑」只落在靈魂名下）；`test_sliding_window_service.py`（先歸檔後剪的對照表）；`test_continuity_block.py`、`test_dialogue_summarizer.py`、`test_content_normalize.py`、`test_prompt_block_delimiters.py` 加新案例；其餘照新行為更新。
- 紅燈：匯出端點在修正前回 500（保護擋下）；原本標 `xfail(strict=True)` 的真實 ResourceManager 端到端測試在修正後轉綠並拿掉標記。
- 數字（merge dev `05be2582` 後）：pytest 全量 9688 passed／7 skipped／6 failed（6 個是既有的 `test_local_compose_bindings`，跳過與基準相同）；ToukaChat Vitest 276 檔 1695 passed；`guard_adr_integrity.py` 0001..0264 連續。
