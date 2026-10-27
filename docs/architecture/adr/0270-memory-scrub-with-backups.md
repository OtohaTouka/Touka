# ADR-0270：記憶系統第二批段 7：重置時一併清除靈魂在備份裡的記憶（清除連備份）

- 狀態：✅ 已接受（維護者 2026-10-15 核准記憶系統第二批計畫，清除連備份只清記憶、預估上限約 +3,100 行、保護全做；本 ADR 是第 7 段的 PR，設計與成果都經對抗式審查；[ADR-0272](0272-iso-c2-agent-command-isolation-and-executor.md) 補註：第 7 段的 6 個 ResourceManager 方法列為權限自癒豁免，快照與記憶庫備份在自癒白名單外；2026-10-16 hist-bak a 補記：刪除與重置畫面加上「還有備份」提醒，見文末「補記：還有備份的提醒」；[ADR-0287](0287-hold-heartbeats-during-destructive-ops.md) 修訂 §6：重置鎖改成不等待的破壞性操作鎖並標記目標，清除旗標也標記目標，刪除靈魂加入互斥）
- 日期：2026-10-15
- 決策者：維護者
- 影響範圍：ToukaProxy（新 `services/memory_scrub/`（`__init__`、`orchestrator`、`leftovers`、`swap`、`qdrant_meta`、`upgrade_backups`、`snapshots`、`ports`；2026-10-28 補記加 `os_metadata`）、新 `services/memory_collection_guard.py`、新 `models/memory_scrub.py`、新 `models/agent_reset.py`、`services/agent_reset/orchestrator.py`、`services/agent_reset/primitives.py`、`services/agent_reset/delete_guard.py`、`services/system_backup.py`、`services/resource_manager.py`、`services/backup_scheduler_service.py`、`services/agent_service.py`、`utils/config_utils.py`、`routers/admin.py`、`routers/system.py`、`routers/debug.py`、`routers/souls.py`、`routers/custom_models.py`、`scripts/manage_backup.py`、`config/errors.json.example`）；ToukaMemory（`factual.py`、`vector.py`、新 `utils/collection_names.py`）；ToukaChat（重置卡、新 `useAgentDataReset`、`AgentResetScrubOption`、`AgentResetScrubResult`、`error_handler`、SoulStudio、ModelStudio、兩語翻譯）；`nginx/nginx.conf.example`；openapi／api.ts／錯誤常數重產；NOQA_REGISTRY `BLE001` +3
- 相關：ADR-0104／0113（重置的備份優先與保留清單）、ADR-0214（測試沙盒）、ADR-0228（Guard #45）、ADR-0030（sqlite3 白名單，照舊）、ADR-0231（結果不明不寫失敗）、ADR-0262（系統快照的單一來源、刪除靈魂前檢查）、ADR-0263（記憶庫 v2）、ADR-0267（活動時間軸）。ADR-0256 留下的「重置時升級備份與系統快照不會一起清」由本 ADR 補上選項（ADR-0256 狀態區已加反向註記）；預設不勾時 ADR-0256 的說法不變。其他 ADR 的決策都不變。

## 背景

- 每靈魂資料重置（ADR-0113）清掉線上資料，但刻意保留「記憶庫升級備份」與「系統快照」；重置後備份裡仍讀得到這個靈魂的記憶，還原任何一份快照就會拿回來。
- 系統快照也會把升級備份包進去。維護者決定：提供一個選項，只清**記憶**（事實與語意索引），而且只給人類管理員。
- 審查時另外發現：向量 collection 名稱把非英數字元換成底線，`a-b` 與 `a_b` 共用同一個 collection；不分大小寫的檔案系統上 `Jin` 與 `jin` 也落到同一個資料夾。一般重置會刪掉整個共用 collection（連別人的向量一起），刪除靈魂前的檢查也會被別人的向量擋住。

## 決策

### 1. 入口與權限

- 只有一個入口：`POST /v1/admin/agents/{target}/reset`（根路徑對等）的 `scrub_backups`（預設 `false`）加第二段確認 `scrub_acknowledged`。兩個欄位都是 `StrictBool`，請求模型 `extra="forbid"`（打錯欄位名 422，不會變成「以為清了其實沒清」）。
- 保證寫成：**需要非代打的人類 admin 登入並重輸密碼**（`require_admin_human`，在驗密碼之前擋；重置協調器也再確認一次，不只靠路由）。CSRF Origin 檢查是多一層防護，但缺 Origin 標頭時會放行；第二段確認是前端的旗標。這兩項都不當作保證。
- 不做成任何工具。允許清單掃描（E10）用 AST 釘住：屬性存取、直接名稱、from-import 的名稱與別名、`**` 展開都算；`reset_agent_data(scrub=)` 只在 `routers/admin.py`；`services.memory_scrub` 只由重置協調器與 `routers/system.py`（只取 `in_progress`）匯入；清除原語、`_execute_reset`、`run_after_reset`、`memory_scrub.begin`、`memory_collection_guard` 都有指定的使用處。

### 2. 範圍

重置完全成功（`clean=True`）後，在重置鎖、清除旗標與備份執行鎖之下，依序處理四個類別，逐項回報：

