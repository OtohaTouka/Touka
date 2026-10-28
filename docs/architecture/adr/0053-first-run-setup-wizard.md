# ADR-0053: 首次執行設定精靈 (First-Run Setup Wizard)

**狀態**：✅ 已接受（**延伸／修訂：[ADR-0118](0118-setup-wizard-reliability-and-embedding-lifecycle.md)**——金鑰持久化/即時生效/失敗必顯示/宿主資料+工作區目錄設定/嵌入模型下載+切換防呆）  
**日期**：2026-05-28  
**影響範圍**：ToukaProxy、ToukaMemory、ToukaChat

> 🔧 **臨時密碼揭露機制已被安全修復 DA21（2026-06-06，見 `SECURITY.md`／`CHANGELOG.md`）改寫**：原決策「以 `secrets.token_hex(16)` 產生 32 字元臨時密碼、`logger.warning` 印至 console」→現行以 `secrets.token_urlsafe(16)` 產生、**寫入一次性檔案** `get_initial_admin_password_file()`（`resource_manager.write_file_text`），`logger.warning` 只印**檔案路徑**——刻意避免輪替 log 永久保留有效 admin 憑證。設定精靈其餘決策（is_temp_password/SetupService/嵌入模型 SSOT/DISPATCH_TOKEN sentinel）仍有效。

---

## 背景

Touka 系統準備開源，但新使用者面臨多個設定障礙：

1. **Admin 密碼不透明**：bootstrap 時建立的預設密碼為硬編碼字串，使用者不知道需要更改。
2. **DISPATCH_TOKEN 已公開**：預設值 `touka_dispatch_secret_2026` 在原始碼中公開，對生產環境是安全風險。
3. **TOUKA_AUTH_SECRET 未持久化**：若 `.env` 未設定此值，每次重啟後所有 JWT 全部失效，導致使用者 Session 中斷。
4. **Embedding 模型硬編碼**：預設的 `all-MiniLM-L6-v2` 對 CJK 語言效果不佳，但無文件引導。
5. **資料路徑硬編碼**：`SANCTUARY_DATA_PATH` 預設指向開發者目錄，不適合一般使用者。

---

## 決策

### 後端（ToukaProxy）

1. **`UserRecord.is_temp_password`**：在 Pydantic 模型新增 `is_temp_password: bool = False` 欄位。向後相容：舊有 users.json 讀取時自動補預設值 `False`。

2. **Bootstrap 臨時密碼**：全新環境啟動時（無 admin 使用者），自動以 `secrets.token_hex(16)` 產生 32 字元臨時密碼，並以 `logger.warning` 印至 console，同時建立 `is_temp_password=True` 的 admin UserRecord。寫入透過 `_atomic_users_write()` 確保原子性（禁令 21）。

3. **密碼更改清除旗標**：`AuthService.update_password()` 成功後，將 `is_temp_password` 設為 `False`。

4. **`SetupService`（SRP / DIP）**：新建 `ToukaProxy/src/services/setup_service.py`，封裝所有設定狀態偵測與寫入邏輯。

5. **Setup API 端點**（`ToukaProxy/src/routers/setup.py`）：
   - `GET /v1/admin/setup/status`：回傳系統設定完整性（需 `require_admin` Depends，Guard #22）。
   - `POST /v1/admin/setup/apply`：寫入 `.env` 設定（需 `require_admin` Depends）。
   - 使用 `python-dotenv` 的 `set_key()` 寫入，確保原子性。

### ToukaMemory

6. **`EMBEDDING_MODEL` SSOT**：在 `config_utils.py` 新增 `get_embedding_model_name()` 函式讀取 `EMBEDDING_MODEL` 環境變數，預設 `all-MiniLM-L6-v2`。`VectorStore.__init__` 與 `MemoryManager.get_vector_store()` 均透過此函式取值，成為唯一事實來源（SSOT）。

### 前端（ToukaChat）

7. **`useSetupStatus` Hook**：僅在 `user.role === 'admin'` 時呼叫 `/v1/admin/setup/status`，靜默處理錯誤。

8. **持續警告 Banner**（`TempPasswordBanner`）：當 `is_admin_temp_password: true` 時顯示紅色全寬 Banner，持續提示 admin 更改臨時密碼，直到密碼被更換。
   - 〔2026-10-23 補記〕改成左側邊欄最上面的紅色小卡（`SidebarSetupNotices`，狀態在 `SetupNoticesProvider`），只在有左側邊欄的頁面顯示（設計系統頁沒有），仍不能關；「變更密碼」直接打開精靈的管理員密碼步驟。舊的全寬 Banner 放在最外層左右排列的版面裡，會變成左側邊欄外的大直欄，已移除。

