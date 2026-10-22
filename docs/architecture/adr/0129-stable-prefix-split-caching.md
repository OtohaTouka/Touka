# ADR-0129: 穩定前綴細分——persona 跨回合快取（Stable-Prefix Split Caching）

## 狀態

✅ **已接受（實作與驗證 2026-07-05、全綠——guard_all 38/38、後端 pytest 2708 passed/4 skipped）**（實作位置修訂 by [ADR-0156](0156-react-core-extraction.md)：快取套用點自 claude_handler 抽取至 claude_adapter，wire 職責；快取行為與斷點位置不變。註：Guard #21 層界引用為自始不準的作者疏漏、非事後漂移，不在本註記範圍）。本 ADR 是 [ADR-0128](0128-system-prompt-prefix-caching.md)（system 前綴快取）的**細分深化**——把穩定前綴（persona 等）與波動後綴（mission/summary）分開快取，讓穩定前綴**跨回合**命中。延續工具系列（[ADR-0119](0119-unified-tool-interface-redesign.md)~[ADR-0128](0128-system-prompt-prefix-caching.md)）。

## 日期

2026-07-05

## 背景

[ADR-0128](0128-system-prompt-prefix-caching.md) 把整個 system prompt 當**一個**快取 block——迴圈內命中，但**跨回合不命中**：mission 進度、被剪對話摘要每回合都可能變，整塊 hash 一變即失效。而 system 內最大的穩定部分是 **persona**（人格/soul 本體，長且會話內不變）；若能單獨快取，長對話的每個回合都能省下 persona 的 token。

## 決策

1. **波動區塊 SSOT**（[`prompt_blocks.VOLATILE_SYSTEM_TAGS`](../../../ToukaProxy/src/services/prompt_blocks.py)）＝ `(MISSION_AWARENESS, CONVERSATION_SUMMARY)`——每回合會變動的區塊；只有排在「最早一個波動區塊」**之前**的內容（persona 等）才可跨回合快取。
2. **細分切點**（[`tool_schema.to_cached_system(system, *, volatile_tags)`](../../../ToukaProxy/src/adapters/tool_schema.py)）：於 system 字串中**最早出現的波動區塊 `<tag>`** 處切成兩個帶斷點的 block——`[穩定前綴(斷點), 波動後綴(斷點)]`。**兩斷點疊加**、Anthropic 取最長匹配前綴：
   - 跨回合（穩定同、波動變）→ 穩定前綴斷點命中（省 persona）；
   - 迴圈內（皆同）→ 波動後綴斷點命中（省整個 system）。
   穩定前綴為空白／無波動 tag／未給 `volatile_tags` → **單一區塊**（整段一斷點，向後相容 [ADR-0128](0128-system-prompt-prefix-caching.md)）。
3. **層界乾淨**（Guard #21）：`volatile_tags` 由 `claude_handler`（handler 層可依賴 services）傳入 `to_cached_system`；`tool_schema`（adapters 層）**不 import services**，維持無業務依賴。

## 後果

- **三層快取斷點疊加**：tools（[ADR-0127](0127-prompt-cache-prefix-optimization.md)，跨回合穩定）＋system 穩定前綴（本 ADR，跨回合命中 persona）＋system 波動後綴（[ADR-0128](0128-system-prompt-prefix-caching.md)，迴圈內命中整段）。長對話每回合省下 tools + persona 的重複 token。
- **驗收**：ruff（改動檔）＋lint_ssot 全清、**guard_all 38/38**（含 #21 模組邊界）、**pytest 2708 passed/4 skipped**（+5；切分四測＋breakdown 相容）。
- **邊界/踩雷**：`context_breakdown._system_to_text` 攤平多 block 時以 `\n` 連接，切成兩塊後可能多出一個**可忽略的 `system_other`**（~0 token）——但 **API 端 Anthropic 直接串接 block（無分隔符）＝送出文字完全正確**，僅遙測估算差一個換行；故 breakdown 主要分類（persona／mission_awareness）不變、總量近似（測試斷言據此放寬）。
- **範圍/邊界（刻意）**：只切一刀（穩定前綴 vs 最早波動之後的全部）；波動後綴內若混有穩定區塊（如 tool_availability 排在 conversation_summary 之後）仍歸波動側（不跨回合快取）——要進一步需讓所有穩定區塊在前。**已由 [ADR-0130](0130-stable-first-system-reorder.md) 補上**：以送出前正規化（`reorder_system_stable_first`）把波動區塊搬到尾端，**不動 pipeline 層順序**，使穩定工具區塊也進跨回合前綴。
- **剩餘**：提案 P4 只餘**逐腦 emphasis 調校**與 **OTel exporter**（皆選配、維護者未選；後者有本地優先/隱私張力）。

## 相關

- [ADR-0128](0128-system-prompt-prefix-caching.md) — system 前綴快取（本 ADR 細分它）；[ADR-0127](0127-prompt-cache-prefix-optimization.md) — 工具 schema 快取（同疊加）
- [ADR-0092](0092-context-window-token-sliding-window.md) — `ALL_BLOCK_TAGS` tag SSOT（`VOLATILE_SYSTEM_TAGS` 取其子集）／`_system_to_text` 多 block 相容
- Claude Code 快取經濟（穩定快取前綴 vs 波動後注入）——借鑑
- [工具可觀測性與可靠度提案](../proposals/tool-observability-and-reliability.md) §6 Phase 4
- 後續（選配、另案）：重排中介層注入順序以擴大穩定前綴、逐腦 emphasis 調校、OTel exporter
