# ADR-0292：Hook（丙-1：引擎、安全界線、四個事件與設定頁）

- 狀態：✅ 已實作（丙-1；其餘事件在丙-2）。維護者 2026-10-28 要求前後端加上 hook 與設定介面、行為盡量和 Claude Code 相同、對不上的先列表；一頁計畫由維護者核准，丙-1 加做對抗式複審。
- 日期：2026-10-28
- 決策者：維護者
- 相關：ADR-0146（回合結束的約定）、ADR-0228／0229（工具執行順序與可信 context）、ADR-0239（`execution_broker`）、ADR-0246（模式、審查員與底線）、ADR-0257（歷史資料界線）、ADR-0269（只執行這一輪列出的工具）、ADR-0272（隔離層級；hook 程式不是代理人撰寫的程式，不經 runner，見決定 4）。
- 影響範圍：ToukaProxy 新增 `models/hooks.py`、`services/hooks/`、`middleware/layers/hooks.py`；修改 `services/tool_executor.py`、`handlers/react_core.py`、`services/proxy_service.py`、`services/approval_flow.py`、`utils/tool_events.py`、`services/prompt_blocks.py`、`adapters/content_normalize.py`、`services/continuity_service.py`、`services/context_snapshot_service.py`、`utils/bootstrap.py`、`utils/hard_floor.py`、`utils/async_db.py`、`services/group_router.py`、`tools/project_tools.py`、`routers/ui_sessions.py`、`routers/ui_interjections.py`、`services/stream_buffer_service.py`、`services/dispatcher.py`、`services/sns_gateway.py`、`services/authorization_gateway.py`、`services/isolation/profiles.py`、`services/resource_manager.py`、`.gitignore`、`.dockerignore`；新增 `routers/hooks.py`（掛在 `main.py` 的 `CORE_ROUTERS`）、`services/hooks/interjection_hooks.py`；設定 `config/hooks.json.example`、`errors.json.example`（6001～6003、1222 `PROMPT_STOPPED`）；`openapi.json`。ToukaChat：設定畫面「Hooks」分頁（`components/settings/tabs/HooksTab.tsx`、`tabs/hooks/`、`hooks/settings/useHooksSettings.ts`、設定註冊表與搜尋索引）、審批卡、串流錯誤通知、被擋下的這一輪從對話拿掉並放回輸入框（`lib/stream_tree.ts`、`lib/providers/StreamRegistryProvider.tsx`、`hooks/useChatStream.ts`、`lib/providers/ChatProvider.tsx`）、插話被擋（`hooks/useInterjectionQueue.ts`、`hooks/useInterjectionComposer.ts`）、`lib/tree_history.ts`、`error_codes.ts`、`api.ts`、`lib/types.ts`、翻譯。

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
   - 檔案動詞與 MCP 工具不經這條底線。一般身分的檔案動詞到不了設定資料夾（決定 12）；不受專案限制的身分（管理員、Debugger、只有管理員擁有的靈魂）若把專案登記在涵蓋設定資料夾的位置，代理人就能用檔案動詞讀到標頭裡的權杖、改寫 `hooks.json`，由管理員自己負責不這樣登記（和決定 12 的界線相同）。
   - `hooks.json.example` 與 `config/hooks/` 底下的腳本屬 F3 的政策與設定。
   - `config/hooks/` 只放管理員自己的腳本，不進版控也不進映像（`.gitignore`、`.dockerignore`）。
