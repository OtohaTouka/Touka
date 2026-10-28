# ADR-0292：Hook（丙-1：引擎、安全界線、四個事件與設定頁）

- 狀態：✅ 已實作（丙-1；其餘事件在丙-2）。維護者 2026-10-28 要求前後端加上 hook 與設定介面、行為盡量和 Claude Code 相同、對不上的先列表；一頁計畫由維護者核准，丙-1 加做對抗式複審。
- 日期：2026-10-28
- 決策者：維護者
- 相關：ADR-0146（回合結束的約定）、ADR-0228／0229（工具執行順序與可信 context）、ADR-0239（`execution_broker`）、ADR-0246（模式、審查員與底線）、ADR-0257（歷史資料界線）、ADR-0269（只執行這一輪列出的工具）、ADR-0272（隔離層級；hook 程式不是代理人撰寫的程式，不經 runner，見決定 4）。
- 影響範圍：ToukaProxy 新增 `models/hooks.py`、`services/hooks/`、`middleware/layers/hooks.py`；修改 `services/tool_executor.py`、`handlers/react_core.py`、`services/proxy_service.py`、`services/approval_flow.py`、`utils/tool_events.py`、`services/prompt_blocks.py`、`adapters/content_normalize.py`、`services/continuity_service.py`、`services/context_snapshot_service.py`、`utils/bootstrap.py`、`utils/hard_floor.py`、`utils/async_db.py`、`services/group_router.py`、`tools/project_tools.py`、`routers/ui_sessions.py`、`services/isolation/profiles.py`、`services/resource_manager.py`、`.gitignore`、`.dockerignore`；新增 `routers/hooks.py`（掛在 `main.py` 的 `CORE_ROUTERS`）；設定 `config/hooks.json.example`、`errors.json.example`（6001～6003）；`openapi.json`。ToukaChat：設定畫面「Hooks」分頁（`components/settings/tabs/HooksTab.tsx`、`tabs/hooks/`、`hooks/settings/useHooksSettings.ts`、設定註冊表與搜尋索引）、審批卡、串流錯誤通知、被擋訊息的標記（`lib/tree_history.ts`、`lib/stream_tree.ts`、`lib/chat/buildApiMessage.ts`、`lib/providers/StreamRegistryProvider.tsx`、`components/chat/MessageItem.tsx`）、`error_codes.ts`、`api.ts`、`lib/types.ts`、翻譯。

## 背景

- Claude Code 的 hook 讓管理者在固定時機（使用者送出訊息、工具執行前後、回覆結束等）執行自己的程式或送 HTTP：事件內容以 JSON 交給程式，程式可以擋下、要求確認或補充說明給模型。
- Touka 沒有這個能力。管理員想加「送出前檢查」「工具執行後跑 lint」之類的規則，只能改原始碼。
- 新增「執行管理員設定的程式」是資安改動：程式在 Touka 自己的權限下跑，如果代理人能改到設定或腳本，就等於繞過審批執行任意程式。

## 決定

1. **設定**：全系統一份 `config/hooks.json`（`HooksConfig`，繼承 `BasePolicyModel`），格式照 Claude Code 的 `hooks` 區塊：`{事件: [{matcher, hooks: [handler]}]}`。
   - 預設關閉（`enabled: false`、`hooks: {}`），沒有設定就什麼都不跑、接線點不組事件內容。
   - 只收已接線的事件（`WIRED_EVENTS`），拼錯或還沒接的事件拒收；每個事件最多 20 個 handler。
   - 啟動時壞檔照 `_LIVE_MODELED` 規則停下並指出檔名；執行中讀到壞檔、讀不到或檔案被刪掉，都沿用上一份好的，健康狀態標 `error`（從來沒讀成功過才當成沒有設定）。
   - `hooks.json` 列入底線 F2 的金鑰檔（`hard_floor._SECRET_FILES`）：http hook 的標頭照字面存、可能有權杖，代理人讀和寫都只有人能核准、無人在場直接拒絕。`hooks.json.example` 與 `config/hooks/` 底下的腳本屬 F3 的政策與設定。
   - `config/hooks/` 只放管理員自己的腳本，不進版控也不進映像（`.gitignore`、`.dockerignore`）。
