# ADR-0266：Claude 出場剝除工具定義根層組合子

- 狀態：✅ 已接受（維護者 2026-10-15 核准計畫）〔ADR-0285 修訂決策 1 與已知限制第 4 條：分支之間同名欄位的定義都保留，多種合成 `anyOf`〕
- 日期：2026-10-15
- 決策者：維護者
- 影響範圍：ToukaProxy（`adapters/tool_schema.py` 的 `openai_to_anthropic_tools`；`tools/project_tools.py` 只改註解）；tests（`tests/unit/test_tool_schema_hygiene.py` 改寫 1 個、新增 `tests/unit/test_provider_tool_schema_egress.py`）
- 相關：本 ADR 修訂 ADR-0119 Tier 2 的「Anthropic 原樣保留」一句（送給 Claude 改為只剝根層組合子；條件必填 oneOf 的正本與 Gemini 剝除不變）；ADR-0100（工具 schema canonical、只在出場轉換）、ADR-0228（原生工具公開輸入合約），決策都不變

## 背景

1. Anthropic 的工具 `input_schema` 根層不收 `oneOf`／`anyOf`／`allOf`，只要有一個工具帶著，整個請求回 400；內層可以。2026-10-15 實測 Claude 時被拒收，錯誤指向這一點。
2. 參數正本 `action_parameters`（`ToukaMemory/src/memory_server/tools/contracts.py`）把「每個動作要哪些欄位」寫成根層 `oneOf`：每個 action 一個分支，分支裡只有 `const` 與 `required`，欄位本身都在根層 `properties`。
3. 時間線：
   - 2026-07-04（`6554bd6e`，原生工具 Tier 2）起，`mission_tools` 根層就有 `oneOf`，且在 `builtin_tools_schema` 裡。同一天 Tier 2 到 Tier 3（`734e77f4`）之間，`vault_tools`／`project_tools` 也帶根層 `oneOf` 並列給模型；Tier 3 之後它們只剩可執行 shim、不再列出。
   - 2026-09-16（`bf946d66`）把同一個形狀擴到 `memory_tools` 與 `edit_soul`，改由 `action_parameters` 產生。
4. 出場轉換 `openai_to_anthropic_tools` 的兩個分支都把參數原樣放進 `input_schema`：OpenAI 形狀分支直接引用 `parameters`，Anthropic 形狀分支把整個 dict 直通。所以 Claude 請求的工具只要含這三顆任一顆（前景跟靈魂聊天、心跳、`/debug/scorecard/run`），或外部 MCP 工具根層帶組合子，整包 400。【推測：7 月起是否真的失敗，取決於當時 Anthropic 是否已擋、靈魂有沒有開這些工具】不受影響：審查員、摘要員與其他 `complete_once` 用途（不帶工具）、`/debug/execute`、子代理與一般群聊（通常不帶這三顆）。
5. Gemini 出場本來就剝 `oneOf`／`anyOf`／`allOf`／`const`／`additionalProperties`；OpenAI 相容與 Responses 照收。

### 為什麼一直沒有測試抓到

- 自動測試一律用假的廠商（禁令 61），沒有任何測試核對「送給各家的最後形狀符不符合該家的規定」：
  - `test_native_tool_payload_contracts.py` 只比語意（properties 與根層 required；`schema_errors` 刻意忽略組合子）；
  - `test_adapter_contract.py` 只看形狀（有 `name`／`input_schema`、沒有 `function`）；
  - 上游錄製測試用的是簡化過的 schema。
- 舊測試 `test_tool_schema_hygiene.py::test_anthropic_conversion_preserves_oneof` 反過來斷言「轉給 Anthropic 要保留根層 `oneOf`」（前提是「Anthropic 收完整 JSON Schema」），等於把錯誤行為鎖住。

## 決策