2. **兩種 handler**：
   - 事件內容是 UTF-8 的 JSON；無法編碼的字元（落單的 surrogate）換成 `?`，hook 照樣執行。
   - `command`：`command` 是程式的絕對路徑，`args` 是參數清單，**不經 shell**；事件內容只從標準輸入給。經 `execution_broker.spawn_exec` 啟動，環境只給 `LaunchEnvironmentPolicy.launcher_base()`（不含金鑰），工作目錄是 `config/hooks/`，`close_fds`、獨立行程群組；逾時、等待被取消或出錯時結束整個行程樹；標準輸出與錯誤各只留前 64 KiB。
   - `http`：POST 事件內容，`follow_redirects=False`、`trust_env=False`；逾時是整次請求（含讀回應）的上限，回應讀到 64 KiB 就停止讀取並關閉連線；3xx 與非 2xx 都算失敗。標頭照字面送出，寫成 `$VAR`／`${VAR}` 拒收（不替換環境變數，避免以為有效或洩金鑰）；標頭名稱必須是 HTTP 的 token（不能有空白、冒號等分隔字元），Touka 自己送的 `Content-Type`、`Content-Length`、`Host`、`Transfer-Encoding` 不能設定（不分大小寫）。
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
   - 參數：拆成字詞逐一檢查（`--script=x`、`-c "… /path …"`、POSIX 用 `:` 串起來的路徑清單如 `-cp a.jar:/data/x.jar`、黏在單字母選項後面的絕對路徑如 `-I/data/lib`、`@` 後面的參數檔也算），看起來像路徑的字詞都必須在 `config/hooks/` 裡（相對路徑以 `config/hooks/` 為起點），在外面回 `outside_hooks_dir`；`file:` 網址看不出最後讀哪個檔，一律拒收（`file_url`，不分大小寫，字詞中間的如 `jar:file:` 也算）。這個判斷只看 `config/hooks/` 本身，不看專案清單一類會變動的資料。
   - 指令在 `config/hooks/` 裡時照參數的規則：字面與解析後都在裡面；`config/hooks/` 本身與底下經過的每一層都不能是連結（symlink、junction），否則回 `linked_path`；檔案不能有其他硬連結（`nlink>1`，回 `hard_link`），另一個名字可能在代理人的資料夾裡。字面在別處、其實在 `config/hooks/` 裡的指令（經過連結、短檔名，或上層某一層的檔案識別碼就是 `config/hooks/`，例如 bind mount）也照這條查，所以會被拒收：要照字面寫 `config/hooks/` 裡的路徑。
   - 字詞中間 `:`／`@` 後面的絕對路徑（`-javaagent:<路徑>`、`pkg@<路徑>`）也取出來查，含 `//` 開頭的（Windows 上是 UNC，回 `unc_path`）；只有 `:` 前面整段是網址的 scheme（`https://`、`jdbc:postgresql://`）時才當成網址略過。
   - 指令在別處（例如系統的直譯器）時不能落在禁區：資料根、sandbox state、執行器資料夾、工作區根（使用者能在底下自己登記專案）、每個專案根（原路徑與翻譯後的路徑）、MCP 伺服器的可寫資料夾，容器裡另加鏡像掛載根；設定資料夾裡 `config/hooks/` 以外的地方也不行（`skills`、`mcp_servers`、`evals` 由創建與演化寫入）。讀不到 MCP 隔離設定檔時當成無法確認，不執行。
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
     - SNS：外人訊息等 pipeline 跑完才寫進對話；被擋下的不寫、不回覆外人（擋下原因是設定的一部分），也不往外丟錯，只記一筆 log；其他錯誤照舊先寫再往外丟。回覆是串流，寫好之前不會讀，寫不進去就不呼叫模型。因為等 pipeline 跑完才寫，家對話模式下這一句也不會在送給模型的家對話裡多出現一次；同一個對話幾乎同時進來兩則時，hook 跑得久的那則較晚寫進對話，紀錄順序可能和到達順序不同。
     - 這是「使用者送出訊息」的檢查，不是管住使用者或代理人的機制：心跳的脈衝提示、排程寫的提示（`init_prompt`）、重新觸發（`/heartbeat/retrigger`）與只有工具結果的回合都不觸發。
   - 插話（ADR-0249）：插話也是使用者送出的文字，進佇列前跑 UserPromptSubmit（`services/hooks/interjection_hooks.py`，事件內容多 `interjection: true`、`agent_id` 取自這次串流）。擋下回 403 `HOOK_PROMPT_BLOCKED`、不進佇列，前端顯示理由並把文字放回輸入框；hook 的補充說明不採用（插話直接接在這一輪的對話裡，沒有「只用在這一次請求」的位置）；hook 服務出錯照樣進佇列。hook 執行期間串流換了或結束了，回 404 `STREAM_NOT_FOUND`，不進新的串流。
   - PreToolUse／PostToolUse：`ToolExecutor` 內，matcher 比對 Touka 的工具名稱，MCP 工具另比對工具清單上的正式名稱 `server__tool` 與 Claude Code 寫法 `mcp__server__tool`（模型用裸名稱叫也比對得到這兩種）；PostToolUse 只在成功後執行，事件內容多 `tool_response`。
     - 只涵蓋經 `ToolExecutor` 的工具呼叫：任務的驗證指令（`verification_cmd`）、演化與創建的試跑由系統自己啟動，不觸發 PreToolUse，照原本的隔離與審批規則執行。人按的除錯執行（`/debug/execute`）也經 `ToolExecutor`，同樣會觸發。
     - 工具在等 PreToolUse hook 時使用者按了停止，hook 跑完後不執行這個工具，回結構化 `permission_denied`（`reason: "stopped_by_user"`）；和 UserPromptSubmit 一樣看這次執行開始之後有沒有被停止過，下一則請求清掉旗標也照樣不執行。沒有 hook 真的執行時不看停止。
     - 舊工具名稱（`LEGACY_ADVERTISED_SUCCESSORS`）也比對它對應的新名稱：規則寫 `execute_command`，模型叫 `bash` 也比得到；舊檔案工具的 `action` 剛好是某個新名稱時只對應那一個，其他動作對應全部檔案動詞（寧可多比到）。事件內容的 `tool_name` 照模型叫的名稱，另加 `tool_aliases` 列出對應的新名稱。
     - 事件內容的 `tool_input` 是模型送來的參數：執行器之後替 MCP 工具補上的 `agent_id`、換成代理人自己資料夾實際路徑的根目錄參數，都不會出現在 hook 看到的內容裡。公開參數的驗證在審批之後才做（ADR-0229），所以 hook 看到的是驗證之前的原樣：欄位可能是別名、多出不認得的欄位或型別不對，hook 不能假設已經正規化。
   - Stop：ReAct 迴圈回完之後在背景執行（`spawn_logged`），回覆不等它；子代理的回合不執行；只記錄，不能讓代理人繼續（ADR-0146：回文字就結束這一輪）。
   - 心跳的每次脈衝都會觸發工具前後與 Stop；脈衝提示不是使用者訊息，所以不觸發 UserPromptSubmit。心跳的停止次數在整次執行開始時記一次，脈衝之間與工具 hook 跑完時都看：同一個對話的 UI 請求清掉旗標，心跳也停得下。
   - 每個事件都有 `session_id`、`agent_id` 與 Touka 多的 `incognito`（hook 自己決定無痕對話的內容要不要留）。
