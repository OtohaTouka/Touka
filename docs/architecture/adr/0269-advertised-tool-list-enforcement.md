# ADR-0269：模型只能執行這一輪列給它的工具（執行前核對清單）

- 狀態：✅ 已接受（安全修補 h2；維護者 2026-10-15 核准計畫第 3 版，同日決定執行器開關採方案 (a)；產品行數上限同日由 +150 提高到 +190）
- 日期：2026-10-15
- 決策者：維護者
- 影響範圍：ToukaProxy（新 `models/advertised_tools.py`；`models/context.py`、`middleware/layers/tool.py`、`services/tool_executor.py`、`handlers/react_core.py`、`routers/debug.py`、`tools/builtin/__init__.py`、`tools/subagent_tools.py`）；tests（新 `tests/unit/test_advertised_tool_list_security.py`；`tests/integration/test_tool_loop_budget.py` 2 個測試的授權設定；`tests/tool_eval/live_episode.py` +2 行）；SECURITY.md 一列
- 相關：本 ADR 修訂 ADR-0119 Tier 3 第 6 點「雙軌遷移」與「後果」的「舊名呼叫經 shim 完全相容」（改為有條件相容，見決策 5）；補註 ADR-0195（軸 B 不變，這一輪的清單另外也是執行條件）。ADR-0228（原生角色權限）、ADR-0229（批准分類順序）、ADR-0246（三種模式、底線、審查員）、ADR-0206（使用者各自授權的 MCP）、禁令 59（結構化工具錯誤），決策都不變
- 2026-10-24 由 [ADR-0281](0281-tool-list-follow-ups.md) 修訂（T1）：行為改變第 4 點的 (a) 改成人在場、和靈魂的前景對話時可以用使用者各自授權的 MCP；(b) 還沒連上的共用 MCP 改成在說明區塊列出名字（仍不列成工具）；(c) 等 T2

## 背景

1. ToolLayer（Gate 1）每一輪先挑好「這次給模型看哪些工具」：靈魂與自訂模型的 `enabled_tools`、無痕拿掉 `memory_tools`、無人在場拿掉 `ask_user`、動態掛載時只列已掛載的 MCP、子代理拿掉 `dispatch_subagent`。
2. 執行端沒有再對一次這份清單。模型直接喊沒列出的工具名，只剩原生角色權限（ADR-0228）、MCP 軸 B（ADR-0195）、PaC 與審批把關：
   - 無痕對話裡叫 `memory_tools(query)` 讀得到記憶（`query` 是唯讀，三種模式都直接放行）；
   - 靈魂只開「讀檔」，硬叫 `execute_command`：YOLO 下直接執行；唯讀類動作在任何模式都直接執行；
   - 無人在場叫 `ask_user`，空等 600 秒；
   - 跳過 `connect_mcp_server` 直接叫沒掛載的 MCP 工具也能執行（依模式可能跳審批）；
   - 外掛（plugin）從不列給模型，卻能被執行、不查角色權限，YOLO 下直接執行；
   - 根本不存在的名稱先進 PaC：預設風險 dangerous，Manual 跳審批卡、Auto 叫審查員、無人在場回 `approval_required`，最後才回 `resource_not_found`。
3. 子代理只拿掉 `dispatch_subagent`，沒拿掉 `dispatch_subagent_group`。
4. 同一個 ReAct 迴圈裡清單不會變：ToolLayer 每個請求只跑一次、`prepare_request` 只呼叫一次；`connect_mcp_server`、`activate_skill` 只寫 session，文件寫的語意本來就是「下一輪可用」；心跳每次脈衝重跑 ToolLayer。

## 決策

