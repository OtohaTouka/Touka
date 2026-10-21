# ADR-0100: 工具 schema 單一 canonical（OpenAI）＋ 只在出場轉換

**狀態**：✅ 已接受（實作位置修訂 by [ADR-0156](0156-react-core-extraction.md)：claude 出場工具 schema 轉換移至 claude_adapter；canonical/出場轉一次的決策不變）
**日期**：2026-06-18
**影響範圍**：ToukaProxy（adapters／middleware/layers/tool／handlers／dispatchers）
**關鍵字**：tool schema, function calling, canonical 格式, SSOT, OpenAI pivot, ToolLayer, claude_handler, convert_to_openai, 消除來回, DeepSeek 工具, 建基於 ADR-0099

---

## 背景

工具（function calling）schema 的處理原本有兩個問題：

1. **轉換邏輯散落**：「OpenAI→Anthropic 工具 schema」轉換**重複寫在兩處**（ToolLayer `_inject_tools` 與 `adapters/openai_to_claude`），而 dispatch 的「Anthropic→OpenAI」那塊**根本缺一份**。
2. **多餘來回**：工具原始定義為 OpenAI 形狀（`tools/builtin/schemas.py`），但進場是 Anthropic → ToolLayer 先把工具轉成 Anthropic 塞進 `request.tools`；送 Gemini/DeepSeek 時 `convert_to_openai` 本該轉回 OpenAI——卻寫死 `tools=None`。形成 **OpenAI→Anthropic→OpenAI 來回**，且漏掉的最後一步造成 **DeepSeek/Gemini/OpenAI 相容模型收不到工具、無法調用**（計分卡 `tool_memory_recall` 揭露）。

訊息的內部樞紐已是 OpenAI（adapters 全部對 OpenAI 轉換，無 Claude↔Gemini 直達），唯獨工具走了相反的「先轉 Anthropic」路徑，與樞紐不一致。

## 決策

**工具 schema 的 canonical 內部格式 ＝ OpenAI**；`request.tools` 全程維持 OpenAI 形狀，**只在出場各轉一次**：

1. **單一轉換器 SSOT**：新增 `adapters/tool_schema.py`（`openai_to_anthropic_tools`、`anthropic_to_openai_tools`），所有工具格式轉換只此一處；ToolLayer 與 `openai_to_claude` 改為共用它。`openai_to_anthropic_tools` 設計為**冪等**（輸入已是 Anthropic 則直通）。
2. **ToolLayer 注入 canonical OpenAI**：不再依進場格式轉成 Anthropic；客戶端若送 Anthropic 工具於 ingress 經 `anthropic_to_openai_tools` 正規化為 OpenAI。
3. **出場才轉一次**：
   - **Claude**：`claude_handler` 出場以 `openai_to_anthropic_tools` 轉 Anthropic（唯一必要的一次轉換）。
   - **Gemini**：`convert_to_openai`（工具直通 OpenAI）→ `openai_to_gemini`（既有 OpenAI→Gemini）。
   - **OpenAI 相容**（DeepSeek/OpenAI/Grok）：OpenAI 直通。
4. 消除 OpenAI→Anthropic→OpenAI 來回；非 Claude 路徑零來回，Claude 只在出場轉一次。

分階段落地（每階段全綠）：階段0 集中轉換器（零行為變更）；階段1 `convert_to_openai` 接上轉換器（修好 DeepSeek/Gemini/OpenAI 工具，仍保留來回）；階段2 ToolLayer 改 canonical OpenAI ＋ claude_handler 出場轉換（消除來回）；階段3 收尾、anti-drift 測試、本 ADR。

## 後果

- ✅ DeepSeek/Gemini/OpenAI 相容模型可正常調用工具（修好 pre-existing bug）。
- ✅ 工具轉換邏輯單一事實來源；新增供應商只需在 `tool_schema` 加一向轉換。
- ✅ 與訊息樞紐（OpenAI）一致；消除多餘來回。
- ⚠️ **動到 Claude 主路徑**：Claude 工具改於出場轉換（先前直接拿 Anthropic）。以冪等轉換器＋完整回歸（claude_handler 測試、含工具的對話、ReAct、記憶幻覺偵測）＋對抗式複審把關。
- 範圍：僅「請求的工具定義」；tool_calls 回應轉換（`claude_to_openai`／`gemini_to_openai`）不變。

建基於 [ADR-0099](0099-provider-registry-ssot.md)（Provider Registry SSOT）。