1. **leftovers**：快照目錄裡上次中斷留下的暫存——自家的 `.memscrub-` 暫存，以及快照腳本的 `.partial`、`.snapshot-<8 個隨機字元>/` 工作資料夾。只刪完全符合形狀的；快照腳本的暫存一組判斷，其中任何一個（含工作資料夾裡的檔）比 10 分鐘新，這一輪全部不碰（命令列快照不經 Proxy 的鎖，可能正在寫）。不符的不碰，記 `unknown_file`。逐項交出，中途出錯已刪的照樣在報告裡。
2. **live**：線上 `core_memory.db` 做 VACUUM，清掉以前刪除留下的殘片。重置後又寫入目標的記憶時不整理（`rewritten_after_reset`；2026-10-27 補記起整理完才發現又有列也報這個代碼）。另外核對磁碟：`_qdrant_db/collection/` 底下若還有和目標同名（含不分大小寫）的資料夾、或這個資料夾本身是連結，記 `live_verify_failed`（Windows 上被占用時 rmtree 會安靜失敗，下次存記憶會把舊點載回）。
3. **upgrade_backups**：`vault/core_memory_backups/` 的每一份升級備份只刪目標的列（所有靈魂共用這個資料夾）。v0、v1、v2 任一版的內容都處理；v2 的列（`status`、`superseded_by`、`safety_flags`、取代鏈、forget 的）一樣整列刪除。
4. **snapshots**：設定頁備份清單上的每一份系統快照（含這次重置前剛做的那份），只改記憶相關的成員：主記憶庫與 zip 裡的升級備份只刪目標的列、拿掉目標的 Qdrant collection（含目錄項目）並同步改 `meta.json`、拿掉中斷暫存成員。其他成員原樣串流照抄。

**不清的範圍**（畫面、本 ADR、SECURITY.md 同一套說法）：快照裡的對話（對話裡提過的事也留在對話紀錄）、活動時間軸、統計、任務、產出；長期記憶（被修剪移入歸檔的舊對話）；舊版 `facts.jsonl`；舊前綴 ID（`ToukaSoul…`）寫進的記憶；不在備份清單裡的備份（例如命令列另存到別處的）；已下載的備份；其他靈魂的資料。舊版 `facts.jsonl` 與舊前綴列處理時會被偵測，回報成 `info`／`out_of_scope`（附數量），不影響「完成」。

**連結**：快照目錄、升級備份資料夾、collection 資料夾本身存在、卻是連結或 junction 時不跟進去，記失敗（`unknown_file`／`live_verify_failed`），不能當成沒東西要處理；只有 lstat 找不到才算沒有。三處與設定頁清單用同一個判斷（`system_backup.path_kind`）。

### 3. 做法

- 每個資料庫都用 `sqlite3.Connection.deserialize` 在記憶體裡打開副本（journal 與暫存都在記憶體），刪除目標的列，再 `VACUUM INTO` 輸出到同資料夾的空暫存檔。刪除前的內容從頭到尾不寫上磁碟。
- 刪除用 `agent_id = ? COLLATE BINARY`（`NOCASE` 欄也不會擴大範圍）；要處理的表由 `MEMORY_SCRUB_TABLES` 登記表決定，有 trigger、view 或沒登記的表一律 `unknown_schema`（fail closed）。
- 要不要處理：有目標列、有空閒頁，或檔案位元組裡找得到目標 ID，就處理（只看目標筆數會漏掉只剩殘片的檔）。
- 一份快照裡的記憶庫逐份讀取→檢查→（需要時）清除→放掉；記憶體峰值約為單一份記憶庫（上限 256 MiB）加上它在記憶體裡的副本與指紋。另設一份快照的總量上限 1 GiB（4×256 MiB），超過記 `too_large`。
- 換檔順序固定：驗證 → fsync 暫存 → 複製原檔的時間與權限 → 再量一次戳記（size、mtime_ns、ino、dev，POSIX 另含 ctime）→ 原子改名 → fsync 資料夾（POSIX）。改名遇到暫時占用（防毒、索引）重試 3 次、每次 0.5 秒，仍占用記 `in_use`。改名之前任何一步失敗原檔不動；改名成功後資料夾 fsync 失敗只記警告，結果仍是 `scrubbed`。
- 原因代碼照實：寫不出暫存、VACUUM INTO 或新 zip 是 `write_failed`；處理到一半原檔不見是 `changed_during_scrub`；線上庫讀不到是 `unreadable`。

### 4. 驗證（獨立重算）

- **資料庫副本**：輸出檔用唯讀連線重開，逐項核對 integrity、結構、`user_version`、目標 0 筆、空閒頁 0，以及**非目標列的多重集合**（在 Python 端依 agent_id 位元組精確分組、逐列雜湊）不變。多刪了別人的列只有多重集合比得出來。
- **zip**：重寫後的驗證只用常數與原始成員資訊重算，不呼叫分類器與 `meta.json` 轉換函式、不看清除時的對照表：成員順序、只少了允許拿掉的成員、目標 collection 不在、其他 collection 目錄集合不變；依常數版面自己找出記憶庫成員（主庫與升級備份資料夾的 `*.db`），每一份在新 zip 裡目標都是 0 筆，內容有變的，非目標列的多重集合要和原本同名成員相同；其他有變的成員只能是 `meta.json`（用常數路徑找），而且要等於「原本拿掉目標」；沒變的成員 CRC、大小、時間、壓縮方式、屬性都要相同。
- **線上庫**：VACUUM 後核對 integrity、結構與 `user_version` 不變、目標 0 筆；不過就是 `live_verify_failed`〔2026-10-27 補記：結構或 integrity 不符才是 `live_verify_failed`；只有目標又有列時是 `rewritten_after_reset`〕（檔案已整理過，畫面請先別再存記憶並找團隊）。

