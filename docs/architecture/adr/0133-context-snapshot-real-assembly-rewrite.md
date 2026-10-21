# ADR-0133: Context Snapshot 真實組裝重寫——預先檢視組裝完成的系統提示（Context Snapshot: Real-Assembly Rewrite）

## 狀態

✅ **已接受（實作與驗證 2026-07-05、全綠——guard_all 39/39、後端 pytest 2734 passed/4 skipped、前端 vitest 971；擁有者尚未 commit）**。補強既有的 `context-snapshot` 診斷功能：去硬編碼、如實反映現況組裝、接上 DebugAgent 前端、六塊接真實/mock、加四項可觀測性加值。

## 日期

2026-07-05

## 背景

`context-snapshot` 端點（[ADR-0104](0104-debugagent-destructive-reset.md) 時期的診斷工具）原以**硬編碼 8 塊**列出組裝順序，且：①已 **stale**——早於 `conversation_summary`（[ADR-0092](0092-context-window-token-sliding-window.md) Phase D）、`tool_availability`（Tier 1）、`validation_protocol`（工具可觀測性 P2a），三塊未列，並把 `mission_awareness` 誤標為 ContextLayer 注入（實為 ToolLayer）；②8 塊中只有 soul＋skill 有真內容，其餘一律 `present=false`；③只有 API、前端無介面。擁有者要求補強四點：**(1) 符合現狀且避免硬編碼**（日後系統改動也如實呈現）、**(2) 接上 DebugAgent 前端**、**(3) 六個 runtime 塊接虛擬或真實資料**、**(4) 其他改進**。

## 決策

1. **真實組裝 harness（Option B，擁有者裁示）**：[`context_snapshot_service.get_context_snapshot(identity, base_ctx)`](../../../ToukaProxy/src/services/context_snapshot_service.py) 改為 async——以合成 DebugAgent 請求跑**真實 `ProxyPipeline`**（`protocol_manager` 沿用 `app.state.context`、MCP `get_all_tools` 唯讀、**永不呼叫模型**、無 session_id 故不碰 session 狀態），讀出組裝後的 `request.system`／`tools`／`messages`。順序與內容全由真實組裝產生＝**零硬編碼**、自動反映未來中介層變更（復用 Scorecard `make_real_runner` 同款組裝基礎設施）。
2. **tag SSOT 有序解析**：[`parse_system_blocks`](../../../ToukaProxy/src/services/context_snapshot_service.py) 迭代 `prompt_blocks.ALL_BLOCK_TAGS`、依**實際出現位置**排序——不依賴硬編碼區塊清單。唯一 per-block 知識是 `BLOCK_META`（tag → 注入層／來源／kind／mock 示例）。
3. **六個 runtime 塊接真實優先（#3）**：Mission（`MissionManager`）、Tool Schemas（`request.tools`）、Home Messages（`request.messages`）**皆真實**；Project 選定專案時真實、否則 mock；IDE 與 Technical Specs 本質 per-request → mock 格式示例。未出現的已知 tag 一律以 `present=false`＋mock 示例佔位（誠實、且涵蓋＝`ALL_BLOCK_TAGS`）。
4. **四項加值（#4）**：①每塊 token＋總量＋%window（reuse `estimate_tokens`／`get_context_window`）；②**快取分層標註**——套**真實 P4 函式**（[`reorder_system_stable_first`](../../../ToukaProxy/src/adapters/tool_schema.py)＋`to_cached_system`）判定 `stable`（跨回合）/`volatile`（迴圈內）＋`cache_reordered` 旗標＝把 [ADR-0130](0130-stable-first-system-reorder.md) 成效可視化＋當快取健康檢查；③**統一 last-payload**——讀 `proxy_payload.log` 最近一筆 DebugAgent payload，static 預覽 vs 實際送出對照；④複製完整 `system_text`。
5. **防漂移 Guard #39**：[`guard_context_snapshot.py`](../../../scripts/guard_context_snapshot.py) 以 AST 比對 `ALL_BLOCK_TAGS ⊆ BLOCK_META` keys——日後新增 block tag 卻忘補 metadata 即 FAIL，**根治「悄悄變 stale」**（正是本次補強動機）。＋單元測試同義。
6. **前端（#2）**：[`DebugMonitoringView`](../../../ToukaChat/src/components/sidebar/DebugMonitoringView.tsx) 加第 4 個「Context」分頁；[`ContextSnapshotPanel`](../../../ToukaChat/src/components/sidebar/ContextSnapshotPanel.tsx)（區塊清單／注入層・kind・快取分層徽章／token／可展開預覽／匯出 .md／複製／last-payload 對照）；[`useContextSnapshot`](../../../ToukaChat/src/hooks/useContextSnapshot.ts) hook；i18n zh/en（Guard #33）。

## 後果

- **如實且自我維護**：快照就是真實 pipeline 組裝的解析，沒有東西要手動同步；Guard #39 再從 CI 層釘住覆蓋。Owner/Debugger 可在 Debug Center 或經 API/匯出**預先檢視**組裝完成的系統提示（純唯讀、不打模型、不花費 API）。
- **副作用審計**：pipeline dry-run 為唯讀——MonitorLayer 的 `clear_interrupt` 受 session_id 保護（快照不設）、Soul/Tool/Context 層皆 DB 讀取、MCP `get_all_tools` 只列已連線者、無模型呼叫、無 session 變更。
- **邊界（誠實）**：Technical Specifications（`apply_soul_to_request` 的關鍵字啟發式吸收，巢狀於 persona）本質 per-request → 佔位示例；`capabilities` 僅 L2 自定義模型路徑注入 → DebugAgent 快照顯示為 absent（皆誠實標註）。
- **守衛 38→39**；`context-snapshot` 端點新增 `include_last_payload` 參數（openapi.json／api.ts 重生）。
- **驗收**：ruff（改動檔）＋lint_ssot＋ESLint（改動檔）全清、tsc 改動檔零錯（既有測試檔 pre-existing 型別問題不在範圍）、**guard_all 39/39**、**後端 pytest 2734 passed/4 skipped**（+19）、**前端 vitest 971**、i18n parity（Guard #33）、防漂移 Guard #39。

## 相關

- [ADR-0092](0092-context-window-token-sliding-window.md) — `ALL_BLOCK_TAGS` tag SSOT／`_system_to_text`（快照解析與遙測同源）
- [ADR-0130](0130-stable-first-system-reorder.md)／[ADR-0128](0128-system-prompt-prefix-caching.md)／[ADR-0127](0127-prompt-cache-prefix-optimization.md) — P4 快取函式（快照的快取分層標註直接復用）
- [ADR-0104](0104-debugagent-destructive-reset.md) — 端點 IDOR 歸屬範圍（沿用未動）；Scorecard `make_real_runner` — 生產同源組裝基礎設施（復用）
- 中介層 [ContextLayer](../../../ToukaProxy/src/middleware/layers/context.py)／[SoulLayer](../../../ToukaProxy/src/middleware/layers/soul.py)／[ToolLayer](../../../ToukaProxy/src/middleware/layers/tool.py) — 真實注入來源