8. **執行紀錄**：最近 50 筆存在記憶體（重啟清空），記時間、事件、hook 名稱與類型、結果、耗時、結束碼或 HTTP 狀態與說明。說明是擋下或要求確認的理由、失敗原因（例外只記類型、stderr 只取第一行）與不採用的欄位，有長度上限，不含 stdout 的內容。無痕對話照跑 hook，但紀錄只留時間、事件、結果與耗時。
9. **存檔**：只經管理 API，以 revision（內容 SHA-256）做 CAS，對不上回 `HOOK_CONFIG_CONFLICT`（409）；存檔前跑決定 5 的檢查；存檔前讀不到目前的檔案、或寫不進檔案（磁碟滿、沒有權限、被鎖住）回 `CONFIG_SAVE_FAILED`（500），不寫檔，執行中的仍是上一版。設定頁存好後重新讀取失敗時，留著剛存的內容與新的 revision，不當成讀取失敗（下一次存檔才不會誤判成衝突）。
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
    - 下方列最近的執行紀錄（hook 名稱與類型、結果、結束碼或 HTTP 狀態、耗時與說明），無痕的只顯示時間、事件與結果。
    - 說明寫明 hook 失敗時不擋動作，不能當成唯一的安全防線；所有使用者的對話（含無痕）都會觸發；「使用者送出訊息」只檢查聊天送出的文字，不是管住使用者的機制，「工具執行前」只涵蓋代理人的工具呼叫；腳本與參數裡的路徑要放在 `config/hooks/`，程式要放在 `config/hooks/` 或代理人寫不到的地方，不要用 `env`、`npx`、`py` 這類啟動器。
