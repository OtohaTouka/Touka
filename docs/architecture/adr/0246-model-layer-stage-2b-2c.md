# ADR-0246：模型溝通層第 2b＋2c 段（審批管線、Manual／Auto／YOLO 三模式、底線與審查員）

- 狀態：✅ 已接受（2b＋2c 已完成；取代 ADR-0021 的四階矩陣與 MonitorLayer 降級閘；修訂 ADR-0143 的四級決策表；修訂 ADR-0018 §5、§6 的四級與等級鎖定；修訂 ADR-0089 的「全等級（含 ReadOnly）」；修訂 ADR-0078 的「UI 重新觸發強制 SafeAuto」。細節見「取代與修訂」；[ADR-0272](0272-iso-c2-agent-command-isolation-and-executor.md) 修訂：決策表多「隔離層級」一個維度〔`none` 時 Auto／YOLO 只有人能核准〕、決定 4〔YOLO 無人在場的 dangerous MCP 只有人能核准〕、F5 的兩個演化舊別名、審查員 `<environment>` 的 `isolation`）
- 修正（[ADR-0254](0254-iso-c1-small-fixes-and-execution-gaps.md)）：已知限制「互審驗證指令被審查員轉人時，那張卡不顯示審查理由」已修；「Auto 下 F5 的自帶審批工具會出兩張卡」的清單少了 `evolution_implement_and_verify`（改為一般分類，Auto 下仍是兩張卡）
- 2026-10-16 由 [ADR-0271](0271-claude-5-5-thinking-by-purpose.md) 修訂（r55）：`auto_review.timeout_s` 預設 20→60 秒（逾時仍是轉人／拒絕，不是放行條件）；設定頁可看可改；審查員照模型預設思考強度（不在少想用途表）
- 日期：2026-10-12
- 決策者：Owner
- 影響範圍（2b）：`services/approval_flow.py`（新）、`services/tool_executor.py`、`services/review/evidence.py`、`services/review/review_loop.py`、`tools/mission_tools.py`、`tools/subagent_tools.py`、ToukaChat `ToolApprovalCard.tsx`、SECURITY.md（B2、B1、A7）、ADR-0137 補記、對應測試
- 影響範圍（2c）：
  - U2c-1 底線：`utils/hard_floor.py`（新）、`models/safety.py`、`services/tool_executor.py`、`services/approval_flow.py`、`services/permission_service.py`、`tools/terminal.py`、`services/review/evidence.py`
  - U2c-4 改名與模式授權：`utils/auth_mode.py`（新）、`utils/command_safety.py`、`utils/hard_floor.py`、`services/permission_service.py`、`models/safety.py`、`models/session_persistence.py`、`models/context.py`、`routers/ui_sessions.py`、`routers/debug.py`、`routers/heartbeat.py`、`routers/souls.py`、`utils/async_db.py`、`middleware/layers/monitor.py`、`middleware/layers/tool.py`、`protocols/manager.py`、`services/prompt_blocks.py`、`auth_policies.json.example`、ToukaChat AuthSwitcher、`session_persistence.ts`、`InterruptedRunsNotice`、翻譯與型別、openapi
  - U2c-2 原意與在場：`utils/user_intent.py`（新）、`utils/presence.py`（新）、`middleware/layers/monitor.py`、`middleware/layers/tool.py`、`services/tool_executor.py`、`tools/subagent_tools.py`、`routers/heartbeat.py`、`protocols/manager.py`（workload OAuth 維持關閉）、`scripts/native_tool_contract_baseline.json`
  - U2c-3 審查員：`services/auto_reviewer.py`（新）、`models/safety.py`、`models/autonomy.py`、`autonomy.json.example`、`services/tool_executor.py`、`services/approval_flow.py`、`utils/tool_events.py`、`tools/execution_context.py`、`tools/terminal.py`、`tools/mission_tools.py`、`services/review/evidence.py`、`services/review/review_loop.py`、`main.py`、`errors.json.example`（＋ToukaChat `error_codes.ts`）、`scripts/native_tool_contract_baseline.json`、openapi
  - U2c-5 心跳：`routers/heartbeat.py`（模式對應、脈衝跑 ToolLayer）、`middleware/layers/tool.py`（保留設定時的模式）、ADR-0242 KF 表（KF-4 改「已修」）
  - U2c-7 IDE：ToukaIDE `mcp/authPolicy.ts`、`mcp/modalApproval.ts`、`mcp/humanApproval.ts`（新）、`mcp/server.ts`、`mcp/protocolServer.ts`、`mcp/handlers/TerminalHandlers.ts`、`mcp/handlers/FileHandlers.ts`、`extension.ts`；Proxy `utils/ide_human_approval.py`（新）、`protocols/ide/handler.py`、`protocols/mcp/canonical.py`、`protocols/manager.py`、`services/tool_executor.py`
  - U2c-6 前端：ToukaChat `AuthSwitcher`、`ChatProvider`、`ToolApprovalCard`、`stream_tree`／`tree_history`、`SecurityTab`＋`AutoReviewerSettings`＋`useAutoReviewSettings`（新）、`HeartbeatScheduler`＋`heartbeat_mode`（新）、`DirectConnectionNotice`（新）、翻譯；Proxy `routers/autonomy.py`（`GET /v1/autonomy/reviewer-models`）、`models/autonomy.py`；openapi／`api.ts`
  - U2c-9 無痕一律 Manual：`utils/auth_mode.py`（`enforce_incognito_manual`）、`middleware/layers/tool.py`、`tools/subagent_tools.py`（`_run_subagent`）；ToukaChat `ChatProvider`、`AuthSwitcher`、翻譯
  - R-2c 修正（U2c-R1a；M6 之後的細修 U2c-R1c 只改 `utils/hard_floor.py`）：`utils/hard_floor.py`、`utils/command_safety.py`、`models/safety.py`、`services/resource_manager.py`（工作區掛載點）、`services/tool_executor.py`、`services/approval_flow.py`、`tools/terminal.py`
  - R-2c 修正（U2c-R1b）：`routers/autonomy.py`、`routers/heartbeat.py`、`utils/user_intent.py`、`services/sns_gateway.py`、`tools/subagent_tools.py`、`services/auto_reviewer.py`、`utils/auth_mode.py`、`routers/proxy.py`、`middleware/layers/tool.py`；ToukaChat `ChatProvider`
  - U2c-8 文件：AGENTS.md §2、模式名稱與禁令 64；SECURITY.md；PROXY／HEARTBEAT／CHAT／IDE_INTERFACE、ROLE_MODEL、SSOT_MASTER_MAP；ADR-0018、0021、0078、0089、0143 的反向註記；README、手冊與指南的模式名稱

## 背景

三種模式都會用到人類審批，所以先修審批管線本身。互審 `request_review` 的驗證指令（`verification_cmd`）有三個問題：

- **B2（人沒看過指令就執行）**：Manual 互動會話核准 `request_review` 卡時，卡上只有 `task_id`／`producer_claims`。核准結果（`_hitl_preapproved`）一路傳到 `policy_command_gate`，任務板上 CONFIRM 級的驗證指令就直接執行。ADR-0137 先前的取捨是「initialize 時已看過指令」，但 initialize 卡的 `tasks` 參數被截到 200 字，後面任務的指令其實看不到。
- **A7（卡上看不到完整指令）**：審批卡的參數預覽把每個字串截到 200 字，前端再單行省略。
- **B1（人沒核准也被扣次數、鎖死）**：無人在場（背景心跳、子代理），或有人在場但卡片逾時、被拒時，指令沒跑，這一輪照樣送 critic、扣 `review_attempts_total`，扣到上限就永鎖。無人在場時回給代理人的訊息還叫它改用 `execute_command` 自己跑、把輸出貼進 `test_log`，等於用自填證據取代系統蒐證。

## 決策

### 審批（2b）

1. **共用審批流程**：「登記 → 推卡 → 等決定 → 推解算事件」抽成 `services/approval_flow.py` 的 `await_human_approval`。`ToolExecutor._await_human_approval` 保留同名方法並委派給它（Guard #40 綁這個方法名與 `prompt_blocks` 的承諾句，兩邊都不改）；工具風險章仍由 ToolExecutor 依政策求出。
2. **逐條審批（B2）**：
   - `policy_command_gate` 改成 async，不再收 `hitl_preapproved`，改收可選的 `approver`（`Callable[[str], Awaitable[str]]`，回審批結果）。`_hitl_preapproved` 只剩 terminal 用來跳過自帶審批。
   - CONFIRM 級指令有 approver 時，每條指令各自跳一張卡，核准才執行。步驟 id 用 `<request_review 的 id>:verify:<n>`（n 依問人順序從 1 起），畫面上各自成一步：核准後送 `tool_start`，審查跑完推結果；拒絕、逾時、佇列滿則該步驟以 `permission_denied` 結束。卡的工具名用 `execute_command`、風險固定 `dangerous`。ALLOW 照常執行、DENY 與未知決策一律拒絕，兩者都不問人。
   - 指令要先解析得出才過政策閘，不為跑不起來的指令跳卡。
   - 只有互動會話（可信 kwarg `_interactive`，有事件出口與 `_call_id`）才建 approver。子代理不建：子 loop 只收文字，卡送不到人，所以子 ctx 帶 `subagent_run`，`request_review` 立刻當無人在場處理，不必等 180 秒逾時。
   - 核准後審查若中途拋例外，已開的驗證步驟以 `internal_error` 收尾，例外照常往上。
