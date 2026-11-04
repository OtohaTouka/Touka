# ADR-0292：Hook（引擎、安全界線、十九個事件與設定頁）

- 狀態：✅ 已實作（丙-1 引擎與四個事件；丙-2 其餘十五個事件〔決定 16、17〕）。維護者 2026-10-28 要求前後端加上 hook 與設定介面、行為盡量和 Claude Code 相同、對不上的先列表；一頁計畫由維護者核准，丙-1 加做對抗式複審。
- 日期：2026-10-28
- 決策者：維護者
- 相關：ADR-0146（回合結束的約定）、ADR-0228／0229（工具執行順序與可信 context）、ADR-0239（`execution_broker`）、ADR-0246（模式、審查員與底線）、ADR-0257（歷史資料界線）、ADR-0269（只執行這一輪列出的工具）、ADR-0272（隔離層級；hook 程式不是代理人撰寫的程式，不經 runner，見決定 4）、ADR-0293（代理人資料夾不跟隨連結；接手本 ADR 已知界線裡和連結有關的幾條）。
- 影響範圍：ToukaProxy 新增 `models/hooks.py`、`services/hooks/`、`middleware/layers/hooks.py`；修改 `services/tool_executor.py`、`handlers/react_core.py`、`services/proxy_service.py`、`services/approval_flow.py`、`utils/tool_events.py`、`services/prompt_blocks.py`、`adapters/content_normalize.py`、`services/continuity_service.py`、`services/context_snapshot_service.py`、`utils/bootstrap.py`、`utils/hard_floor.py`、`utils/touka_dirs.py`、`utils/async_db.py`、`services/group_router.py`、`tools/project_tools.py`、`routers/ui_sessions.py`、`routers/ui_interjections.py`、`services/stream_buffer_service.py`、`services/dispatcher.py`、`services/sns_gateway.py`、`services/authorization_gateway.py`、`services/isolation/profiles.py`、`services/resource_manager.py`、`.gitignore`、`.dockerignore`；新增 `routers/hooks.py`（掛在 `main.py` 的 `CORE_ROUTERS`）、`services/hooks/interjection_hooks.py`；丙-2 新增 `services/hooks/background.py`、`services/hooks/notifications.py`、`utils/secret_text.py`（`services/models/manager.py` 的憑證樣式搬過來），修改 `services/tool_errors.py`、`tools/builtin/schemas.py`、`tools/builtin/interaction.py`、`protocols/mcp/interaction.py`、`main.py`；決定 17 新增 `services/hooks/lifecycle.py`，修改 `handlers/adapters/`（`base.py`、`retry.py`、`openai_adapter.py`、`responses_adapter.py`）、`tools/subagent_tools.py`、`tools/mission_tools.py`、`services/sliding_window_service.py`，以及會改設定的路由（`routers/` 的 autonomy、bots、config、connections、creation、custom_models、group_settings、heartbeat、identity、mcp、models、permissions、setup、skills、system、ui_sessions）。ToukaMemory 新增 `utils/no_follow.py`（`scripts/lint_ssot.py` 的 I/O 白名單加上它，和 `core_file_ops.py` 一樣），修改 `tools/core_file_ops.py`、`protocols.py`、`services/personal_catalog.py`；設定 `config/hooks.json.example`、`errors.json.example`（6001～6003、1222 `PROMPT_STOPPED`）；`openapi.json`。ToukaChat：設定畫面「Hooks」分頁（`components/settings/tabs/HooksTab.tsx`、`tabs/hooks/`（比對規則的提示依事件不同，`HookEventSection.tsx`）、`hooks/settings/useHooksSettings.ts`、設定註冊表與搜尋索引）、審批卡、串流錯誤通知、被擋下的這一輪從對話拿掉並放回輸入框（`lib/stream_tree.ts`、`lib/providers/StreamRegistryProvider.tsx`、`hooks/useChatStream.ts`、`lib/providers/ChatProvider.tsx`）、插話被擋（`hooks/useInterjectionQueue.ts`、`hooks/useInterjectionComposer.ts`）、`lib/tree_history.ts`、`error_codes.ts`、`api.ts`、`lib/types.ts`、翻譯；無痕提示（`hooks/useHooksActive.ts`、`components/chat/IncognitoHookNotice.tsx`、`ChatArea.tsx`）；回覆失敗的標示（`lib/stream_tree.ts`、`lib/providers/StreamRegistryProvider.tsx`、`components/chat/MessageItem.tsx`）。

## 背景

- Claude Code 的 hook 讓管理者在固定時機（使用者送出訊息、工具執行前後、回覆結束等）執行自己的程式或送 HTTP：事件內容以 JSON 交給程式，程式可以擋下、要求確認或補充說明給模型。
- Touka 沒有這個能力。管理員想加「送出前檢查」「工具執行後跑 lint」之類的規則，只能改原始碼。
- 新增「執行管理員設定的程式」是資安改動：程式在 Touka 自己的權限下跑，如果代理人能改到設定或腳本，就等於繞過審批執行任意程式。

## 決定

1. **設定**：全系統一份 `config/hooks.json`（`HooksConfig`，繼承 `BasePolicyModel`），格式照 Claude Code 的 `hooks` 區塊：`{事件: [{matcher, hooks: [handler]}]}`。
   - 預設關閉（`enabled: false`、`hooks: {}`），沒有設定就什麼都不跑、接線點不組事件內容。
   - 只收已接線的事件（`WIRED_EVENTS`），拼錯或還沒接的事件拒收；每個事件最多 20 個 handler。
   - 啟動時壞檔照 `_LIVE_MODELED` 規則停下並指出檔名；執行中讀到壞檔、讀不到或檔案被刪掉，都沿用上一份好的，健康狀態標 `error`（從來沒讀成功過才當成沒有設定）。
   - `hooks.json` 列入底線 F2 的金鑰檔（`hard_floor._SECRET_FILES`）：http hook 的標頭照字面存、可能有權杖，代理人用終端指令讀和寫都只有人能核准、無人在場直接拒絕。和 `users.json`、`connections.json` 一樣只看檔名，所以專案裡剛好叫 `hooks.json` 的檔案（例如其他工具的 `hooks/hooks.json`、`.cursor/hooks.json`）在終端裡碰到也要人核准（寧可多擋，不靠路徑判斷）。
   - 檔案動詞與 MCP 工具不經這條底線。一般身分的檔案動詞到不了設定資料夾（決定 12；檔案動詞也不跟隨連結）；不受專案限制的身分（管理員、Debugger、只有管理員擁有的靈魂）若把專案登記在涵蓋設定資料夾的位置，代理人就能用檔案動詞讀到標頭裡的權杖、改寫 `hooks.json`，由管理員自己負責不這樣登記（和決定 12 的界線相同）。
   - `hooks.json.example` 與 `config/hooks/` 底下的腳本屬 F3 的政策與設定。
   - `config/hooks/` 只放管理員自己的腳本，不進版控也不進映像（`.gitignore`、`.dockerignore`）。
2. **兩種 handler**：
   - 事件內容是 UTF-8 的 JSON；無法編碼的字元（落單的 surrogate）換成 `?`，hook 照樣執行。
   - `command`：`command` 是程式的絕對路徑，`args` 是參數清單，**不經 shell**；事件內容只從標準輸入給。經 `execution_broker.spawn_exec` 啟動，環境只給 `LaunchEnvironmentPolicy.launcher_base()`（不含金鑰），工作目錄是 `config/hooks/`，`close_fds`、獨立行程群組；逾時、等待被取消或出錯時結束整個行程樹；標準輸出與錯誤各只留前 64 KiB。
   - `http`：POST 事件內容，`follow_redirects=False`、`trust_env=False`；逾時是整次請求（含讀回應）的上限，回應讀到 64 KiB 就停止讀取並關閉連線；只收沒有壓縮的回應（送 `Accept-Encoding: identity`，回應有壓縮就算失敗、不解開），64 KiB 算的就是收到的位元組：壓縮過的內容解開前看不出多大，幾百位元組可以解成幾百 MiB。3xx 與非 2xx 都算失敗，先看狀態、不讀內容（有壓縮也照 HTTP 狀態說明）。標頭照字面送出，寫成 `$VAR`／`${VAR}` 拒收（不替換環境變數，避免以為有效或洩金鑰）；標頭名稱必須是 HTTP 的 token（不能有空白、冒號等分隔字元），Touka 自己送的 `Content-Type`、`Content-Length`、`Host`、`Transfer-Encoding`、`Accept-Encoding` 不能設定（不分大小寫）。
   - 逾時預設 30 秒、上限 300 秒。
3. **結果解讀照 Claude Code**（`services/hooks/outcome.py`）：
   - 結束碼 2：UserPromptSubmit、PreToolUse、Elicitation 擋下，原因取 stderr；PostToolUse、PostToolUseFailure 把 stderr 當回饋；其他事件只記一筆說明，沒有效果。結束碼 2 時不讀 stdout 的 JSON。
   - 結束碼 0：stdout 是 JSON 就讀欄位（`decision: "block"`、`hookSpecificOutput.permissionDecision` 的 `deny`／`ask`、`additionalContext`）；純文字只在 UserPromptSubmit 當補充說明；看起來是 JSON 卻壞掉算 hook 失敗。
   - `continue: false`：UserPromptSubmit、PreToolUse、Elicitation 擋下，原因取 `stopReason`；其他事件不採用（工具已經跑完、回合已經結束，或這個事件本來就不能擋），記一筆說明。
   - PermissionRequest 只認 `hookSpecificOutput.decision.behavior` 的 `deny`，理由取 `message`；照官方，結束碼 2 不生效；`decision: "block"` 與 `continue: false` 也不生效。
   - `additionalContext` 只有 UserPromptSubmit、PreToolUse、PostToolUse、PostToolUseFailure、SubagentStart 採用；其他事件沒有回給模型的路。
   - http：2xx 的回應內容當結束碼 0 的 stdout。
   - 其他結束碼、逾時、連不上、路徑被拒：hook 自己失敗，不擋動作，但記在執行紀錄裡。
   - 同一事件符合的 hook 同時執行，相同的 handler 只跑一次，任一個擋下就擋下。
   - `hookEventName` 對不上的 `hookSpecificOutput` 整段不採用。