2. **兩種 handler**：
   - `command`：`command` 是程式的絕對路徑，`args` 是參數清單，**不經 shell**；事件內容只從標準輸入給。經 `execution_broker.spawn_exec` 啟動，環境只給 `LaunchEnvironmentPolicy.launcher_base()`（不含金鑰），工作目錄是 `config/hooks/`，`close_fds`、獨立行程群組；逾時結束整個行程樹；標準輸出與錯誤各只留前 64 KiB。
   - `http`：POST 事件內容，`follow_redirects=False`、`trust_env=False`；逾時是整次請求（含讀回應）的上限，回應讀到 64 KiB 就停止讀取並關閉連線；3xx 與非 2xx 都算失敗。標頭照字面送出，寫成 `$VAR`／`${VAR}` 拒收（不替換環境變數，避免以為有效或洩金鑰）。
   - 逾時預設 30 秒、上限 300 秒。
3. **結果解讀照 Claude Code**（`services/hooks/outcome.py`）：
   - 結束碼 2：UserPromptSubmit、PreToolUse 擋下，原因取 stderr；PostToolUse 把 stderr 當回饋；Stop 只記錄。結束碼 2 時不讀 stdout 的 JSON。
   - 結束碼 0：stdout 是 JSON 就讀欄位（`decision: "block"`、`hookSpecificOutput.permissionDecision` 的 `deny`／`ask`、`additionalContext`）；純文字只在 UserPromptSubmit 當補充說明；看起來是 JSON 卻壞掉算 hook 失敗。
   - `continue: false`：UserPromptSubmit、PreToolUse 擋下，原因取 `stopReason`；PostToolUse 與 Stop 不採用（工具已經跑完、回合已經結束），記一筆說明。
   - http：2xx 的回應內容當結束碼 0 的 stdout。
   - 其他結束碼、逾時、連不上、路徑被拒：hook 自己失敗，不擋動作，但記在執行紀錄裡。
   - 同一事件符合的 hook 同時執行，相同的 handler 只跑一次，任一個擋下就擋下。
   - `hookEventName` 對不上的 `hookSpecificOutput` 整段不採用。
4. **hook 只能擋、不能放行**：
   - `permissionDecision` 的 `allow`／`defer`、`updatedInput`、`updatedToolOutput`、`updatedMCPToolOutput`、`systemMessage` 一律不採用，只在執行紀錄記一筆說明；`continue: false` 只在能擋的事件當成擋下（決定 3），不能讓代理人停下或繼續。
   - PreToolUse 在政策判定（含角色權限、底線）之後執行：政策已拒絕的不會送給 hook；hook 的 `ask` 跳同一張人類審批卡，不送審查員，無人在場直接拒絕（終端也不落到舊的審批通道），Auto 與 YOLO 都不能代替人；審批卡以 `hook_reason` 欄位顯示「管理員設定的 hook 要你確認」，和審查員的理由分開。
   - hook 不能改寫工具參數，工具照原本的參數與審批流程走。
   - hook 程式不是代理人撰寫或指定的程式（只有人能設定、腳本放在受保護的位置），所以不經 ADR-0272 的隔離 runner，以 Touka 自己的權限執行；靠決定 5 保證代理人改不到它。