1. **清單物件**：新純模組 `models/advertised_tools.py` 的 `AdvertisedTools`（frozen dataclass，不做 I/O）：`native` 是原生工具名的 frozenset，`mcp` 是（伺服器, 裸工具名）的 frozenset；`__post_init__` 一律轉成不可變集合（呼叫端事後改自己的 list 影響不到）。`from_schemas` 由注入的 schema 算清單（帶 `server` 的是 MCP，沒有裸名時用廣告名）；`permits_native`、`permits_mcp`、`has_server` 三個查詢。
2. **只有系統能寫**：`PipelineContext.advertised_tools`，型別 `InstanceOf[AdvertisedTools] | None`（dict、frozenset 一律拒絕，不會被轉型成物件）。寫入者：
   - ToolLayer：`_inject_tools` 改成回傳實際放進請求、去重之後的 schema，清單由它算；一律覆寫（心跳與子代理組裝共用 ctx，舊清單不得沿用）。被客戶端同名 schema 蓋掉的伺服器端工具不算列出（模型看到的是客戶端的版本）。
   - `_run_subagent`：用子代理自己組裝出的 `request.tools` 算，不沿用父代理。
   - `tests/tool_eval/live_episode.py`：唯一列名的測試例外（見決策 7）。
   不加進 `EXECUTION_CONTEXT_KWARG_NAMES`（那是交給 handler 的 kwargs）；核對不讀模型參數，執行器再用 `isinstance` 把一次關，不是 `AdvertisedTools` 就當成「清單不可用」。
3. **執行器開關**：`ToolExecutor(..., *, enforce_advertised: bool = False)`，只能用名稱指定，存成私有屬性、對外只有唯讀 property。`run_react_loop` 寫字面 `True`；`/debug/execute` 明寫 `False`（人按的、已過特權閘，模型選不到這條路）。理由與守法見下一節。
4. **核對位置與結果**：開關打開時，順序是身分 → 原生角色權限（ADR-0228，保留 `native_tool_rbac` 原因碼）→ MCP 名稱解析 → **清單核對** → 軸 B → 分類＋PaC → 審查員／審批卡 → 參數驗證 → 執行。

   | 情況 | 判定 | 錯誤類別 | reason |
   |---|---|---|---|
   | 完全找不到 | 不是 builtin／memory／plugin，且解析出的 MCP 伺服器不在 registry、也沒有任何一對用這台出現在清單上 | `resource_not_found` | （無） |
   | 清單不可用 | 缺失或型別不對（含 SNS 的 dict ctx 放了別的東西），並記 `logger.error` | `internal_error` | `advertised_tools_unavailable` |
   | 這一輪沒列出 | 其餘不在清單上的 | `permission_denied` | `not_advertised` |

   三種都不呼叫審查員、不跳審批卡、不執行；串流送一則 `tool_result`（`status` 為錯誤、帶 `error_class`；工具事件不帶模型給的參數）並結束這次呼叫。`record_tool_usage` 留在最前面，被擋的呼叫仍算「試過」，不會被當成幻覺（禁令 59(c)）。誤判只會落在錯誤類別，不會放行沒列出的工具。
5. **舊名工具**：`tools/builtin/__init__.py` 的 `LEGACY_ADVERTISED_SUCCESSORS`（`MappingProxyType`）：`bash`→`execute_command`；`vault_tools`、`project_tools`→ 7 個檔案動詞（read、list、find、grep、write、edit、delete）；`update_my_soul`→`edit_soul`、`read`、`list`；`sns_control`→`send_message`；`propose_new_capability`→`evolution_define`；`write_mcp_implementation`、`verify_mcp_locally`、`request_mcp_creation`→`evolution_implement_and_verify`。後繼工具**全部**列出才放行，不看模型給的 action。鍵必須等於 Guard #45 基準（`scripts/native_tool_contract_baseline.json`）的 `legacy_tools`，測試釘住。舊名各自的角色權限照舊。
6. **MCP**：核對（伺服器, 裸工具名）這一對。廣告名是 `server__tool`、執行時從第一個 `__` 切開，伺服器名含 `__` 時兩對會撞名；清單從去重後的注入結果算，只放行模型實際看到的那一對。
7. **各入口**：四個 handler、前景 UI／IDE、SNS、群聊（`model_copy` 沿用清單）、心跳 awaken／retrigger、子代理與子代理群組、計分卡都經 `run_react_loop`，自動涵蓋；換備援模型共用同一個 ctx，清單保留。`tests/tool_eval/live_episode.py`（人工 live 工具評估）自己組 ctx 再呼叫 `run_react_loop`，不寫清單就會全部回 `advertised_tools_unavailable`，所以加 2 行寫入；它是結構測試 C3 白名單裡唯一的測試檔，其餘 `tests/` 不在掃描範圍。
8. **子代理**：常數 `_SUBAGENT_STRIPPED_TOOLS = {dispatch_subagent, dispatch_subagent_group}`，請求與清單同步拿掉；就算角色權限允許，子代理也不能再派子代理或子代理群組。

