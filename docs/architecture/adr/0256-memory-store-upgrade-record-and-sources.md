# ADR-0256：記憶系統第一批段 2～4：記憶庫結構升級與備份、事實庫是正本、記憶來源

- 狀態：✅ 已接受（維護者 2026-10-15 核准記憶系統第一批計畫；本 ADR 是段 2～4 的 PR。段 2 的計畫與成果各經一次對抗式審查，維護者同意合併；段 3、段 4 照一般驗收）
- 日期：2026-10-15
- 決策者：維護者
- 影響範圍：ToukaMemory（`factual.py`、新 `services/index_state.py`、新 `models/source_ref.py`、`protocols.py`、`services/manager.py`、`services/legacy_store.py`、`services/reembed.py`、`scripts/reembed_memory.py`、`vector.py`、`tools/diag.py`、`tools/contracts.py`、`tools/__init__.py`）；ToukaProxy（`main.py` lifespan、`routers/observability.py`、`routers/proxy.py`、`routers/heartbeat.py`、`models/context.py`、記憶閘道 `tools/builtin/memory.py`、新 `services/memory_source_resolver.py`、`services/embedding_service.py`、`services/agent_reset/primitives.py`、`config/auth_policies.json.example` 與被追蹤的 `config/auth_policies.json`）；ToukaChat（`MemoryNoticeBar`、`VaultDashboard`、兩語翻譯）；Guard #45 基準（`scripts/native_tool_contract_baseline.json`）；NOQA_REGISTRY `S603`
- 相關：ADR-0070（事實庫＋向量庫）、ADR-0078（失效可觀察性）、ADR-0086（無痕零殘留）、ADR-0116（整合模式單一部署）、ADR-0118（嵌入模型下載、切換與離線重建）、ADR-0214（測試沙盒）、ADR-0228（原生公開合約、可信參數、Guard #45）、ADR-0246（Manual／Auto／YOLO）、ADR-0253（記憶提示管道）。這些 ADR 的決策都不變；ADR-0253 列的已知限制「`query_facts` 把讀取失敗吞成空清單」由本 ADR 修正。
- 後續：決策段「逐個靈魂清除或重置記憶時，升級備份與系統快照不會一起清」與附錄「還原後會回來什麼」——重置時可勾選一併清除這個靈魂在升級備份與系統快照裡的記憶，見 [ADR-0270](0270-memory-scrub-with-backups.md)（2026-10-15）；預設不勾時這裡的說法不變，勾選並完成後還原任何一份清單上的快照都拿不回那個靈魂的記憶。

## 背景

- **記憶庫沒有版本**：`core_memory.db` 是所有靈魂共用的一個 SQLite 檔。`FactualStore._init_db` 每次建構都跑 `CREATE TABLE IF NOT EXISTS`，再加兩句吞掉所有錯誤的 `ALTER`；沒有版本、沒有備份，失敗也看不到。`query_facts` 把讀取錯誤吞成空清單，壞掉時看起來像「沒有記憶」。段 3、段 4 要加新欄位，下次更新重啟時 Proxy 會在真實記憶庫上升級一次，開發時沒辦法先拿真資料試。
- **向量失敗會刪事實**：存記憶的順序是「向量查重 → 寫事實庫 → 寫向量」。向量寫入回 False 時，`save_memory` 呼叫 `delete_fact_permanently` 把剛存的事實刪掉；建 collection 失敗時例外直接外拋、事實卻留著，而且沒有任何標記；取不到向量庫（例如 Qdrant 本機鎖被別的行程佔住）時整個存記憶失敗。`forget` 在事實庫封存後才改向量 payload，向量庫失敗時，語意查詢會把已封存的記憶再拿出來（語意結果直接用向量 payload，不回查事實庫），查重也會被已封存的相似事實擋住。沒有方法知道哪些事實沒有向量，也沒有補的入口。
- **不知道記憶從哪來**：執行器早就把可信的 `request_id`、`session_id`、`_call_id`、`_ctx` 注入每次工具呼叫（ADR-0228），但記憶閘道在轉給記憶系統前全部剝掉；facts 沒有來源欄位，查記憶也帶不出「這段記憶從哪來」。UI 的 user 節點 id（`ui_persistence.user_parent_id`）和心跳的觸發節點沒有進 pipeline context。

## 決策

### 段 2：記憶庫結構版本、升級備份與失敗狀態

#### 2-1. 單一版本 v1、只增、凍結

- 結構版本 SSOT 是 `memory_server.factual.FACTS_SCHEMA_STEPS`，已套用的版本記在 `PRAGMA user_version`；程式要的版本由 `_target_version()` 每次從步驟表推導。
- 段 2～4 的欄位全放進 v1，一次備份、一次交易：`importance`、`emotions`（補極舊的庫）、`embedding_state`（預設 `'unverified'`）、`embedding_model`、`embedding_updated_at`（秒）、`sources`。
- 每一版只能 ADD COLUMN（可 NULL 或常數預設）或 CREATE TABLE／INDEX IF NOT EXISTS（不含 UNIQUE）。不准 DROP、RENAME、改型別、沒有預設值的 NOT NULL。測試逐句檢查，並檢查原始碼不用「一次跑多句」的 API（它會先提交進行中的交易、放掉鎖）。
- 本 ADR 合併後 v1 凍結（`test_t17_v1_is_frozen`），之後的變更只能往後加 v2。破壞性變更要另開 ADR，並把「版本比程式新就照用」改成唯讀。

#### 2-2. 只在 Proxy 啟動時升級，不依賴跨 OS 檔案鎖

- lifespan 在 `collect_memory_notices()` 之後、任何 `spawn_logged` 與 `start_bots()` 之前呼叫 `_prepare_memory_store(app)` → `FactualStore.shared()`，同步跑完。yield 前不接請求，系統備份排程也還沒開始。
- `FactualStore.shared()` 是行程內共用實例（`MemoryManager.get_factual_store()` 也回它），啟動時不載入向量模組（torch）。`MemoryManager.reset_singletons()` 一併 `FactualStore.reset_shared()`；換嵌入模型後下一次取用時重建，結構仍落後就再試一次，這是唯一「執行中」重試升級的路徑。啟動時建不出事實庫（例如資料夾權限）時，第一次使用會在那個請求裡建構並升級（同步，比照啟動）。
- 離線工具 `reembed_memory.py` 經 `open_factual_store_for_reembed()` 用 `allow_schema_upgrade=False`：版本落後回 OUTDATED，不拿鎖、不備份、不寫，工具拒絕執行並提示先啟動 Touka。
- 流程：讀寫連線讀 `user_version`（當機殘留的 journal 先被 SQLite 回滾）→ 落後就用 `isolation_level=None` 的連線 `BEGIN IMMEDIATE`（最多等 `FACTS_SCHEMA_LOCK_TIMEOUT_S`）→ 鎖內重讀版本（別人剛升完就照新結果）→ 記一行現況（journal_mode、user_version、page_count）→ 清中斷留下的備份暫存檔 → 舊庫先備份 → 基準建表與各版步驟逐句執行（欄位先查再加）→ 寫版本 → COMMIT。
- 任何例外記一次 WARNING（含 traceback）、分類成失敗狀態、回滾；`_init_db` 最外層再收一次，單例一定建得起來。升級連線離開時不自動提交：連 ROLLBACK 也失敗時，關閉連線由 SQLite 回滾，做了一半的升級不會被提交。

