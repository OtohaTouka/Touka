# ADR-0316：系統說明不被靈魂設定蓋掉、每輪說明模式與核准

- 狀態：✅ 已實作（PR 1：第 1～5 點；PR 2：第 6～9 點，2026-11-16 補記）。一頁計畫由維護者核准（2026-11-16）。
- 補註（2026-11-17）：決定 8 的同步派工按停止會跟著停；`await_dispatch_result` 逾時後再等會接著等（[ADR-0318](0318-stop-timeout-failure-made-clear.md) 決定 5、7）。
- 補註（2026-11-19）：決定 7 子代理不再帶主代理的看板（`mission_awareness`）（[ADR-0323](0323-group-heartbeat-home-tool-results.md) PR 3）。
- 日期：2026-11-16
- 決策者：維護者
- 相關：[ADR-0092](0092-context-window-token-sliding-window.md)（區塊標籤）、[ADR-0139](0139-harness-context-block.md)（`<harness_context>` 與 Guard #40）、[ADR-0246](0246-model-layer-stage-2b-2c.md)（三種模式、在場判斷）、[ADR-0272](0272-iso-c2-agent-command-isolation-and-executor.md)（決定 4：YOLO 沒人時的危險 MCP 工具）、[ADR-0133](0133-context-snapshot-real-assembly-rewrite.md)（快照與 Guard #39）
- 影響範圍：`ToukaProxy/src/services/client_system.py`（新）、`services/soul_service.py`、`middleware/pipeline.py`、`middleware/layers/soul.py`、`middleware/layers/context.py`、`middleware/layers/tool.py`、`services/dispatchers/group.py`、`routers/heartbeat.py`、`services/prompt_blocks.py`、`services/context_snapshot_service.py`、`utils/presence.py`、`services/approval_flow.py`、`services/tool_executor.py`、`tools/terminal.py`、`scripts/guard_harness_context.py`；PR 2：`services/mission_status.py`（新）、`tools/mission_tools.py`、`tools/subagent_tools.py`、`tools/contracts.py`、`tools/builtin/schemas.py`

## 背景

可視性檢查（2026-11-16）找到幾個「AI 看不到對的現況」的問題：

1. 靈魂對話綁專案時，專案說明被靈魂設定整段蓋掉。`apply_soul_to_request` 把整段 system 換成 `<persona>`，只有內容剛好含 api、tool、path 這類字母才整段保留。群聊走同一段，而且群聊在套用靈魂前已經跑完工具層，工具清單說明、看板等也一起被蓋掉。system 是 list 形狀時還會出錯。
2. 模式只在列出改檔工具時由 `<validation_protocol>` 說。沒人在場時說明仍寫「會等人核准」；被擋後又叫它「請使用者切到 Auto 或 YOLO」或「打開 ToukaUI 核准」，子代理和心跳都做不到。
3. 系統提示說 `send_message` 能聯絡其他靈魂，實際上它是發 Discord／Telegram 通知。

## 決定

### 1. 系統自己加的說明一律保留

- 流程入口（`ProxyPipeline.execute`）每次都記下用戶端原本送來的每一則 system（`client_system.capture`，存成字串 tuple；子代理的 ctx 從父 ctx 複製來，所以每次都覆寫）。
- 套用靈魂時（`apply_soul_to_request(..., client_system=...)`）逐則比對：現在的內容以原內容開頭，原內容部分算用戶端的、多出來的算系統加的；超過原則數的整則算系統加的；對不上（已不是以原內容開頭）就整則算用戶端內容並記 log，往安全的方向錯。系統加的部分原樣接在 `<persona>` 後面。
- 用戶端原本的內容照舊依字母判斷（ToukaIDE 會自己送 system），所以外面送來的偽造系統標籤仍算用戶端內容。不靠解析標籤區分，以後新加的系統說明也自動保留，不用登記。
- 三個呼叫點：靈魂層、群聊（取流程 ctx 裡記下的原 system）、心跳（伺服器自己組請求，傳空 tuple）。沒傳（`None`）時照舊整段依字母判斷。
- 專案名稱與路徑來自使用者設定，包進 `<project_context>` 前先跳脫系統標籤字元（`neutralize_tags`）。

### 2. 每一輪說清楚模式、有沒有人在場、需要核准的會怎樣

