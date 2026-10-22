# ADR-0244：模型溝通層第 1b 段（修計費、complete_once）

- 狀態：✅ 已接受（維護者 2026-10-12 核准模型層總計畫）
- 日期：2026-10-12
- 決策者：維護者
- 影響範圍：三個 provider adapter 與 `handlers/adapters/usage.py`（新）、`react_core`、`telemetry`、`providers/pricing.py`、`services/completion.py`（新）、`dispatchers/route_family.py`／`factory.py`、`dialogue_summarizer`／`sliding_window_service`、`review/review_loop`／`tools/mission_tools`、`creation`（`model_judge`／`evaluation`／`engine`／`api`／`routers/creation.py`）、`main.py`、14 份 characterization golden、對應測試與文件
- 修訂 [ADR-0156](0156-react-core-extraction.md)（原生 `/v1/messages` 出口不記帳的凍結舊制，見決策 2）
- 2026-10-16 由 [ADR-0271](0271-claude-5-5-thinking-by-purpose.md) 修訂（r55）：`_shape_claude` 把 schema 併進 adapter 已放的 `output_config`（不覆寫 effort）；`complete_once` 在送出前多一道最壞花費估算（`cost_guard`＋`would_exceed`，超過回 `over_budget`、不送不記帳）；畫面 `token_usage` 與稽核 CSV 的整次 run 金額改為每輪分檔相加。「契約測試原封不動」是 3a／3b 的要求：`test_complete_once_contract.py` 等三個檔與 `test_side_caller_billing.py` 的假預算物件只補 `would_exceed`（回 False），斷言不改
- 剩餘風險中的 Gemini 思考 token 未計入、DeepSeek 快取命中未讀，已由 [ADR-0250](0250-touka-leftovers.md) A6、A7 修正（2026-10-14：Gemini 輸出＝`candidatesTokenCount`＋`thoughtsTokenCount`；快取讀先取 `prompt_tokens_details.cached_tokens`，沒有才用頂層 `prompt_cache_hit_tokens`）

## 背景

ReAct 迴圈只記最後一次回覆的用量，工具輪全部漏記，成本上限（`autonomy.is_over_budget`）看不到同一次 run 正在花的錢；畫面事件與稽核成本不含快取；OpenAI 與 Gemini 的輸入 token 本來就含快取，直接相加會重算或漏算。摘要、互審 critic、創建評分三個旁路呼叫直接用 Anthropic SDK，不記帳、也不受成本上限約束。之後的審查員需要一個共用、可注入假實作的「問模型一次」入口。

## 決策

1. **用量正規化**：三家 `extract_usage` 一律經 `handlers/adapters/usage.py` 回同一組四欄＋本輪總輸入。
   - 四欄：`prompt_tokens`（未快取輸入）、`cache_read_input_tokens`、`cache_creation_input_tokens`、`completion_tokens`；本輪總輸入＝三種輸入相加（context 佔用）。
   - Anthropic 的 `input_tokens` 本來不含快取；OpenAI 相容扣掉 `prompt_tokens_details.cached_tokens`；Gemini 扣掉 `cachedContentTokenCount`。鍵名就是 `token_usage.json` 的既有鍵，檔案格式不變（P20）。
   - 價格表沒填快取費率（OpenAI 相容、Gemini）時，快取 token 照 input 價計：快取已從 prompt 拆出，照 0 計會讓快取變免費、成本上限少算。Anthropic 用填好的快取費率。
2. **每輪記帳**：
   - `react_core` 每輪解析後就把該輪用量寫進 `token_usage.json`，同一次 run 中途的成本上限就看得到。
   - E-Index 歷史每次 run 只記一筆（`accumulate_token_usage` 的 `e_index` 改選填，run 結束時以空用量帶一次）。
   - 稽核 CSV 仍是每次 run 一列、欄位不變；token 與 `cost_usd` 改為整次 run 加總，含快取讀寫（決定 A5）。
   - 畫面 `token_usage` 事件位置與欄位不變：`total`＝整次 run 的四欄加總，`cost`＝整次 run 含快取；`usage_pct`（Claude 另附的 `context_breakdown`）仍用最後一輪的總輸入。
   - 原生 `/v1/messages` 出口：ADR-0156 凍結的「回空用量＝不記帳」改為照常記帳並寫 CSV，但不送 Touka 的 `token_usage` 事件（新 `ProviderAdapter.emits_usage_event`），原生出口的 wire 與 golden 不變。
   - golden 刻意重錄 14 份（三家的單一／多工具輪、預算用完收尾、中途按停，以及 Gemini 的誠實矯正輪與長迴圈）：只有 `token_usage` 那一格的 total／cost 變；只呼叫一次模型的情境與原生出口不變。
