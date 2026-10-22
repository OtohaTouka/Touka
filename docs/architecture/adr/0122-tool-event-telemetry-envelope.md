# ADR-0122: 統一工具事件遙測封套（Unified Tool-Event Telemetry Envelope, OTel gen_ai 對齊）

## 狀態

✅ **已接受（實作與驗證於 2026-07-04 完成、全綠——guard_all 38/38、後端 pytest 2672 passed/5 skipped、前端 vitest 934 passed；本 ADR 於 2026-07-05 補立）**。本 ADR 是「[工具可觀測性與可靠度](../proposals/tool-observability-and-reliability.md)」提案的 **Phase 1**，延續工具介面重設計系列（[ADR-0119](0119-unified-tool-interface-redesign.md)/[ADR-0120](0120-async-command-approval.md)/[ADR-0121](0121-evolution-pipeline-merge.md)）。同提案的 P0「量尺」屬純測試層（未立 ADR，照多回合 eval 先例）；P1 因輕度重構 **runtime 事件流**，故正式立此 ADR。細部 TDD 拆解見 [`p1-telemetry-envelope-plan.md`](../proposals/p1-telemetry-envelope-plan.md)。

## 日期

2026-07-05（實作 2026-07-04）

## 背景

工具生命週期的 SSE 事件（`tool_start` / `tool_stream` / `tool_result` / `error`）散落各處、各自為政：皆為 `tool_executor.py`（4 處）、`terminal.py`、`interaction.py` 內的**臨時 dict 字面量**；三個 handler（claude/gemini/openai）各自硬編 `f"data: {json.dumps(ev)}\n\n"`（21 處），**無中央序列化、無事件型別註冊表**。錯誤類別 `error_class` 埋在 `result_text` 的 JSON 字串裡（非一級屬性）；工具執行**完全無 duration 量測**。

提案北極星＝提高模型調用工具的正確與成功率。介面層已由 ADR-0119~0121 收斂；P1 處理「調用的可觀測性骨幹」——先建**單一遙測封套**，讓後續 P2（模型面）/P3（人面）都消費同一份事實、不必返工。命名對齊 **OpenTelemetry GenAI semantic conventions**（`gen_ai.*` / `execute_tool` span），未來要接外部觀測工具免改生產端。

事前完整測繪：後端事件流（上述缺口）＋前端**凍結契約**（[`connections.ts`](../../../ToukaChat/src/lib/connections.ts) 的 `customEventTypes` ＋ [`stream_tree.ts`](../../../ToukaChat/src/lib/stream_tree.ts) `applyEventToTree` 硬比對 `id`/`tool`/`args`/`status`/`result`/`chunk`）。據此界定：**輕度重構、wire 欄位凍結、只借 OTel 命名不引 SDK**。

## 決策

1. **封套 SSOT `ToolCallEvent`**（[`src/utils/tool_events.py`](../../../ToukaProxy/src/utils/tool_events.py)，frozen dataclass，鏡像 `progress_events.py` 純函式風格）——屬性名對齊 OTel gen_ai：`tool_name`←`gen_ai.tool.name`、`call_id`←`gen_ai.tool.call.id`、`arguments`←`gen_ai.tool.call.arguments`、`operation="execute_tool"`←`gen_ai.operation.name`、`error_class`←`error.type`、`duration_ms`←span duration。同檔定義 wire 型別常量（`TOOL_START`…）＝事件型別的**單一定義處**。
2. **一份事實、兩個投影**：
   - **人面投影** `to_result_sse()` → 既有 `tool_result` SSE 事件。**wire 凍結四鍵**（`type`/`id`/`result`/`status`）恆在；`error_class`、`duration_ms` 僅在有值時 **additive** 附加（前端 `?.` 忽略未知鍵，人面卡片重整留待 P3）。內部 span status（`ok`/`error`）由 `status_wire()` 映射回前端字串（`success`/`error`）。
   - **模型面投影**：沿用 Tier 1 的 `tool_errors` 結構化 JSON（`result_text` 不變，不破壞既有模型自我修正）；新增 `tool_errors.extract_error_class`（`raw_decode` 只解析開頭 JSON、容忍尾端時間附註）把類別抬成封套一級屬性。