4. **hook 只能擋、不能放行**：
   - `permissionDecision` 的 `allow`／`defer`、PermissionRequest 的 `decision.behavior: allow` 與 `updatedInput`、`updatedPermissions`、`interrupt`，以及 `updatedInput`、`updatedToolOutput`、`updatedMCPToolOutput`、`systemMessage` 一律不採用，只在執行紀錄記一筆說明；`continue: false` 只在能擋的事件當成擋下（決定 3），不能讓代理人停下或繼續。
   - PreToolUse 在政策判定（含角色權限、底線）之後執行：政策已拒絕的不會送給 PreToolUse hook（PermissionDenied hook 會收到，含 `tool_input`，見決定 16）；hook 的 `ask` 跳同一張人類審批卡，不送審查員，無人在場直接拒絕（終端也不落到舊的審批通道），Auto 與 YOLO 都不能代替人；審批卡以 `hook_reason` 欄位顯示「管理員設定的 hook 要你確認」，和審查員的理由分開。
   - hook 不能改寫工具參數，工具照原本的參數與審批流程走。
   - hook 程式不是代理人撰寫或指定的程式（只有人能設定、腳本放在受保護的位置），所以不經 ADR-0272 的隔離 runner，以 Touka 自己的權限執行；靠決定 5 保證代理人改不到它。
5. **指令與腳本必須在代理人寫不到的地方**（`services/hooks/paths.py`；存檔時檢查全部 command hook、含停用的，每次執行前再檢查一次）：
   - 參數：拆成字詞逐一檢查（`--script=x`、`-c "… /path …"`、POSIX 用 `:` 串起來的路徑清單如 `-cp a.jar:/data/x.jar`、黏在單字母選項後面的絕對路徑如 `-I/data/lib`、`@` 後面的參數檔也算），看起來像路徑的字詞都必須在 `config/hooks/` 裡（相對路徑以 `config/hooks/` 為起點），在外面回 `outside_hooks_dir`；`file:` 網址看不出最後讀哪個檔，一律拒收（`file_url`，不分大小寫，字詞中間的如 `jar:file:` 也算）。這個判斷只看 `config/hooks/` 本身，不看專案清單一類會變動的資料。
   - 指令在 `config/hooks/` 裡時照參數的規則：字面與解析後都在裡面；`config/hooks/` 本身與底下經過的每一層都不能是連結（symlink、junction），否則回 `linked_path`；檔案不能有其他硬連結（`nlink>1`，回 `hard_link`），另一個名字可能在代理人的資料夾裡。字面在別處、其實在 `config/hooks/` 裡的指令（經過連結、短檔名，或上層某一層的檔案識別碼就是 `config/hooks/`，例如 bind mount）也照這條查，所以會被拒收：要照字面寫 `config/hooks/` 裡的路徑。
   - 字詞中間 `:`／`@` 後面的絕對路徑（`-javaagent:<路徑>`、`pkg@<路徑>`）也取出來查，含 `//` 開頭的（Windows 上是 UNC，回 `unc_path`）；只有 `:` 前面整段是網址的 scheme（`https://`、`jdbc:postgresql://`）時才當成網址略過。
   - 指令在別處（例如系統的直譯器）時不能落在禁區：資料根、sandbox state、執行器資料夾、工作區根（使用者能在底下自己登記專案）、每個專案根（原路徑與翻譯後的路徑）、MCP 伺服器的可寫資料夾，容器裡另加鏡像掛載根；設定資料夾裡 `config/hooks/` 以外的地方也不行（`skills`、`mcp_servers`、`evals` 由創建與演化寫入）。讀不到 MCP 隔離設定檔時當成無法確認，不執行。
     - Touka 自己的資料夾（資料根、sandbox state、執行器資料夾、工作區根、鏡像掛載根）字面、解析連結後與檔案識別碼都禁。使用者登記的根（專案根與翻譯後的路徑、MCP 可寫資料夾）路上有連結（解析前後不同）時只禁字面：隔離層不授權這種根（ADR-0272 的永不授權規則），代理人的專案檔案工具也不用這種根（決定 12），連結指到的地方代理人寫不到；若照解析結果禁，代理人在上層專案裡把下層專案根換成指向直譯器資料夾或 `/` 的連結，所有 hook 都會被拒（失敗不擋，決定 15）。沒有連結的登記根照舊比對識別碼。
   - 檢查方式：字面比對（不分大小寫）、解析後的路徑、每一層上層資料夾以 `(st_dev, st_ino)` 比對（抓大小寫、8.3 短名與 bind mount 別名）、路徑上每一個連結的位置與它直接指向的地方都查（最多 40 跳）；`..` 一律拒收，拒絕 UNC 與磁碟機相對路徑；指令必須是存在的絕對路徑一般檔；讀不到狀態一律當成無法確認而拒絕。
   - 存檔時被拒回 `HOOK_PATH_REJECTED`（422）；執行時被拒記為 `rejected`、不執行。
   - hook 程式執行後自己再去讀寫哪裡，Touka 管不到；這裡只擋設定裡寫得出來的路徑。
6. **回給模型的文字是資料**：
   - 補充說明包成 `<hook_output>` 區塊，開頭註明這是管理員設定的 hook 的輸出、不是使用者的話也不是指令；內文的區塊 tag 先中和（`<` 換成 `&lt;`），不能提早關閉區塊或冒充別的區塊。
   - UserPromptSubmit 的補充說明接在最後一則 user 訊息尾端，只用在這一次請求（`REQUEST_TAIL_TAGS`，不進下一輪歷史，對話歷史快取斷點打在它之前）。
   - 工具前後的補充說明接在工具結果後面：結構化 JSON 結果加 `hook_output` 鍵（標 `trust: "untrusted"`），文字結果加 `<hook_output>` 區塊。工具結果會顯示在對話裡、也存進對話紀錄（無痕不存），所以這段補充說明對話的使用者看得到，管理員不要在裡面放不給使用者看的內容。
   - PreToolUse 擋下時回結構化 `permission_denied`（`reason: "hook_denied"`、`hook_reason` 中和後截斷 2,000 字）。
7. **四個事件的接線點**（丙-1）：
   - UserPromptSubmit：`HookLayer` 排在 `MonitorLayer`（身分判定）之後、組上下文之前；只在 `process_proxy_request` 設的 `ctx.run_hooks` 時執行（UI 聊天、SNS、評分卡），上下文預覽明確設成不執行，子代理不執行，只有工具結果的 user 回合不算使用者送出的訊息。事件內容的 `prompt` 只有這次請求最後一則 user 訊息的文字；同一次請求裡較早的 user 訊息（例如自己組歷史的用戶端塞進去的）不會送給 hook。這時模式還沒決定（`ToolLayer` 才決定），事件內容不帶 `permission_mode`。擋下回 `HOOK_PROMPT_BLOCKED`（403）並帶 `reason`（最多 4,000 字），前端以專屬通知顯示擋下與理由。
     - 停止：hook 執行期間使用者按了停止，前端這時還沒有進行中的串流，送這個對話不指名的停止。後端在請求開始時就記下這則還沒開始串流的請求是誰送的（`stream_buffer_service.pending_request`，串流開始或請求結束時移除），停止端點認出是同一位請求者，就把這個人在這個對話還在等的請求標成已停止（標在請求上，同一個對話之後的請求清掉對話的停止旗標也不影響），不讀這一輪的對話內容，上一則已結束的串流登記還留著（保留 10 分鐘）時也一樣。
       - 對話已存在時要有權限（沒有就照原本的路回 403），這時也和有串流時一樣設這個對話的停止旗標、結束這個對話登記的子程序。查不到對話（無痕、還沒存過）時，對話 id 是請求自己帶的、可能是別人的，只標這個人的請求，不停整個對話。
       - 每次執行開始時記下這個對話被停止過幾次（`rhythm_service.stop_count`，只增不減、記在記憶體、只記被停止過的對話；`services/hooks/stopping.py`）：前景請求在 `MonitorLayer` 清掉殘留的停止旗標之後記，心跳在整次執行開始時記。之後次數變多就是這次執行期間被停止過，同一個對話的下一則請求或下一次心跳清掉旗標也認得。
       - hook 跑完後看到已停止（這一則被標成已停止、這次執行開始之後被停止過，或對話的停止旗標）就不送模型，回 409 `PROMPT_STOPPED`；組上下文期間才按的停止，換綁對話之前與開始串流之前各看一次，看到已停止就回同樣的 409、不換綁、不建立串流（這兩次只看這一則被標成已停止與停止次數；只剩對話旗標時照原本的流程，由 ReAct 迴圈在第一輪之前結束、回「已中斷」）。hook 擋下時照樣回擋下。沒有 hook 真的執行時 hook 這一層不看這些（照原本的流程；換綁與開始串流前那兩次照樣看）。
       - 這個 409 是使用者停止，不是模型失敗：不記成主要模型失敗、不改用上次能用的模型再送一次。
       - 已經有進行中的串流時，不指名的停止不會停到它：停止端點回 404，前端再讀一次串流狀態，是這一輪的串流才指名停止。
     - SNS：外人訊息等 pipeline 跑完才寫進對話；被擋下的不寫、不回覆外人（擋下原因是設定的一部分），也不往外丟錯，只記一筆 log；擁有者按停止（`PROMPT_STOPPED`）時照寫、不回覆外人，也不往外丟錯（平台那邊才不會回「內部錯誤」）；其他錯誤照舊先寫再往外丟。回覆是串流，寫好之前不會讀，寫不進去就不呼叫模型。因為等 pipeline 跑完才寫，家對話模式下這一句也不會在送給模型的家對話裡多出現一次；同一個對話幾乎同時進來兩則時，hook 跑得久的那則較晚寫進對話，紀錄順序可能和到達順序不同。
     - 這是「使用者送出訊息」的檢查，不是管住使用者或代理人的機制：心跳的脈衝提示、排程寫的提示（`init_prompt`）、重新觸發（`/heartbeat/retrigger`）與只有工具結果的回合都不觸發。
   - 插話（ADR-0249）：插話也是使用者送出的文字，進佇列前跑 UserPromptSubmit（`services/hooks/interjection_hooks.py`，事件內容多 `interjection: true`、`agent_id` 取自這次串流）。擋下回 403 `HOOK_PROMPT_BLOCKED`、不進佇列，前端顯示理由並把文字放回輸入框；hook 的補充說明不採用（插話直接接在這一輪的對話裡，沒有「只用在這一次請求」的位置）；hook 服務出錯照樣進佇列。hook 執行期間串流換了或結束了，回 404 `STREAM_NOT_FOUND`，不進新的串流。
   - PreToolUse／PostToolUse：`ToolExecutor` 內，matcher 比對 Touka 的工具名稱，MCP 工具另比對工具清單上的正式名稱 `server__tool` 與 Claude Code 寫法 `mcp__server__tool`（模型用裸名稱叫也比對得到這兩種）；PostToolUse 只在成功後執行，事件內容多 `tool_response`。
     - 只涵蓋經 `ToolExecutor` 的工具呼叫：任務的驗證指令（`verification_cmd`）、演化與創建的試跑由系統自己啟動，不觸發 PreToolUse，照原本的隔離與審批規則執行。人按的除錯執行（`/debug/execute`）也經 `ToolExecutor`，同樣會觸發；事件內容的 `agent_id` 是請求指定的除錯目標（字串，例如 `DebugAgent`），不是按的人。
     - 工具在等 PreToolUse hook 時使用者按了停止，hook 跑完後不執行這個工具，回結構化 `permission_denied`（`reason: "stopped_by_user"`）；和 UserPromptSubmit 一樣看這次執行開始之後有沒有被停止過，下一則請求清掉旗標也照樣不執行。沒有 hook 真的執行時不看停止。
     - 舊工具名稱（`LEGACY_ADVERTISED_SUCCESSORS`）也比對它對應的新名稱：規則寫 `execute_command`，模型叫 `bash` 也比得到；舊檔案工具的 `action` 剛好是某個新名稱時只對應那一個，其他動作對應全部檔案動詞（寧可多比到）。事件內容的 `tool_name` 照模型叫的名稱，另加 `tool_aliases` 列出對應的新名稱。
     - 事件內容的 `tool_input` 是模型送來的參數：執行器之後替 MCP 工具補上的 `agent_id`、換成代理人自己資料夾實際路徑的根目錄參數，都不會出現在 hook 看到的內容裡。公開參數的驗證在審批之後才做（ADR-0229），所以 hook 看到的是驗證之前的原樣：欄位可能是別名、多出不認得的欄位或型別不對，hook 不能假設已經正規化。
   - Stop：ReAct 迴圈回完之後在背景執行（背景入口，決定 16），回覆不等它；子代理的回合不執行；只記錄，不能讓代理人繼續（ADR-0146：回文字就結束這一輪）。
   - 群聊時每位發言的成員各觸發一次 Stop（工具 hook 也照成員各自的工具呼叫觸發）；事件內容的 `agent_id` 是這位成員（`events.base_payload` 看群聊成員旗標），`session_id` 是群聊的對話。
   - 心跳的每次脈衝都會觸發工具前後與 Stop；脈衝提示不是使用者訊息，所以不觸發 UserPromptSubmit。心跳的停止次數在整次執行開始時記一次，脈衝之間與工具 hook 跑完時都看：同一個對話的 UI 請求清掉旗標，心跳也停得下。
   - 每個事件都有 `session_id`、`agent_id` 與 Touka 多的 `incognito`（hook 自己決定無痕對話的內容要不要留）。
