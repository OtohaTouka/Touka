# ADR-0157: ReAct 核心抽取後續三項——keepalive 升核心（K1）／誠實守衛推廣三家（K2）／Gemini thought 分頻（K3）

## 狀態

✅ 已接受（[ADR-0156](0156-react-core-extraction.md) 收官時列為另案的三項遺留，維護者於 2026-07-14 核准全修；**本批＝刻意行為變化**，非行為凍結重構）

## 日期

2026-07-14

## 背景

ADR-0156 以行為保持為鐵則收官，三項「新行為」依決策 K1/K2/K3 刻意排除。抽取後的架構讓三項的成本大降：K1＝核心一個 wrapper（而非三份拷貝）、K2＝兩個 adapter 各一個 `mid_loop_hook`（機制核心早已接好）、K3＝改一個 `parse_round`。另含第四小項：誠實矯正輪注入模型的上游文案仍教已退役工具名（`query_memory`/`access_personal_drive`）＝ADR-0155 D3（下游警告文案）漏網的最後一站。

## 決策與實作

- **K1：keepalive 升核心單一實作**——`react_core.pump_send_round`（模組常數 `_KEEPALIVE_S=5.0`）包住每輪 `send_round`：每 5s 無新項即 yield SSE heartbeat comment，其餘項目原樣透傳（`asyncio.wait` 輪詢＋消費端提前收流時取消孤兒任務）。claude adapter 內建版（wait_for+shield）**拆除**——推廣三家時不複製三份，正是本抽取案的海拔原則；openai 長 POST 與 gemini 503 退避睡眠自此同享心跳。
- **K2：Honesty Guard 推廣三家**——openai/claude 補 `mid_loop_hook`（機制＝ADR-0156 既有 Protocol 掛點，核心零改動）：偵測沿用 `guard_service.check_fact_without_tools`；矯正輪＝assistant 原形＋矯正 user 訊息寫回（raw 透傳不變式）＋各方言強制查證——openai `tool_choice="required"`（OpenAI Chat 標準值，Grok/DeepSeek 皆支援）、claude `{"type": "any"}`（**thinking 啟用時不強制**＝Anthropic 規格 extended thinking 僅容 auto/none，強制即 400，僅靠提示引導）、gemini `ANY`（原制）。**推廣設計新增兩道保險（三家一致）**：①一次性閂鎖（`honesty_corrected`，每 run 至多矯正一次——弱強制方言下模型若持續瞎掰，無閂鎖＝矯正迴圈燒到額度耗盡）；②強制解除（`honesty_forced`，工具輪寫回後把 tool_choice 復位 auto——只解除自己設的、不碰呼叫端原生值；不解除＝之後每輪被迫調工具）。gemini 原生 `tool_config` 條件式 AUTO 重置語意不變。
- **K3：Gemini thought parts 分頻**——`parse_round` 把 `thought: true` 的思考摘要 part 分流至 reasoning 頻道（ADR-0152 同款 `delta.reasoning_content`，前端 ThinkingFold 直接生效）：不進正文 chunk、不進 guard 累積文字（`cumulative_text`）、**不回送歷史**（`raw_assistant` 過濾；Google 標記 `thought` 為 output-only，對齊 openai reasoning pop 語意）。修掉的潛在漏出：舊碼把 thought part 當一般文字＝思考直印正文（gemini thinking 系模型一旦回傳思考即觸發）。`thoughtSignature` 保存（函式呼叫流的加密簽章回送）不在本批範圍——現行 payload 從未處理、另案。
- **矯正文案 SSOT**——`guard_service.get_honesty_correction_prompt()`（模型面英文矯正提示的唯一家，與下游 `get_honesty_warning_message` 分工）；文案改教實存工具（`memory_tools(action="query")`＋`read`/`grep`），三 adapter 一律引用。防幻覺文案鐵則測試（test_warning_messages_reference_only_real_tools）擴充納管本文案。

## 後果（誠實行為變化）

- **全供應商**：上游久候 >5s 開始出現 SSE 心跳 comment（原僅 claude）；ReAct run 中「宣稱過去記憶/檔案內容但零查證」開始被注入矯正輪＋強制查證（原僅 gemini）。
- **Gemini**：思考摘要不再漏進正文（前端改摺疊顯示）、不再回灌歷史（省 token）；誠實矯正提示不再教已退役工具名。
- **每 run 誠實矯正至多一次**（gemini 舊制理論上可重複觸發——生產因 ANY 強制罕見；閂鎖為推廣後的防禦性統一）。
- **特徵化 golden**：既有 30 檔**零位元組變動**（K1 心跳在即時 mock 下不觸發；K2 觸發情境僅 honesty_warning、其 SSE 面不變——變的是 payload 內矯正文字；K3 為新增行為）＋新錄 `gemini__thought_parts_split`（新行為對新碼錄製）＝31 檔。
- **驗收終態**：pytest 全套 **3530 passed**／goldens **31/31**（舊 30 零 diff）／guard_all **41/41**／lint_ssot clean／ruff 基線持平（新增碼零檢出）。

## 相關

- [ADR-0156](0156-react-core-extraction.md)（抽取本體；本批＝其「已知遺留」三項的收尾）
- [ADR-0155](0155-handler-drift-fix-batch.md)（D3 下游警告文案修復；本批補上游矯正提示＝最後一個舊工具名站點）
- [ADR-0152](0152-reasoning-channel-and-fold.md)（reasoning 分頻 wire 慣例＝K3 的頻道）
- [ADR-0146](0146-tool-round-budget.md)（工具輪保險絲＝矯正迴圈的最終後盾；閂鎖讓它不被誠實守衛白白燒掉）

> **補記（2026-10-11，[ADR-0241](0241-model-layer-stage-0.md)）**：K2 的 Claude 強制查證改由 `claude_model_rules` 判定——5.x 以後或開思考時不強制，只靠矯正提示。（2026-10-12 起改讀每模型規則表 `services/models/model_rules.py`，見 [ADR-0247](0247-model-layer-stage-3a.md)）
