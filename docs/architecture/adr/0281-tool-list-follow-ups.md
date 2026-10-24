# ADR-0281：ADR-0269 的後續：工具使用分三種、個人授權的 MCP、工具名稱比對（T1～T3）

- 狀態：🟡 部分實作（T3、T1 已實作；T2 在後續 PR 補進本 ADR）
- 日期：2026-10-24
- 決策者：維護者（2026-10-24 核准 tool-listing 一頁計畫：T3 → T1 → T2 三個 PR）
- 影響範圍（T3）：ToukaProxy（新 `utils/tool_usage.py`；`services/tool_executor.py`、`services/guard_service.py`、
  `handlers/react_core.py`）；tests（新 `tests/unit/test_tool_usage_tracking.py`；假執行器補記三種集合、
  `memory_warning` 三份 golden 的提醒文字）
- 影響範圍（T1）：ToukaProxy（新 `utils/mcp_request_identity.py`；`middleware/layers/tool.py`、`protocols/manager.py`、
  `protocols/mcp/oauth/runtime.py`、`protocols/mcp/oauth/store.py`、`services/tool_executor.py`、`services/guard_service.py`、
  `services/dispatchers/group.py`、`services/prompt_blocks.py`、`services/context_snapshot_service.py`、`handlers/react_core.py`）；
  tests（新 `tests/unit/test_user_scoped_mcp_security.py`；OAuth 儲存與執行期各補測試、兩個替身補新方法）
- 相關：[ADR-0269](0269-advertised-tool-list-enforcement.md)（另開任務 T1～T3、已知限制 LOW-2）、
  [ADR-0155](0155-handler-drift-fix-batch.md)（C2 訊號單源）、禁令 59（幻覺偵測涵蓋 MCP、被擋的呼叫不算幻覺）；
  T3 的決策都不變更這三者。T1：本 ADR 修訂 ADR-0269 行為改變第 4 點的 (a)(b)（已回指）；
  [ADR-0206](0206-mcp-interactive-oauth-authorization-code.md)（使用者授權只代表人在場、時鐘防線）、
  [ADR-0273](0273-mcp-connection-owner-task.md)（不從呼叫端取消連線）、ADR-0195（軸 B）決策都不變

## 背景

ADR-0269 讓模型只能執行這一輪列給它的工具，另開三件事：

- **T3**：執行器在所有檢查之前就把工具記成「用過」（`record_tool_usage`）。被擋下的呼叫（例如無痕或 SNS 裡存記憶）
  也算用過，所以模型說「我記住了」時，記憶守衛以為有存、不跳提醒；查證守衛也把被擋的查詢當成「查過」。
  資料沒有被寫入，只是少了提醒。
- **T1**：組工具清單時沒帶「是誰在問」，使用者各自登入授權的 MCP（`oauth_user`）一律被略過、叫不到；還沒連上的
  共用 MCP 也不列，模型不知道它存在。另外，技能或自訂模型需要這類伺服器時，ToolLayer 沒有身分就呼叫
  `ensure_connected`，`handle_key_for` 丟出 `ProtocolError`，整個請求失敗。
- **T2**：見後續章節（後續 PR 補）。

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

### T1：使用者各自授權的 MCP 只在人在場、和靈魂的前景對話時帶身分

1. 條件是一個純函式（禁令 18）：`utils/mcp_request_identity.py` 的 `user_scoped_mcp_allowed(ctx, request_model=)`。
   全部成立才帶身分：ctx 裡的身分是已驗證的 `AgentIdentity` 而且不是人類（前景代打後的靈魂）、執行身分就是對話目標
   （`identity.agent_id == ctx["agent_id"]`；X-Agent-ID 切成 DebugAgent 或另一個靈魂來驅動這個靈魂時不算）、`is_toukasoul`、
   人在場（`is_human_present`：互動 UI 對話，不是心跳、子代理、派工）、不是群聊（組清單時看 `request.model` 是不是
   `ToukaGroup`；群聊分發器在分給每個成員的 ctx 設 `group_member_run`）、有原始人類 `human_principal_id`。
   ToolLayer 自己用字串補出來的舊路徑不是 ctx 裡的身分，一律不帶。判斷不讀模型參數或客戶端標頭（禁令 16）。
2. 組清單：條件成立時 ToolLayer 用同一個模組的 `mcp_request_context`（從執行器抽出來、語意不變）建請求上下文，
   交給 `ProtocolManager.get_all_tools`。`get_all_tools` 只把它交給 `oauth_user` 的 installation；共用伺服器照舊
   不帶身分、不因此連線，工具清單快取的分區不變；企業版與機器對機器照舊略過。