#### 2-3. 升級備份

- 位置：`core_memory.db` 旁的 `FACTS_BACKUP_DIR_NAME`（`core_memory_backups`），從 db_path 推導。建資料夾、改名、刪檔、列檔都經資源代理（`IResourceProxy` 補宣告 `move_file`、`iter_files`）。
- 名稱：`core_memory.v{舊}-to-v{新}.{epoch}.db`；同一秒撞名加 `.{n}`（最多 `FACTS_BACKUP_NAME_MAX_SUFFIX`），先查再改名，不覆蓋。
- 只在「版本落後而且 facts 表已存在」時備份。全新庫不備份，但備份資料夾已有備份檔時記 WARNING 並出提示（防還原時檔名改錯、默默建出空庫）。
- 空間預檢：可用空間 < 記憶庫大小 × `FACTS_BACKUP_SPACE_FACTOR` 就不備份也不升級；查不到可用空間或 total 為 0 當未知，照常備份。
- 做法：持鎖時另開唯讀連線（`absolute().as_uri()` 加 `?mode=ro`），`backup()` 到暫存檔，兩條連線都關掉後才改名成正式檔；任何一步失敗都刪暫存檔並丟例外，不升級。來源不能用持鎖的升級連線（測試證實會卡住）。
- 輪替：這次做出新備份就輪替（升級成不成功都做）。只看頂層、只認備份檔名格式；新檔永遠不列入刪除候選，其他留最新 `FACTS_BACKUP_KEEP - 1` 份（含新檔共 `FACTS_BACKUP_KEEP` 份）；出錯只記警告。

#### 2-4. 失敗狀態與提示

- `FactsSchemaStatus`：CURRENT、NEWER（比程式新：照用、不升級、不備份）、OUTDATED（只給離線工具），以及四個失敗狀態 NO_SPACE、LOCKED、UNREADABLE、FAILED_OTHER。
- 分類依 errno 與 SQLite 固定英文訊息；`unable to open database file` 歸讀不到。快速路徑或複查讀檔頭就失敗時，除了被鎖住與空間不足，一律算讀不到（「其他」的提示保證記憶可以查，不能亂用）；讀不到之後檔頭又讀得到、但版本仍落後時，改算「升級沒完成」。
- `GET /v1/memory` 的 `memory_notices` ＝ 啟動時算的靜態提示（ADR-0253）＋每次請求即時算的結構提示（`memory_schema_notices()`）。
  - 狀態讀自目前的共用實例（`FactualStore.existing_shared()`，不建構；換嵌入模型重置後就是新的那個），還沒建就用啟動時放進 context 的那個。
  - 失敗狀態時複查檔頭：用 `mode=rw` 開（檔案不在就不建），最多等 `FACTS_STATUS_RECHECK_TIMEOUT_S`。
  - 四個失敗各一個 `warning` 級代碼：`memory_upgrade_no_space`、`memory_upgrade_locked`、`memory_store_unreadable`、`memory_upgrade_failed`。建全新空庫、旁邊卻有升級備份時另出 `memory_new_store_beside_backups`（只在建出空庫的那次執行期間）。啟動時建不出事實庫就出 `memory_store_unreadable`，之後建成就消失。原因細節只進日誌、不進 payload。
- 前端 `MemoryNoticeBar` 的代碼對應表加 5 行、兩語翻譯各 5 句。任何一句都不引導人還原升級備份；空間不足、被佔用、升級沒完成三句寫明「記憶庫沒有被改動，不需要還原」；「讀不到」先請人找團隊，團隊同意才自己還原，還原前先按「立即建立備份」。
- **不打開全域 `system_degraded`**：它會讓各家 adapter 在 prompt 裡說記憶系統不可用，但這時查記憶大多能用。改用同樣醒目的警告提示（同一個 destructive 樣式＋`role="alert"`，左側身分列也亮警告標記）。這是維護者核准的偏差，見「和計畫不同的地方」。

#### 2-5. 寫入閘與讀取

- 存記憶要 CURRENT 或 NEWER，否則丟 `FactsSchemaNotReadyError`（固定訊息、帶 `.status`），經記憶閘道的通用分支變成結構化 `internal_error`。
- 寫入改成一般 INSERT：撞到既有 id 丟 `FactIdConflictError`，絕不覆蓋（原本的 REPLACE 是刪掉再插入，會洗掉 v1 欄位，或把列搬到別的 agent 名下）。寫入失敗丟訊息固定的 `FactWriteError`（`FACT_WRITE_FAILED_MESSAGE`），經閘道結構化，SQLite 原文只進日誌。
- `query_facts`／`query_all_facts` 不回空清單：
  - 檔頭讀不到就先丟。
  - 實際讀取遇到「讀不到」類錯誤時，把實例轉成 UNREADABLE、設一個「讀不到過」旗標，再丟。旗標只有之後真的讀取成功一次才清，檔頭複查不能清；在那之前提示一律是讀不到、寫入也擋，查詢照常嘗試（成功的讀取才能清旗標）。
  - 被鎖住等暫時性錯誤照丟，不改狀態、不出提示。
- 單一列的 tags／emotions 不是合法 JSON、或不是字串清單時，只把那一列的欄位當空值（tags `[]`、emotions `None`），記一筆只帶 fact id 的警告，其他列照常回傳；只有資料庫層級的錯誤才讓整個查詢失敗。
- `/v1/memory/list` 讀取失敗回 `SYSTEM_ERROR`（500），不回空清單；記憶系統沒載入時照舊回空。記憶寶庫頁收到非 2xx 時顯示錯誤（訊息經 `getErrorMessage`、外框文字經 i18n），記憶筆數顯示「—」；不是權限不足時同時叫側欄重抓 `/v1/memory`，文字請人照側欄提示做，沒有提示就重新整理，仍失敗找團隊；權限不足照錯誤原文顯示。
- 換嵌入模型的記憶偵測 `has_existing_memories()`：記憶系統沒載入回 False；記憶庫讀不到或查詢失敗回 True（當作有記憶，一定要求確認）。