3. **完整指令（A7）**：預覽對 `command` 保留到 8 KB（模組常數 `APPROVAL_COMMAND_PREVIEW_LIMIT`，禁令 18），超過才截斷並附 `…[truncated: N more chars not shown]`；其他參數仍截到 200 字。審批卡改成換行顯示（`whitespace-pre-wrap break-all`，捲動區高度 `max-h-64`），只用標準 class（禁令 49）。
4. **卡在人類這一關（B1，決策 P6 甲）**：
   - 審批結果對應固定理由：沒有 approver（無人在場）→ `AWAITING_HUMAN_APPROVAL`；逾時、佇列滿或其他未知結果 → `NO_HUMAN_DECISION`（fail-closed）；被拒 → `REJECTED_BY_HUMAN`。
   - `run_review_round` 見到任一條，本輪標 `BLOCKED_FOR_HUMAN` 後直接返回：不呼叫 critic、不扣次數、不上鎖，也不佔 mission 子 loop 名額。
   - `request_review` 回結構化 `permission_denied`，帶 `reason`、`review_status`、`review_locked=false`、`evidence`。無人在場、逾時與佇列滿是 `reason=human_approval_required`（請等人回來後在對話裡重跑，不要用別的方式跑那條指令）；被拒是 `reason=human_rejected_command`（先問人類要怎麼驗證）。訊息不再叫代理人改用 `execute_command`。往返次數達上限的永鎖仍回一般審查結果，和這三種情形分得開。
5. **P7**（同一對話被擋連續 3 次或累計 20 次就直接問人）不在 2b，留給 2c（已由 U2c-3 完成，見「Auto 審查員」第 5 點）。

### 三模式（2c）

#### 總覽

三種模式（Manual／Auto／YOLO）取代四級（ReadOnly／Manual／SafeAuto／FullYOLO）。一次工具呼叫依序經過下面五步，後面各小節依這個順序各管一段：

1. **模式從哪來**（U2c-4、U2c-2、U2c-5）：互動對話讀 session row，經 `normalize_auth_mode` 對照舊值，存下的 Auto／YOLO 只在 session 綁的 agent 就是這次執行的 agent 時採用（R-2c）；子代理沿用父代理的模式；心跳用排程設定時決定的模式，不採 session row；請求參數裡的 `auth_level`（前端、IDE 帶的）不是來源。例外兩個，都受底線約束：`/debug/execute` 採用 payload 的 `auth_level`；`/retrigger` 固定 Auto。無痕對話一律 Manual，蓋過上面所有來源（見「無痕對話」）。
2. **誰能設 Auto／YOLO**（U2c-4）：在 session 寫入時判定，只有 admin 或該靈魂的擁有者；其他人的寫入存成 Manual。前端存檔後立即顯示實際存下的模式（U2c-6）。`/retrigger` 用同一個判斷（R-2c）。
3. **判斷層**（`AuthPoliciesConfig.evaluate`，同步純函式）：底線（U2c-1）→ 黑名單 → 風險分類 → 三模式決策表（U2c-4 改名，U2c-3 加 `REVIEW`）。
4. **執行層**（`ToolExecutor`）：依效果與在場判斷（U2c-2）處理；`REVIEW` 交給審查員（U2c-3）；人類審批卡沿用 2b 的 `approval_flow`。
5. **入口差異**：心跳（U2c-5）一律無人在場；IDE（U2c-7）自己只有 Manual／YOLO，並接受「由人核准」標記；前端（U2c-6）提供模式選單、審查員設定與卡片欄位。

| 動作 | Manual | Auto | YOLO |
|---|---|---|---|
| read_only 類工具 | ALLOW | ALLOW | ALLOW |
| safe 類工具 | CONFIRM | ALLOW | ALLOW |
| 終端指令、dangerous 類工具（含未列名 MCP） | CONFIRM | REVIEW | ALLOW |
| 含動態 shell 寫法的指令 | CONFIRM | HUMAN_ONLY | ALLOW |
| F5 能力擴張與身分、F6 對外發言 | CONFIRM | HUMAN_ONLY | ALLOW |
| 底線 F1–F4、F7 | HUMAN_ONLY | HUMAN_ONLY | HUMAN_ONLY |
| 黑名單 | DENY | DENY | DENY |

看不懂的模式值 CONFIRM（等同 Manual）。各效果在執行層的處理：

| 效果 | 有人在場 | 無人在場（心跳、子代理、派工） |
|---|---|---|
| ALLOW | 執行 | 執行 |
| CONFIRM | 跳人類審批卡，核准才執行 | 拒絕（終端照舊走 terminal 的遺留通道，沒有 ToukaUI 就拒絕） |
| REVIEW | 審查員 allow 就執行；escalate（含任何失敗）跳卡、卡上帶理由；deny 拒絕 | 不送審、拒絕；只有終端可由 `auto_review.headless_terminal` 打開送審（預設關） |
| HUMAN_ONLY | 跳卡、標「只有你能核准」，只認人類核准 | 拒絕 |
| DENY | 拒絕 | 拒絕 |

#### 無痕對話一律 Manual（人類 10/12 12:44）

- **規則**：無痕對話一律 Manual。理由：無痕不寫資料庫、不留紀錄，事後無法追溯代理人做過什麼，所以不給會自動放行的模式（人類原文：「強制無痕只能manual好了，因為無痕沒辦法追溯做了什麼，嚴格比較安心」）。這是收緊，不是放寬。
- **修掉的漏洞**：以前無痕是 Manual 只是「無痕不存檔、ToolLayer 讀不到 session row」的副作用；無痕請求帶著已存成 Auto／YOLO 的 session id 時，會照存的模式跑。
- **強制（U2c-9）**：純函式 `utils/auth_mode.enforce_incognito_manual`（無痕回 Manual、非無痕原樣不動）。ToolLayer 在所有模式來源（存檔、心跳、子代理繼承）之後最後套一次；子代理的執行 ctx 不經 ToolLayer，`subagent_tools._run_subagent` 也套一次，組裝與執行一致。
- **原因提示在前端**：無痕對話不會進對話存檔端點，所以由前端說明：模式選單鎖定顯示 Manual，說明「無痕對話無法追溯，只能用 Manual」；在無痕對話要求 Auto／YOLO 時維持 Manual、不存檔，並以 toast 說明原因。審查員設定的資料說明也拿掉了「無痕對話也一樣」。
- **驗證**：`test_incognito_manual_only_security.py`（21 個）；U2c-6 的 `test_auth_mode_authority_security.py` 另釘「沒有存檔的無痕對話帶 Auto／YOLO 仍是 Manual」。
- **④ 的資料外送因此不涵蓋無痕**：Auto 會把最近 3 則訊息和要做的動作送到所選模型的廠商審查；無痕對話不會開 Auto，所以不會送。

#### 事先核准的變寬（不放寬總表）

master §7-4 事先核准的變寬只有四項，其他格子同嚴或更嚴：

1. **ReadOnly→Manual**（U2c-4）：舊 ReadOnly 對話的寫入工具與指令改成問人後可跑。
2. **KF-4**（U2c-5）：心跳第一次拿到工具；變寬的格子恰好是核准清單（safe 4 格、full 12 格、看不懂 6 格），每格都不比對應舊等級在無人在場時寬。
3. **Auto 經審查員放行**（U2c-3）：終端指令與高風險工具，只在有人在場時；表上三格是 SafeAuto→Auto 的 `curl`、`delete`、未列名 MCP。
4. **IDE 決定 9 甲**（U2c-7）：只限人類本人在 Touka 卡片上核准過的那一次呼叫，IDE 不再跳第二次確認；審查員或 YOLO 放行的照 IDE 自己的模式處理。

比原本嚴的：SafeAuto→Manual、看不懂的心跳值→Manual、`/debug/execute` 在 YOLO 也擋底線、IDE 黑名單由確認改拒絕、無痕一律 Manual。逐格由 `test_auth_mode_not_wider_security.py`（Proxy）與 `ToukaIDE/src/__tests__/authPolicy.test.ts`（IDE）釘住，變寬的格子必須恰好等於核准清單。

#### 取代與修訂

- 取代 ADR-0021 的四階矩陣（§1）與 MonitorLayer 降級閘（§3）：四級改三模式；降級閘的結果會被 ToolLayer 用 session 的值覆寫、實際失效（D_permissions §1.9-1），已拿掉，授權移到 session 寫入。黑名單優先、`auth_level` 深傳等其餘決策仍有效。
- 修訂 ADR-0143 的四級決策表：4×3 表改成上面的三模式表（Auto 加 `REVIEW`、底線加 `HUMAN_ONLY`）；終端不再依 `safe_whitelist` 分 safe／dangerous，一律 dangerous；心跳的四級欄位改收三模式。`tool_risk` 分類、CONFIRM fail-closed、聊天審批 broker、`/v1/tools/approve|reject` 的人類硬閘不變。
- 修訂 ADR-0018 §5、§6：四級改三模式；「只有 admin 或擁有者能提升等級」從 MonitorLayer 移到 session 寫入。JWT、Bcrypt、多使用者認證不變。
- 修訂 ADR-0089：「read-only ⇒ 全等級（含 ReadOnly）ALLOW」改讀作「三種模式都 ALLOW」；ReadOnly 已退役，語意不變。
- 修訂 ADR-0078：UI 重新觸發原本「強制 SafeAuto」，改成照 `safe` 對應的 Auto（無人在場不跑終端、不叫審查員）；擁有者守衛、能力閘、pulse 上限不變。

五份 ADR 的狀態區與索引列都已加反向註記。

#### 底線（U2c-1）

總覽第 3 步的第一關：判斷層最先看底線，模式與審查員都在它之後。

決策：

1. **硬地板寫在程式裡**：新模組 `utils/hard_floor.py`（純函式＋模組常數，禁令 18）。`AuthPoliciesConfig.evaluate` 最前面呼叫 `classify_hard_floor`，再和設定檔規則合併：F1–F4、F7 → 新效果 `HUMAN_ONLY`；設定檔規則已經是 DENY（黑名單、ReadOnly）時維持 DENY，地板只會更嚴。`auth_policies.json` 沒有任何能關掉地板的欄位。命中時 `PolicyDecision.metadata["hard_floor"]` 記類別。`evaluate` 仍是同步純函式。
2. **`HUMAN_ONLY`**：ToolExecutor 有人在場就跳同一張人類審批卡（`approval_flow`），核准後以可信 kwarg `_hitl_preapproved`（執行情境覆寫模型參數）交給 terminal；terminal 遇到 `HUMAN_ONLY` 只認這個值為 `True`，DENY 照擋，`HUMAN_ONLY` 不加進 `RUNNABLE_STRATEGIES`。無人在場直接拒絕（`reason=human_only`），終端也不落到遺留通道。互審 `policy_command_gate` 比照 CONFIRM：有人在場逐條問人，無人在場回等人確認（`AWAITING_HUMAN_APPROVAL`）。**只有人能滿足 `HUMAN_ONLY`**：U2c-3 的「審查員核准」kwarg 不得被 executor、terminal 或互審當成人類核准。MCP MRTR 只在 DENY 時拒絕，不把它當 DENY。
3. **F5、F6 只回類別**：Manual 照常 CONFIRM、YOLO 放行；Auto 下只有人能核准（U2c-4 接上）。
4. **規則檔寫壞時保留黑名單**：熱重載讀到解析或驗證失敗的 `auth_policies.json`，黑名單保留上一份可用的，白名單與風險表退回最嚴的預設；啟動時沒有上一份才用預設。

