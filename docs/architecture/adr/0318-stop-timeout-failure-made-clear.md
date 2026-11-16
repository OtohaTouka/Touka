# ADR-0318：停止、逾時、失敗講清楚（可視性第二批）

- 狀態：🚧 實作中（PR 1：子代理的結果，2026-11-16）。一頁計畫由維護者核准（2026-11-16）。PR 2～4（子代理跟著停止與等結果的憑證、上一輪的標記、核准的說法）合併時補記在本檔。
- 日期：2026-11-16
- 決策者：維護者
- 相關：[ADR-0134](0134-in-process-subagent-dispatch.md)（in-process 子代理）、[ADR-0135](0135-structured-report-finalize-and-board-bound-stop.md)（回報塊）、[ADR-0316](0316-keep-harness-blocks-and-state-approval-mode.md)（第一批：派工情境、同步派工）、[ADR-0248](0248-model-layer-stage-3b.md)（主迴圈的失敗與重試）
- 修訂：[ADR-0138](0138-parallel-fanout-and-resumable-delivery.md)（多個子代理的成功數）、[ADR-0292](0292-hooks.md)（SubagentStop 的 `last_assistant_message`）
- 影響範圍：PR 1：`ToukaProxy/src/tools/subagent_tools.py`、`handlers/react_core.py`、`handlers/adapters/retry.py`、`models/context.py`、`services/hooks/lifecycle.py`

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

- 時限（`SUBAGENT_TIMEOUT_SECONDS`）包住整個子代理：SubagentStart hook、組裝、模型都算在內。
- 逾時時丟 `SubagentTimeoutError`（`TimeoutError` 的子類別），帶著已經收到的文字；結果是 FAILED，寫「逾時：超過約 N 分鐘沒做完，已中止」，接著「已做到的部分（沒做完，可能不完整）」。
- 只有時限本身到了才算子代理逾時；子代理裡別的 `TimeoutError` 照原樣往外，不說成逾時。
- 子代理的串流用 `contextlib.aclosing` 收掉。
- SubagentStop hook 的 `last_assistant_message`：逾時或模型失敗時是已做到的部分，其他失敗仍是空字串（修訂 ADR-0292 決定 17 的「失敗時是空字串」）。

### 3. 保留結論

- 結果太長時保留結尾 4000 字，開頭標「…（前段過長已省略）」；以前是保留開頭。逾時、模型錯誤時交回的部分也一樣。
- 子代理的文字不再夾工具日誌行（`react_core` 在 `subagent_run` 時不輸出 log 事件）。子代理沒人看畫面，模型照樣收到工具結果。

### 4. 多個子代理的計數

- 每個結果分三種：完成（跑完且回報 done）、部分完成或未確認（回報 partial、或沒附回報塊）、失敗（沒跑完，或自己回報 failed）。
- `dispatch_subagent_group` 的回覆寫「N 完成／N 部分完成或未確認／N 失敗」，欄位 `completed`（只算完成）、`partial`、`failed`（修訂 ADR-0138 決定 1 的「沒崩潰就算成功」）。同步的 `dispatch_subagent` 回覆也用同樣的說法。

## 不做

- 停止、等結果的憑證、上一輪的標記、核准的說法：PR 2～4。
- 真模型評估（`tests/tool_eval/live_episode.py` 也用 `parse_subagent_report`）不動，禁令 61。

## 後果

- 主代理看得出子代理是被限流、金鑰錯還是逾時，也拿得到逾時前做到的部分，可以決定重派、縮小範圍或改做別的。
- 自己回報失敗的子代理不再被算成功；`completed` 的意思從「沒崩潰」變成「真的做完」。
- 子代理的模型失敗不再串出廠商原文；網頁上的人類看得到的失敗說明不變（那是主代理那一輪的事）。

## 驗證

`tests/unit/test_subagent_outcomes.py`：三種主代理的模型錯誤都是 FAILED 加種類、不帶廠商原文；失敗前寫的文字交回；子代理不沿用父代理的旗標；逾時交回部分文字、時限包住組裝、別的逾時不算子代理逾時；長結果保留結尾；子代理的文字沒有工具日誌行；自評失敗不算成功；同步派工的說法。