12. **一般使用者的專案根不能放在 Touka 保護的位置**（`services/isolation/profiles.project_root_reason`）：決定 5 把專案根當禁區，但專案是使用者自己登記的；專案根若是設定資料夾、`config/hooks/` 或它們的上層，代理人就能經專案檔案工具寫進 hook 腳本。
    - 判斷沿用 ADR-0272 的永不授權規則：受保護的位置與它們的上層、經連結指到受保護位置的一律拒絕；容器裡工作區掛載根本身也不能當專案根，另查 Touka 目錄（只有使用者自建的頂層資料夾能當專案）。
    - 本機安裝不往上找 Touka 目錄（安裝根就是 Touka 目錄，往上找會擋掉所有專案），改用固定規則：專案根在安裝根底下時，第一層資料夾不能是 Touka 自己的（點開頭的，或 `TOUKA_TOPLEVEL_PROTECTED` 裡的 `ToukaChat`、`ToukaProxy`、`docs`、`scripts`、`tests`、`data`、`backups`、`venv`、`node_modules` 等，不分大小寫），例如 `.venv`、`.git/hooks`、`.claude` 底下都不行；安裝根本身也不能當專案。
    - 容器裡的 Touka 目錄判斷會誤擋：專案裡（往下 3 層內）剛好有和 Touka 標記或資料根同名的資料夾就當成受保護的位置；資料夾太多、有讀不到的資料夾或還在掃（前景最多等 2 秒，背景繼續掃、結果快取 10 分鐘）時回「檢查沒做完」（`scan_limit`），和受保護的位置分開說明，也不放行。快取過期後會重掃，大的專案大約每 10 分鐘可能遇到一次「檢查沒做完」，稍後再試即可。
    - 三處檢查：登記與更新專案（`ui_sessions._check_project_root`，回 422 並帶 `reason`：`outside_workspace`、`protected_location` 或 `scan_limit`）、列專案檔案與每次專案檔案工具呼叫（`project_tools`，回結構化 `permission_denied`，`reason` 同上，檢查沒做完時建議稍後再試）。聊天畫面的專案檔案樹（`ui_sessions` 的檔案樹端點）被拒時照舊回不分原因的 403，檢查沒做完時也一樣，稍後重新整理即可。
    - 管理員與 Debugger 不受這條限制（他們本來就能指定任何位置，`resource_manager.bypasses_project_jail`）；只有管理員擁有的靈魂也不受限（前景交談時執行身分會換成靈魂本身，ADR-0154，所以另查擁有者，`authorization_gateway.only_admins_own`）。有一般使用者共同擁有的靈魂照一般身分檢查：一般使用者也能用它對話，不能因為管理員也擁有就放寬。這是已知的界線：管理員登記在受保護位置的專案，只有管理員擁有的靈魂用專案工具時仍寫得到，由管理員自己負責不這樣登記。其他靈魂在前景與心跳都照這條檢查。
    - 行為變更：一般使用者的專案不能在受保護的資料夾裡，也不能是它們的上層（例如工作區根就是 Touka 安裝根時，工作區根本身不能當專案）；既有的這類登記留著，但專案工具會拒絕。
13. **群聊**：hook 補充說明裡的 `@別名` 不算點名（`group_router` 找點名前先拿掉 `<hook_output>` 區塊），hook 的輸出不能叫其他靈魂發言。
14. **被擋下的這一輪從對話拿掉**（和 Claude Code 一樣）：聊天前端送出時先存使用者這一輪（ADR-0231），被 UserPromptSubmit 擋下後，把這則訊息和它的空回覆從對話樹拿掉，照「後端沒有存文字」的完成流程以 revision 比對存回去（後端在送模型前就拒絕，什麼都沒寫）；之後心跳、重新觸發、滑動視窗摘要與歸檔讀存檔都看不到它。對話標題若是用被擋文字開頭建的，一起換回。文字放回輸入框（還在這個對話、而且輸入框還空著時），顯示擋下的理由。
    - 已知的空窗：這一輪在 hook 執行前已經存了；hook 執行期間剛好跑心跳，或存回時 revision 對不上（別的分頁改過），被擋的訊息會留在存檔裡，要使用者自己刪。hook 執行期間使用者按了停止、重新整理、關掉分頁，或請求先被 nginx 逾時切斷，前端已經不在等這次回應，hook 之後擋下時也一樣留在存檔裡（停止時不會送模型，決定 7）。
    - 放回輸入框的只有文字：附件不放回；編輯舊訊息後重送被擋時，文字也不放回（編輯框已經關掉），畫面跳到被編輯那則訊息的上一層（原本的分支還在，切換分支就看得到）。
    - 自己組歷史的用戶端（IDE、自訂 client）不在這個範圍：被擋下的訊息若被它們放進之後的請求，hook 只看最後一則 user 訊息（決定 7），較早的那則會送到模型。