8. **執行紀錄**：最近 50 筆存在記憶體（重啟清空），記時間、事件、hook 名稱與類型、結果、耗時、結束碼或 HTTP 狀態與說明。說明是擋下或要求確認的理由、失敗原因（例外只記類型、stderr 只取第一行）與不採用的欄位，有長度上限，不含 stdout 的內容。背景 hook 超過同時執行上限被跳過時也記一筆（`skipped`，決定 16）。無痕對話照跑 hook，但紀錄只留時間、事件、結果與耗時。
9. **存檔**：只經管理 API，以 revision（內容 SHA-256）做 CAS，對不上回 `HOOK_CONFIG_CONFLICT`（409）；存檔前跑決定 5 的檢查；存檔前讀不到目前的檔案、或寫不進檔案（磁碟滿、沒有權限、被鎖住）回 `CONFIG_SAVE_FAILED`（500），不寫檔，執行中的仍是上一版。設定頁存好後重新讀取失敗時，留著剛存的內容與新的 revision，不當成讀取失敗（下一次存檔才不會誤判成衝突）。
10. **管理 API**（`routers/hooks.py`，`/hooks` 與 `/v1/hooks`）：
    - 下面三個端點只給人類管理員（`require_admin_human`；admin 代打、靈魂、DebugAgent 都不行）；`GET /hooks/active` 見決定 17。
    - `GET /hooks`：目前生效的設定（壞檔時是上一份好的）、`health`（`ok`／`missing`／`error`、目前檔案的 revision、錯誤類型）、已接線的事件與看比對規則的事件（`matcher_events`：有比對規則的事件，見決定 7、16、17）。
    - `PUT /hooks`：送 `{config, revision}`，revision 必填（沒有檔案時明確送 `null`），多的欄位拒收；先查 CSRF Origin；`author` 與 `updated_at` 由伺服器填，用戶端送的不採用；回新的 revision。
    - `GET /hooks/runs`：決定 8 的執行紀錄，新的在前。
    - `hooks.json` 不在設定匯出／匯入（`settings_snapshot_service.SECTIONS`）裡：匯入不經存檔的路徑檢查。
11. **設定頁**：設定畫面「系統」組的「Hooks」分頁（`adminOnly`，不加左側欄入口）。
    - 依伺服器回的事件逐區顯示；比對規則只在 `matcher_events` 的事件出現，提示依事件不同；每組可加 command 或 http hook。
    - command 填程式的絕對路徑、參數一行一個（原樣交給程式）；http 填網址、標頭一行一個「名稱: 值」。
    - 存檔被拒時列出原因：路徑問題逐項列事件、位置、原因與值；衝突提示重新載入；欄位不合規則顯示第一個錯誤的位置。設定檔壞掉時提醒目前用的是上一份好的設定、存檔會取代它。
    - 下方列最近的執行紀錄（hook 名稱與類型、結果、結束碼或 HTTP 狀態、耗時與說明），無痕的只顯示時間、事件與結果。
    - 說明寫明 hook 失敗時不擋動作，不能當成唯一的安全防線；所有使用者的對話（含無痕）都會觸發；「使用者送出訊息」只檢查聊天送出的文字，不是管住使用者的機制，「工具執行前」只涵蓋代理人的工具呼叫；腳本與參數裡的路徑要放在 `config/hooks/`，程式要放在 `config/hooks/` 或代理人寫不到的地方，不要用 `env`、`npx`、`py` 這類啟動器。
