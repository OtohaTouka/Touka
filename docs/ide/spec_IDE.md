# 🛡️ Touka IDE: AI Agent VS Code 擴充功能開發規格書 (v1.0)

## 1. 專案概述
本專案旨在開發一款類似 Continue、Roo Code 或 Cline 的 VS Code 擴充功能。該擴充功能定位為「IDE 前端介面與本機執行環境」，完美對接已自主開發的 AI Agent Proxy 系統。

### 1.1 設計原則
*   **薄前端、厚後端**：Extension 本身僅負責 IDE 介面呈現與本機資源存取，不內嵌任何 AI 推理邏輯。
*   **標準化協議**：透過 **MCP (Model Context Protocol)** 協議將 IDE 能力標準化，解耦 Extension 與 Proxy 的實作細節。

### 1.2 技術定位
| 項目 | 定位 |
| :--- | :--- |
| **前端介面** | 嵌入 VS Code Webview |
| **通訊協議** | MCP `2026-07-28` Streamable HTTP `/mcp`（modern-only；舊 HTTP+SSE 入口回 404） |
| **AI 邏輯** | 完全由 Proxy 與 Agent 系統處理 |
| **狀態管理** | 四大記憶庫由 Proxy 端 Agent 自主管理 |

---
## 2. 系統架構設計 (Modular Architecture)

### 2.1 模組化目錄結構
為了提升可維護性，IDE 端代碼現已全面拆分：
- **`src/extension.ts`**: 核心進入點，負責插件生命週期。
- **`src/mcp/server.ts`**: VS Code 能力、AuthLevel 與工具 executor 組裝。
- **`src/mcp/httpBridge.ts`**: modern `/mcp` transport、退役入口 404 與逐 request 認證。
- **`src/mcp/protocolServer.ts`**: SDK v2 `McpServer` 與工具 schema 註冊。
- **`src/mcp/contracts.ts`**: endpoint／discovery contract SSOT。
- **`src/mcp/handlers/`**: 工具具體實作邏輯（File, Git, Terminal, Editor）。
- **`src/commands/`**: VS Code 命令實作（如 Inline Chat）。
- **`src/providers/`**: Webview 視圖與代碼輔助邏輯。

### 2.2 UI 整合策略

*   **生產模式**：`src/webview/` 的靜態HTML／JavaScript／CSS由`copy-webview.js`複製至`dist/webview/`，
    並以`webview.asWebviewUri()`取得VSIX內資源；不使用Next.js export或遠端runtime。
*   **CSP 設定**：`default-src 'none'`；script/style只允許VS Code `${cspSource}`，`connect-src 'none'`，
    禁止CDN與inline script/style。
*   **渲染策略（ADR-0219）**：Extension送入的訊息、串流、計畫與workspace metadata一律以
    `textContent`顯示並保留換行；ToukaIDE 0.2.0不解析Markdown或HTML。

---

## 3. 核心功能模組規劃 (MCP Tools)

### 3.1 工作區感知工具
*   `get_workspace_structure`: 回傳專案目錄樹。
*   `get_diagnostics`: 取得 Problems 面板錯誤（Debug 優先於終端機執行）。
*   `get_open_file`: 取得當前開啟檔案全文。
*   `get_selection`: 取得游標選取文字。

### 3.2 檔案系統工具
*   `read_file`: 讀取檔案內容。
*   `write_file`: 寫入檔案內容（Phase 4 加入 Diff 確認模式）。
*   `list_directory`: 列出目錄結構。
*   `search_files`: 全域字串搜尋。
*   `show_diff`: 觸發 VS Code 原生 Diff 視窗。

### 3.3 終端機工具
*   `execute_terminal`: 在 Workspace 執行指令。
*   **安全邊界**：過濾 `rm -rf /`、`sudo`、`mkfs`、`dd` 等高危指令，觸發時強制跳出確認對話框。

---

## 4. 優化與降級策略

### 4.1 Token 效能優化
*   **動態工具載入**：根據上下文動態決定提供的 MCP Tools Schema。
*   **主動檢索**：Agent 依需求主動查詢摘要而非全量載入背景資料。
*   **Context 分級**：
    *   **Level 1**: 當前檔案全文。
    *   **Level 2**: 相關檔案摘要（函式簽名 + 註解）。
    *   **Level 3**: 向量搜尋結果片段。

### 4.2 錯誤處理
*   **連線失敗**：Webview 自動重試，顯示連線狀態列。
*   **工具執行失敗**：Agent 收到結構化錯誤訊息，不靜默失敗。
*   **危險攔截**：強制彈出確認對話框，使用者可選擇允許或拒絕。

