# ADR-0318：停止、逾時、失敗講清楚（可視性第二批）

- 狀態：🚧 實作中（PR 1：子代理的結果，2026-11-16；PR 2：子代理跟著停止、等結果的憑證，2026-11-17）。一頁計畫由維護者核准（2026-11-16）。PR 2～4（子代理跟著停止與等結果的憑證、上一輪的標記、核准的說法）合併時補記在本檔。
- 日期：2026-11-16
- 決策者：維護者
- 相關：[ADR-0134](0134-in-process-subagent-dispatch.md)（in-process 子代理）、[ADR-0135](0135-structured-report-finalize-and-board-bound-stop.md)（回報塊）、[ADR-0316](0316-keep-harness-blocks-and-state-approval-mode.md)（第一批：派工情境、同步派工）、[ADR-0248](0248-model-layer-stage-3b.md)（主迴圈的失敗與重試）
- 修訂：[ADR-0051](0051-dispatch-callback-channel.md)（站內憑證逾時不作廢）、[ADR-0134](0134-in-process-subagent-dispatch.md)（決定 4 的子代理時限、後果邊界②）、[ADR-0309](0309-subagent-project-and-read-only.md)（停止後子代理還會跑）、[ADR-0312](0312-local-helper-for-host-files.md)（綁在這一輪的工具）、[ADR-0316](0316-keep-harness-blocks-and-state-approval-mode.md)（決定 8 的同步派工與等候說法）、[ADR-0138](0138-parallel-fanout-and-resumable-delivery.md)（多個子代理的成功數）、[ADR-0292](0292-hooks.md)（SubagentStop 的 `last_assistant_message`）
- 影響範圍：PR 1：`ToukaProxy/src/tools/subagent_tools.py`、`handlers/react_core.py`、`handlers/adapters/retry.py`、`models/context.py`、`services/hooks/lifecycle.py`；PR 2：`services/subagent_registry.py`（新）、`tools/subagent_tools.py`、`handlers/react_core.py`、`services/dispatch_callback_store.py`、`routers/dispatch_callback.py`、`tools/mission_tools.py`、`services/continuity_service.py`

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
- 外層取消照樣是取消，不算逾時；SubagentStop 拿到空字串。目前按停止還碰不到子代理（同步派工只收 `local_files`、背景派工照 ADR-0134 不跟著父請求取消），PR 2 的停止會走這條。
- 失敗的結果（逾時、模型錯誤）只有 `task_id`、`status`、`result_summary`：已做到的部分去掉回報塊後放進 summary，回報塊裡的 `claims`、`artifact_refs`、`report_status` 不帶（沒做完的自評不能當真）。
- SubagentStop hook 的 `last_assistant_message`：逾時或模型失敗時是已做到的部分，其他失敗仍是空字串（修訂 ADR-0292 決定 17 的「失敗時是空字串」）。

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
- 子代理沒做完（被停、逾時、模型錯誤、主代理這一輪被收掉）時，`process_registry.terminate_agent` 收掉它開的指令行程；它的迴圈被收掉時，還在跑的工具全部一起取消（`subagent_run` 時每個工具都綁在這一輪）。
- `dispatch_subagent`、`dispatch_subagent_group` 加進綁在這一輪的工具（修訂 ADR-0312 的 `_TOOLS_TIED_TO_THE_TURN`）：主代理這一輪被收掉時，同步派的子代理跟著停。按停止時已派出的子代理不再多跑 10 分鐘（修訂 ADR-0309 的既有限制、ADR-0316 決定 8 的代價）。
- 背景派工的 task 本身被取消（例如 Touka 關機）時也交回「已取消」，等的一方不空等。
- 多個子代理的計數與同步派工的說法多一種「已取消」，回覆多 `cancelled` 欄位。被停不記成錯誤，只記一行。

### 6. 下一輪看得到還在跑的子代理

- `services/subagent_registry.py`：純資料登記表（標籤、派它的靈魂、對話、開始時間），子代理開始時登記、結束時移除；不放 task、不做取消，Touka 重啟就清空。
- `<continuity>`（請求尾端，不影響快取、不新增系統區塊）多一行：這個靈魂在這段對話派出、還在跑的子代理與已跑多久，最多列 5 個。標籤是模型給的，比照 ADR-0267 經 `safe_ref`；無痕不列。

### 7. 等結果的憑證

- `dispatch_subagent` 的憑證登記成站內（`in_process`）：等候逾時不作廢，再等就接著等；拿到結果才移除；超過子代理時限加 30 分鐘寬限（`IN_PROCESS_TOKEN_LIFETIME_SECONDS`）才在下次登記或等候時清掉（修訂 ADR-0051 的「等完就移除」）。外部派工的憑證照舊。
- HTTP 回呼（`/v1/internal/dispatch/callback`）拒絕站內憑證，回應和沒登記的一樣：模型看得到這個憑證，不能拿來從外面送假結果。
- `await_dispatch_result` 的說法照新行為改寫（修訂 ADR-0316 PR 2 補記的等候訊息）：逾時寫「之後再等同一個憑證會接著等」；憑證不明寫「拿過結果、太久沒來拿被清掉或 Touka 重啟過，不會再有結果」。

## 不做

- 上一輪的標記、核准的說法：PR 3、PR 4。
- 真模型評估（`tests/tool_eval/live_episode.py` 也用 `parse_subagent_report`）不動，禁令 61。

## 後果

- 主代理看得出子代理是被限流、金鑰錯還是逾時，也拿得到逾時前做到的部分，可以決定重派、縮小範圍或改做別的。
- 自己回報失敗的子代理不再被算成功；`completed` 的意思從「沒崩潰」變成「真的做完」。
- 子代理的模型失敗不再串出廠商原文；網頁上的人類看得到的失敗說明不變（那是主代理那一輪的事）。

## 驗證

PR 2：`tests/unit/test_subagent_stop.py`：同步、背景、一次派多個的子代理都跟著停並交回「已取消」與已做到的部分；別段對話的停止、沒有對話的執行不受影響；較早派出的背景子代理也會被之後的停止停掉；主代理這一輪被收掉時取消照樣往外傳；沒做完才收行程；子代理的迴圈收掉時工具全停；心跳主代理同步派工被停照常以「被中斷」收尾；還在跑的子代理列出與中和；站內憑證逾時不作廢、過期清掉、外部憑證照舊、HTTP 回呼拒絕站內憑證；等候說法。

PR 1：`tests/unit/test_subagent_outcomes.py`：三種主代理的模型錯誤都是 FAILED 加種類、不帶廠商原文；失敗前寫的文字交回；子代理不沿用父代理的旗標；逾時交回部分文字、時限包住組裝、別的逾時不算子代理逾時、外層取消不算逾時；逾時與模型失敗時 SubagentStop 拿到已做到的部分；長結果保留結尾；子代理的文字沒有工具日誌行；自評失敗不算成功；同步派工的說法。