3. **執行層改走封套**（[`tool_executor.py`](../../../ToukaProxy/src/services/tool_executor.py)）：identity-missing / DENY / 主結果 / 例外 / tool_start 全部經封套 builder 產生。**duration 量測**用單調時鐘 [`time_utils.get_monotonic_ms`](../../../ToukaProxy/src/utils/time_utils.py)（`perf_counter`；wall-clock epoch 會因 NTP 校時倒退、不可用於間隔），且**僅在工具實際執行的路徑**設值——DENY／identity-missing 等未執行的早退帶 `error_class` 但**不帶** `duration_ms`。`terminal.py` 的 `tool_stream` 亦改 builder。
4. **事件一致性守衛（Guard #38）**（[`scripts/guard_tool_events.py`](../../../scripts/guard_tool_events.py)，註冊於 `guard_all.py`，守衛 37→**38**）：純 AST 靜態掃描，擋 `tool_start`/`tool_result`/`tool_stream` 原始字面量繞過封套。**判別器＝字面 `id` 鍵**——SSE 事件帶 `id`，Anthropic `tool_result` 訊息內容區塊帶的是 `tool_use_id`（見 `openai_to_claude.py` / `claude_handler.py` 回灌模型處），據此乾淨排除誤判。泛型 `"error"` 型別**刻意不納入**（型別名太泛、易誤判），改由 builder ＋文件約定。
5. **SSE 序列化 SSOT**（[`src/utils/sse.py::sse_line`](../../../ToukaProxy/src/utils/sse.py)，`ensure_ascii=False`）：收斂三 handler 21 處硬編。傳輸層為 UTF-8、前端 `JSON.parse` 與是否轉義無關，且省 payload。

## 後果

- **P2/P3 直接消費**：`duration_ms`／`error_class` 已成一級屬性、`sse_line` 為序列化 SSOT、事件型別單一定義——模型面收口（P2）與人面卡片（P3）不必返工。未來若接外部 OTel exporter（P4），命名可直接映射、免改生產端。
- **前端零改動**：wire 欄位全凍結，新屬性 additive；vitest **934 passed / 183 檔**＝凍結契約的證明。
- **安全重構事件流三件套**：wire 凍結＋additive-only＋Guard #38 防漂移，佐以排空佇列的回歸測試（`tests/proxy/test_tool_events_emit.py` ＋既有 `test_progress_emit.py` / `test_tool_errors_structured.py`）。
- **驗收**：ruff（改動檔）＋lint_ssot 全清、**guard_all 38/38**、**pytest 2672 passed/5 skipped**（+24 新測）、離線 eval 數字**零回歸**（未動工具 schema／選擇邏輯，P0 的尺確認）。
- **明確不做（範圍控制）**：不改 wire 欄位名（→P3）；不寫外部 OTel exporter／spans（→P4）；回合層 `tokens`/`finish_reason` 不塞進工具事件（已由 `progress_events.token_usage_event` 承載、共用命名詞彙即可）；不動 `touka_progress`／`log`／`touka_status`。
- **遞延（另案）**：`group.py`／`dispatcher.py` 另約 15 處 `f"data: {json.dumps}"` 尚未收斂進 `sse_line`（本輪限三 handler；日後可 ratchet）。既有 `gemini_handler.py:117` 的 E501（錯誤訊息行、非本次造成）留置。

## 相關

- [ADR-0119](0119-unified-tool-interface-redesign.md) — 原生工具介面重設計（Tier 1 結構化錯誤＝本 ADR 模型面投影的基礎）
- [ADR-0120](0120-async-command-approval.md) / [ADR-0121](0121-evolution-pipeline-merge.md) — 工具介面重設計收尾系列
- [ADR-0092](0092-context-window-token-sliding-window.md) — token 估算器 SSOT（P0 token 成本量測沿用；`token_usage_event` 共用遙測詞彙）
- [工具可觀測性與可靠度提案](../proposals/tool-observability-and-reliability.md) §6 Phase 1 ＋ [P1 細部計畫](../proposals/p1-telemetry-envelope-plan.md)
- OpenTelemetry GenAI semantic conventions（`gen_ai.*` / `execute_tool` span）——命名對齊來源
- 下一步（另案）：提案 **P2a 驗證慣例**（Codex「Validating your work」）、P2b 長指令監控