- 新區塊 `<approval_context>`（`build_approval_context`），工具層在這一輪有列工具時注入：靈魂、子代理、心跳、IDE、自訂模型、群聊都會帶到。內容是模式、這一輪有沒有人在場（`is_human_present`）、需要核准的動作會怎樣，說法對齊實際判斷：
  - Manual：有人＝跳核准卡；沒人＝改動類一律被拒，只有唯讀工具會跑。
  - Auto：低風險直接跑；有人時其他交給審查員或核准卡；沒人時不讀設定檔，用保守說法「可能由審查員判斷，也可能需要人核准而被拒」（實際上沒人時高風險工具直接轉人、指令除非開了 `headless_terminal` 也轉人，轉人就是被拒）。
  - YOLO：除了只有人能核准的都直接跑；沒人時危險 MCP 工具與沒有隔離時的部分動作也只有人能核准，所以被拒。串接或嵌入其他指令的 shell 指令（`;`、`|`、`&&`、`||`、反引號、`$(...)`、換行）不會自動跑：執行器判 ALLOW、不跳核准卡，進終端後 `classify_command` 升成 CONFIRM、走舊的 ToukaUI 通道，網頁聊天沒有連這個通道，所以被擋。說明照實寫「不會自動跑，能拆就拆成單一指令」（拆開後每一段照樣經過黑名單）。執行器和終端判斷不一致是原本就有的落差，記在 proxy 交班檔。
  - 模式照 `normalize_auth_mode` 正規化（看不懂的值寫成 Manual，和工具層同一個來源）。
  - 在場的說法寫「有沒有人能回答核准」，不寫「有沒有人在」：SNS 有人在聊，但沒有核准通道。
- 內容在同一段對話裡不變（不放時間、輪數），歸「不變」組（`STABLE_SYSTEM_TAGS`），由工具層接在不變說明的最後。一頁計畫原本寫歸「會變」組；成果審查發現這樣走 Claude 時，只要有列工具 system 就一定切成兩個快取斷點，原生 `/v1/messages` 第一輪不清用戶端自己放在訊息上的斷點，用戶端帶兩個時總數到 5 個、Anthropic 回 400，所以改歸「不變」組。模式或「有沒有人能核准」改變時，走 Claude 的整段 system 重寫一次快取（system 只有尾端一個斷點），tools 的快取照樣命中；模式很少切換，心跳送的內容本來就和網頁聊天不同，同一段對話的命中率不受影響。
- 模式說法只在這裡：`<validation_protocol>` 拿掉模式句，只留自我驗證的規矩（各模式都強制）。`<harness_context>` 的 "Tools and approval" 改成指向新區塊；Guard #40 釘住的「pause for human approval」跟著搬到 `build_approval_context`（守衛改成可指定承諾句在哪個產生函式，檢查不放寬）。

### 3. 被擋後的建議依情境寫

- `utils/presence.py` 的 `no_human_followup(ctx)` 是唯一來源：子代理＝「寫進回報，交給派你的主代理」；其他沒人在場（心跳、派工）＝「寫進回覆或看板，等人回來處理」。`<approval_context>` 與被擋建議（`blocked_call_suggestion`）共用。
- 套到四處：執行器的「需要核准但沒有核准通道」、只有人能核准、Auto 轉人沒人，以及 `terminal.py` 危險指令沒有 ToukaUI（心跳 Manual、YOLO 串接指令）。只換 `suggestion`，拒絕本身、結構與 `reason` 不變；`approval_flow` 的兩個函式由執行器把建議傳進去，保持純函式。
- 執行器的三處只在沒人在場時走得到；終端那處在 YOLO 串接指令時有人在場也會走到（一頁計畫的附註寫只在沒人時走到，成果審查更正），所以 `blocked_call_suggestion` 有人在場時改說中性的「告訴使用者這會做什麼，讓他決定或自己執行」。
- 心跳跑危險指令時若有舊版 ToukaUI 連著，仍會改成等那邊核准；ToukaChat 沒有連這個通道，一般安裝照「Manual 沒人＝被拒」。

### 4. 區塊分類

`ALL_BLOCK_TAGS` 每個標籤都要剛好歸進三組之一：不變（新的 `STABLE_SYSTEM_TAGS`）、會變（`VOLATILE_SYSTEM_TAGS`）、接在訊息尾端（`REQUEST_TAIL_TAGS`）。測試把關，新加的說明沒歸類就變紅。

### 5. `send_message` 的說法

`<harness_context>` 改成「send_message 發通知到 Discord 或 Telegram 頻道，送不到其他代理人」。

## PR 2 補記（2026-11-16）

### 6. 看板寫出卡住、失敗與原因

`services/mission_status.py` 是狀態圖示與狀態字的唯一來源（⬜ TODO、⏳ DOING、✅ DONE、⛔ BLOCKED、❌ FAILED；看不懂的值寫 `?`，不原樣寫進 system）。`<mission_awareness>` 每格寫成「圖示 編號 [狀態]: 說明」，卡住或失敗的附原因（先看 `blocker_detail`，沒有再看 `test_log`，壓成一行、取前 120 字，照舊跳脫區塊標籤）。`get_board` 本來就逐項印完整的 Blocker 與 Test Log，只換圖示與狀態字，不重複印原因。

### 7. 子代理知道派工情境

子代理的任務前多一段【派工情境】（在 user 訊息裡，不進 system）：誰派的；主代理是在和人對話、心跳叫醒的，還是這一輪沒有人；子代理這裡沒有人能回答或核准，需要核准的寫進回報；時限（由 `SUBAGENT_TIMEOUT_SECONDS` 算）；工具輪（心跳派生的有上限，沒另外設定時 `HEADLESS_FALLBACK_ROUNDS` 輪；互動派生照系統設定）；只回文字就算做完。模式由 `<approval_context>` 說，這裡不重複；不讀設定檔。

