# ADR-0011: 脈衝串流等價性與 Schema 統一 (Pulse Streaming Parity & Schema Unification)

> **現況**：決策 #3「非串流 content 重組」現況已經不同，沒有對應的修訂 ADR（見下方註記）。

## 狀態
✅ 已接受

> 🔧 **決策 #3 現況已改（無對應修訂 ADR）**：原「非串流 JSON 重組優化會清空 content、僅 `stream=False` 且非多輪 ReAct 生效」→現行 `dispatcher.py` 無條件（`not is_streaming`）執行且 **populate** content、無 ReAct 多輪閘。決策 #1/#2（`stream` 欄位處理）仍有效。

## 日期
2026-05-04

## 上下文 (Context)
在實作 ReAct 循環的整合測試時，發現 `AnthropicRequest` 缺乏 `stream` 參數定義，導致 `Dispatcher` 誤判為非串流請求。這觸發了針對非串流回應的「JSON 重組優化」，該優化會清空 `content` 欄位以避免在 UI 重複顯示。這導致整合測試無法正確斷言工具調用的結果文本，且在實際應用中造成串流與非串流行為的不一致。

## 決策 (Decision)
1. **Schema 補全**：將 `stream: bool | None = False` 補齊至 `AnthropicRequest` Pydantic 模型中。
2. **串流優先原則**：確保全系統的所有對話端點（OpenAI 與 Anthropic）在處理 ReAct 多脈衝循環時，皆具備一致的串流旗標識別能力。
3. **優化邏輯保護**：保留非串流模式下的 JSON 重組優化，但僅在明確的 `stream=False` 且非多輪 ReAct 任務中生效，確保整合測試的觀察點完整性。

## 後果 (Consequences)
- **正面**：解決了 ReAct 整合測試無響應的 Bug，提升了系統的可測試性。
- **正面**：對外介面與 Anthropic 官方規範達成 100% 參數等價性。
- **負面**：需更新所有測試 Case 顯式宣告 `stream=True` 以獲得最佳觀察效果。