| # | 類別 | 怎麼判斷 | Manual／Auto／YOLO |
|---|---|---|---|
| F1 | 刪改 Touka 資料 | 寫入動詞（rm、mv、cp、Remove-Item、Set-Content、sed、直譯器、`>` 重新導向…）＋絕對路徑含 data、soul、legacy，或相對路徑含 soul、往上跳出工作目錄（`..`） | 只有人能核准 |
| F2 | 讀金鑰 | 不看動詞：`.env`、`.env.*`（.example 等範本除外）、`users.json`、`connections.json`、`/proc/*/environ` | 只有人能核准 |
| F3 | 改權限設定 | 寫入動詞＋`auth_policies`、`permissions.json`、`identity_map`、`autonomy.json`、`dispatch_hooks.json`、`server_config.json`、`group_whitelist.json`，或 `app/config`、`ToukaProxy/config` 目錄 | 只有人能核准 |
| F4 | 改原始碼 | 寫入動詞＋`ToukaProxy/src`、ToukaMemory、ToukaDispatch、ToukaHeartbeat、ToukaChat、ToukaIDE、`host_root` | 只有人能核准 |
| F5 | 能力擴張與身分 | 原生工具名：裝技能、建或接 MCP、演化部署、改靈魂；`agent_control` 的 `switch_brain` | CONFIRM／只有人能核准／放行 |
| F6 | 對外發言 | `send_message`、`sns_control` | CONFIRM／只有人能核准／放行 |
| F7 | 系統破壞 | 不錨定開頭，逐段看指令名（`;`、`&&`、`\|`、引號內各算一段，`bash -c`、`cmd /c`、`powershell -Command` 後面也算）：shutdown、reboot、halt、poweroff、mkfs*、dd、`format X:`、diskpart、Stop-Computer、`docker compose`／`docker-compose` 的 up／down／stop／restart／kill／rm、pkill、killall、taskkill、Stop-Process、`kill 1`／`kill -1`；遞迴刪除（`rm -fr`、`rm -r -f`、`Remove-Item -Recurse`、`rd /s`）根目錄、第一層目錄、家目錄、`.`、`*` 或 `..` | 只有人能核准 |

（U2c-1 時 ReadOnly 照舊 DENY；ReadOnly 已在 U2c-4 退役，讀取時當 Manual。）

F5 對照（decision_memo_v6 §4.6-4 的五類）：

| 類 | 算 F5 的原生工具／動詞 | 不算的與理由 |
|---|---|---|
| 裝技能 | `request_skill_install`（catalog 安裝申請）、`create_skill`（創建引擎新增技能） | `activate_skill` 只把已安裝、角色允許的技能指令載入對話，不新增能力；`search_skill_market` 唯讀；catalog 安裝端點是人類路由、不是工具 |
| 建立或連接 MCP | `create_mcp`、`scaffold_mcp`、`request_mcp_creation`、`connect_mcp_server` | — |
| 演化部署 | `evolution_implement_and_verify`（可 `submit_for_deploy`）、`request_mcp_creation` | `evolution_define`、`propose_new_capability` 只寫規格；`write_mcp_implementation` 寫在演化沙箱（不是 Touka 原始碼，不歸 F4），`verify_mcp_locally` 只在沙箱試跑；新能力要經 `request_mcp_creation` 部署才生效 |
| 改靈魂 | `edit_soul`、`update_my_soul`（不分動詞：它的 action 不在 `PolicyContext`，讀取也算） | — |
| 換腦 | `agent_control` 的 `switch_brain` | `list_brains`、`get_status` 唯讀；soul／config 的寫入只經 `edit_soul`，已算 F5 |

F6 核對：原生工具只有 `send_message` 與舊名 `sns_control` 以靈魂名義對外發訊息；`research`（送出搜尋字串）、`ask_user`（問使用者本人）、`mission_tools`／`dispatch_subagent`（內部委派與看板）、`ide_open_diff`（本機 IDE）都不算。

已知改變：`/v1/debug/execute` 沒有人在場的旗標，YOLO（原 FullYOLO）下遇到 F1–F4、F7 指令也拒絕（`reason=human_only`），比原本嚴，測試釘住。

已知限制：

- shell 文字只能盡力而為：變數（`$X`、`$env:`）、萬用字元（`.e*v`）、編碼（base64、`-EncodedCommand`）、字串拼接、`cd` 之後的相對路徑、先寫腳本再執行，都能繞過文字地板。真正的地板是隔離執行環境（第 5 段）。
- 誤判偏嚴：`echo "rm -rf /"`、`cat soul/x > out.txt`、`docker exec … stop` 也會要人核准。
- 檔案工具（write、edit、delete）的路徑不在 `PolicyContext` 裡，F1–F4 只看終端指令；檔案工具仍靠 `is_protected_path` 與 vault 監獄。
- MCP 工具不分類（名字可能和原生工具撞名）；會對外發訊息的 MCP 工具在 Auto 下怎麼處理留給 U2c-3（已處理：未列名 MCP 一律 dangerous，Auto 下送審查員，不歸 F6）。
- 自帶審批工具遇到 `HUMAN_ONLY` 會先過 broker 卡、再過自己的卡（兩張）；本單位 F5 不產生 `HUMAN_ONLY`，碰不到，留給 U2c-3（U2c-4 起 Auto 下的 F5 會碰到，未修，見「代價與剩餘風險」）。
- 規則檔被清空（空字串或 `{}`）或刪掉時照舊退回預設，黑名單是空的；只保證解析或驗證失敗時保留。

驗證：`tests/unit/test_hard_floor_security.py`（111 個，含每一類地板的指令或工具、YOLO 下仍擋、DENY 不放寬、設定檔關不掉、HUMAN_ONLY 有人跳卡／無人拒絕、terminal 只認可信核准、模型自帶的核准旗標無效、互審逐條問人、debug 端點照擋、壞檔保留黑名單）。

回復：撤 U2c-1 的程式、測試與 SECURITY.md FL1–FL2，本節標「已撤回」。撤回後地板消失，規則檔寫壞時黑名單又會被清空，不建議。

#### 改名、舊值讀取對照與模式授權（U2c-4）

總覽第 1、2 步與三模式決策表本身。

決策：

1. **三種模式**：`utils/auth_mode.py`（純函式＋常數表，禁令 18）定義 `AuthMode`＝Manual／Auto／YOLO。寫入與 API 輸入（`SessionChanges`、`SessionModel`、`/debug/execute`、`/heartbeat/awaken`）只收新名稱，舊名稱回 422。
2. **讀取時對照，不遷移**（人類 10/12 04:30）：儲存層讀 `user_sessions.auth_level` 時經 `normalize_auth_mode`：ReadOnly、SafeAuto、看不懂的值 → Manual；FullYOLO → YOLO。快照、清單、ToolLayer、MCP MRTR 都只看到新名稱；之後任何一次存檔自然寫回新名稱。沒有資料庫遷移，也不備份。
3. **決策表**：Manual 照舊；YOLO＝原 FullYOLO（地板仍擋）；Auto：read_only、safe 類 ALLOW（決定 7 甲），含動態 shell 寫法（`has_dynamic_shell_syntax`：變數、子指令、PowerShell 編碼參數、eval、寫進檔案的 `>`）與 F5、F6 → HUMAN_ONLY（決定 4 丙、6），其他終端與 dangerous → CONFIRM。這個 CONFIRM 是 U2c-3 審查員接上前的佔位，fail-closed，不自動放行。終端一律算 dangerous：`safe_whitelist` 從 `SafetyRules` 與 `.example` 拿掉，舊 live 檔留著這個鍵讀到會被忽略。看不懂的模式值 CONFIRM。MCP MRTR 的等級排序改三級（Manual 最嚴）。
4. **模式授權移到 session 寫入**（D_permissions §1.9-1）：MonitorLayer 對請求參數的降級閘會被 ToolLayer 用 session 的值覆寫，實際失效，已拿掉；請求參數（前端、IDE 帶的 `auth_level`）不是模式來源。`ui_sessions._authorized_changes`：存檔後是 Auto／YOLO 的對話，只有 admin 或該靈魂的擁有者能寫，其他人的寫入照 AGENTS.md §2 降成 Manual。看的是存檔後的模式與靈魂，所以沒帶 auth_level 的存檔、改掛靈魂也會重驗。
5. **心跳只改名**：safe→Auto、full→YOLO、看不懂→Auto、retrigger 強制 Auto（Auto 在每一格都不比 SafeAuto 寬）。看不懂改 Manual、無人在場不跑終端、KF-4 留給 U2c-5（已做，見「心跳」）。

不放寬總表（`test_auth_mode_not_wider_security.py`）：14 個代表動作 × 四組對應（ReadOnly→Manual、SafeAuto→Auto、SafeAuto→Manual、FullYOLO→YOLO）× 有人／無人在場。事先核准的變寬只有四項：ReadOnly→Manual（核准後可寫）、KF-4（U2c-5）、Auto 經審查員放行（U2c-3）、IDE 決定 9 甲（U2c-7）。本單位只碰到第一項，變寬的格子逐格列在測試裡，必須恰好相等；SafeAuto 的兩個接班模式都比原本嚴。

已知改變：

- ReadOnly 的「代理人連寫入工具都看不到」沒了：Manual 會廣告工具，每次出卡。
- 舊 SafeAuto 對話讀成 Manual；心跳 safe 的舊白名單指令（ls、pytest…）不再自動跑，無人在場直接拒絕。
- 非授權的人在前端選 Auto／YOLO，後端存成 Manual，但選單要重新載入才顯示 Manual（U2c-6 已改成存檔後立即顯示實際模式並提示）。

