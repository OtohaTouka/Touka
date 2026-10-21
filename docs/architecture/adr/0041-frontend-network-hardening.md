# ADR-0041: Frontend HTTP/API Network Hardening — 前端 HTTP/API 網路層安全加固與容錯機制

**狀態**: ✅ 已接受（時點性內容修訂：文中 authService.logout try-catch 為生產死碼——AuthContext 實走 logoutTouka（早於本 ADR 已接線）；前端網路加固機制行為等價不變）
**日期**: 2026-05-24
**關鍵字**: fetchTouka, Token Handshake, Auth Resilience, Network Hardening

---

## 背景與問題

系統的前端透過統一的 API 適配器 `fetchTouka` 與後端 `ToukaProxy` 進行安全通訊。然而，在面臨異常網絡或會話登出時，原本的前端網絡層存在以下漏洞與限制：
1. **登出異常阻斷**：登出操作 `logout` 需要通知後端註銷 Token，但若後端服務斷線或網絡中斷，`fetch` 請求會拋出異常。由於原本未對該請求進行適當捕獲，異常會阻斷前端的憑證清除邏輯，使用戶被迫卡在無效的登入會話中，損害了 UX 韌性。
2. **自動降級繞過 SSOT**：當前端以不帶 `/v1` 的 URL 路徑（如 `/auth/login`）存取後端，且後端回傳 404 時，`fetchTouka` 原本會自動補上 `/v1` 並進行降級請求。但該降級請求直接呼叫了瀏覽器原生的裸 `fetch()`，繞過了 `fetchTouka` 核心的 Token 注入與安全憑證標頭。同時，這也引進了 `// noqa: FETCH_SSOT` 的靜態檢查豁免，增加了安全隱憂。
3. **主機名稱解析脆弱**：`getActiveHeartbeatUrl()` 在解析本地環境的 Heartbeat 位址時，使用字串的 `replace`（如替換 `:8000` 為 `:8082`），這種基於字串的替換在複雜網址或非預期的 URL 結構下極易損壞。

---

## 決策

為提升前端 API 網路層的安全性、容錯性與 SSOT 合規性，我們實施了以下網路層加固方案：

### 1. 登出流程異常防禦
- 修改 `authService.logout`：在發送 POST `/v1/auth/logout` 時包覆 `try-catch` 區塊。
- 當網絡出錯或後端響應異常時，將錯誤靜默忽略，並回傳 `{ status: 'success' }`。這確保了調用方（如 `AuthContext`）能順利清除前端的 `_accessToken`、localStorage 連線設定與用戶狀態，不會因為網絡故障導致無法登出。

### 2. fetchTouka 自動降級遞迴重構
- 重構 `fetchTouka`：當發生 404 且路徑尚未包含 `/v1` 時，將原先使用的裸 `fetch` 調用改為**遞迴調用 `fetchTouka(proxyUrl, '/v1' + cleanPath, options)`**。
- 此舉使降級請求能完整套用 `fetchTouka` 內部的 Header 注入（如 `Authorization: Bearer <token>`、`Content-Type`）與 CORS 設定（`credentials: 'include'`），確保降級與常規請求具有完全相同的安全等級。
- 順利移除了 `// noqa: FETCH_SSOT` 豁免，實現 100% 靜態守衛（Guard #11）合規。

### 3. 基於 URL 物件的安全主機解析
- 重構 `getActiveHeartbeatUrl()`：使用 JavaScript 原生的 `URL` 物件來解析傳入的 `proxyUrl`，提取出 `hostname`、`port` 與 `protocol`。
- 僅在 `hostname` 為本地端（`localhost` 或 `127.0.0.1`）且 `port` 為 `"8000"` 時，才動態映射至 `"8082"`。這避免了硬編碼的 replace 操作，提升了 Hostname 解析的強健性。

---

## 影響

- **提升 UX 與會話韌性**：即使後端伺服器完全斷線，用戶依然能夠順利完成前端登出並返回登入頁面，清空所有敏感的 JWT Token 憑證。
- **維護安全防禦鏈的一致性**：自動補全 `/v1` 的降級請求不再有繞過安全標頭與認證機制的隱憂，維持前端與後端安全通道的完整封閉。
- **守衛綠燈與程式碼精簡**：消除了 `connections.ts` 中不必要的 `# noqa` 戰術豁免，使代碼符合 TDD、SOLID 與 SSOT 原則。
