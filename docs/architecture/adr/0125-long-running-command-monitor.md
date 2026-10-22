# ADR-0125: 長指令 during 監控——背景執行、部分輸出/停滯訊號與 stop_command（Long-Running Command Monitor）

## 狀態

✅ **已接受（實作與驗證 2026-07-05、全綠——guard_all 38/38、後端 pytest 2694 passed/4 skipped）**。本 ADR 完成「[工具可觀測性與可靠度](../proposals/tool-observability-and-reliability.md)」提案 **Phase 2b**，與 [ADR-0123](0123-verification-convention.md)／[ADR-0124](0124-structured-success-and-applied-state-echo.md)（P2a）合為 **Phase 2「模型面收口（北極星本體）」全貌**。延續工具系列（[ADR-0119](0119-unified-tool-interface-redesign.md)~[ADR-0124](0124-structured-success-and-applied-state-echo.md)），沿用 [ADR-0120](0120-async-command-approval.md) 的非同步結果機制。

## 日期

2026-07-05

## 背景

提案的「目標 1 during（含長指令監控）」與現況三缺口（§3）：[`terminal`](../../../ToukaProxy/src/tools/terminal.py) 的 `_execute_core` **硬編 30 秒逾時**（長指令根本跑不完就被殺）；`get_command_result` 的 running 狀態**不回傳部分輸出**（模型看不到進度）；**無模型面 stop_command**。

第二條誠實邊界（提案 §4）：「during 對模型」只在**長指令/背景**成立，**且不自動 kill**——攤停滯/暴衝訊號給模型與人判斷，硬逾時當最後 backstop。借鑑 Claude Code 的背景執行＋poll 部分輸出＋kill（`run_in_background`/`BashOutput`/`KillShell`）。

## 決策

1. **背景執行**（`execute_command(background=True)`）：長指令背景跑、**立即回 running handle（含 execution_id）不阻塞**；`_run_background` 背景任務 backstop 硬逾時放寬為 **600 秒**（前景 inline 維持 30 秒、行為不變）。
2. **`_execute_core` 重構**：加 `timeout` 參數＋可選 `record`——背景模式逐行寫入 `record["output"]`／`last_output_at` 並存 `_proc`，供部分輸出＋停滯偵測＋硬終止。
3. **`get_command_result` running → 部分輸出＋諮詢訊號**：回 `partial_output`（尾段）＋`signals`（`stalled`＝閒置 ≥ 20s、`runaway_output`＝累積輸出 ≥ 256KB）＋`elapsed_seconds`／`idle_seconds`。**訊號僅諮詢——系統絕不自動 kill**。
4. **模型面 `stop_command(execution_id)`**：掛既有 [`terminate_process_tree`](../../../ToukaProxy/src/services/process_registry.py)（連坐殺行程樹、P5d）、**依 agent 隔離**（跨身分不洩漏存在性）、先同步定案為 `stopped`（附部分輸出）再硬終止（背景任務不覆寫）；已完成則回結構化成功「無需終止」。
5. **兩層看門狗**：harness 硬逾時 backstop（600s，最後防線）＋模型 peek-and-decide（看 `get_command_result` 訊號後**自行決定** stop_command）。**不自動 kill**。
6. **廣告面 27→28**（新增 `stop_command`）：registry／`builtin_tools_schema`／`CRITICAL_TOOLS` 同步；`execute_command` schema 加 `background`、`get_command_result` schema 描述部分輸出＋訊號。
7. **eval 轉正**：P0-C 的 `MONITOR_STUB_CASES` 於本階段落地後**移入 `SEED_CASES`**（`long-run-monitor`：背景跑→見 `stalled`→`stop_command`；三工具皆列 expected＝正確流程、不計 wrong_tool）。

## 後果

- **長指令可跑**：測試套件／建置不再 30 秒被殺；模型背景跑、poll 進度、必要時收手。
- **模型面 during 到位、且守住邊界**：模型看得到部分輸出與停滯/暴衝訊號，但**決定權在模型/人**、系統只發訊號＋硬逾時 backstop——不做「自動判斷該不該 kill」的系統（呼應提案不做假裝自動判斷的原則）。
- **完成 Phase 2（北極星本體）**：模型面 before（Tier 2 schema）／during（本 ADR）／after（[ADR-0123](0123-verification-convention.md) 驗證慣例＋[ADR-0124](0124-structured-success-and-applied-state-echo.md) 回最終狀態）全數收口。
- **驗收**：ruff（改動檔）＋lint_ssot 全清、**guard_all 38/38**、**pytest 2694 passed/4 skipped**（+11；監控 stub 轉正、離線 aggregate 重算 n_cases 11→12）。
- **範圍界線（本 ADR 落地時；已被 [ADR-0143](0143-hitl-unification.md) H2 超越）**：本 ADR 落地時 `background` 僅套用於安全（ALLOW）路徑；危險（CONFIRM）指令維持 ADR-0120 的審批→前景執行，background+審批之組合當時列為未來另案。**更新（G-3，ADR-0143 H2）**：ADR-0143 H2 令互動 CONFIRM 改走統一 broker 於 tool_executor Gate-2 預核准（`_hitl_preapproved=True`）；核准後 `execute_command(background=True)` 現已落背景分支——此組合已可達且安全（人類知情同意為 Gate-2 前置、background 不繞過審批；完整繼承本 ADR 的 600s backstop／never-auto-kill／`stop_command`／agent 隔離；headless 仍 fail-closed）。行為由 `tests/unit/test_background_command.py::test_dangerous_background_preapproved_runs_and_keeps_danger_badge` 釘死（含背景完成卡保留 ⚠️ [危險] 徽章）。行程登記表仍 per-session；per-execution 硬終止靠 record 內的 `_proc`。
- **不做**：依訊號自動 kill；把 during 套到單次同步調用（無此需要）。

## 相關

- [ADR-0120](0120-async-command-approval.md) — 指令非同步審批（共用 `_ASYNC_RESULTS` 結果機制與 agent 隔離）
- [ADR-0123](0123-verification-convention.md)／[ADR-0124](0124-structured-success-and-applied-state-echo.md) — P2a（本 ADR 與其合為 P2 全貌）
- [ADR-0076](0076-evolution-sandbox-os-resource-limits.md) — 行程樹終止／資源限制（`terminate_process_tree` 同源，P5d）
- Claude Code：`run_in_background`／`BashOutput`／`KillShell`——背景執行＋poll＋kill 借鑑來源
- [工具可觀測性與可靠度提案](../proposals/tool-observability-and-reliability.md) §6 Phase 2b
- 後續（另案）：**P3**（人面「工具結果卡片」一致性）、P4（選配：OTel exporter／逐腦 emphasis 調校）
