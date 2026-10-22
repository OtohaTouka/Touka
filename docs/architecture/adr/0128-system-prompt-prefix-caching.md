# ADR-0128: System 前綴快取（System-Prompt Prefix Caching）

## 狀態

✅ **已接受（實作與驗證 2026-07-05、全綠——guard_all 38/38、後端 pytest 2703 passed/4 skipped）**（實作位置修訂 by [ADR-0156](0156-react-core-extraction.md)：快取套用點自 claude_handler 抽取至 claude_adapter，wire 職責；快取行為與斷點位置不變）。本 ADR 是 [ADR-0127](0127-prompt-cache-prefix-optimization.md)（P4 快取前綴）的**延續**——把 Anthropic prompt caching 從工具 schema 推廣到 **system prompt**。延續工具系列（[ADR-0119](0119-unified-tool-interface-redesign.md)~[ADR-0127](0127-prompt-cache-prefix-optimization.md)）。

## 日期

2026-07-05

## 背景

[ADR-0127](0127-prompt-cache-prefix-optimization.md) 已快取最大穩定塊（工具 schema ≈6346 token），但 **system prompt**（persona／capabilities／各 context 區塊）仍未快取。Anthropic prompt caching 需 `system` 為 **block list**（現況是中介層以字串串接組裝，`inject_anthropic_system` 逐段 `+=`）。

關鍵觀察：**system prompt 在 ReAct 迴圈內穩定**——每個使用者回合組裝一次，迴圈內多次 `messages.create` 重複送出同一份 system。快取整個 system block → 迴圈內重複命中（跨回合則因 mission 進度／時間波動而重快取，但 ADR-0127 的工具斷點仍跨回合命中）。

## 決策

1. **純函式**（[`tool_schema.to_cached_system`](../../../ToukaProxy/src/adapters/tool_schema.py)）：`str` → 單一 text block ＋ `cache_control: {type: ephemeral}`；已是 `list` → 對最後一個 text block 加斷點（冪等：先清各 block 既有 `cache_control`）；空字串／None → 原樣。純函式、不改輸入。
2. **套用點**（[`claude_handler`](../../../ToukaProxy/src/handlers/claude_handler.py)）：在 ReAct 迴圈**之前**（所有 system 字串附加——含降級警告——之後）把 `kwargs["system"]` 轉為 cached block list。**Anthropic-only 路徑**。
3. **遙測零改動**：[`context_breakdown._system_to_text`](../../../ToukaProxy/src/services/context_breakdown.py) 早已能攤平 block list（抽 `b["text"]`、忽略 `cache_control`），故 `compute_context_breakdown`／ContextMeter 不受影響——round-trip 測試鎖定（str 與 cached-list 分解相同）。

## 後果

- **system 前綴於 ReAct 迴圈重複命中**→快取讀取省 token／加速首 token；配合 ADR-0127 的工具斷點，**tools + system 前綴皆快取**（tools 跨回合穩定命中、system 迴圈內命中）。
- **驗收**：ruff（改動檔）＋lint_ssot 全清、**guard_all 38/38**、**pytest 2703 passed/4 skipped**（+5；`to_cached_system` 四測＋breakdown 相容 round-trip）。claude_handler 的 system-list 化零回歸（既有 handler／emit 測試全綠）。
- **範圍/邊界（刻意）**：快取**整個 system block**（迴圈內穩定即命中）——**未細分**「穩定前綴（persona）vs 波動後注入（mission/time/`<system-reminder>`）」，那需中介層標記穩定/波動邊界、較侵入，**列為另案**（可讓 persona 跨回合也命中）。跨回合 system 波動則重快取（可接受——迴圈是重複呼叫的主場）。
- **不做**：把波動內容移出 system 改走訊息晚注入（Claude Code 式，另案）；非 Anthropic 供應商（各有自動快取）。

## 相關

- [ADR-0127](0127-prompt-cache-prefix-optimization.md) — P4 工具 schema 快取（同一優化的姊妹；本 ADR 把斷點推廣到 system）
- [ADR-0092](0092-context-window-token-sliding-window.md) — `_system_to_text`／context breakdown（早已相容 block list，故遙測免改）
- Claude Code 快取經濟（穩定快取前綴）——借鑑
- [工具可觀測性與可靠度提案](../proposals/tool-observability-and-reliability.md) §6 Phase 4
- 後續（選配、另案）：穩定前綴 vs 波動後注入之細分、逐腦 emphasis 調校、OTel exporter