15. **失敗不擋（fail-open）**：hook 自己失敗（結束碼、逾時、連不上、路徑被拒、輸出壞掉）和 hook 服務本身出錯都不擋動作；前者記在執行紀錄與 log，後者只記 log。例外內容不回給模型或用戶端，只記類型。所以 hook 不能當成唯一的安全防線：模型可以故意讓 hook 失敗（例如送很大的參數讓 hook 逾時、送讓 hook 腳本出錯的內容），hook 失敗時動作照常執行。「失敗就擋」列為下一批，讓每個 hook 自己選。

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
  - matcher 的正規表示式是 Python 的 `re`（`re.search`，不錨定）；只有字母、數字、`_`、`-`、空白、`,`、`|` 的寫法當成精確名稱或清單（`,` 也能分隔）。
- **Touka 沒有對應功能，不做**：工作目錄切換、檔案監看、worktree、Agent 團隊相關事件。
- **下一批**：hook 類型 `mcp_tool`、`prompt`、`agent`（後兩種會用到模型額度）；非同步 hook（`async`）、`if` 條件、`once`；每個 hook 可選的「失敗就擋」；設定頁與 `GET /hooks` 遮住 http 標頭的值。
- **已知的界線**：
  - Windows 上 `.bat`／`.cmd` 由系統經 `cmd.exe` 啟動，參數會被 `cmd.exe` 重新解讀；要避開 shell 就用其他程式寫 hook。
  - 參數只檢查看起來像路徑的字詞：直譯器的 `-c`／`-m` 給的程式碼或模組名稱不是路徑，hook 實際執行哪些程式碼由管理員負責。
  - MCP 可寫資料夾只來自 `mcp_server_isolation.json` 的 `sandbox_paths`：包在 Landlock 裡的第三方與 scaffold 伺服器所引用的資料夾（啟動時才算出來、可寫）不在禁區；IDE 與未隔離的 MCP 伺服器能寫哪裡 Touka 不知道。管理員不要把 hook 程式放在這些地方，也不要讓 IDE 或 MCP 伺服器寫得到 `config/hooks/`。
  - 只檢查設定寫得出來的程式：`env`、`npx`、`py` 這類啟動器會自己找要執行的程式（依 PATH、上層的 `node_modules` 或自己的設定），找到的程式在哪裡 Touka 不檢查。請寫成直譯器＋`config/hooks/` 裡的腳本，不要用啟動器當 hook 的程式。直譯器自己也會找模組：Node 的 `require` 會一路往上層資料夾找 `node_modules`，Python 依 `sys.path` 找；腳本用到的套件請裝在 `config/hooks/` 裡（例如 `config/hooks/node_modules`），不要依賴上層資料夾的套件。
  - Linux／Docker 上參數不能放網址：POSIX 的 `:` 也是路徑清單的分隔（`-cp a.jar:/x.jar`），`https://host/x` 會被拆出 `//host/x`，當成 `config/hooks/` 以外的絕對路徑拒收。網址不另外放行，因為 `-cp https://x/data/evil.jar` 這種寫法 Java 會讀 `//x/data/evil.jar`。要傳網址請寫在 `config/hooks/` 裡的設定檔，由腳本讀取；Windows 的 `:` 不拆，網址照常可用。
  - PreToolUse 與 PostToolUse 只涵蓋 Touka 代理人經工具執行器的工具呼叫；ToukaDispatch 給外部 AI 的派工工具不經工具執行器，不會觸發。
  - hook 程式本身可以讀寫任何 Touka 權限能到的地方（決定 5 最後一點）。像「工具執行後跑 lint」這類 hook，會以 Touka 的權限處理代理人剛寫的檔案；lint 工具若會執行檔案裡的設定或外掛，就等於以 Touka 的權限跑代理人寫的東西，管理員要選不會執行專案內容的工具。
  - 本機安裝時，安裝資料夾裡的 Python（例如 `.venv` 底下的直譯器）在工作區根裡，不能當 hook 的程式（`agent_writable`）；請用系統安裝的直譯器，腳本放 `config/hooks/`。
  - 腳本被刪掉或改名時，直譯器自己會以結束碼 2 結束（例如 Python 找不到檔案），照 Claude Code 的規則就是擋下，而且 stderr（含路徑）會當成理由顯示；這時所有符合的動作都會被擋，直到修好設定。
  - 事件內容一律送給 hook，無痕對話也一樣（決定 7 的 `incognito` 讓 hook 自己決定要不要留）；設定頁說明會寫明。之後在丙-2 讓使用者在無痕對話裡看得到「有 hook 在執行」。
  - 事件內容的 `session_id` 是請求帶來的對話 id，hook 不能把它當成已驗證的身分。
  - PreToolUse 的「要人確認」跳的是一般的人類審批卡，誰能按和其他審批卡相同（ADR-0246）；hook 不能指定由誰核准。
  - 備份還原會寫回 `hooks.json`（照原樣、不經存檔的路徑檢查）；還原後下一次執行前仍會檢查路徑（決定 5），不合規則的 hook 不執行。
  - hook 執行中按停止的時間窗：請求剛進來、還沒登記成等待中之前就送到的停止停不到它（大約幾毫秒）；不指名的停止若晚到、同一個人已經在同一個對話送出下一則，可能停到下一則；同一個對話在另一個分頁有進行中的串流時，這個分頁的不指名停止不會停到任何東西。
  - 無痕請求自己帶對話 id，Touka 不核對那個 id 是不是別人的對話（這次之前就是這樣，另案處理）；所以等待中的停止查不到對話時只標自己的請求，不停整個對話。
  - 等待中的請求「是不是同一位請求者」用執行身分判斷：兩個人用 X-Agent-ID 代打同一個靈魂時（管理員與擁有者，或兩位管理員；擁有者自己在前景交談時登記的是人類身分，不會和代打的人撞在一起），彼此的不指名停止都停得到對方在等的那一則。兩人本來都能操作這個靈魂的對話；之後改成登記時記下人類身分。
  - 黏在多字母選項後面、沒有 `=` 或 `:` 隔開的絕對路徑（POSIX 的 `-isystem/data/x`）認不出來：分不出選項名稱到哪裡為止，照單字母的規則拆會誤擋 `-Ilib/sub` 這類 `config/hooks/` 底下的相對路徑。請寫成 `--opt=/x` 或分成兩個參數。
  - 每次檢查有沒有 hook 都同步讀一次 `hooks.json`、算雜湊比對有沒有變（檔案很小；沒有這個檔時只多一次存在檢查）；這樣手動改檔也會在下一次執行時生效。
  - UserPromptSubmit 在回應開始之前執行；用附的 nginx 範本時，一般路徑的讀取逾時是 nginx 預設的 60 秒，hook 超過約 60 秒請求就會失敗。UserPromptSubmit 的逾時請設在 60 秒以內。