1. **只修出場最後一步**：`openai_to_anthropic_tools` 的兩個分支都改用 `_anthropic_input_schema` 另建一份 input_schema：
   - 剝掉根層 `oneOf`／`anyOf`／`allOf`，其他根層鍵（含 `$defs`）照留，內層不動；
   - 剝之前把各分支的 `properties` 併進根層 `properties`（同名以根層為準；分支之間的同名欄位定義都保留，多種合成 `anyOf`〔ADR-0285 甲-2 修訂；原本是分支之間先出現者為準〕）：外部 MCP 工具只把欄位寫在分支裡時，欄位不會跟著消失，`$ref` 也還指得到；
   - `required` 只留根層原有的；
   - 根層 `type` 一律 `object`；參數不是 dict 時給空的 object schema。
2. **一律建新 dict、絕不就地修改輸入**：ToolLayer 把模組層 schema 以參考放進請求，就地改會連帶改到 Gemini、OpenAI 與我們自己的檢查。Anthropic 形狀分支也改成複製後再放出去，順帶修好一個既有問題：`openai_to_claude` 之後在輸出末項寫 `cache_control`，以前會寫到呼叫端傳進來的那個工具物件上。沒有 `input_schema` 的 Anthropic 伺服器工具原樣複製、不補欄位。
3. **不改參數正本**：正本同時給 Gemini、OpenAI 與執行前檢查用；改正本會打破既有測試與條件必填的單一來源。Guard #45 基準不動（它記的是 consumer 形狀，不含 `adapters/`）。
4. **補測試**：
   - 舊測試改寫為：根層沒有組合子、內層保留、原本那份沒被改。
   - 新增跨廠商出場形狀測試：用真的 ToolLayer 組出實際列給模型的工具（含 mission_tools、memory_tools、edit_soul），經四家 adapter 的 `prepare_request`（不送出、不建連線）後逐家核對：Anthropic 根層沒有組合子、是 object、欄位不少於正本根層＋分支；Gemini 不支援的欄位（轉換器的清單＋官方 Schema 物件可用欄位表）都不在；OpenAI 相容／Responses 根層是 object；出場不改模組層 schema。另有根層 oneOf 的 MCP 工具、欄位只寫在分支裡的 MCP 工具（含 `$ref`）、本來就是 Anthropic 形狀的直通工具、伺服器工具、缺根層 type 等案例。全部離線、不連任何廠商。

## 影響

- 鬆緊：不變。只轉格式；執行前照樣用正本嚴格檢查（`validate_native_arguments`），錯了回結構化錯誤。
- Claude 看不到「每個動作要哪些欄位」的條件提示，只剩說明文字（`action` 的 description 已列出各動作必要參數）。比起整包被拒是改善。
- Gemini、OpenAI 相容、Responses 送出的工具定義不變。端點、OpenAPI、NOQA、Guard #45 基準、AGENTS.md 都不變。
- 驗證：改寫的舊測試與 8 個新案例在修正前失敗；突變 10 條（就地修改、只處理一個分支（兩種）、不併分支欄位、不補 type、只剝 oneOf、丟 `$defs`、同名以分支為準、分支必填升根層、伺服器工具補 schema）全紅。

## 已知限制

- 條件必填對 Claude 只剩說明文字，模型送錯欄位組合時要靠執行前檢查擋下、多一輪修正。
- 只處理根層的三個組合子；根層 `not`、`if`／`then`／`else` 等其他關鍵字不處理（目前沒有工具使用）。
- 分支的 `required` 不升到根層（照設計），所以 MCP 工具「各分支各自必填」的資訊對 Claude 也只剩描述文字。
- 〔已由 ADR-0285 甲-2 解決：定義都保留，多種合成 `anyOf`〕原本分支之間同名欄位定義不同時，只取第一個出現的定義。
- 【推測】Gemini 出場對外部 MCP 工具的 `$ref`／`$defs` 不處理，欄位只寫在分支裡的也會在剝分支時消失；這類 MCP 工具在 Gemini 可能被拒收或變成沒有欄位。本 ADR 範圍外，未修（見 HANDOVER §3）。
- 不涵蓋：SNS 走 Claude 時 ctx 缺連線物件的問題、OpenAI 相容轉送服務後端是 Claude 時的同類限制（另案）。

## 回復

revert 這個 PR 即可（產品只動 `tool_schema.py` 與一行註解），Claude 會回到帶這三顆工具就整包 400 的狀態；ADR-0119 狀態區與索引列的反向註記一併還原。
