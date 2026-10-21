# ADR-0018: 多使用者身分認證與安全增強 (Multi-User Auth & Security Hardening)

> **現況**：§5 ReadOnly 終端白名單（ADR-0143 反轉）；§5、§6 改為三模式，等級鎖定移到 session 寫入（ADR-0246）。

## 狀態 (Status)
✅ 已接受 (Accepted)

> 🔧 **§5 ReadOnly 終端白名單已被 [ADR-0143](0143-hitl-unification.md)（HITL 統一）反轉**：原「ReadOnly 允許白名單指令（ls/pwd）執行」→現行 `safety.py` terminal 只分 safe/dangerous，ReadOnly 對兩者**皆 DENY**（ls/pwd 現被拒），只有 read_only-class 工具自動放行。JWT/Bcrypt/多使用者認證等其餘決策仍有效。

> 🔧 **§5 四階等級與 §6「ABAC 等級鎖定」已被 [ADR-0246](0246-model-layer-stage-2b-2c.md)（Manual／Auto／YOLO 三模式）修訂**：四級改三模式（舊值讀取時對照：ReadOnly、SafeAuto→Manual，FullYOLO→YOLO），加上程式常數底線與 Auto 審查員；「只有 admin 或擁有者能開 Auto／YOLO」改在 session 寫入時判定，不再由 MonitorLayer 降級。JWT／Bcrypt／多使用者認證等其餘決策仍有效。

## 上下文 (Context)
在 Touka 系統的早期演進中，系統主要針對「單一使用者」情境設計，Agent 的身分（AgentIdentity）雖然已經物件化，但缺乏與真實人類使用者（Human User）的權屬綁定。這導致了以下局限性：
1. **安全性風險**：任何人連線至 Proxy 都能存取所有 Agent 的資料與工具。
2. **缺乏擁有權概念**：無法區分哪些 Agent 屬於管理員（Admin），哪些屬於普通用戶。
3. **自主與授權的矛盾**：Heartbeat 自主脈衝需要在使用者離線時運作，但現有的短效 JWT 認證無法滿足長生命週期的自主行為，且缺乏「使用者不登入則停止自主」的安全閥。
4. **Bcrypt 技術局限**：原生的 `passlib[bcrypt]` 實作受限於 bcrypt 演算法的 72-byte 輸入限制，導致超長密碼或複雜字元集會引發 API 500 錯誤。

## 決策 (Decision)

### 1. 人類使用者主權 (Human User SSOT)
實施 `ToukaProxy/config/users.json` 作為人類使用者的唯一事實來源（SSOT）。
- **欄位規範**：包含 `id`, `email`, `hashed_password`, `role`, `owned_agents`, `refresh_token_exp` 以及個人資料欄位 (`display_name`, `avatar_url`, `bio`)。
- **擁有權綁定**：實施「使用者 -> Agent ID」的映射。在生成 `AgentIdentity` 時，必須注入其 `owner_user_id`，達成物理路徑與權限的雙重隔離。
- **身分物件化存取**：(原 ADR-005) 系統執行層不再依賴原始字串 ID，而是透過經由 `IdentityService` 驗證後的不可變 `AgentIdentity` 物件進行資源定址。

### 2. 雙軌 Token 與自主授權
- **人類登入**：採用 Access Token (1h) 與 Refresh Token (30d) 機制。Refresh Token 儲存於 `HttpOnly` Cookie 以防止 XSS 攻擊。
- **Heartbeat 雙重守衛**：
    - **守衛 A (Service JWT)**：Heartbeat 服務與 Proxy 通訊時，必須攜帶專屬的「服務級 JWT」，廢除明文 API Key。
    - **守衛 B (Refresh Token 檢驗)**：喚醒 Agent 前，Proxy 會強制檢查該 Agent 擁有者的 Refresh Token 是否在 30 天效期內。若使用者超過 30 天未登入，系統將自動暫停該 Agent 的自主運作。

### 3. Bcrypt 預雜湊策略 (Pre-hashing)
為了繞過 bcrypt 的 72-byte 限制並移除已停止維護的 `passlib`：
- **流程**：`SHA256(Raw Password) -> Bcrypt(SHA256 Hex)`。
- 將原始密碼先轉為固定的 64-byte Hex 字串後再進行 bcrypt 處理，確保全系統密碼處理的穩定性與安全性。

### 4. 前端認證導航 (Auth Guard)
- **AuthProvider**：全域管理登入狀態與靜默刷新（Silent Refresh）邏輯。
- **AuthGuard**：實施 React 組件級路由攔截。未登入者除 `/login` 與 `/register` 外一律重新導向。
- **Credentials Include**：強制全系統 `fetch` 調用必須開啟 `credentials: 'include'`。

### 5. ABAC 四階安全等級體系 (4-Tier Security Matrix)
為了在「極致安全」與「自主演化」間取得平衡，實施基於環境屬性（ABAC）的執行策略：
- **ReadOnly**: 強制只讀，僅允許白名單指令（如 `ls`, `pwd`）執行，其餘一律 `DENY`。
- **Manual**: 所有人機交互行為（包含安全指令）均需使用者手動點選確認 (HITL)，達成 100% 交互主權。
- **SafeAuto**: 自動放行白名單內的安全指令，其餘指令需手動確認。
- **FullYOLO**: 除了預定義的黑名單（Regex 攔截，如 `rm -rf /`）外，所有指令全自動執行。
- **執行鏈路加固**：修復了 `ToolExecutor` 參數遺失 Bug，確保 `auth_level` 從 UI 到底層工具（bash）的 100% 透傳。

### 6. 管理員代理權限優化 (Identity Assumption Optimization)
解決管理員在 Assume 或 Proxy 代理人身分時「丟失管理權限」的問題：
- **權限繼承**：在 `IdentityRouter` 與 `MonitorLayer` 中，系統會識別發起請求的原始人類身分。若原始身分為 `admin`，即使當前操作上下文是 `AgentIdentity`，系統依然保留其存取 `/v1/admin/*` 管理端點的權力。
- **ABAC 等級鎖定**：只有具備 `admin` 角色或身為 Agent 擁有者，方可將執行等級提升至 `SafeAuto` 或 `FullYOLO`。其餘請求一律自動降級為 `Manual`。

## 後果 (Consequences)
- **安全性提升**：實現了人類使用者級別的隔離與權限控制，並透過 4-tier ABAC 矩陣實現了細粒度的執行安全。
- **管理體驗優化**：管理員現在可以在代理模式下無縫切換 Agent，而無需頻繁重新登入以管理使用者。
- **自主行為受控**：建立了「使用者活性」與「Agent 自主性」的聯動機制，防止資源無限浪費。
- **開發效率**：修正了 FullYOLO 在 Terminal 中的斷鏈問題，提升了自主演化的開發體驗。
- **數據不相容性**：由於雜湊演算法變更，升級後的系統需要刪除舊的 `users.json` 並重新註冊/初始化。

---
*Created on 2026-05-09*