5. **指令與腳本必須在代理人寫不到的地方**（`services/hooks/paths.py`；存檔時檢查全部 command hook、含停用的，每次執行前再檢查一次）：
   - 參數：拆成字詞逐一檢查（`--script=x`、`-c "… /path …"` 也算），看起來像路徑的字詞都必須在 `config/hooks/` 裡（相對路徑以 `config/hooks/` 為起點），在外面回 `outside_hooks_dir`。這個判斷只看 `config/hooks/` 本身，不看專案清單一類會變動的資料。
   - 指令在 `config/hooks/` 裡時照參數的規則：字面與解析後都在裡面；`config/hooks/` 本身與底下經過的每一層都不能是連結（symlink、junction），否則回 `linked_path`；檔案不能有其他硬連結（`nlink>1`，回 `hard_link`），另一個名字可能在代理人的資料夾裡。
   - 指令在別處（例如系統的直譯器）時不能落在禁區：資料根、sandbox state、執行器資料夾、工作區根（使用者能在底下自己登記專案）、每個專案根（原路徑與翻譯後的路徑）、MCP 伺服器的可寫資料夾，容器裡另加鏡像掛載根；設定資料夾裡 `config/hooks/` 以外的地方也不行（`skills`、`mcp_servers`、`evals` 由創建與演化寫入）。讀不到 MCP 隔離設定檔時當成無法確認，不執行。
   - 檢查方式：字面比對（不分大小寫）、解析後的路徑、每一層上層資料夾以 `(st_dev, st_ino)` 比對（抓大小寫、8.3 短名與 bind mount 別名）、路徑上每一個連結的位置與它直接指向的地方都查（最多 40 跳）；`..` 一律拒收，拒絕 UNC 與磁碟機相對路徑；指令必須是存在的絕對路徑一般檔；讀不到狀態一律當成無法確認而拒絕。
   - 存檔時被拒回 `HOOK_PATH_REJECTED`（422）；執行時被拒記為 `rejected`、不執行。
   - hook 程式執行後自己再去讀寫哪裡，Touka 管不到；這裡只擋設定裡寫得出來的路徑。
6. **回給模型的文字是資料**：
   - 補充說明包成 `<hook_output>` 區塊，開頭註明這是管理員設定的 hook 的輸出、不是使用者的話也不是指令；內文的區塊 tag 先中和（`<` 換成 `&lt;`），不能提早關閉區塊或冒充別的區塊。
   - UserPromptSubmit 的補充說明接在最後一則 user 訊息尾端，只用在這一次請求（`REQUEST_TAIL_TAGS`，不進下一輪歷史，對話歷史快取斷點打在它之前）。
   - 工具前後的補充說明接在工具結果後面：結構化 JSON 結果加 `hook_output` 鍵（標 `trust: "untrusted"`），文字結果加 `<hook_output>` 區塊。
   - PreToolUse 擋下時回結構化 `permission_denied`（`reason: "hook_denied"`、`hook_reason` 中和後截斷 2,000 字）。
7. **四個事件的接線點**（丙-1）：
   - UserPromptSubmit：`HookLayer` 排在 `MonitorLayer`（身分判定）之後、組上下文之前；只在 `process_proxy_request` 設的 `ctx.run_hooks` 時執行（UI 聊天、SNS、評分卡），上下文預覽明確設成不執行，子代理不執行，只有工具結果的 user 回合不算使用者送出的訊息。這時模式還沒決定（`ToolLayer` 才決定），事件內容不帶 `permission_mode`。擋下回 `HOOK_PROMPT_BLOCKED`（403）並帶 `reason`，前端以專屬通知顯示擋下與理由。
   - PreToolUse／PostToolUse：`ToolExecutor` 內，matcher 比對 Touka 的工具名稱，MCP 工具另比對 Claude Code 寫法 `mcp__server__tool`；PostToolUse 只在成功後執行，事件內容多 `tool_response`。
   - Stop：ReAct 迴圈回完之後在背景執行（`spawn_logged`），回覆不等它；子代理的回合不執行；只記錄，不能讓代理人繼續（ADR-0146：回文字就結束這一輪）。
   - 心跳的每次脈衝都會觸發工具前後與 Stop；脈衝提示不是使用者訊息，所以不觸發 UserPromptSubmit。
   - 每個事件都有 `session_id`、`agent_id` 與 Touka 多的 `incognito`（hook 自己決定無痕對話的內容要不要留）。