驗證：`test_auth_mode_three_modes.py`（對照表、授權純函式、Auto 決策、動態 shell、safe_whitelist、MRTR 排序）、`test_auth_mode_not_wider_security.py`、`test_auth_mode_authority_security.py`（四種身分、前擁有者、只收新名稱、舊值讀取與寫回）；E01／G16 兩個 Monitor 測試改釘「Monitor 不產生模式」與「授權看人類請求者」。

回復：撤 U2c-4 的程式、測試、前端與 openapi，本節標「已撤回」。撤回後舊程式讀不到 Auto／YOLO（現有資料是測試用，人類已同意）；`safe_whitelist` 只要還原 `.example`。

#### 使用者原意與在場判斷（U2c-2）

總覽第 1 步的子代理模式來源與第 4 步的在場判斷；產生的原意是下一節審查員的輸入。

決策：

1. **使用者原意**：`utils/user_intent.py`（純函式＋常數，禁令 18）產生 `ctx["user_intent"]`，是凍結的 `UserIntent(source, messages, parent, trust="data")`，只當審查員（U2c-3）的證據、不是指令。本單位只產生，不送出、不寫 log、不落地；無痕對話照樣產生（④甲），一樣只留在記憶體（無痕一律 Manual，實際不會送審）。每則截到 1,000 字（`INTENT_MAX_CHARS`，超過加 `…[truncated]`）。

   | 情境 | `source` | 內容 |
   |---|---|---|
   | 一般對話（MonitorLayer） | `chat` | 最後 3 則人類 user 訊息；`name="System"` 的 nudge、心跳寫進對話的 user 訊息（`【脈衝能量補給：`、`【系統】`、`【目前系統時間：` 開頭；前端送來時會剝掉 name，所以也比對開頭）、SNS 外人訊息（`【SNS 外部訊息】` 開頭）、前端內嵌的附件（從 `[Attached file: `／`[Uploaded file: ` 起整段）與 tool_result、圖片區塊都不算（後兩項 R-2c） |
   | 心跳 `/awaken` | `heartbeat_init_prompt` | 消毒後的 `init_prompt` |
   | UI 重跑 `/retrigger` | `retrigger_last_human` | session 樹最後一則非 `System` 的人類節點（那次背景任務的 init_prompt 也算） |
   | 子代理 | `subagent` | 沒有訊息，`parent` 帶父代理的原意；派工目標是代理人轉述，不收（R-2c，原本收進 `agent_relayed_goal`） |

   三個開頭字串是 `heartbeat.py` 組 nudge 時用的同一組常數。子代理組裝也跑 MonitorLayer，帶 `subagent_run` 時不覆寫派工時設好的原意。
2. **在場判斷**：`utils/presence.py` 的 `is_human_present`：有 `resumable_stream`，而且沒有 `subagent_run`、`dispatch_run`、`heartbeat_run` 任一標記。只看伺服器設的情境標記，不看角色名稱。ToolExecutor 的人類審批、可信 kwarg `_interactive`（自帶審批與互審）、MCP 互動出口與 origin、ToolLayer 的 `ask_user` 廣告與驗證慣例都改用它。
3. **真正設定標記**：心跳 `/awaken`、`/retrigger` 設 `heartbeat_run`；in-process 子代理（組裝與執行兩個 ctx）設 `subagent_run`＋`dispatch_run`。Proxy 裡只有子代理是派工執行路徑；ToukaDispatch 的外部 AI 直接操作任務板，不經 ToolExecutor。MCP `execution_kind`／origin 派工優先於心跳（心跳派出的子代理算派工），有人在場才是 foreground。
4. **子代理一律無人在場**：子 loop 只收文字，以前互動派生的子代理遇到 CONFIRM 會跳卡、被 `_drain_text` 吞掉、等 180 秒逾時；現在 CONFIRM 與 HUMAN_ONLY 立刻拒絕（終端 CONFIRM 照無人在場的舊路：沒有 ToukaUI 就拒絕、有就非同步等人），MCP 互動直接 decline，不登記審批。
5. **子代理繼承父代理的模式**（D_permissions §1.9-2）：ToolLayer 沒有 session 時，帶 `subagent_run` 才沿用 ctx 的模式（`normalize_auth_mode`），否則照舊 Manual，組裝與執行一致。無人在場，所以繼承 YOLO 也跑不了底線指令（HUMAN_ONLY 直接拒絕）。

6. **workload OAuth 刻意維持關閉**：MCP 分類已修正（心跳標 heartbeat、子代理標 dispatch，headless 的 MRTR 拒絕因此更正確）。照修正後的分類，ADR-0207 只給心跳用的 workload OAuth 第一次走得到，等於讓無人在場的心跳多一種能力，不在 2c 範圍。所以 `protocols/manager.py` 的模組常數 `HEARTBEAT_WORKLOAD_OAUTH_ENABLED = False` 在進入點擋住，實際行為和 07eeab59 之前一樣：心跳的 MCP 呼叫照舊得到 `MCP workload OAuth is heartbeat-only.`，不連線、不拿 token。要不要打開留給人類決定；ADR-0207 本文不改，它的設計路徑測試只在測試裡把常數打開。

驗證：`test_user_intent_presence_security.py`（28 個：排除 nudge 與系統注入、最後 3 則、截短、不可變、Monitor 產生與不覆寫子代理、無痕不寫 log 不碰資料庫、心跳與 UI 重跑的來源、子代理原意、組裝標記、ToolLayer 繼承模式與不放寬、在場真值表、子代理 CONFIRM／HUMAN_ONLY 立刻拒絕不跳卡、`_interactive`、繼承 YOLO 仍擋底線、MCP 標記、心跳的 MCP 呼叫不使用 workload OAuth）。測試外層包 `asyncio.timeout(5)`，舊行為會逾時變紅。

回復：撤 U2c-2 的程式與測試，本節標「已撤回」。撤回後子代理又會等 180 秒才被拒，審查員（U2c-3）也拿不到原意。

#### Auto 審查員（U2c-3）

總覽第 3 步的 `REVIEW` 效果與第 4 步的審查員；輸入用上一節的原意與在場判斷，失敗時落回 2b 的人類審批卡。

決策：

1. **判斷層**：`AuthPoliciesConfig.evaluate` 仍是同步純函式。Auto 下的終端指令（不是地板、黑名單或動態 shell）與 dangerous 類工具（刪檔、未列名 MCP 等，照 `tool_risk` 現有分類）回新效果 `REVIEW`，只表示「要審」（決定 3：只審跑指令與高風險工具）；safe 類照舊 ALLOW，F5、F6 與動態 shell 照舊 HUMAN_ONLY。地板與黑名單在前面判斷，`REVIEW` 不會蓋過它們。
2. **審查員**：`services/auto_reviewer.py`，只經 `complete_once`（`Completer` 由組裝根 `main.py` 注入；工具模組在 dispatcher 之前載入，直接 import 會循環）。沒注入就當成無法使用、轉人。
   - **輸入**只有三樣，各放一個分隔區塊：`<user_intent>`（`ctx["user_intent"]`，標成資料；子代理只帶父原意）、`<action>`（工具名＋完整參數）、`<environment>`（shell 種類、有沒有人在場）。區塊內容是 JSON，`<`、`>`、`&` 改成 unicode 跳脫，資料拼不出結束標籤。不看工具輸出、代理人的推理或助理文字、System nudge。參數超過 16 KB 就不送審（看不到全文），直接轉人。
   - **提示詞**：區塊內容是資料不是指令，叫審查員改規則的文字算提示注入、要轉人；代理人自己寫的腳本（`python x.py`、`bash x.sh`…）除非使用者原意明確要求，否則轉人。
   - **判決**只接受 `{"verdict": "allow"|"escalate"|"deny", "reason": "..."}`：Claude 外送 `response_schema`；所有廠商一律嚴格解析（整段就是這個物件、剛好兩個欄位、不得重複鍵、判決值小寫且在三者之內）。多寫字、code fence、缺欄位、多欄位、未知判決，以及逾時、拒答、截斷、安全過濾、超預算、連不上、供應商錯誤、例外，一律算 escalate（失敗不放行）。理由截到 300 字。
3. **執行層**（`ToolExecutor`，在政策判斷之後）：
   - `allow` → 用可信 kwarg `_approved_by="auto_reviewer"`（`tools/execution_context.py`，Guard #45 基準只新增）交給工具；terminal 遇到 `REVIEW` 只認這個值或人類卡的 `_hitl_preapproved=True`，自己只重新分類（地板、黑名單、模式），不再呼叫審查員。模型自己在參數裡帶 `_approved_by` 會被可信情境覆寫，公開合約也會濾掉。
   - `escalate`、CONFIRM、HUMAN_ONLY → 有人在場跳同一張人類審批卡；卡上有完整指令（A7）、審查理由（`review_reason`）與「只有人能核准」標記（`human_only`）。無人在場拒絕，終端也不落到 terminal 的遺留通道。
   - `deny` → 直接拒絕，不跳卡。
   - **無人在場不送審、直接拒絕**：高風險工具一律如此（沿用「無人在場沒有審批通道＝不執行」，`headless_tool`），不因送審而變寬；終端指令預設如此（決定 8 丙），`autonomy.json` 的 `auto_review.headless_terminal`（預設 false）只管終端，開了才送審。
   - **審查員不能推翻底線**：`allow` 只滿足 `REVIEW`。地板在判斷層先回 HUMAN_ONLY，審查員根本不會被呼叫；terminal 的 HUMAN_ONLY 只認 `_hitl_preapproved=True`。
   - 互審 `policy_command_gate` 多一個 `reviewer` 接縫：Auto 下 `REVIEW` 級驗證指令先問審查員，allow 才跑、deny 回 `denied by the Auto reviewer`、escalate 照 CONFIRM 問人（無人在場等人確認，B1 不變）。MCP MRTR 只在 DENY 時拒絕，`REVIEW` 照常通過、不重審。