#### 2-6. 安全判斷不能依賴降版行為

- 換回舊版期間，舊程式新增的列一律拿到預設值（`sources` 是 NULL、`embedding_state` 是 `'unverified'`），舊程式也不做任何新驗證。
- 凡是安全判斷都只能把 `sources` 是 NULL 當「來源未記錄」，不能當成可信。`embedding_model` 在換回舊版期間不會被更新，只當參考，不能用來判斷向量是否有效。

#### 2-7. 之後加欄位的硬約束

- 讀取路徑在失敗狀態（讀不到除外）也要能用。SELECT 要加 v1 欄位時，依結構版本在兩句固定 SQL 之間選一句（段 4 的 `_read_select`）。
- 所有寫 v1 欄位的新路徑（INSERT 或 UPDATE）先過 `_require_schema(write=True)`；INSERT 維持一般 INSERT，不改回 REPLACE。`test_t40` 用 AST 把關：`factual.py` 裡寫新欄位的 INSERT／UPDATE 字串所在的函式，必須自己呼叫 `_require_schema(write=True)`。`test_t39` 在空間不足、被佔用、其他三種失敗狀態下跑 `query_all_facts`、匯出、隨機回想與帶篩選的查詢。
- 新的事實庫讀取路徑照 `_fetch_rows` 的規矩：讀取錯誤不回空清單。

### 段 3：事實庫是正本，語意索引可以手動補

#### 3-1. 向量失敗不刪事實

- 狀態值 SSOT：`memory_server.factual.EmbeddingState`（`unverified`／`ready`／`failed`）。
- `save_memory`：事實先寫進事實庫（一般 INSERT，不指定 `embedding_state`，新列拿到 `'unverified'`），再由 `services/index_state.index_new_fact` 寫向量。成功標 `ready`（`embedding_model` 記 `get_embedding_model_name()`、只當參考）；任何失敗（`save_vector` 回 False、建 collection 例外、取不到向量庫）標 `failed`，事實不刪，回「已存，語意索引稍後補上 (ID: …)」。保留 ASCII 括號的 `(ID: …)`，smoke 用它解析 ID。狀態寫不進去只記警告，事實留在 `unverified`，補索引照樣會處理。
- `delete_fact_permanently` 只剩回滾這個呼叫者，一併刪除（禁令 46）。
- 向量 payload 的 `created_at` 改用事實的 `created_at`（`save_vector` 新增選填 `created_at`，沒給才用現在）。

#### 3-2. 語意結果一律回查事實庫

- `query` 的語意半與存記憶的語意查重經 `index_state.live_semantic_hits` → `FactualStore.get_active_facts`：只留同一身分、未封存、事實庫裡還在的事實；內容與標籤以事實庫的列為準，只保留向量的相似度。id 清單用 `json_each(?)` 帶入一句固定 SQL（禁令 24：結構固定、只綁一個參數）。
- `manage_dups` 的 scan 用掃描開頭那次 `query_facts` 拿到的未封存事實當回查集合，不多讀；候選不在集合裡就不列。
- `forget` 與 `manage_dups` 的 resolve（同一條 `resolve_duplicate`）：事實庫封存成功就回成功；向量庫封存失敗或取不到向量庫只記日誌（以事實庫為準，舊向量因回查不會再被回傳）。

#### 3-3. 查重的事實庫備援

- 存記憶前先查同一身分、未封存、內容完全相同（去頭尾空白）的事實（`FactualStore.find_active_fact_by_content`），有就回重複；向量庫壞了也擋得住。
- 語意查重改取 `DUPLICATE_CANDIDATES_PER_FACT` 個候選、回查後再比 `DUPLICATE_SIMILARITY_THRESHOLD`，避免第一名是已封存事實時漏掉第二名。

#### 3-4. 手動補索引：記憶工具的新 action `reindex`

- 公開合約 `MemoryReindexInput` 只有 `action`（`extra="forbid"`），模型不能指定別人；處理器 `tools/diag.reindex_memories` → `index_state.reindex_facts`。
- 只取呼叫者自己、未封存、`embedding_state` 是 NULL／`unverified`／`failed` 的事實，依 `created_at, id` 舊的先，一次最多 `MAX_REINDEX_FACTS`。
- 先用 `VectorStore.indexed_fact_ids` 一次問向量庫哪些 point 已存在（`fact_point_id` 是固定的 uuid5，不載入模型、不算向量），有的直接標 `ready`；沒有的才重算並 upsert，沿用原本的 `created_at`。問不到時全部重寫（point id 固定，不會重複）。
- 逐筆隔離：一筆寫不進去只把那一筆標 `failed`，其他照做；回報檢查、已在索引、重算、失敗與做完後仍待補（未核對＋失敗）的筆數。
- 讀待補清單與寫狀態都先過 `_require_schema(write=True)`（`set_embedding_state` 自己呼叫，符合 `test_t40`）；v0 或失敗狀態時回結構化錯誤、不動記憶庫。
- 仍是手動、不自動觸發（維持「補索引手動、全量重建離線」，ADR-0118）；在 Proxy 同行程跑，沒有 Qdrant 本機鎖問題。
- **風險分類**：`auth_policies.json.example` 與被追蹤的 `ToukaProxy/config/auth_policies.json` 的 `memory_tools.reindex` 都是 `safe`。後者經維護者 2026-10-15 核准，只加這一行。沒列在設定檔裡時走 `ToolRiskConfig.class_for` 的 fail-safe（動詞未知一律 dangerous：Manual 要人核准、Auto 交審查員），測試釘住這個 fail-safe 不被放寬。

#### 3-5. 診斷只列各狀態筆數

- `diagnostics` 加一行未封存事實的各狀態筆數（`FactualStore.count_embedding_states`），有待補時提示用 `reindex`；不抽查向量庫。
- 依結構狀態在兩句固定 SQL 之間選：CURRENT／NEWER 用 `GROUP BY embedding_state`；其他（升級失敗、仍是 v0）全部算 `unverified`。讀不到時顯示「無法取得」，診斷其他部分照常。讀取經 `_fetch_rows`（從 `_fetch_facts` 抽出，同樣「讀取錯誤不回空清單」）。

#### 3-6. 離線重建不回寫狀態

