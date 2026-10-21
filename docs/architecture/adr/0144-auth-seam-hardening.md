# ADR-0144: 認證縫三合一——改密碼即撤 access × session 死亡導回登入 × LDH 首裝限定

## 狀態

✅ 已接受（07-11 認證事故修復清單 #1／#2／#3，同批落地）（watermark SSOT 擴充 by [ADR-0160](0160-auth-seam-hardening-p3.md)：access_token_is_stale 判準 `iat < password_changed_at`→`watermark = max(password_changed_at, tokens_revoked_at)`（§2 蓋登出撤銷）；本 ADR 認證縫三合一決策本身不變，watermark 為超集故改密碼路徑仍準）

## 日期

2026-07-11

## 背景

07-11 傍晚認證事故（前傳＝修復 #0 空密碼覆蓋，見
`tests/unit/test_password_change_safety.py` 檔頭）暴露同一條縫上的三個獨立缺陷，
組合成「一小時後全系統鎖死＋殭屍身分擋登入」的完整災難鏈：

1. **改密碼只撤 refresh，不撤 access**：access token 為無狀態 JWT（TTL=3600s），
   改密碼後既有 access 照用最長 1 小時——被害 session 毫不知情地繼續工作（事故中
   「14:08 改壞密碼、15:0x 才鎖死」的成因）；若改密碼動機是憑證外洩，被竊 token
   同樣多活 1 小時。前端也**收不到任何通知**。
2. **refresh 判死後前端死循環**：`refreshAccessToken` 失敗一律「保留舊卡」（設計
   註解如此），對「網路抖動」正確、對「後端權威拒絕（401/403＝撤銷/過期/重放）」
   錯誤——app 卡在帶死卡狂打 401 的狀態，永不導回登入頁，使用者只看到滿屏錯誤。
3. **LDH 跨面發卡**：人類面認證死亡（refresh 401）→ 重載後無卡本機請求觸發 LDH
   → 後端鑄發 7 天 DebugAgent 卡 → 前端「未登入採納」接手＝**殭屍身分**：
   ADR-0141 語言的跨面越界（human 面失敗換到 agent 面的卡），整個 app 被降級卡
   挾持、看起來像登入卻不是任何人，還擋住正常登入流程。

owner 反思定調（HANDOVER 07-11）：主病根＝「縫上的承諾沒被釘」＋接縫預設
fail-open／靜默降級。本案三修全部向 fail-loud 收斂。

## 決策

### 修復 1：改密碼即撤 access token（後端授權面）

- access token 簽發加 `iat`（authenticate_user／refresh_access_token）。
- `User.password_changed_at`（epoch 秒）：`change_own_password` 與
  `admin_update_user` 密碼分支在 `_atomic_users_write` 鎖內蓋戳（縮小「舊密碼
  並發登入搶不死 token」競態窗至鎖粒度）。
- 判準 SSOT `auth_service.access_token_is_stale`：`iat < password_changed_at`
  ＝拒絕；無 `iat` 的舊制 token 在時間戳已設定時一律拒絕（fail-closed）；同秒
  邊界放行（改完密碼立即重登不受阻）。
- **兩道 Bearer 閘一致掛鉤**：`routers/auth.get_current_user`（/auth 系＋admin
  端點）與 `identity_service.verify_token` 人類分支（Gate 1＝聊天/工具全域）。
  單閘漏網＝修復不完整，測試對兩閘同時斷言。
- 修復 0 的「空密碼＝保持不變」路徑**不**蓋戳（誤撤＝另一種鎖死；有測試釘住）。
- 自助改密碼 UX 不變：ChangePassword 本就成功後登出重登；本修補上的是「其他
  session／被竊 token」與「admin 改他人密碼」的即時失效。

### 修復 2：session 死亡導回登入（前端）

- `connections.ts` 新 `setSessionExpiredHandler`：`refreshAccessToken` 收到
  **401/403**（後端權威判死）→ 清記憶體卡＋通知訂閱者；網路錯誤／5xx／429
  維持舊行為（保留卡、稍後重試）——暫時性故障不得誤殺 session。
- `fetchTouka`／`fetchChatStream`（proxy）：帶模組卡的請求被判 401（例：修復 1
  的撤銷——token 未過期但已失效，送出前續期不會觸發）→ single-flight 續期一次
  並以新卡重送一次；續期也死＝handler 已觸發，原 401 照回（不迴圈）。
- `AuthContext` 掛載時註冊 handler：清 React 認證狀態（AuthGuard 據此導向
  /login）＋toast `error.auth.session_expired` 說明原因。

### 修復 3：LDH 首裝限定（雙面閘）

- **後端**（ADR-0131「帶壞卡→401」的續篇）：LDH 僅在「系統未認領」時可用——
  判準 SSOT `auth_service.any_claimed_user()`＝任一使用者完成真密碼設定
  （bootstrap 暫時密碼 admin 不算，首裝精靈流程不受影響）。一旦認領，無卡本機
  請求不再降級亦不發卡，`get_optional_identity` 回 None（強制閘 401 fail-loud）。
  開發工具首領卡同受此限（首裝後請用正規登入／service token）。
- **前端**：`touka_has_logged_in` 持久旗標（登入成功時標記，登出**不**清除）；
  曾登入過的瀏覽器對 `X-New-Auth-Token` 一律不採納（fetchTouka 與
  fetchChatStream 兩處領卡點同閘）。從未登入的瀏覽器維持首裝領卡相容。

## 後果

- **正面**：改密碼＝所有既有 session（含被竊 access）秒級失效且前端立即導回
  登入；session 死亡從「滿屏 401 死循環」變成「toast＋登入頁」；殭屍身分不可能
  再現（後端不發、前端不收，雙保險）；LDH 攻擊面從「永久」縮到「首裝窗口」。
- **代價／限制**：
  - `iat` 為秒級解析度：與密碼變更同一秒內簽發的舊 token 有 ≤1s 存活窗（接受；
    換取「改完密碼立即重登」不被誤殺）。
  - 舊制無 `iat` token 在該使用者首次改密碼後即全數失效（正是意圖）。
  - 已認領系統上，依賴 LDH 的本機腳本／開發工具需改用正規憑證（fail-loud 揭露
    而非掩蓋設定缺失，與 owner 修復哲學一致）。
  - 測試面：LDH 行為自此依賴使用者狀態，相關測試必須顯式建立認領狀態
    （`test_ldh_first_run_gate.py` 為狀態矩陣 SSOT）。
- **不做**（刻意）：access token 黑名單／jti 撤銷表（有狀態、複雜度不成比例）；
  自助改密碼後自動續 session（保留「請重新登入」既有契約）。

## 相關

- [ADR-0131](0131-auth-renewal-and-ldh-containment.md)（LDH 收斂前篇：帶壞卡→401）
- [ADR-0141](0141-role-plane-separation.md)（人類面／agent 面互斥——修復 3 堵住的跨面越界）
- [ADR-0143](0143-hitl-unification.md)（同事故季的 HITL 統一）
- 事故鏈與修復清單：HANDOVER.md「07-11 認證事故」節；修復 0＝
  `tests/unit/test_password_change_safety.py`