4. **設定**：`autonomy.json` 的 `auto_review` 區塊（`AutoReviewConfig`：`model`、`max_tokens`、`timeout_s`、`headless_terminal`）。`model` 預設空白；沒設模型或設定無效時，每次審查都以「審查員未設定」轉人（有人在場）或拒絕（無人在場），不呼叫模型。從已設定金鑰的模型中選是 U2c-6 的畫面（見「前端」）。
5. **P7**：同一對話（session；沒有 session 用 request id）被審查員擋下（escalate 或 deny，含失敗）連續 3 次或累計 20 次，之後一律轉人、不再呼叫審查員；allow 只把連續次數歸零。計數只在行程記憶體（`ReviewBreaker`）：重啟歸零，超過 4,096 個對話淘汰最舊的，從不落地（無痕一律 Manual，不會送審）。互審的人類閘門不另外計數。
6. **費用與稽核**：`complete_once` 把用量記在被審的靈魂身上，provider 標籤 `auto_reviewer`；超預算時不送出、算失敗（轉人或拒絕）。`audit_service.log_decision` 記 `REVIEW_<判決>` 與固定原因碼（例如 `model`、`unconfigured`、`headless_terminal`、`p7_breaker`、`failed:timeout`），不記使用者訊息，也不記模型寫的理由。
7. **④甲的資料外送範圍**：最近 3 則人類訊息（各截到 1,000 字）＋這個動作的完整參數＋shell 種類與在場與否，送到所設模型的廠商；靈魂用其他廠商也一樣。無痕對話一律 Manual、不會開 Auto，所以不會送（人類 10/12 12:44，見「無痕對話」）。本機只記用量，不記內容。
8. **錯誤碼**：`AUTO_REVIEW_DENIED`（1118）、`AUTO_REVIEW_HEADLESS_TERMINAL`（1119）、`AUTO_REVIEW_UNAVAILABLE`（1120），放在工具錯誤的 `touka_error_code`；翻譯由 U2c-6 補上（缺 key 時仍用 `msg` 回退）。

不放寬總表：事先核准的「Auto 經審查員放行」（決定 3、master §7-4）在表上是三格，都在有人在場時：SafeAuto→Auto 的 `curl`（終端）、`delete`、未列名 MCP（dangerous 工具）；無人在場的同樣動作一律不送審、不變寬，F5、F6 與地板仍是 HUMAN_ONLY，測試釘住。

已知限制：審查員的準確率、拒答率、解析失敗率、延遲都沒有實測（禁令 61，留到下一段）；互審驗證指令被審查員轉人時，那張卡不顯示審查理由；先寫檔再執行、shell 變數繞過文字地板仍是已知限制（第 5 段的隔離環境才根治）。

驗證：`test_auto_reviewer_security.py`（判斷層，85 個）、`test_auto_review_execution_security.py`（執行層，48 個）；`test_auth_mode_not_wider_security.py` 改列審查員那三格；既有 Auto 終端與 dangerous 工具的期望由 CONFIRM 改 REVIEW。全部用假的 Completer。

回復：撤 U2c-3 的程式、測試、錯誤碼、openapi 與 Guard #45 基準那兩項，本節標「已撤回」。撤回後 Auto 的終端指令與 dangerous 工具回到要人核准（CONFIRM），不會變寬。

#### 心跳（U2c-5）

總覽第 5 步的心跳入口：一律無人在場，所以效果表只看右欄。

決策：

1. **模式對應**（決定 8 丙）：`routers/heartbeat.py` 的 `_SECURITY_LEVEL_TO_AUTH`。

   | `security_level` | 模式 | 無人在場時 |
   |---|---|---|
   | `safe` | Auto | read_only、safe 類（寫自己空間的檔、存記憶、更新任務）照跑；終端與高風險工具直接拒絕，不呼叫審查員（U2c-3 的 `headless_terminal` 預設 false） |
   | `full` | YOLO | 黑名單與地板以外照跑 |
   | 其他（含大小寫不對） | Manual（最嚴，修正 U2c-4 暫時的 Auto） | read_only 照跑；其他工具拒絕；終端照舊走 terminal 的遺留通道（沒有 ToukaUI 就拒絕） |

   `/awaken` 明確帶三模式 `auth_level` 時照舊優先。UI 重跑 `/retrigger` 寫死跟 `safe` 一樣（直接引用對應表）。
2. **KF-4（心跳拿到工具）**：`_execute_single_pulse` 在 `dispatch_request` 前跑 ToolLayer，模型第一次在心跳裡看得到工具（事先核准的變寬，master §7-4）。脈衝自己設 `identity`、`heartbeat_run`（`/awaken`、`/retrigger` 已設，呼叫端漏設也補上）、`is_toukasoul`（`soul_service.is_toukasoul`），靈魂 `config.json` 有 `enabled_tools` 時照 SoulLayer 設 `desired_tools`，所以心跳看到的工具不比前景對話多。ADR-0242 KF 表 KF-4 改「已修」。
3. **ToolLayer 保留設定時決定的模式**：帶 `heartbeat_run` 時採 ctx 的模式（`normalize_auth_mode`，缺值或看不懂是 Manual），不採 session row。家對話共用人類的 session，人類在 AuthSwitcher 選的模式不套到無人的排程 run；session 的 `active_mcp_servers` 照讀。
4. **不跑 MonitorLayer**：它會把 session 換成請求上的值（心跳的請求沒有，會變成空）、用對話訊息覆寫 `/awaken` 設好的原意，有 session 時還會清掉中斷旗標（心跳的中斷旗標只在每次背景 run 起點清）。舊版的 MonitorLayer 還會把靈魂降成 Manual；ToolLayer 已不需要它提供的任何東西。
5. **仍受底線與無人在場約束**：心跳一律無人在場（`heartbeat_run`）。地板 F1–F4、F7 在三種模式都是 HUMAN_ONLY，無人在場直接拒絕，所以 YOLO 心跳也跑不了 `docker compose down`、`cat .env`；Auto 心跳的終端與高風險工具不送審、直接拒絕；`ask_user` 不廣告。

不放寬總表多一組 KF-4（`test_auth_mode_not_wider_security.py`）：以前心跳送不出任何工具，每格都等於拒絕；變寬的格子必須恰好是核准清單（safe 4 格、full 12 格、看不懂 6 格），而且每格都不比對應舊等級（SafeAuto、FullYOLO、看不懂的舊映射 SafeAuto）在無人在場時寬。

驗證：`test_heartbeat_modes_security.py`（8 個；從 `/awaken` 入口經脈衝的 ToolLayer 到真的 ToolExecutor：safe 的 safe 類照跑、終端與 delete 拒絕、審查員沒被呼叫；full 跑非地板指令、擋兩條地板；看不懂是 Manual；session 存的模式不套用；不跑 MonitorLayer；靈魂 enabled_tools 收斂；漏設 `heartbeat_run` 時脈衝自己補上）；`test_heartbeat_tool_schema_pinned.py` 改成「有工具」；`test_heartbeat_awaken_hardening_security.py`、`test_hitl_headless_presets.py` 的看不懂改 Manual（比原本嚴）。

回復：撤 U2c-5 的程式與測試，本節標「已撤回」，ADR-0242 的 KF-4 改回「第 2 段」。撤回後心跳又沒有工具（KF-4 回來），看不懂的值回到 Auto。

#### IDE（U2c-7）

總覽第 5 步的 IDE 入口：ToukaIDE 有自己的 MCP 伺服器與確認視窗，不經 Proxy 的判斷層；這裡只決定 IDE 自己的模式，以及 Proxy 怎麼把「人類已在 Touka 卡片核准」傳過去。

決策（P9、決定 9 甲）：

1. **兩種模式**：IDE 沒有能叫審查員的連線，`ToukaIDE/src/mcp/authPolicy.ts` 只剩 Manual／YOLO。IDE 自己那份 SafeAuto 前綴清單（含 `npm install`、`cat `，前綴比對、不看串接）刪掉，ReadOnly 選項也拿掉。IDE 的模式只在記憶體、重啟回 Manual，沒有舊值要對照；`toukaide.toggleYolo` 改切到 YOLO。IDE 聊天送給 Proxy 的 `auth_level` 變成 `Manual`／`YOLO`，Proxy 本來就不拿它當模式來源（U2c-4）。
2. **黑名單直接拒絕**：命中 IDE 黑名單（git 的 main／master、checkout、push、reset，docker stop／restart／rm，rm -rf、format、drop table）在兩種模式都拒絕，不再變成確認視窗，人類核准標記也解不開。原本只給黑名單用的「⚠️ 危險」確認視窗一起拿掉。
3. **人類核准過的動作不再問第二次**（決定 9 甲，事先核准的變寬）：
   - **來源**：ToolExecutor 只在人類於 Touka 審批卡核准（可信 `_hitl_preapproved=True`）時把 `McpRequestContext.human_approved` 設成 True；審查員放行（`_approved_by="auto_reviewer"`）、YOLO 放行、模型在參數裡自帶的同名欄位都是 False。`ProtocolManager` 的 ToukaUI 分支把它交給 `call_ide_tool`。
   - **格式**：`call_ide_tool(human_approved=True)` 在 tools/call 的 `_meta` 放 `"touka.dev/human-approval": {"v": 1, "nonce", "exp", "mac"}`（`utils/ide_human_approval.py`，純函式＋常數，禁令 18）。`nonce` 是 16 bytes 隨機 hex；`exp` 是現在＋60 秒（epoch ms）；`mac` 是 HMAC-SHA256，金鑰是這次 IDE session 的 discovery bearer（ADR-0202），內容是 `touka-ide-human-approval/v1`、nonce、exp、工具名、完整參數 canonical JSON（鍵排序、緊湊、不轉 ASCII）的 SHA-256，用換行串起來。
   - **驗證**（`ToukaIDE/src/mcp/humanApproval.ts`）：只讀 `_meta`，而工具只有經 loopback＋bearer 的 `/mcp` 才叫得到。格式不對、過期、`exp` 超過現在＋120 秒、nonce 用過、MAC 對不上（換工具、換參數、換 session 金鑰）都當成沒有標記；通過就記下 nonce 到過期為止，只能用一次。通過時跳過 Manual 的確認視窗（終端、git commit）與寫檔的 diff 審核；YOLO 本來就不問。
   - **範圍**：只限人類卡片核准過的那一次呼叫；其他情況照 IDE 自己的模式（Manual 跳視窗）。