- `reembed_agent` 仍只讀事實庫；重建後 `embedding_state` 不變，下一次 `reindex` 先問向量庫就會標回 `ready`。腳本結尾印一行提示。

### 段 4：記得每段記憶從哪來

#### 4-1. 來源的形狀

- SSOT：`memory_server/models/source_ref.py`。`SourceRef` 是 Pydantic、`extra="forbid"`、frozen；欄位 `kind`（`conversation`／`heartbeat`，第一批只做這兩類）、`session_id`、`message_id`（觸發的已保存 user 節點）、`request_id`、`captured_at`（秒，和 `facts.created_at` 一致）、`content_sha256`（pattern `^[0-9a-f]{64}$`）。只記位置與指紋，不存原文或摘錄。
- `sources` 欄存 JSON 清單（目前一筆，保留之後多來源的空間）。讀出來經 `load_sources` 驗過、轉成普通 dict 清單，GDPR 匯出可直接序列化；格式壞掉當空清單，只記 fact id。
- 指紋算法 `content_fingerprint` 放在同一模組，記下與查回共用。

#### 4-2. 在記憶閘道組來源

- `tool_executor.py`、`execution_context.py` 零修改。
- `PipelineContext.source_message_id` 只有兩處寫：`routers/proxy._authorize_ui_persistence` 在明示 UI 持久模式、owner 與 revision 驗過後寫 `user_parent_id`（無痕不寫）；`routers/heartbeat._execute_single_pulse` 每次脈衝寫觸發的 user 節點（線性路徑最後一則是 user 才算）。
- 閘道（`src/tools/builtin/memory.py`）存記憶時用執行器注入的 `session_id`、`request_id` 與 `_ctx` 交給 `MemorySourceResolver.capture`；`ctx.heartbeat_run` 決定 `kind`。
- 指紋在存記憶當下讀「已保存」的節點算（`session_service.read_persistence_snapshot`，strict 讀取）。讀不到、或那段對話不屬於這個身分（`authorization_gateway.can_access_session`，和路由、dispatcher 同一個判斷），就不記來源、記憶照存。前提「UI 持久模式下 user 節點在模型呼叫前已保存」已用測試確認：開始生成前 dispatcher 會確認 user 節點已在該 revision 的已保存樹裡，沒保存就不生成。

#### 4-3. 模型不能提供或改寫

- `source_context`（存）與 `source_resolver`（查）是可信參數，不在公開合約裡；模型帶了，`extra="forbid"` 的驗證器直接拒絕，不回顯值。
- 閘道再把外面帶進來的這兩個參數剝掉，只放自己組的（防其他呼叫路徑繞過驗證器）。`test_native_contract_invariants` 的保留欄位加入這兩個名稱，公開 schema 不得廣告它們。
- 參數不叫 `source_id: str`（IDENTITY_SSOT）。

#### 4-4. 不記來源的情況（`sources` 是 NULL）

- 無痕（本來就不提供記憶工具；就算被叫到也不記，而且連那段對話都不查，零殘留，ADR-0086）、子代理（`subagent_run`／`dispatch_run`）、沒有明示 UI 持久模式的請求（一般 API、IDE）、離線工具、升級前的舊事實、換回舊版程式期間存的、結構比程式新時存的。
- NULL 只能說「來源未記錄」，不推論可信或不可信；任何安全判斷都不依賴它。

#### 4-5. 查回

- 檢索帶來源用 `query` 的新公開欄位 `include_sources`（預設 false），沿用 `query` 的 read_only，不新增 action；權限設定檔不用改。
- 查回器 `MemorySourceResolver` 由閘道持有單例（不在 `main.py` 注入），實作 `memory_server.protocols.ISourceResolver`；只有 `include_sources` 為真時才交給 `query_memory`。
- 先查對話樹，再查舊對話庫（`LegacyStore.get_message`，新增；資料庫不存在回 None、不建檔），舊對話庫裡的訊息要 session 對得上才算。只回狀態：`available`（原文仍在）／`changed`（原文已修改）／`missing`（原文已不在），不回原文、不寫回事實庫；原文不在也不刪記憶。
- 顯示（`describe_sources`）：每筆附一行「來源：對話／心跳 <session>／訊息 <message>：狀態」。查回丟錯、沒有查回器或這一欄格式壞掉時顯示「無法確認」，不讓整個查詢失敗；NULL 顯示「來源未記錄」；結構不是目前版、讀不到這一欄時顯示「來源暫時無法讀取」，和「未記錄」分開。

#### 4-6. 讀寫遵守段 2 的硬約束

- `query_facts`、`query_all_facts`、`get_active_facts`、`find_active_fact_by_content` 依結構狀態在兩句固定欄位前綴之間選（`_read_select`；讀不到時擋下，同原本的查詢閘），讀取仍經 `_fetch_rows`。
- 只有 CURRENT 讀寫 `sources`：失敗狀態（仍是 v0）與 NEWER 都用舊版欄位。NEWER 用舊版欄位，是因為既有測試以「v0 欄位＋版本 99」代表較新版、要求照常讀寫；這版程式對較新的結構不做任何欄位假設，等同降版期間的行為。
- `_insert_record` 自己先過 `_require_schema(write=True)`，一般 INSERT。有來源而且是 CURRENT 時用含 `sources` 的那句，否則用舊版欄位那句（欄位預設 NULL）。`save_fact` 照樣在寫入失敗時丟 `FactWriteError`，結構不就緒的例外照原樣往外丟。

### Guard #45 基準

- 基準檔只改本 PR 造成的形狀變化，逐項同步，沒有整份重拍：段 3 的 `reindex`（合約清單加 `MemoryReindexInput`、`reindex_memories` 條目、diag 的匯入），段 4 在 `MemoryManager.save_memory`／`query_memory` 的 trusted 各加一行（`source_context`、`source_resolver`），以及這個 PR 改到的 3 個 typed consumer 條目（`MemoryManager.save_memory` 與 `MemoryManager.query_memory` 的參數、Proxy 記憶閘道 `memory_tools` 的讀取）。
- 基準檔裡其他和程式不一致的快照不在本 PR 範圍（見 HANDOVER §3）。

## 取捨