### 5. Qdrant `meta.json`

本機模式的 `meta.json` 是 `{"collections": {...}, "aliases": {...}}`。拿掉 collection 時同步改 `collections`，其他鍵原樣保留；`aliases` 非空一律 `unknown_qdrant_meta`（Touka 不用 alias，有就交給人判斷）。

### 6. 鎖、旗標與並行

- 取得順序固定：重置鎖 → 清除旗標（`memory_scrub.begin()`）→ 備份執行鎖（`run_backup_job`，shield 保護）→ 升級備份鎖（只在升級備份類別）。〔ADR-0287 修訂：重置鎖改由 `destructive_op(target)` 不等待地取，同時標記目標；`memory_scrub.begin(target)` 也標記目標、工作結束時放行；標記期間這個靈魂的心跳不開跑、在下一個脈衝前停下。〕
- 事實庫與已知 ID 在拿任何鎖之前、在工作執行緒裡取得（不卡事件迴圈，也避免在鎖內建構觸發升級而死結）。
- 升級（備份＋輪替）全程持有升級備份鎖；清除等不到記 `lock_timeout`，升級等不到就延後（`LOCKED`，記憶庫不動）。
- 互斥：清除進行中，還原回 409 `MEMORY_SCRUB_IN_PROGRESS`；重置或清除進行中，另一個重置與還原回 409 `AGENT_RESET_IN_PROGRESS`〔ADR-0287：刪除靈魂也加入，三者互斥〕；還原進行中，任何重置（勾不勾清除、含 DebugAgent 重置）回 409 `SYSTEM_RESTORE_IN_PROGRESS`。
- 刪除快照經備份執行鎖：清除進行中刪除快照要等清除跑完；刪除的稽核寫在工作裡，等待中斷線也不會遺失。
- 請求被取消或斷線：清除在 shield 裡照樣跑完；重查、報告與結果稽核都在工作執行緒的 `finally` 裡寫，不靠 await 之後的程式。

### 7. 主庫：secure_delete 加 VACUUM

- `FactualStore` 的每條連線都開 `PRAGMA secure_delete = ON`，刪除與搬動釋出的空間寫成 0。
- 限制：VACUUM 的舊頁面會先進 `-journal` 再刪檔（WAL 模式留在 `-wal`），檔案被刪掉但內容沒有被覆寫；`secure_delete` 只作用在下指令的那條連線。所以保證的是**記憶庫檔與備份檔裡讀不到**，不是硬碟層的鑑識抹除。
- 判斷規則的殘留邊界：事實內容超過一頁時溢出頁只裝內容尾段、不含 ID；secure_delete 上線前釋放的溢出頁被重用後，可能留下「空閒頁 0、又找不到 ID 位元組」的舊殘片，被判成不需處理。secure_delete 上線後不會再產生新的這種殘片（F07b 驗證）；剩下的風險只在本功能上線前、用命令列做的舊快照。

### 8. 結果回報與稽核

- 三層邊界：每一檔（`swap.guard_item`）、每一類別（類別失敗也產生 `kind=category` 的項目）、整個清除（`run_after_reset` 的外層攔截，連準備步驟出錯也在內，端點照常回 200）。
- `complete` 只有在四類都 `done`、每一項成功（`scrubbed`／`not_needed`／`removed`／`info`）、重查線上殘留為 0、這次重置前做的快照以快照項目出現在結果裡，而且（重置清掉的記憶大於 0 時）那份快照真的清掉了東西（`scrubbed` 且刪了事實或拿掉 collection）時才為真。不完成時 `reason` 取第一個失敗類別，否則依序是 `internal_error`、`recheck_failed`、`own_snapshot_missing`、`own_snapshot_unverified`；只有個別檔失敗時是 `null`。
- 稽核：
  - 勾選清除、重置開始前被擋：`AUDIT: memory-scrub rejected actor=%r target=%r reason=%s`，原因有 `not_human`、`not_acknowledged`、`not_confirmed`、`bad_origin`、`bad_password`、`restore_in_progress`、`reset_in_progress`、`not_resettable`、`collection_shared`、`unavailable`、`internal_error`。格式只有清除協調器一份，路由經它寫。
  - 其他結局一律寫一行結果稽核（ID、檔名、代碼與筆數，沒有內容與路徑；檔名 repr，不能偽造另一行）：重置沒完全成功（`reset_not_clean`）、交給清除工作前就結束（`backup_failed`、`cancelled`、`internal_error`，`ran=False`），以及清除跑完（含取消後在背景跑完）。
- 前端：結果畫面沒完成時總結句一律寫「清除沒有完成：原因」，不拿檔數下結論；只有四類都跑完、只有個別檔沒清掉時，另一句照實寫「這 N 個檔維持原樣…其他 M 個已清除」。沒跑完的類別與沒清掉的中斷暫存逐項列出。只有 `clean` 為真而且（沒勾，或 `scrub.complete` 為真）才跳成功；勾了但回應沒有 `scrub` 時寫「伺服器沒有執行清除」。
- 斷線、逾時、沒有 Touka 錯誤碼的錯誤回應、或閘道回的 502／504／524，畫面寫「結果不明」（請求自帶 signal，`fetchTouka` 不套連線逾時）；伺服器自己回的錯誤碼（例如 503 `MEMORY_SCRUB_UNAVAILABLE`）仍是明確拒絕。

