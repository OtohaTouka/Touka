# ADR-0002: 記憶系統整合模式 (Integrated Memory Mode)

## 狀態
已接受 (Accepted)

## 日期
2026-04-15

## 上下文 (Context)
最初嘗試使用 MCP (Model Context Protocol) 模式作為 `ToukaProxy` 與 `ToukaMemory` 之間的通訊標準。然而在實際部署中發現：
1. **通訊延遲**：Stdio/SSE 通訊在高頻率記憶讀寫下產生明顯延遲。
2. **部署複雜度**：需要額外管理 MCP 伺服器的進程生命週期與連接埠。
3. **型別安全**：JSON-RPC 的動態特性使得跨模組的型別驗證變得困難。

## 決策 (Decision)
在 Touka 專案內部強制使用「整合模式 (Integrated Mode)」。`ToukaProxy` 直接透過 Python `importlib` 匯入 `ToukaMemory` 的代碼庫，將其作為一個本地函式庫調用。

## 後果 (Consequences)
- **優點**：
  - **零延遲存取**：消除網路或進程間通訊的開銷。
  - **強型別支援**：支援 Pydantic 模型與靜態分析檢查。
  - **部署簡化**：記憶系統隨 Proxy 啟動而載入，無需獨立守護進程。
- **缺點**：
  - **模組依賴性**：Proxy 與 Memory 的代碼版本必須高度對齊。
  - **記憶體占用**：Proxy 進程會載入完整的向量資料庫與 Embedding 模型。
