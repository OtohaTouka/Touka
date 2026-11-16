# ADR-0316：系統說明不被靈魂設定蓋掉、每輪說明模式與核准

- 狀態：🚧 部分實作（PR 1：第 1～3、5 點；看板、子代理、外部派工的說明在 PR 2 補進本 ADR）。一頁計畫由維護者核准（2026-11-16）。
- 日期：2026-11-16
- 決策者：維護者
- 相關：[ADR-0092](0092-context-window-token-sliding-window.md)（區塊標籤）、[ADR-0139](0139-harness-context-block.md)（`<harness_context>` 與 Guard #40）、[ADR-0246](0246-model-layer-stage-2b-2c.md)（三種模式、在場判斷）、[ADR-0272](0272-iso-c2-agent-command-isolation-and-executor.md)（決定 4：YOLO 沒人時的危險 MCP 工具）、[ADR-0133](0133-context-snapshot-real-assembly-rewrite.md)（快照與 Guard #39）
- 影響範圍：`ToukaProxy/src/services/client_system.py`（新）、`services/soul_service.py`、`middleware/pipeline.py`、`middleware/layers/soul.py`、`middleware/layers/context.py`、`middleware/layers/tool.py`、`services/dispatchers/group.py`、`routers/heartbeat.py`、`services/prompt_blocks.py`、`services/context_snapshot_service.py`、`utils/presence.py`、`services/approval_flow.py`、`services/tool_executor.py`、`tools/terminal.py`、`scripts/guard_harness_context.py`

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
  - YOLO：除了只有人能核准的都直接跑；沒人時危險 MCP 工具與沒有隔離時的部分動作也只有人能核准，所以被拒。
  - 看不懂的模式值照 Manual 寫。
- 內容在同一段對話裡不變（不放時間、輪數），登記成「會變的區塊」（`VOLATILE_SYSTEM_TAGS`）：模式與在場可能隔輪改變，走 Claude 時排在不變的說明後面，不影響前面的快取。
- 模式說法只在這裡：`<validation_protocol>` 拿掉模式句，只留自我驗證的規矩（各模式都強制）。`<harness_context>` 的 "Tools and approval" 改成指向新區塊；Guard #40 釘住的「pause for human approval」跟著搬到 `build_approval_context`（守衛改成可指定承諾句在哪個產生函式，檢查不放寬）。

### 3. 被擋後的建議依情境寫

- `utils/presence.py` 的 `no_human_followup(ctx)` 是唯一來源：子代理＝「寫進回報，交給派你的主代理」；其他沒人在場（心跳、派工）＝「寫進回覆或看板，等人回來處理」。`<approval_context>` 與被擋建議（`blocked_call_suggestion`）共用。
- 套到四處：執行器的「需要核准但沒有核准通道」、只有人能核准、Auto 轉人沒人，以及 `terminal.py` 心跳 Manual 跑危險指令沒有 ToukaUI。只換 `suggestion`，拒絕本身、結構與 `reason` 不變；`approval_flow` 的兩個函式由執行器把建議傳進去，保持純函式。這四處只在沒人在場時走得到。
- 心跳跑危險指令時若有舊版 ToukaUI 連著，仍會改成等那邊核准；ToukaChat 沒有連這個通道，一般安裝照「Manual 沒人＝被拒」。

### 4. 區塊分類

`ALL_BLOCK_TAGS` 每個標籤都要剛好歸進三組之一：不變（新的 `STABLE_SYSTEM_TAGS`）、會變（`VOLATILE_SYSTEM_TAGS`）、接在訊息尾端（`REQUEST_TAIL_TAGS`）。測試把關，新加的說明沒歸類就變紅。

### 5. `send_message` 的說法

`<harness_context>` 改成「send_message 發通知到 Discord 或 Telegram 頻道，送不到其他代理人」。

## 不做

- 外面送進來的 system 怎麼處理不變（照舊依字母判斷）；IDE 檔名的跳脫不在這次範圍。
- 不改任何拒絕或放行，不多給工具。
- 看板狀態、子代理情境、只勾派子代理時的同步派工、外部派工凍結的說明：PR 2。

## 影響

- 綁專案的靈魂對話每一輪多一段專案說明（本來就該有），群聊也帶得到工具清單說明與看板；token 比現在多一點。
- 有列工具的每一輪多一段兩三行的 `<approval_context>`。
- 被擋後模型拿到的建議做得到：子代理交回主代理，其他寫進回覆或看板。

## 驗證

- `tests/unit/test_soul_keeps_harness_blocks.py`：專案說明在 OpenAI、Anthropic 兩種形狀都留下；list 形狀不出錯；偽造標籤仍算用戶端內容；兩則用戶端 system 各自判斷；對不上時整則算用戶端並記 log；入口每次覆寫；專案名稱裡的標籤被跳脫。
- `tests/unit/test_approval_context.py`：三種模式 × 有沒有人與看不懂的值，說法和同一組條件跑出的實際判斷一致；只有唯讀工具也有區塊；區塊分類全覆蓋；四處被擋建議依子代理與心跳各自的說法，不再出現「Auto or YOLO」「open ToukaUI」。
- 故意改壞（保留邏輯、各模式說法、注入條件、四處建議、分類）都有對應測試變紅；Guard #40 在核准句被拿掉時 FAIL。
