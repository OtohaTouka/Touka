# ADR-0160: 認證縫修復批 P3——null 毒化防護／登出撤銷完整／refresh 重武裝／前端殘留

## 狀態

✅ 已接受（四功能區唯讀調查 §auth 的 P3 項，維護者 2026-07-15 決定：登出撤銷=完整方案、範圍=全納入三個 low 項）

## 日期

2026-07-15

## 背景

四功能區調查（07-15）對 07-11 空密碼事故（ADR-0144）同族做了完整 code review，在「安全設定/使用者管理改動引發 token 或權限補發競態」這條線找到 9 個未修站點。owner 點名處理 P3；多分頁 JTI 亂序（#5）經查 proxy 日誌**零筆 `AUTH_REFRESH_TOKEN_REUSE`**＋動 refresh 輪替核心有風險，判為「已知不動」。其餘八站分四區實作。

## 決策與實作

### §1 輸入驗證（含 high #1）

- **admin PATCH 顯式 null 毒化**（high）：`AdminUpdateUserRequest` 全欄位 Optional＋`model_dump(exclude_unset=True)` 保留顯式 null＋`admin_update_user` 無值驗證 setattr → `{"role": null}` 一發即把 users.json 寫成 JSON null → 下次 `_read_users` 的 `User(**u)` 驗證失敗 → PERSISTENCE_ERROR → 登入/refresh/認證全 500（07-11「看鍵不看值」的 null 版）。三重修：①`_write_users` 寫前逐筆 `User.model_validate` fail-loud（毒化當下該請求失敗，而非讀時全站掛——核心兜底）；②`admin_update_user` 對關鍵欄位（role/status/email/owned_agents）的顯式 null 收斂為 no-op（API 面友善）；③`role`/`status` 收斂為 `Literal` 枚舉（擋 typo 悄悄失權/停權）。
- **admin 改 email 無唯一性**（medium）：`admin_update_user` 鎖內查重（改成自己現有 email＝no-op 放行）——舊制撞號後 `get_user_by_email` first-match 恆命中前者、另一帳號永久遮蔽。
- **create_user 兩段寫**（low）：`UserService.create_user` 收為單一 `_atomic_users_write` 閉包（建立＋掛擴充欄位），杜絕兩寫之間並發變更被覆蓋。

### §2 登出撤銷完整（high #2，維護者選完整方案）

- **`tokens_revoked_at` 水位**：`User` 加欄位；`revoke_user_tokens` 設戳；`access_token_is_stale` 判準改 `iat < max(password_changed_at, tokens_revoked_at)`（與 ADR-0144 改密碼即撤共用 SSOT）——登出即撤既有 access token，不再殘活至 1h 自然過期。
- **logout 憑 refresh cookie 撤銷**：logout 端點改為從 HttpOnly refresh cookie 解出 user_id 撤銷（不再依賴 `get_current_user` 需有效 Bearer）——使用者閒置逾 1h（access 過期）點登出即得 401、後端撤銷從未執行、30 天 refresh cookie 完好、下次開頁 session 復活（「登出了其實沒登出」）。端點入 auth allowlist（憑自己的 HttpOnly cookie 撤銷自己、非破壞性）。

### §3 refresh 重武裝競態（#7）

- **authenticate 重驗 hash 快照**：`authenticate_user` 於鎖外 verify_password 後、`_mutate` 內比對 `hashed_password` 快照未變——若合法使用者於驗證期間改密碼，舊密碼登入者不得重新武裝 refresh（不符即這次登入作廢）。
- **refresh 水位閘**：refresh payload 加 `iat`；`refresh_access_token` 輪替時 `iat < max(水位)` 一律拒（改密碼前已持有的 refresh token 不得持續換新卡繞過踢人）；無 iat 舊制 refresh 在水位已設時 fail-closed。jwt.decode 對 refresh 關 `verify_iat`（水位用業務時鐘 `get_current_epoch`，不走 jwt UTC iat「未成熟」驗證，避免時鐘偏移誤殺）。

### §4 前端（#9 in-flight refresh 復活、#6 URL 分裂）

- **#9 半殭屍**：`clearAccessToken` 加 token 世代 counter——登出/換帳號遞增之，在途 refresh run 回填前比對世代（不符即不回填），並清 `_knownAgentIds`（換帳號跨帳號殘留關閉）。`logoutTouka` 改走 `clearAccessToken`（不再裸設 null）。
- **#6 URL 收斂**：`authService.login/refresh/logout/register` 改走 `getActiveProxyUrl()`（對齊 changePassword 與業務請求）——多連線設定下不再 cookie/token 面向分裂。

## 後果（誠實行為變化）

- admin 編輯面對 null/typo/撞號 email fail-loud（毒化不再拖垮全站）；登出真正撤銷（過期卡也能登出、既有 access 即失效、session 不復活）；改密碼前的 refresh token 與驗證期間改密碼的重武裝皆被堵；前端登出無半殭屍卡殘留、換帳號無跨帳號清單殘留。
- **未納入（已知不動）**：多分頁 refresh 亂序「中間代 JTI」誤判 replay（#5）——proxy 日誌零實跡＋動輪替核心風險高，記錄於此；owner 若日後見無故被踢＋日誌出現 `AUTH_REFRESH_TOKEN_REUSE` 再議。
- **openapi 變更**：`AdminUpdateUserRequest` role/status → enum、logout 端點簽章（移除 Bearer 依賴）；`User` 內部欄位 `tokens_revoked_at` 不進 wire（UserOut 未含）。api.ts 隨之重生。
- **驗收**：後端 pytest **3546 passed**（新 test_auth_seam_hardening_p3 14 條＋既有 grace/h6/logout 回歸）／前端 vitest **1060 passed**（新 connections/authService P3 測試，紅→綠 stash 驗證）／guard_all **41/41**（logout 入 allowlist、NOQA registry 淨 +6→205）／lint_ssot clean／ruff 基線持平（664）／tsc 零新增（44≤基線 45）。

## 相關

- [ADR-0144](0144-auth-seam-hardening.md)（07-11 事故三修＝本批同族源頭；改密碼即撤/session 判死/LDH 首裝限定）
- [ADR-0131](0131-auth-renewal-and-ldh-containment.md)（續期 single-flight/LDH containment＝#9 世代 counter 的所在鏈路）
- 四功能區調查 P0（部署漂移）/P1（[ADR-0158](0158-soul-creation-auto-attribution.md) 靈魂歸屬）/P2（[ADR-0159](0159-providers-auth-self-heal-parity.md) Provider 自癒）
