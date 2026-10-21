# ADR-0253：記憶系統第一批 PR-1：退役舊事實檔自動匯入、記憶提示管道、舊對話同步照實回報、恢復重複掃描、Guard #45 讀內部 action 正本

- 狀態：✅ 已接受（Owner 2026-10-15 核准記憶系統第一批計畫；本 ADR 是第一個 PR（段 1））
- 日期：2026-10-15
- 決策者：Owner
- 影響範圍：ToukaMemory（`factual.py`、`services/legacy_store.py`、`services/legacy_sync.py`、`services/manager.py`、`tools/recall.py`、`tools/diag.py`、`tools/__init__.py`）；ToukaProxy（記憶閘道 `tools/builtin/memory.py`、`routers/stats.py`、`routers/observability.py`、`main.py` lifespan、`services/gdpr_export_service.py`）；ToukaChat（`SystemStatusContext`、`MemoryNoticeBar`、`SidebarIdentity`／`LeftSidebar`、`useLegacySync`、兩語翻譯）；Guard #45（`scripts/guard_native_tool_contracts.py`）
- 相關：ADR-0078（失效可觀察性）、ADR-0116（整合模式單一部署）、ADR-0228（原生公開合約；本 ADR 維持其決策 5 的 `manage_dups` 合約）。這些 ADR 的決策都不變。

## 背景

記憶系統有幾處壞了卻顯示正常，都被寬泛的 `except`、沒有 spec 的 `MagicMock` 或 noqa 蓋住：

- 隨機回想：事實那一半傳字串給要身分物件的方法，舊對話那一半呼叫已改名的私有方法；兩半的錯誤都被吞成空清單，永遠回「腦海中空蕩蕩」。
- 舊對話同步呼叫已不存在的 `_get_legacy_dir`（兩個 `# noqa: SLF001` 壓掉了提示）；設定頁的統計與同步出錯時仍回 200，前端又只看 `res.ok`，畫面顯示「同步成功」。
- 記憶診斷的向量數傳字串給要身分物件的方法，永遠顯示 Check Failed。
- `manage_dups` 預設的 scan 呼叫 2026-04-22 已刪除的 `find_duplicates`。
- 舊版 `facts.jsonl` 自動匯入自 2026-05-05 起每次都失敗並被吞掉，每個行程啟動都重試一次。

## 決策

### 1. 退役 `facts.jsonl` 自動匯入，改成只看檔名的提示

- 拿掉 `FactualStore._migrate_from_jsonl` 與建構時的觸發。舊檔原地保留，不讀、不改、不改名。
- 舊程式遷移成功時會把檔案改名成 `facts.jsonl.bak`；所以還叫 `facts.jsonl` 的檔案，代表裡面的記憶從沒搬進記憶庫。
- `memory_server.factual.find_unmigrated_jsonl_agents()` 只用資源代理的 `glob_search` 列檔名（`<vault>/<agent>/fact/facts.jsonl`，深度固定），不開檔。
- 正式匯入列在第二批（可由 Proxy 端用身分服務建出身分物件後匯入）。

### 2. 記憶提示管道（只給畫面看，不影響模型）

- 不用既有的 `system_degraded`：它是全域啟動旗標，四家 adapter 會因它在 prompt 加「記憶系統不可用」，派送也會走降級；舊檔沒搬入時記憶系統其實能用。
- `GET /v1/memory` 新增 `memory_notices`：每一項是 `{"code", "level", 選填欄位…}`，只放代碼、不放自由文字；既有三個欄位不變。
  - `level` 只有 `info`、`warning`。`info` 平靜顯示（不是紅色、`role="status"`）；`warning` 和降級橫幅一樣醒目（同色、`role="alert"`）。
  - 第一個代碼是 `unmigrated_legacy_facts`（`info`，帶 `count`）。之後的「記憶庫升級失敗」用 `warning` 加新代碼。
  - 代碼與等級的正本是 `ToukaProxy/src/routers/observability.py` 的 `MEMORY_NOTICE_*` 常數；加新提示就是加一個新代碼常數、在 `collect_memory_notices()` 產生它，前端在 `MemoryNoticeBar` 的對照表加一行並補兩語翻譯，不改既有欄位。
