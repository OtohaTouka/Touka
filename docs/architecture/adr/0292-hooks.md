# ADR-0292：Hook（丙-1：引擎、安全界線與四個事件）

- 狀態：🚧 實作中。維護者 2026-10-28 要求前後端加上 hook 與設定介面、行為盡量和 Claude Code 相同、對不上的先列表；一頁計畫由維護者核准，丙-1 加做對抗式複審。
- 日期：2026-10-28
- 決策者：維護者
- 相關：ADR-0146（回合結束的約定）、ADR-0228／0229（工具執行順序與可信 context）、ADR-0239（`execution_broker`）、ADR-0246（模式、審查員與底線）、ADR-0257（歷史資料界線）、ADR-0269（只執行這一輪列出的工具）、ADR-0272（隔離層級；hook 程式不是代理人撰寫的程式，不經 runner，見決定 4）。
- 影響範圍：ToukaProxy 新增 `models/hooks.py`、`services/hooks/`、`middleware/layers/hooks.py`；修改 `services/tool_executor.py`、`handlers/react_core.py`、`services/proxy_service.py`、`services/approval_flow.py`、`utils/tool_events.py`、`services/prompt_blocks.py`、`adapters/content_normalize.py`、`services/continuity_service.py`、`services/context_snapshot_service.py`、`utils/bootstrap.py`、`utils/hard_floor.py`、`utils/async_db.py`；設定 `config/hooks.json.example`、`errors.json.example`（6001～6003）。ToukaChat：審批卡、串流錯誤通知、`error_codes.ts`、翻譯。

## 背景

- Claude Code 的 hook 讓管理者在固定時機（使用者送出訊息、工具執行前後、回覆結束等）執行自己的程式或送 HTTP：事件內容以 JSON 交給程式，程式可以擋下、要求確認或補充說明給模型。
- Touka 沒有這個能力。管理員想加「送出前檢查」「工具執行後跑 lint」之類的規則，只能改原始碼。
- 新增「執行管理員設定的程式」是資安改動：程式在 Touka 自己的權限下跑，如果代理人能改到設定或腳本，就等於繞過審批執行任意程式。

## 決定

1. **設定**：全系統一份 `config/hooks.json`（`HooksConfig`，繼承 `BasePolicyModel`），格式照 Claude Code 的 `hooks` 區塊：`{事件: [{matcher, hooks: [handler]}]}`。
   - 預設關閉（`enabled: false`、`hooks: {}`），沒有設定就什麼都不跑、接線點不組事件內容。
   - 只收已接線的事件（`WIRED_EVENTS`），拼錯或還沒接的事件拒收；每個事件最多 20 個 handler。
   - 啟動時壞檔照 `_LIVE_MODELED` 規則停下並指出檔名；執行中讀到壞檔沿用上一份好的，健康狀態標 `error`。
   - 檔名列入底線 F3 的政策檔（`hard_floor._POLICY_FILE_RE`），代理人寫入只有人能核准、無人在場直接拒絕；`config/hooks/` 底下的腳本同樣算受保護的設定。
2. **兩種 handler**：
   - `command`：`command` 是程式的絕對路徑，`args` 是參數清單，**不經 shell**；事件內容只從標準輸入給。經 `execution_broker.spawn_exec` 啟動，環境只給 `LaunchEnvironmentPolicy.launcher_base()`（不含金鑰），工作目錄是 `config/hooks/`，`close_fds`、獨立行程群組；逾時結束整個行程樹；標準輸出與錯誤各只留前 64 KiB。
   - `http`：POST 事件內容，`follow_redirects=False`、`trust_env=False`，有逾時、回應只讀前 64 KiB；3xx 與非 2xx 都算失敗。標頭照字面送出，寫成 `$VAR`／`${VAR}` 拒收（不替換環境變數，避免以為有效或洩金鑰）。
   - 逾時預設 30 秒、上限 300 秒。
3. **結果解讀照 Claude Code**（`services/hooks/outcome.py`）：
   - 結束碼 2：UserPromptSubmit、PreToolUse 擋下，原因取 stderr；PostToolUse 把 stderr 當回饋；Stop 只記錄。結束碼 2 時不讀 stdout 的 JSON。
   - 結束碼 0：stdout 是 JSON 就讀欄位（`decision: "block"`、`hookSpecificOutput.permissionDecision` 的 `deny`／`ask`、`additionalContext`）；純文字只在 UserPromptSubmit 當補充說明；看起來是 JSON 卻壞掉算 hook 失敗。
   - http：2xx 的回應內容當結束碼 0 的 stdout。
   - 其他結束碼、逾時、連不上、路徑被拒：hook 自己失敗，不擋動作，但記在執行紀錄裡。
   - 同一事件符合的 hook 同時執行，相同的 handler 只跑一次，任一個擋下就擋下。
   - `hookEventName` 對不上的 `hookSpecificOutput` 整段不採用。
