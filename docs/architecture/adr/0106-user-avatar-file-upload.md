# ADR-0106：使用者頭像檔案上傳與安全存取 — multipart 上傳、magic-byte 驗證、納入備份、向後相容 avatar_url

- **狀態**：✅ 已接受（2026-06-22 經 TDD 分階實作完成並全綠驗證：guard 37/37、後端 pytest（含 avatar 18 測）、vitest 735、smoke 24/24；資安導向對抗式複審，加固 AV-001 寫入失敗回滾保護）（命名草圖修訂：影響範圍/Action B1 具名的 `utils/image_validation` 模組從未存在，實作在 `services/avatar_service.py`（權威 Decision D1-D7 忠實實作）；頭像上傳/驗證安全機制不變）
- **日期**：2026-06-22
- **影響範圍**：ToukaProxy（`routers/auth.py` 新 avatar 端點、新 `routers/users.py` 或既有 router 的 GET 服務端點、`utils/` 新 `image_validation`、`config.py` avatar 常數、`scripts/manage_backup.py` SANCTUARY_FOLDERS、`core` 錯誤碼）＋ ToukaChat（`GeneralTab.tsx`、`useUserSettings.ts`、`lib/connections.ts` FormData 支援、Avatar 渲染、i18n catalogs）
- **關鍵字**：avatar upload, multipart/form-data, magic-byte validation, SVG-XSS, polyglot, EXIF, path traversal, ResourceManager, atomic write, SANCTUARY_FOLDERS 備份, avatar_url 向後相容, 設定頁路線圖, 建基於 ADR-0078（原子寫）/ADR-0033（資安加固）/ADR-0014（錯誤碼 SSOT）

## 背景（Context）

設定頁批次審查（路線圖 ADR 級項目 #2）指出：頭像目前是**手填 URL 文字框**（`avatar_url: str|None`，user.py:11），非技術使用者難用、外部 URL 有可用性與隱私顧慮。需要「選圖 → 上傳 → 儲存 → 顯示」的真實檔案上傳，且安全、納入備份、不破壞既有相容。

經 6 區唯讀測繪（avatar 生命週期、上傳/服務前例、儲存/部署/備份、資安威脅模型、ADR 慣例、前端）確認的現況：

- **現況流程**：`PATCH /v1/auth/me`（`UpdateProfileRequest` `extra="forbid"`，safe_fields `{display_name, avatar_url, bio}`）→ `UserService.update_user_profile` → `AuthService._atomic_users_write` 寫 `users.json`。前端 `<img src={avatar_url}>`（GeneralTab/UsersTab，fallback UserIcon）。
- **無任何 multipart 上傳**，但 `python-multipart==0.0.32` 已裝、FastAPI 就緒。
- **可複用前例**：`GET /v1/outputs/{agent_id}/{filename}`（main.py:285–318，身分驗證 + `is_relative_to` 路徑防穿越 + `FileResponse`）；`ResourceManager` 原子二進位讀寫（ADR-0078，`tempfile.mkstemp`→`fsync`→`os.replace`）；`validate_no_symlink_escape`（path_security）；`RequestSizeLimitMiddleware`（10MB→413）；`SecurityHeadersMiddleware`（`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`，P11）。
- **lint_ssot IOPrisonRule**：業務邏輯**嚴禁裸 `open()`/`os`**，所有 I/O 必須走 `ResourceManager`。
- **儲存/備份（關鍵約束）**：單機 Docker，資料根 `SANCTUARY_DATA_PATH`（/app/data）。備份 `manage_backup.py` **只收 `SANCTUARY_FOLDERS`（soul/custom/vault/personal_drive/stats/legacy）＋根層 `*.db`**。⚠️新目錄不加入清單就**不會被備份 → 還原時頭像全失**。容器以非 root（uid 1001）執行。
- **資安缺口**：目前**無圖片 magic-byte 驗證**（Content-Type 為用戶端控制、不可信）；未確認有 Pillow。
- **前端約束**：`fetchTouka` 有 body 即自動設 `application/json` → 需偵測 FormData 跳過；`<img>` 為 bearer-in-memory 認證**無法帶 Authorization header**（決定服務端點認證模式）；next/image `unoptimized`（靜態匯出）用原生 `<img>`；無裁切/壓縮套件。

## 決策（Decision）

七個子決策，皆經維護者核准（2026-06-22）：

### D1 — 檔案儲存：`SANCTUARY_DATA_PATH/avatars/{user_id}.{ext}`，納入備份
單檔/人（重傳覆蓋 → **零孤兒檔**），經 `ResourceManager` 原子二進位寫入（ADR-0078）。**`avatars` 加入 `manage_backup.py` 的 `SANCTUARY_FOLDERS`（SSOT）**，確保納入備份與還原。
- **拒絕**：外部 CDN（違資料主權/隱私）；base64 嵌入 `users.json`（破壞 SSOT 分離、肥大、效能）；UUID 檔名（用 `user_id` 即單檔、清理與定址最簡）。

### D2 — 欄位相容：沿用 `avatar_url`，不改 model
上傳成功後將 `avatar_url` 設為內部服務路徑 `/v1/users/{user_id}/avatar?v={epoch}`（`?v` 破瀏覽器快取，重傳即更新）。**舊有外部 URL 照常渲染**（`<img src>` 對兩者皆相容）→ 向後相容、零 model 遷移、零 openapi 破壞性變更。
- **拒絕**：新增 `avatar_file_id`/`avatar_uploaded_at` 欄位（非必要；路徑＋版本 query 已足）。

