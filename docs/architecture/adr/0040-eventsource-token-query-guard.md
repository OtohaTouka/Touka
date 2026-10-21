# ADR-0040: Fetch-based SSE Authorization and Anti-pattern Guard — 改用 Fetch 串流以 Header 進行身份驗證與 EventSource 禁用守衛

**狀態**: ✅ 已接受
**日期**: 2026-05-25
**關鍵字**: EventSource, Server-Sent Events, Fetch, ReadableStream, Authorization Header, Front-End Guard, Anti-Pattern, LaC

---

## 背景與問題

系統中使用 Server-Sent Events (SSE) 協定實現脈衝 ReAct 循環的流式傳輸與輸出。原設計（URL Query Token 降級驗證機制）由於瀏覽器原生 `EventSource` 不支援自定義 HTTP Headers，因而將 JWT Token 附加在 URL Query string 中（例如：`?token=...`）。

此做法帶來了顯著的安全隱患：
1. **日誌洩漏**：Web 伺服器、反向代理（如 Nginx）預設會將包含 Query 參數的完整 URL 記錄在 Access Logs 中，使 JWT 憑證明文留底。
2. **網頁與 Referer 洩漏**：Token 會殘留在瀏覽器歷史紀錄中，且若頁面跳轉至第三方，可能在 `Referer` 標頭中洩漏。

為滿足強安全性防禦，我們必須將憑證收攏回 HTTP Headers 中，並防範未來開發者誤用原生 `EventSource` 引入此反模式。

---

## 決策

我們決定**改用 Fetch + ReadableStream 實現 SSE 串流傳輸**，並在前端建立自動化 EventSource 禁用守衛。

### 1. 前端 useAgentSSE 改用 Fetch 串流
- 廢除原生 `new EventSource()`。
- 改為使用原生 `fetch` 發起串流連線，並在 Header 中攜帶 `"Authorization": "Bearer <JWT>"`。
- 利用 `response.body.getReader()` 來獲取 `ReadableStream` 進行串流讀取，並透過 `TextDecoder` 自行解析 SSE 的 `data:` 行格式。
- 利用 `AbortController` 來掌控連線中斷（`closeStream`），確保資源關閉與記憶體防漏。

### 2. 後端移除 URL Token 降級驗證
- 修改後端認證核心中介 `get_optional_identity`：移除從 `query_params` 獲取 token 的降級邏輯。
- 後端僅接受 HTTP `Authorization` Bearer 標頭中的憑證，統一認證入口 (SSOT)。

### 3. 前端實施 `guard_eventSource.ts` 靜態禁用守衛
- **定位**：將原本的 Token 攜帶守衛轉型為**「安全性防護網（反模式攔截）」**。
- **偵測規則**：AST 掃描所有 `new EventSource(arg)` 調用，一旦偵測到即判定為安全性違規（除非標記 `// noqa: EVENTSOURCE_SSOT`）。
- **目的**：強制未來開發者統一使用更安全、基於 Header 驗證的 `useAgentSSE` 或是 fetch 串流封裝。
- **進入點集成**：於 `scripts/guard_all.py` 註冊為 **第 26 道關卡 (Frontend EventSource Token Guard)**。

---

## 影響

- **提升安全性**：JWT Token 始終存在於安全加密的 HTTP 標頭中，不會暴露在 Access Logs、瀏覽器歷史紀錄與 Referer 標頭中。
- **認證一致性 (SSOT)**：前後端統一使用 Bearer Token Header 進行認證，減少了認證管道的複雜度與後端暴露的攻擊面。
- **防止安全漏洞退化**：透過靜態 AST 禁用 `new EventSource`，能在建置期 (CI) 自動發現並攔截不安全的原生 SSE 連線。
