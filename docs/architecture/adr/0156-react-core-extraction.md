# ADR-0156: ReAct 核心抽取——單一迴圈實作＋Provider Protocol（絞殺者 E0–E4）

## 狀態

✅ 已接受（提案 [react-core-extraction](../proposals/react-core-extraction.md) owner 核准；拍板 K1=keepalive 維持 claude-only、K2=Honesty Guard 維持 gemini-only、K3=Gemini reasoning 另案、K4=新 `adapters/` 套件、K5=假時鐘＋固定 request_id；人類逐階段 commit）

- 2026-10-12：`extract_usage` 對原生 Anthropic 出口回空用量（不記帳）的凍結舊制，已由 [ADR-0244](0244-model-layer-stage-1b.md) 修訂為照常記帳、只是不送 Touka 的 `token_usage` 事件（原生出口 wire 不變）。
- 2026-10-13：`send_round` 不再 yield「gemini 503 重試的 `touka_status`」——重試與重試狀態文字改由 react_core 產生，adapter 每次只送一次（由 [ADR-0248](0248-model-layer-stage-3b.md) 修正；Claude keepalive 早已由 ADR-0157 K1 移進核心）。
- 2026-10-13：`ProviderAdapter` 多了 `append_interjection`、`hold_tool_choice`，`run_react_loop` 多了關鍵字參數 `interjections=`、`stream_state=`；4b 再加 `append_interjection(..., interrupted=)`、`pump_send_round(round_gen, urgent)` 與 `RoundInterrupted`（插話，由 [ADR-0249](0249-model-layer-stage-4a-4b.md) 修訂）。

## 日期

2026-07-14（E0/E1=07-13、E2/E3/E4=07-14）

## 背景

三 handler（openai 410/claude 507/gemini 498＝1,415 行）約 60% 為同一 ReAct 狀態機的三份拷貝——[ADR-0155](0155-handler-drift-fix-batch.md) 修掉的六個漂移 bug 即此結構的病理切片（每個修復都只落在其中一份）。本案在漂移修復後的乾淨基線上做**行為保持**的結構抽取：迴圈邏輯單一實作（修一次＝三家生效）、新增供應商＝寫一個 adapter 而非複製第四個 handler。外部統一庫（LiteLLM/aisuite/any-llm/Pydantic AI）評估後全數不引入（病＝拷貝、藥＝內部抽取；`send_round`/`extract_usage` 即未來接外部庫的天然接縫）。

## 決策與實作

### 終態架構

- **`src/handlers/react_core.py`（單一實作＝SSOT）**：身分回填／`allow_react`／ADR-0146 工具輪保險絲全鏈／雙中斷閘（**刻意不對稱**：呼叫前中斷不發遙測、回合後中斷發遙測）／`react_slots` 進度／asyncio.Queue 工具 fan-out／guard 呼叫點（ADR-0155 後的 guard_service SSOT）／C4 計時／遙測收尾／**唯一一份 SSE 方言**（`yield_delta`/`yield_reasoning_delta`；ADR-0081 重播＋Guard #38 封套＋ADR-0152 reasoning 頻道）。
- **`src/handlers/adapters/{base,openai_adapter,gemini_adapter,claude_adapter}.py`**：`ProviderAdapter`＝`typing.Protocol`（DIP 鐵則）；核心唯一消費本合約、adapter 之間互不相識。設計不變式：`Turn.raw_assistant` 對核心不透明（歷史寫回一律 provider 原形透傳）；`send_round` 失敗回 `RoundError(user_message)` 不拋例外。
- **三 handler 收薄殼（計畫偏差，刻意）**：提案原訂 E4 整檔刪除；實作改為保留 6 行薄殼（`handle_*_request → run_react_loop(XxxAdapter(), …)`）——入口名凍結＝factory 與所有呼叫端**零改動**，絞殺者共存期的分流點原地退役。守衛錨（`tests/unit/test_react_core_ssot.py`）擔保薄殼永不長回邏輯。

### Protocol 實戰進化（E2/E3 壓力測試所得）

