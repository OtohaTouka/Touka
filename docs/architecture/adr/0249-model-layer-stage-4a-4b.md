# ADR-0249：模型溝通層第 4a＋4b 段（插話）

- 狀態：✅ 已接受（維護者 2026-10-13 看過模型層總計畫的最後報告後核准，PR #79 已合併進 dev（`2e15da49`）；4a、4b 實作完成；R-4 對抗式複審與跨段最終審查通過附註；維護者 2026-10-12 核准總計畫；DB-20、DB-21、DB-22、DB-24 第 6、8 點；4a 計畫審查裁定 1～11、追加 6～8；4b 計畫審查裁定 1～8）
- 日期：2026-10-12（4a、4b 定稿 2026-10-13）
- 決策者：維護者
- 影響範圍：（4a）新 `services/interjection_queue.py`、`models/interjection.py`、`routers/ui_interjections.py`、`handlers/interjections.py`、`utils/interjection_notes.py`；`services/authorization_gateway.py`（`require_human_requester`）、`services/stream_buffer_service.py`（清空佇列）、`handlers/react_core.py`、`handlers/adapters/{base,claude_adapter,openai_adapter,gemini_adapter,responses_adapter}.py`、`services/dispatcher.py`、`routers/heartbeat.py`、`utils/async_db.py`、`services/dialogue_summarizer.py`、`main.py`、`config/errors.json.example`；ToukaChat（`lib/chat/interjections.ts`、`lib/chat/interjectionStore.ts`、`buildApiMessage`、`stream_tree`、`connections`、`StreamRegistryProvider`、`useInterjectionQueue`、`useInterjectionComposer`、`QueuedMessageList`、`InterjectionMarker`、`ChatArea`、`MessageItem`、兩語 locales）、openapi.json／api.ts、AGENTS.md（§3、禁令 65）；（4b）`services/interjection_queue.py`（`urgent_signal`、打斷事件）、`handlers/react_core.py`（`pump_send_round` 的 `urgent`、`RoundInterrupted`）、`handlers/interjections.py`、`handlers/adapters/*`（`interrupted=`、`INTERRUPT_TEMPLATE`）；ToukaChat（`interjections.ts`、`stream_tree`、`connections`、`InterjectionMarker`、`MessageItem`、兩語 locales）
- 修訂 [ADR-0156](0156-react-core-extraction.md) 的 Provider Protocol 與 `run_react_loop` 簽名：`ProviderAdapter` 多 `append_interjection`、`hold_tool_choice`；`run_react_loop` 多關鍵字參數 `interjections=`、`stream_state=`；4b 再加 `append_interjection(..., interrupted=)`、`pump_send_round(round_gen, urgent)` 與 `RoundInterrupted`
- 修訂 [ADR-0247](0247-model-layer-stage-3a.md) 剩餘風險的 Mythos Preview：中途 system 已核對，規則表改 False（M9-R1a）

## 背景

AI 跑多步工具時，使用者想補一句（改方向、補條件）只能等回合結束，或按停止重來。4a 讓使用者在生成中把補充排進佇列，AI 在下一個工具步驟之間收到；4b 讓 Ctrl+Enter 直接打斷正在接收的那一輪（依賴 3b 的上游串流）。插話把使用者文字送進模型的 context，是新的提示注入入口，所以授權與結構限制是本段重點。

## 決策

第 4a、4b 段分別見下面兩節。共通原則（禁令 65）：只有對話的擁有者或管理員能插話；插話不能核准任何動作、不能改模式；工具輸出等外部內容永遠不能走這條路。

## 第 4a 段：工具邊界插話

### 1. 佇列（U4a-1）

- `services/interjection_queue.py`：`typing.Protocol` `InterjectionQueue`＋記憶體實作，鍵是 (session_id, stream_id)，和 rhythm_service 的中斷旗標分開放（心跳會清同一個 session 的旗標）。
- 模組常數：每個串流 10 則（含已送達，收回的不算）、每則 4,000 字、TTL 600 秒（queued 過期改 undelivered、不再交給模型；閒置超過 TTL、而且沒有 pending 也沒有 delivered 的整串清掉；已送達的留到串流結束的 `clear()`，存檔讀得到，10 則上限也不會中途歸零；R-4 ①）。
- 狀態：queued → pending(step=N) → delivered；或 → undelivered、retracted。`drain` 原子地把 queued 改成 pending，同一則不會取出兩次；只有 queued 能收回。
- 清空：StreamBufferService 在串流結束（完成、錯誤、停止、刪除退休、逾時收割）時清該串流，同一對話開新串流時清整個 session。只放記憶體，重啟就清。

### 2. 端點（U4a-1）

