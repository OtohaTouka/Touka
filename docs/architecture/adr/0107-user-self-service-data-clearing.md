# ADR-0107：使用者自助清空個人資料 + 專案歸屬 — 範圍邊界、明確擁有權、密碼閘自助清除

- **狀態**：✅ 已接受（2026-06-22 經 TDD 實作完成並全綠驗證：guard 37/37、後端 pytest（含自助清除/專案歸屬測）、vitest、smoke 24/24；對抗式複審）
- **日期**：2026-06-22
- **影響範圍**：ToukaProxy（`utils/async_db.py` projects schema+migration+SessionService、`routers/ui_sessions.py` save/access、`routers/auth.py` clear-my-data 端點）＋ ToukaChat（`GeneralTab.tsx` 危險區、`useUserSettings.ts`、i18n）
- **關鍵字**：self-service data clearing, project ownership, owner_user_id, JWT self-only, password re-entry, CSRF, 資料模型 agent-keyed, 範圍邊界, 保留實體資料夾, audit retention, 設定頁路線圖, 建基於 ADR-0104（破壞性重置）/ADR-0003（身分主權）/ADR-0078（原子寫）

## 背景（Context）

設定頁審查路線圖項目「使用者自助清空資料」。經 5 區唯讀研究釐清一個**關鍵資料模型事實**：Touka 中**對話、記憶、產出、feedback 全部以 `agent_id` 為鍵**——技術上掛在使用者「擁有的 AI agents」名下，不是掛在「人類帳號」下。人類使用者透過 `User.owned_agents` 擁有 agents。

因此「純人類使用者、與 agent 無關」的資料**只有**：個人檔案（`display_name`、`bio`）+ 頭像（檔案＋`avatar_url`）。另研究發現：**專案（projects）資料表無 owner 欄位**（`ui_sessions.py:79-106`，擁有權僅從「該專案 sessions 的 agent_id」反推；空專案無可推導擁有者）。

擁有者裁示：自助清空**只清純人類資料（profile+頭像）**，並**新增專案歸屬**、清空時連帶刪「我擁有專案的設定」，但**保留實體資料夾**。不含 agent 對話/記憶、不含帳號刪除（後者為獨立路線圖項）。

ADR-0104 的 reset 原語以 `agent_id` 參數化（可複用），但其 guard 寫死 DebugAgent 且封鎖有 soul 者、auth 為 Owner/admin（非自助）→ 不適用；本案另建自助路徑。

## 決策（Decision）

### D1 — 清除範圍：純人類資料 + 我擁有專案的「設定」
清除：`display_name`、`bio`、頭像（檔＋url）、**我擁有專案的設定列**。
**保留**：email/密碼/角色/`owned_agents`（帳號身分）、**所有 agent 對話/記憶/產出**、**專案 `root_path` 實體資料夾**、稽核軌跡。
- **拒絕**：清 agent 累積資料（屬另一概念，需個別 per-agent 設計）；刪帳號（獨立路線圖項「自助刪號」）。

### D2 — 專案明確擁有權（新）
`projects` 表新增 `owner_user_id TEXT`（CREATE + `PRAGMA table_info`/`ALTER TABLE` 遷移，沿用 user_sessions 既有 pattern）。`save_project` 於 `identity.is_human` 時設 `owner_user_id = identity.agent_id`（人類 JWT sub 即 user_id）；更新時保留原 owner（不在 ON CONFLICT UPDATE）。
存取控制（`_can_access_project`/`_can_modify_project`）：**明確 owner 優先**（owner 本人可存取）；無 owner（legacy）→ 退回既有「從 session 反推」。補上研究發現的「無擁有權」缺口。

### D3 — 清除時的專案處理
`SessionService.clear_projects_owned_by(user_id)`：`DELETE FROM projects WHERE owner_user_id=?`；該專案底下 sessions 的 `project_id` 置 **NULL**（**保留對話內容**、只解標籤）；**絕不刪 `root_path` 實體資料夾**。`project_audit` 保留。

### D4 — 自助端點與防護
`POST /v1/auth/clear-my-data`（`Depends(get_current_user)` 自助、身分取自 JWT → 無 IDOR）。防護：
- `verify_origin`（CSRF，對齊 terminal/restore）
- **密碼重輸**（`AuthService.verify_password`；stolen access token 單獨擋不住——DA25 pattern）→ 錯誤回 `AUTH_INVALID_CREDENTIALS`（補記 2026-10-21：改為 403 `AUTH_REAUTH_FAILED`，401 會被前端續期重送；見 PROXY_INTERFACE「密碼重輸錯誤碼」）
- 前端兩步確認（危險區紅框 + 密碼對話框）
- **不做全系統備份**（刪除範圍輕：profile/頭像/專案設定列，實體與對話皆保留，全備份不成比例）
- 成功**不強制登出**；回傳透明報告 `{cleared, retained}`；稽核 `logger.warning` 一行（保留）。

### D5 — 複用既有
profile 清除複用 `update_user_profile`（safe_fields）；頭像複用 `avatar_service.delete_avatar`（ADR-0106）；零新錯誤碼（密碼錯用既有 `AUTH_INVALID_CREDENTIALS`）。

## 後果（Consequences）

**正面**：
- 非技術擁有者可一鍵清空個人足跡（名稱/介紹/頭像/專案設定），語意清楚、與 agent 資料分離。
- 專案獲得**明確擁有權**（修補長期「無 owner 欄位」缺口），存取控制更精確。
- 對話/agent/實體檔案/帳號全保留——破壞面小、可預期。

**代價/風險**：
- projects schema 變更（遷移；既有專案 owner 為 null＝維持 legacy 反推，不回溯歸屬）。
- 存取控制對「有明確 owner 的專案」改為 owner-only（比 session-反推嚴）；僅影響 ADR-0107 後新建的專案，既有專案不變。
- 清除為盡力（無備份）：profile/專案設定刪除若中途失敗可能部分完成（資料輕、實體與對話安全，可接受）。

## 相關 ADR
- 建基於 **ADR-0104**（破壞性重置——本案不複用其 DebugAgent guard，僅借「備份/稽核保留」思路並刻意省略全備份）、**ADR-0003**（身分主權，JWT 自助無 IDOR）、**ADR-0106**（頭像，複用 delete_avatar）、**ADR-0078**（原子寫）。
- **同構延伸**：[ADR-0132](0132-session-owner-attribution.md) 把本案 D2 的 `owner_user_id` pattern（INSERT 記錄、UPDATE 保留、owner 優先/legacy fallback）延伸至 `user_sessions`（授權採聯集 additive 變體）。
- **獨立未來項**：agent 累積資料 per-agent 重置；使用者自助刪帳號 + 資料匯出（GDPR 可攜）。