### 9. 不變的地方

- sqlite3 匯入白名單不變：所有 SQLite 操作都在 `factual.py` 裡。
- Guard #45 基準不變，`manager.py` 不改；新邏輯在 consumer 檔以外的新模組。

### 10. collection 撞名的兩道防護（不改命名）

判定 `names_fold_equal`：相等、casefold 相同或 upper 相同任一成立（只用 casefold 會漏掉土耳其文 ı／i，只用 upper 會漏掉 ς／σ 與 K）。已知 ID 是靈魂、自訂模型、agents_registry 的鍵與事實庫裡的 agent_id 四個來源的聯集；registry 存在卻讀不出來時拒絕（不能確認就不放行）。

1. **重置與刪除**：撞名時一般重置只刪目標自己的點（先刪向量、再刪事實，可重跑；刪除與計數用 BINARY 比對），殘留自檢只數不屬於別人任何事實的點；判定出錯就往外丟，絕不退回整個 collection 刪除。刪除靈魂前的檢查用同一套判斷。勾選清除時撞名，預檢直接拒絕（409 `MEMORY_SCRUB_COLLECTION_SHARED`，附撞到的 ID），什麼都不刪；快照裡另外核對目錄名與 `meta.json` 的鍵。
2. **建立**：建立靈魂、建立自訂模型、自訂模型改名時，會撞名的 ID 回 409 `AGENT_ID_COLLECTION_COLLISION`（附 `collides_with`，畫面顯示撞到哪個 ID）；事實庫讀不到時回 503 `AGENT_ID_CHECK_FAILED`（不能確認就不建立）。改名時舊 ID 只從名稱來源排除，舊 ID 在事實庫裡的記憶照樣算撞名（改名不搬記憶）。檢查在工作執行緒裡跑。

### 11. 快照列舉的單一來源與端點收緊

清單、統計、排程修剪、清除、還原、下載、刪除都用 `system_backup.list_snapshot_files()`／`is_listed_snapshot()`：頂層、`lstat` 判為一般檔（符號連結、junction 等 reparse point 不算）、不是點開頭、名稱不含 `:`（Windows 替代資料流）、副檔名不分大小寫是 `.zip`。快照目錄本身是連結時，清單是空的（清除用嚴格模式，記失敗）。

### 12. 部署：nginx 範例

`nginx/nginx.conf.example` 只替重置端點（`location ~ ^/(v1/)?admin/agents/[^/]+/reset$`，`/v1` 與根路徑同一條）把 `proxy_read_timeout` 拉長到 1800 秒（30 分鐘），其他設定照抄 REST 的 `location /`；regex location 的 `proxy_pass` 不帶路徑，upstream 拿到的路徑不變。其他 REST（含其他 admin）仍是 nginx 預設 60 秒。

30 分鐘的估法【推測】：清除逐份處理快照，每份要讀整個 zip、重寫一份、再讀回驗證，約為快照大小的 3 倍 I/O；以慢速磁碟 50 MB/s 估，30 分鐘約可處理 30 GB 的快照總量。超過時前端顯示「結果不明」，伺服器照樣跑完、結果稽核照樣寫。

## 一般功能的行為改變

- **預設不勾選時，重置仍保留備份裡的記憶**（和以前一樣）。
- 還原進行中不能重置（勾不勾清除、含 DebugAgent 重置）；重置或清除進行中不能還原。
- 重置進行中改回 409 `AGENT_RESET_IN_PROGRESS`（原本是寫死中文的 422）。
- 還原、下載、刪除只接受清單上的檔名；刪除快照會等候正在進行的備份或清除。
- **快照目錄（`backups`）本身是連結或 junction 時，設定頁清單看不到快照**（以前會跟著連結列出），清除記失敗。
- `create_backup` 的回應拿掉 `output`（腳本輸出含絕對路徑），改回這次新增的快照檔名 `filename`（備份鎖內比對前後清單；不是剛好多一份就填 `null` 並記警告）；前後列目錄失敗也算 `SYSTEM_BACKUP_FAILED`。
- 建立靈魂與自訂模型（含改名）時拒絕撞名的 ID；**自訂模型改名時，舊 ID 的記憶照算撞名**；記憶系統讀不到時不能建立。
- 撞名的靈魂一般重置只刪自己的點（以前會刪掉整個共用 collection）。
- 事實庫每條連線開 `secure_delete`；刪除與計數一個靈魂的事實用 BINARY 比對。
- 重置卡：閘道逾時與沒有錯誤碼的回應顯示「結果不明」；ModelStudio 存檔被拒時不再顯示「已存好」。

## 替代方案