12. **一般使用者的專案根不能放在 Touka 保護的位置**（`services/isolation/profiles.project_root_reason`）：決定 5 把專案根當禁區，但專案是使用者自己登記的；專案根若是設定資料夾、`config/hooks/` 或它們的上層，代理人就能經專案檔案工具寫進 hook 腳本。
    - 判斷沿用 ADR-0272 的永不授權規則：受保護的位置與它們的上層、經連結指到受保護位置的一律拒絕；容器裡工作區掛載根本身也不能當專案根，另查 Touka 目錄（只有使用者自建的頂層資料夾能當專案）。
    - 本機安裝不往上找 Touka 目錄（安裝根就是 Touka 目錄，往上找會擋掉所有專案），改用固定規則：專案根在安裝根底下時，第一層資料夾不能是 Touka 自己的（點開頭的，或 `TOUKA_TOPLEVEL_PROTECTED` 裡的 `ToukaChat`、`ToukaProxy`、`docs`、`scripts`、`tests`、`data`、`backups`、`venv`、`node_modules` 等，不分大小寫），例如 `.venv`、`.git/hooks`、`.claude` 底下都不行；安裝根本身也不能當專案。
    - 容器裡的 Touka 目錄判斷會誤擋：專案裡（往下 3 層內）剛好有和 Touka 標記或資料根同名的資料夾就當成受保護的位置；資料夾太多、有讀不到的資料夾或還在掃（前景最多等 2 秒，背景繼續掃、結果快取 10 分鐘）時回「檢查沒做完」（`scan_limit`），和受保護的位置分開說明，也不放行。快取過期後會重掃，大的專案大約每 10 分鐘可能遇到一次「檢查沒做完」，稍後再試即可。
    - 三處檢查：登記與更新專案（`ui_sessions._check_project_root`，回 422 並帶 `reason`：`outside_workspace`、`protected_location` 或 `scan_limit`）、列專案檔案與每次專案檔案工具呼叫（`project_tools`，回結構化 `permission_denied`，`reason` 同上，檢查沒做完時建議稍後再試）。聊天畫面的專案檔案樹（`ui_sessions` 的檔案樹端點）被拒時照舊回不分原因的 403，檢查沒做完時也一樣，稍後重新整理即可。
    - 管理員與 Debugger 不受這條限制（他們本來就能指定任何位置，`resource_manager.bypasses_project_jail`）；只有管理員擁有的靈魂也不受限（前景交談時執行身分會換成靈魂本身，ADR-0154，所以另查擁有者，`authorization_gateway.only_admins_own`）。有一般使用者共同擁有的靈魂照一般身分檢查：一般使用者也能用它對話，不能因為管理員也擁有就放寬。這是已知的界線：管理員登記在受保護位置的專案，只有管理員擁有的靈魂用專案工具時仍寫得到，由管理員自己負責不這樣登記。其他靈魂在前景與心跳都照這條檢查。
    - 代理人（含 Debugger 與只有管理員擁有的靈魂）的專案根路上有連結（symlink、junction，解析前後不同）時，專案檔案工具一律拒絕（`profiles.has_link`，回結構化 `permission_denied`、`reason: "linked_root"`），和隔離層的永不授權規則一樣：檔案工具會跟著連結寫，代理人在上層專案裡把下層專案根換成指向 `config/hooks/` 的連結，就能改寫不經隔離執行的 hook 程式。查的是翻譯後的路徑，容器裡的連結也看得到。查有沒有連結與是不是受保護的位置用同一次解析的結果；讀寫時 core ops 再解析一次根，和查過的真實路徑不同（查過之後才把根換成連結）就拒絕（`root_is_real`）。人類自己操作照舊。
    - 代理人的檔案動詞（專案、個人空間、靈魂與舊對話的 read、write、append、str_replace、delete、list、find、grep）不跟隨連結（ToukaMemory `utils/no_follow.py`；經 `ResourceManager` 的用它的 `*_no_follow` 方法）。路徑只在開檔之前檢查一次，代理人可以趁這中間用背景指令把路上一段換成指向 `config/hooks/` 的連結；根裡代理人自己建的連結也會讓 list、find、grep 讀到根外面（例如 `.env`、`hooks.json`）。
      - 路徑先解析、確認在根底下；開檔時路上任何一段（含最後一段）是 symlink 或 junction 就拒絕，回「the path goes through a symbolic link or junction; links are not followed.」。
      - Linux（容器）從檔案系統的根起逐段以 `O_NOFOLLOW` 開（`dir_fd`、`O_PATH`），換成連結的那一段開不起來；寫入、改名、刪除都在開好的上一層資料夾裡做；開檔帶 `O_NONBLOCK`，只認一般檔案；失敗時例外帶絕對路徑，被鎖住的資料夾或檔案照常權限自癒（ADR-0272）。讀不到狀態時不先回「找不到」，交給開檔（經 `ResourceManager` 的會先自癒）。Windows 沒有 `dir_fd`：開檔後查路上沒有連結、開到的就是路徑現在指的那個檔案（比對檔案識別碼），建立或寫入時（含原子寫入的暫存檔）開檔前也先查；開檔失敗時也查，是連結就報連結。磁碟區掛在資料夾上的掛載點和 junction 是同一種，也當成連結（見已知的界線）。
      - list 把連結列成「名稱 (link, not followed)」、不進入；find、grep 不進入也不列出連結，讀不到狀態的資料夾（例如有讀取、沒有執行權限）也不進入，走訪前先查一次根。find 的比對大致同 `rglob`（只列一般檔案；`src/**` 這類結尾是 `**` 的寫法也會列出檔案）。find、grep 找到 50 筆就停（專案邊走邊比；經 `ResourceManager` 的先列出再比）。
      - 指回根裡面的連結照常可用（路徑先解析，解析後沒有連結）。專案檔案照舊就地寫入；經 `ResourceManager` 的照舊原子寫入、權限 0600、受保護路徑（靈魂、舊對話）拒絕。個人空間目錄（ADR-0255）讀檔算雜湊、讀寫目錄檔也不跟隨連結，個人空間路徑先解析。
    - 行為變更：一般使用者的專案不能在受保護的資料夾裡，也不能是它們的上層（例如工作區根就是 Touka 安裝根時，工作區根本身不能當專案）；既有的這類登記留著，但專案工具會拒絕。專案根路上有連結時（8.3 短檔名、subst 與對應的網路磁碟機解析後也不同，一樣算），代理人不能用專案檔案工具（終端本來就不能用這種根），要改登記真實路徑。代理人的檔案動詞遇到連結不再跟隨：list、find、grep 不進入，路上有連結時讀寫刪回錯誤。
13. **群聊**：hook 補充說明裡的 `@別名` 不算點名（`group_router` 找點名前先拿掉 `<hook_output>` 區塊），hook 的輸出不能叫其他靈魂發言。
14. **被擋下的這一輪從對話拿掉**（和 Claude Code 一樣）：聊天前端送出時先存使用者這一輪（ADR-0231），被 UserPromptSubmit 擋下後，把這則訊息和它的空回覆從對話樹拿掉，照「後端沒有存文字」的完成流程以 revision 比對存回去（後端在送模型前就拒絕，什麼都沒寫）；之後心跳、重新觸發、滑動視窗摘要與歸檔讀存檔都看不到它。對話標題若是用被擋文字開頭建的，一起換回。文字放回輸入框（還在這個對話、而且輸入框還空著時），顯示擋下的理由。
    - 已知的空窗：這一輪在 hook 執行前已經存了；hook 執行期間剛好跑心跳，或存回時 revision 對不上（別的分頁改過），被擋的訊息會留在存檔裡，要使用者自己刪。hook 執行期間使用者按了停止、重新整理、關掉分頁，或請求先被 nginx 逾時切斷，前端已經不在等這次回應，hook 之後擋下時也一樣留在存檔裡（停止時不會送模型，決定 7）。
    - 放回輸入框的只有文字：附件不放回；編輯舊訊息後重送被擋時，文字也不放回（編輯框已經關掉），畫面跳到被編輯那則訊息的上一層（原本的分支還在，切換分支就看得到）。
    - 自己組歷史的用戶端（IDE、自訂 client）不在這個範圍：被擋下的訊息若被它們放進之後的請求，hook 只看最後一則 user 訊息（決定 7），較早的那則會送到模型。
15. **失敗不擋（fail-open）**：hook 自己失敗（結束碼、逾時、連不上、路徑被拒、輸出壞掉）和 hook 服務本身出錯都不擋動作；前者記在執行紀錄與 log，後者只記 log。例外內容不回給模型或用戶端，只記類型。所以 hook 不能當成唯一的安全防線：模型可以故意讓 hook 失敗（例如送很大的參數讓 hook 逾時、送讓 hook 腳本出錯的內容），hook 失敗時動作照常執行。「失敗就擋」列為下一批，讓每個 hook 自己選。
16. **工具失敗、審批與通知四個事件**（丙-2）：工具類事件的比對與事件內容照決定 7（舊工具名稱也比對新名稱、`tool_input` 是模型送來的參數）。
    - PostToolUseFailure：`ToolExecutor` 裡工具真的開始跑之後才失敗時執行（工具回錯誤結果或丟例外），等 hook 跑完，補充說明與回饋照決定 6 接在工具結果後面（`ran_tool`）。失敗是：結構化錯誤（MCP 的 `isError` 與例外都會變成這種）、終端結束碼不是 0（只看輸出區塊第一行的標頭，指令文字裡的同樣字串不算）、舊工具結果開頭是 `Error:` 或 `❌`（過渡做法：舊工具改成結構化錯誤之前，用別的寫法回報失敗的會走 PostToolUse）。內容裡才出現這些字不算失敗，走 PostToolUse（畫面上的成功與否是另一個判定，沒有改）。權限拒絕（含工具自己回的 `permission_denied`）、參數不對、查無工具、MCP 參數太大或清單不允許都不觸發。內容多 `tool_use_id`、`error`：取結構化錯誤的訊息，先遮憑證（`utils/secret_text.redact_credentials`）再遮路徑與長 token（`sanitize_diagnostic`）。
    - PermissionRequest：審查員判完、要跳人類審批卡之前執行；無人在場沒有卡，也不問 hook。等 hook 跑完。`deny` 時不跳卡、不執行，回結構化 `permission_denied`（`reason: "hook_denied"`）；其他回應卡照樣跳。內容沒有 `tool_use_id`（照官方）。等 hook 的期間按了停止：不跳卡、不執行（`stopped_by_user`）。只有 `ToolExecutor` 的卡會觸發：驗證指令的卡與工具自帶的審批不觸發（決定 7 的範圍）。
    - PermissionDenied：工具呼叫被政策（含底線、MCP 工具的角色權限）、審查員、PermissionRequest hook、人按拒絕或審批逾時擋下，或無人在場時，在背景通知，不能改結果。`reason` 只放固定代碼（`policy`、`mcp_tool_rbac`、`human_only`、`auto_review_denied`、`hook_denied`、`hook_requires_human`、`approval_rejected`、`approval_timeout` 等；政策的原因文字不送）。使用者按停止不算被拒。不算的：PreToolUse 擋下（照官方）、更早的出口（角色沒有這個工具、不在這一輪清單、使用者範圍的 MCP、vault 限制的 MCP、分類對不上）、工具自帶審批（終端舊通道、技能安裝）的拒絕。
    - Notification：在背景通知，不能改結果；比對規則比的是通知類型。`permission_prompt`：審批卡真的送出之後（含驗證指令的卡；佇列滿沒送出不發；PermissionRequest 拒絕就沒有卡，也不發）。`agent_needs_input`：代理人問使用者問題。`elicitation_dialog`：MCP 工具的表單送出之後。`isolation_level`：隔離層級改變，開機第一次判定也算（`IsolationLevelNotifier` 接 `isolation_monitor.on_level_change`）；這則不屬於任何對話，`session_id` 是 null。`message` 是固定文字加工具名稱，隔離只放層級名稱、不放原因細節。
    - 背景入口（`services/hooks/background.py`）：Stop、PermissionDenied、Notification 共用。沒有符合的 hook 時不組內容。同時最多 32 個（`MAX_BACKGROUND_HOOK_RUNS`），慢的 hook 碰上大量事件時背景工作不會一直累積；超過就跳過這次、在執行紀錄記一筆 `skipped`，設定頁看得到（無痕只留時間、事件與結果）。入口出錯只記 log，不影響呼叫端。名額是所有事件與對話共用的：模型或其他對話可以把名額佔滿，這段期間別的通知會被跳過，所以這些事件不能當成完整的稽核紀錄。
    - 工具缺必要參數：內建工具少了廣告 schema 的 `required` 參數時，在 `tool_start` 之前回結構化 `validation_error`（`param` 是第一個缺的；只看鍵在不在，不回顯參數值），不進工具本體。以前會變成 `TypeError` 顯示在畫面上。照官方，這種情況不算 PostToolUseFailure。順序照 ADR-0229（政策與審批在前、參數檢查在後）不變：缺參數的呼叫仍可能先跳卡、先觸發 PermissionRequest 與 Notification，人按批准後才被退回。走 Pydantic 合約的七個工具照舊由 `validate_native_arguments` 檢查；舊工具不在廣告清單裡，由工具自己處理。