3. 先查授權再連線：新增只讀的 `McpOAuthRuntimeService.has_usable_grant`（功能開關、伺服器啟用、profile、授權
   fence 都照 `authorize_operation` 的條件）→ `OAuthSqliteStore.has_usable_grant`：不開寫入交易、不推進時鐘高水位、
   不改授權狀態、不解密；指標與授權用同一句 SELECT 讀（中途的提交不會被誤判成損毀）；指標指向不存在的授權算損毀、
   照 ADR-0206 丟出，`get_all_tools` 記錯誤並略過那一台。沒授權就不連；有授權時第一次連線最多等
   `USER_SCOPED_CONNECT_TIMEOUT_S`（5 秒），逾時只略過這一輪，背景連線照樣跑完、失敗由 `spawn_logged` 記下
   （ADR-0273：不從呼叫端取消）。同一個人＋靈魂＋伺服器上一輪的連線還在跑時不再開、也不等；連線、授權查詢失敗或被取消
   後 `USER_SCOPED_RETRY_COOLDOWN_S`（60 秒）內不再試。真的連線時照舊由既有防線推進時鐘（ADR-0206）。
4. 每一台各自包住錯誤：一台的授權查詢、handle 解析或連線出錯只略過那一台。
5. 技能或自訂模型需要要個人授權的伺服器時，ToolLayer 不再為它們呼叫 `ensure_connected`；要列出只能經第 1 點的條件。
6. 執行器的第二層防線（模型迴圈的執行器，`enforce_advertised=True`）：呼叫 `oauth_user` 伺服器的工具前再用同一個
   條件確認，排在這一輪清單核對之後、隔離層級判定、審查員、審批卡與同意帳本之前；不符回 `permission_denied`／
   `user_scoped_mcp_unavailable`。人按的 `/debug/execute` 照 ADR-0269 不套用。
7. 幻覺偵測認得的工具名多加「這一輪 ToolLayer 為使用者列出的授權工具」（ToolLayer 記在 `ctx["user_scoped_tool_names"]`，
   `collect_known_tool_names` 的 `extra_names`）；不收客戶端自帶的工具名，不擴大誤報；不讀這一輪的清單物件。
8. 沒連上的共用伺服器（維護者決定 1 甲：只在說明裡寫明）：`ProtocolManager.unavailable_shared_servers()` 只看已同步的
   installation 狀態、不連線、不寫入（沒有 handle、連線已死或斷路器打開）；ToolLayer 依角色權限（軸 B）、動態掛載與
   這一輪的工具清單（`desired_tools`）過濾後，注入每輪會變動的 `<mcp_unavailable>` 區塊。名稱可由有權限的人自訂，
   只原樣列出 `[A-Za-z0-9._-]{1,64}` 的名稱，其他只寫數量；讀狀態出錯只記警告、不注入、不讓請求失敗。區塊登記在
   `ALL_BLOCK_TAGS`、`VOLATILE_SYSTEM_TAGS` 與情境快照的區塊說明。

## 行為改變（T3）

- 存記憶被擋下或失敗時，模型說「記住了」會跳記憶提醒（原本不會）。
- 無痕或 SNS 裡查證工具被擋時，查證守衛會認為「沒查」，可能多一次矯正（四個轉接器都有一次性的 `honesty_corrected`
  上限）：矯正會要求模型去查，工具結果回來還要再送一次，所以約多 1～2 次上游請求，並扣 1 個工具輪額度。
- e_index 的檢索分量只在 `memory_tools.query` 真的交給 handler 時才算。

## 已知限制（T3）

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

## 行為改變（T1）

- 人在場、和靈魂的前景對話：你自己登入授權過的 `oauth_user` MCP 會列給模型、叫得到（原本一律 `not_advertised`）。
  心跳、SNS、群聊、子代理、派工、直接和一般模型聊天、情境快照、計分卡都照舊不列：沒列出的工具照舊回
  `not_advertised`；同一輪列出了、但這次執行不是人在場的前景對話（例如群聊成員）時回 `user_scoped_mcp_unavailable`。
- 技能或自訂模型需要要個人授權的伺服器時，請求不再失敗。
- 設定好但沒連上的共用伺服器會在說明區塊裡列出名字，模型不會以為它不存在，也不會被列成工具。
- 前景對話每輪為每台 `oauth_user` 伺服器做一次只讀的授權查詢（沒有這類伺服器時成本是零）。有授權、還沒連上時，
  第一次連線最多多等 5 秒；連線還在跑或失敗後 60 秒內，這一輪直接略過、不再等。

