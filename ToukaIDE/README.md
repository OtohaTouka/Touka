# ToukaIDE (燈火 VS Code 擴充套件)

ToukaIDE 是為 VS Code 量身打造的 AI 原生開發插件。它不僅提供了側邊欄對話，更透過內建的 **ToukaMcpServer** 賦予 AI 讀寫檔案、執行終端機指令以及操作 Git 的能力。

## 🚀 核心功能

- **內嵌式聊天視圖**：直接與 Touka 進行對話，支援代碼解釋、重構與計畫追蹤。0.3.0延續安全純文字模式：保留換行，但不解析Markdown或HTML；Webview不載入外部CDN JavaScript（ADR-0219）。
- **提及系統 (@Mentions)**：
    - `@problems`: 獲取目前的診斷資訊 (Diagnostics)。
    - `@terminal`: 獲取終端機最後的輸出內容。
    - `@git`: 獲取 Git 暫存區與未暫存的變更摘要。
    - `@opened`: 引用所有目前已開啟的檔案內容。
- **Artifact 橋接接收器**：
    - **預覽與套用**：接收來自 `ToukaChat` 的 `Apply to Project` 指令。
    - **虛擬編輯器 (Virtual Buffer)**：內容會以「未儲存檔案」形式在 VS Code 中開啟，讓使用者審核後再決定是否存檔，確保安全性。
- **MCP 工具整合 (Model Context Protocol)**：
  - **檔案操作**：基於 `FileHandlers` 的讀寫與目錄遍歷。
  - **Terminal Auto-Fix**：自動捕捉 Exit Code 並在 ReAct 迴圈中嘗試修正。
- **分級授權機制 (Tiered Auth，ADR-0246)**：
  - `Manual`: 寫入與執行前需手動確認 (預設)。
  - `YOLO`: 全自動執行。
  - 黑名單（Git main／master、checkout、push、reset，Docker stop／restart／rm 等）兩種模式都直接拒絕。人類已在 Touka 卡片核准過的那一次呼叫不再問第二次（需經 loopback＋bearer 帶合法標記）。
- **MCP 2026 bridge (ADR-0202)** ⭐：0.3.0只提供 Streamable HTTP `/mcp`；
  退役 `/mcp/sse`＋`/mcp/messages` 回 404，保留 modern request-scoped SSE。此版本不再接受舊 HTTP+SSE clients；更新客戶端後使用 `/mcp`。
- **逐 request Bearer**：啟動時以 `crypto.randomBytes(32)` 生成 session token，
  discovery 只交給 ToukaProxy；modern 每個 HTTP request 都重驗，空 token
  fail-closed。
- **多連線與取消**：modern request 彼此隔離；client
  關閉 stream 時取消訊號會傳至 terminal executor。

## 🛠️ 技術架構

- `src/extension.ts`: 核心入口與指令註冊。
- `src/mcp/server.ts`: AuthLevel 與 IDE 工具 executor 組裝。
- `src/mcp/httpBridge.ts`: modern-only `/mcp` transport bridge。
- `src/mcp/protocolServer.ts`: 官方 Node SDK v2 server/tool registry。
- `src/mcp/contracts.ts`: endpoint 與 discovery SSOT。
- `src/mcp/handlers/`: 工具邏輯分離 (`Editor`, `File`, `Git`, `Terminal`)。
- `src/providers/`: 提供 `ChatViewProvider` 與 `CodeLens` 支援。

## 安裝、連線與更新

1. 在 VS Code 的 Extensions 搜尋 **Touka IDE**，核對發行者 **touka-ai**、完整 ID **touka-ai.toukaide**，按 Install／Update；也可由 Extensions 的「Install from VSIX」選擇已取得的正式 VSIX。
2. 先啟動同一本機的 Touka Proxy。ToukaIDE 是獨立的 VS Code 擴充套件，不包含 Proxy，也不在三個 runtime images 內。
3. 在 VS Code Settings 搜尋 `Touka`，將 `touka.proxyUrl` 設為你的 Proxy 網址（預設 `http://localhost:8000`），`touka.apiKey` 填入供此連線使用的 Bearer 憑證。不要使用 Dispatch mission token，也不要分享個人 token。
4. 執行 **Start Touka** 開啟側欄；`touka.defaultModel` 留空時採第一個可用 Agent。行內補全可由 `touka.enableInlineCompletion` 關閉；補全請求帶 `X-Touka-Purpose: inline-completion`（Touka 只拿來讓 Claude 5.5 少想）、10 秒逾時，繼續打字時取消舊請求。
5. IDE 提供給 Touka 的工具反向連線只綁 loopback，使用 `/mcp`及啟動時產生的 discovery token；這與側欄聊天到 Proxy 的認證是不同方向。

**0.3.0 更新內容**：Node SDK v2／MCP `2026-07-28` modern-only bridge；退役 `/mcp/sse`與`/mcp/messages`，保留 modern request-scoped SSE、逐 request Bearer、取消與多連線隔離。Webview仍以純文字顯示HTML／Markdown並保留換行，不載入外部CDN腳本。版本更新不替你改動Touka的個人資料或部署。

套件宣告 VS Code `^1.90.0`／Node20+；建置工具版本與實際驗證的host分開記錄，不代表所有Windows／Linux／macOS都已實測。0.3.0採一份universal VSIX；原0.2.0保留為歷史版本。

## 原始碼開發與發行

建置固定 **Node.js 24.22.1／npm 11.19.1**。從 `ToukaIDE` 目錄依序執行：

```powershell
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm run typecheck
npm run compile
npm run package
```

`compile`／`package`共用`dist/`，必須串行。F5可啟動擴充開發主機；source建置不會自行安裝或發布。
正式候選由`ToukaIDE/scripts/build_marketplace_candidate.py`在clean、`v0.3.0`錨定的來源建立，使用repo-local
`@vscode/vsce@3.9.2`、10-member allowlist、root MIT LICENSE及create-only SHA-256 manifest；不上傳、不登入、不使用npx。
正式發行須另驗隔離Windows Stable smoke與Marketplace回讀，不因本機runtime映像成功而通過。

以下是GitHub上的來源與進階文件；Marketplace安裝不要求取得原始碼：

- [Touka來源與安裝指南](https://github.com/OtohaTouka/Touka/blob/main/README_QUICKSTART.md)
- [ToukaIDE技術規格](https://github.com/OtohaTouka/Touka/blob/main/docs/ide/spec_IDE.md)
- [建置與測試指南](https://github.com/OtohaTouka/Touka/blob/main/docs/guides/testing_guide.md)
- [Marketplace發行與驗證程序](https://github.com/OtohaTouka/Touka/blob/main/docs/guides/toukaide-marketplace-release.md)

MIT License。Publisher：touka-ai；extension ID：touka-ai.toukaide。