17. **其餘十一個事件與無痕提示**（丙-2，`services/hooks/lifecycle.py`）：除了 SubagentStart 與 Elicitation，都走背景入口（決定 16），不等 hook、不能改結果；hook 回「擋下」一律不採用。事件內容只放下面列的欄位（鍵的白名單由實際驗證與單元測試釘住），不放設定值、不放派工目標。
    - StopFailure：一輪回覆因錯誤結束時（重試和備援都用完才算；`react_core` 的 `RoundError` 分支，`dispatcher` 只補沒走到那裡的例外，不會發兩次），子代理的回合不發，這一輪不跑 Stop。`error` 是錯誤類型（`rate_limit`、`overloaded`、`authentication_failed`、`billing_error`、`invalid_request`、`model_not_found`、`server_error`，其他都是 `unknown`；`RoundError.error_type`，adapter 依 HTTP 狀態與錯誤內容標），比對規則看它；`error_details` 先遮憑證再遮路徑與長 token。使用者按停止不算。心跳的模型失敗也發（HeartbeatStop 的 `end` 仍是 `completed`，見下）；心跳迴圈本身的例外不走這條，由 `end: "error"` 表示。
    - SubagentStart：子代理開始之前（`dispatch_subagent` 與群組都經過 `_assemble_subagent_request`），`agent_id` 是子代理、`agent_type` 固定 `subagent`；比對規則看 `agent_type`。補充說明以 `<hook_output>` 區塊接在第一則訊息後面（資料，決定 6）；不能擋。等 hook 的期間按了停止就不開始（`SubagentStoppedError`）。審查員的輸入不變（禁令 64：只看使用者的要求）。內容不含派工目標。
    - SubagentStop：子代理結束時（成功、失敗或逾時都發），`last_assistant_message` 失敗時是空字串；不能叫它繼續。
    - PreCompact、PostCompact：滑動視窗剪裁前與存好之後（`_check_and_trim_inner`），`trigger` 固定 `auto`，比對規則看它。不新增觸發點，也不等 hook（禁令 28、57）：hook 卡住不拖住對話也不拖住剪裁。PostCompact 只在確認存好時發，`compact_summary` 最多 10,000 字（沒有摘要時是空字串）。手動切分是前端做的，後端沒有這個時點，所以沒有 `manual`。
    - TaskCreated、TaskCompleted：任務看板建立時每個任務各一次，任務變成 `DONE` 時一次；內容 `mission_id`、`task_id`、`task_subject`。在寫入之後才發，不能擋。外部 AI 經 ToukaDispatch 的操作不經工具執行器，不觸發（和決定 7 的工具事件同一條界線）。
    - Elicitation：有人在場、MCP 工具用表單要資料、表單送出之前（`McpInteractionSink.before_shown`，表單的 schema 已正規化）；比對規則看 MCP 伺服器名稱。內容 `mcp_server_name`、`message`、`mode`（固定 `form`）、`requested_schema`。只有拒絕有效（結束碼 2、`decision: block`、`continue: false`、`action: decline|cancel`），結果是不跳表單、回 MCP 伺服器 `decline`；`action: accept` 與 `content` 不採用，表單照樣跳，執行紀錄記下不採用的欄位。等 hook 的期間按了停止也不跳表單。無人在場本來就直接拒絕，不觸發。hook 的 `timeout` 最多 30 秒（`MAX_ELICITATION_HOOK_TIMEOUT_SECONDS`，存檔時檢查）：hook 和等人填表算在同一個 MCP 回合，上限短 hook 才會自己先逾時、留下紀錄，表單照樣跳；回合與整體的上限（`mrtr.py`）也多加這 30 秒和收掉行程的 10 秒，第一個回合的人一定有完整的 120 秒倒數；後面回合能用的時間和之前一樣（整體上限管著，最多約 60 秒）。
    - ConfigChange：設定頁或 API 確認存好之後才發（不是存檔前），比對規則看 `source`（`autonomy`、`backup_schedule`、`bots`、`connections`、`custom_models`、`embedding`、`env`、`group_whitelist`、`hooks`、`identity`、`mcp`、`models`、`permissions`、`restore`、`settings_import`、`setup`、`skills`、`sliding_window`、`workspace`）。內容只有 `source` 與共同欄位（`session_id`、`agent_id` 是空的），不放值。擋不了任何設定，所以 hook 壞掉也關得掉；關閉 hooks 那次已經關了，不觸發。每個會改狀態的路由都要在 `tests/unit/test_hook_config_change_security.py` 分類：存好後通知，或列進「不是設定」並寫理由，新端點沒分類就紅。這批歸「不是設定」的：帳號與登入、對話與專案資料、審批與回答、靈魂的資料與排程、備份檔案本身、唯讀的重新整理、除錯工具、MCP OAuth 的連線與憑證（有自己的稽核紀錄）。代理人用工具建立 MCP、Skill 不算（走工具事件）；設定頁的建立只在真的寫入時發。還原在接受還原時發（完成後行程就重啟，沒有「成功之後」可發）；接受不等於還原成功，所以這個通知不代表已經還原。
    - HeartbeatStart、HeartbeatStop（Touka 自己的）：心跳確定要跑之後與結束時（`_process_heartbeat_loop`），內容 `pulses_total`、`home`；結束多 `pulses_done`、`end`（`completed`、`interrupted`、`quota`、`budget`、`no_model`、`error`、`cancelled`）。不看比對規則。重新觸發（`/heartbeat/retrigger`）也算。模型失敗時心跳把錯誤訊息當這次脈衝的回覆（既有行為），StopFailure 照發，但 `pulses_done` 照算、`end` 仍是 `completed`；`error` 是心跳迴圈本身出例外。要知道模型有沒有失敗請看 StopFailure，不要只看 HeartbeatStop。
    - 無痕提示：`GET /hooks/active` 回 `{active}`，任何已登入身分都能查、只回布林值，不透露設定。`active` 是「有啟用中的 hook 會收到對話內容」：`NO_CONVERSATION_EVENTS`（ConfigChange、HeartbeatStart、HeartbeatStop）以外的事件才算。聊天畫面只在無痕對話查，送出訊息後再查一次（hook 在對話中途打開時，下一則就知道）；查不到時維持上次結果。
    - 順手修：模型回 200 卻讀不懂或沒有回覆時，是一般的模型失敗。OpenAI 相容 API 內容讀不懂或不是物件時回 `RoundError`（不再變成 `KeyError` 一類的例外）；只有錯誤本文時也是，忙碌類的和串流一樣會重試。解析後沒有文字也沒有工具呼叫、停止原因又是 `error` 時（`{}`、`{"choices": []}`、空內容配不認得的 `finish_reason`、Gemini 的 `MALFORMED_FUNCTION_CALL`），`react_core` 當成失敗：不重試，回報的用量照記，插話算沒送到。錯誤文字由 `retry.no_reply_error` 組，只放錯誤的 `type`、`code`、`status`、`message` 與回應的 `status`、`incomplete_details.reason`：整份回應可能帶我們送出的系統提示，而這段文字會進 StopFailure、心跳與 SNS 的回覆。Gemini 非串流讀不懂時是例外，走 `dispatcher`。被安全過濾擋下、拒答、模型正常結束卻沒有字不算失敗。串流以 `generation: "error"` 結束時，那則回覆的泡泡標「這一輪回覆失敗」（`generationFailed`，只用來顯示、不送給模型），無痕對話也看得到。文字不寫「模型」：還沒呼叫模型就結束（畫面帶的版本已過期）、Touka 自己的例外也是這個結束方式。標示在畫面的對話樹裡，模型失敗時後端不存這一輪，要重試保存才進存檔；沒有重試保存就重新整理，而且串流緩衝已過期（10 分鐘）時，這則回覆和標示都不在。ADR-0231 的保存狀態不變：模型失敗時對話的保存結果仍是不確定，要核對、重試保存才能送下一則（見 ADR-0295「實際驗證找到的既有問題」）。

## 和 Claude Code 對不上的部分

