# ADR 0008: 優雅身分重新簽發 (Graceful Identity Reissue)

> **現況**：LDH 壞卡處理改成 fail-loud 401（ADR-0131）、預設身分改 DebugAgent（ADR-0147）、範圍縮到首裝（ADR-0144）。

## 狀態
已接受 (Accepted)

> 🔧 **核心 Decision 三點已被後續認證/身分 ADR 反轉（部分修訂；本 ADR 原文保留為歷史紀錄）**：
> 1. **「壞卡當無卡」→ fail-loud 401**：[ADR-0131](0131-auth-renewal-and-ldh-containment.md)（禁令 60）令 `identity_router.get_optional_identity` 對任何 verify 失敗（含本地過期/壞 token）一律 raise `AUTH_INVALID_TOKEN`(401)、不再匿名重簽，以防過期人類 session 被劫成 DebugAgent 殭屍身分。
> 2. **LDH 預設身分 Guest → DebugAgent**：[ADR-0147](0147-soul-role-type-binding.md) 後 LDH 預設解析為 `DebugAgent`（經 `agent_overrides` → Debugger）。
> 3. **LDH 範圍縮到首裝/未認領**：[ADR-0144](0144-auth-seam-hardening.md) 以 `AuthService().any_claimed_user()` 為閘，系統一旦被認領即**永久停用 LDH**（不再無條件 always-available 降級）。
>
> **仍有效**：LDH 核心存在——信任域判定（loopback/RFC-1918）、role/caps 解析為 AgentIdentity、`X-New-Auth-Token` 回應標頭續期。

## 背景
在 Docker 或長效運行環境中，前端持有的 JWT Token 可能因過期或系統重啟（Secret 變更）而失效。若直接回傳 401 Unauthorized，會中斷本地自動化診斷 (如 `DebugAgent` 的心跳探針) 與本地開發者的流暢體驗，且在受信任的本地網域下強制手動登入是不必要的摩擦。

## 決策
實施「本地動態握手 (Local Dynamic Handshake, LDH)」機制：
1. **信任網域判定**：針對來源為 Loopback (127.0.0.1) 或私有網路 (RFC 1918) 的請求進行特殊處理。
2. **壞卡當無卡**：若本地請求攜帶無效或過期的 Token，系統不應報錯，而是清除該 Token 並將其視為匿名請求。
3. **自動重新發卡**：
    - 根據請求標頭中的 `X-Agent-ID` (如果有) 或預設的 `Guest` 身分。
    - 透過 `PermissionService` 判定該 ID 在當前環境下的角色與能力。
    - 生成新的 `AgentIdentity` 物件並簽發新 Token。
4. **回應標頭傳遞**：將新 Token 置於回應標頭 `X-New-Auth-Token` 中，告知前端更新存儲。

## 後果
- **正向**：極大提升了本地開發與自動化測試的穩定性；實現了「無感認證恢復」。
- **負向**：稍微增加了身分路由層的邏輯複雜度。
- **風險**：必須嚴格限制僅限本地私網觸發，防止外部網路利用此機制繞過認證。