- 另開「只清備份」的端點：多一個破壞性入口，權限與稽核要重做一份；不採。
- 整份刪掉快照：連別的靈魂與其他資料一起沒了；不採。
- 只開 secure_delete、不做 VACUUM：既有殘片還在；不採。
- 在磁碟上做副本再刪：刪除前的內容會寫上磁碟；改用記憶體副本。
- 每份快照都重寫：沒有目標的快照也被改動（時間、風險）；只處理需要的。
- 產品端也用 Qdrant 本機模式打開驗證：要載入向量模組與大量相依；改成測試端驗證（S02）、產品端獨立重算。
- 改 collection 的命名方式：會牽動所有既有資料；不採，改用兩道撞名防護。
- 在快照裡只刪目標的點：要在產品端操作 Qdrant 的儲存格式；撞名的靈魂改成不能勾選清除。
- 依 payload 內容判斷撞名：payload 不一定可信、也要載入向量庫；改用已知 ID 的聯集。
- 背景工作加輪詢：多一套狀態與端點；改成請求內完成、斷線也留稽核。
- 擁有者限制（只有靈魂的擁有者能清）：見下方限制最後一點。
- 不認得的中斷暫存只算資訊、不算失敗（成果審異議）：不採，維持 fail closed——`.partial` 可能是完整的重置前 zip，`.snapshot-` 工作資料夾裡可能有記憶庫副本，內容不明的不能當成沒事；改成在畫面逐項列出檔名與原因，由人判斷要不要刪。

## 後果與限制

- 勾選後無法復原，包括重置前剛做的那份快照。
- 已下載的備份不受影響；命令列另存的備份不在範圍內。
- Linux 上正在被下載的那份快照，下載者拿到的是舊內容（改名取代不影響已開的檔）。
- 撞名的靈魂不能勾選清除。
- 判斷規則的殘留邊界（決策 7）。
- 命令列快照連續 600 秒沒有寫入 `.partial` 與工作資料夾時（行程被暫停、或檔案系統時鐘比 Proxy 慢 10 分鐘以上），清除會把它當成中斷殘留刪掉；那次備份會在最後改名時失敗、不留壞檔（2026-10-25 補記，舊審查報告丙-1 查證）。
- 線上庫 VACUUM 超過事實庫鎖等候時間（5 秒）時，其他靈魂同時存記憶可能失敗。
- 每份資料庫副本都在記憶體裡處理（上限 256 MiB），容器的記憶體需求跟著增加。
- 快照目錄本身是連結時，清單看不到快照、清除不能完成（要改成真的資料夾）。
- 閘道逾時（預設 60 秒）的部署沒有照新範本加長時，清除超過 60 秒畫面會顯示「結果不明」。
- 多個管理員時，任何 admin 都能清除任何靈魂的備份記憶：權限只限「人類 admin」，單一管理員的部署不受影響；加擁有者限制可能擋到管理員自己對未登記擁有者的舊靈魂。

## 和設計不同的地方

- `BLE001` 新增 3 個（設計寫 2 個）：`swap.guard_item`（每一檔）、`memory_scrub/orchestrator._attempt`（每一類別與重查）、`run_after_reset`（整個清除的安全網）。
- 重置協調器拆成 `_execute_reset`、`_reset_stores`、`_prepare_scrub`、`_preflight_scrub`、`_scrub_dependencies`、`_scrub_after`；`primitives` 新增公開的 `identity_for`、`memory_fact_store` 與 `MEMORY_STORE_NAME`。「重置進行中」在協調器裡判斷（驗密碼之後）；還原進行中與第二段確認在路由、驗密碼之前擋。
- 快照撞名檢查另外核對 `meta.json` 的鍵（Windows 上只靠目錄名抓不到大小寫不同的撞名）。
- 中斷暫存刪不掉時原因代碼是 `in_use`。
- 前端：撞名時從錯誤內容讀出撞到的 ID 顯示；閘道逾時與沒有錯誤碼的回應算「結果不明」（成果審彙整原寫「503 也算」，伺服器自己的 503 都發生在什麼都還沒做之前，所以只有沒錯誤碼的 503 算）。
- 成果審之後補做：見決策 2～12 與上方行為改變（結果畫面、所有結局的稽核、沙盒守衛改成吞不掉、連結、自己那份快照要真的清掉東西、線上 collection 磁碟核對、記憶體改逐份、獨立驗證不看對照表、原因代碼、換檔重試與 fsync、暫存成組判斷、雙向互斥、名稱含 `:`、`fsync_file` 保護、建立時撞名的範圍與畫面、nginx 範例）。

## 影響