- **刻意不同**：
  - 指令不經 shell，要寫成程式＋參數清單；管線等 shell 語法要寫成腳本。
  - 只能擋與要求確認，不能放行、不能改寫工具輸入或輸出、不能叫模型繼續（Stop 的 `block`）。
  - http 標頭不替換環境變數；hook 程式拿不到金鑰。
  - UserPromptSubmit 的補充說明只用在這一次請求，Claude Code 會留在之後的對話裡。
  - 被 UserPromptSubmit 擋下的這一輪由聊天前端在擋下後從對話拿掉（決定 14），有已知的空窗；Claude Code 是一開始就不寫進紀錄。插話也跑 UserPromptSubmit，但補充說明不採用。
  - hook 失敗一律不擋，沒有「失敗就擋」的選項（決定 15）。
  - 全系統一份設定，沒有使用者、專案、本機多層設定，也沒有技能或外掛帶的 hook。
  - 事件內容沒有 `transcript_path`、`cwd`；`permission_mode` 是 Touka 的模式名稱（Manual／Auto／YOLO），UserPromptSubmit 沒有；多一個 `incognito`。
  - 指令與參數有路徑限制（決定 5）。
  - PermissionRequest 只能拒絕：Claude Code 能放行、改參數或權限、`interrupt`，Touka 都不採用，卡照樣跳；內容沒有 `permission_suggestions`。
  - PermissionDenied 範圍較廣：Claude Code 只有自動模式被拒才觸發、回 `retry` 可提示模型重試；Touka 手動拒絕、政策與審批逾時也觸發，`retry` 不採用；`reason` 是固定代碼，不是說明文字。
  - Notification：Claude Code 的閒置、登入、額度、完成等類型 Touka 沒有；`isolation_level` 是 Touka 多的；沒有 `title`。驗證指令的卡發 Notification 但不觸發 PermissionRequest。
  - 工具缺必要參數：Claude Code 先驗證參數才處理權限，所以不會有卡；Touka 政策與審批在前（ADR-0229），可能先跳卡、先觸發 PermissionRequest 與 Notification。
  - PostToolUseFailure 沒有 `is_interrupt`、`duration_ms`。MCP 工具沒有 `mcp_server` 欄位，和其他工具事件一樣用 `mcp_tool_name`。
  - StopFailure 的錯誤類型用同樣的名稱，Claude Code 的 `oauth_org_not_allowed`、`account_on_hold`、`cloud_credential_error`、`max_output_tokens` 在 Touka 都算 `unknown`（`max_output_tokens` 不走錯誤路徑）；內容的 `error_details` 先遮憑證與路徑，不附回覆全文，也沒有 `last_assistant_message`。HTTP 錯誤的類型只看狀態碼：OpenAI 的 `429 insufficient_quota` 是 `rate_limit`，不是 `billing_error`。
  - SubagentStart、SubagentStop：`agent_type` 固定 `subagent`；沒有 `agent_transcript_path`、`stop_hook_active`。SubagentStop 只通知：Claude Code 能叫子代理繼續，Touka 不採用（和 Stop 一樣）。SubagentStart 的補充說明 Claude Code 放在第一個提示之前，Touka 接在派工內容後面（資料，決定 6）；Claude Code 結束碼 2 的 stderr 會給使用者看，Touka 沒有這條路。
  - PreCompact 只通知：Claude Code 能擋；Touka 規定剪裁不能被拖住（禁令 28、57）。`trigger` 只有 `auto`，沒有 `custom_instructions`。
  - TaskCreated、TaskCompleted 只通知：Claude Code 能擋；Touka 的紀錄點在寫入之後，要擋得另外改任務流程。多一個 `mission_id`，沒有 `task_description`、`teammate_name`。外部 AI 經派工伺服器的操作不觸發。
  - Elicitation 只能拒絕：Claude Code 能代填（`accept`＋`content`）；Touka 不能替人回答。Touka 只支援表單，沒有 `url`、`elicitation_id`；無人在場本來就直接拒絕，不觸發。
  - ConfigChange 只通知、在存好之後：Claude Code 在變更時可以擋。`source` 是 Touka 的設定名稱，沒有 `file_path`；看的是設定頁與 API，不看手動改檔。
  - HeartbeatStart、HeartbeatStop 是 Touka 多的。
  - matcher 的正規表示式是 Python 的 `re`（`re.search`，不錨定）；只有字母、數字、`_`、`-`、空白、`,`、`|` 的寫法當成精確名稱或清單（`,` 也能分隔）。
- **Touka 沒有對應功能，不做**：工作目錄切換、檔案監看、worktree、Agent 團隊相關事件。
- **這批不接的事件**：ElicitationResult（改寫人填的內容違反界線；人回答完才擋意義不大）；SessionStart、SessionEnd（Touka 的對話沒有明確的開始與關閉）；Setup、MessageDisplay（Touka 沒有對應的時點）；PostToolBatch、InstructionsLoaded、UserPromptExpansion（有接近的時點，但目前沒有用途）。
- **下一批**：hook 類型 `mcp_tool`、`prompt`、`agent`（後兩種會用到模型額度）；非同步 hook（`async`）、`if` 條件、`once`；每個 hook 可選的「失敗就擋」；設定頁與 `GET /hooks` 遮住 http 標頭的值。
- **已知的界線**：
  - Windows 上 `.bat`／`.cmd` 由系統經 `cmd.exe` 啟動，參數會被 `cmd.exe` 重新解讀；要避開 shell 就用其他程式寫 hook。
  - 參數只檢查看起來像路徑的字詞：直譯器的 `-c`／`-m` 給的程式碼或模組名稱不是路徑，hook 實際執行哪些程式碼由管理員負責。
  - MCP 可寫資料夾禁區的界線移到 ADR-0293「已知的界線」的「不經 RM 的程式」。
  - 只檢查設定寫得出來的程式：`env`、`npx`、`py` 這類啟動器會自己找要執行的程式（依 PATH、上層的 `node_modules` 或自己的設定），找到的程式在哪裡 Touka 不檢查。請寫成直譯器＋`config/hooks/` 裡的腳本，不要用啟動器當 hook 的程式。直譯器自己也會找模組：Node 的 `require` 會一路往上層資料夾找 `node_modules`，Python 依 `sys.path` 找；腳本用到的套件請裝在 `config/hooks/` 裡（例如 `config/hooks/node_modules`），不要依賴上層資料夾的套件。
  - Linux／Docker 上參數不能放網址：POSIX 的 `:` 也是路徑清單的分隔（`-cp a.jar:/x.jar`），`https://host/x` 會被拆出 `//host/x`，當成 `config/hooks/` 以外的絕對路徑拒收。網址不另外放行，因為 `-cp https://x/data/evil.jar` 這種寫法 Java 會讀 `//x/data/evil.jar`。要傳網址請寫在 `config/hooks/` 裡的設定檔，由腳本讀取；Windows 的 `:` 不拆，網址照常可用。
  - PreToolUse 與 PostToolUse 只涵蓋 Touka 代理人經工具執行器的工具呼叫；ToukaDispatch 給外部 AI 的派工工具不經工具執行器，不會觸發。
  - hook 程式本身以 Touka 的權限讀寫（決定 5 最後一點）：移到 ADR-0293「已知的界線」的「不經 RM 的程式」。
  - 本機安裝時，安裝資料夾裡的 Python（例如 `.venv` 底下的直譯器）在工作區根裡，不能當 hook 的程式（`agent_writable`）；請用系統安裝的直譯器，腳本放 `config/hooks/`。
  - 腳本被刪掉或改名時，直譯器自己會以結束碼 2 結束（例如 Python 找不到檔案），照 Claude Code 的規則就是擋下，而且 stderr（含路徑）會當成理由顯示；這時所有符合的動作都會被擋，直到修好設定。
  - PermissionRequest 在審查員之後：審查員放行或拒絕就沒有卡，也不會問這個 hook；PreToolUse 要人確認時兩個都會跑。
  - PostToolUseFailure 的遮罩是比對樣式：認得的憑證格式（`Bearer`、`sk-`、`key=`、`api_key`、`password`、`secret`、`token`，含 JSON 的引號寫法與網址參數；網址裡的帳密）與路徑會遮掉，認不出的格式不保證；`tool_input` 照原樣給 hook，和其他工具事件相同。
  - 事件內容一律送給 hook，無痕對話也一樣（決定 7 的 `incognito` 讓 hook 自己決定要不要留）；設定頁說明會寫明，無痕對話也會提示（決定 17）。提示是畫面查的：查不到時維持上次結果，所以一開始就查不到時不會顯示；這個提示只是告知，不能當成擋住內容外送的保證。
  - 背景事件不保證送達（決定 16 的名額）：ConfigChange、StopFailure 這些也一樣，不能當成完整的稽核紀錄。一次建立超過 32 個任務的看板時，TaskCreated 在同一段程式裡排進背景，第 33 個起一定超過名額而跳過（紀錄看得到），和 hook 快慢無關。
  - 無痕提示把 PreCompact、PostCompact 也算進去：這兩個事件在無痕不會觸發，只設這兩個也會提示；方向是多報，不是漏報。
  - 模型回 200、讀得懂、但正常結束卻沒有文字（`finish_reason: stop` 的空內容，或 Responses `completed` 沒有輸出）不算失敗（決定 17），畫面仍是空白泡泡。
  - 沒有回覆的這一輪不一定是第一輪：工具跑完後的回合才沒有回覆時，前面的文字還在畫面上，泡泡同時標失敗，保存狀態也和其他模型失敗一樣不確定。
  - PreCompact 發出之後剪裁仍可能沒存好（版本對不上；見 ADR-0295「實際驗證找到的既有問題」），這時只有 PreCompact、沒有 PostCompact。
  - ConfigChange 的分類表寫在測試裡：看路由是否改狀態、原始碼裡有沒有這個呼叫；路由把存檔交給別的函式、呼叫放在失敗的分支裡，分類表抓不到，要靠每個路由自己的測試。
  - 事件內容的 `session_id` 是請求帶來的對話 id，hook 不能把它當成已驗證的身分。
  - PreToolUse 的「要人確認」跳的是一般的人類審批卡，誰能按和其他審批卡相同（ADR-0246）；hook 不能指定由誰核准。
  - 備份還原會寫回 `hooks.json`（照原樣、不經存檔的路徑檢查）；還原後下一次執行前仍會檢查路徑（決定 5），不合規則的 hook 不執行。
  - hook 執行中按停止的時間窗：請求剛進來、還沒登記成等待中（路由還在認證或讀取內容；通常很短，附件大或連線慢時可能好幾秒）就送到的停止，指名的找不到它；不指名的照原本的路停整個對話，這個對話正在跑的心跳也會停下，和有串流時的停止一樣（這之前按停止什麼都不送），這則請求本身停不停得到，看它是不是已經記下停止起點。不指名的停止若晚到、同一個人已經在同一個對話送出下一則，可能停到下一則；同一個對話在另一個分頁有進行中的串流時，這個分頁的不指名停止不會停到任何東西。
  - 無痕請求自己帶對話 id，Touka 不核對那個 id 是不是別人的對話（這次之前就是這樣，另案處理）；所以等待中的停止查不到對話時只標自己的請求，不停整個對話。
  - 等待中的請求「是不是同一位請求者」用執行身分判斷：兩個人用 X-Agent-ID 代打同一個靈魂時（管理員與擁有者，或兩位管理員；擁有者自己在前景交談時登記的是人類身分，不會和代打的人撞在一起），彼此的不指名停止都停得到對方在等的那一則。兩人本來都能操作這個靈魂的對話；之後改成登記時記下人類身分。
  - 黏在多字母選項後面、沒有 `=` 或 `:` 隔開的絕對路徑（POSIX 的 `-isystem/data/x`）認不出來：分不出選項名稱到哪裡為止，照單字母的規則拆會誤擋 `-Ilib/sub` 這類 `config/hooks/` 底下的相對路徑。請寫成 `--opt=/x` 或分成兩個參數。
  - 每次檢查有沒有 hook 都同步讀一次 `hooks.json`、算雜湊比對有沒有變（檔案很小；沒有這個檔時只多一次存在檢查）；這樣手動改檔也會在下一次執行時生效。
  - 能讓路徑檢查拒收的人就能讓那個 hook 不執行，例如對 `config/hooks/` 裡的腳本建一個硬連結（檢查回 `hard_link`）：被拒算失敗，不擋（決定 15），執行紀錄看得到。代理人在 Landlock 裡做不到（不能跨資料夾建連結，設定資料夾也寫不進去）；未隔離、經人核准的指令，以及 IDE、未隔離的 MCP 伺服器做得到。
  - `config/hooks/` 裡裝的套件（例如 `node_modules`、虛擬環境）若是用硬連結裝的（uv、pnpm 可能如此），檔案可能和代理人專案裡的同一份共用，代理人在 Landlock 裡就地改自己那份，hook 用的那份也跟著變。路徑檢查只看指令與參數本身有沒有硬連結，不逐一檢查套件裡的檔案；請用複製的方式裝（uv 的 `UV_LINK_MODE=copy`、pnpm 的 `--package-import-method=copy`，npm 本來就是複製），設定頁說明有寫。
  - 專案根路上有連結時 hook 這邊只禁字面（決定 5），因為代理人的終端與專案檔案工具都不用這種根（決定 12）。8.3 短檔名、subst 與對應的網路磁碟機解析後和字面不同，也照這條處理。查過之後才把專案根本身或它上面的一段換成連結，讀寫時再解析的根和查過的不同，拒絕；根底下的路徑在開檔前被換成連結，由不跟隨連結擋下（決定 12；Windows 見 ADR-0293「已知的界線」，list、find 見後面的走訪）。
  - Windows 本機的檔案動詞查連結和開檔不是同一個動作：時間差的細節移到 ADR-0293「已知的界線」的「Windows 本機」。
  - Windows 本機的 Touka 安裝位置、工作區或專案的路上有掛在資料夾上的磁碟區（沒有磁碟機代號）時，那個資料夾和 junction 是同一種：代理人的檔案動詞可能一律回連結錯誤，專案也可能被當成路上有連結（依程式推得，沒有實機驗證；登記專案時不一定先被擋下）。請改用有磁碟機代號的路徑。
  - list、find 在走訪前查一次根，個人空間目錄的重建（`stat_files`）先解析根。Linux 上 ADR-0293 起改成從開好的資料夾不跟隨地往下走，走訪中途換進來的連結不進入。Windows 沒有 `dir_fd`，每進一層資料夾仍用路徑重新列出：走訪中途把根、它上面的一段或任何一層資料夾換成連結，可能列出連結另一端的檔名，個人空間目錄還會把那些檔案的大小與修改時間寫進目錄檔。grep 讀檔不跟隨連結，讀不到內容。
  - 決定 12 只涵蓋上面列的代理人檔案動詞；Touka 自己在代理人資料夾裡的其他讀寫由 ADR-0293 處理（PR 1：個人空間；PR 2：`mcp_servers/managed`、docgen 輸出、sandbox state），兩個都已合併。之前「暫時不要啟用 command hook」的提醒隨 PR 2 解除；ADR-0293 各自的「已知的界線」照舊適用。
  - 停止次數（`rhythm_service.stop_count`）只增不減、記在記憶體，每個被停止過的對話留一筆（幾十位元組），重啟才清掉；停止起點要靠次數只增不減，所以不在中途刪。
  - 子代理的工具在等 PreToolUse hook 時按停止，子代理不會停（子代理本來就不看對話的停止旗標，這次之前就是這樣）；hook 跑完後工具照常執行。
  - 選靈魂的後備路徑（MonitorLayer 沒認出靈魂時，`SoulLayer` 用最後一則 user 文字比對關鍵字）看得到 UserPromptSubmit 接上的 `<hook_output>` 區塊，可能因此選到另一個靈魂；選出的靈魂仍要通過 `require_soul_access`，不會越權。UI 請求很少走到這條路。
  - UserPromptSubmit 在回應開始之前執行；用附的 nginx 範本時，一般路徑的讀取逾時是 nginx 預設的 60 秒，hook 超過約 60 秒請求就會失敗。UserPromptSubmit 的逾時請設在 60 秒以內。