- **只在啟動時升級，不在執行中升級**：Proxy 啟動 yield 前不接請求、背景工作也還沒開始，不需要跨 OS 的檔案鎖；執行中只有換嵌入模型這一條路會再試一次。代價是啟動多一次和記憶庫大小成正比的備份。
- **段 2～4 的欄位放進同一個 v1**：一次備份、一次交易，Owner 那邊只會升級一次；代價是 v1 在合併前要先把段 3、4 的欄位定好。
- **不打開 `system_degraded`**：理由見 2-4。
- **補索引做成記憶工具的 action，不做管理端點加設定頁按鈕**：計畫寫「可在診斷裡手動補」，診斷本來就是記憶工具的 action；仍是手動、不是自動。
- **查回來源用 `query` 的新欄位，不新增 action**：沿用 `query` 的 read_only，權限設定檔不用改。
- **查回器由閘道持有，不在 `main.py` 注入**：避開和其他工作線重疊的啟動熱點。
- **來源只記位置與指紋，不存原文**：不多一份對話內容；原文還在不在、有沒有改，讀時用指紋比對就知道。

## 和計畫不同的地方

維護者核准的偏差：

- **備份資料夾**是 vault 裡的 `core_memory_backups\`，不是計畫寫的 `vault\backups\`：避免和系統快照的 `backups` 資料夾搞混。
- **備份份數**：新備份不覆蓋舊的，但最多留 3 份（`FACTS_BACKUP_KEEP`），更舊的自動刪掉。
- **升級失敗的顯示**：不打開全域降級（`system_degraded`），改用同樣醒目的警告提示（見 2-4）。

其他依實作調整：

- 計畫寫「舊資料升到 v2」，實際是 v1；舊事實升級後本來就是 `'unverified'`，不另寫回填。
- 診斷只列各狀態筆數，不抽查向量庫。
- `sources` 是 NULL 只說「來源未記錄」，不說「上線前建立」；結構不是目前版時另外顯示「來源暫時無法讀取」。
- 不做：不加 `embedding_state` 索引、不用 `embedding_model` 判斷待補、不做「換模型標過期」；不做 tool_result、檔案等其他來源種類，不持久化 `tool_call_id`；記憶寶庫畫面不顯示來源（`/v1/memory/list` 的 DTO 不帶 `sources`）；不回填舊事實的來源。

## 影響

- **第一次啟動**：更新後 Proxy 第一次啟動會在真實記憶庫上升級一次（先備份、單一交易），之後重啟不再升級。全新庫或已是 v1 時只有幾毫秒。
- `GET /v1/memory` 的 `memory_notices` 多 5 個 `warning` 代碼；`GET /v1/memory/list` 讀取失敗由 200＋空清單改成 500 `SYSTEM_ERROR`。兩個端點沒有 `response_model`，openapi 不變。
- 記憶工具公開合約：新 action `reindex`、`query` 新欄位 `include_sources`；可信參數 `source_context`、`source_resolver` 不廣告。存記憶在向量寫不進去時改回「已存，語意索引稍後補上 (ID: …)」，不再回滾。
- 權限：`.example` 與被追蹤的 `ToukaProxy/config/auth_policies.json` 各加一行 `reindex: safe`（後者經維護者核准）。
- 重置畫面：保留清單多「記憶庫升級備份（含所有靈魂）」，完成訊息與 DebugAgent 重置說明寫明系統備份與升級備份仍保有舊資料（`RETAINED_STORES` 加 `FACTS_BACKUP_DIR_NAME`）。
- NOQA_REGISTRY：`S603` 30 → 31（`tests/unit/test_facts_schema_migration.py` 的當機子程序）。
- AGENTS.md、SECURITY.md 不改。

## 已知限制

段 2：

- 啟動多一次和記憶庫大小成正比的備份（同步、擋在 yield 前）。真實記憶庫大小對啟動時間與 healthcheck（`start_period: 20s`）的影響沒有實測【推測：一般記憶庫幾 MB，影響可忽略】。
- 只有被鎖住這類暫時性錯誤不出提示，但查詢照樣回結構化錯誤，不會顯示成沒有記憶。
- 啟動時讀檔頭的 I/O 錯誤（例如 `disk I/O error`）歸「讀不到」，不歸「其他」：寧可說查不到，也不保證記憶可以查。
- 檔頭完好、資料頁損壞（malformed）時：查詢失敗會設「讀不到過」旗標，提示一直是讀不到、寫入也擋，直到真的讀取成功一次；修好檔案後要有人查一次記憶（或重啟），提示才消失。
- 前端不輪詢：側欄在頁面載入、連線或登入變化時重抓 `/v1/memory`，記憶寶庫列表讀取失敗也會叫它重抓；除此之外，後端狀態改變要重新整理頁面才看得到。
- `memory_new_store_beside_backups` 只在建出空庫的那次執行期間出現；重啟後庫已是 v1，不再判斷。
- 升級備份是整個資料庫檔的複本，可能含刪除後還留在空閒頁的殘片（主庫本來也有，同等保護）。備份含所有靈魂的事實，和記憶庫同位置、同保護；GDPR 匯出逐 agent，不會帶到；vault URI 只解析 personal／legacy／soul，碰不到備份資料夾。
- 逐個靈魂清除或重置記憶時，升級備份與系統快照不會一起清（重置畫面已誠實列出）；系統快照也會把升級備份包進去。要不要做成清除時一併處理，等維護者決定。
- GDPR 匯出遇到單一列的 tags／emotions 格式壞掉時，那個欄位匯出成空值（其他欄位照常），日誌只記 fact id。
- Docker Desktop（Windows 主機）bind mount 上：容器內 `shutil.disk_usage` 回的是哪個磁碟、改名或刪除開著的檔、同行程兩條連線對同一檔的鎖，都沒有實測【推測】。Linux 主機還原後檔案擁有者（uid 1001）不對時會落在「讀不到」。
- 驗證過的降版範圍：`8b2e242e` 起（測試用該版的 SQL 字面量證明舊程式能讀寫升級後的庫）。更舊的版本 facts 表本身沒問題，但 2026-05-05 以前的版本會自動匯入還沒搬的 `facts.jsonl`；其他資料是否相容沒有查證。
- 換回舊版期間可能出現重複記憶【推測】：向量寫入失敗時事實會留著（`failed`），舊程式的重複檢查只查向量，看不到這些事實，模型可能再存一次同樣內容。

段 3：

- 取不到向量庫時，`query` 仍回結構化錯誤，沒有退成只回事實庫的結果；要不要改留給之後決定。〔ADR-0263 已修：退成只回事實庫的有效結果，並在界線外註明。〕
- 存記憶在成功路徑多一次 UPDATE（標 `ready`）與一次事實庫查重 SELECT；語意查詢多一次回查 SELECT（只有語意半有結果時）。
- `json_each` 需要 SQLite 內建 JSON（3.38 起預設內建）。本機 Python 3.12 的 SQLite 是 3.53.1【推測：Debian trixie 映像的 libsqlite3 3.46 也內建，沒有在容器實測】。
- `reindex` 在事件迴圈裡同步執行（和既有 `manage_dups` scan 一樣）；最多 `MAX_REINDEX_FACTS` 筆，每筆要重算時會占用 CPU【推測：e5-base 單筆數十毫秒，最壞數秒】。
- 狀態值來自較新版程式（NEWER）而且不認得時，診斷照實列出，但 `reindex` 不處理。
- 向量庫裡已封存事實的 payload 可能仍是未封存（封存時向量庫失敗）；回查擋住它們，`reindex` 不修 payload（只處理未封存事實）。〔ADR-0263 已修：封存時向量沒同步就標 `failed`，`reindex` 也處理已封存的事實、把 payload 設成已封存。〕

段 4：

- 對話交接給別的靈魂（session 的 agent 換了）後，原靈魂的記憶查回是「原文已不在」。
- 群聊或其他不屬於這個靈魂的 session 存的記憶不記來源（`can_access_session` 不通過）。
- 被滑動視窗剪掉、但還沒同步進舊對話庫的訊息（還在 JSON 檔），查回是「原文已不在」。90 天清除非家對話的已知問題（ADR-0253）若修好，這類記憶也會變成「原文已不在」，事實照樣留著。
- 較新的結構（NEWER）不讀寫 `sources`：這段期間存的記憶不記來源，查詢顯示「來源暫時無法讀取」。
- 存記憶多一次對話讀取（strict、持該 session 的鎖、只讀）；帶來源查詢每筆多一次對話讀取，對話不在時再讀一次舊對話庫【推測：`limit` 預設 5，影響很小】。
- `sources` 欄可被持有資料庫的人改；查回只核對指紋與歸屬，不防資料庫被直接竄改。

## 決策落點表

| 決策 | 檔案 | 位置 | 測試／守衛 |
|---|---|---|---|
| 2-1 版本、只增、凍結 | `ToukaMemory/src/memory_server/factual.py` | `FACTS_SCHEMA_STEPS`、`_SchemaStep`、`_BASELINE_SQL`、`_target_version`、`_apply_steps` | `test_facts_schema_migration.py`（`test_t16`、`test_t17`） |
| 2-2 只在啟動時升級 | `factual.py`；`ToukaProxy/src/main.py`；`services/manager.py`；`services/reembed.py`、`scripts/reembed_memory.py` | `FactualStore.shared`／`existing_shared`／`reset_shared`、`_init_db`、`_upgrade_locked`；`_prepare_memory_store`；`get_factual_store`、`reset_singletons`；`open_factual_store_for_reembed` | `test_facts_schema_migration.py`、`test_facts_schema_startup.py` |
| 2-3 升級備份 | `factual.py`；`ToukaMemory/src/memory_server/protocols.py` | `_prepare_upgrade`、`_backup`、`_check_backup_space`、`_free_backup_name`、`_rotate_backups`、`FACTS_BACKUP_*`；`IResourceProxy.move_file`／`iter_files` | `test_facts_schema_migration.py` |
| 2-4 狀態與提示（後端） | `factual.py`；`ToukaProxy/src/routers/observability.py` | `FactsSchemaStatus`、`_classify_failure`、`_classify_read_failure`、`_rechecked_status`、`current_schema_status`；`MEMORY_NOTICE_*`、`_SCHEMA_STATUS_NOTICES`、`memory_schema_notices`、`get_memory_stats` | `test_facts_schema_migration.py`（`test_t27`、`test_t31`～`test_t34`、`test_t38`）、`test_facts_schema_startup.py` |
| 2-4 狀態與提示（前端） | `ToukaChat/src/components/sidebar/MemoryNoticeBar.tsx`、兩語 `translation.json` | `NOTICE_MESSAGE_KEYS` | `MemoryNoticeBar.schema.test.tsx` |
| 2-5 寫入閘與讀取 | `factual.py`；`routers/observability.py`；`ToukaChat/src/components/dashboard/VaultDashboard.tsx`；`ToukaProxy/src/services/embedding_service.py` | `_require_schema`、`_insert_record`、`save_fact`、`_fetch_rows`、`_row_to_fact`、`FactIdConflictError`、`FactWriteError`；`get_memory_list`；`failList`；`has_existing_memories` | `test_facts_schema_migration.py`（`test_t18`、`test_t19`、`test_t28`～`test_t30`、`test_t39`、`test_t40`）、`test_facts_schema_startup.py`、`VaultDashboard.listError.test.tsx` |
| 2 重置畫面列出升級備份 | `ToukaProxy/src/services/agent_reset/primitives.py`、兩語翻譯 | `RETAINED_STORES` | `test_facts_schema_migration.py`（`test_t26`） |
| 3-1 向量失敗不刪事實 | `ToukaMemory/src/memory_server/services/index_state.py`；`services/manager.py`；`vector.py` | `index_new_fact`；`save_memory`；`save_vector(created_at=…)`、`fact_point_id` | `test_memory_index_state.py` |
| 3-2 語意結果回查 | `index_state.py`；`factual.py`；`manager.py` | `live_semantic_hits`；`get_active_facts`；`query_memory`、`find_duplicates`、`resolve_duplicate` | `test_memory_index_state.py`、`test_memory_tool_contracts.py` |
| 3-3 查重備援 | `factual.py`；`manager.py` | `find_active_fact_by_content`；`save_memory` | `test_memory_index_state.py` |
| 3-4 `reindex` | `tools/contracts.py`、`tools/diag.py`、`tools/__init__.py`、`index_state.py`、`factual.py`、`vector.py`；`ToukaProxy/config/auth_policies.json.example`、`ToukaProxy/config/auth_policies.json` | `MemoryReindexInput`、`reindex_memories`、`MEMORY_ACTIONS`、`reindex_facts`、`MAX_REINDEX_FACTS`、`list_pending_index`、`set_embedding_state`、`indexed_fact_ids`；`memory_tools.reindex` | `test_memory_index_state.py`、`test_memory_p0_repairs.py`、`test_native_tool_parameter_flow.py` |
| 3-5 診斷 | `tools/diag.py`、`factual.py` | `memory_diagnostics`、`count_embedding_states` | `test_memory_index_state.py` |
| 3-6 離線重建不回寫 | `services/reembed.py`、`scripts/reembed_memory.py` | `reembed_agent` | `test_memory_index_state.py` |
| 4-1 來源的形狀 | `ToukaMemory/src/memory_server/models/source_ref.py` | `SourceRef`、`content_fingerprint`、`load_sources`、`describe_sources`、`SOURCE_*` | `test_memory_sources.py` |
| 4-2 在閘道組來源 | `ToukaProxy/src/tools/builtin/memory.py`、`services/memory_source_resolver.py`、`models/context.py`、`routers/proxy.py`、`routers/heartbeat.py` | `_SOURCE_RESOLVER`、`MemorySourceResolver.capture`、`PipelineContext.source_message_id`、`_authorize_ui_persistence`、`_execute_single_pulse` | `test_memory_sources.py` |
| 4-3 模型不能提供 | `tools/builtin/memory.py` | `_TRUSTED_SOURCE_KWARGS`、`_NON_MEMORY_KWARGS` | `test_memory_sources.py`、`test_native_contract_invariants.py`（`RESERVED_FIELDS`） |
| 4-5 查回 | `memory_source_resolver.py`、`protocols.py`、`services/legacy_store.py`、`manager.py` | `MemorySourceResolver.resolve`、`ISourceResolver`、`LegacyStore.get_message`、`query_memory(include_sources=…)` | `test_memory_sources.py` |
| 4-6 讀寫依結構版本 | `factual.py` | `_read_select`、`_FACT_SELECT_V0`／`_FACT_SELECT_V1`、`_insert_record` | `test_memory_sources.py` |
| Guard #45 基準 | `scripts/native_tool_contract_baseline.json` | `reindex` 相關條目、兩個 trusted、3 個 typed consumer | Guard #45 |

## 驗證

本機 Windows、3.12 venv、fnm Node 24.21.0。自動測試都在 conftest 沙盒或 pytest `tmp_path` 裡：舊庫用 `8b2e242e` 的 SQL 字面量合成，對話與舊對話 DB 也是合成的，向量庫用 autospec 替身或 tmp 本機 Qdrant 加固定的替身編碼器。不讀 `data/`、不載入嵌入模型、不打付費 API，也沒有啟動 Touka。

- 新測試（`pytest --collect-only` 與 `vitest list` 實數）：pytest 149 個——段 2 `test_facts_schema_migration.py` 84、`test_facts_schema_startup.py` 13，段 3 `test_memory_index_state.py` 22，段 4 `test_memory_sources.py` 30；既有檔 `test_native_tool_parameter_flow.py` 加 1 個（36 → 37），`test_memory_p0_repairs.py`、`test_native_contract_invariants.py`、`test_memory_tool_contracts.py` 個數不變。Vitest 12 個（`MemoryNoticeBar.schema.test.tsx` 10、`VaultDashboard.listError.test.tsx` 2）。
- 新行為的測試都先紅後綠；沒有紅燈階段的（SQLite 平台特性、正常時沒有提示、既有行為的補強）各由突變把關。
- 突變：段 2 93 個（pytest 88、Vitest 5）、段 3 38 個、段 4 39 個，每個只改一處，pytest 或 Vitest exit 1 才算紅；全部變紅，還原後回綠。
- 被測試證實的推測（段 2）：唯讀連線遇到當機殘留的 journal 會報 `attempt to write a readonly database`（Windows 內建 SQLite）；用持鎖的升級連線當備份來源會卡住；`as_uri()` 編碼後含 `#`、`%` 的路徑能備份，不編碼就失敗；`user_version` 跟著交易回滾；子程序能逼出熱 journal（Windows）。Linux CI 上的結果以 CI 為準。
- 文件與計數檔：`guard_noqa_registry.py`（`S603` 31）、`guard_adr_integrity.py`（0001..0256 連續）、`lint_ssot.py` 通過；文件契約、Guard #19／#28、API schema 同步與來源掃描邊界測試 82 passed。
- 全量（`03c7c4ac`）：pytest 固定順序 9218 passed／7 skipped／6 failed（6 個是本機 Docker Desktop 的 `test_local_compose_bindings`，以 CI 為準）；guard_all 43／43 PASS；ToukaChat Vitest 全量 1669 passed（`341d356f`，之後只改文件）。

