# 提案：工具可觀測性與可靠度（Tool Observability & Reliability）

> 狀態：**✅ 收官（2026-07-05）——P0→P4 全數落地（擁有者尚未 commit 最後一批 ADR-0130）。** 三目標（量測／模型面／人面）可交付部分全數收口、皆以 P1 統一封套（[ADR-0122](../adr/0122-tool-event-telemetry-envelope.md)）為骨幹；P4 快取前綴**四層斷點疊加**（工具＋system＋穩定前綴細分＋順序重排）。工具系列 ADR **0119~0130（12 份）**。**剩餘僅兩項選配另案**（逐腦 emphasis 調校＝需 LIVE eval、探索性；OTel exporter＝外送遙測、與本地優先/隱私有張力），**擁有者明示啟動前不做**。本提案是「原生工具介面重設計」（`native-tool-interface-redesign.md` / ADR-0119、0120、0121）的**續章**：介面（用什麼工具、參數形狀、雙軌）已收斂，本提案提升「調用的**可觀測性與可靠度**」。
> 約束：**由人類 commit**；只用 **DebugAgent** 身分驗證、不動其他 agent 資料；**嚴禁自動修改 `data/`**；每步過 ruff＋lint_ssot＋guard_all(38)＋pytest＋vitest。
> 依據：**Claude Code 與 Codex CLI 的 system prompt 研究**（2026-07-04，含出處與官方/洩漏/推測分級）＋**公開評測標準盤點**（BFCL v3/v4、τ-bench pass^k、MCP-Bench 家族、OpenTelemetry GenAI semantic conventions）。

---

## 1. 北極星

與工具重構同一個：**提高模型調用工具的正確與成功率、降低失敗與錯誤。** 介面層（用什麼工具、參數形狀）已由 ADR-0119~0121 處理；本提案處理**「調用的前中後——模型與人類各自看得見什麼，以及怎麼量測有沒有變好」**。

---

## 2. 三目標（重整後）

- **目標 1 — 模型面全生命週期可見（before / during / after）**：
  - before：明確告知如何調用（schema＋何時用/不用＋必填）。
  - **during（含長指令監控——本輪自「during 對模型」討論併入）**：長時執行的指令/測試，模型能定期回看進度、抓到無窮迴圈或中途錯誤、必要時終止。
  - after：知道成功/失敗、**能分類原因**、能自我修正、能回報給人類。
- **目標 2 — 人類面可視**：模型調用、執行中、結果，人類都掌握；可摺疊/展開、有格式與顏色區分、不塞滿畫面。
- **目標 3 — 量測**：有沒有公開標準能確認模型/harness 的工具調用表現、且能用在 Touka。

---

## 3. Touka 現況盤點（**非綠地**——多數骨架已在）

| 目標 | 已有 | 缺口 |
|---|---|---|
| 1 before | Tier 2 schema 含「何時用/不用」＋必填提示；ToolLayer 注入 `<tool_availability>`（Tier 1） | 一致性可再收 |
| 1 during（長指令） | `execute_command` 背景任務（ADR-0120）；`_execute_core` 逐行串流；`process_registry`＋`terminate_process_tree`；`get_command_result(wait_seconds≤60)` | 背景模式只給審批用；running 狀態**不回傳部分輸出**給模型；無模型面 `stop_command`；30s 硬逾時使長指令根本跑不完 |
| 1 after | `tool_errors`（`error_class`＋`suggestion`，結構化、**領先 Claude Code 自由文字**） | 「成功但語意錯」無驗證慣例；模型「回報人類」的中文投影未成規範 |
| 2 人類面 | ToolStepTracker、bash `<details>` 摺疊、SSE 事件、記憶/檔案徽章（HAX G11）、`text-status-*` token | **只有 bash 摺疊**；跨工具結果卡片格式/顏色不統一 |
| 3 量測 | `tests/tool_eval/`（select-only＋多回合＋pass 率/幻覺/選錯/政策拒絕/turns_to_success） | 無 pass^k 可靠度、無「該不該叫工具」relevance 案例、未量工具 schema token 成本 |

---

## 4. 兩個誠實邊界（前輪評估已確立，寫死避免範圍蔓延）

