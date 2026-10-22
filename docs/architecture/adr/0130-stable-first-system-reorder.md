# ADR-0130: 中介層順序重排——穩定工具區塊進跨回合快取（Stable-First System Reorder）

## 狀態

✅ **已接受（實作與驗證 2026-07-05、全綠——guard_all 38/38、後端 pytest 2715 passed/4 skipped）**（實作位置修訂 by [ADR-0156](0156-react-core-extraction.md)：快取套用點自 claude_handler 抽取至 claude_adapter，wire 職責；快取行為與斷點位置不變）。本 ADR 補上 [ADR-0129](0129-stable-prefix-split-caching.md)「範圍/邊界」明列的後續：把排在波動區塊之後的**穩定工具區塊**也納入跨回合快取前綴。延續工具系列（[ADR-0119](0119-unified-tool-interface-redesign.md)~[ADR-0129](0129-stable-prefix-split-caching.md)），為提案 P4「快取前綴」的收尾。

## 日期

2026-07-05

## 背景

[ADR-0129](0129-stable-prefix-split-caching.md) 於「最早出現的波動區塊」處切分 system，讓穩定前綴跨回合快取。但實際組裝順序（soul 路徑）為：

```
<persona>              ← 穩定
<conversation_summary> ← 波動（SoulLayer 注入；僅剪過的長對話有）
<tool_availability>    ← 穩定（會話級）
<validation_protocol>  ← 穩定（審批級）
<mission_awareness>    ← 波動（ToolLayer 注入）
```

長對話一旦有 `conversation_summary`，它夾在 persona 與穩定工具區塊之間 → 切點落在 persona 之後，**穩定的 `tool_availability`／`validation_protocol` 被困在波動後綴**，每回合重新計費、不跨回合命中。根因是波動區塊**跨兩層注入**（`conversation_summary` 於 SoulLayer、`mission_awareness` 於 ToolLayer），無法靠重排 pipeline 層順序修正——SoulLayer 須早於 ToolLayer 以備妥身份／腦／`desired_tools`（層順序是承重的）。

## 決策

1. **送出前正規化**（[`tool_schema.reorder_system_stable_first(system, *, volatile_tags)`](../../../ToukaProxy/src/adapters/tool_schema.py)）：把所有波動區塊（`<tag>…</tag>`）**逐字搬到 system 尾端**（保持各區塊位元不變、保持同組出現順序），使穩定區塊連成單一前綴。**冪等**；波動區塊已在尾端（如短對話僅 `mission_awareness`）→ 原樣傳回（no-op，避免無謂位元變動）。
2. **接線**（[`claude_handler`](../../../ToukaProxy/src/handlers/claude_handler.py)）：於 [ADR-0128](0128-system-prompt-prefix-caching.md)/[ADR-0129](0129-stable-prefix-split-caching.md) 的 `to_cached_system` **之前**呼叫重排——`reorder → split`。切點因而落在「所有穩定區塊之後」，穩定前綴＝`persona ＋ tool_availability ＋ validation_protocol`，跨回合命中。
3. **不動 pipeline 層順序**：只在組裝完成的**單一出場點**正規化區塊順序（唯一能看到「完整組裝後 system」的位置）。**層界乾淨**（Guard #21）：`volatile_tags` 由 `claude_handler` 傳入，`tool_schema`（adapters）**不 import services**。

## 後果

- **穩定工具區塊跨回合快取**：長對話（有 `conversation_summary`）中，過去被困的 `tool_availability`／`validation_protocol`（存在時 ~200–400 token）改為跨回合命中。大宗（persona）已由 [ADR-0129](0129-stable-prefix-split-caching.md) 交付，本 ADR 補上其明列的缺口。
- **範圍/邊界（誠實）**：僅在 `conversation_summary` 存在（剪過的長對話）**且**工具區塊存在時才有增益；短對話 `mission_awareness` 本已在尾端＝no-op。屬邊際但真實的優化，非熱路徑巨幅節省。
- **遙測相容**：重排只搬整塊；`context_breakdown` 逐 tag 解析（與位置無關）→ 各命名區塊分類與 token 計數不變，僅空白殘留（`system_other`）差 ~0 token。
- **假設**：波動區塊為頂層 XML 邊界、不巢狀（與 `context_breakdown` 解析假設、`wrap_block` 不巢狀不變式一致）。
- **驗收**：ruff（改動檔）＋lint_ssot 全清、**guard_all 38/38**（含 #21 模組邊界）、**pytest 2715 passed/4 skipped**（+7；重排六測＋breakdown 相容一測）。
- **剩餘**：提案 P4 只餘**逐腦 emphasis 調校**與 **OTel exporter**（皆選配、維護者未選；後者有本地優先/隱私張力）。快取前綴四層（工具＋persona＋穩定工具區塊＋波動後綴）至此收尾。

## 相關

- [ADR-0129](0129-stable-prefix-split-caching.md) — 穩定前綴細分（本 ADR 讓其切點落在所有穩定區塊之後）；[ADR-0128](0128-system-prompt-prefix-caching.md)／[ADR-0127](0127-prompt-cache-prefix-optimization.md) — system／工具 schema 快取（同疊加）
- [ADR-0092](0092-context-window-token-sliding-window.md) — `ALL_BLOCK_TAGS` tag SSOT／`_system_to_text` 多 block 相容（逐 tag 解析與順序無關）
- 中介層 [ContextLayer](../../../ToukaProxy/src/middleware/layers/context.py)／[SoulLayer](../../../ToukaProxy/src/middleware/layers/soul.py)／[ToolLayer](../../../ToukaProxy/src/middleware/layers/tool.py) 注入順序（承重、不重排）
- [工具可觀測性與可靠度提案](../proposals/tool-observability-and-reliability.md) §6 Phase 4
- 後續（選配、另案）：逐腦 emphasis 調校、OTel exporter