8. **執行紀錄**：最近 50 筆存在記憶體（重啟清空），記時間、事件、hook 名稱與類型、結果、耗時、結束碼或 HTTP 狀態與說明。說明是擋下或要求確認的理由、失敗原因（例外只記類型、stderr 只取第一行）與不採用的欄位，有長度上限，不含 stdout 的內容。無痕對話照跑 hook，但紀錄只留時間、事件、結果與耗時。
9. **存檔**：只經管理 API，以 revision（內容 SHA-256）做 CAS，對不上回 `HOOK_CONFIG_CONFLICT`（409）；存檔前跑決定 5 的檢查。
10. **管理 API**（`routers/hooks.py`，`/hooks` 與 `/v1/hooks`）：
    - 三個端點都只給人類管理員（`require_admin_human`；admin 代打、靈魂、DebugAgent 都不行）。
    - `GET /hooks`：目前生效的設定（壞檔時是上一份好的）、`health`（`ok`／`missing`／`error`、目前檔案的 revision、錯誤類型）、已接線的事件與看工具名稱的事件。
    - `PUT /hooks`：送 `{config, revision}`，revision 必填（沒有檔案時明確送 `null`），多的欄位拒收；先查 CSRF Origin；`author` 與 `updated_at` 由伺服器填，用戶端送的不採用；回新的 revision。
    - `GET /hooks/runs`：決定 8 的執行紀錄，新的在前。
    - `hooks.json` 不在設定匯出／匯入（`settings_snapshot_service.SECTIONS`）裡：匯入不經存檔的路徑檢查。
11. **設定頁**：設定畫面「系統」組的「Hooks」分頁（`adminOnly`，不加左側欄入口）。
    - 依伺服器回的事件逐區顯示；比對規則只在看工具名稱的事件出現；每組可加 command 或 http hook。
    - command 填程式的絕對路徑、參數一行一個（原樣交給程式）；http 填網址、標頭一行一個「名稱: 值」。
    - 存檔被拒時列出原因：路徑問題逐項列事件、位置、原因與值；衝突提示重新載入；欄位不合規則顯示第一個錯誤的位置。設定檔壞掉時提醒目前用的是上一份好的設定、存檔會取代它。
    - 下方列最近的執行紀錄，無痕的只顯示時間、事件與結果。
    - 說明寫明 hook 失敗時不擋動作，不能當成唯一的安全防線；腳本與參數裡的路徑要放在 `config/hooks/`，程式要放在 `config/hooks/` 或代理人寫不到的地方。
12. **一般使用者的專案根不能放在 Touka 保護的位置**（`services/isolation/profiles.project_root_reason`）：決定 5 把專案根當禁區，但專案是使用者自己登記的；專案根若是設定資料夾、`config/hooks/` 或它們的上層，代理人就能經專案檔案工具寫進 hook 腳本。
    - 判斷沿用 ADR-0272 的永不授權規則：受保護的位置與它們的上層、經連結指到受保護位置的一律拒絕；容器裡工作區掛載根本身也不能當專案根，另查 Touka 目錄（只有使用者自建的頂層資料夾能當專案）。本機安裝只用固定規則，不往上找 Touka 目錄（安裝根就是 Touka 目錄，往上找會擋掉所有專案）。
    - 三處檢查：登記與更新專案（`ui_sessions._project_root_allowed`，回 422）、列專案檔案與每次專案檔案工具呼叫（`project_tools`，回錯誤、不執行）。
    - 管理員與 Debugger 不受這條限制（他們本來就能指定任何位置，`resource_manager.bypasses_project_jail`）；這是已知的界線：管理員登記在受保護位置的專案，代理人以管理員身分使用專案工具時仍寫得到，由管理員自己負責不這樣登記。
    - 行為變更：一般使用者的專案不能在受保護的資料夾裡，也不能是它們的上層（例如工作區根就是 Touka 安裝根時，工作區根本身不能當專案）；既有的這類登記留著，但專案工具會拒絕。