## 執行器開關預設關，靠結構測試＋執行期檢查守住

**決定**：開關預設關、只能用名稱指定、私有屬性加唯讀 property；正式程式只允許兩個建立點，各自寫出字面值；react_core 建好執行器後再做一次執行期檢查。

| 方案 | 既有測試改動 | 新入口忘了設定時 |
|---|---|---|
| (a) 預設關＋只能用名稱指定＋結構測試＋執行期檢查（採用） | 0 | 經 `run_react_loop` 一定開；在別處建執行器 → CI 紅 |
| (b) 必須傳、沒有預設值 | 31 檔 64 處建構 | 建立時 TypeError 當掉＋仍要 C2 |
| (c) 預設開 | 同 (b)（補 `False` 或補清單） | 執行期就是開的＋仍要 C2 |

- **結構測試 C2**（範圍見下方「掃描範圍」，只讀原始碼、不匯入）：所有建立點收成（檔案, 外層函式, 字面值）的清單，**不去重**，排序後必須**整個等於** `[(debug.py, debug_execute_tool, False), (react_core.py, run_react_loop, True)]`；新增、移除、改值、沒帶參數（`MISSING`）、傳變數（`NON_LITERAL`），或同一個允許點重複建立（字面值相同也算），都轉紅，失敗訊息印出每個建立點。識別字 `ToolExecutor` 只能出現在三個檔；改名 import、`partial`、子類別、當參數傳遞、整串等於 `"ToolExecutor"`／`"src.services.tool_executor"` 的字串都轉紅；tool_executor.py 以外對開關的任何寫入、出現 `_enforce_advertised`、整串等於開關名的字串也轉紅。**匯入棘輪**：`tool_executor` 模組只有 react_core.py、debug.py 能匯入，而且各只能是那一行 `from src.services.tool_executor import ToolExecutor`；其他正式檔用絕對、相對、`import … as`、`from … import` 任何名稱，或 `importlib.import_module`／`__import__` 加字面字串匯入這個模組，都轉紅；tool_executor.py 以外出現屬性 `.tool_executor` 或整串字串 `"tool_executor"`（經上層套件取得模組）也轉紅。附掃描器自我測試（每種繞法一個合成片段要轉紅，docstring、路徑字串、名為 `tool_executor` 的區域變數這些反例要保持綠）。
- **結構測試 C3**：識別字或整串字串 `advertised_tools` 只能出現在 5 個白名單檔，各自恰好一處指定用法（context.py 欄位宣告、tool.py 寫入、subagent_tools.py 寫入、tool_executor.py 讀取、live_episode.py 寫入）。
- **執行期檢查**：`run_react_loop` 建好執行器、送出上游之前，`if not tool_executor.enforce_advertised:` 就記 `logger.error` 並 `raise ToukaException(ErrorCode.SYSTEM_ERROR)`（不用 `assert`，不帶字串，禁令 10）。用真假值判斷、不寫 `is not True`：16 個既有測試檔把執行器換成 MagicMock，屬性是真值，要照常跑。
- **為什麼不選 (c) 預設開**：要改 31 個測試檔共 64 處建構（h2 基準的 29 檔 60 處，加上 iso-c2 的 2 檔 4 處），會和 iso-c2 及其他同時進行的改動的測試衝突；正式程式的行為沒有任何差別。
- **為什麼不選 (b) 必須傳參數**：同樣要改 64 處；忘了傳參數會在建立時直接 TypeError 當掉，不是安全地擋下；而且仍然要 C2 才能確認 react_core 傳的是字面 `True`。
- **掃描範圍**：C2、C3 走訪 `ToukaProxy/`、`ToukaMemory/`、`ToukaHeartbeat/`、`ToukaDispatch/` 與 repo 根的 `scripts/` 底下全部 .py（含 `ToukaProxy/plugin_manager.py`、`ToukaProxy/scripts/`、`ToukaMemory/scripts/`），走訪時就剪掉 `tests`、`.venv`、`venv`、`node_modules`、`__pycache__`、`build`、`dist`、`.pytest_cache`、`.mypy_cache`、`.ruff_cache`、`.git`、`*.egg-info`，以及 `data`、`soul`（不讀實體資料）；用 `Path` 走訪，不呼叫 git 或子程序。C3 另外明列 `tests/tool_eval/live_episode.py`。另有一個測試釘住這幾個位置確實在範圍內。不掃：`ToukaChat/`、`ToukaIDE/`（含 `ToukaIDE/scripts/`）、`violations/` 與其餘 `tests/`，這些地方沒有引用執行器或清單欄位。
- **殘餘限制**：C2、C3 是靜態掃描，抓不到動態取得類別、模組或屬性的寫法，例如：
  - 在 react_core.py、debug.py 這兩個允許匯入的檔裡，用算出來的字串配 `getattr`／`setattr` 建立執行器或改開關；
  - 在任何檔用 `importlib.import_module`／`__import__` 加**算出來的**模組字串（例如 `"src.services." + "tool_executor"`）取得模組；
  - 先匯入上層套件，再用**算出來的**屬性名取得子模組（例如 `getattr(src.services, "tool_" + "executor")`），或從 `sys.modules` 用算出來的鍵取得；
  - `type(x)(...)`、`x.__class__(...)`；
  - 在建立點以外覆寫實例方法（例如 `_offer_denial`）：C2 抓不到，但會被 B14 這類行為測試抓到。
  匯入棘輪讓「在別的正式檔先匯入 `tool_executor` 模組、再用動態寫法建執行器」（例如 `getattr(te, "Tool" + "Executor")(...)`）也會轉紅。另一條規則擋住「經上層套件的屬性取得模組」：tool_executor.py 以外，出現屬性 `.tool_executor`（例如 `import src.services` 之後寫 `src.services.tool_executor`）或整串字串 `"tool_executor"`（例如 `getattr(src.services, "tool_executor")`）都轉紅。仍然抓不到的是：同一個允許檔裡的動態寫法，以及屬性名或模組名是算出來的寫法。執行期檢查只保護 `run_react_loop`，別處新建的執行器只靠 C2。
