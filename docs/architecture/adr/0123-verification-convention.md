# ADR-0123: 驗證慣例——改動後自我驗證的 prompt 段（Verification Convention）

> **現況**：決策 3 的積極度從二元改成三分（ADR-0143 修訂）。

## 狀態

✅ **已接受（實作與驗證 2026-07-05、全綠——guard_all 38/38、後端 pytest 2679 passed/5 skipped；擁有者尚未 commit）**。本 ADR 是「[工具可觀測性與可靠度](../proposals/tool-observability-and-reliability.md)」提案 **Phase 2a** 的核心（模型面「after／正確性」收口），延續工具系列（[ADR-0119](0119-unified-tool-interface-redesign.md)~[ADR-0122](0122-tool-event-telemetry-envelope.md)）。**P2a-2「工具回傳最終狀態（省重讀）」暫緩**——見「後果」。

> 🔧 **決策 3「積極度依審批模式」已被 [ADR-0143](0143-hitl-unification.md)（H4）由二元改為三分**：原「Manual＝提醒／自動模式＝強制」→現 `build_validation_protocol(auth_level, *, interactive=True)` 三分支——Manual+互動＝審批卡提醒／Manual+headless＝fail-closed「拒絕、勿重試」／自動＝強制（0143 明言舊 Manual 措辭在 fail-open 時代不準）。`validation_protocol` 塊、tool-presence gate（`_MUTATING_DEV_TOOLS`）核心仍有效。

## 日期

2026-07-05

## 背景

提案的第一條誠實邊界：**「正確／錯誤」不是遙測能給的**。工具層只知道「有沒有執行成功」；一個**成功卻寫錯檔／改錯地方**的調用，工具層無從得知「錯」。[ADR-0122](0122-tool-event-telemetry-envelope.md) 的封套帶 `status`/`error_class`，但那是「執行成敗」，不是「語意正確」。

業界作法不是假裝自動判斷「正確」，而是把**驗證變成慣例**：Codex CLI 的「Validating your work」段（依審批模式決定驗證積極度）、Claude Code 的「完成後跑 lint／測試」。本 ADR 據此把「改動後自我驗證」定為模型面慣例——**不做假裝自動判斷正確的系統**。

## 決策

1. **`<validation_protocol>` prompt 段**（[`prompt_blocks.build_validation_protocol(auth_level)`](../../../ToukaProxy/src/services/prompt_blocks.py)，純函式）：模型面英文，核心訊息＝「無錯誤 ≠ 正確——改動後要驗證：檢視工具回傳而非假設成功、可跑測試/lint、確認滿足需求並檢查不變式、發現問題就修好再驗」。新增 tag `VALIDATION_PROTOCOL` 進 `ALL_BLOCK_TAGS`（ADR-0092 context breakdown 可解析）。
2. **僅開發流程注入**（[`ToolLayer`](../../../ToukaProxy/src/middleware/layers/tool.py)）：廣告工具集含任一 **mutating dev verb**（`write`/`edit`/`insert`/`delete`/`execute_command`/`evolution_define`/`evolution_implement_and_verify`/`edit_soul`）時才注入。唯讀／一般 persona 對話無改動類工具 → 不注入，避免無謂 token 與語氣干擾。**以工具存在為門（tool-presence gate）＝提案「範圍限開發流程、不碰 persona」的乾淨落地**（不靠身分硬編）。
3. **積極度依審批模式**（提案「依審批模式決定驗證積極度」）：`Manual`（逐步人審）＝提醒（驗證仍是你的責任）；自動模式（`SafeAuto` 等、免逐步人審）＝**強制**（動作免逐步批准，自我驗證為義務、絕不對未驗證的改動宣告成功）。
4. **注入機制沿用既有範式**：`wrap_block(VALIDATION_PROTOCOL, …)` → `inject_anthropic_system`／`inject_openai_system`（與 `<tool_availability>`／`<mission_awareness>` 同路徑，格式一致）。

## 後果

- **模型面「正確性」有了正解**：不再期待遙測判斷對錯，而是提示模型自我驗證——與 P1 封套（知道成敗）互補、分工明確。
- **範圍自然收斂**：tool-presence gate 讓一般對話零負擔，只有真的能改動東西的開發情境才提示驗證。
- **驗收**：ruff（改動檔）＋lint_ssot 全清、**guard_all 38/38**、**pytest 2679 passed/5 skipped**（+7 新測）、離線 eval 數字**零回歸**（未動工具 schema／選擇邏輯）。既有 `test_no_filtering_no_note` 更新為「無過濾→無 `<tool_availability>`」（validation_protocol 依工具存在另行注入、與過濾無關，屬本 ADR 正當行為變更）。
- **P2a-2「工具回傳最終狀態（省重讀）」暫緩（另案）**：Codex 的「apply_patch 後不要重讀」需 `edit`/`insert` 把變更後的區段回傳給模型。但盤點發現 [`tool_executor`](../../../ToukaProxy/src/services/tool_executor.py) 的 `is_success` 以**子字串 `"Error:"`／`"❌"` 偵測**成敗——若讓 `edit`/`insert` 回傳檔案內容，而該內容含 `Error:`／`❌`（本 repo 大量用 ❌），會把成功誤判為 error。故 P2a-2 需**先把成功偵測結構化**（延續 [ADR-0122](0122-tool-event-telemetry-envelope.md) 的封套精神）方能安全落地，拆為 fast-follow，避免引入回歸。
- **不做**：假裝自動判斷「正確」的系統；把驗證塞進 persona/一般對話；動工具 schema。

## 相關

- [ADR-0119](0119-unified-tool-interface-redesign.md)~[ADR-0122](0122-tool-event-telemetry-envelope.md) — 工具介面重設計＋遙測封套系列（本 ADR 為模型面「after」收口）
- [ADR-0092](0092-context-window-token-sliding-window.md) — prompt 區塊 tag SSOT／context breakdown（新增 `validation_protocol` tag）
- [ADR-0143](0143-hitl-unification.md) — HITL 統一（**修訂決策 3：積極度二元→三分，加 `interactive` 分支**）
- [工具可觀測性與可靠度提案](../proposals/tool-observability-and-reliability.md) §6 Phase 2a
- Codex CLI「Validating your work」、Claude Code「完成後跑 lint」——慣例化來源
- 後續（另案）：**P2a-2**（結構化成功偵測 → `edit`/`insert` 回最終狀態省重讀）、**P2b**（長指令 during 監控）