### D3 — 上傳/刪除端點（自助、無 IDOR）
- `POST /v1/auth/avatar`（multipart/form-data，`Depends(get_current_user)`，**只能操作自己**，身分取自 JWT → 無 IDOR）。
- `DELETE /v1/auth/avatar`（刪檔 + 清 `avatar_url`）。
- **v1 範圍＝僅自助**：admin 仍可用既有 `PATCH /v1/admin/users/{id}` 設別人的 `avatar_url`（URL）。「admin 替別人上傳檔案」列為未來增強（需額外 ABAC + admin multipart 端點）。

### D4 — 縱深驗證（後端為安全邊界，前端僅預檢 UX）
1. **大小** ≤ `AVATAR_MAX_BYTES`（**2MB**，config SSOT）→ 逾時 `AVATAR_TOO_LARGE`。
2. **magic-byte 檢測**：讀首位元組驗 `image/jpeg`(FF D8 FF)/`image/png`(89 50 4E 47…)/`image/webp`(RIFF…WEBP)；**禁 SVG 與其餘一切** → `AVATAR_INVALID_FORMAT`。**不信任副檔名/Content-Type**。
3. **副檔名由 magic-byte 決定**（正規化 jpg/png/webp），不採用戶端檔名。
4. **服務時**帶與內容相符的 `Content-Type` ＋ 既有 `nosniff` header → 防 polyglot 被當 script 執行、防 SVG-XSS。
- **EXIF**：v1 保留（家用單人影響小）。未來可選 Pillow 重編碼徹底去 EXIF/中和 polyglot（記為增強，避免新依賴維持「精簡」）。

### D5 — 服務端點：公開讀取
`GET /v1/users/{user_id}/avatar`（`FileResponse` + `is_relative_to` 路徑防穿越，無檔案回 404）。**不要求認證**——理由：① bearer 在記憶體、`<img src>` 無法帶 Authorization header；② 頭像屬顯示內容、低敏感；③ 端點僅依 opaque `user_id` 回圖片位元組、無使用者資料列舉。
- **拒絕**：認證 blob 拉取（每個頭像渲染需 fetch→objectURL、使用者列表 N 次拉取+清理，複雜度不成比例）。

### D6 — 清理：刪使用者連帶刪頭像
admin `delete_user`（及自助刪號未來功能）移除使用者時，一併刪除其 `avatars/{user_id}.*` 檔，避免孤兒檔長期占碟。

### D7 — 錯誤碼（ADR-0014 SSOT）
新增 `AVATAR_TOO_LARGE`、`AVATAR_INVALID_FORMAT`、`AVATAR_WRITE_FAILED`（前後端 SSOT 同步，前端 `errorMsg(code)` + toast）。

## 後果（Consequences）

**正面**：
- 非技術使用者可直接選圖上傳，取代易錯的手填 URL。
- 檔案納入既有備份/還原架構（D1 加入 SANCTUARY_FOLDERS）。
- 向後相容（D2 沿用 `avatar_url`），舊外部 URL 不受影響、零 model 遷移。
- 複用既有安全前例（sovereign-output 服務模式、ResourceManager 原子寫、path_security、nosniff）。

**代價/風險**：
- 新增 multipart 上傳端點 + 公開靜態服務面 → 需資安測試（路徑穿越、magic-byte、大小、IDOR）。
- `fetchTouka` 需支援 FormData（偵測跳過自動 Content-Type）。
- 備份體積略增（每使用者一張圖）。
- 公開讀取端點：知道 `user_id` 者可取該使用者頭像圖片（已評估為低風險顯示內容）。
- EXIF 於 v1 保留（地理資訊隱私的殘餘風險，家用單人情境可接受；未來 Pillow 重編碼可消除）。

## 實作計畫（Action Items，待批准後分階 TDD）

- **B1 後端上傳/服務**：`POST/DELETE /v1/auth/avatar`、`GET /v1/users/{id}/avatar`；純函式 `validate_image_bytes()`（magic-byte/大小/禁 SVG）、`avatar_path(user_id)`；經 ResourceManager 原子寫；`user_service` 設/清 `avatar_url`。測：magic-byte 接受 jpeg/png/webp、拒 SVG/文字/超大；path 穿越 user_id；IDOR（只能傳自己）；服務 404/正確 Content-Type。
- **B2 備份納入**：`manage_backup.py` `SANCTUARY_FOLDERS` 加 `avatars`；測備份含 avatars。
- **B3 前端**：`GeneralTab` URL 文字框 → 檔案選擇器 + 預覽（FileReader）+ 上傳（FormData）；`fetchTouka` 支援 FormData；Avatar 渲染加 `?v` 破快取；a11y（label/aria）+ i18n 新鍵 + 失敗 toast。
- **B4 收尾**：openapi + api.ts 重生；錯誤碼進 SSOT；本 ADR 翻 ✅ 已接受、更新 CHANGELOG + SSOT_MASTER_MAP（avatar 檔案儲存權威列）；guard_all/vitest/pytest/smoke 全綠 + 對抗式複審。

## 相關 ADR

- 建基於 **ADR-0078**（ResourceManager 原子寫/韌性）、**ADR-0033**（資安加固，擴充 avatar MIME 白名單/大小/symlink 防護）、**ADR-0014**（錯誤碼 SSOT）。
- 參照 **ADR-0003**（身分物件化，上傳綁定 JWT 身分無 IDOR）、**ADR-0104**（破壞性重置——未來釐清 reset 是否清頭像）。