| 決策 | 檔案 | 主要函式 | 測試 |
|---|---|---|---|
| 1 入口與權限 | `routers/admin.py`；`services/agent_reset/orchestrator.py` | `AgentResetRequest`、`reset_agent`、`_scrub_rejected`；`reset_agent_data`、`scrub_rejected` | `test_memory_scrub_endpoint_security.py`（E 組、E10／E10b） |
| 2 範圍 | `services/memory_scrub/`（`leftovers`、`orchestrator`、`upgrade_backups`、`snapshots`） | `iter_snapshot_leftovers`、`_live`、`_leftover_collections`、`iter_upgrade_backups`、`iter_snapshots` | `test_memory_scrub_upgrade_backups.py`、`test_memory_scrub_snapshots.py`、`test_memory_scrub_orchestrator.py` |
| 3 做法 | `factual.py`；`services/memory_scrub/swap.py`；`services/resource_manager.py` | `inspect_store_image`、`scrub_store_image`、`_vacuum_into`；`replace_verified`、`_rename`、`guard_item`、`folder_problem`；`rewrite_archive`、`copy_file_metadata`、`fsync_file`、`fsync_dir`、`read_file_prefix`、`make_writable` | `test_memory_scrub_store.py`（F 組）、`test_resource_manager_archive.py`（R 組） |
| 4 驗證 | `factual.py`；`services/memory_scrub/snapshots.py` | `_verify_scrubbed`、`compact_after_reset`；`_verify_rewrite`、`_verify_store_member`、`_verify_meta` | F、S 組（S21～S22、S28） |
| 5 meta.json | `services/memory_scrub/qdrant_meta.py` | `collection_names`、`without_collection` | S 組（S02、S10、S22） |
| 6 鎖與並行 | `factual.py`；`services/memory_scrub/__init__.py`；`services/agent_reset/orchestrator.py`；`routers/system.py`；`routers/debug.py` | `upgrade_backup_lock`、`_upgrade_with_backup_lock`；`begin`、`in_progress`；`reset_in_progress`；`run_restore`、`delete_backup`；`debug_reset` | O 組、E12～E15、E19、E20、L08 |
| 7 secure_delete | `factual.py` | `_connection` | F07、F07b |
| 8 結果與稽核 | `models/memory_scrub.py`；`services/memory_scrub/orchestrator.py`；ToukaChat 重置卡、`AgentResetScrubResult`、`useAgentDataReset` | `MemoryScrubReport.build`；`run_after_reset`、`audit_result`、`audit_rejected` | O07、O07b、O08、O08b、O12、O12b、O17～O19；V01～V10、V07b |
| 10 撞名 | `services/memory_collection_guard.py`；`services/agent_reset/primitives.py`、`delete_guard.py`；`routers/souls.py`、`routers/custom_models.py`；`services/agent_service.py` | `known_memory_ids`、`colliding_ids`、`assert_id_free`；`purge_memory`、`count_remaining_memory`、`assert_memory_cleared`；`registered_ids` | `test_memory_collection_collision_security.py`（G 組） |
| 11 快照列舉 | `services/system_backup.py`；`services/backup_scheduler_service.py`；`scripts/manage_backup.py` | `path_kind`、`is_snapshot_name`、`list_snapshot_files`、`is_listed_snapshot`、`create_backup`；`SNAPSHOT_WORKDIR_PREFIX` | `test_snapshot_listing.py`（L 組、E13） |
| 12 nginx | `nginx/nginx.conf.example` | 重置端點的 location | `test_nginx_reset_location_only_extends_the_read_timeout` |

## 驗證

本機 Windows、3.12 venv（`uv run --locked`）、fnm Node 24.21.0。自動測試都在 conftest 沙盒或 pytest `tmp_path` 裡：合成資料庫與 zip、tmp 本機 Qdrant；清除測試用正面白名單守衛（快照目錄換成 tmp、路徑來源被呼叫就丟 `SandboxBreach`，它是 BaseException，產品的 `except Exception` 吞不掉，每次違規也記下、收尾斷言是空的；寫入與讀取出口只准 tmp）。不讀 `data/`、不載入嵌入模型、不打付費 API，也沒有啟動 Touka。

- 新測試（pytest）：`test_memory_scrub_store.py` 66、`test_memory_scrub_upgrade_backups.py` 23、`test_memory_scrub_snapshots.py` 49、`test_memory_scrub_orchestrator.py` 42、`test_memory_scrub_endpoint_security.py` 29、`test_memory_collection_collision_security.py` 25、`test_snapshot_listing.py` 33；`test_resource_manager_archive.py` 加 R01～R05。前端：`AgentDataResetCard.scrub.test.tsx`、ModelStudio 撞名、`error_handler` 兩條。先紅後綠。
- 突變：設計表 42 列（M8、M35、M39 兩種改法分開跑，共 45 次）、合併記憶庫 v2 後 3 條（Mv2、Mg1、Mg2）、成果審修正後 16 條（R1a～R13、R4c），合計 64 條，最後全部有測試變紅；第一輪倖存的（S19、O14、M34、M39b）都已補強測試或改寫等價的突變後變紅。M26／M26b（清除程式自己推路徑）現在直接以沙盒違規失敗。
- 全量（最後 merge dev `8e565b33` 並配號後）：pytest 10355 passed／7 skipped／6 failed（6 個是本機 Docker Desktop 的 `test_local_compose_bindings`，以 CI 為準；7 個跳過與 ADR-0251 的基準相同）；vitest 277 個檔、1718 個測試全過，前端守衛都通過（merge `583e1c7f` 後跑，之後合進來的 dev 沒動前端）。`guard_adr_integrity.py`（0001..0270 連續、修訂雙向一致）、`guard_noqa_registry.py`、`lint_ssot.py`、`guard_native_tool_contracts.py`（基準不動）、`guard_module_boundaries.py`、文件契約測試、ruff（改到的產品程式沒有新增問題）通過。nginx 範例本機沒有 nginx 可跑 `nginx -t`，由測試核對設定照抄、只多逾時、regex 只涵蓋重置端點。

## 回復

- 不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。程式可以單獨撤；Guard #45 基準沒有動。
- 撤回後：勾選欄位消失、重置回到不清備份；`secure_delete` 與撞名防護一起撤回時，撞名靈魂的一般重置會恢復刪整個 collection 的舊行為，要一起評估。已經清掉的備份記憶無法復原（撤回不會帶回來）。

## DATA_PROTECTION

本功能是產品受管行為：由人類管理員在產品介面上勾選並確認後，由 Touka 執行，屬維護者授權的 runtime 行為。開發代理不因此取得直接修改 `data/` 的授權；所有自動化測試都在 `tmp_path` 沙盒裡，用正面白名單守衛擋住路徑來源與寫入、讀取出口。

## 補記：還有備份的提醒（2026-10-16，hist-bak a）