## 回復

- 不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。
- 段 3、段 4 都建在段 2 的 v1 欄位與共用事實庫上：撤段 2 的程式要連段 3、4 一起撤。被追蹤的 `auth_policies.json` 那一行可以單獨撤（撤了 `reindex` 回到 fail-safe 的 dangerous）。
- 撤回程式不會把記憶庫降回 v0：v1 欄位與 `user_version` 留著。舊程式讀寫升級後的庫沒有問題（測試用 `8b2e242e` 的 SQL 字面量證明），它新增的列拿到欄位預設值。
- 升級後要人做的回復（換回舊版、還原升級備份）見附錄。有資料時，每次更新前先照附錄做「更新前先做兩件事」。
- 開發代理不碰 `data/`：`core_memory_backups` 裡的升級備份由使用者手動清。

## 附錄：回復步驟全文（給部署者）

> 有資料時，每次更新都要先做下面的「更新前先做兩件事」。

> 寫給不寫程式的人。指令都在「Touka 資料夾」（有 `docker-compose.yml` 或 `docker-compose.ghcr.yml` 的那個資料夾）裡，用 PowerShell 執行。不確定是哪一種部署方式時，看資料夾裡用的是哪一個 compose 檔。

**更新前先做兩件事（每次更新都做）**

1. 記下目前版本：
   - 用 GHCR 映像（`docker-compose.ghcr.yml`）：打開 `.env`，找 `TOUKA_VERSION=` 那一行，抄下後面的值。沒有這一行，或值是 `latest`，就執行 `docker inspect touka-proxy --format "{{json .Config.Labels}}"`，在輸出裡找 `org.opencontainers.image.revision`，抄下後面那串 40 個字元。你的版本就是 `sha-` 加上那串字。
   - 自己從原始碼建置（`docker-compose.yml`）：執行 `git log -1 --format=%H`，抄下輸出的那串字。