9. **Setup Prompt Banner**（`SetupPromptBanner`）：當設定尚未完整時（預設 Token、未持久化 Secret 等）顯示 amber Banner，可點擊「立即設定」或「稍後設定」。
   - 〔2026-10-23 補記〕同樣改成左側邊欄裡的小卡，出現條件不變；「稍後」記在 `AuthGuard` 裡的提供者，同一個分頁切換畫面都有效，重新整理、登出、登入過期或換使用者後再出現。精靈關掉時重讀狀態（重讀失敗保留上次的警告）。有任一張卡時，桌面側欄收合鈕與手機選單鈕上有紅點，按鈕名稱說明側欄裡有設定警告。〔2026-10-28 補記〕上導覽列拿掉後，桌面版的紅點改在左側邊欄收起時主區左上角的展開鈕上（側欄展開時警告卡本來就看得到）。

10. **Wizard 容器（OCP 設計）**：`SetupWizard.tsx` 使用 `setupWizardConfig.ts` 的步驟設定陣列驅動，新增步驟只需修改設定陣列（Open-Closed Principle）。

11. **7 個 Wizard Step 元件**（`ToukaChat/src/components/setup/steps/`）：
    - Step 1：系統健康 + Embedding 模型語言感知選擇
    - Step 2：Admin 密碼更改
    - Step 3：API Keys 狀態確認
    - Step 4：DISPATCH_TOKEN + AUTH_SECRET 自動產生
    - Step 5：資料路徑設定（可選）
    - Step 6：建立使用者帳號（可選）
    - Step 7：設定摘要與完成
    - 〔2026-10-23 補記：改密碼後重新登入〕Step 2 改完密碼後，後端撤銷這個人所有舊卡（ADR-0144：改密碼後一律請重新登入），精靈因此不再直接前進，而是記下接續點（`useSetupResume`：瀏覽器裡只存使用者編號、下一步與時間，1 小時內有效、用過即刪），改畫「請重新登入」與「前往登入」，並隱藏上一步。同一位管理員登入後、而且狀態顯示還有沒設定完的項目，精靈自動開在下一步；都設好了就丟掉接續點。背景請求常在幾十秒內先觸發「登入已逾期」，而提示會隨 Toaster 一起卸載，所以登入頁看到有效的接續點時自己說明（只看不取）。管理員密碼已不是臨時密碼時（後端看第一位管理員，和側欄警告同一個判準），Step 2 多一個「保留目前密碼」；瀏覽器不讓存接續點或超過 1 小時時，走「立即設定 → 保留目前密碼」。前進到 Step 7 時重讀狀態，摘要才不會用精靈打開時的舊狀態。修正前精靈直接前進，下一個請求 401、續期也 401，在中途被帶回登入頁；Step 2 又不能略過，重新打開精靈會再逼改密碼，精靈走不完。

---

## 後果

### 優點

- **安全性提升**：新使用者在第一次啟動時即被引導更換所有高風險預設值，顯著降低以預設設定上線的風險。
- **使用者體驗改善**：結構化的 7 步 Wizard 取代了需要手動編輯 `.env` 的繁瑣流程。
- **語言感知**：Wizard 推薦 CJK 使用者切換至 `paraphrase-multilingual-MiniLM-L12-v2`，避免 Embedding 品質下降。
- **持續提醒機制**：臨時密碼 Banner 不可關閉（直到真正更改），避免使用者忽略安全警告。（2026-10-23 起在左側邊欄，仍不可關閉；側欄收起時看不到卡片，改由選單按鈕上的紅點提醒。）

### 權衡

- **`os.environ` 直讀（TID251 豁免）**：`SetupService` 的狀態偵測需要讀取目前生效的環境變數（非 Pydantic Settings 快取值），因此各行加 `# noqa: TID251` 並登記至 NOQA_REGISTRY。
- **`B008` 豁免**：Setup 路由使用標準 FastAPI `Depends()` 依賴注入模式，登記至 NOQA_REGISTRY。
- **`TRY003` 豁免**：`SetupService.apply()` 的輸入驗證錯誤訊息需要說明具體欄位，採用局部變數 + `# noqa: TRY003` 模式。

---

## 相關 ADR

- [ADR-0018](0018-multi-user-auth-and-security-hardening.md)：多使用者認證加固（is_temp_password 的延伸）
- [ADR-0023](0023-dynamic-ssot-suppression-with-noqa.md)：Dynamic SSOT Suppression（noqa 豁免登記規範）
- [ADR-0034](0034-endpoint-auth-dependency-guard.md)：端點認證守衛（Guard #22 相容性）
- [ADR-0015](0015-environment-variable-sovereignty.md)：環境變數主權（EMBEDDING_MODEL SSOT 延伸）