4. **核准的指令只跑一次**：`TerminalHandlers.ts` 原本先 `terminal.sendText`（在 VS Code 終端跑一次）再 `spawn`（擷取輸出又跑一次），git commit、npm install 這類會做兩次。現在只留 `spawn`；輸出照舊寫進 `@terminal` 用的 `terminalOutputStore`。

不放寬總表（IDE 的格子，`ToukaIDE/src/__tests__/authPolicy.test.ts`；`test_auth_mode_not_wider_security.py` 只管 Proxy 的決策）：舊四級對應兩種模式 × 有無合法標記 × 5 個代表動作。變寬的格子恰好是 ③（ReadOnly→Manual：npm test、curl、寫檔、git commit 改成問人後可跑）與決定 9 甲（有合法標記時 Manual 不再問：同樣 4 個動作，以及 SafeAuto→Manual 的 curl、git commit）。黑名單在 YOLO 由「問人」改成「拒絕」，舊 SafeAuto 自動跑的 npm test 與直接寫檔在 Manual 改成要問。

已知限制與發現：

- 金鑰就是 discovery bearer：能讀 discovery 檔（`<tmp>/gemini/ide/`）的同一使用者行程也能自己算出標記。這和 bearer 本身的信任界線相同（這種行程本來就能直接跑指令）；審查員與 YOLO 的路徑不會產生標記。
- **目前 Proxy 只把 `openDiff`／`closeDiff` 轉給 ToukaUI**（`protocols/manager.py`），IDE 卻沒有實作這兩個工具，IDE 的 `execute_terminal`／`write_file`／`git_commit` 從 Proxy 的自動發現路徑叫不到。標記的產生、傳遞與驗證已接好，並由真 Node 端到端測試證明；正式環境要等 Proxy 轉送 IDE 工具（新能力，不在 2c）才會真的省掉第二次確認。
- 終端指令不再出現在 VS Code 的整合終端。

驗證：Vitest `humanApproval.test.ts`（8）、`mcpServerApproval.test.ts`（9，真 ToukaMcpServer＋bridge：沒有標記跳視窗；合法標記不跳且只跑一次；偽造、對不上與重放照常跳；沒 bearer、錯 bearer 401，非本機 Host 403；黑名單帶合法標記仍拒；git commit、寫檔、YOLO）、`terminalHandlers.test.ts`（3）、`authPolicy.test.ts`（7，含不放寬總表）、`modalApproval.test.ts`（4）；pytest `test_ide_human_approval_security.py`（11：共用向量、綁住金鑰／工具／參數、只在人類核准時附標記、Manager 傳遞、卡片核准才標、被拒不呼叫、YOLO 與審查員放行不標、模型自帶欄位無效）、`test_mcp_modern_companion.py` 加 1（Python 產生、真 Node 驗證）、`test_ide_handler.py` 改釘 `meta=None`。

回復：撤 U2c-7 的 IDE 與 Proxy 程式和測試，本節標「已撤回」。撤回後 IDE 回到四級（含 SafeAuto 清單、黑名單只確認）、核准的指令又會跑兩次，人類核准過的動作在 IDE 會再問一次（不變寬）。

#### 前端（U2c-6）

前面各小節的後端行為在 ToukaChat 的呈現。資料存取一律包在 Hook 裡（DIP），沒有新事件種類。

1. **模式選單**（`AuthSwitcher`、`ChatProvider`）：只剩 Manual／Auto／YOLO，選 YOLO 仍二次確認。存檔後重新讀對話清單，顯示後端實際存下的模式；被降成 Manual 時提示「只有管理員或這個靈魂的擁有者能開」（呼應「改名、舊值讀取對照與模式授權」第 4 點）。較舊的回應晚到時忽略。無痕對話鎖定顯示 Manual 並說明；U2c-9 改寫說明，並在要求 Auto／YOLO 時不存檔、toast 說明原因（見「無痕對話」）。
2. **審批卡**（`ToolApprovalCard`、`stream_tree`、`tree_history`）：`tool_approval_request` 帶 `review_reason` 時顯示「審查員轉給你的理由」，帶 `human_only` 時顯示「只有你能核准」與說明；解算事件後欄位保留，型別不對不顯示。
3. **審查員設定**（`SecurityTab` 的 `AutoReviewerSettings`、`useAutoReviewSettings`）：選項只來自新端點 `GET /v1/autonomy/reviewer-models`（只列供應商金鑰已設定、啟用中的一般模型；靈魂、自訂模型、不用金鑰的端點型供應商不列；Owner／Debugger／admin），預設未設定，建議 Haiku 5.5 只是提示文字、不自動選；存檔沿用 `POST /v1/autonomy` 寫 `auto_review.model`（只有人類 admin 能改，R-2c）。說明照 ④ 的通用寫法（不寫廠商名），並註明費用算在被審的靈魂、沒設模型時一律轉人；已存的模型沒有金鑰時警告。
4. **心跳排程**（`HeartbeatScheduler`、`heartbeat_mode`）：safe／full 的標籤兩語改成 Auto／YOLO，看不懂的值顯示 Manual（與「心跳」的對應表一致）。
5. **直連標示**（P10，`DirectConnectionNotice`）：新增連線表單標明直連不經 Proxy，沒有工具、不受權限模式管、不記帳；聊天一律經 Proxy。
6. **錯誤碼翻譯**：1118～1120 兩語。

審查員設定的說明原本照 ④ 原文寫「無痕對話也一樣」；人類 10/12 12:44 決定無痕一律 Manual 之後，U2c-9 已拿掉這半句。

驗證：Vitest `AutoReviewerSettings.test.tsx`（9；U2c-9 再加 1）、`useAutoReviewSettings.test.ts`（3）、`ChatProvider.authLevel.test.tsx`、`AuthSwitcher.test.tsx`（含無痕鎖定）、`ToolApprovalCard.test.tsx`、`stream_tree.approval.test.ts`、`heartbeat_mode.test.ts`、`HeartbeatScheduler.test.tsx`、`AddConnectionForm.test.tsx`、`SecurityTab.test.tsx`、`error_handler.test.ts`；pytest `test_reviewer_models_endpoint.py`（3）。Vitest 全量 1,537。

回復：撤 U2c-6 的前端、端點、openapi 與測試，本節標「已撤回」。撤回後選單在降級時要重新載入才顯示 Manual，審批卡看不到審查理由，審查員模型只能手改 `autonomy.json`。

#### R-2c 修正（U2c-R1b）

對抗式複審 R-2c（三位審查員 r1、r2、r3）提出的問題，這一節記 U2c-R1b 修的部分。

1. **審查員設定只有人類 admin 能改**（r3 #1）：`POST /v1/autonomy` 原本整份用 `require_privileged`，非 admin 擁有者帶 X-Agent-ID 切成自己的靈魂、靈魂本身、Debugger 都能改 `auto_review`。`model` 決定 ④ 的資料送到哪家廠商；`headless_terminal` 打開等於讓所有靈魂無人在場的終端指令都送審（心跳、子代理、派工、SNS 都算），超出事先核准的「只在有人在場時」。現在 `auto_review` 有變更時另外過 `require_admin_human`（admin 代打也不算；router 只用 gateway 的別名，ADR-0239），其他預算欄位照舊 Owner／Debugger／admin（前端整份送回原值不算變更）。新設定的 `model` 必須空白或在 reviewer-models 清單內，否則 422 `VALIDATION_ERROR`（`field="auto_review.model"`）；舊模型後來沒金鑰時，不擋沒改模型的存檔（審查時照樣轉人）。不放寬總表補上開關打開才多出的格子（SafeAuto→Auto 與心跳 safe 的 `curl`，無人在場），標成「需人類決定才可打開」；預設維持 false。
2. **重跑只給 admin 或人類擁有者、只在自己的對話**（r3 #2）：`/retrigger` 一律以 Auto 執行，但 `is_owner` 對 Debugger 一律放行，也沒檢查對話屬於誰。現在多三道：過 session 寫入同一個模式授權（`authorize_auth_mode`：只有 admin 或人類擁有者，Debugger 旁路與代理身分都不算）；對話的 `agent_id` 必須等於要重跑的靈魂；呼叫者要通過 `_can_access_session`（和第一道用同一個 `is_owner`，屬多餘保險）。`resource_manager.is_owner` 的全域行為不變。
3. **SNS 外人訊息與附件不是人類原意**（r1 #3）：SNS 存進對話與送進 pipeline 的訊息都加固定開頭 `【SNS 外部訊息】`，原意擷取時排除。前端內嵌的文字附件與已上傳檔案提示選擇直接排除（從第一個附件標記起整段不算）：附件不是人類原話，另開欄位等於多一個注入面；少了這份證據，審查員只會更常轉人。附件可能偽造結束標記，所以不找結尾。
4. **子代理不送代理人轉述**（r3 #4）：子代理原意改成 `source="subagent"`、沒有訊息、`parent` 帶父代理的人類原意；派工目標只留在 `ctx["subagent_goal"]`，不再進審查員的輸入（`_mark_subagent_run` 簽名不變，Guard #45 基準不動）。
5. **無痕看兩個欄位**（r1 #8）：純函式 `auth_mode.request_is_incognito`，`request.incognito` 或 `ui_persistence.mode == "incognito"` 任一成立就算無痕；`proxy.py` 據此設 `ctx["incognito"]`，ToolLayer 也直接看請求。HTTP 請求兩欄不一致時 schema 本來就回 422，這裡防的是內部組出的請求。
6. **模式授權補成正式測試**（r3 #3）：靈魂本身、DebugAgent 存 YOLO → Manual；DispatchAgent、子代理 → 403；admin 代打靈魂 → YOLO。
7. **新對話還沒發第一句就切模式**（r2 #1）：前端切模式時，對話還沒有 `agent_id`、而且目前選的是靈魂，就一起帶上存檔，後端照樣驗擁有權；非 admin 擁有者因此能直接存成 Auto／YOLO，非擁有者照樣降成 Manual。選的是一般模型時不綁（一般模型沒有工具，模式對它沒有意義），否則之後選靈魂時會被同步 effect 拉回預設模型（R-2c ② 複審）；還沒有訊息的對話，第一句前換模型也不會被拉回。
8. **`get_session_by_id` 的舊值對照**（r2 #2）：補斷言測試，拿掉對照會紅。
9. **存下的模式只給綁定的 agent**（r2 第 3 次複審 #3）：照 A 存下 Auto 的空對話，第一句前換成另一個認得的 agent B（或自己組請求點名 B），這一輪 ToolLayer 會照 Auto 跑 B。現在 ToolLayer 把 session 的 `agent_id` 經 `agent_service.identify_agent` 換成正式 ID（舊 `ToukaSoul` 前綴也對得上），和前景代打後的 ctx `agent_id`（ADR-0154）比，一致才採用 Auto／YOLO，不一致或 session 沒綁 agent 一律 Manual（純函式 `auth_mode.mode_for_acting_agent`）；心跳、子代理的來源與最後的無痕覆蓋不變。一般模型認不出 agent 時，MonitorLayer 走 session 連結回到 A、SoulLayer 用 A 的大腦，模式照 A，屬原本行為；一般模型與群聊的身分是人類、沒有工具，變 Manual 沒有影響。前端送出第一句前，空對話綁的 agent 和選的模型不同時，先重綁存檔、顯示後端存下的模式再送出。測試：`test_auth_mode_authority_security.py` +7、Vitest +4；五個既有測試讀的是沒綁 agent 的 session，改成綁上這次執行的 agent（斷言不變）。