維護者選擇保留「一併清除備份裡這個靈魂的記憶」的勾選、只補提醒。只改 ToukaChat 畫面與兩語翻譯；後端、API、openapi 與清除本身都不變，**預設仍不清備份**。

- **放在哪裡**：重置卡的確認畫面（「將清除／將保留」下方）與完成畫面（清除連備份完整完成時換成另一版：記憶已從備份清掉，但對話與其他資料還留在系統快照裡，只列快照位置、刪整份快照與還原的副作用，不提升級備份）；SoulStudio 刪除靈魂的確認框，與完成訊息（toast 的說明行）；DebugAgent 重置區（計分卡面板）。三處共用 `components/common/BackupRetentionNotice.tsx`（`variant` 決定列哪些句子），翻譯在 `backup_notice.*`。
- **寫什麼**：資料還留在系統快照裡、記憶也可能留在升級備份裡（刪除畫面寫「可能還留在之前的快照」，因為刪除前不做快照）；系統快照在 `.env` 所在資料夾的 `backups`（`Touka_Backup_*.zip`、`Touka_AutoBackup_*.zip`），升級備份在資料資料夾的 `vault/core_memory_backups`；要徹底刪：重置時勾選（只清這個靈魂的記憶）、或到「設定 → 系統備份」刪整份快照（連其他靈魂一起刪）、升級備份是共用檔只能手動刪；DebugAgent 沒有勾選，只寫刪整份快照；刪除前不會自動存快照；還原舊快照會把這個代理人的舊資料帶回來，同時所有靈魂的記憶退回那個時間點；這裡的系統快照和重置會刪的情境快照是兩回事。
- **伺服器不回傳路徑或份數**：位置是寫死在畫面上的固定文字（決策 8 拿掉 `output` 的理由不變）。要讓伺服器回傳快照份數或升級備份份數，會改到 API，另開 ADR。
- 不寫成備份的：執行器裡的代理人家目錄（ADR-0272，不是備份）；ADR-0256 附錄「受 WORKSPACE_ROOT 影響」的位置說法已過時，提醒以 ADR-0262 決策 6 為準。
- 測試（Vitest）：`BackupRetentionNotice.test.tsx` 6（兩語每句都有、位置字樣、不提過時位置與執行器；四種版本各列哪些句子）、重置卡 +2、清除連備份 V11（完整完成與沒完成兩種結果）、SoulStudio +2、計分卡 +1；先紅後綠，25 條突變全部變紅。

## 補記：清除的小修（2026-10-27，舊審查報告丙-3）

- **線上庫驗證拆開（修訂 §4 線上庫那一條）**：VACUUM 之後結構或 integrity 不符才報 `live_verify_failed`；只有目標又有列時報 `rewritten_after_reset`。兩者同時發生時報比較嚴重的 `live_verify_failed`。查過「目標 0 筆」之後、VACUUM 之前別的連線可能寫進來（SQLite 不能在交易裡 VACUUM，報告建議的 `BEGIN IMMEDIATE` 做不到）；VACUUM 不會多出資料列，多出來的只可能是重置後的新寫入，原本把它說成「驗證失敗、請找團隊」太嚴重。
  - `rewritten_after_reset` 從此有兩種情況：整理前就發現（沒有整理，§2 原本的意思）與整理完才發現。畫面不再說「線上記憶庫沒有整理」，改成新句 `live_rewritten`（重置之後又寫入了這個靈魂的記憶，線上記憶庫裡又有它的記憶）。
- **中斷暫存在清的時候消失**：快照腳本的 `.partial` 在列出之後才被發布（改名成正式快照）時，原本判斷「有沒有太新」的 `lstat`、或檢查內容時開檔丟 FileNotFoundError，整個 leftovers 類別失敗（`list_failed`）；刪工作資料夾時才不見則記成刪不掉（`in_use`）。刪單一檔本來就是 `missing_ok`，不見照樣算刪掉。現在：
  - 判斷新舊時，消失的部分（含工作資料夾裡的檔）不算；
  - 每一項處理前已經不在就跳過；檢查內容或刪除時才丟 FileNotFoundError，這一項真的不在了才跳過，新的正式快照會在同一個清除工作稍後的快照類別被清到；
  - 這一項還在（例如工作資料夾刪到一半、裡面的檔被別人先刪掉）就記成刪不掉（`in_use`）：裡面可能還有記憶庫副本，內容不明的不能當成沒事。
- **畫面**：清除完成、但有備份還留著不在範圍的舊格式或舊前綴資料（`out_of_scope` 資訊項，每份備份一項）時：
  - 總結句改用 `summary_complete_out_of_scope`：每一份快照與升級備份都已清除範圍內的記憶；其中 N 份還留著舊格式或舊前綴的記憶，見下方清單。數字是備份份數，筆數在清單裡（同一份資料在幾份快照裡就各算一份）。
  - 完成畫面的「還有備份」提醒改用新版 `reset_scrubbed_partial`：照實說留在備份裡，列快照與升級備份的位置與刪法、還原的副作用，不叫人再勾選（再清一次也清不到）。
  - 原本會說「每一份都已清除」「記憶已從備份清掉」。兩語都加鍵。