1. **「正確/錯誤」不是遙測能給。** 工具層知道「有沒有執行成功」；一個**成功卻寫錯檔**的調用，工具層無從得知「錯」。「正確」要靠**驗證步驟**（跑測試/重讀/檢查不變式）或人類判斷或 eval 期望答案。→ 學 Codex 的「Validating your work」/Claude Code 的「完成後跑 lint」，把**驗證變成慣例（prompt＋工具）**，不做假裝自動判斷「正確」的系統。
2. **「during 對模型」只在長指令/背景成立**；單次同步調用不需要。**且不自動 kill**——攤訊號（停滯/暴衝）給模型與人判斷，硬逾時當最後 backstop。

---

## 5. 從 CC / Codex 研究抽出的可用招（對應階段）

| 招 | 來源 | 用在 |
|---|---|---|
| 背景執行＋poll 部分輸出＋kill（`run_in_background`/`BashOutput`/`KillShell`） | Claude Code | 目標 1 during（P2b） |
| 「Validating your work」段、依審批模式決定驗證積極度 | Codex | 目標 1 after「正確性＝驗證慣例」（P2a） |
| 「apply_patch 後不要重讀（會報錯）」＝結果告知狀態、省 token | Codex | 目標 1 after（P2a） |
| 穩定快取前綴 vs `<system-reminder>` 波動後注入 | Claude Code | 目標 1 before（P4，快取經濟） |
| 工具 schema 佔 token 大半（~15k） | Claude Code | 目標 3 量測（P0） |
| pass^k 可靠度（跨 k 次都成功） | τ-bench | 目標 3（P0） |
| relevance/irrelevance（該不該叫工具） | BFCL | 目標 3（P0） |
| 對真實 MCP server 測 | MCP-Bench 家族 | 目標 3（P0 延伸） |
| OTel `gen_ai`/`execute_tool` 標準屬性（tool/args/status/duration/tokens/finish_reason） | OpenTelemetry GenAI semconv | **統一 schema（P1）**，serves 1+2+3 |
| 模型面結構化 vs 人面渲染分離（file:// 禁用、可點路徑） | Codex/Claude Code | 目標 2（P3） |
| 依審批模式參數化 prompt（sandbox 段執行期注入） | Codex | 已對齊（execute_command 已依模式），驗證 |
| 逐腦調 emphasis（硬規則→軟化） | Claude Code 1.x→2.x | 目標 3 加值：用 eval 逐腦實測（P4） |

---

## 6. 計畫與實作排序

> 排序原則：**先有尺（P0）→ 統一遙測骨幹（P1）→ 模型面收口（P2）→ 人類面打磨（P3）→ 選配延伸（P4）。** 每階段獨立可回退、TDD（紅→綠）、附 eval 前後對照。

### Phase 0 — 量尺（目標 3 便宜部分）｜**✅ 已完成 2026-07-04**
- **內容**：擴 `tests/tool_eval/`：①`pass^k` 可靠度指標（跨 k 次全對的機率）；②BFCL 式 **relevance/irrelevance 案例**（`expect_no_tool`＋`abstention_accuracy`）；③長指令監控 stub（`MONITOR_STUB_CASES`，供 P2b 轉正）；④`scripts/measure_tool_token_cost.py` 量**廣告工具 schema token 成本**。
- **實作**（TDD 紅→綠、純測試層、無 runtime、無 ADR）：`metrics.pass_hat_k`/`mean_pass_hat_k`＋`Report.reliability`；`record_baseline` 多樣本改**逐試分開評分**（單樣本路徑逐位不變）；`cases.expect_no_tool`＋2 relevance SEED 案；`token_cost.measure_tool_token_cost`（過 ADR-0092 估算器 SSOT）。新增 5 測試檔（reliability/relevance/monitor_stub/token_cost＋更新 suite metrics）。
- **量測結果**：**Owner 廣告 27 工具 ≈ 6346 tokens**（比 Claude Code ~15k 精瘦，`mission_tools` 886 最肥）。兩腦 `--samples 3`：**grok-4.3 選對 100%／abstention 100%／pass^1=2=3=1.00**（3 次全穩）；**deepseek-v4-flash 選對 ~76%（pass^1）／abstention 100%／pass^k 0.76→0.67→0.57**——pass^k 立即揭露 DeepSeek 的**可靠度缺口**（平均選對率看不出）。
- **驗收**：ruff＋lint_ssot 全清、guard_all 37/37、pytest 2648 passed/5 skipped。**這把尺就位，P1~P3 的改動都可量「有沒有真的變好」。**