## 後果

- 沒有設定時 hook 的接線點不改任何結果：接線點先問 `has_hooks`，不組事件內容（golden 不變）。下面幾項和 hook 設定無關、一律生效：決定 12（專案根的限制、檔案動詞不跟隨連結）、停止（等待中的登記、停止次數、`PROMPT_STOPPED`）、SNS 存外人訊息的時機。
- 設定了 PreToolUse 的工具，每次呼叫多等 hook 執行完（最多到逾時）；PostToolUse 也一樣，工具結果要等 hook 跑完才回到模型；Stop 不影響回覆。
- 審批卡事件多一個選填欄位 `hook_reason`；聊天請求被擋下時回 403 `HOOK_PROMPT_BLOCKED`，hook 執行期間或開始串流前被停止時回 409 `PROMPT_STOPPED`（停止可能來自另一個分頁、裝置或重置靈魂；前端當成中止，只提示一次已停止）。
- 聊天前端按停止時，若這個對話還沒有進行中的串流（含無痕查不到、上一則的結束登記還留著），改送這個對話不指名的停止（原本什麼都不送）。對話已存在時，和有串流時的停止一樣，這個對話正在跑的其他工作（例如心跳）也會停下；無痕與還沒存過的對話只停自己還在等的那一則。
- 新增三個管理端點（決定 10）與 `GET /hooks/active`（決定 17）；`openapi.json` 與 `api.ts` 隨之更新。
- 內建工具缺必要參數時回 `validation_error`、沒有 `tool_start`（決定 16），和 hook 設定無關、一律生效；以前有些舊測試與自訂用戶端靠工具自己報錯，現在看到的是這個結構化錯誤。
- 工具丟例外時，PreToolUse 的補充說明也接在錯誤結果後面（以前丟掉）。
- Stop 改走背景入口：同時有 32 個背景 hook 在跑時，這次 Stop hook 跳過並留紀錄（以前沒有上限）。
- 設定了 PermissionRequest 的工具，跳卡前多等 hook 執行完；PostToolUseFailure 也一樣，錯誤結果要等 hook 跑完才回到模型。
- 設定了 SubagentStart 時，子代理開始前多等 hook 執行完；設定了 Elicitation 時，表單跳出前多等 hook 執行完。其餘新事件都在背景，不影響原本的流程。
- 下面幾項和 hook 設定無關、一律生效：模型回 200 但讀不懂或沒有回覆時，這一輪是一般的模型失敗，不再是空白泡泡（決定 17）；串流以錯誤結束時，那則回覆的泡泡標「這一輪回覆失敗」，重試保存後存下來的節點多一個 `generationFailed`；MCP 互動的回合與整體上限各多 40 秒（Elicitation hook 的上限與收尾）；工作區設定寫不進去回 `CONFIG_SAVE_FAILED`（以前回成功）；Hub 條目缺欄位裝不了回 422（以前 500）。
- 代理人的檔案動詞不跟隨連結（決定 12）：list 把連結列成名稱、find 與 grep 略過連結，路上有連結時讀寫刪回錯誤；指回根裡面的連結照常可用。
- 撤回：把 `enabled` 設成 false 或清空 `hooks` 只停用 hook；決定 12、停止與 SNS 的改動不看 `enabled`，要撤回只能 revert（整個移除，`hooks.json` 留著無害）。

## 替代方案

- **照 Claude Code 經 shell 執行**：事件內容或參數可能被當成指令執行，也較難檢查用到哪些腳本。不採用。
- **hook 程式經隔離 runner 執行**：hook 常需要讀專案或呼叫外部工具，執行器的限制會讓大多數用途失效；改以「只有人能設定、腳本在受保護位置」保證代理人無法影響它。不採用。
- **允許 `allow`／`updatedInput`**：會讓 hook 成為繞過審批與底線的途徑。不採用。
- **每個靈魂或專案各一份設定**：設定入口與權限檢查變多，現在沒有需求。不採用。

## 驗證