- **何時改用 (c)**：一旦需要第二個正式的建立點，就改用預設開，連同測試建構子一起改。

## 行為改變（原本能用、之後會被擋）

1. 模型叫這一輪沒列出的工具一律被擋，YOLO 也一樣；靠模型「硬叫」的流程會停下並顯示錯誤。
2. 舊名工具：代理只開了部分新工具時，用舊名會被擋，改用新名稱就好；工具全開的代理不受影響。
3. 動態掛載 MCP：`connect_mcp_server` 之後下一輪對話（背景執行是下一次脈衝）才能用，同一輪叫回 `not_advertised`；伺服器當下沒連上時，憑記憶叫也會被擋。
4. Gate 1 從來不列、今天模型卻叫得到的三類，改後一律 `not_advertised`，不呼叫上游、也不現場連線：(a) 使用者各自授權的 MCP（`auth_mode` 是 `oauth_user`、`oauth_client_credentials` 或 enterprise；ToolLayer 呼叫 `get_all_tools()` 沒帶 request context，這類安裝被跳過）；(b) 還沒連線的共享 MCP（啟動失敗、斷線、斷路器打開）；(c) SoulStudio 勾選的 MCP 工具與外掛（畫面存短名、Gate 1 比長名，所以靈魂設了工具清單時 MCP 工具一律被濾掉；外掛從不列）。
5. 外掛：模型完全叫不到；`/debug/execute` 仍可手動執行。外掛的角色權限照 ADR-0228 #7、ADR-0229 延後，不在本 ADR 補；之後要讓模型用外掛，必須「列給模型＋補角色權限」一起另開 ADR。
6. SNS（Discord 等）：模型叫記憶工具會被擋；外人的訊息不能再誘導模型讀記憶。
7. 無人在場叫 `ask_user` 立刻拒絕，不再空等 600 秒。
8. 根本不存在的名稱直接回 `resource_not_found`，不再先跳審批卡或叫審查員。
9. 第三方客戶端在請求裡帶了和伺服器端同名的 tools 時，模型看到的是客戶端版本，伺服器端那支算沒列出。第一方的 ToukaChat、ToukaIDE 聊天請求不帶 tools，不受影響。

