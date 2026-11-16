# ADR-0318：停止、逾時、失敗講清楚（可視性第二批）

- 狀態：✅ 已實作（PR 1：子代理的結果，2026-11-16；PR 2：子代理跟著停止、等結果的憑證，2026-11-17；PR 3：上一輪的標記，2026-11-17；PR 4：核准的說法，2026-11-17）。一頁計畫由維護者核准（2026-11-16）。
- 日期：2026-11-16
- 決策者：維護者
- 相關：[ADR-0134](0134-in-process-subagent-dispatch.md)（in-process 子代理）、[ADR-0135](0135-structured-report-finalize-and-board-bound-stop.md)（回報塊）、[ADR-0316](0316-keep-harness-blocks-and-state-approval-mode.md)（第一批：派工情境、同步派工）、[ADR-0248](0248-model-layer-stage-3b.md)（主迴圈的失敗與重試）
- 修訂：[ADR-0051](0051-dispatch-callback-channel.md)（站內憑證逾時不作廢）、[ADR-0134](0134-in-process-subagent-dispatch.md)（決定 4 的子代理時限、後果邊界①②）、[ADR-0309](0309-subagent-project-and-read-only.md)（停止後子代理還會跑）、[ADR-0312](0312-local-helper-for-host-files.md)（綁在這一輪的工具）、[ADR-0316](0316-keep-harness-blocks-and-state-approval-mode.md)（決定 8 的同步派工與等候說法）、[ADR-0138](0138-parallel-fanout-and-resumable-delivery.md)（多個子代理的成功數）、[ADR-0292](0292-hooks.md)（SubagentStop 的 `last_assistant_message`；`generationFailed` 會送給模型）、[ADR-0231](0231-session-persistence-outcomes-and-revisions.md)（決定 6：空回覆加了記號不再合併）、[ADR-0267](0267-trusted-activity-timeline-and-continuity-v1.md)（心跳的 `crashed`、`heartbeat_skipped`）
- 影響範圍：PR 1：`ToukaProxy/src/tools/subagent_tools.py`、`handlers/react_core.py`、`handlers/adapters/retry.py`、`models/context.py`、`services/hooks/lifecycle.py`；PR 2：`services/subagent_registry.py`（新）、`tools/subagent_tools.py`、`handlers/react_core.py`、`services/dispatch_callback_store.py`、`routers/dispatch_callback.py`、`tools/mission_tools.py`、`services/continuity_service.py`；PR 3：`utils/interjection_notes.py`、`services/continuity_events.py`、`utils/continuity_events_db.py`、`services/continuity_service.py`、`routers/heartbeat.py`、`main.py`，前端 `ToukaChat/src/lib/chat/turnNotes.ts`（新）、`chat/buildApiMessage.ts`、`stream_tree.ts`、`tree_history.ts`、`providers/StreamRegistryProvider.tsx`；PR 4：`services/approval_flow.py`、`services/approval_service.py`、`services/tool_executor.py`

## 背景

可視性檢查（2026-11-15～16）的第二批：AI 不知道子代理到底有沒有做完、為什麼沒做完。

1. 子代理撞到模型錯誤時，網頁派的變成「UiGenerationStopped: UI generation did not complete」，心跳和 IDE 派的被當成「完成」，結果是廠商的錯誤原文。
2. 子代理跑到 10 分鐘被中止時，已經寫好的部分整段丟掉，只剩一行「TimeoutError: 」。時限也只算模型那段，SubagentStart hook 和組裝卡住不會被中止。
3. 結果只留前 4000 字，子代理的結論寫在最後，最容易被切掉；工具的日誌行（「執行完畢」之類）也串進子代理的文字。
4. 一次派多個子代理時，只要沒當掉就算「成功」，子代理自己回報失敗也一樣。

## 決定（PR 1）

### 1. 模型錯誤一律算失敗，只寫固定的種類