- `tests/unit/test_hooks_config.py`：設定模型（只收已接線事件、handler 型別、標頭環境變數、標頭名稱與 Touka 自己送的標頭、上限）。
- `tests/unit/test_hook_paths_security.py`：參數必須在 `config/hooks/`（含 `:` 串起來的路徑清單、黏在單字母選項或 `@` 後面的路徑）、`file:` 網址拒收（含字詞中間的）、字詞中間 `:`／`@` 後面的絕對路徑（`-javaagent:<路徑>`，Windows 也查得到）、Windows 上夾在字詞中間或黏在選項後面的 `D:x`、網址參數依平台的路徑清單分隔（POSIX 拒收）、字詞中間 `:`／`@` 後面接 `//` 的路徑（`:` 前面是網址的 scheme 才略過）、經連結或別名指進 `config/hooks/` 的指令、專案清單變動不影響 `config/hooks/` 裡的腳本、禁區（工作區根、專案根與翻譯後路徑、MCP 可寫資料夾、容器鏡像路徑）、讀不到 MCP 隔離設定檔、以檔案識別碼擋別名、連結（含 `config/hooks/` 本身是連結）、硬連結、相對參數、UNC 與磁碟機相對路徑、讀不到狀態、使用者登記的根路上有連結時只禁字面（Touka 自己的資料夾照舊跟著解析）、沒有連結的登記根照舊比對識別碼。
- `tests/unit/test_project_root_protected_security.py`：受保護的位置與上層、本機安裝根底下 Touka 自己的第一層資料夾、經連結指到設定資料夾、容器裡的 Touka 目錄、使用者資料夾照常可用、專案工具對一般身分拒絕受保護的專案根並回結構化錯誤、檢查沒做完時分開說明也不放行、管理員與只有管理員擁有的靈魂不受限、有一般使用者共同擁有的照一般身分檢查、代理人（含 Debugger 與只有管理員擁有的靈魂）的專案根路上有連結就拒絕且不寫入（人類照舊）、查過專案根之後才換成連結時八個動作都拒絕且不碰 hook 資料夾、登記時說明被拒的原因。
- `tests/unit/test_file_ops_no_follow_security.py`：檢查之後才把路上一段換成連結時，write、append、新檔、read、str_replace、delete 都拒絕且不碰根外面的檔案，list、find、grep 遇到換成連結的根拒絕（直接 I/O 與經 `ResourceManager` 兩條路）；呼叫端查過的根在讀寫前被換成連結時七個 core op 都拒絕，沒有連結時照常；list 只列連結名稱、find 與 grep 不進入；指回根裡面的連結照常可用；沒有連結時編輯、追加、建上層資料夾、刪除照常；輔助函式對任何一段（含最後一段與還不存在的上層）的連結拒絕，只在開檔那一刻是連結也拒絕、不讀寫根外面的檔案（Windows 比對檔案識別碼；原子寫入的暫存檔不寫進內容）；走訪略過連結與排除的名稱、不進入讀不到狀態的資料夾；原子寫入權限 0600；FIFO 不卡住；`ResourceManager` 的不跟隨連結寫入照舊保護靈魂；find 的比對（大致同 `rglob`）且很多 `**` 也很快；find、grep 找到 50 筆就停；`is_link` 認得 symlink 與 junction，沒有 junction 的系統照常判斷；讀不到狀態時交給 `ResourceManager`，走訪前的連結檢查也經它；POSIX 被鎖住時例外帶絕對路徑（上層某一段、建上層、檔案本身、刪除、暫存檔）。`tests/unit/test_personal_catalog.py`：讀目錄時 `.touka` 被換成連結不丟例外。`tests/integration/test_isolation_linux.py`：個人空間、子資料夾或檔案被鎖住時，檔案動詞經 `ResourceManager` 權限自癒（讀、編輯、列出當次成功，寫入、刪除下一次成功，不記成白名單外）。`tests/lint/test_resource_manager_permission_repair.py` 登記新方法與不跟隨連結的讀寫輔助函式。
- `tests/unit/test_sns_gateway.py`：被擋下的外人訊息不寫進對話、不回覆也不往外丟錯，被停止的照寫、不回覆也不往外丟錯，其他錯誤照舊寫入、通過時先寫訊息再讀回覆。
- `tests/unit/test_interjection_endpoint_security.py`：插話被擋回 403 且不進佇列、擋下理由有上限、事件內容（含 `interjection` 與無痕）、補充說明不採用、hook 服務出錯照樣進佇列、陌生人碰不到 hook、hook 執行期間串流換了回 404。
- `tests/unit/test_hook_runners_security.py`：不經 shell、環境不含金鑰、逾時真的結束程式、逾時或被取消時在執行緒裡結束程式（不卡住事件迴圈）、輸出上限、http 不跟隨轉址、整次請求的逾時與讀到上限就停、要求不壓縮的回應、壓縮過的回應算失敗且不解開（一層與兩層 gzip）、失敗的回應照 HTTP 狀態說明（有壓縮也一樣）且不讀內容（回應內容被讀就記下）。
- `tests/unit/test_hook_service.py`：解讀規則（含 `continue: false`）、同時執行、只擋不放行、執行紀錄與無痕、壞檔／讀不到／被刪掉時沿用上一份、服務出錯不外洩例外、存檔 CAS 與路徑檢查、寫不進檔案時回存檔錯誤並沿用上一版、落單的 surrogate 不讓 hook 停擺。
- `tests/unit/test_hook_tool_events_security.py`：決定 16 的四個事件與缺參數（每個事件內容的鍵、比對規則、只有 `deny` 生效、`allow`／`updatedInput` 不放行、hook 失敗不擋、沒有 hook 不組內容、遮罩、固定的原因代碼、各拒絕出口、卡片送出後才通知、背景入口的上限與紀錄、無痕）。`tests/unit/test_isolation_status.py`：真的 monitor 層級改變時發一次 Notification。`tests/unit/test_secret_text.py`：憑證樣式。ToukaChat `HookEventSection.test.tsx`：比對規則提示依事件不同。
- `tests/unit/test_hook_lifecycle_events_security.py`：決定 17 的事件清單、輸出解讀、StopFailure 的類型與兩條路徑（子代理不發、不發兩次）、讀不懂的回覆、子代理開始與結束（補充說明只進第一則、審查員輸入不變、停止就不開始）、剪裁前後（不等 hook、沒存好不發 PostCompact）、任務（每個任務一次、只有 `DONE`）、Elicitation（只有拒絕有效、代填不採用、出錯照樣問人）、心跳（有開始才有結束、結束原因）、ConfigChange 內容。`tests/unit/test_hook_config_change_security.py`：每個會改狀態的路由都分類、通知的路由都是 async、區段都用到；存檔失敗不發、確認存好才發、回擋下照樣存好、hook 卡住不拖住回應、關閉 hooks 成功且不發、預覽不發、創建只在真的寫入時發。`tests/unit/test_hooks_router_security.py` r13～r16：`/hooks/active` 任何已登入身分只得到布林值、沒登入拒絕、只算帶對話內容且啟用的 hook。ToukaChat `IncognitoHookNotice.test.tsx`：有 hook 才提示、一般對話不查、中途打開下一則就提示、查不到維持上次；`MessageItem` 與 `stream_tree` 的測試：回覆失敗的標示。實際驗證見 ADR-0295（`e2e-real/hooks-lifecycle.spec.ts`）。
- `tests/unit/test_hook_wiring_security.py`：四個事件的接線（只在真的請求執行、擋下帶理由、補充說明是資料、UserPromptSubmit 不帶模式、PreToolUse 擋下不送審不跳卡、政策已拒絕的不送 hook、要求確認跳標成 hook 的卡且不送審、無人在場拒絕、YOLO 也要人、`allow` 不放行、matcher、舊工具名稱比對新名稱、hook 看到的是模型送來的參數、PostToolUse 只在成功後、太深的 JSON 結果改接資料區塊、Stop 在背景、hook 服務出錯不影響結果、沒有設定時結果不變；hook 執行期間停止時不送模型也不執行工具、擋下優先於停止、沒有 hook 執行（含 hook 全部停用）或沒比到時不看停止旗標、停止標在請求上（之後的請求清掉對話的旗標也一樣停）、UserPromptSubmit 與 PreToolUse 都看這次執行開始之後的停止次數（之後的請求清掉旗標也一樣停）、前景請求清掉旗標之後才記停止起點、開始串流前只看這次執行自己的記號（只剩旗標時交給 ReAct 迴圈）、UI 請求在串流開始前登記為等待中且結束時移除、MCP 工具用裸名稱叫時也比對 `server__tool` 與 `mcp__server__tool`）、群聊成員各自觸發 Stop 且事件內容是這位成員、除錯執行時事件內容的 `agent_id` 是字串。
- `tests/unit/test_stream_resume_endpoints.py`：還沒開始串流的請求，請求者本人不指名停止就標成已停止、不讀這一輪的對話內容（含無痕、上一則的結束登記還留著）；查不到對話時不停整個對話，對話已存在時才停；別人不能用它停；借用沒有權限的對話 id 時回 403、什麼都不停；查對話期間串流開始時要指名；只標這個人的請求；有進行中的串流時不指名的停止照樣回 404。
- `tests/unit/test_dispatcher_resumable.py`：還沒開始串流就被停止的請求不換綁對話、不建立串流、不送模型（無痕與一般對話）；換綁對話期間按的停止也不建立串流。
- `tests/unit/test_continuity_event_hooks.py`：心跳開始之後被停止、旗標又被清掉時，脈衝不跑。
- `tests/unit/test_rhythm_interrupt.py`：停止次數只增不減，清掉旗標或會話都不動它。
- `tests/unit/test_proxy_fallback_family.py`：使用者停止不記成主要模型失敗、不改用備援模型。
- `tests/unit/test_config_live_validation.py`：啟動時壞掉的設定檔，提示照同資料夾有沒有 `.example` 說刪掉會不會重建（下次啟動照範本補回），`hooks.json` 的範本是關閉的（所有 hook 都不執行），沒有範本的檔說刪掉就少了那份設定，讀不到範本時說看不出來、不另外丟例外。
- `tests/unit/test_request_tail_cache_breakpoint.py`：多模態訊息加上 hook 輸出時，Claude 的快取斷點仍打在 hook 輸出與 continuity 之前。
- `tests/unit/test_group_soul_only.py`：hook 補充說明裡的 `@別名` 不算點名。
- `tests/unit/test_hard_floor_security.py`：讀寫 `hooks.json` 屬 F2，寫 `hooks.json.example` 與 `config/hooks/` 屬 F3。
- `tests/unit/test_public_repo_hygiene.py`：`config/hooks/` 不進版控。
- `tests/unit/test_hooks_router_security.py`：只有人類管理員能讀寫（admin 代打、一般使用者、靈魂、DebugAgent、DispatchAgent、子代理、沒帶憑證與壞掉的權杖都被拒）、存檔寫檔並回 revision、作者由伺服器填、舊 revision 拒絕且不動檔、路徑被拒不寫檔、不可信的 Origin 拒絕、格式不對拒收、執行紀錄與無痕、壞檔時的 revision、`/v1` 前綴、設定匯出不含 `hooks.json`。
- ToukaChat：還沒有進行中的串流時按停止會送這個對話的停止（含讀不到對話與只剩上一則的結束登記），停止回 404 時再讀一次、是這一輪才指名停止；審批卡的 hook 理由、串流事件解析、被擋訊息的通知與理由；被擋下的這一輪從對話樹拿掉（其他分支保留；被擋的是第一則時接到剩下最新的第一則那條分支）、照完成流程存回並換回標題、文字放回輸入框；插話被擋時不進佇列、顯示理由並放回文字；設定頁的草稿函式、資料層（revision、錯誤分類、存好後重新讀取失敗）、分頁（事件區塊、編輯後送出的設定、存檔被拒的原因、壞檔提醒）與執行紀錄清單（類型、結束碼、HTTP 狀態）。