## 已知限制

- **LOW-2（另開 T3）**：`record_tool_usage` 在所有拒絕之前就記錄（角色權限、PaC、審批的拒絕本來就這樣，不是本 ADR 造成的），記憶承諾守衛（`guard_service`）把被擋的 `memory_tools` 也算成用過：無痕或 SNS 裡模型說「記住了」不會跳提醒。因為實際沒有寫入，資料保護不受影響，只少一個使用者提示。正確修法要把「試過」（幻覺偵測需要）和「真的執行」（記憶守衛、查證守衛需要）分成兩個集合，會動到 `guard_service`、react_core 與四個 adapter 的 mid_loop_hook，超出本次範圍。〔已修：ADR-0281 T3。〕
- 行為改變第 4 點的三類要等 T1、T2 才能重新列給模型。〔(a)(b) 已由 ADR-0281 T1 處理；(c) 等 T2。〕
- C2、C3 不聲稱能抓任意動態 Python（見上一節殘餘限制）。
- 呼叫 MCP 工具當下，manager 另外依伺服器當下的清單與隔離紀錄核對（dep1，見 ADR-0203 的 dep1 補記）：所有入口都適用、`/debug/execute` 也擋；和本 ADR 不重疊，分工見該節。
- 伺服器名本身含 `__` 時，誤判只落在錯誤類別（`resource_not_found` 或 `not_advertised`），不會放行。
- 合併 iso-c2（隔離執行環境）時，本 ADR 的核對必須排在 iso-c2 的 `policy_level` 之前，被擋的呼叫不得寫同意紀錄；屆時補測試 D1（三種被擋的呼叫都不呼叫 `policy_level` 與 `consent_ledger.record_policy`）與突變 M25（把核對移到 `policy_level` 之後要轉紅）。（已完成：iso-c2 整合 9 補上 D1 與 M25，見 ADR-0272 驗證段。）
- 附帶發現、本 ADR 不修：無痕對話仍列出 `read(scope=legacy)`〔ADR-0274 已修：無痕不列、硬叫也擋，含讀寫家族與 `vault_tools` shim〕；ToukaUI 的 `openDiff`（不帶伺服器前綴）照舊回 `resource_not_found`（測試只釘現況）；ModelStudio 的工具選單是空的。
- 【推測】既有本機設定若寫了舊名、短名或外掛，更新後叫那個工具會照實回 `not_advertised`。