- 計算時機：Proxy lifespan 建好 `app.state.context` 後呼叫 `collect_memory_notices()` 一次（理由見「取捨」）；有提示時記一次 warning log。記憶診斷報告也列出該靈魂的舊檔。
- 前端：`SystemStatusContext` 把這個欄位當選填；不是陣列或項目格式不對就略過，不改變既有狀態判斷（hybrid／standalone／degraded）；看不懂的等級當資訊級顯示。`MemoryNoticeBar` 依代碼找翻譯 key，看不懂的代碼不顯示；放在右側欄降級橫幅下方。
- 右側欄收起時提示列看不到，所以有警告級提示時，左側身分列（`SidebarIdentity`）多一個警告標記（`aria-label`＋tooltip，經翻譯）。判斷只在 `ToukaChat/src/lib/memoryNotices.ts` 的 `hasMemoryWarning()` 一處；只有資訊級時不標記。這不改 `systemDegraded`／`checkState`，也不碰送給模型的內容。

### 3. 舊對話同步照實回報

- `LegacySyncService` 改用公開的 `MemoryManager.get_legacy_dir(identity, resource_proxy=…)`，拿掉兩個 `# noqa: SLF001`；`uuid` 匯入移到模組頂端。
- `LegacyStore` 寫入失敗往上拋；同步逐檔處理，寫入都成功才把 JSON 移到 `backup/`，失敗的檔案原地保留並計入 `errors`。
- `LegacyStore` 的資料庫在第一次寫入時才建立；只查看（統計、隨機抽取）時資料庫不存在就回 0／空清單，不建檔。同步時目錄不存在就什麼都不建。
- `GET /v1/stats/legacy/{agent_id}` 失敗回 `SYSTEM_ERROR`（500）；`POST /v1/stats/legacy/{agent_id}/sync` 發生例外或有任一檔失敗回 `SYNC_FAILED`（500，附 `synced`、`errors`）。前端只在 HTTP 成功且 `status == "success"` 時顯示「同步成功」，失敗時顯示錯誤並重抓統計；統計失敗也顯示錯誤。

### 4. 隨機回想與記憶閘道

- 隨機回想兩個來源各自記錄錯誤；什麼都沒抽到、且至少一個來源失敗時，丟 `MemoryRecallUnavailableError`，不再回「腦海中空蕩蕩」。只有一半失敗、另一半有片段時，照常回片段並加一行「部分記憶來源暫時讀不到」。
- Proxy 記憶閘道的錯誤（未載入、呼叫失敗）改用 `build_tool_error(internal_error, …)`，不回傳例外原文。
- 閘道錯誤改成 JSON 後也能被 `json.loads` 解開，所以 GDPR 匯出（`_fetch_agent_facts`）除了舊的 `startswith("Error")`，也用 `is_tool_error()` 判斷，失敗一律回 None（沿用原本的優雅降級），不把錯誤 JSON 當成事實匯出。Proxy 內沒有其他呼叫 `memory_tools(` 並靠字首判斷的地方。

### 5. 恢復 `manage_dups` 的 scan

- 照 2026-04-22 刪除前的做法恢復 `MemoryManager.find_duplicates`，改收身分物件：取最近的事實，每筆做一次向量查詢，相似度達門檻才列為候選，每對只列一次。公開合約（`operation=scan|resolve`）不變。
- 模組常數（禁令 18）：`MAX_DUPLICATE_SCAN_FACTS`（每次最多比對筆數）、`DUPLICATE_SIMILARITY_THRESHOLD`、`DUPLICATE_CANDIDATES_PER_FACT`。
- 記憶診斷的向量數改用既有的 `VectorStore.agent_point_count(identity)`。
- 刪除死碼 `diag_tools`。

### 6. Guard #45 讀內部 action 正本，加確切清單測試