### Phase 1 — 統一遙測 schema（目標 1+2 共同骨幹，借 OTel 命名）｜**✅ 已完成 2026-07-04**
- **內容**：定義**單一「工具事件封套」SSOT**——屬性名對齊 OTel `gen_ai`（`tool.name`/`tool.arguments`/`status`/`error_class`/`duration_ms`/`tokens`/`finish_reason`）；**一份事實、兩個投影**：模型面（結構化，沿用 `tool_errors` 的 `error_class`）＋人面（SSE 渲染）。輕度重構既有 `tool_start`/`tool_stream`/`tool_result`→此封套。
- **細部計畫**：[`p1-telemetry-envelope-plan.md`](p1-telemetry-envelope-plan.md)（含現況盤點、OTel 對照表、TDD 拆解）。
- **實作**（TDD 紅→綠，四子項）：
  - **P1-A** 封套 SSOT `src/utils/tool_events.py`（`ToolCallEvent` dataclass ＋純函式 builder，OTel 命名、wire 凍結、additive `duration_ms`/`error_class`）＋`tool_errors.extract_error_class`。
  - **P1-B** `tool_executor` 的 tool_start/tool_result/error 字面量全走封套；`time_utils.get_monotonic_ms`（單調時鐘量 duration，wall-clock 會校時倒退）；成功事件帶 `duration_ms`、失敗（含 DENY）抬 `error_class`；`terminal` tool_stream 亦改 builder。
  - **P1-C** 新增 **Guard #38（Tool-Event Envelope Guard）**：AST 靜態擋 tool_start/tool_result/tool_stream 原始字面量繞過封套（判別 `id` vs Anthropic 區塊 `tool_use_id`，不誤傷；泛型 `error` 刻意不納入）。
  - **P1-D** 中央 SSE 序列化 SSOT `src/utils/sse.py::sse_line`（`ensure_ascii=False`）；三 handler（claude/gemini/openai）21 處 `f"data: {json.dumps(...)}"` 硬編全收斂。
- **借鑑**：OTel GenAI semconv（未來要接 Datadog/Phoenix 也免改）；保留 Touka 既有「模型面英文結構化 vs UI 面中文」分離。
- **驗收**：ruff（改動檔）＋lint_ssot 全清、**guard_all 38/38**（新增 #38）、pytest 全綠（+新測試）、vitest 綠＝**前端 wire 契約零改動的證明**。P0 離線 eval 數字不變（未動 schema/選擇邏輯）。
- **風險**：中（碰事件流）；緩解＝wire 欄位凍結、additive-only、排空佇列回歸、guard 防漂移。**只對齊命名與封套、不寫外部 exporter**（那放 P4）。

### Phase 2 — 目標 1 收口（模型面）｜**北極星本體**
- **P2a 驗證慣例（after 的「正確性」正解）**：新增依審批/唯讀模式決定的**驗證段**（改動類工具後，提示模型跑測試/lint/重讀核對）；`edit`/`write` 等**結果回傳最終狀態**讓模型免重讀（Codex 省 token 招）。
  - **P2a-1 驗證段 ✅ 已完成 2026-07-05＝[ADR-0123](../adr/0123-verification-convention.md)**：`prompt_blocks.build_validation_protocol(auth_level)` 純函式＋`<validation_protocol>` tag；ToolLayer 在**廣告集含 mutating dev verb**（write/edit/insert/delete/execute_command/evolution/edit_soul）時注入＝tool-presence gate 落地「範圍限開發流程、不碰 persona」；積極度依審批模式（Manual＝提醒、自動＝強制）。驗收：guard 38/38、pytest 2679（+7）、離線 eval 零回歸。
  - **P2a-2 工具回傳最終狀態（省重讀）✅ 已完成 2026-07-05＝[ADR-0124](../adr/0124-structured-success-and-applied-state-echo.md)**：先把成功偵測**結構化**（`tool_errors.build_tool_success`/`is_tool_success`＋`is_success` 以其為權威、舊 `"Error:"`/`"❌"` 子字串降為 legacy 後備＝根治誤判），再讓 edit/insert 回 `build_tool_success(preview=_applied_hunk(…))`（帶行號變更片段、免重讀即可驗證）；write/undo/delete 不動。驗收：guard 38/38、pytest 2683（+4）、離線 eval 零回歸。**P2a 至此完整。**