## 已知限制（T1）

- 靈魂設定畫面的工具清單不知道是誰在看，列不出使用者個人授權的工具；設了工具清單的靈魂照樣用不到這些工具，要用就
  把工具清單留空（和 T2 的交集）。
- 第一次連線逾時或失敗的那一輪不列；連上後下一輪才出現，失敗時 60 秒後才再試。
- 有好幾台使用者授權伺服器同時要第一次連線時，`get_all_tools` 一台一台等，最壞那一輪多等 5 秒乘以台數（之後各台
  不再等）。
- 連上的使用者授權連線和共用伺服器一樣常駐（沒有閒置關閉；訂閱照伺服器設定）；解除授權後連線不會立刻關，工具可能還
  列到清單快取過期為止，但呼叫仍會被 `authorize_operation` 與每次換發的 bearer 防線擋下。
- 自己的使用者授權伺服器連不上或授權資料損毀時，只記 log；說明區塊刻意只列共用伺服器，畫面與模型都看不到原因
  （和 ADR-0078「失效不沉默」的方向不完全一致，另排）。
- 除了 ToolLayer，`get_all_tools` 的其他呼叫端（除錯面板、觀測、守衛的 MCP 清單）照舊不帶身分，看不到使用者授權的工具。
- 說明區塊只寫「沒連上」，不寫原因；心跳、SNS 等無人執行也會收到這段說明。
- 維護者目前沒有設定任何 `oauth_user` 伺服器，這部分只用替身測過，沒有真的走一次 OAuth。

## 驗證（T1）

- 新增 `tests/unit/test_user_scoped_mcp_security.py`（52 個）：條件函式每一種情況（含執行身分不是對話目標）、真的
  `ProtocolManager` 組清單（只有 `oauth_user` 拿到身分、共用伺服器不連線且快取分區不變、沒授權不連、授權資料損毀記錯誤、
  慢連線略過但不取消、連線還在跑時不再等也不再開、失敗後冷卻、授權查詢出錯不每輪重查、取消不外漏、一台出錯不影響其他、
  enterprise 不拿身分、不同人的連線分開）、ToolLayer（前景靈魂帶身分、其他情況不帶、技能與自訂模型不再失敗、說明區塊依
  權限、掛載與工具清單過濾、名稱跳脫、讀狀態出錯不失敗、記下列出的使用者授權工具）、`unavailable_shared_servers` 的
  連線已死與斷路器分支且不寫入、執行器第二層防線排在隔離層級判定之前、`/debug/execute` 不套用、幻覺偵測只多認伺服器
  列出的工具、群聊成員的 ctx 有旗標。OAuth 儲存補 4 個（只讀：前後資料列完全相同、過期沒有 refresh 也不改狀態、懸空指標
  丟損毀、不解密且一句讀完），執行期補 2 個（不推進時鐘、fence 或停用時回否且不查儲存）。每一批都先紅再實作。
- 只改替身的：`test_skill_mcp_tool_injection` 的 `get_all_tools` 收 `request_context`、`test_mcp_isolation_reconnect`
  的 OAuth 替身補 `has_usable_grant`（讓 stdio 伺服器照舊在列清單時顯示拒絕原因）。ADR-0269 的 A8、B15、B16 原樣綠。
- 突變 29 項全部有測試變紅：條件函式拿掉在場、群聊旗標、原始人類、靈魂身分、執行身分等於目標；每台都拿身分；不查授權
  就連；逾時取消背景連線；連線還在跑時照樣再開；失敗不冷卻；取消外漏；一台出錯中斷整份清單；拿掉第二層防線；技能路徑
  照樣連要個人授權的伺服器；群聊請求也帶身分；儲存查詢改成會寫入；儲存分兩次讀；懸空指標當成沒授權；執行期推進時鐘；
  略過授權 fence；群聊沒標成員；說明不依權限過濾、不依工具清單過濾、名稱不跳脫、讀狀態出錯讓請求失敗；ToolLayer 不記
  列出的使用者授權工具；幻覺偵測不認這些工具；損毀不記錯誤。
- 審查：DEV-B（新上下文）與兩位只讀對抗式審查（攻擊者、回歸），三位都獨立找到「壞掉的使用者授權伺服器每輪重連」；
  其他意見（取消外漏、名稱跳脫、執行身分要等於目標、只讀查詢的兩次讀、說明區塊過濾與讀錯處理、幻覺偵測誤報）都已修。