13. **群聊**：hook 補充說明裡的 `@別名` 不算點名（`group_router` 找點名前先拿掉 `<hook_output>` 區塊），hook 的輸出不能叫其他靈魂發言。
14. **被擋下的訊息不再送給模型**：前端把被 UserPromptSubmit 擋下的 user 訊息標成 `hookBlocked`（存在對話樹裡，後端照 `extra: allow` 保留），訊息下方註明它被 hook 擋下、不會送給模型；下一輪組歷史時略過它與它底下的分支。自己組歷史的用戶端（IDE、自訂 client）不在這個範圍，被擋下的訊息若被它們放進歷史，仍會送到模型。
15. **失敗不擋（fail-open）**：hook 自己失敗（結束碼、逾時、連不上、路徑被拒、輸出壞掉）和 hook 服務本身出錯都不擋動作，記在執行紀錄與 log；例外內容不回給模型或用戶端，只記類型。所以 hook 不能當成唯一的安全防線；「失敗就擋」列為下一批，讓每個 hook 自己選。

## 和 Claude Code 對不上的部分

- **刻意不同**：
  - 指令不經 shell，要寫成程式＋參數清單；管線等 shell 語法要寫成腳本。
  - 只能擋與要求確認，不能放行、不能改寫工具輸入或輸出、不能叫模型繼續（Stop 的 `block`）。
  - http 標頭不替換環境變數；hook 程式拿不到金鑰。
  - UserPromptSubmit 的補充說明只用在這一次請求，Claude Code 會留在之後的對話裡。
  - 被 UserPromptSubmit 擋下的訊息仍留在對話紀錄裡、標成被擋下且不再送給模型（決定 14），Claude Code 會把它從紀錄抹掉。
  - hook 失敗一律不擋，沒有「失敗就擋」的選項（決定 15）。
  - 全系統一份設定，沒有使用者、專案、本機多層設定，也沒有技能或外掛帶的 hook。
  - 事件內容沒有 `transcript_path`、`cwd`；`permission_mode` 是 Touka 的模式名稱（Manual／Auto／YOLO），UserPromptSubmit 沒有；多一個 `incognito`。
  - 指令與參數有路徑限制（決定 5）。
- **Touka 沒有對應功能，不做**：工作目錄切換、檔案監看、worktree、Agent 團隊相關事件。
- **下一批**：hook 類型 `mcp_tool`、`prompt`、`agent`（後兩種會用到模型額度）；非同步 hook（`async`）、`if` 條件、`once`；每個 hook 可選的「失敗就擋」；設定頁與 `GET /hooks` 遮住 http 標頭的值。
- **已知的界線**：
  - Windows 上 `.bat`／`.cmd` 由系統經 `cmd.exe` 啟動，參數會被 `cmd.exe` 重新解讀；要避開 shell 就用其他程式寫 hook。
  - 參數只檢查看起來像路徑的字詞：直譯器的 `-c`／`-m` 給的程式碼或模組名稱不是路徑，hook 實際執行哪些程式碼由管理員負責。
  - MCP 可寫資料夾只來自 `mcp_server_isolation.json`；IDE 與未隔離的 MCP 伺服器能寫哪裡 Touka 不知道，管理員不要把 hook 程式放在那些地方。
  - hook 程式本身可以讀寫任何 Touka 權限能到的地方（決定 5 最後一點）。
- **丙-2 接線**：工具失敗與被拒、跳出審批卡、通知、上游錯誤結束、子代理開始與結束、滑動視窗剪裁前後（不能拖住對話）、任務建立與完成、MCP 向使用者要資料（只能拒絕）、設定變更（不能擋關閉 hook），以及 Touka 自己的心跳開始與結束。

## 後果

- 沒有設定時行為不變：接線點先問 `has_hooks`，不組事件內容、不改任何結果（golden 不變）。
- 設定了 PreToolUse 的工具，每次呼叫多等 hook 執行完（最多到逾時）；Stop 不影響回覆。
- 審批卡事件多一個選填欄位 `hook_reason`；聊天請求被擋下時回 403 `HOOK_PROMPT_BLOCKED`。
- 新增三個管理端點（決定 10）；`openapi.json` 與 `api.ts` 隨之更新。
- 撤回：把 `enabled` 設成 false 或清空 `hooks` 立即停用；revert 即可整個移除，`hooks.json` 留著無害。