- **P2b 長指令 during 監控（併自「during 對模型」）✅ 已完成 2026-07-05＝[ADR-0125](../adr/0125-long-running-command-monitor.md)**：`execute_command(background=true)` 背景執行、立即回 running handle（backstop 600s、前景維持 30s）；`get_command_result` 的 running 回 `partial_output`＋`signals`（`stalled` 閒置≥20s／`runaway_output` 輸出≥256KB）＋elapsed/idle；新增模型面 `stop_command(execution_id)`（掛既有 `terminate_process_tree`、agent 隔離、先定案 stopped 再殺）；**兩層看門狗**＝harness 硬逾時 backstop＋模型 peek-and-decide，**絕不自動 kill**。廣告面 27→28；P0-C 的 `MONITOR_STUB_CASES` 轉正進 SEED。驗收：guard 38/38、pytest 2694（+11）、離線 eval aggregate 重算（n_cases 12）。
- **借鑑**：Codex「Validating your work」＋「不重讀」；Claude Code 背景執行＋poll＋kill。
- **驗收**：P0 的 relevance＋長指令案例通過；eval 選錯/幻覺不升、pass^k 不降。
- **風險**：中（P2b 動 terminal）；範圍限 **DebugAgent/開發流程**，不碰 persona/一般對話。

### Phase 3 — 目標 2 一致性 pass（人類面）｜**✅ 已完成 2026-07-05＝[ADR-0126](../adr/0126-tool-result-card-spec.md)**
- **內容**：一套**「工具結果卡片」設計規範**並套用**全工具類型**（非只 bash）：狀態圖示配色（成功綠/拒絕黃/錯誤紅，走 `text-status-*` token）、預設摺疊、`overflow` 限高不塞滿畫面、強制「模型面 JSON 不外顯」。
- **實作**（純渲染層、不動後端/stream_tree）：新 `lib/tool_result.ts::parseToolResult`（純函式判別聯集，**消費 P1 `error_class`／P2a-2 結構化成功＋preview**；`permission_denied`→denied 黃、其餘 error 紅、`ok:true`→success 綠、非結構化→raw）；`ToolStepTracker` 依 parsed 三態配色（`ShieldAlert`/`AlertCircle`/`CheckCircle2`＋`text-status-warning/error/success`）＋展開渲染人話（message/建議/preview）而非原始 JSON；i18n `suggestion_label` 雙語。
- **借鑑**：Codex CLI-renderer 感知格式（模型面結構化 vs 人面渲染分離）。
- **驗收**：ESLint＋tsc（Guard #27）＋前端 vitest **944 passed/184 檔**（+10）＋Guard #5 主題/#33 i18n／guard_all 38/38。**範圍守住**＝「一次卡片規範＋套用」，未膨脹成大前端（`duration_ms`／bash markdown 合流留另案）。

### Phase 4 — 選配延伸（三子項獨立、另案）
- **快取前綴優化 ✅ 已完成 2026-07-05＝[ADR-0127](../adr/0127-prompt-cache-prefix-optimization.md)**（擁有者三選一裁示）：`tool_schema.apply_tool_cache_breakpoint` 在最後一個工具加 Anthropic `cache_control` ephemeral——快取**最大穩定塊＝工具 schema**（P0 量到 ≈6346 token），ReAct 迴圈重複命中省 ~90% 讀取 token/加速；`claude_handler` Anthropic-only 套用、無 flag、graceful。驗收：guard 38/38、pytest 2698（+4）。
- **system 前綴快取 ✅ 已完成 2026-07-05＝[ADR-0128](../adr/0128-system-prompt-prefix-caching.md)**（延續快取前綴）：`tool_schema.to_cached_system` 把 system 字串轉 block list＋cache 斷點；`claude_handler` 迴圈前套用——ReAct 迴圈內 system 穩定→命中；遙測零改動（`_system_to_text` 早已相容 block list）。驗收：guard 38/38、pytest 2703（+5）。
- **穩定前綴細分 ✅ 已完成 2026-07-05＝[ADR-0129](../adr/0129-stable-prefix-split-caching.md)**（讓 persona 跨回合快取）：`prompt_blocks.VOLATILE_SYSTEM_TAGS`（mission/summary）＋`to_cached_system` 於最早波動 tag 切成 `[穩定前綴(斷點), 波動後綴(斷點)]`——**兩斷點疊加**：穩定前綴（persona）跨回合命中、波動後綴迴圈內命中。加上 tools 斷點＝三層快取。驗收：guard 38/38、pytest 2708（+5）。
- **中介層順序重排 ✅ 已完成 2026-07-05＝[ADR-0130](../adr/0130-stable-first-system-reorder.md)**（讓穩定工具區塊也進跨回合前綴）：`tool_schema.reorder_system_stable_first` 在送出前把波動區塊（跨 SoulLayer/ToolLayer 兩層注入的 summary/mission）逐字搬到 system 尾端，使 `tool_availability`／`validation_protocol` 連進穩定前綴——**不動 pipeline 層順序**（承重）、只在單一出場點正規化；冪等、尾端即 no-op；逐 tag 遙測相容。四層快取前綴收尾。驗收：guard 38/38、pytest 2715（+7）。
- **其餘子項維持選配/另案**：**逐腦 emphasis 調校**（用 P0 eval 實測「某句 IMPORTANT 對 DeepSeek 有沒有用」，需 LIVE、探索性）；**真 OTel exporter/spans**（需 SDK 依賴＋遙測外送，**與本地優先/隱私/開源定位有張力**，擁有者未選、不做）。

