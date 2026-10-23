# ADR-0258：記憶系統第二批段 T1：舊對話時間統一毫秒，給模型看的時間一律台北並標時區

- 狀態：✅ 已接受（維護者 2026-10-15 核准記憶系統第二批計畫；本 ADR 是段 T1 的 PR，P28 撤回另見同一個 PR 的 ADR-0259）
- 日期：2026-10-15
- 決策者：維護者
- 影響範圍：ToukaMemory（新 `utils/legacy_time.py`、`services/legacy_store.py`、`services/legacy_sync.py`、`tools/legacy_vault.py`、`tools/recall.py`、`services/manager.py` 的 `normalize_time_range` 與 `query_memory` 日期顯示）；ToukaProxy（`utils/time_utils.py` 的 `inject_time_to_result`）
- 相關：ADR-0013（時間 SSOT）、ADR-0030（sqlite3 匯入限制）、ADR-0119（檔案動詞）、ADR-0228（Guard #45）、ADR-0253（第一批修補與已知限制）、ADR-0256（記憶來源）、ADR-0257（`<continuity>` 用台北時間、歷史資料界線），決策都不變；解決 ADR-0253 已知限制「`time_utils.py` 仍判斷已退役的 `save_memory`」

## 背景

- 舊對話庫（`legacy_history.db`）的訊息時間混了秒與毫秒：滑動視窗匯出與同步寫毫秒；前端「切分存入舊對話」與 Open WebUI 匯出寫秒；同步照原值寫入。
- 日期搜尋：`normalize_time_range` 回秒，舊對話查詢直接拿去比毫秒列——`date_end` 擋掉所有毫秒列，`date_start` 沒有作用，只有秒列會被找到。
- 顯示：舊對話搜尋只要大於 1e9 就除以 1000，秒列顯示成 1970 年，而且用 UTC、不標時區。隨機回想與查記憶用程序本機時區（Docker 是 UTC），和 `<continuity>` 的台北時間最多差 8 小時，日期也可能差一天。
- `YYYY-MM-DD` 日期參數用程序本機時區解析成一天的範圍，Docker 下和台北差 8 小時。
- `inject_time_to_result` 判斷 `tool_name == "save_memory"` 才加「勿捏造記憶時間」提醒，但執行器傳的是註冊名 `memory_tools`，這段永遠不會觸發。

## 決策

1. **新純函式模組** `memory_server/utils/legacy_time.py`（Memory 不能 import Proxy 的 `time_utils`；只做轉換與格式化，不取現在時間，現在時間仍經 `config_utils.get_current_epoch`）：
   - `to_epoch_ms(value)`：秒或毫秒統一成毫秒整數；小於 `SECONDS_CUTOFF`（1e11）的數字視為秒（1e11 秒是公元 5138 年，1e11 毫秒是 1973 年 3 月）。數字字串照數字讀；ISO 8601 只收帶時區的；空值、非正數、布林、NaN、無限大、沒有時區的 ISO 一律回 None（讀不出，不猜）。
   - `format_taipei(value, with_time=True, unknown=...)`：給模型看的時間，格式 `2026-05-13 18:00 Asia/Taipei`（可只到日期）；讀不出回呼叫端給的字樣。時區名稱與 `<continuity>` 相同。
   - `taipei_day_start(date)`：`YYYY-MM-DD` 當台北那天 00:00，回 Unix 秒。
   - `seconds_range_to_ms(start, end)`：含頭含尾的秒範圍換成毫秒範圍，結尾那一秒整秒算進去。
   - 台北用固定 UTC+8（台灣 1980 年起沒有日光節約，與 `Asia/Taipei` 規則結果相同，不依賴 tzdata）。
2. **舊對話庫內部一律毫秒**：同步（`_extract_messages`）與寫入（`upsert_messages`）都經 `to_epoch_ms`，順序是 `timestamp` → `createdAt` → 同步當下。既有列不改（`ON CONFLICT` 本來就不更新 timestamp）；`query_messages` 的日期篩選與排序用 `CASE WHEN timestamp < ? THEN timestamp * 1000 ELSE timestamp END`，修正前寫入的秒列在查詢時才換算。SQL 是固定字面量，門檻用參數傳入（禁令 24）。
3. **事實庫與向量庫仍是秒**（結構不在本段）：`normalize_time_range` 繼續回秒，只把日期改成台北的一整天；舊對話搜尋在呼叫端用 `seconds_range_to_ms` 換成毫秒範圍。
4. **給模型看的時間一律台北並標時區**：舊對話搜尋（日期與時間）、隨機回想（舊對話與事實，只到日期）、查記憶（只到日期）都經 `format_taipei`；讀不出的列顯示「未知時間」（舊對話搜尋顯示 `Unknown`）。同一時刻在這幾處與 `<continuity>` 顯示同一個台北時間。
5. **Guard #45**：`access_legacy_vault`、`random_recall`、`normalize_time_range` 是未分類型 consumer，形狀完全不變（參數、`.get`、`**` 都沒增減，三個檔沒新增函式）；新邏輯都在 `utils/`。基準檔不動。
6. **刪 `inject_time_to_result` 的 save_memory 提醒**：工具結果的時間尾綴保留（長 ReAct 與心跳多輪中唯一隨工具呼叫前進的後端時鐘）；`tool_name` 參數保留，不改 `tool_executor.py`。「存記憶要寫絕對日期」的用意之後由 continuity 承接。

## 模型實際走得到的路徑（寫給之後的人）

- `memory_tools(action="random")` → `random_recall`：唯一讀舊對話資料庫的模型入口（本段修顯示）。
- `memory_tools(action="query")` → `query_memory` → `normalize_time_range`：只查事實庫與向量庫（本段修日界與顯示）。
- 檔案動詞 `read／list／find／grep(scope="legacy")`（ADR-0119；舊 `vault_tools(vault="legacy")` shim 同路）→ `core_file_ops`：只讀 `legacy/<agent>/` 的原始 JSON（含 `backup/`），`grep` 略過 `.db`；檔案動詞必須逐字回傳內容，時間是原始數字，本段不改。
- 〔ADR-0274 補記：已改寫為 `search_conversations` 並接到 `memory_tools(action="conversations")`。〕`legacy_vault.access_legacy_vault`（舊對話資料庫的日期搜尋）目前不在任何分發表；本段照修照測，接不接上另外決定。

## 後果

- 秒列不再顯示成 1970 年；日期搜尋秒列、毫秒列都找得到，日界是台北；排序依真實時間。
- 給模型看的時間多了 `Asia/Taipei` 標記（每個時間約多 3～4 個 token）。
- 讀不出時間的舊列（例如任意文字）顯示「未知時間」，不再原樣顯示前 10 個字。
- 沒有時區的 ISO 字串在同步時視為讀不出，改用 `createdAt` 或同步當下。

## 驗證

- `tests/unit/test_legacy_time.py`：換算與呈現的對照表（秒、毫秒、1e11 邊界、跨日、無效值）、與 continuity 同字串、台北日界、秒範圍換毫秒。
- `tests/unit/test_legacy_dates.py`：從 Proxy `memory_tools` 入口測隨機回想與查記憶；`normalize_time_range` 對照表；舊對話日期搜尋（前一天最後一毫秒、隔天第一毫秒、最後一秒內）、混合單位排序；同步與寫入存毫秒；三個呈現點不呼叫沒時區的 `fromtimestamp`／`strptime`（主機時區剛好是台北時，行為測試測不出，靠這條）。
- `tests/unit/test_tool_result_time_suffix.py`：任何工具名（含 save_memory）的尾綴都只有時間。
