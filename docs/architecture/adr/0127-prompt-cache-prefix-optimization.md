# ADR-0127: Prompt 快取前綴優化——工具 schema 快取（Prompt Cache Prefix Optimization）

## 狀態

✅ **已接受（實作與驗證 2026-07-05、全綠——guard_all 38/38、後端 pytest 2698 passed/4 skipped；擁有者尚未 commit）**（實作位置修訂 by [ADR-0156](0156-react-core-extraction.md)：快取套用點自 claude_handler 抽取至 claude_adapter，wire 職責；快取行為與斷點位置不變）。本 ADR 落地「[工具可觀測性與可靠度](../proposals/tool-observability-and-reliability.md)」提案 **Phase 4（選配延伸）的快取前綴一項**（擁有者三選一裁示：做快取前綴）。延續工具系列（[ADR-0119](0119-unified-tool-interface-redesign.md)~[ADR-0126](0126-tool-result-card-spec.md)）。

## 日期

2026-07-05

## 背景

提案 P4「before-call 快取前綴優化（穩定前綴 vs `<system-reminder>` 波動後注入，Claude Code 快取經濟）」。P0 已量到：**工具 schema 是最大的穩定 token 塊**（Owner 廣告 27 工具 ≈ **6346 token**）。現況 Anthropic prompt caching 的 `cache_control` 只加在**訊息**（tool_results，見 `claude_handler`），**tools／system 前綴完全未快取**——每次 API 呼叫都重付整組工具 schema 的 token。

Anthropic prompt caching 的快取順序＝**tools → system → messages**；在某段尾端設 `cache_control` 斷點即快取其前綴。工具 schema 穩定（同一會話廣告集不變）、且在 **ReAct 迴圈**內每次 `messages.create` 重複送出——是快取的理想對象。

## 決策

1. **純函式斷點器**（[`tool_schema.apply_tool_cache_breakpoint`](../../../ToukaProxy/src/adapters/tool_schema.py)）：在**最後一個工具**加 `cache_control: {type: ephemeral}`——快取整組工具 schema 前綴。**冪等**（先清各工具既有 `cache_control`、只標最後一個，避免超過斷點上限）、純函式（複製、不改輸入）、空/None 原樣。
2. **套用點**（[`claude_handler`](../../../ToukaProxy/src/handlers/claude_handler.py)）：`request.tools` 出場轉 Anthropic 後、設上 `kwargs["tools"]` 前套用。**Anthropic-only 路徑**（其他供應商各有自動快取、不走此碼）；**無 config flag**——快取對 Touka 的 ReAct-heavy 使用是淨效益，且 Anthropic 對過小/不支援會 graceful 忽略。

## 後果

- **ReAct 迴圈省 token／加速**：同一組工具（≈6346 token）於迴圈內每次 `messages.create` 重複命中→**快取讀取省約 90% token 成本**、加速首 token；跨會話同工具集也命中。首次寫入 +25%（攤銷於重複命中）。
- **落地提案 P4 的一項**（擁有者三選一）：其餘 P4 子項維持選配/另案（見下）。
- **驗收**：ruff（改動檔）＋lint_ssot 全清、**guard_all 38/38**、**pytest 2698 passed/4 skipped**（+4；純函式斷點器四測，claude_handler 含新快取路徑零回歸）。
- **範圍/邊界（刻意）**：
  - 只快取 **tools**（最大、最穩定、最乾淨的一塊，斷點獨立於 system 波動）。
  - **system 前綴快取**需把 `system` 由串接字串改成 block list＋處理 degraded 警告附加與遙測讀取，較侵入——**列為後續另案**（迴圈內 system 也穩定、有進一步空間）。
  - **逐腦 emphasis 調校**（P4 另一子項）需 LIVE eval、探索性——未選。
  - **真 OTel exporter/spans**（P4 另一子項）需新增 SDK 依賴＋把遙測外送，**與 Touka 本地優先／隱私／開源定位有張力**——擁有者未選、不做。

## 相關

- [ADR-0122](0122-tool-event-telemetry-envelope.md) — P1 統一封套（本 ADR 屬同提案 P4；OTel 命名對齊但不引 SDK）
- [ADR-0092](0092-context-window-token-sliding-window.md) — token 估算器（P0 據以量到工具 schema ≈6346 token）
- Claude Code 快取經濟（穩定快取前綴）——借鑑來源
- [工具可觀測性與可靠度提案](../proposals/tool-observability-and-reliability.md) §6 Phase 4
- 後續（選配、另案）：system 前綴快取、逐腦 emphasis 調校、OTel exporter