2. 建一份系統快照：Touka 設定 →「系統備份／還原」→ 按「立即建立備份」，等它顯示成功。自動備份預設是關的，要手動按。

**情況一：更新後畫面出現紅色提示列，一直不消失**

提示在右側欄；重新啟動 Touka 之後要重新整理頁面，提示才會更新。不要還原任何備份。照提示的內容做：

- 「需要更多磁碟空間」：清出磁碟空間，重新啟動 Touka。記憶庫沒有被改動。
- 「正被其他程式使用」：關掉其他開著記憶庫的程式（例如資料庫檢視軟體、重建索引的工具），重新啟動 Touka。記憶庫沒有被改動。
- 「讀不到記憶庫」：暫時查不到也存不了記憶。請先找維護者。若維護者請你自己還原：先按「立即建立備份」存下現在的狀態，再選最近一份正常時的快照（對話和設定也會一起回到那時）。
- 「升級沒有完成」：重新啟動 Touka 會再試一次。一直失敗就照「情況二」換回舊版，並告訴維護者。記憶庫沒有被改動。
- 「需要更多磁碟空間」或「正被其他程式使用」照做之後還是一直出現：照「情況二」換回舊版，並告訴維護者。
- 「記憶庫是新的，但旁邊有升級前的備份」：先不要存新記憶，找維護者（你可能剛還原過或改過資料路徑）。