## 替代方案

- **照 Claude Code 經 shell 執行**：事件內容或參數可能被當成指令執行，也較難檢查用到哪些腳本。不採用。
- **hook 程式經隔離 runner 執行**：hook 常需要讀專案或呼叫外部工具，執行器的限制會讓大多數用途失效；改以「只有人能設定、腳本在受保護位置」保證代理人無法影響它。不採用。
- **允許 `allow`／`updatedInput`**：會讓 hook 成為繞過審批與底線的途徑。不採用。
- **每個靈魂或專案各一份設定**：設定入口與權限檢查變多，現在沒有需求。不採用。

## 驗證

- `tests/unit/test_hooks_config.py`：設定模型（只收已接線事件、handler 型別、標頭環境變數、上限）。
- `tests/unit/test_hook_paths_security.py`：參數必須在 `config/hooks/`、專案清單變動不影響 `config/hooks/` 裡的腳本、禁區（工作區根、專案根與翻譯後路徑、MCP 可寫資料夾、容器鏡像路徑）、讀不到 MCP 隔離設定檔、以檔案識別碼擋別名、連結（含 `config/hooks/` 本身是連結）、硬連結、相對參數、UNC 與磁碟機相對路徑、讀不到狀態。
- `tests/unit/test_project_root_protected_security.py`：受保護的位置與上層、經連結指到設定資料夾、容器裡的 Touka 目錄、使用者資料夾照常可用、專案工具對一般身分拒絕受保護的專案根、管理員不受限、登記受保護的專案根被拒。
- `tests/unit/test_hook_runners_security.py`：不經 shell、環境不含金鑰、逾時結束行程樹、輸出上限、http 不跟隨轉址、整次請求的逾時與讀到上限就停。
- `tests/unit/test_hook_service.py`：解讀規則（含 `continue: false`）、同時執行、只擋不放行、執行紀錄與無痕、壞檔／讀不到／被刪掉時沿用上一份、服務出錯不外洩例外、存檔 CAS 與路徑檢查。
- `tests/unit/test_hook_wiring_security.py`：四個事件的接線（只在真的請求執行、擋下帶理由、補充說明是資料、UserPromptSubmit 不帶模式、PreToolUse 擋下不送審不跳卡、政策已拒絕的不送 hook、要求確認跳標成 hook 的卡且不送審、無人在場拒絕、YOLO 也要人、`allow` 不放行、matcher、PostToolUse 只在成功後、Stop 在背景、hook 服務出錯不影響結果、沒有設定時結果不變）。
- `tests/unit/test_group_soul_only.py`：hook 補充說明裡的 `@別名` 不算點名。
- `tests/unit/test_hard_floor_security.py`：讀寫 `hooks.json` 屬 F2，寫 `hooks.json.example` 與 `config/hooks/` 屬 F3。
- `tests/unit/test_public_repo_hygiene.py`：`config/hooks/` 不進版控。
- `tests/unit/test_hooks_router_security.py`：只有人類管理員能讀寫、存檔寫檔並回 revision、作者由伺服器填、舊 revision 拒絕且不動檔、路徑被拒不寫檔、不可信的 Origin 拒絕、格式不對拒收、執行紀錄與無痕、壞檔時的 revision、`/v1` 前綴、設定匯出不含 `hooks.json`。
- ToukaChat：審批卡的 hook 理由、串流事件解析、被擋訊息的通知與理由；被擋訊息標成 `hookBlocked`、顯示說明、下一輪不送給模型；設定頁的草稿函式、資料層（revision、錯誤分類）、分頁（事件區塊、編輯後送出的設定、存檔被拒的原因、壞檔提醒）與執行紀錄清單。