---

## 7. 風險與明確「不做」

- **不做**：自動判斷「正確」；自動 kill 長指令（只發訊號＋硬逾時 backstop）；追 BFCL 跑分（Touka 非通用模型）；把防禦安全拒絕塞進 soul/prompt（維持 sandbox-first，權威在 permission/harness）；P1 一次做到外部 OTel exporter（放 P4）。
- **範圍控制**：P2b 限 DebugAgent/開發；P3 限一次卡片規範。
- **多腦**：所有改動以 P0 的 eval 逐腦驗收（grok/deepseek），弱腦可能仍需硬規則——由數據決定，非品味。

---

## 8. 決策紀錄與待明示項

- 排序＝ **P0 ✅ → P1 ✅ → P2 ✅（a＋b）→ P3 ✅ → P4 快取前綴 ✅（四層：工具＋system＋穩定前綴細分＋順序重排；其餘選配）**；理由：先有尺、再統一骨幹、再收模型面（北極星）、再人面打磨、最後選配優化。
- **P0、P1（2026-07-04）、P2、P3、P4 快取前綴（工具＋system＋順序重排，2026-07-05）已完成**（見上）。**提案三目標的可交付部分全數收口**：量測（P0）／模型面 before-during-after（P2）／人面卡片一致性（P3，[ADR-0126](../adr/0126-tool-result-card-spec.md)），皆以 P1 統一封套（[ADR-0122](../adr/0122-tool-event-telemetry-envelope.md)）為骨幹；P4 取**快取前綴**並推廣至工具 schema（[ADR-0127](../adr/0127-prompt-cache-prefix-optimization.md)）＋system（[ADR-0128](../adr/0128-system-prompt-prefix-caching.md)）＋穩定前綴細分讓 persona 跨回合快取（[ADR-0129](../adr/0129-stable-prefix-split-caching.md)）＋中介層順序重排讓穩定工具區塊也進跨回合前綴（[ADR-0130](../adr/0130-stable-first-system-reorder.md)，**四層斷點疊加**）。**提案主體收官**；剩餘僅選配另案（逐腦 emphasis、OTel exporter），**擁有者明示啟動前不改任何程式碼。**
- P0（純測試層）不立 ADR；**P1 因動 runtime 事件流，已正式收斂為 [ADR-0122](../adr/0122-tool-event-telemetry-envelope.md)**（延續 0119~0121 工具系列）。
- **🏁 收官（2026-07-05）**：本提案自 2026-07-04 起 P0→P4 逐階段 TDD（盤點→紅燈→綠燈→全套驗證→ADR＋文件、由人類 commit）落地完畢。最終驗收：**guard_all 38/38、後端 pytest 2715 passed/4 skipped、前端 vitest 944/184、ADR 0001..0130 連續無斷鏈**。可交付範圍全數收口；**兩項選配另案（逐腦 emphasis、OTel exporter）不在本提案範圍內收束，若日後啟動另立提案/ADR**。工具系列自此告一段落（ADR 0119~0130，共 12 份）。