## 另開任務

- **T1**：ToolLayer 帶精確的 request context，列出使用者各自授權的 MCP 與還沒連線的共享 MCP。〔已完成：見 [ADR-0281](0281-tool-list-follow-ups.md) T1；還沒連線的共享 MCP 依維護者決定只在說明區塊列出名字。〕
- **T2**：修 `enabled_tools` 的短名與長名比對（SoulStudio 存短名），並決定外掛選項的語意（要讓模型用外掛時和角色權限一起另開 ADR）。在 T2 修好前，建立新靈魂時工具清單留空（等於全部可用），不要只勾幾個 MCP 工具。
- **T3**：記憶守衛與查證守衛分開「試過」和「真的執行」（LOW-2）。〔已完成：見 [ADR-0281](0281-tool-list-follow-ups.md) T3。〕
- **終端配套工具（候選）**：`execute_command` 走非同步流程時，會叫模型之後呼叫 `get_command_result(execution_id=…)` 取結果（`tools/terminal.py` 的 `_pending_payload`）。靈魂的 `enabled_tools` 只勾 `execute_command` 時，取結果那一步回 `not_advertised`，所以兩個要一起勾（`stop_command` 同理）。後續可讓 ToolLayer 列出 `execute_command` 時自動帶上配套工具，或讓 SoulStudio 成組勾選；另開任務，不在本 ADR 做。

## 和設計的小差異

設計以 h2 設計第 2 版加第 3 版改動段落為準（第 3 版經審查通過；設計文件未入版控）。實作時：

- `permits_native` 遇到空的後繼集合不放行（fail-closed）。
- `from_schemas` 略過不是 dict 的條目。
- `_offer_denial` 的參數名縮成 `handler`／`server`／`tool`。
- C2 比規格嚴：react_core 以外讀 `.enforce_advertised` 也算違規；不是建立呼叫卻用 `enforce_advertised=` 也算違規；react_core、debug.py 各只能有一個不改名的 import 加一個建立呼叫。「各只能一個建立呼叫」最初用集合收集、會合併字面值相同的重複建立點，最終審查指出後改成逐一比對（不去重），現在成立。
- 最終審查之後另外補上（只改測試，產品 0 行）：沒列出又沒授權的原生工具回 `native_tool_rbac` 的順序釘子；`tool_executor` 模組的匯入棘輪；掃描範圍從各子專案的 `src` 擴大到上面「掃描範圍」所列。
- C3 只算欄位名本身的用法，import 的模組路徑不算；白名單檔各「恰好一處、指定用法」。
- Guard #40（`scripts/guard_harness_context.py`）的行為標記隨 `_SUBAGENT_STRIPPED_TOOLS` 一起更新（經維護者同意）：「cannot dispatch further sub-agents」這句承諾原本對到 `subagent_tools.py` 的 `== "dispatch_subagent"`，子代理剝除改用集合後，標記改成整行 `_SUBAGENT_STRIPPED_TOOLS = frozenset({"dispatch_subagent", "dispatch_subagent_group"})`，集合裡少了單一子代理或子代理群組任一個都會轉紅。指南文字不變。
- 測試檔名：工作說明原訂 `tests/unit/test_advertised_tools_security.py`；`scripts/lint_ssot.py` 用「完整路徑含 `tools` 子字串」判斷業務層，把這個測試檔當成業務層，結構測試讀原始碼的 `read_text` 因此被判 IO_PRISON_VIOLATION。改用 `git mv` 換成 `test_advertised_tool_list_security.py`，內容不變、守衛不改（守衛的子字串判斷是另一個問題，另列候選）。
- 設計 §13 只推測 `test_progress_emit`、`test_budget_cap` 可能受影響，漏了 `tests/integration/test_tool_loop_budget.py` 的 `test_interactive_multi_tool_rounds_unlimited_by_default`、`test_bounded_run_wraps_up_gracefully`：模型叫舊名 `vault_tools`，測試把授權 patch 成只有 `vault_tools`，7 個檔案動詞後繼都沒列出 → `not_advertised`。兩個測試只把授權補成 `vault_tools`＋7 個檔案動詞，斷言不動（測試要驗的是預算與多輪行為；舊名仍走 shim，只是新規則要求後繼動詞都在這一輪的清單上）。審查裁定這不算改既有測試的判定。
- 產品程式 +180／−11（設計估約 +130；`tool_executor.py` 照檔案原有格式寫拒絕事件與唯讀屬性說明，功能範圍沒變），在核准的 +190 內。