重新啟動 Touka：

- GHCR：`docker compose -f docker-compose.ghcr.yml restart touka-proxy`
- 原始碼建置：`docker compose restart touka-proxy`

不管哪一種，都把下面指令的輸出交給維護者：

- GHCR：`docker compose -f docker-compose.ghcr.yml logs touka-proxy | Select-String Factual`
- 原始碼建置：`docker compose logs touka-proxy | Select-String Factual`

**情況二：更新後 Touka 不正常（打不開、記憶看起來不對）→ 第一步一律換回舊版**

換回舊版不動任何資料，不會少任何記憶。「舊版」就是這次更新前你在用的版本，也就是更新前抄下來的那個。

- GHCR：
  1. 打開 `.env`，把 `TOUKA_VERSION=` 改成抄下來的版本（沒有這一行就加一行），存檔。
  2. 執行 `docker compose -f docker-compose.ghcr.yml up -d touka-proxy`。
  3. 執行 `docker compose -f docker-compose.ghcr.yml ps -a`，等 touka-proxy 顯示 healthy，再執行 `docker compose -f docker-compose.ghcr.yml up -d --no-deps touka-heartbeat touka-chat`。
- 原始碼建置：
  1. 執行 `git checkout 抄下來的那串字`。它會印出一大段「detached HEAD」的英文說明，那是正常的；如果出現 `error:` 開頭的訊息就停下來找維護者。
  2. 執行 `docker compose up -d --build touka-proxy`。
  3. 等它 healthy 後執行 `docker compose up -d --build --no-deps touka-heartbeat touka-chat`。
- 可以放心換回的版本：
  - 維護者用測試驗證過 `8b2e242e` 之後的版本，都能直接讀寫升級後的記憶庫。
  - 更舊的版本：記憶庫本身沒問題；但 2026-05-05 以前的版本會自動匯入還沒搬的舊記憶檔，其他資料是否相容沒有查證。請維護者判斷。
- 換回舊版後一切正常：就停在這裡，告訴維護者。什麼時候再更新由維護者決定。
- 換回舊版後還是不對（記憶寶庫是空的、記憶內容不對、打不開）：才做情況三。

**情況三：還原升級備份（只在維護者確認後才做；而且只有在舊版下記憶仍然不對時）**

先確認：備份之後你沒有做過「代理人資料重置」、DebugAgent 重置或刪除靈魂。做過的話先找維護者，因為還原會把那些已清掉的記憶帶回來。

1. 停止 Touka：
   - GHCR：`docker compose -f docker-compose.ghcr.yml stop`
   - 原始碼建置：`docker compose stop`
2. 找到記憶庫資料夾：Touka 資料夾裡的 `data`，再進 `vault`。如果 `.env` 裡的 `SANCTUARY_DATA_PATH` 改過，就是那個資料夾裡的 `vault`。
3. Windows 檔案總管先打開副檔名顯示（Windows 10：上方「檢視」分頁，勾選「副檔名」；Windows 11：「檢視」→「顯示」→「副檔名」），不然看不出 `.db` 和 `-journal`。
4. 打開 `vault` 裡的 `core_memory_backups`，確認裡面有 `core_memory.v0-to-v1.一串數字.db` 這樣的檔。**沒有就停下來找維護者，不要動記憶庫。**
5. 在 `vault` 裡建一個新資料夾，叫 `before-restore`（已經有就叫 `before-restore-2`）。
6. 把 `vault` 裡的 `core_memory.db` 搬進 `before-restore`。旁邊如果有 `core_memory.db-journal`、`core_memory.db-wal`、`core_memory.db-shm`，也一起搬進去。**名字都不要改**：SQLite 只認原來的名字配對。不要動 `_qdrant_db` 和 `core_memory_backups`。
7. 回到 `core_memory_backups`，選修改時間最新的那個 `core_memory.v0-to-v1.數字.db`，**複製**（不是移動）到 `vault`，再把複製出來的檔改名成 `core_memory.db`。
8. 用**舊版**啟動（照情況二的指令）。不要用新版啟動，新版會再升級一次。
9. 打開 Touka 的記憶寶庫，確認看得到記憶。看不到的話：停止 Touka（照第 1 步），不要存任何新記憶，找維護者。
10. 什麼時候再更新到新版，交給維護者決定。

- **還原後會失去什麼**：備份時間之後新存的記憶（所有靈魂都會，因為是同一個檔）。這些記憶還在 `before-restore` 裡的 `core_memory.db`；之後要搬回來時，由維護者提供步驟或工具，由你執行。
- **還原後會回來什麼**：備份之後被封存、被「代理人資料重置」清掉、或刪除靈魂前清掉的記憶，都會回來。
- 語意搜尋可能找到已經不在記憶庫裡的記憶；要不要在停機時重建索引，請維護者判斷。
- Linux 主機：還原的檔要讓容器（使用者 1001）能寫。用 `up -d` 啟動時會先跑 `touka-init` 調整權限；原始碼建置版會調整整個 `data`，GHCR 版是否涵蓋 vault 裡的檔要維護者確認。
- Touka 執行中不要用別的程式打開記憶庫檔案。

**確認升級成功**

- 執行情況一的日誌指令，看得到 `schema upgraded v0->v1` 那一行。
- `core_memory_backups` 多了一個檔。
- 畫面沒有紅色提示列。

**升級沒問題後，怎麼刪掉升級備份**

新版用了一陣子都正常（記憶都在、能存能查），而且維護者同意之後：刪掉 `vault` 裡 `core_memory_backups` 資料夾裡的檔，或整個資料夾刪掉。不用停 Touka。注意：之後建的系統快照 zip 也包著升級備份，要清乾淨就連舊快照一起刪。系統快照放在工作區根目錄底下的 `backups` 資料夾，實際位置受 `.env` 的 `WORKSPACE_ROOT` 影響；找不到就問維護者。