- **丙-2 接線**：工具失敗與被拒、跳出審批卡、通知、上游錯誤結束、子代理開始與結束、滑動視窗剪裁前後（不能拖住對話）、任務建立與完成、MCP 向使用者要資料（只能拒絕）、設定變更（不能擋關閉 hook），以及 Touka 自己的心跳開始與結束。

## 後果

- 沒有設定時行為不變：接線點先問 `has_hooks`，不組事件內容、不改任何結果（golden 不變）。
- 設定了 PreToolUse 的工具，每次呼叫多等 hook 執行完（最多到逾時）；PostToolUse 也一樣，工具結果要等 hook 跑完才回到模型；Stop 不影響回覆。
- 審批卡事件多一個選填欄位 `hook_reason`；聊天請求被擋下時回 403 `HOOK_PROMPT_BLOCKED`，hook 執行期間或開始串流前被停止時回 409 `PROMPT_STOPPED`（停止可能來自另一個分頁、裝置或重置靈魂；前端當成中止，只提示一次已停止）。
- 聊天前端按停止時，若這個對話還沒有進行中的串流（含無痕查不到、上一則的結束登記還留著），改送這個對話不指名的停止（原本什麼都不送）。對話已存在時，和有串流時的停止一樣，這個對話正在跑的其他工作（例如心跳）也會停下；無痕與還沒存過的對話只停自己還在等的那一則。
- 新增三個管理端點（決定 10）；`openapi.json` 與 `api.ts` 隨之更新。
- 撤回：把 `enabled` 設成 false 或清空 `hooks` 立即停用；revert 即可整個移除，`hooks.json` 留著無害。