- 守衛改從 `memory_server/tools/contracts.py` 的 AST 讀 `MEMORY_INTERNAL_ACTIONS`，不再寫死 `{"export_facts"}`。只接受唯一一次、`frozenset({…字串常數…})` 形式的宣告；其他寫法（非字面值、重複宣告）直接報錯。
- 這讓「把 action 加進內部清單」不必再改守衛，所以同時新增 `test_memory_internal_actions_exact`：斷言執行期常數與守衛讀到的內容都等於 `{"export_facts"}`；新增內部 action 一定要改這個測試，審核時看得到。
- 另補兩個合約不變式測試：七個公開 schema 都不廣告執行期保留欄位；`guard_service` 的「工具.action」訊號都存在於合約表。

### 7. 收掉直接名 `random_recall`、`memory_diagnostics`

- 產品與測試內沒有呼叫者；唯一出現處是 smoke 工具覆蓋表的分類列，同步移除。公開入口維持 `memory_tools(action="random"|"diagnostics")`。

## 取捨

- 提示在 lifespan 算一次，而不是 `/v1/memory` 第一次被讀時：
  - 計畫寫的是 Proxy 啟動時記一次警告；放在 lifespan，沒人開畫面也會在啟動 log 留下紀錄。
  - lifespan 已集中所有啟動接線，可用 `test_lifespan_bootstrap_wiring` 這類測試的模式釘住；端點只讀 context，沒有副作用。
  - 計算只列檔名、同步完成；smoke 沙盒裡記憶目錄是空的，立刻回空清單，不留背景工作。
  - 代價：重啟前不會反映人手動搬移的檔案（等第一次讀取才算的做法也一樣）。
- 不重用 `system_degraded`：理由見決策 2。
- 不新增錯誤碼：統計失敗用既有 `SYSTEM_ERROR`，同步失敗用既有 `SYNC_FAILED`。

## 影響

- `/v1/memory` 多一個欄位；端點沒有 `response_model`，openapi 不變。
- `/v1/stats/legacy/*` 失敗由 200 改為 500＋錯誤碼；兩個端點沒有 `response_model`，openapi 的回應 schema 不變。
- 人按「同步」時，舊對話 JSON 才會真的寫進舊對話庫並移到 `backup/`（原檔不刪）；寫入失敗的檔案原地保留。
- NOQA_REGISTRY：`SLF001` 69 → 67，刪 `services/legacy_sync.py` 的兩列；同檔 `ARG002` 行號隨程式位移。

## 已知限制（本批不動）

- `async_db.cleanup_expired_sessions()` 以秒算截止值，但 `last_activity_at` 存毫秒，所以「刪 90 天前的舊對話」從沒刪過任何列。直接修好的話，下次啟動會一次刪光 90 天前的非家對話（不轉存）。列在第二批，連同對話生命週期由 Owner 決定（修好並先轉存、正式拿掉這個清理、或維持現狀）；決定前不要順手修。
- `FactualStore.query_facts` 仍把查詢失敗吞成空清單；隨機回想只偵測得到呼叫層級的失敗（例如參數錯），資料庫讀取失敗仍會被當成「沒有事實」。列在第二批，和記憶庫正本化一起處理。
- `time_utils.py` 仍判斷已退役的工具名 `save_memory`，要改 `tool_executor.py`，延到第二批。
- 舊事實檔提示最多算到 50 個靈魂：`glob_search` 每次最多回 50 筆。
- `/v1/memory` 沒有正式的 Pydantic 回應模型，前端的提示型別（`MemoryNotice`）只能自己宣告，不經 openapi 產生。
- 同步時檔案都已搬完、但最後讀統計（`get_stats`）失敗，整次會回 `SYNC_FAILED`。
- `useLegacySync` 不帶參數呼叫時，預設的 `agents=[]` 每次 render 都是新陣列，載入統計的 effect 會一直重跑；目前唯一的呼叫端有帶參數。

## 決策落點表

