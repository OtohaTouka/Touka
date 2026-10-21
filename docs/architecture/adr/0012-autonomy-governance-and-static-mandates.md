# ADR-0012: 自主預算治理、API 合約同步與全域守則執法 (Autonomy Governance, API Sync & Mandate Enforcement)

> **現況**：nudge 續圈移除、slots 改成 50 工具輪保險絲（ADR-0146 反轉）。

## 狀態
✅ 已接受

> 🗑️ **兩處決策已被 [ADR-0146](0146-tool-round-budget.md)（自主語意翻轉）反轉**：①「預算 0 觸發 Nudge 續圈」整段移除——純文字回合直接結束、配額耗盡走 `WRAP_UP_NOTE`+`tool_choice=none` 收尾；②「System Fallback 5 slots」→ `HEADLESS_FALLBACK_ROUNDS=50`，鍵改名 `default_max_slots`→`max_tool_rounds`、語意由「slots」改為「工具輪保險絲」。API sync / time / env 靜態守則（決策 2–4）仍有效。

## 日期
2026-05-05

## 上下文 (Context)
隨著 Touka 系統的複雜度增加，手動維護 API 合約與確保開發者遵循「時間主權」與「環境變數主權」變得日益困難。此外，自主代理人 (Agent) 的預算決定機制過於分散，缺乏統一的優先級鏈，且在 ReAct 循環中存在預算耗盡後無法優雅終止的風險。

## 決策 (Decision)
1.  **自主預算大一統 (Unified Autonomy Budget)**：
    *   實施優先級決策鏈：`Request Specified > Global Config (autonomy.json) > System Fallback (5 slots)`。
    *   在 `RhythmService` 中集中管理 Slots，並確保 `ReAct` 循環在預算為 0 時強制攔截並觸發 Nudge。
2.  **API 合約即代碼 (Contract as Code)**：
    *   實施 `test_api_schema_sync.py`，確保 Pydantic 模型定義與物理 `openapi.json` 檔案達成 100% 同步。
    *   提供 `update_openapi.py` 工具，將 Schema 更新納入 CI/CD 與開發工作流。
3.  **時間主權禁令 (Time SSOT Enforcement)**：
    *   在 `lint_ssot.py` 中新增規則，嚴禁直接調用 `datetime.now()` 或 `time.time()`。
    *   強制全系統時間獲取操作錨定於 `src.utils.time_utils`。
4.  **環境變數禁令 (Env SSOT Enforcement)**：
    *   在 `lint_ssot.py` 中新增規則，嚴禁在業務邏輯中直接存取 `os.environ` 或 `os.getenv`。
    *   強制所有外部配置必須透過 `src.config` 的 Pydantic 驗證層。

## 後果 (Consequences)
- **正面**：消除了代碼中隱藏的「環境與時間地雷」，確保系統在不同時區與環境下的行為強一致。
- **正面**：API 合約自動化驗證降低了前端與代理人通訊失敗的風險。
- **正面**：自主預算的透明化與強制執行防止了 Agent 陷入無限循環消耗資源。
- **負面**：開發者需要適應新的「禁令」，在寫代碼時必須使用系統提供的適配器而非原生 API。
- **負面**：測試腳本需要為 `time_utils` 與 `config` 提供更精細的 Mock 支援。