4. **hook 只能擋、不能放行**：
   - `permissionDecision` 的 `allow`／`defer`、`updatedInput`、`updatedToolOutput`、`updatedMCPToolOutput`、`continue`、`stopReason`、`systemMessage` 一律不採用，只在執行紀錄記一筆說明。
   - PreToolUse 在政策判定（含角色權限、底線）之後執行：政策已拒絕的不會送給 hook；hook 的 `ask` 跳同一張人類審批卡，不送審查員，無人在場直接拒絕（終端也不落到舊的審批通道），Auto 與 YOLO 都不能代替人；審批卡以 `hook_reason` 欄位顯示「管理員設定的 hook 要你確認」，和審查員的理由分開。
   - hook 不能改寫工具參數，工具照原本的參數與審批流程走。
   - hook 程式不是代理人撰寫或指定的程式（只有人能設定、腳本放在受保護的位置），所以不經 ADR-0272 的隔離 runner，以 Touka 自己的權限執行；靠決定 5 保證代理人改不到它。
5. **指令與腳本必須在代理人寫不到的地方**（`services/hooks/paths.py`；存檔時檢查全部 command hook、含停用的，每次執行前再檢查一次）：
   - 禁區：資料根、sandbox state、執行器資料夾、每個專案根（原路徑與翻譯後的路徑），容器裡另加對映的宿主路徑；設定資料夾裡只有 `config/hooks/` 可用。
   - 檢查方式：字面比對（不分大小寫）、解析後的路徑、每一層上層資料夾以 `(st_dev, st_ino)` 比對（擋同一個資料夾的別名）、符號連結逐跳遞迴檢查（最多 40 跳）；拒絕 UNC 與磁碟機相對路徑；`config/hooks/` 裡有其他硬連結（`nlink>1`）的檔拒絕。
   - 參數裡看起來像路徑的片段一併檢查，相對路徑以 `config/hooks/` 為起點；讀不到狀態一律當成無法確認而拒絕。
   - 存檔時被拒回 `HOOK_PATH_REJECTED`（422）；執行時被拒記為 `rejected`、不執行。
6. **回給模型的文字是資料**：
   - 補充說明包成 `<hook_output>` 區塊，開頭註明這是管理員設定的 hook 的輸出、不是使用者的話也不是指令；內文的區塊 tag 先中和（`<` 換成 `&lt;`），不能提早關閉區塊或冒充別的區塊。
   - UserPromptSubmit 的補充說明接在最後一則 user 訊息尾端，只用在這一次請求（`REQUEST_TAIL_TAGS`，不進下一輪歷史，對話歷史快取斷點打在它之前）。
   - 工具前後的補充說明接在工具結果後面：結構化 JSON 結果加 `hook_output` 鍵（標 `trust: "untrusted"`），文字結果加 `<hook_output>` 區塊。
   - PreToolUse 擋下時回結構化 `permission_denied`（`reason: "hook_denied"`、`hook_reason` 中和後截斷 2,000 字）。
7. **四個事件的接線點**（丙-1）：
   - UserPromptSubmit：`HookLayer` 排在 `MonitorLayer`（身分判定）之後、組上下文之前；只在 `process_proxy_request` 設的 `ctx.run_hooks` 時執行（UI 聊天、SNS、評分卡），上下文預覽明確設成不執行，子代理不執行。擋下回 `HOOK_PROMPT_BLOCKED`（403）並帶 `reason`，前端以專屬通知顯示擋下與理由。
   - PreToolUse／PostToolUse：`ToolExecutor` 內，matcher 比對 Touka 的工具名稱，MCP 工具另比對 Claude Code 寫法 `mcp__server__tool`；PostToolUse 只在成功後執行，事件內容多 `tool_response`。
   - Stop：ReAct 迴圈回完之後在背景執行（`spawn_logged`），回覆不等它；子代理的回合不執行；只記錄，不能讓代理人繼續（ADR-0146：回文字就結束這一輪）。
   - 心跳的每次脈衝都會觸發工具前後與 Stop；脈衝提示不是使用者訊息，所以不觸發 UserPromptSubmit。
