# ToukaIDE 介面文件 (Standardized IDE Interface)

> **TL;DR** — ToukaIDE 只綁 `127.0.0.1`；modern-only 只提供 `/mcp`，每個 request 都須帶 discovery Bearer，空 token fail-closed，modern request-scoped SSE 保留。舊 `/mcp/sse`＋`/mcp/messages` 回 404（ADR-0221）。Webview只載入VSIX內script/style，Extension→Webview動態值固定純文字、保留換行，不解析Markdown／HTML（ADR-0219）。Marketplace 發行沿 ADR-0218 路徑；`npm run package` 只驗 bundle，不是 VSIX／Marketplace 安裝資格。Terminal只剩Manual／YOLO兩種AuthLevel（黑名單兩種都拒絕）＋VS Code modal HITL；tools/call的`_meta`帶合法「由人核准」HMAC標記（人類在Touka卡片核准過的那一次呼叫，60秒、nonce只用一次）時免第二次確認（ADR-0246）；檔案路徑必須留在`vscode.workspace.workspaceFolders`。Proxy `/editor/files`仍需`_require_editor_role()`。

本文件定義了 Touka 系統中與 IDE 功能（檔案編輯、終端執行、工具管理、VSCode 整合）相關的對外介面與資料來源。本文件不包含內部實作，旨在提供開發者整合與呼叫的基準。

## 🔗 與記憶系統的集成 (ToukaMemory Integration)

IDE 工具系統（`vault_tools`, `project_tools` 等）由 ToukaMemory 提供，透過 Proxy 呼叫：
- **唯一整合路徑（ADR-0116：整合模式單一部署）**：Proxy 同行程掛載 ToukaMemory；`get_resource_proxy()` 只取得注入的真實 `ResourceManager`，取不到即 fail-loud。
- **IDE 邊界**：`ResourceManager` 注入是 Proxy／ToukaMemory 的內部實作；IDE 只依賴工具合約，不感知也不選擇注入方式。

---

## 📁 檔案與編輯器介面 (Editor API)

檔案服務定義了對工作區物理檔案的讀取與列舉權限。

### 1. 檔案列舉與發現
- **端點**: `GET /editor/files`
- **參數**: `path` (相對路徑，預設為 `.`)
- **說明**: 用於前端 `@Mentions` 檔案連結或側邊欄瀏覽。支援專案過濾 (如排除 `node_modules`, `.git`)。
- **格式**:
  ```json
  {
    "path": "current/path",
    "items": [
      { "name": "file.py", "path": "path/file.py", "is_dir": false, "size": 1024 }
    ]
  }
  ```

### 2. 內容讀取
- **端點**: `GET /editor/read`
- **參數**: `path` (必填)
- **輸出**: `{"path": "...", "content": "..."}`

---

## 💻 終端與執行介面 (Terminal API)

終端介面負責 Bash 指令執行過程的串流推送、審核流程與歷史紀錄。

### 1. 指令執行串流 (SSE)
- **端點**: `GET /terminal/stream`
- **格式**: `text/event-stream`
- **事件類型**:
  - `bash_start`: 指令開始執行。
  - `bash_approval_request`: 偵測到危險指令，請求使用者授權。
  - `bash_result`: 指令結束，回傳 `exit_code`, `output` 與耗時。
  - `bash_approval_timeout`: 審核超時自動拒絕。

### 2. 審核互動 (Approval Flow)
- **批准**: `POST /terminal/approve/{exec_id}`
- **拒絕**: `POST /terminal/reject/{exec_id}`
- **歷史**: `GET /terminal/history`
- **說明**: 當前端收到 `bash_approval_request` 後，應呼叫批准端點。

---

## 🛠️ 工具與 MCP 管理 (MCP Interface)

管理模型可用的外部工具箱與 MCP (Model Context Protocol) 伺服器。

### 1. MCP 伺服器清單
- **端點**: `GET /v1/mcp/list`
- **輸出**: 獲取目前所有已連線 MCP 伺服器的健康狀態與可用工具總數。

### 2. 靜態工具配置 (SSOT)
- **獲取**: `GET /ui/mcp/config`
- **更新**: `POST /ui/mcp/config`
- **資料來源**: `config/server_config.json`

### 3. ToukaIDE 本機 MCP Bridge（ADR-0202）

| 用途 | 端點 | 協定 |
|---|---|---|
| 主路徑 | `POST /mcp` | MCP `2026-07-28` Streamable HTTP；response 使用 SSE |
| 退役建連 | `GET /mcp/sse` | 404，不進 SDK |
| 退役訊息 | `POST /mcp/messages?sessionId=...` | 404，不進 SDK |

- Node runtime 使用 `@modelcontextprotocol/server`／`node` v2 public API；已移除 `server-legacy` 依賴、相容入口及 session transport Map。
- Server 只監聽 loopback，並驗證 Host／Origin；缺少或錯誤 Bearer 回 401，啟動 token
  為空回 503。MCP session 與 `clientInfo` 不能代替 Bearer。
- JSON 請求內容只收未壓縮的：`Content-Type` 是 JSON 的請求帶 `Content-Encoding`（`identity` 或空值除外）
  時回 415。這一步在 Bearer 驗證之前，也在建立任何解壓縮串流之前（GHSA-27r3-jxmh-p828）。其他
  `Content-Type` 不在這裡解析，沒帶權杖照常回 401，整條路上都不解壓縮。