- 主迴圈失敗時（`react_core.fail_round`）除了 `model_failed`，另把種類記在 `ctx.model_error_type`（`retry.py` 的固定名稱，和 StopFailure hook 同一套）。
- 子代理的 ctx 從零開始：`ui_authorized`、`model_failed`、`model_error_type` 都重設（`_mark_subagent_run`）。子代理不是網頁對話，所以網頁、心跳、IDE 派的子代理走同一條失敗路徑，不再丟 `UiGenerationStopped`。
- 子代理那一輪失敗時，主迴圈不把廠商的錯誤訊息串進子代理的文字（`subagent_run` 時不輸出）；派工端讀到失敗記號就丟 `SubagentModelError`，結果是 FAILED，寫「模型錯誤（種類）：子代理沒有做完」。
- 種類只取 `retry.KNOWN_ERROR_TYPES` 裡的值，表外的一律寫 `unknown`：交給主代理的只有固定說法，不放廠商原文（可能帶金鑰片段）。

### 2. 逾時交回已經做到的部分

- 時限（`SUBAGENT_TIMEOUT_SECONDS`）包住整個子代理：SubagentStart hook、組裝、模型都算在內（修訂 ADR-0134 決定 4 的「子 loop 硬上限 `asyncio.wait_for`」）。
- 逾時時丟 `SubagentTimeoutError`（`TimeoutError` 的子類別），帶著已經收到的文字；結果是 FAILED，寫「逾時：超過約 N 分鐘沒做完，已中止」，接著「已做到的部分（沒做完，可能不完整）」。
- 只有時限本身到了才算子代理逾時；子代理裡別的 `TimeoutError` 照原樣往外，不說成逾時。
- 子代理的串流用 `contextlib.aclosing` 收掉。
- 外層取消（主代理這一輪被收掉、Touka 關機）照樣是取消，不算逾時；SubagentStop 拿到空字串。按停止走決定 5 的 `SubagentCancelledError`。
- 失敗的結果（逾時、模型錯誤）只有 `task_id`、`status`、`result_summary`：已做到的部分去掉回報塊後放進 summary，回報塊裡的 `claims`、`artifact_refs`、`report_status` 不帶（沒做完的自評不能當真）。
- SubagentStop hook 的 `last_assistant_message`：逾時或模型失敗（PR 2 起加上被停）時是已做到的部分，其他失敗仍是空字串（修訂 ADR-0292 決定 17 的「失敗時是空字串」）。

### 3. 保留結論

- 結果太長時保留結尾 4000 字，開頭標「…（前段過長已省略）」；以前是保留開頭。逾時、模型錯誤時交回的部分也一樣。
- 子代理的文字不再夾工具日誌行（`react_core` 在 `subagent_run` 時不輸出 log 事件）。子代理沒人看畫面，模型照樣收到工具結果。

### 4. 多個子代理的計數

- 每個結果分三種：完成（跑完且回報 done）、部分完成或未確認（回報 partial、或沒附回報塊）、失敗（沒跑完，或自己回報 failed）。
- `dispatch_subagent_group` 的回覆寫「N 完成／N 部分完成或未確認／N 失敗」，欄位 `completed`（只算完成）、`partial`、`failed`（修訂 ADR-0138 決定 1 的「沒崩潰就算成功」）。同步的 `dispatch_subagent` 回覆也用同樣的說法。

## 決定（PR 2）

### 5. 按停止，子代理也會停