## 驗證

本機 Windows、3.12 venv、fnm Node 24.21.0；全部走 conftest 沙盒與 mock，不讀 `data/`、不連外、不打付費 API，也沒有啟動 Touka。

- 新測試 `tests/unit/test_advertised_tool_list_security.py` 159 個、59 個測試函式（`--collect-only`）：清單資料結構與型別 9、舊名對應表 C7 4、ToolLayer A1～A10 12、子代理 C4／心跳 C5／群聊 C8／備援 C9 各 1、執行器 B 組 67（B7 參數化 32、B8 5、B11 7，含順序釘子 B9b）、接線 C1／C1b／C1c／B14／`/debug/execute` C6／live_episode 各 1、C2 43（正式掃描 1、掃描範圍釘子 1、掃描器自我測試 41，其中匯入棘輪與套件屬性 16）、C3 14（含 13 個自我測試）。每一項先紅後綠；順序釘子在現行程式本來就綠，紅燈由突變 M-B1 證明。
- 突變 M1～M24、M26～M31 全殺（M25 屬 iso-c2 合併時的 D 組，已由 ADR-0272 補上並轉紅）。審查提出的突變：M-B1（原生角色權限搬到清單核對之後）、M-B3（新正式檔用 `getattr(te, "Tool" + "Executor")` 建執行器）、M-B5（`debug_execute_tool` 多建一個 `False` 的執行器），與再審提出的 M-B6（新正式檔 `import src.services` 再 `getattr(src.services.tool_executor, "Tool" + "Executor")`），補測試後全殺；另在 `ToukaProxy/scripts/`、`ToukaMemory/scripts/`、`ToukaProxy/` 放探針檔，擴大掃描後 C2／C3 都轉紅。
- 全量（固定順序）：實作 HEAD `eb97f280` 9779 passed（＝基準 9641＋138）／7 skipped／6 failed；merge dev `583e1c7f` 後 9983 passed（＝dev 的 9845＋138）／7 skipped／6 failed；審查後補測試、改號再 merge dev `585a42b2`（含 e1b）後 10076 passed（＝dev 的 9921＋155）／7 skipped／6 failed；再審補 C2 規則後 10080 passed（＝9921＋159）／7 skipped／6 failed。6 個都是 E1 的 `test_local_compose_bindings`（本機 Docker Desktop，以 CI 為準），7 個跳過與 ADR-0251 的基準相同；`test_tool_loop_budget.py` 那 2 個是綠的。`lint_ssot.py`、`guard_noqa_registry.py`、`guard_adr_integrity.py`（0001..0269 連續）、`guard_native_tool_contracts.py`（基準不改）、`guard_endpoint_auth.py`、`guard_ruff_banned_api.py`、`guard_module_boundaries.py` 通過。
- 不變：openapi、NOQA_REGISTRY、Guard #45 基準、characterization golden。

## 回復

revert 這個 PR 即可。不寫任何資料，撤回後沒有要清的東西；SECURITY.md 那一列、ADR-0119 與 ADR-0195 的註記要一併還原。撤回後背景第 2 點的缺口全部回來（無痕讀得到記憶、YOLO 下叫得到沒列出的終端、外掛直接執行），子代理又能派子代理群組。
