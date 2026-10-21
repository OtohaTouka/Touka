# ADR-0039: X-Agent-ID Front-End Identity Sovereignty Guard — 前端 X-Agent-ID 身份主權守衛

**狀態**: ✅ 已接受
**日期**: 2026-05-24
**關鍵字**: Identity Sovereignty, AST Scan, xAgentId, Front-End Guard, LaC

---

## 背景與問題

在先前的系統設計中，為了方便本機測試與快速取得 MCP schemas，前端的部分 API 呼叫（如 `connections.ts` 中的 `fetchToolSchemas` 及 `DebugMonitoringView.tsx`）手動在 HTTP Headers 中注入了 `"X-Agent-ID": "DebugAgent"`。

這產生了兩大嚴重的安全與架構衝突：
1. **身份主權 (Identity SSOT) 違規**：身份的確立必須一律來自經 JWT Token 解密後的 `AgentIdentity` 物件。前端手動宣告身份繞過了認證鏈。
2. **身份偽造風險 (Spoofing Risk)**：如果允許請求透過 Headers 直接覆寫 `X-Agent-ID`，惡意請求即可在不擁有目標密鑰的情況下，隨意偽裝為任何 Agent 身份，造成路徑逃逸與越權。

因此，全系統已立法封鎖後端讀取 `X-Agent-ID` Header 作為身份 fallback。為了確保前端程式碼的合規性，需要有一套自動化靜態檢查工具（Law as Code, LaC）在編譯或建置期阻斷這類 Headers 注入。

---

## 決策

我們決定移除前端所有手動注入 `X-Agent-ID` 的程式碼，並在前端建立自動化靜態守衛（`guard_xAgentId.ts`），將其註冊為 `guard_all.py` 的全局檢查關卡。

### 1. 移除前端違規代碼
- 移除 `connections.ts` 的 `fetchToolSchemas` 中手動注入的 `X-Agent-ID` header。
- 移除 `DebugMonitoringView.tsx` 呼叫 `fetchTouka` 時的 `X-Agent-ID` header。

### 2. 實施 `guard_xAgentId.ts` 靜態掃描器
前端掃描器採用 TypeScript Compiler API 進行 AST 語意分析：
- **掃描對象**：`ToukaChat/src` 底下所有 `.ts` 與 `.tsx` 檔案。
- **偵測規則**：
  - 當 PropertyAssignment 中的 `name` 經大小寫歸一化為 `"x-agent-id"` 時判定違規。
  - 當 ElementAccess 中的 `argumentExpression` 具備 StringLiteral 且值為 `"x-agent-id"` 時判定違規。
- **豁免機制**：支援行尾 `// noqa: IDENTITY_SSOT` 精確戰術豁免。
- **進入點集成**：於 `ToukaChat/package.json` 註冊 `guard:xAgentId`，並於全局 `scripts/guard_all.py` 註冊為 **第 13 道關卡 (Frontend X-Agent-ID Identity Guard)**。

---

## 影響

- **消除安全漏洞**：防止開發者在未來開發中出於便利性再次寫死或硬編碼 `X-Agent-ID`。
- **加固身份主權**：前端與後端身份判定事實來源 (SSOT) 達到 100% 強對齊，所有安全權限與路徑定位皆基於驗證後的 JWT Token Payload。
- ** Law as Code 執法自動化**：不合規的前端代碼將無法通過 `guard_all.py` 驗證，直接阻斷 CI/CD 流程，確保代碼庫的防禦性。