- 每個子代理在自己的 task 裡跑，旁邊有停止監看（`_unless_parent_stops`）：主代理那段對話被按停止（`stopped_since_start`，看停止次數有沒有比派出時多）就只取消子代理的 task，丟一般例外 `SubagentCancelledError`。主代理的工具 task 不被取消，照常收到結構化的「已取消」（`status="CANCELLED"`，附已做到的部分），心跳主代理照常以「被中斷」收尾。
- 停止沒有事件可掛，只有累計次數，所以每 `STOP_CHECK_SECONDS`（0.5 秒）看一次；沒有對話的執行（IDE）不看。
- 停止以整段對話計：之後在同一段對話按停止，或心跳在家對話跑的時候按停止，也會停掉同一段對話裡較早派出、還在背景跑的子代理（修訂 ADR-0134 後果邊界②「背景子 loop 不隨父請求中斷」）。
- 子代理沒做完（被停、逾時、模型錯誤、主代理這一輪被收掉）時，在背景呼叫 `terminal.stop_agent_commands`（不卡事件迴圈）停掉它的背景與待審指令、收掉行程（SubagentStop hook 先發，不保證指令已經停完）；它的迴圈被收掉時，還在跑的工具全部一起取消（`subagent_run` 時每個工具都綁在這一輪）。
- `dispatch_subagent`、`dispatch_subagent_group` 加進綁在這一輪的工具（修訂 ADR-0312 的 `_TOOLS_TIED_TO_THE_TURN`）：主代理這一輪被收掉時，同步派的子代理跟著停。按停止時已派出的子代理不再多跑 10 分鐘（修訂 ADR-0309 的既有限制、ADR-0316 決定 8 的代價）。
- 背景派工的 task 本身被取消（例如 Touka 關機）時也交回「已取消：Touka 停止或重啟」，等的一方不空等；這時拿不到已做到的部分。
- 等 SubagentStart hook 時被停（`SubagentStoppedError`）也是「已取消」。停止監看出錯會留紀錄（`spawn_logged`）。
- 多個子代理的計數與同步派工的說法多一種「已取消」，回覆多 `cancelled` 欄位。被停不記成錯誤，只記一行。

### 6. 下一輪看得到還在跑的子代理

- `services/subagent_registry.py`：純資料登記表（標籤、派它的靈魂、對話、開始時間），子代理開始時登記、結束時移除；不放 task、不做取消，Touka 重啟就清空。
- `<continuity>`（請求尾端，不影響快取、不新增系統區塊）多一行：這個靈魂在這段對話派出、還在跑的子代理與已跑多久，最多列 5 個；沒有在跑的就不加這一行。標籤是模型給的，比照 ADR-0267 經 `safe_ref`；無痕、沒有對話（IDE）不列。

### 7. 等結果的憑證

- `dispatch_subagent` 的憑證登記成站內（`in_process`）：等候逾時不作廢，再等就接著等；拿到結果才移除；超過子代理時限加 30 分鐘寬限（`IN_PROCESS_TOKEN_LIFETIME_SECONDS`，用單調時鐘算，系統時間跳動不影響）才在下次登記或等候時清掉（修訂 ADR-0051 的「等完就移除」）。外部派工的憑證照舊。
- HTTP 回呼（`/v1/internal/dispatch/callback`）拒絕站內憑證，回應和沒登記的一樣：模型看得到這個憑證，不能拿來從外面送假結果。這是配合憑證活得更久的加固，不是修既有弱點（以前憑證只在等候期間有效，本機行程要先拿到它），所以不記進 `SECURITY.md`。
- `await_dispatch_result` 的說法照新行為改寫（修訂 ADR-0316 PR 2 補記的等候訊息）：逾時寫「之後再等同一個憑證會接著等」；憑證不明寫「拿過結果、太久沒來拿被清掉或 Touka 重啟過，不會再有結果」。

## 決定（PR 3）

### 8. 上一輪沒做完，下一輪看得到

- 網頁對話被使用者停止或出錯的那則 AI 回覆，送給模型時 content 最前面加一行固定記號：「〔本回合被使用者停止，沒有做完〕」或「〔本回合出錯，沒有完成〕」（兩個都有時寫停止）。記號在那則回覆裡、不另加訊息，角色仍是 assistant，回合數不變（禁令 27）。是給模型看的固定文字，不走 `t()`；畫面不變。
- 前端：後端說這一輪 `cancelled`，或自己中止（含別處停止的 `PROMPT_STOPPED`）時，`stream_tree.markGenerationStopped` 在那則回覆記 `generationStopped: true`；網路錯誤不標（不知道後端有沒有跑完）。後端回 `cancelled` 也包括同一段對話在別處開了新的一輪（舊串流被取代）或 Touka 關機，一樣標「被使用者停止」；記號說的是「沒做完」，這樣標可以接受。`buildApiHistory` 只對助理節點、只認 `true` 加記號，在插話標註之前。
- 後端：`interjection_notes.annotate_node` 讀同樣兩個欄位、加同樣的記號（心跳、家原文、摘要從存檔組歷史都經它）；`TURN_STOPPED_NOTE`、`TURN_FAILED_NOTE` 和前端 `turnNotes.ts` 同名常數以純文字比對測試鎖住。`generationFailed` 從此也送給模型（修訂 ADR-0292 的「只用來顯示、不送給模型」）。
- 停止或出錯、一個字都沒回的那一輪，以前被 `drop_blank_assistant_turns` 拿掉、前後兩則 user 合成一則；加了記號就不是空白，保留下來（修訂 ADR-0231 決定 6）。