8. **執行紀錄**：最近 50 筆存在記憶體（重啟清空），記時間、事件、hook 名稱與類型、結果、耗時、結束碼或 HTTP 狀態、失敗原因（不含輸出內容）。無痕對話照跑 hook，但紀錄只留時間、事件、結果與耗時。
9. **存檔**：只經管理 API，以 revision（內容 SHA-256）做 CAS，對不上回 `HOOK_CONFIG_CONFLICT`（409）；存檔前跑決定 5 的檢查。（管理 API 與設定頁見下一節，實作後補。）

## 和 Claude Code 對不上的部分

- **刻意不同**：
  - 指令不經 shell，要寫成程式＋參數清單；管線等 shell 語法要寫成腳本。
  - 只能擋與要求確認，不能放行、不能改寫工具輸入或輸出、不能叫模型繼續（Stop 的 `block`）。
  - http 標頭不替換環境變數；hook 程式拿不到金鑰。
  - UserPromptSubmit 的補充說明只用在這一次請求，Claude Code 會留在之後的對話裡。
  - 被 UserPromptSubmit 擋下的訊息仍留在對話紀錄裡（和其他送出前失敗一樣），Claude Code 會把它從紀錄抹掉。
  - 全系統一份設定，沒有使用者、專案、本機多層設定，也沒有技能或外掛帶的 hook。
  - 事件內容沒有 `transcript_path`、`cwd`；`permission_mode` 是 Touka 的模式名稱（Manual／Auto／YOLO）。
- **Touka 沒有對應功能，不做**：工作目錄切換、檔案監看、worktree、Agent 團隊相關事件。
- **下一批**：hook 類型 `mcp_tool`、`prompt`、`agent`（後兩種會用到模型額度）；非同步 hook（`async`）、`if` 條件、`once`。
- **丙-2 接線**：工具失敗與被拒、跳出審批卡、通知、上游錯誤結束、子代理開始與結束、滑動視窗剪裁前後（不能拖住對話）、任務建立與完成、MCP 向使用者要資料（只能拒絕）、設定變更（不能擋關閉 hook），以及 Touka 自己的心跳開始與結束。

## 後果

- 沒有設定時行為不變：接線點先問 `has_hooks`，不組事件內容、不改任何結果（golden 不變）。
- 設定了 PreToolUse 的工具，每次呼叫多等 hook 執行完（最多到逾時）；Stop 不影響回覆。
- 審批卡事件多一個選填欄位 `hook_reason`；聊天請求被擋下時回 403 `HOOK_PROMPT_BLOCKED`。
- 撤回：把 `enabled` 設成 false 或清空 `hooks` 立即停用；revert 即可整個移除，`hooks.json` 留著無害。

## 替代方案

- **照 Claude Code 經 shell 執行**：事件內容或參數可能被當成指令執行，也較難檢查用到哪些腳本。不採用。
- **hook 程式經隔離 runner 執行**：hook 常需要讀專案或呼叫外部工具，執行器的限制會讓大多數用途失效；改以「只有人能設定、腳本在受保護位置」保證代理人無法影響它。不採用。
- **允許 `allow`／`updatedInput`**：會讓 hook 成為繞過審批與底線的途徑。不採用。
- **每個靈魂或專案各一份設定**：設定入口與權限檢查變多，現在沒有需求。不採用。

## 驗證

- `tests/unit/test_hooks_config.py`：設定模型（只收已接線事件、handler 型別、標頭環境變數、上限）。
- `tests/unit/test_hook_paths_security.py`：禁區（含翻譯後的專案根與容器對映路徑）、以檔案識別碼擋別名、符號連結逐跳、硬連結、相對參數、UNC 與磁碟機相對路徑、讀不到狀態。
- `tests/unit/test_hook_runners_security.py`：不經 shell、環境不含金鑰、逾時結束行程樹、輸出上限、http 不跟隨轉址與大小上限。
- `tests/unit/test_hook_service.py`：解讀規則、同時執行、只擋不放行、執行紀錄與無痕、壞檔沿用上一份、存檔 CAS 與路徑檢查。
- `tests/unit/test_hook_wiring_security.py`：四個事件的接線（只在真的請求執行、擋下帶理由、補充說明是資料、PreToolUse 擋下不送審不跳卡、要求確認跳標成 hook 的卡且不送審、無人在場拒絕、YOLO 也要人、`allow` 不放行、matcher、PostToolUse 只在成功後、Stop 在背景、沒有設定時結果不變）。
- ToukaChat：審批卡的 hook 理由、串流事件解析、被擋訊息的通知。