- Express 轉交的錯誤（請求內容解析失敗、路由丟錯）回固定 JSON：4xx 照原狀態碼回
  `{"error":"Invalid request"}`，5xx 回 `{"error":"Internal server error"}`，不回 stack、錯誤訊息或檔案路徑；
  5xx 另寫擴充主機日誌。Bearer、Host／Origin、exact route 與關閉中的既有回應不變；沒有對應路由仍是
  Express 的 404 頁（不含 stack，只回顯跳脫過的路徑）。
- modern HTTP stream 關閉會把 AbortSignal 傳入工具 executor；終端子程序收到取消後
  會被終止，避免 client 斷線後繼續執行。
- 多個 modern client 彼此 request-scoped；取消其中一個 stream 後，另一個 client 仍須能執行工具。
- `/mcp` 以 exact route guard 處理：大小寫／尾斜線不同的 path alias 回 404，method mismatch 回 405。
- Bridge 開始 close 時同步設 closing latch，其後的 late request 回 503；modern handler close 有 1,000 ms
  上限，逾時只記 degraded 警告。

### 4. Discovery contract

Extension 在 OS temp 的 `gemini/ide/gemini-ide-server-touka.json` 寫入：

```json
{
  "ideInfo": { "name": "ToukaUI" },
  "port": 3005,
  "authToken": "<session-only bearer>",
  "transport": {
    "type": "streamable-http",
    "endpoint": "/mcp",
    "protocolMode": "2026-07-28"
  }
}
```

Proxy 只接受合法 port、非空 token、exact modern mode 與本機絕對 path；不接受外部 URL、
`//host`、query 或 fragment。token 只進 discovery file 與 Proxy process memory，
不得出現在 log 或 Webview message。

### 5. Marketplace candidate boundary（ADR-0218）

- Identity固定`touka-ai.toukaide`、MIT；版本與tag由`ToukaIDE/scripts/deterministic_vsix.py`固定（現行`0.3.0`／`v0.3.0`）；唯一registry是Visual Studio Marketplace。
- `@vscode/vsce@3.9.2`必須同時是package、lock與installed exact pin；不用`npx`，不加入
  `@vscode/test-electron`。
- Candidate builder要求root `LICENSE`與`ToukaIDE` release inputs clean、tag解析到HEAD，並在bundle前後
  重驗source identity；不檢查或存取`data/`。
- Node 24.22.1直接執行npm 11.19.1的package-local `npm-cli.js`及repo-local `vsce` JavaScript entry；
  禁止batch／shell fallback與publish credential繼承。
- Repo外create-only output只能含`toukaide-<version>.vsix`與
  `toukaide-<version>.manifest.json`。Manifest綁source commit、tag、identity、license、archive SHA-256與
  exact 10-member inventory。
- Candidate build不等於smoke、upload或readback。三者各需fresh human gate；upload只能使用同一份VSIX，
  不得rebuild。發行步驟見`docs/guides/toukaide-marketplace-release.md`。
- Candidate內的Webview必須符合ADR-0219 packaged-only CSP與純文字渲染；在該修復前建立的VSIX固定
  `DO NOT PUBLISH`，不得沿用舊manifest或hash。

---

## 🐳 VSCode 橋接介面 (VSCode Bridge)

當系統運行於 VSCode Webview 內部時，透過 `postMessage` 實現雙向通訊。

### Webview 顯示安全邊界（ADR-0219）

- 所有Extension→Webview payload（含user／model／history／plan／Agent名稱與workspace檔名）皆視為不可信。
- 動態內容只可使用DOM `createElement()`＋`textContent`；禁止任何HTML sink，清空固定用
  `replaceChildren()`。
- CSP只允許`${cspSource}`內的封裝script/style，禁止CDN、inline script/style與Webview connect。
- 0.2.0只顯示純文字並以`white-space: pre-wrap`保留換行；`**粗體**`與HTML標記不解析。

- **發送指令**: `window.vscode.postMessage({ command: "...", data: {...} })`
- **常見指令**:
  - `openFile`: 在 VSCode 編輯器中打開特定檔案。
  - `showNotification`: 在 VSCode 狀態列顯示訊息。
  - `applyDiff`: 將 AI 生成的 Diff 直接套用到當前編輯檔案。

---

## 🔄 SSOT 來源位置 (Single Source of Truth)

| 功能模組 | SSOT 來源 | 存放位置 |
| :--- | :--- | :--- |
| **ToukaIDE 工作區根目錄** | VS Code Workspace API | `vscode.workspace.workspaceFolders` |
| **ToukaIDE MCP 端點** | `MCP_ENDPOINTS` | `ToukaIDE/src/mcp/contracts.ts` |
| **ToukaIDE discovery** | `buildDiscoveryPayload()` | OS temp `gemini/ide/` |
| **ToukaIDE AuthLevel** | `ToukaMcpServer` runtime state | Extension process memory |
| **ToukaIDE Webview rendering policy** | ADR-0219 | `docs/architecture/adr/0219-toukaide-webview-plaintext-security-boundary.md` |
| **ToukaIDE Webview rendering implementation** | CSP＋DOM＋CSS | `ToukaIDE/src/webview/index.html`、`main.js`、`style.css` |
| **ToukaIDE Webview rendering guard** | Vitest真script／fake DOM＋靜態契約 | `ToukaIDE/src/__tests__/webviewSecurity.test.ts` |
| **ToukaIDE Marketplace candidate** | `package.json`＋`package-lock.json`＋`build_marketplace_candidate.py`＋`deterministic_vsix.py` | exact Node/npm/vsce、clean HEAD＋版本tag、repo外一份VSIX＋canonical manifest；人工Marketplace upload，ADR-0218 |
| **Proxy MCP 工具配置** | JSON 配置檔 | `ToukaProxy/config/server_config.json` |