### 9. 心跳當機、在入口被擋、模型錯誤的種類

- 當機：Proxy 啟動時掃到的 in-flight 標記，各記一筆 `heartbeat_finished`，結束原因 `crashed`、帶那次的開始時間（`started_ms`）。和正常結束用同一個鍵（`heartbeat_run_key`），已經記過結束的不會被改，重複掃描也不重複；那次開始得比這個靈魂的可信紀錄起點早（例如之後重置過）就不記（ADR-0287）。寫入在 `main.py` 用掃描回傳的清單做，`autonomy.py` 不碰事件。
- 入口被擋：新事件種類 `heartbeat_skipped`，原因只有固定列舉 `no_brain`（沒設大腦）、`owner_login_expired`（擁有者登入過期，常見的靜默停擺）；重置、清除或刪除中照 ADR-0287 不記（登入過期那條先看 `destructive_ops.generation`，沒設大腦那條在寫初始提示之後再看一次 `disturbed`；查身分出錯只記 log，回的錯誤不變）。同一原因連續被擋只記第一次（寫入時比對最新一筆心跳事件，單一 SQL 敘述）；中間跑過一次或換了原因就再記。這是「只在事情做成之後記」的例外：記的是「被擋」這個確定的事實（修訂 ADR-0267）。
- 模型錯誤：結束原因 `model_error` 另記 `error`，只用 `retry.KNOWN_ERROR_TYPES`，表外的記 `unknown`。脈衝共用 ctx，每個脈衝前把 `model_error_type` 和 `model_failed` 一起清掉。
- `<continuity>` 的自主執行那一行改看最新一筆（結束或被擋）：被擋寫「not started, refused since …; reason …」；當機寫開始與重啟時發現的時間；模型錯誤後面加種類。

## 決定（PR 4）

### 10. 結果寫明是誰核准的

- 經過核准才執行的工具，結果加一行固定說明：人在聊天的核准卡按的（`APPROVED_BY_PERSON_NOTE`），或 Auto 審查員放行、沒有人核准（`APPROVED_BY_REVIEWER_NOTE`）。結構化 JSON 結果改加 `approval` 鍵，和 hook 輸出同樣做法，不破壞 JSON；其他接在最後一行。工具跑了卻失敗或丟例外時也附（核准照樣發生過）；核准後在參數驗證（`validate_native_arguments`、`missing_required_error`）就被擋下的不附；核准後才被其他檢查擋下（MCP 參數超過上限、MCP 清單不允許、找不到工具）的照樣附，人確實核准過。上游結果已經有 `approval` 鍵時不覆蓋，改接在最後一行。
- 沒經過核准的不附：YOLO 或政策直接放行的不算核准路徑。解算的人是誰（`ApprovalResolution.resolver_id`）不寫進模型看到的文字。
- 小幫手 `local_files` 的 propose 成功時結果自己寫了誰核准（`propose_result_note`，ADR-0317），執行器不再附誰核准，人核准時只補審查員轉人的理由；小幫手離線、逾時或出錯時結果沒有這段，照一般附。
- 審查員把動作轉給人時，它的理由（模型寫的）經 `neutralize_hook_text`、單行、最多 300 字（`MAX_REVIEW_REASON_CHARS`）後也交給主代理：人核准時接在說明後面，被拒絕、取消或逾時時接在錯誤說明裡。以前理由只在卡片上。

### 11. 取消、拒絕、逾時分開說

