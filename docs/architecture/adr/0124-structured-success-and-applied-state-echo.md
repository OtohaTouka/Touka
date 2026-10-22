# ADR-0124: 結構化成功偵測與工具回傳最終狀態（Structured Success & Applied-State Echo）

## 狀態

✅ **已接受（實作與驗證 2026-07-05、全綠——guard_all 38/38、後端 pytest 2683 passed/5 skipped）**。本 ADR 完成「[工具可觀測性與可靠度](../proposals/tool-observability-and-reliability.md)」提案 **Phase 2a 的第二半（P2a-2）**，與 [ADR-0123](0123-verification-convention.md)（驗證慣例）合為 P2a 全貌，延續工具系列（[ADR-0119](0119-unified-tool-interface-redesign.md)~[ADR-0123](0123-verification-convention.md)）。

## 日期

2026-07-05

## 背景

[ADR-0123](0123-verification-convention.md) 把「改動後自我驗證」定為慣例，但**刻意暫緩** P2a-2「工具回傳最終狀態（Codex『apply_patch 後不要重讀』省 token）」，原因是盤點發現一個脆弱點：[`tool_executor`](../../../ToukaProxy/src/services/tool_executor.py) 判定工具成敗的 `is_success` 靠**子字串 `"Error:"`／`"❌"`** 偵測（Tier 1 之前的遺留啟發）。若讓 `edit`／`insert` 把變更後的檔案片段回傳給模型，而該片段含 `Error:`／`❌`（本 repo 大量用 ❌），會把**成功誤判成 error**。

本 ADR 先把成功偵測**結構化**（延續 [ADR-0122](0122-tool-event-telemetry-envelope.md) 的封套精神／Tier 1 的結構化錯誤），再安全地讓 `edit`／`insert` 回傳最終狀態。

## 決策

1. **結構化成功 SSOT**（[`tool_errors`](../../../ToukaProxy/src/services/tool_errors.py)）：新增 `build_tool_success(message, *, preview=None, **fields)` → `{"ok": true, "message": …, "preview": …}`（`ok` 保證第一個 key）＋ `is_tool_success(text)`（前綴 `{"ok"` 偵測）；並納入 `is_structured_payload`（結構化成功也跳過時間尾綴注入，保持 JSON 可解析）。與既有 `build_tool_error` 對稱。
2. **執行層穩健判定**（`tool_executor.is_success`）：`is_tool_success(result_text)` **為權威**成功訊號；舊子字串啟發（`"Error:"`／`"❌"` not in）**保留為未結構化 legacy 工具的後備**。故結構化成功即使回傳內容含 `Error:`／`❌` 也不誤判，legacy 工具零回歸。
3. **`edit`／`insert` 回傳最終狀態**（[`file_verbs`](../../../ToukaProxy/src/tools/file_verbs.py)）：成功時回 `build_tool_success(message, preview=<hunk>)`。新增純函式 `_applied_hunk(content, line_1based, radius=3)`＝帶行號、以 `>` 標記變更行的變更周邊片段，讓模型**免重讀**即可驗證（呼應 ADR-0123：驗證要便宜）。`edit` 由 snapshot 推算新內容與變更行；`insert` 用已在本地組好的行。
4. **範圍**：只 `edit`／`insert` echo 片段（in-place 改動、模型最需看結果）；`write`（模型已有內容）／`undo`（回復、模型知先前狀態）／`delete`（無內容可 echo）**不動**——無 echo 即無 `is_success` 風險，維持原字串回傳。

## 後果

- **脆弱的子字串成敗判定被結構化訊號兜底**：結構化成功的工具（edit/insert）不再受回傳內容影響；這也為未來其他「回傳含使用者內容」的工具提供了安全回傳途徑。
- **省重讀、支撐驗證慣例**：模型改完 edit/insert 直接從 `preview` 驗證，不必再叫一次 read（Codex 省 token 招），正是 [ADR-0123](0123-verification-convention.md) 驗證慣例「檢視工具回傳而非重開檔案」的落地。
- **驗收**：ruff（改動檔）＋lint_ssot 全清、**guard_all 38/38**、**pytest 2683 passed/5 skipped**（+4 新測）、離線 eval 數字**零回歸**（未動工具 schema／選擇邏輯）。既有 `test_file_verbs` 的 `"Success" in` 斷言更新為 `is_tool_success`＋`preview`（edit/insert 的正當回傳格式變更；undo 維持字串）。
- **不做**：把全部工具一次遷成結構化成功（legacy 子字串後備刻意保留、逐步遷移）；為 `write`/`delete` 加無意義的內容 echo。

## 相關

- [ADR-0123](0123-verification-convention.md) — 驗證慣例（P2a 第一半；本 ADR 完成 P2a）
- [ADR-0119](0119-unified-tool-interface-redesign.md) — 統一檔案動詞（edit/insert 所在）
- [ADR-0122](0122-tool-event-telemetry-envelope.md) — 遙測封套（結構化訊號精神一脈）；Tier 1 結構化錯誤（`build_tool_error` 對稱物）
- Codex CLI「apply_patch 後不要重讀＝結果告知狀態、省 token」——回傳最終狀態來源
- 後續（另案）：**P2b**（長指令 during 監控）、P3（人面卡片一致性）