3. **`complete_once`**（`services/completion.py`，介面本段凍結）：
   - `complete_once(identity, purpose, model, system, user, max_tokens, timeout_s, response_schema=None) -> CompletionResult(text, usage, cost_usd, stop_reason, error)`；`.ok`＝`error is None`。`CompletionError(error_class, reason, message)`，`error_class` 為禁令 59 六類之一。另有 `Completer` Protocol，呼叫端依賴它、測試注入假實作。
   - 順序：先過成本上限 → 路由 → 單次送出 → 廠商回了用量就記帳（provider 標籤＝purpose，不記 E-Index）→ 讀停止原因。拒答、截斷已計費，照記。
   - 路由：factory 的「模型名→哪一家＋連線資訊」抽成純函式 `route_family.resolve_model_route`，factory 與 `complete_once` 共用；factory 六個入口行為不變。
   - R3：只記用量、不記提示或回覆內容（無痕對話因此也不留內容）；不跨廠商；不重試：Claude client `max_retries=0`，Gemini 每次只送一次（`max_attempts=1`，主迴圈預設仍 3 次），adapter 送出重試通知就中止；`timeout_s` 是整次呼叫的總時間。拒答、截斷、安全過濾、未知停止原因、逾時、超預算、找不到路由或金鑰、廠商錯誤，一律回結構化非成功：`text` 為空，`stop_reason` 不會被改成 `end`。主迴圈的退避重試（3b）不得放進這一層。
   - `ProviderAdapter` 新成員 `stop_reason`：正規化成 `end`／`max_tokens`／`refusal`／`safety`／`error`，表外值、缺欄一律 `error`；三家實作，1a 錄製台以委派自動涵蓋。
   - R4：`response_schema` 只對 Claude 外送（`output_config.format`）；其他家族不外送、原文照回，由呼叫端嚴格解析，解析失敗就不放行。
4. **三個旁路呼叫改走 `complete_once`**：都會記帳到對應身分，超過成本上限時照各自原本的失敗語意處理。
   - 摘要（`summarizer`）：記到滑動視窗手上的身分；任何非成功回 None，降級為純無損剪裁。禁令 28／57 的觸發點與非阻塞性不變。
   - 互審 critic（`critic`，1024 tokens、逾時 120 秒）：`build_critic_completer(identity, model)` 換掉 `build_anthropic_completer`，mission_tools 傳入已驗證的身分。非成功一律拋 `CriticCallError`，GroundedReviewer 照原本「傳輸失敗＝REJECT」處理，結果帶文字也不採用。行為改變：critic 依 critic 模型路由，非 Claude 模型現在真的會被審、也可能 APPROVE（以前一律送 Anthropic SDK，必定失敗成 REJECT）；fail-closed 改在 `complete_once` 這層由測試證明。
   - 創建評分（`judge`，16 tokens、逾時 30 秒）：`ModelJudge(completer, model)`，main 注入 `complete_once`；建立者身分從 `/v1/creation/*` 一路傳到 `Judge.score`，拿不到時記到沒有角色的 `System` 身分；非成功回 0.0。

## 代價與剩餘風險

- 中途結束的 run（被按停或例外）各輪已記進 `token_usage.json`，但沒有稽核 CSV 列，也沒有 E-Index。
- 單次呼叫的輸出上限與 schema 由 `completion._SHAPERS` 寫進 adapter 的整備結果，3a 規則表上線後收回 adapter。
- 廠商沒回 `finish_reason`（或回表外值）一律算 `error`，`complete_once` 會把這種回應當失敗（已計費）。
- OpenAI 推理模型的 `max_completion_tokens` 留給 3b；目前 `complete_once` 對這類模型送 `max_tokens`，可能被拒。
- Gemini 的 `thoughtsTokenCount` 未計入用量，思考 token 的費用會少算。（2026-10-14 已由 ADR-0250 A6 修正）
- DeepSeek 的 `prompt_cache_hit_tokens` 未讀，快取命中照 input 價計（偏高；2026-10-14 已由 ADR-0250 A7 修正）；OpenAI 相容與 Gemini 的快取同樣照 input 價，估計偏保守。
- 前端「本回合用量」改為整次加總，多輪對話的數字會比以前大，不是變貴。
- 新記帳與旁路呼叫只用 mock 驗證，廠商實際回應留給下一段實測（禁令 61）。

## 驗證

- 用量正規化：`test_usage_normalization.py` 15 個、`test_pricing.py` 只換新字面值；11 個突變都紅。
- 每輪記帳：`test_per_round_accounting.py` 6 個、`test_adapter_contract.py` 改為原生出口記帳但不送事件；14 個突變都紅。重錄的 14 份 golden 由獨立審查代理逐份手算核對，其餘 26 份不該變的理由成立。
- `complete_once`：契約測試 `test_complete_once_contract.py` 35 個（3a 換 SDK、3b 換傳輸時必須原封不動通過）、`upstream/test_adapter_stop_reason.py` 36 個、`test_model_route_resolution.py` 15 個、`test_gemini_send_attempts.py` 2 個；36 個突變都紅。
- 旁路呼叫：`test_side_caller_requests.py` 改為 `complete_once` 層的整組字面值比對，`test_side_caller_billing.py` 證明三者記帳與超預算行為，critic 10 種非成功都 REJECT；16 個突變都紅。
- 全量 pytest 與守衛結果見 HANDOVER。本段沒有打任何 API，也沒有啟動 Touka。

## 回復

不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。依相依倒序逐單位 revert：旁路呼叫（1b-U4）可單獨撤回；撤 `complete_once`、`stop_reason` 與路由純函式（1b-U3）前先撤 1b-U4；撤每輪記帳（1b-U2）要連同 14 份 golden 與 ADR-0156 的反向註記一起撤；用量正規化與價格表（1b-U1）最後撤。本 ADR 改「⛔ 已撤回」（或註明撤回哪一項），CHANGELOG 加一列。`token_usage.json` 已按新語意累積的數字不會回溯。