- `approval_denied_error` 多「被取消」：卡片被關掉或這一輪被停止，沒有人決定（`reason=approval_cancelled`）；以前掉到「Approval was not granted.」。
- `wait_for_resolution` 找不到待審時回 `cancelled`，不再當成使用者拒絕；照樣不執行。
- 這一輪被停止時，還在等人的核准卡每 0.5 秒（`APPROVAL_STOP_CHECK_SECONDS`）看一次停止，被停就收成「已取消」（解算者記 `touka:turn-stopped`），解算事件照常送出、卡片收斂；以前卡片沒有結果，工具空等 180 秒逾時。畫面的核准卡顯示「已取消，沒有人決定」。按停止收掉的卡（看解算者是不是 `touka:turn-stopped`）不發 PermissionDenied hook（ADR-0292：使用者按停止不算被拒）；卡片被關（`/v1/tools/cancel`）照舊發 `approval_cancelled`。互審驗證指令（`CommandApprover`）不在這次範圍，照舊。

## 不做

- 真模型評估（`tests/tool_eval/live_episode.py` 也用 `parse_subagent_report`）不動，禁令 61。

## 後果

- 主代理看得出子代理是被限流、金鑰錯還是逾時，也拿得到逾時前做到的部分，可以決定重派、縮小範圍或改做別的。
- 自己回報失敗的子代理不再被算成功；`completed` 的意思從「沒崩潰」變成「真的做完」。
- 子代理的模型失敗不再串出廠商原文；網頁上的人類看得到的失敗說明不變（那是主代理那一輪的事）。

## 驗證

PR 4：`tests/unit/test_approval_wording.py`：人按的、審查員放行的各寫一行，沒經過核准的不寫，解算的人不寫出來；JSON 結果加 `approval` 鍵、文字結果接最後一行；審查員轉人的理由核准與拒絕都交給模型、單行、中和、有上限；取消、拒絕、逾時說法不同；找不到待審算取消；這一輪被停止時待審的卡收成已取消、工具不執行、不發 PermissionDenied，卡片被關照舊發；工具核准後丟例外照樣附；上游已有 `approval` 鍵不覆蓋；propose 成功只補審查員理由、失敗照一般附；YOLO 不附；工具呼叫被取消不留待審卡；`ToolApprovalCard.test.tsx`：卡片顯示已取消；`test_local_files_mode_approval.py`：propose 不重複寫誰核准；`test_hook_wiring_security.py` 跟著改成看 `approval` 鍵。

PR 3：`tests/unit/test_previous_turn_marks.py`：記號和前端常數相同、停止與出錯都加、停止優先、只認 `true`、在插話標註之前、空回覆加了記號不再合併、回合數不變、家原文帶記號；`test_continuity_event_hooks.py`：模型錯誤只記白名單裡的種類、脈衝之間清掉種類、當機記一次且不改已記的結束、可信紀錄起點以前的不記、入口被擋連續同一原因只記一次；`test_continuity_block.py`：接續行寫出當機、被擋、模型錯誤的種類，原因只認列舉；`test_brain_not_set_group_heartbeat.py`：沒設大腦、擁有者登入過期都在入口記一筆；前端 `buildApiMessage.test.ts`、`stream_tree.generationFailed.test.ts`、`StreamRegistryProvider.persistence.test.tsx`：組歷史加記號、後端說取消與自己中止都標停止、網路錯誤不標。

PR 2：`tests/unit/test_subagent_stop.py`：同步、背景、一次派多個的子代理都跟著停並交回「已取消」與已做到的部分；別段對話的停止、沒有對話的執行不受影響；較早派出的背景子代理也會被之後的停止停掉；主代理這一輪被收掉時取消照樣往外傳；這一輪被收掉時又按停止仍是取消；等 SubagentStart hook 時被停也是已取消；沒做完才收行程；子代理的迴圈收掉時工具全停；心跳主代理同步派工被停照常以「被中斷」收尾；還在跑的子代理列出與中和、沒有對話不列；站內憑證逾時不作廢、過期清掉、外部憑證照舊、HTTP 回呼拒絕站內憑證；等候說法。

PR 1：`tests/unit/test_subagent_outcomes.py`：三種主代理的模型錯誤都是 FAILED 加種類、不帶廠商原文；失敗前寫的文字交回；子代理不沿用父代理的旗標；逾時交回部分文字、時限包住組裝、別的逾時不算子代理逾時、外層取消不算逾時；逾時與模型失敗時 SubagentStop 拿到已做到的部分；長結果保留結尾；子代理的文字沒有工具日誌行；自評失敗不算成功；同步派工的說法。