驗證：新檔 `test_autonomy_reviewer_authority_security.py`（21）；`test_auth_mode_not_wider_security.py` +2、`test_heartbeat_retrigger.py` +6、`test_user_intent_presence_security.py` +5、`test_sns_gateway.py` +2、`test_incognito_manual_only_security.py` +3、`test_auth_mode_authority_security.py` +10、Vitest `ChatProvider.authLevel.test.tsx` +2。既有測試只改輸入或改成更嚴：重跑的四個既有測試改用人類擁有者、對話帶 `agent_id`（斷言不變）；子代理原意的四個既有測試改成不含派工目標。突變 32 個、31 個變紅；存活的是拿掉 `_can_access_session`（和 `is_owner` 同一判斷，預期如此）。

回復：撤 U2c-R1b 的程式、測試與文件，本節標「已撤回」。撤回後 Debugger 與靈魂又能改審查員設定、重跑又能指定別人的對話、SNS 外人訊息又會被當成人類原意。

#### R-2c 修正（U2c-R1a）

底線與執行層的部分（r1 #1、#2、#4～#7）。

1. **甲：受保護根目錄**（r1 #1，HIGH）：資料目錄（`resource_manager.get_base_path()`）、repo 根、`.env` 所在目錄、設定目錄（`get_config_dir()`）。寫入類指令（含 `git -C`、`tar -C`、解壓縮、`find -delete`／`-exec`）的目標是它們本身、底下或上層，就是底線：資料 F1、其他 F4。遞迴讀放金鑰的目錄（`.env` 所在目錄、設定目錄）或它的上層算 F2。
2. **乙：使用者工作區**（`resource_manager.get_workspace_mount()`：Docker 裡是容器掛載點 `get_container_mount()`，不在 Docker 時是設定值（DB 快取或 `HOST_WORKSPACE_ROOT`），空值或相對路徑當作沒有工作區；SSOT 是 ResourceManager 原有的容器掛載點設定）：沒有 Touka 的使用者工作區，裡面照常寫入和讀取；它本身和上層只擋刪除、搬走或清空，以整句（含整條管線）判斷。清空的寫法：`{ws}/*`、`{ws}/**`、`*.*`、`rsync --delete`、`robocopy /MIR`／`/PURGE`、`git clean`，以及直譯器裡的刪除呼叫（`rmtree` 類、`rmSync`／`rmdirSync`、`remove_tree`、`rimraf`，字詞比對；指令裡只要出現刪除呼叫就算寫入）。
   - 不在 Docker：工作區和根目錄都先 resolve 再比，經過別名工作區（symlink／junction）的路徑換成真實位置再判斷，resolve 失敗就整片保護；工作區和受保護根目錄重疊（本身、底下或上層），整個交給甲。
   - Docker：掛載點只有在 `/proc/self/mountinfo` 證明它的宿主來源和資料、`.env` 所在目錄、設定目錄都不重疊時，才豁免「在 repo 根底下」（Docker 的 `/app/workspace` 掛的是使用者專案）；證明不了就當成受保護根目錄的上層，不豁免，遞迴讀它也算 F2。mountinfo 經 ResourceManager 讀一次並快取，解析是純函式 `mounts_separate`：工作區要有自己的獨立掛載項，而且它和底下每個子掛載都要和錨點分開；同裝置時 root 欄位不分大小寫、互為本身或上下層算重疊；不同裝置只有兩邊都是實體區塊裝置（major≠0）才假設不重疊，有一邊是 major 0（9p、virtiofs、FUSE、overlay）就算證明不了；唯一例外（U2c-R1c）：錨點是實體區塊裝置上的 Docker named volume（root 欄位符合 Docker 的資料佈局 `…/volumes/<名稱>/_data`；佈局字樣寫成模組常數，是 Docker 的慣例、不是部署路徑），而工作區在另一個裝置上時，算分開。理由只在 Docker Desktop 成立：主機共享（9p、virtiofs、FUSE）看不到 VM 磁碟裡的 volume（ghcr compose 的設定目錄 `proxy-config` 就是這個情形）；在 Linux 原生上沒有這層隔離，這條例外靠的是其他 bind 錨點（host_root、資料）仍要各自證明分開。每個錨點都要各自證明，只有 volume 那個分開不夠；解析失敗或找不到掛載項也算證明不了。
   - 第 5 輪拿掉的特例：Touka 標記檢查、工作區不存在或沒權限時整片保護、容器旗標（R-2c ② 第 3 次複審 HIGH 之後改寫成甲、乙兩句）。
3. **F2 遞迴讀取**：目標是放金鑰的目錄或它的上層、證明不了的 Docker 掛載點、以 `..` 開頭，或心跳的 `ToukaHeartbeat(/config)` 目錄。遞迴讀取的指令：find／rg／ag／ack／Compress-Archive、tar 建立封存、grep／cp／scp／rsync／zip 帶 -r／-R／-a、gci／Copy-Item 帶 -Recurse、findstr／xcopy／robocopy 帶 /s、/e、/mir；複製的目的地不算讀取。心跳 `config.json` 算 F2。
   - 拿不掉的特例：單獨的 `/` 或磁碟根不算上層（分不出除號，刪根目錄由 F7 擋）；每個指令開頭的程式路徑不算寫入目標（執行不等於寫入）；F2 只看金鑰目錄本身和上層、不看底下；原有名稱規則（data、soul、ToukaProxy/src、政策檔、`.env` 檔名、心跳 config）與相對路徑 `..` 保留。
4. **F7 繞法**（r1 #2）：前綴指令（sudo／doas／env／nice／ionice／stdbuf／timeout／time／xargs／nohup／exec／command／busybox／setsid／chrt／taskset）連同參數一起跳過；find 帶 -delete 或 -exec rm 算刪除；`A | xargs rm`／`Remove-Item` 沿用 A 的目標；powershell／pwsh 沒帶 -File 時整段當指令判斷。
5. **其他**：F5 的動詞由 executor 從 `args["action"]` 取，不受設定檔 `action_param` 左右（r1 #4）；審查員與執行共用 `utils/command_safety.py` 的 `effective_shell`，非 Windows 一律 bash（r1 #5）；Windows 路徑去掉 `\\?\`／`\\.\` 前綴、資料串流與結尾的點和空白（r1 #6）；IDE「由人核准」標記只在審批卡沒有截斷任何參數時產生（r1 #7）。

驗證：`test_hard_floor_roots_security.py`（366；對照表 176 格，U2c-R1c 加 ghcr 加 Docker Desktop 的正例與 5 個反例）、`test_floor_executor_wiring_security.py`（19）。偏嚴的誤判與仍抓不到的寫法見「代價與剩餘風險」。

回復：撤 U2c-R1a 的程式與測試，本節標「已撤回」。撤回後 r1 #1 的 HIGH 回來：YOLO 下對 repo 根、資料目錄的上層做刪除、搬移、覆寫會直接放行。

## 使用者看得到的改變

- Manual 下互審要跑指令時有兩張卡：先 `request_review`，再那條指令（卡上是完整指令）。
- 2b 時的 SafeAuto 互動會話多一張指令卡（以前這條指令一定不會跑）；2c 起 Auto 下這條指令先交審查員，審查員轉人才跳卡。
- 長指令在審批卡上換行完整顯示，不再單行省略。
- 無人在場、沒等到決定或被拒時，任務標「等人確認」，不會因此被扣次數或鎖死；人回來在對話裡重跑 `request_review` 就會跳卡。
- 2c：模式選單只剩 Manual／Auto／YOLO。舊 ReadOnly、SafeAuto 對話打開是 Manual，舊 FullYOLO 是 YOLO；不是管理員或擁有者選 Auto／YOLO 會被改回 Manual 並提示；無痕對話鎖定 Manual。
- 2c：用 Auto 前要先在安全設定選審查員模型，沒設時要審的動作一律問你。審批卡會寫審查員轉給你的理由，碰到底線的卡標「只有你能核准」。
- 2c：YOLO 下刪改 Touka 資料、讀金鑰、改權限設定、改原始碼、系統破壞仍要你核准；你不在場時直接拒絕。
- 2c：心跳排程的 safe 顯示成 Auto（不在場不跑指令）、full 顯示成 YOLO；心跳第一次能用工具。
- 2c：IDE 只剩 Manual／YOLO，黑名單直接拒絕；終端指令不再出現在 VS Code 整合終端。新增連線表單標明直連沒有工具、不受權限模式管。

## 代價與剩餘風險