- `send_round` → **async generator**：先 yield SSE 事件字串（gemini 503 重試的 `touka_status`、claude 5s keepalive 心跳＝K1）、終端 yield 非 str 項（raw 或 RoundError）——上游等待期間的即時性不因抽取而犧牲。
- `append_tool_results` → **async**：gemini 迴圈中 re-cache（await cache service，含 ADR-0155 C3 保存值）＋條件式 tool_config AUTO 重置。
- `mid_loop_hook`：純文字輪收尾前的方言介入點（gemini Honesty Guard 專用＝K2）。
- `Turn.text_parts`/`Turn.reasoning_parts`：逐段 yield＝保真 gemini 多 text part 與 claude 多 thinking block 的 chunk 邊界。
- `extract_usage(state, raw)`：claude native Anthropic egress 依 state 回空 usage＝跳過整段 usage 遙測（凍結舊制：native 提前 return、不進 accumulate/audit/token_usage）。
- `telemetry_model(state, request)`：**保真發現**——`convert_openai_to_anthropic` 會改寫模型名（對映表＋去 `-thinking`），舊 claude 遙測/計費用改寫後的 `kwargs["model"]`；核心統一讀 `request.model` 對 claude 不保真 → 掛點化（claude 回對映實名、openai/gemini 回 `request.model` 凍結）。
- `emits_context_breakdown`：token_usage 事件僅 claude 附完整 context 分解（ADR-0092 Phase B；golden 檔證實形狀差異，餘家事件凍結不變）。

### TDD 兩層測試網

- **特徵化 golden-SSE（E0 錄製、全程凍結）**：`tests/proxy/characterization/`——共通 8 情境×3 家＋方言專屬（gemini：503 retry/迴圈中 re-cache/honesty 矯正輪；claude：thinking_split/native egress）＝**30 golden 逐位元**。正規化＝假時鐘（上游 +7s/工具 +5s）＋固定 request_id（K5）；golden 生命週期＝缺檔錄製+skip、刻意變化＝刪檔重錄進 review。
- **Protocol 契約套件**：`tests/unit/test_adapter_contract.py`（21 條）＝Protocol 語意的 SSOT（denied turn 形狀/raw 透傳/usage 正規化/keepalive/雙 egress/斷點輪替/對映實名）。
- **守衛錨**：`tests/unit/test_react_core_ssot.py`——迴圈指紋（`while True`/`wrapping_up`/中斷字串/`consume_round`/`react_slots_event`/`ToolExecutor`/`collect_known_tool_names`）**唯一存在於 react_core**，薄殼與 adapter 皆不得含。

## 後果

- **行為**：30 golden 零位元組變動＝下游 SSE 契約（ADR-0081 重播/Guard #38/ThinkingFold）零影響；openapi/api.ts 零變動。
- **文件化的刻意微差**（各 adapter docstring 存檔）：①gemini 舊碼以「收到 log 事件」置 `has_tool_call` 才寫回工具輪歷史——生產 ToolExecutor 每工具必發 log＝該邊生產不可達，核心改「執行即寫回」；②claude 舊碼把 thinking 包 `<thinking>` 累積進 write-only 變數（無讀取點）＝不再複製；③claude 舊碼按 content block 順序交錯發射 thinking/text，核心改 reasoning 全先於 text——Anthropic 現行 wire 形狀 thinking 恆在前＝生產順序不變。
- **測試面**：patch 靶點隨 adapter 邊界遷移（httpx/cache/asyncio 代理→adapter 模組、時鐘/ToolExecutor→react_core，計 17 檔）；三個靜態矩陣（hallucination/memory_signal/drift_c5/code_review）收斂為 react_core 單條目。
- **已知遺留（皆 owner 另案，非本案範圍）**：Gemini reasoning thought-parts（K3；新架構下＝改一個 `parse_round`）；keepalive/Honesty Guard 推廣三家（K1/K2 後續小案）；gemini 誠實矯正輪注入上游的文案仍教 `query_memory`/`access_personal_drive` 舊名（模型面 prompt 文字，行為凍結原樣移植；ADR-0155 D3 修的是下游警告文案）。
- **驗收終態**：pytest 全套 **3521 passed**／goldens **30/30 零 diff**／guard_all **41/41**／lint_ssot clean／ruff 基線持平（改動檔零新增）／NOQA 淨增 2（E0 golden 錄製/讀取站，registry 199 已入帳）。

## 相關

- 提案：[react-core-extraction](../proposals/react-core-extraction.md)（K1–K5 拍板、階段規劃、風險清單）
- [ADR-0155](0155-handler-drift-fix-batch.md)（前置步驟 0＝漂移修復批；本案的乾淨基線）
- [ADR-0146](0146-tool-round-budget.md)／[ADR-0152](0152-reasoning-channel-and-fold.md)（收進核心的保險絲與 reasoning 分頻語意）
- [ADR-0127](0127-prompt-cache-prefix-optimization.md)～[ADR-0130](0130-stable-first-system-reorder.md)（claude 四斷點快取＝ClaudeAdapter wire 職責）
- [ADR-0081](0081-resumable-stream-cursor-buffer.md)（下游 SSE 逐 chunk 重播＝特徵化逐位元凍結的理由）