## 替代方案

- **照 Claude Code 經 shell 執行**：事件內容或參數可能被當成指令執行，也較難檢查用到哪些腳本。不採用。
- **hook 程式經隔離 runner 執行**：hook 常需要讀專案或呼叫外部工具，執行器的限制會讓大多數用途失效；改以「只有人能設定、腳本在受保護位置」保證代理人無法影響它。不採用。
- **允許 `allow`／`updatedInput`**：會讓 hook 成為繞過審批與底線的途徑。不採用。
- **每個靈魂或專案各一份設定**：設定入口與權限檢查變多，現在沒有需求。不採用。

## 驗證

- `tests/unit/test_hooks_config.py`：設定模型（只收已接線事件、handler 型別、標頭環境變數、標頭名稱與 Touka 自己送的標頭、上限）。
- `tests/unit/test_hook_paths_security.py`：參數必須在 `config/hooks/`（含 `:` 串起來的路徑清單、黏在單字母選項或 `@` 後面的路徑）、`file:` 網址拒收（含字詞中間的）、字詞中間 `:`／`@` 後面的絕對路徑（`-javaagent:<路徑>`，Windows 也查得到）、Windows 上夾在字詞中間或黏在選項後面的 `D:x`、網址參數依平台的路徑清單分隔（POSIX 拒收）、字詞中間 `:`／`@` 後面接 `//` 的路徑（`:` 前面是網址的 scheme 才略過）、經連結或別名指進 `config/hooks/` 的指令、專案清單變動不影響 `config/hooks/` 裡的腳本、禁區（工作區根、專案根與翻譯後路徑、MCP 可寫資料夾、容器鏡像路徑）、讀不到 MCP 隔離設定檔、以檔案識別碼擋別名、連結（含 `config/hooks/` 本身是連結）、硬連結、相對參數、UNC 與磁碟機相對路徑、讀不到狀態。
- `tests/unit/test_project_root_protected_security.py`：受保護的位置與上層、本機安裝根底下 Touka 自己的第一層資料夾、經連結指到設定資料夾、容器裡的 Touka 目錄、使用者資料夾照常可用、專案工具對一般身分拒絕受保護的專案根並回結構化錯誤、檢查沒做完時分開說明也不放行、管理員與只有管理員擁有的靈魂不受限、有一般使用者共同擁有的照一般身分檢查、登記時說明被拒的原因。
- `tests/unit/test_sns_gateway.py`：被擋下的外人訊息不寫進對話、不回覆也不往外丟錯，其他錯誤照舊寫入、通過時先寫訊息再讀回覆。
- `tests/unit/test_interjection_endpoint_security.py`：插話被擋回 403 且不進佇列、擋下理由有上限、事件內容（含 `interjection` 與無痕）、補充說明不採用、hook 服務出錯照樣進佇列、陌生人碰不到 hook、hook 執行期間串流換了回 404。
- `tests/unit/test_hook_runners_security.py`：不經 shell、環境不含金鑰、逾時真的結束程式、逾時或被取消時在執行緒裡結束程式（不卡住事件迴圈）、輸出上限、http 不跟隨轉址、整次請求的逾時與讀到上限就停。
- `tests/unit/test_hook_service.py`：解讀規則（含 `continue: false`）、同時執行、只擋不放行、執行紀錄與無痕、壞檔／讀不到／被刪掉時沿用上一份、服務出錯不外洩例外、存檔 CAS 與路徑檢查、寫不進檔案時回存檔錯誤並沿用上一版、落單的 surrogate 不讓 hook 停擺。
- `tests/unit/test_hook_wiring_security.py`：四個事件的接線（只在真的請求執行、擋下帶理由、補充說明是資料、UserPromptSubmit 不帶模式、PreToolUse 擋下不送審不跳卡、政策已拒絕的不送 hook、要求確認跳標成 hook 的卡且不送審、無人在場拒絕、YOLO 也要人、`allow` 不放行、matcher、舊工具名稱比對新名稱、hook 看到的是模型送來的參數、PostToolUse 只在成功後、太深的 JSON 結果改接資料區塊、Stop 在背景、hook 服務出錯不影響結果、沒有設定時結果不變；hook 執行期間停止時不送模型也不執行工具、擋下優先於停止、沒有 hook 執行（含 hook 全部停用）或沒比到時不看停止旗標、停止標在請求上（之後的請求清掉對話的旗標也一樣停）、UserPromptSubmit 與 PreToolUse 都看這次執行開始之後的停止次數（之後的請求清掉旗標也一樣停）、前景請求清掉旗標之後才記停止起點、開始串流前只看這次執行自己的記號（只剩旗標時交給 ReAct 迴圈）、UI 請求在串流開始前登記為等待中且結束時移除、MCP 工具用裸名稱叫時也比對 `server__tool` 與 `mcp__server__tool`）。
- `tests/unit/test_stream_resume_endpoints.py`：還沒開始串流的請求，請求者本人不指名停止就標成已停止、不讀這一輪的對話內容（含無痕、上一則的結束登記還留著）；查不到對話時不停整個對話，對話已存在時才停；別人不能用它停；借用沒有權限的對話 id 時回 403、什麼都不停；查對話期間串流開始時要指名；只標這個人的請求；有進行中的串流時不指名的停止照樣回 404。
- `tests/unit/test_dispatcher_resumable.py`：還沒開始串流就被停止的請求不換綁對話、不建立串流、不送模型（無痕與一般對話）；換綁對話期間按的停止也不建立串流。
- `tests/unit/test_continuity_event_hooks.py`：心跳開始之後被停止、旗標又被清掉時，脈衝不跑。
- `tests/unit/test_rhythm_interrupt.py`：停止次數只增不減，清掉旗標或會話都不動它。
- `tests/unit/test_proxy_fallback_family.py`：使用者停止不記成主要模型失敗、不改用備援模型。
- `tests/unit/test_config_live_validation.py`：啟動時壞掉的設定檔，提示照同資料夾有沒有 `.example` 說刪掉會不會重建（下次啟動照範本補回），`hooks.json` 的範本是關閉的（所有 hook 都不執行），沒有範本的檔說刪掉就少了那份設定。
- `tests/unit/test_request_tail_cache_breakpoint.py`：多模態訊息加上 hook 輸出時，Claude 的快取斷點仍打在 hook 輸出與 continuity 之前。
- `tests/unit/test_group_soul_only.py`：hook 補充說明裡的 `@別名` 不算點名。
- `tests/unit/test_hard_floor_security.py`：讀寫 `hooks.json` 屬 F2，寫 `hooks.json.example` 與 `config/hooks/` 屬 F3。
- `tests/unit/test_public_repo_hygiene.py`：`config/hooks/` 不進版控。
- `tests/unit/test_hooks_router_security.py`：只有人類管理員能讀寫（admin 代打、一般使用者、靈魂、DebugAgent、DispatchAgent、子代理、沒帶憑證與壞掉的權杖都被拒）、存檔寫檔並回 revision、作者由伺服器填、舊 revision 拒絕且不動檔、路徑被拒不寫檔、不可信的 Origin 拒絕、格式不對拒收、執行紀錄與無痕、壞檔時的 revision、`/v1` 前綴、設定匯出不含 `hooks.json`。
- ToukaChat：還沒有進行中的串流時按停止會送這個對話的停止（含讀不到對話與只剩上一則的結束登記），停止回 404 時再讀一次、是這一輪才指名停止；審批卡的 hook 理由、串流事件解析、被擋訊息的通知與理由；被擋下的這一輪從對話樹拿掉（其他分支保留）、照完成流程存回並換回標題、文字放回輸入框；插話被擋時不進佇列、顯示理由並放回文字；設定頁的草稿函式、資料層（revision、錯誤分類、存好後重新讀取失敗）、分頁（事件區塊、編輯後送出的設定、存檔被拒的原因、壞檔提醒）與執行紀錄清單（類型、結束碼、HTTP 狀態）。