- **後果補一句（不改）**：短 ID 用子字串比對抓頁內殘片（§3 的刻意設計），所以快照每次清除都會重寫；安全但慢，也會讓「結果不明」更常出現。
- 測試：
  - `tests/unit/test_memory_scrub_store.py` F08c（查過之後別的連線寫進一筆 → `rewritten_after_reset`）、F08d（integrity 不過又有新寫入 → `live_verify_failed`）。
  - `tests/unit/test_memory_scrub_snapshots.py` S18d（列出後被發布 → 跳過，另一個夠舊的照清）、S18e（檢查內容時才不見 → 跳過；刪工作資料夾丟 FileNotFoundError：資料夾真的不見才跳過，還在記 `in_use`）、S18f（工作資料夾裡的檔在判斷新舊時被搬走 → 那個檔不算）。
  - `BackupRetentionNotice.test.tsx` B05b 與兩語新鍵；`AgentDataResetCard.scrub.test.tsx` V05b（完成但有不在範圍的資料：總結句寫份數、提示換版）、V05c（又有新寫入：不說有沒有整理）。
- 變異檢查第 1 輪 9 個全部變紅（線上庫報錯、leftovers 四處、`_mtime`、總結句、筆數、提示版本）；審查後補的測試另外擋住：兩個檢查順序對調、消失時當成「太新」、資料夾還在卻跳過、提示不列升級備份的刪法。

## 補記：作業系統放的雜檔（2026-10-28，舊審查報告丙-3b）

- **問題**：Windows 檔案總管會在資料夾裡放 `Thumbs.db`（縮圖快取）、`desktop.ini`（資料夾設定），macOS 放 `.DS_Store`。升級備份資料夾（`vault/core_memory_backups/`）裡不認得的檔原本一律記 `unknown_file`，快照裡同一個位置的成員記 `unknown_member`（fail closed），所以只要有人用檔案總管打開過這個資料夾，清除就永遠不會「完成」。另外 `Thumbs.db` 結尾是 `.db`，獨立驗證會把它當記憶庫打開，得到 `verify_failed`。快照目錄本身（leftovers）不受影響：名字不符任何暫存樣式的檔本來就不列。
- **決定**：
  - 一份名單 `services/memory_scrub/os_metadata.py` 的 `OS_METADATA_NAMES`：`thumbs.db`、`desktop.ini`、`.ds_store`。比對不分大小寫，但名字必須全是 ASCII（克耳文符號 `K` 這類非 ASCII 字元轉小寫後才相同的不算）。
  - 名字相符**而且**是一般檔、不超過 4 MiB、開頭不是 SQLite 檔頭、內容找不到目標 ID（UTF-8、UTF-16LE、UTF-16BE 三種寫法；`desktop.ini` 常是 UTF-16LE，`.DS_Store` 的檔名是 UTF-16BE），才當雜檔。先看大小（檔案大小、zip 宣告的大小；zipfile 讀出的不會超過宣告的大小），太大的不讀。
  - 升級備份資料夾裡的雜檔：原樣不動，記成資訊項 `info`／`os_metadata_file`（不附數量）。不影響「完成」，也不算沒清掉的檔；畫面在「其他資訊」列一行，不寫數量（新鍵 `info_line_plain`）。
  - 快照裡升級備份資料夾的雜檔成員：照抄。獨立驗證認得同一份名單，不當記憶庫打開；成員不能有變，大小、SQLite 檔頭、目標 ID 都自己重算一次，不呼叫分類那邊的內容判斷。升級備份資料夾裡留下來的成員只能是記憶庫或雜檔，其他名字驗證失敗。
  - 其他情況照舊 fail closed：名字相符但是資料夾、太大、是 SQLite、含目標 ID，或名字只是相近（例如 `Thumbs.db.old`）。這不放寬 §2 的範圍：雜檔不是記憶，含目標 ID 的仍然擋下。
- **限制**：目標 ID 很短（例如 `Al`）時，雜檔內容可能剛好含那幾個位元組，那時照舊記失敗、清除不會完成（安全的方向）。現場的雜檔刪掉再清一次就好；但已經打包進快照的（例如重置前剛做的那份），那份快照每次都會算沒清掉，要另外處理那份快照（刪掉，或確認裡面沒有要清的記憶後接受「沒有完成」）。
- **測試**：
  - `tests/unit/test_memory_scrub_upgrade_backups.py` U19（三種雜檔記成資訊、原樣保留）、U20（SQLite 檔頭、含目標 ID 的三種寫法、非 ASCII 的相近名字、`Thumbs.db.old` → `unknown_file`）、U21（同名資料夾、太大的不讀 → `unknown_file`）、U22（內容判斷的大小上限）、U23（讀雜檔出錯只記那一檔失敗）。
  - `tests/unit/test_memory_scrub_snapshots.py` S33（雜檔成員照抄、快照照清）、S34（SQLite 檔頭、含目標 ID 的三種寫法 → `unknown_member`）、S34b（太大的不讀 → `unknown_member`）、S34c（同名的資料夾項目 → `unknown_member`）、S35（分類那邊放過含目標 ID 或 SQLite 的雜檔，三個名字與大小寫都測，驗證抓到）、S35c（分類那邊漏查大小，驗證抓到）、S35d（分類那邊把別的名字當雜檔，驗證只放行記憶庫與雜檔）、S35b（重寫時雜檔被改，驗證抓到）。
  - `tests/unit/test_memory_scrub_orchestrator.py` O28（升級備份資料夾有 `Thumbs.db` 時，連同重置前的快照一起清除完成；稽核行的資訊項不附數量時不寫 `:None`）；`AgentDataResetCard.scrub.test.tsx` V05e（資訊行不寫數量、總結照樣是完成）。