- `POST`／`GET /v1/ui/sessions/{id}/interjections`、`DELETE /v1/ui/sessions/{id}/interjections/{iid}`，都帶 `stream_id`；登記在 CORE_ROUTERS。
- 認證 Depends 是授權閘道新函式 `require_human_requester`：人類或 admin 代打可以；非 admin 的 X-Agent-ID 代打、靈魂 token、DebugAgent 一律 403。接著沿用停止端點的 `_authorized_stream_state`（串流發起者、現在能存取這個對話、同一串流代）；加入只限進行中的串流。
- 串流的擁有者鍵是 (agent_id, is_human)，和停止端點同一套授權（追加 7）：人類 admin 不是發起者時 403；但 admin 用 X-Agent-ID 代打時身分是 (靈魂, 非人類)，和擁有者代打或靈魂 token 開的串流同鍵，可以插進同一靈魂別人開的串流。仍在禁令 65「擁有者或管理員」之內，前端也不送 X-Agent-ID。
- 請求只有 `text`、`urgent`，多送的欄位一律 422（核准、模式、工具欄位在結構上不存在）。錯誤碼 1216～1219：`INTERJECTION_QUEUE_FULL`、`INTERJECTION_TOO_LONG`、`INTERJECTION_NOT_FOUND`、`INTERJECTION_NOT_RETRACTABLE`。

### 3. 取出點與送達（U4a-2）

- 位置：`react_core` 的 `append_tool_results` 之後、進下一輪之前。不在迴圈頂端（honesty 與預算閘的 continue 也會回到頂端），也不在重試迴圈裡（重試會重複附加）。
- 只在 UI 對話：`ui_authorized`、有 session、不是子代理、串流進行中。ctx 沒有 stream_id，所以回合開始時從串流緩衝讀一次（`stream_state`，預設 `stream_buffer_service.get_state`），只有 active 才開佇列，讀不到就不取出（fail-closed）。新串流取代舊串流時，舊的 producer 會在下一個 await 被取消，讀不到新串流的佇列。心跳、子代理、`complete_once` 拿不到佇列。
- step：這次 run 的第 N 個工具步驟（每次 `append_tool_results` 加一）。
- 取出＝pending(step=N)。下一輪送出成功後才標 delivered，送 `interjection_delivered`（`interjection_id`、`step`、`urgent`、`text`），位置在那一輪送出之後、該輪文字之前；下一輪重試全部失敗就標 undelivered。
- 回合結束（純文字結束、預算閘、收尾輪、按停止、錯誤）時，還在 queued／pending 的一律 undelivered，送一次 `interjection_undelivered`（`interjection_ids`），位置在 [DONE] 或 `UiGenerationStopped` 之前，也就是串流緩衝清空佇列之前。
- 佇列是空的時候一個事件都不送；40 份 golden 零差異。
- 事件名稱與欄位是雙端常數：後端 `services/interjection_queue.py`（含產生事件的函式），前端 `lib/chat/interjections.ts`。

### 4. urgent（Ctrl+Enter，裁定 5）

取出的插話有 urgent 時，下一輪 `tool_choice=none`（`adapter.hold_tool_choice`），那一輪送出後精確還原（原本沒有就仍然沒有），讓模型先用文字回應。這一輪請求沒有工具時（含會被 KF-8c 拿掉的空 tools）`hold_tool_choice` 什麼都不改，不送只有 `tool_choice` 沒有 tools 的請求（M9-R1a）。一般 Enter 不限制工具。正在接收上游串流時的打斷見第 4b 段。

### 5. 四家格式（裁定 6）

- 範本句（`handlers/adapters/base.py` 模組常數 `INTERJECTION_TEMPLATE`）：「使用者在工具執行期間補充：{text}」，多則以空行分隔。
- Claude：規則表 `mid_conversation_system=True`（Opus 5、Opus／Sonnet／Haiku 5.5、Opus 4.8、Fable、Mythos 5／5.1）時，在工具結果之後放 system 訊息；最後一則已是 system（工具邊界插話之後下一輪又被打斷）就併進同一則，不產生連續兩則（M9-R1a；官方規則：中途 system 要接在 user 後面，且是最後一則或後接 assistant）。其他（Sonnet 5.0、4.7 以下、Mythos Preview、認不出的版本）在同一則 user 訊息、最後一個 tool_result 後面加 text block，不會出現連續兩則 user 訊息；不新增 `cache_control`。
- OpenAI 相容：tool 訊息之後一則 user 訊息。Gemini：functionResponse 同一個 content 加 text part。Responses：function_call_output 之後一個 `{"role":"user"}` input item。
- OpenAI 相容、Gemini、Responses 是否接受這些格式未實測，只有模擬契約測試；Claude 中途 system 訊息也待實測（live-check backlog L6、L7）。

### 6. 存檔（U4a-3，DB-21）