| 決策 | 檔案 | 位置 | 測試／守衛 |
|---|---|---|---|
| 1 退役自動匯入、只看檔名 | `ToukaMemory/src/memory_server/factual.py` | `FactualStore.__init__`、`find_unmigrated_jsonl_agents`、`LEGACY_FACTS_JSONL_PATTERN` | `test_memory_p0_repairs.py` |
| 2 記憶提示（後端） | `ToukaProxy/src/routers/observability.py`、`ToukaProxy/src/main.py` | `MEMORY_NOTICE_*`、`collect_memory_notices`、`get_memory_stats`；`_lifespan_runtime` | `test_memory_notices.py` |
| 2 記憶提示（前端） | `ToukaChat/src/lib/SystemStatusContext.tsx`、`components/sidebar/MemoryNoticeBar.tsx`、`lib/memoryNotices.ts`、`components/sidebar/left/SidebarIdentity.tsx`、`components/LeftSidebar.tsx`、`components/RightSidebar.tsx` | `parseMemoryNotices`、`NOTICE_MESSAGE_KEYS`、`hasMemoryWarning` | 7 個 Vitest 檔（見 `tests/README.md`） |
| 3 同步照實回報 | `services/legacy_sync.py`、`services/legacy_store.py`、`ToukaProxy/src/routers/stats.py`、`ToukaChat/src/hooks/useLegacySync.ts` | `_legacy_dir`、`sync_all_json_to_db`；`_init_db`、`_db_exists`；`get_legacy_sync_stats`、`trigger_legacy_sync` | `test_legacy_sync_repairs.py`、`test_legacy_stats_errors.py`、`useLegacySync.test.ts` |
| 4 隨機回想與閘道 | `tools/recall.py`、`ToukaProxy/src/tools/builtin/memory.py`、`services/gdpr_export_service.py` | `random_recall`、`MemoryRecallUnavailableError`；`memory_tools`；`_fetch_agent_facts` | `test_memory_p0_repairs.py` |
| 5 重複掃描與診斷 | `services/manager.py`、`tools/diag.py` | `find_duplicates`、三個常數；`memory_diagnostics`、`manage_duplicate_memories` | `test_memory_p0_repairs.py` |
| 6 Guard #45 | `scripts/guard_native_tool_contracts.py` | `assigned_string_set`、`run_all` | `test_guard_native_tool_contracts.py`、`test_native_contract_invariants.py` |
| 7 直接名 | `tools/__init__.py`、`tests/smoke/smoke_tool_coverage.py` | `memory_tool_registry` | `test_memory_p0_repairs.py` |

## 驗證

本機 Windows、3.12 venv、fnm Node 24.21.0。自動測試都在 conftest 沙盒或 mock 裡，用合成的舊對話 DB 與事實檔、tmp 本機 Qdrant，不載入 embedding 模型。

- 新測試：pytest 31 個（`test_memory_p0_repairs.py` 15、`test_legacy_sync_repairs.py` 6、`test_legacy_stats_errors.py` 4、`test_memory_notices.py` 3、`test_native_contract_invariants.py` 3），`test_guard_native_tool_contracts.py` 加 4 個（35 → 39）；Vitest 7 檔 27 個。數字是 `pytest --collect-only` 與 `vitest list` 的實數。
- 文件與計數檔：`guard_noqa_registry.py`（`SLF001` 67）、`guard_adr_integrity.py`（253 份連續）、`lint_ssot.py` 通過；文件契約、Guard #19／#28、API schema 同步與來源掃描邊界測試 82 passed。
- 全量：pytest 固定順序 8923 passed／7 skipped／6 failed（6 個是本機 Docker Desktop 的 `test_local_compose_bindings`，以 CI 為準）；guard_all 43／43 PASS；前端 Vitest 全量 1651 passed。
- 突變 43 個（每個只改一處，pytest 或 Vitest exit 1 才算紅）全部變紅，還原後回綠。

## 回復

- 不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）；後端各項與前端可分開 revert。
- 撤回決策 1 會讓每個行程啟動時重新嘗試那個一直失敗的自動匯入，不建議；要正式匯入請走第二批。
