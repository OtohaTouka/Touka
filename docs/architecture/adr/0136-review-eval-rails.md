# ADR-0136: 互審驗收量測軌——LIVE episode＋gate 指標＋非 LLM oracle（Phase 3 前置）

## 狀態

✅ **已接受（實作與驗證 2026-07-07；擁有者尚未 commit）**。[對抗式互審設計文件](../proposals/adversarial-review-subsystem-design.md) **§7.1 前置工程（A/B/C）**——design-independent、「需求 2 最大的隱藏工程」：沒有這條量測軌，「互審會降幻覺」永遠無法被證偽＝邊界 1 無法觸發＝需求 2 不可宣稱完成。

> **2026-08-14 修訂（[ADR-0214](0214-standalone-smoke-data-and-metered-api-boundaries.md)）**：
> LIVE episode 永久排除 pytest／smoke／CI／guard。CLI 固定單一 case＋DebugAgent，app-data／
> workspace／config／artifact 全錨定 temp sandbox；Gemini、`research-auto` 與非 DebugAgent
> fail-closed。每次執行仍須揭露 model、case、最大輪次／requests／tokens、額度與工具寫入範圍，
> 並取得該次人類同意；offline replay 不受影響。

## 日期

2026-07-07

## 背景

互審設計 workflow 的核碼支點 3（✔已複核）：`harness._run_case_live` 是 `NotImplementedError` stub、唯一 LIVE 路徑 `live_select_only` 只選不執行（無 artifact、無 persona/soul）；且既有指標 `hallucinated_tool_rate`（工具名捏造）／`selection_ok`（選對工具）與互審要降的 **artifact 內容／宣稱型幻覺不同軸**——門檻若劃在無關曲線上恆無訊號（設計文件 §6.2-7 否決其作為主指標）。五臂 A/B（§7.2）需要：真跑 producer 產 artifact、episode 級 gate 指標、與**不靠 LLM** 的失敗裁決。

## 決策

1. **A：LIVE episode seam 接通**（[`live_episode.py`](../../../tests/tool_eval/live_episode.py)）：`run_case_live_episode(case, *, model, agent_id=None, artifact_root=None)` 跑**真實 in-process producer 子 loop**——重用 Phase 1（[ADR-0134](0134-in-process-subagent-dispatch.md)）的 `_assemble_subagent_request`＝完整 ProxyPipeline 組裝（**persona/soul 依身分注入**——隔離對照 C-same vs C-diff 的必要條件）；工具**真的執行**（真實 builtin registry、受 Gate 1/2 約束）；SSE 抽 `tool_calls`（`tool_start`/`tool_result` join；status 映射 success→ok／`error_class=permission_denied`→denied／其餘→error）＋最終文字＋ **Phase 2 回報塊**解析（[ADR-0135](0135-structured-report-finalize-and-board-bound-stop.md) claims/artifact_refs/confidence）＋artifact 存在檢查（顯式 `artifact_root`，唯讀）。`harness._run_case_live` 改實作（舊契約 `list[ToolCall]` 保持）：**顯式 `live_model` 必填**——不讀環境、不猜預設，真實跑＝擁有者手動帶金鑰（新增 [scripts/run_tool_eval_episode.py](../../../scripts/run_tool_eval_episode.py)，不進 CI）。
2. **B：注錯欄位＋gate 指標**（[`cases.py`](../../../tests/tool_eval/cases.py)／[`metrics.py`](../../../tests/tool_eval/metrics.py)）：`EvalCase` additive 新欄位——`injected_failure ∈ {none, goal_drift, fabrication, unverified_claim}`（gate_recall 的分母標籤）、`expected_artifact_check`（oracle 機械判定規格：`must_exist`／`forbidden_content`／`required_content`）、`ground_truth`（人類閱讀，機器不消費）。metrics 新增 episode 級 `GateOutcome`→`gate_recall`（抓壞率）／`gate_precision`（擋得準率）／`false_block_rate`（誤殺率）＋`aggregate_gate`／`format_gate_report`——分母為零一律回 `None`（誠實呈現不可算）。**明文軸警告** `HALLUCINATION_AXIS_NOTE`：舊指標不可作為互審降幻覺主指標（測試釘住）。既有 `aggregate`／baseline 零改動。
3. **C：非 LLM oracle**（[`oracle.py`](../../../tests/tool_eval/oracle.py)）：`judge_episode(case, report, artifact_root) -> OracleVerdict` 全機械裁決——**fabrication**（宣稱的 artifact 不存在；宣稱可運作的 `.py` 解析失敗或 AST 裸名呼叫未定義——屬性呼叫保守跳過不誤殺）／**unverified_claim**（有 claims 零 artifact；要求產出但回報無結構）／**goal_drift**（`forbidden_content` 命中、`required_content` 缺失）／**missing_artifact**（`must_exist` 缺）。catch 判定不靠另一個 LLM judge，避免「用會幻覺的東西驗幻覺」的循環信任。

## 後果

- **邊界 1 可觸發**：五臂 A/B（§7.2）所需的三根量測支柱就位——真跑產 artifact（A）、gate 指標（B）、機械 ground truth（C）。Phase 3 本體落地後，gate 產 `GateOutcome` 即可跑臂比較＋預註冊門檻（§7.3）。
- **邊界（誠實）**：①**注錯案例本身尚未撰寫**——五臂需要一組 injected_failure 案例集（fabrication/goal_drift/unverified_claim 各若干），屬 §7.2 實跑時人類 commit 的工作；②episode v1 限制沿襲 select-only：不掛 MCP、無 memory registry、Claude 原生 brain 需另備 anthropic_client（v1 走 OpenAI 相容供應商）——記錄於模組 docstring 與 README；③oracle 的 AST 檢查刻意保守（只查裸名呼叫）＝寧漏勿誤殺，涵蓋率靠 `expected_artifact_check` 規格補；④工具真的執行——案例務必只寫 agent 沙盒（artifact 相對路徑）、不碰 `data/`。
- **驗收**：ruff（改動檔）＋lint_ssot＋guard_all 39/39＋tool_eval 套件 72 passed（新增 test_gate_metrics 8 測＋test_oracle 11 測＋test_live_episode 5 測；`test_live_mode_placeholder` skip 佔位改為明確設定錯誤釘子）＋全套 pytest／vitest 無回歸。

## 相關

- [設計文件：對抗式互審](../proposals/adversarial-review-subsystem-design.md) §7（本 ADR＝§7.1 A/B/C；§7.2 五臂與 §7.3 門檻待 Phase 3 本體）
- [ADR-0134](0134-in-process-subagent-dispatch.md) — 子 loop 組裝（episode 重用）；[ADR-0135](0135-structured-report-finalize-and-board-bound-stop.md) — 回報塊（claims 的來源）
- [ADR-0122](0122-tool-event-telemetry-envelope.md) — 工具事件 wire（episode drain 的 join 依據）
- P0 量尺（pass^k／abstention）— 同套件既有；與 gate 指標分軸並存
