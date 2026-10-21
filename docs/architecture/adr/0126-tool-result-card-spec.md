# ADR-0126: 工具結果卡片規範——結構化封套的人面渲染（Tool-Result Card Spec）

## 狀態

✅ **已接受（實作與驗證 2026-07-05、全綠——guard_all 38/38、前端 vitest 944 passed/184 檔；擁有者尚未 commit）**。本 ADR 完成「[工具可觀測性與可靠度](../proposals/tool-observability-and-reliability.md)」提案 **Phase 3（目標 2：人類面可視）**，延續工具系列（[ADR-0119](0119-unified-tool-interface-redesign.md)~[ADR-0125](0125-long-running-command-monitor.md)）。**消費** P1（[ADR-0122](0122-tool-event-telemetry-envelope.md)：`error_class`）與 P2a-2（[ADR-0124](0124-structured-success-and-applied-state-echo.md)：結構化成功＋`preview`）的封套。

## 日期

2026-07-05

## 背景

提案目標 2（人面可視）與現況缺口（§3）：「只有 bash 摺疊；跨工具結果卡片格式/顏色不統一」。P1/P2 已讓後端把工具結果結構化（`error_class`、結構化成功＋`preview`），但**前端 `ToolStepTracker` 未消費**——結構化 JSON 被當**原始文字**直接顯示（違反提案「模型面 JSON 不外顯」），且只有 success（藍）/error（紅）兩態、**無「拒絕黃」**（提案要成功綠/拒絕黃/錯誤紅）。

提案護欄：**易膨脹成大前端**——**定義成「一次卡片規範＋套用」，非開放式 UI 專案**。

## 決策

1. **純函式解析器**（[`lib/tool_result.ts::parseToolResult`](../../../ToukaChat/src/lib/tool_result.ts)）：把工具結果字串解析成判別聯集 `{kind: 'denied'|'error'|'success'|'raw', message?, suggestion?, preview?, errorClass?, raw}`——消費 P1 的 `error_class`（`permission_denied` → `denied`、其餘 → `error`）＋P2a-2 的結構化成功（`ok:true` → `success`＋`preview`）；容忍尾端附註（先完整 parse、失敗抓第一個平衡物件）；非結構化（bash markdown／legacy）→ `raw`。純函式、可單測。
2. **`ToolStepTracker` 消費 parsed**（[元件](../../../ToukaChat/src/components/chat/ToolStepTracker.tsx)）：
   - **三態配色**（走 `text-status-*` token，過主題守衛）：`denied`＝黃（`ShieldAlert`／`text-status-warning`）、`error`＝紅（`AlertCircle`／`text-status-error`）、`success`＝綠/藍（`CheckCircle2`／`text-status-success`）、`running`＝轉圈。
   - **展開面板渲染人話**：結構化 payload → `message`＋建議（`suggestion_label`）＋`preview`（等寬區塊），**不外顯原始 JSON**；`raw` → 維持原樣純文字（bash markdown 等）。
   - 維持既有**摺疊 button ＋ `max-h-64 overflow-y-auto`**（限高不塞滿畫面）＝一套卡片規範套**全工具類型**（非只 bash）。
3. **i18n**：新增 `chat.tool_step_tracker.suggestion_label` 雙語（zh-TW／en，過 Guard #33）。

## 後果

- **跨工具卡片一致**：任何工具的 `tool_result` 都經同一卡片規範渲染（denied 黃/error 紅/success 綠）；結構化封套渲染成人話——**「模型面 JSON 不外顯」落地**。
- **完成 Phase 3（目標 2 人面）**：三目標（模型面 P2／人面 P3／量測 P0）的可交付部分收口，只剩 P4（選配）。
- **驗收**：ESLint＋`tsc`（Guard #27）＋前端 vitest **944 passed/184 檔**（+10）＋Guard #5 主題／#33 i18n／guard_all 38/38。後端零改動（純渲染層）。
- **範圍控制到位**：`parseToolResult` 從 `result` 字串**自解析**、不動 `stream_tree`／`ToolStep`／後端＝「一次卡片規範＋套用」，未膨脹成大前端。
- **刻意未做**：消費 `duration_ms`（P1 additive 事件屬性，不在 `result` 字串內，需 thread `stream_tree`→ToolStep；為守範圍留待未來）；running 中間態（`{"status":"running",…}`）的專屬渲染（目前 `raw`）；bash markdown `<details>` 與卡片的完全合流（另案）。

## 相關

- [ADR-0122](0122-tool-event-telemetry-envelope.md) — P1 遙測封套（`error_class` 來源；本 ADR 為其人面消費端）
- [ADR-0124](0124-structured-success-and-applied-state-echo.md) — P2a-2 結構化成功＋`preview`（本 ADR 人面渲染）
- [ADR-0119](0119-unified-tool-interface-redesign.md) — Tier 1 結構化錯誤（`error_class` 五類）
- [ADR-0083](0083-toukachat-wcag-22-aa-baseline.md) — 卡片沿用既有 WCAG 可達性（button 語意／aria-expanded）
- Codex CLI-renderer 感知格式（人面渲染與模型面結構化分離）——借鑑
- [工具可觀測性與可靠度提案](../proposals/tool-observability-and-reliability.md) §6 Phase 3
- 後續（另案）：**P4 選配**（真 OTel exporter／逐腦 emphasis 調校／快取前綴優化）