- 看板 `get_board` 不顯示審查欄位（`review_status`、`review_locked`），人從看板分不出「等人核准指令」和「永鎖、軟任務等其他 `BLOCKED_FOR_HUMAN`」，只能看 `request_review` 的回傳。
- 正式流程每個任務只有一條 `verification_cmd`，`:verify:<n>` 的 n 實際都是 1；多條指令各自一步只由測試覆蓋。
- ~~子代理裡其他需要人核准的工具，卡片仍送不到人、要等 180 秒逾時才被拒~~：已在 U2c-2 修好，子代理一律無人在場、立刻拒絕（見「使用者原意與在場判斷」）。
- 審批卡的指令超過 8 KB 仍會截斷，人看不到截掉的部分。
- 只用 sandbox／mock 驗證；沒有啟動 Touka，也沒有打任何 API。

2c 的剩餘風險：

- **文字地板只能盡力而為**：先寫檔再執行（先寫腳本、再 `python x.py`）、shell 變數（`$X`、`$env:`）與萬用字元（`.e*v`）、編碼、字串拼接、`cd` 之後的相對路徑都能繞過；檔案工具的路徑不經地板（靠 `is_protected_path` 與 vault 監獄）。Auto 下動態 shell 寫法只有人能核准、審查員被要求把代理人自寫的腳本轉人，都只是減少，不是根治；根治要等第 5 段的隔離執行環境。
- **審查員準確率沒有實測**：準確率、拒答率、解析失敗率與延遲都只用假的 Completer 驗證（禁令 61），列入下一段的實測待辦。
- **workload OAuth 刻意維持關閉**（`HEARTBEAT_WORKLOAD_OAUTH_ENABLED = False`，見「使用者原意與在場判斷」第 6 點），要不要打開待人類決定。
- **無痕對話一律 Manual**（人類 10/12 12:44 已決定，見「無痕對話」）：無痕不能用 Auto／YOLO，非唯讀動作都要問人；程式與前端強制由 U2c-9 完成（`test_incognito_manual_only_security.py`）。
- **底線偏嚴的誤判**（R-2c U2c-R1a，只是多問人、不放寬）：用絕對路徑寫進 repo 底下（mountinfo 證明不重疊的 Docker 工作區掛載點除外，例如 `python /app/ToukaProxy/scripts/x.py`）；用絕對路徑寫進 `/app/hf_cache` 這類在 repo 根底下、程式裡沒有 SSOT 的快取掛載點，仍算 F4；Docker 讀不到 mountinfo 時，掛載點整片保護；Linux 區分大小寫，但 `/Projects` 和 `/projects` 會被當成同一個；Docker Desktop 上工作區和 repo 在不同磁碟機（每個磁碟機是不同的 major 0 裝置）時，寫入專案要人核准；btrfs 子卷、ZFS dataset 上的工作區或錨點會判成證明不了、整片保護（推論：它們在 mountinfo 是 major 0 的匿名裝置號，屬偏嚴、不是漏洞）；不在 Docker 時把工作區設在 Touka 資料夾裡面，會整片當 Touka 保護（甲的直接結果）；程式不在段首（`sudo /venv/python`）；`find . -type f -delete` 沒有檔名篩選；`Copy-Item -Force` 加受保護目錄；`grep -a` 加受保護目錄。
- **底線仍抓不到的寫法**（R-2c U2c-R1a 之後）：WSL 的 `/mnt/x` 路徑、8.3 短檔名、自訂 `HEARTBEAT_CONFIG_DIR`、不在表上的帶值前綴選項、直接執行受保護目錄裡的腳本；不在 Docker 時，第一次非同步讀取 DB 之前，工作區用的是 `HOST_WORKSPACE_ROOT`；「不同裝置不重疊」的假設只限兩顆實體區塊裝置；mountinfo 只讀一次，重新掛載要重啟；先 `cd` 再用相對路徑清空工作區；單獨 `/` 的覆寫（例如 `cp -r x/. /`）；直譯器裡的刪除呼叫只靠字詞比對，混淆寫法抓不到；掛載點裡原本就有、指向 Touka 的連結看不到；工作區本身是 Docker 資料區的 FUSE／NFS 視圖（把 VM 磁碟上的 volume 再經共享掛回來）這種極端設定，named volume 例外會判成分開、抓不到；例外只看 mountinfo root 字樣，使用者自己的 bind 來源剛好叫 `…/volumes/<x>/_data` 時也會被當成 volume，前提一樣極端（U2c-R1c）。
- **刪除目標從檔案或 stdin 進來**（R-2c ① 附註）：例如 `xargs rm < list`，指令文字看不到目標，底線抓不到，和變數、`cd` 後相對路徑、先寫檔再執行同一類；真正的防線是隔離執行環境。
- **IDE 標記要等 Proxy 轉送 IDE 工具才生效**：目前 Proxy 只轉 `openDiff`／`closeDiff`，IDE 的終端、寫檔、git commit 從 Proxy 叫不到；標記只由真 Node 端到端測試證明。
- **IDE 終端不再顯示核准的指令**：只留 `spawn` 擷取輸出（避免跑兩次），VS Code 整合終端看不到。
- ~~`POST /v1/autonomy` 的權限含 Debugger~~：已在 R-2c 修好，`auto_review` 只有人類 admin 能改；成本上限等預算欄位仍是 Owner／Debugger／admin（見「R-2c 修正」）。
- **`headless_terminal` 預設關、需人類決定才可打開**：打開後所有靈魂無人在場的終端指令都送審、審查員放行就執行（心跳、子代理、派工、SNS 都算），超出事先核准的變寬。
- **`/retrigger` 不再給靈魂自己與 Debugger 用**：只剩 admin 與人類擁有者（前端只以人類身分呼叫）。
- **心跳在 YOLO 下會用 MCP 工具**：KF-4 之後心跳看得到 MCP 工具；MCP 工具不經底線分類，YOLO 心跳在無人在場時會照跑（Auto 心跳的未列名 MCP 屬 dangerous，直接拒絕；workload OAuth 仍關閉）。
- **Auto 下 F5 的自帶審批工具會出兩張卡**：`request_skill_install`、`evolution_implement_and_verify`、`request_mcp_creation` 先過 broker 的「只有你能核准」卡，再過工具自己的卡；只是多問一次、不放寬【推論：讀程式，沒有專門測試】。〔ADR-0254 起 `evolution_implement_and_verify` 改為一般分類，Auto 下仍是兩張卡：executor 的「只有你能核准」卡＋部署卡。〕
- P7 計數只在行程記憶體，重啟歸零；~~互審驗證指令被審查員轉人時，那張卡不顯示審查理由~~（ADR-0254 已修）。
- ④甲的資料外送：有人在場的 Auto 對話會把最近 3 則人類訊息（各截到 1,000 字）、動作的完整參數與執行環境送到所選模型的廠商；本機只記用量，不記內容。

## 驗證

- 新測試：`test_review_command_approval_security.py` 8 個（B2）、`test_review_awaiting_human_security.py` 12 個（B1）、`test_approval_card_full_command_security.py` 4 個（A7）、`ToolApprovalCard.test.tsx` 1 個。改動：`test_subprocess_hardening_security.py`（政策閘改 async 與 approver，現 29 個）、`test_dispatch_subagent.py` 加 1 個（子 ctx 帶 `subagent_run`）；`test_evidence_collector.py`、`test_exec_cleanup_off_loop.py` 的放行閘改 async，`test_hitl_broker_executor.py` 改從 `approval_flow` 匯入預覽函式，斷言都沒動。
- 先紅後綠：B2 紅 4、A7 紅 3、卡片 1、B1 兩輪各紅 4、6。突變證明：U2b-1 13 個、U2b-2 17 個，改壞都會變紅、還原後無殘留。
- golden 零差異。全量 pytest 與守衛結果見 HANDOVER。
- 2c 新測試（全部用假的 Completer、不打 API）：`test_hard_floor_security.py` 111、`test_auth_mode_three_modes.py` 69、`test_auth_mode_not_wider_security.py` 6、`test_auth_mode_authority_security.py` 28、`test_user_intent_presence_security.py` 28、`test_auto_reviewer_security.py` 85、`test_auto_review_execution_security.py` 48、`test_heartbeat_modes_security.py` 8、`test_ide_human_approval_security.py` 11、`test_reviewer_models_endpoint.py` 3、`test_incognito_manual_only_security.py` 21（U2c-9）；IDE Vitest 83（14 檔）、ToukaChat Vitest 1,537。既有測試只有事先核准的四項變寬與 `test_kf*` 照 docstring 翻轉，其餘同嚴或更嚴（見各小節「驗證」）。
- 2c 突變證明：U2c-1 24 個、U2c-4 15 個、U2c-2 27 個、U2c-3 56 個、U2c-5 15 個、U2c-7 19 個、U2c-6 33 個，改壞都會變紅、還原後無殘留。各單位 golden 零差異。

## 回復

不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。2b 依相依倒序撤：先撤 U2b-2（B1：固定理由、等人確認分支、結構化回傳），再撤 U2b-1（approval_flow、逐條審批、8 KB 預覽、卡片換行）；SECURITY.md 的 B2、B1、A7 三列與 ADR-0137 的 2026-10-12 補記一起撤，本 ADR 註明撤回哪一節，CHANGELOG 加一列。撤 U2b-1 會帶回 B2（人沒看過指令就執行），不建議。

2c 依相依倒序撤：U2c-9（無痕強制）→ U2c-6（前端）→ U2c-7（IDE）→ U2c-5（心跳）→ U2c-3（審查員）→ U2c-2（原意與在場）→ U2c-4（改名與授權）→ U2c-1（底線），各單位的做法見各小節的「回復」。撤到 U2c-4 時舊程式讀不到 Auto／YOLO（現有資料是測試用，人類已同意）。文件一起撤：SECURITY.md 本段各列、介面文件的「第 2c 段」小段、SSOT_MASTER_MAP 的 2c 列、ADR-0018、0021、0078、0089、0143 的反向註記與索引列（本 ADR 的「取代與修訂」同時標已撤回，否則 Guard #28 會紅）、AGENTS.md §2 回到四級；禁令 64 不刪號，比照禁令 54、62 留墓碑。撤 U2c-1 會讓地板消失，不建議。