### 8. 沒列任務看板工具時，派子代理等做完才回

這一輪列出的工具沒有 `mission_tools` 時，`dispatch_subagent` 等子代理做完，直接把結果放進工具回覆（和 `dispatch_subagent_group` 同一條路），不產生憑證、不寫耐久結果；沒有工具清單（除錯執行器）時照舊回憑證。不多給工具：配上 `mission_tools` 會多一條跑驗證指令、跨身分的路。`<harness_context>` 那句「跨重啟仍等得到憑證」只在同時列出 `mission_tools` 時出現（Guard #40 釘的字樣不變）。判斷讀這一輪的工具清單：ADR-0269 的結構測試 C3 原本只准 `subagent_tools` 寫這個欄位，這次另准一處 `.get` 讀取（只做這個判斷，錯了也是往同步的安全方向）；寫入的規則不變。

代價：等候期間最長 10 分鐘、不送保活。網頁走緩衝串流不會斷，但同一輪連續同步派多次可能逼近緩衝串流 3600 秒上限；IDE 與 OpenAI 相容 API 這類沒經過緩衝的用戶端，這段期間收不到任何資料，可能被中間的程式斷線。

### 9. 外部派工標明沒接通

`notify_on_complete`、`target_agent_id`（DispatchAgent）的說明與 initialize 的回覆寫明外部 AI 派工沒有接通、要委派用 `dispatch_subagent`；`callback_token` 說明改成「dispatch_subagent 或 initialize 回傳」。等候逾時與憑證不明的訊息不再叫它去看板找外部 AI，改成照實說子代理可能還在跑，並寫明停止條件：不要連續重複等，派出超過約 10 分鐘仍是憑證不明就表示結果不會來（例如 Touka 重啟過）。逾時常數搬到 `tools/contracts.py`，`mission_tools` 不匯入 `subagent_tools`（會在啟動時循環匯入）。

## 不做

- 外面送進來的 system 怎麼處理不變（照舊依字母判斷）；IDE 檔名的跳脫不在這次範圍。
- 不改任何拒絕或放行，不多給工具。
- 同步派工時按停止：子代理照樣跑完（最長 10 分鐘、照樣花額度），結果收不回；`dispatch_subagent_group` 現在也一樣，留給第二批（停止與逾時）兩個一起改。
- 外部 AI 派工（DispatchAgent）本身不修不刪，只把說明寫成「沒有接通」。

## 影響

- 綁專案的靈魂對話每一輪多一段專案說明（本來就該有），群聊也帶得到工具清單說明與看板；token 比現在多一點。
- 有列工具的每一輪多一段兩三行的 `<approval_context>`。
- 被擋後模型拿到的建議做得到：子代理交回主代理，其他寫進回覆或看板，有人在場時請使用者決定。
- 專案說明一律保留之後，聊天請求帶的 `project_id` 有沒有擁有權檢查變得更要緊：審查時發現原本沒檢查，維護者決定先修再合這個變更，已由 ADR-0304 的 2026-11-16 補記處理（router 核對請求的人看得到才標 `project_authorized`，ContextLayer 只認這個標記；佔位編號 `default` 一律當沒有專案）。
- 未驗證：ToukaIDE 沒有找到核准卡的程式；IDE 走聊天端點時算有人在場，Manual 會說「跳核准卡」，實際可能等到逾時（`is_human_present` 原本的語意）。

## 驗證

- `tests/unit/test_soul_keeps_harness_blocks.py`：專案說明在 OpenAI、Anthropic 兩種形狀都留下；list 形狀不出錯；偽造標籤仍算用戶端內容；兩則用戶端 system 各自判斷；對不上時整則算用戶端並記 log；入口每次覆寫；專案名稱裡的標籤被跳脫。
- `tests/unit/test_approval_context.py`：三種模式 × 有沒有人與看不懂的值，說法和同一組條件跑出的實際判斷一致（YOLO 串接指令實際跑一次終端被擋）；只有唯讀工具也有區塊、沒列工具就沒有；區塊分類全覆蓋；只有不變說明時 system 只佔一個快取斷點；四處被擋建議依子代理與心跳各自的說法，不再出現「Auto or YOLO」「open ToukaUI」。
- 故意改壞（保留邏輯、各模式說法、注入條件、四處建議、分類）都有對應測試變紅；Guard #40 在核准句被拿掉時 FAIL。
- PR 2：`tests/unit/test_mission_board_status.py`（五種狀態、原因的來源與截斷、跳脫、get_board）、`tests/unit/test_subagent_dispatch_visibility.py`（派工情境三種說法與時限、同步分支與失敗、照舊回憑證的兩種情況、外部派工說明與兩段等候訊息）；`test_harness_context.py` 那條拆成有沒有 `mission_tools` 兩條。