---

## 5. 開發階段與里程碑

*   **Phase 1**: Extension 骨架與 Proxy 串接（能送訊息、收 SSE 串流）。
*   **Phase 2**: Next.js 前端生產模式整合（靜態 HTML 嵌入）。
*   **Phase 3**: MCP Server 與 IDE API 封裝（已完成）。
*   **MCP 2026 P4-B**: Node SDK v2、modern `/mcp`、隔離 legacy endpoints、逐 request
    Bearer、多 client／取消／HITL conformance（已完成，ADR-0202）。
*   **MCP modern-only（ADR-0221）**: 舊 `/mcp/sse`＋`/mcp/messages` 入口退役回 404，只保留 modern
    `/mcp` 與 request-scoped SSE。
### 4. 自主權與安全機制 (AuthLevel System)
- **分級授權機制**（ADR-0246，IDE 沒有審查員，只有兩種）:
  - `Manual`: 每步操作均需人類點擊確認；人類已在 Touka 卡片核准過的那一次呼叫（帶合法「由人核准」標記）不再問。
  - `YOLO`: 除黑名單外全自動執行。
  - 黑名單在兩種模式都直接拒絕。
- **強制安全護欄 (Blacklist)**: 
  - 禁止對 `main/master` 分支進行任何 Git 操作 (Push, Checkout, Reset)。
  - 禁止對 `docker/docker-compose` 進行重啟或終止操作，防止 Agent 陷入自我循環。
  - 破壞性指令 (`rm -rf`, `format`) 強制要求二次確認。
- **環境感知**: 模式切換時自動向 Webview 與 Agent 注入系統通知。
*   **Phase 5: 編輯器 UX 與提及系統 (Implemented)**
    - **提及系統 (@Mentions)**: 支援 `@problems` (診斷資訊)、`@terminal` (終端輸出)、`@git` (變更摘要) 與 `@opened` (已開啟檔案)。
    - **Inline UX**: 實作了 Code Lens (解釋/重構)、Inline Completion (FIM 預測) 與 Inline Chat (`Ctrl+I`)。
*   **Phase 6: 代理人計畫與 Artifacts 橋接 (Implemented)**
    - **計畫追蹤**: 支援結構化 SSE 工具事件解析 (`tool_start`/`tool_result`) 與多步驟執行視覺化。
    - **Artifact 橋接**: 實作 `touka.applyArtifact` 處理器。
    - **安全套用機制**: Artifact 內容會以 **「虛擬編輯器 (Virtual Buffer)」** 形式開啟，不直接寫入磁碟，確保使用者能在存檔前進行最終審核。
*   **Phase 7: Visual Studio Marketplace 最小首發（ADR-0218；implementation candidate）**
    - 固定`0.2.0`／`v0.2.0`、MIT、`touka-ai.toukaide`與一份universal VSIX。
    - `@vscode/vsce@3.9.2` direct exact pin；Node 24.21.0直接執行npm 11.19.1 CLI與local vsce，
      不使用`npx`或batch fallback。
    - Builder只接受clean、tag-anchored source，在repo外create-only輸出VSIX＋canonical manifest；root
      LICENSE只注入staging，不建立tracked `ToukaIDE/LICENSE`。
    - Windows VS Code smoke與Marketplace upload/readback由人類逐項核准；Open VSX不在範圍。
      現行 `0.3.0` 發行步驟見 `docs/guides/toukaide-marketplace-release.md`。

---

## 6. 技術選型
*   **語言**: TypeScript
*   **打包工具**: esbuild (比 Webpack 快 10x)
*   **MCP 實作**: Express＋官方 Node v2 分拆套件
    (`@modelcontextprotocol/server`／`node`)
*   **Diff 視窗**: `vscode.diff()` 原生 API

---

## 附錄：MCP 協議握手流程
1.  **Extension discovery**：發布 `/mcp`、`protocolMode=2026-07-28` 與 session Bearer；token
    不進 log／Webview。
2.  **Proxy -> Extension**：對 `/mcp` 真送 `server/discover` 並完整驗證；Method Not Found 等
    modern 錯誤一律 fail-loud，不重連 legacy。
3.  **協商**：modern request 以每筆 `_meta` 傳 protocol/client metadata。
4.  **工具目錄**：Proxy 發送 `tools/list` 取得完整 Schema。
5.  **工具呼叫**：每個 HTTP request 都重帶 Bearer；Extension 回傳標準 MCP
    `content`／`isError`，斷線時取消訊號傳到 executor。
