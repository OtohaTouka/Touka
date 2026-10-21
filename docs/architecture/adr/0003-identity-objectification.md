# ADR-0003: 身分物件化主權 (Identity Objectification)

## 狀態
已接受 (Accepted)

## 日期
2026-04-19 (於 2026-04-24 加固)

## 上下文 (Context)
系統長期使用字串形式的 `agent_id` 作為身分識別。這導致了多個 SSOT 違規：
1. **路徑歧義**：不同的服務對同一 ID 可能解析出不同的物理路徑。
2. **參數劫持**：在 Middleware 鏈中，原始請求的 ID 容易被意外修改。
3. **驗證斷層**：無法保證傳遞到底層 Service 的 ID 是經過 JWT 驗證的。

## 決策 (Decision)
全面推行「身分物件化」。系統在 Gateway 層完成 JWT 認證後，立即生成不可變的 `AgentIdentity` 物件。後續所有 Service 層、Resource 層與工具執行層皆必須傳遞此物件，而非原始字串。

## 後果 (Consequences)
- **優點**：
  - **路徑主權隔離**：`ResourceManager` 僅根據 `AgentIdentity` 物件解析路徑，徹底杜絕越權訪問。
  - **安全加固**：物件化確保了身分在傳遞過程中的不可變性與真實性。
  - **自動糾偏**：身分物件可封裝別名糾偏邏輯（如：小愛 -> Alice）。
- **缺點**：
  - 需要大規模重構舊有代碼的函式簽章。
  - 在開發與測試階段需要更複雜的 Mock 物件。

## 最終實施 (Final Implementation - 2026-05-14)
- **路由層強制物件化**：透過 Pydantic `model_validator` 實現在 DTO 邊界自動將字串 ID 轉化為 `AgentIdentity` 物件。
- **靜態執法**：`lint_ssot.py` 加入 `IDENTITY_SSOT` 規則，強制要求所有 Service 與 Router 參數定義必須使用 `AgentIdentity` 型別。
- **例外管理**：針對隔離運行的 `ToukaHeartbeat` 與 MCP 邊界的 `ToukaMemory` 實施 `# noqa` 豁免並記錄於 [NOQA_REGISTRY.md](../NOQA_REGISTRY.md)。
