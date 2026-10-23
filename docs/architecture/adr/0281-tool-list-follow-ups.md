# ADR-0281：ADR-0269 的後續：工具使用分三種、個人授權的 MCP、工具名稱比對（T1～T3）

- 狀態：🟡 部分實作（T3 已實作；T1、T2 在後續 PR 補進本 ADR）
- 日期：2026-10-24
- 決策者：維護者（2026-10-24 核准 tool-listing 一頁計畫：T3 → T1 → T2 三個 PR）
- 影響範圍（T3）：ToukaProxy（新 `utils/tool_usage.py`；`services/tool_executor.py`、`services/guard_service.py`、
  `handlers/react_core.py`）；tests（新 `tests/unit/test_tool_usage_tracking.py`；假執行器補記三種集合、
  `memory_warning` 三份 golden 的提醒文字）
- 相關：[ADR-0269](0269-advertised-tool-list-enforcement.md)（另開任務 T1～T3、已知限制 LOW-2）、
  [ADR-0155](0155-handler-drift-fix-batch.md)（C2 訊號單源）、禁令 59（幻覺偵測涵蓋 MCP、被擋的呼叫不算幻覺）；
  T3 的決策都不變更這三者

## 背景

ADR-0269 讓模型只能執行這一輪列給它的工具，另開三件事：

- **T3**：執行器在所有檢查之前就把工具記成「用過」（`record_tool_usage`）。被擋下的呼叫（例如無痕或 SNS 裡存記憶）
  也算用過，所以模型說「我記住了」時，記憶守衛以為有存、不跳提醒；查證守衛也把被擋的查詢當成「查過」。
  資料沒有被寫入，只是少了提醒。
- **T1**、**T2**：見後續章節（後續 PR 補）。

## 決策

### T3：工具使用分成「試過」「執行過」「存成功」

1. 新模組 `ToukaProxy/src/utils/tool_usage.py` 是訊號生產的單一來源：
   - `ToolUsage` 是 `set[str]` 的子類別：集合本身是「試過」，另帶 `executed`（執行過）與 `succeeded`（存成功）。
   - 三個記錄函式 `record_tool_usage`／`record_tool_executed`／`record_tool_succeeded`：每筆記工具名本身，聚合工具帶
     `action` 時另記限定名 `name.action`（照 ADR-0155 C2）。
   - 讀取函式 `executed_tools`／`succeeded_tools`：給的是 `ToolUsage` 就回對應集合；只給一個集合的呼叫端（舊的測試
     替身）照舊看那個集合。
   - 放在 `utils/` 而不是執行器模組：ADR-0269 的匯入棘輪只准 `react_core` 與 `routers/debug.py` 從 `tool_executor`
     匯入 `ToolExecutor`，守衛與其他模組不能匯入執行器。`tool_executor` 從這裡匯入 `record_tool_usage`，原本的
     匯入路徑照常可用。
2. 執行器（`ToolExecutor.execute_to_queue`）：
   - 「試過」照舊在最前面記（幻覺偵測用）。
   - 「執行過」在真正交給 handler（原生、外掛或 MCP）之前記；在那之前的所有提前返回（找不到、沒列出、角色權限、PaC、
     審批、參數驗證、沒帶參數）都不算執行過。
   - 「存成功」在結果判定成功（`is_success`）時記；工具回結構化錯誤或丟例外都不算。
3. 讀的人：
   - 記憶守衛（`guard_service.check_memory_hallucination`）只看「存成功」。
   - 查證守衛（`check_fact_without_tools`）與檢索訊號 e_index 看「執行過」。
   - 幻覺偵測（`detect_tool_hallucinations`）照舊看「試過」：被擋的呼叫不算幻覺（禁令 59(c)）。
4. `react_core` 每次 run 建一個 `ToolUsage` 傳給執行器與四個轉接器；轉接器 `mid_loop_hook` 的簽名不變、不新增匯入。
5. 記憶守衛的提醒文字從「未呼叫 `memory_tools` 進行儲存」改成「這一輪沒有成功用 `memory_tools` 存下記憶」
   （呼叫了但被擋時原本的說法不準）；開頭的 `[記憶幻覺警告]` 標籤保留（歷史清理靠它辨認）。

## 行為改變

- 存記憶被擋下或失敗時，模型說「記住了」會跳記憶提醒（原本不會）。
- 無痕或 SNS 裡查證工具被擋時，查證守衛會認為「沒查」，可能多一次矯正（四個轉接器都有一次性的 `honesty_corrected`
  上限）：矯正會要求模型去查，工具結果回來還要再送一次，所以約多 1～2 次上游請求，並扣 1 個工具輪額度。
- e_index 的檢索分量只在 `memory_tools.query` 真的交給 handler 時才算。

## 已知限制

- 「執行過」包含執行後失敗的呼叫：查證守衛把「查了但後端出錯」也算有查（守衛管的是「有沒有去查」，不是查得對不對）。
- 只給一個集合的呼叫端沒有「執行過」與「存成功」可記，守衛照舊看那個集合；正式程式只有 `react_core` 這一個建立點，
  它建的是 `ToolUsage`。
- 「存成功」沿用執行器既有的成功判斷：記憶系統因內容相似而沒寫入時回的「⚠️ [重複內容]」也算成功，記憶守衛不提醒
  （完全相同的內容本來就存在時這是對的；「語意相近」時新內容其實沒寫入）。要分開得讓 ToukaMemory 回結構化結果，另排。

## 驗證（T3）

- 新增 `tests/unit/test_tool_usage_tracking.py`（22 個）與 `test_react_memory_detection.py` 的「save 被擋仍要提醒」；
  先跑出紅燈（新模組不存在；DEV-B 補的「沒帶參數不算執行過」也先紅）再實作。真執行器測試涵蓋沒列出、PaC 拒絕、
  無人在場要審批、只有人能核准、審批被拒、參數驗證錯誤、沒帶參數、成功、回結構化錯誤、丟例外、舊式失敗文字、重複提示。
- 假執行器（characterization harness、`test_per_round_accounting`、`test_usage_thoughts_and_cache_hit`、
  `test_long_horizon_prompt_invariants`、`test_react_memory_detection`）改成也記「執行過」「存成功」，代表真執行器的成功路徑；
  `test_memory_signal_ssot` 的靜態確認改成 e_index 讀 `.executed`。
- `memory_warning` 的三份 golden（claude、gemini、openai）照刻意行為變化的規則刪檔重錄，各只差提醒那一行。
- 突變 10 項全部有測試變紅：記憶守衛改看試過、查證守衛改看試過、e_index 改看試過、每個結果都算存成功、
  在檢查之前就記執行過、在 PaC 之前記執行過、在工具開始事件時記執行過、從不記執行過、存成功記進試過、拿掉
  `[記憶幻覺警告]` 標籤。