- `dispatcher._persist_ui_assistant_turn` 把這個串流 delivered 的插話存進助理節點 `interjections[]`，形狀同 API `Interjection`（id、text、urgent、status、step、created_at），和前端寫進節點的相同。讀佇列發生在 `finish` 清空之前，禁令 28／57 不動。
- 沒有已送達的插話時不加欄位（存檔逐位元不變）；停止、錯誤、無痕照舊不存。

### 7. 回合與重組（裁定 7）

- 插話存在那一則 AI 回覆裡，不算回合。組下一輪歷史時，帶 `interjections[]` 的助理節點在 content 前面加標註 `〔本回合中使用者補充（第 N 步）：…〕`（只取已送達、有文字的，照步數排、每則一行，與原文空一行），角色仍是 assistant。
- 前端 `buildApiHistory` 與伺服器三處（心跳脈衝訊息、家對話注入 `get_home_messages`、滑動視窗摘要的輸入）共用同一個範本與組法：後端 `utils/interjection_notes.py`、前端 `lib/chat/interjections.ts`，跨端測試以純文字比對。封存匯出不套，保留原文紀錄。
- 三處回合計數、滑動視窗剪裁、`_valid_ui_base` 都不變。

### 8. 前端（U4a-4，DB-20）

- 生成中輸入框可用：Enter 排隊、Ctrl／Cmd+Enter 優先排隊、Esc 停止（@ 選單開著時先關選單）；沒在生成時 Enter 照舊送出。
- `useInterjectionQueue` 經 `fetchTouka` 打端點、帶 stream_id；送不出去時文字留在前端。`QueuedMessageList`（灰色，可收回或修改）、`InterjectionMarker`（第 N 步送達）。
- 回合正常結束時，未送達的合成下一則自動送出，掛在剛答完的 assistant 節點下（`handleSend` 的 `parentIdOverride`；R-4 修正前會掛到送出前的舊節點而分岔）；按停止或出錯時放回輸入框（切到別的對話時停止旗標歸零）；串流剛結束時插話被拒（404）也放回輸入框；背景對話不自動送出。不做心跳插話與 /btw（DB-22）。

### 9. 已知限制

- 串流任務被取消時（停止端點取消 producer、串流被取代）送不出 `interjection_undelivered`；前端在回合結束時直接從本地佇列取剩下的項目（停止時放回輸入框），使用者的文字不會消失。
- 滑動視窗估 token 時不算插話文字。
- 停止或錯誤時伺服器不存（照舊）；已送達的插話只留在前端的樹裡，前端停止時存不存檔照現行行為。
- 佇列只在記憶體，伺服器重啟後未送達的項目消失；重整後重播靠 `interjection_delivered` 的 `text` 還原節點。
- ~~AGENTS.md §2 的授權閘道函式清單沒有列 `require_human_requester`~~：維護者 2026-10-13 同意後，已在 `0b9ace28` 補進 AGENTS.md 第 161 行。
- Claude `mid_conversation_system` 為真時，使用者原文放在 system 角色（已核准的設計），使用者文字因此有 system 權威，可能蓋過系統提示的規則；HITL、模式與工具權限由程式強制，不受影響。
- 同一步有多則插話、urgent 又不是最先加入時，伺服器存的順序（加入順序）和前端（取出順序）可能不同，只影響標註順序。
- 自動送出會帶走當時暫存的附件。
- 佇列裡只剩從沒被取出的插話時，它過期改成 undelivered 的那次清理會連整串一起刪掉，回合結束的 `interjection_undelivered` 不會列到它；前端回合結束時直接從本地佇列取剩下的項目，不靠這個事件。這時同一串流的 10 則上限也會歸零。
- 歷史標註〔…〕沒有跳脫，模型可以寫出長得一樣的文字；審查員只讀 user 訊息，不影響核准。

## 第 4b 段：生成中途插話

### 1. 語意（master R2，取代 DB-25）

Ctrl+Enter（urgent）時，如果正在接收上游串流：取消這一輪，半截的文字、思考、工具呼叫全部丟掉（沒有解析，畫面上本來就沒有）；先送排隊的訊息（urgent 優先，其餘照順序），下一輪 `tool_choice=none`，再繼續迴圈；時間軸標「這一輪被你打斷」。不是 urgent 的 Enter 照 4a 等下一個工具邊界。

### 2. 觸發（U4b-1）

- 佇列 Protocol 加 `urgent_signal(session_id, stream_id)`：每個串流一個 `asyncio.Event`，只在有 urgent 排隊時 set；取出、收回、過期、清空都會重設（拿著舊訊號的也看得到被清掉），不會漏到下一個串流。
- `pump_send_round(round_gen, urgent)` 在本輪結果到達前同時等下一個項目和訊號；訊號先到就交出 `RoundInterrupted`，走 3b 的收流路徑（取消這一輪、關上游、不回半輪、逾時區塊外不 yield）。結果到了之後不再打斷；set 之後又被收回的不算。
- react_core 在同一步取出（urgent 優先，中間沒有 await），之後才收回的回 409。訊號 set 卻取不到東西時，本回合不再理這個訊號（防空轉）。
- 一輪開始前 urgent 已在排隊：只在 UI 路徑，這一輪不送、先附加插話、不送打斷事件；附加只做一次，在重試迴圈外（重試送同一個 state）。

### 3. 迴圈不變式

- 不是錯誤也不是停止：不重試、不拋 `UiGenerationStopped`；被取消的那一輪不算一步、不推進預算。心跳、子代理、`complete_once` 拿不到訊號。
- 計費：串流那一輪，廠商回報的照記，沒有就用 3b 的 `partial_usage` 估；「估計」只在 `token_usage` 事件與日誌。非串流那一輪被打斷時不記也不估（見已知限制）。
- 下一輪送出成功才標 delivered（同 4a），同一則不會重複送達。`InterjectionRound` 的 pending 改成累加：工具邊界那一批若下一輪被打斷，之後仍會送達；只走 4a 的流程不變。
- `tool_choice` 已經壓著就不重壓：連續兩則 urgent 時，還原一次就回到原值。

### 4. 送達位置與範本句

- 打斷後 state 最後一則是使用者側的訊息（原本的提問或上一批工具結果）；`append_interjection(..., interrupted=True)` 接在它後面，四家形狀照 4a 裁定 6（Claude 併進同一則 user 訊息或依規則表放 system 訊息，不出現連續兩則 user；OpenAI 相容、Responses 另起一則 user；Gemini 加 text part）。
- 範本句分工（都是事實陳述，`handlers/adapters/base.py`）：打斷路徑用 `INTERRUPT_TEMPLATE`「使用者打斷了正在寫的回答並補充：{text}」；工具邊界照舊用 `INTERJECTION_TEMPLATE`，4a 的期望不翻轉。

### 5. 事件與 step

- `round_interrupted_by_user`（`step`）：被打斷那一輪收掉之後、下一輪的 `interjection_delivered` 之前送；開始前搶先時不送。後端常數與產生函式在 `services/interjection_queue.py`，前端是 `ROUND_INTERRUPTED_EVENT`，跨端測試比對兩邊的值。
- step＝已完成的工具步數；第一輪就被打斷是 0，送達事件與歷史標註會顯示「第 0 步」。

### 6. 前端（U4b-2）

Ctrl+Enter 的說明改成「立即中斷正在寫的回答」（兩語）。收到事件時，在同一個助理節點的 `interruptions[]` 加一筆 `{step}`（同一步被打斷兩次記兩筆），重播時重建；`InterjectionMarker` 和送達的補充照步數排，同一步先列打斷。

### 7. 已知限制

- 伺服器存檔不帶 `interruptions`，只靠前端 full 存檔。
- Claude SDK 在 message_stop 之後、連線關閉之前的極短空檔仍可能被打斷；這時整輪已經完成、廠商已收費，但 partial_usage 因進度已完成回 None，那一輪的用量不會記（相容廠商串流在 [DONE] 之後同理）。窗口極小，照記帳需要另外設計。
- 串流任務被取消時不送 undelivered（同 4a）。
- 非串流那一輪（自訂連線、不在串流清單的相容廠商）被打斷時，用量不記也不估；要估需要另外設計。
- 打斷後的訊息形狀只有模擬契約，列入 live-check backlog L8。

## 驗證

- 第 4a 段：每個單位紅測試先行，突變全紅（U4a-1 15 個、U4a-2 18 個、U4a-3 9 個、U4a-4 20 個），期望翻轉 0。段分支 4e7ec871 的最終審查：pytest 全量兩種順序 8,580 passed／17 skipped／6 failed（已知 compose 環境），smoke 24/24，guard_all 只有 Guard #20 的 knip 段（工作資料夾）紅；golden 40 份零差異；Vitest 258 檔、1,591 個測試。沒有打任何 API。
- 第 4b 段：紅測試先行，突變全紅（U4b-1 22 個、U4b-2 8 個、U4b-3 一致性測試 4 個），期望翻轉 0；golden 40 份零差異；U4b-1 工作資料夾 pytest 全量 8,605 passed／17 skipped／6 failed（已知 compose 環境）；Vitest 260 檔、1,600 個測試（U4b-2）。沒有打任何 API。

## 回復

- 第 4a 段：revert 4a 的產品與測試即可。已存的 `interjections[]` 是額外欄位（MessageNode `extra=allow`），舊程式會忽略，不需要遷移；錯誤碼 1216～1219 保留不重用。
- 第 4b 段：revert 4b 的產品與測試即可，4a 的工具邊界插話照常運作；前端存的 `interruptions[]` 是額外欄位，舊程式會忽略。
