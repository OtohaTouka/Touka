# ADR-0108：備份下載時即時加密 — WinZip-AES-256、明文不出網、7-Zip 可開

- **狀態**：✅ 已接受（2026-06-23 經 TDD 實作完成並全綠驗證：guard 37/37、後端 pytest（含 crypto round-trip／下載端點）、vitest、smoke 24/24；對抗式安全複審）（依賴宣告載體修訂 by [ADR-0117](0117-uv-workspace-single-lock-migration.md)：pyzipper 自 requirements.in 遷 pyproject/單一 uv.lock；加密機制不變）
- **日期**：2026-06-23
- **影響範圍**：ToukaProxy（`requirements.in` +pyzipper、`services/backup_crypto.py`、`routers/system.py` 下載端點）＋ ToukaChat（`EnvSettings.tsx` 下載 UI、i18n）
- **關鍵字**：backup download, on-the-fly encryption, WinZip AES-256, pyzipper, 機密不出網, 設定頁資料生命週期 軌1 Phase 2, 建基於 ADR-0071（依賴鎖）/ADR-0078（ResourceManager）/Phase 1（BK1-BK3）

## 背景（Context）

設定頁「資料生命週期·軌1」Phase 1 已補齊系統備份的還原密碼閘（BK1）、刪除（BK2）、磁碟統計（BK3）。但仍缺「**下載備份**」。研究（4 探子）挖出關鍵風險：**備份 zip 含 `.env` 明文（所有 API 金鑰）且無遮罩**——若直接提供下載端點，等於用 HTTP 把全系統機密外送。

維護者決定：**下載＝加密**；並選擇**標準加密 ZIP（pyzipper）**方案（而非自訂 `.zip.enc` + 專屬解密工具），使下載產物可被 **7-Zip / WinRAR 直接以密碼開啟**，對非技術使用者更友善。

## 決策（Decision）

### D1 — 下載時即時加密（非加密儲存）
- 伺服器端**保留明文 zip**：內部還原（Phase 1 流程）與 ADR-0104「重置前備份優先」依賴它，且**明文 zip 永不以明文離開伺服器**。
- 管理員**下載**時提供**密碼短語（≥8 字）**→ 後端把明文備份**即時重新封裝**成 WinZip-AES-256 加密 zip → 串流回傳。
- 還原仍走伺服器端明文檔（**Phase 1 不變**）；下載產物屬離線異地備援。
- 要還原一個下載過的加密檔：用 **7-Zip / WinRAR 輸入密碼解開**後放回 `backups/` 再還原（無需專屬工具）。

**為何不「加密儲存」**：會破壞內部 `create_backup()`（重置備份優先流程無密碼）＋既有明文備份，blast radius 大。下載時加密只動下載路徑、對既有流程零干擾、且對既有備份也適用。

### D2 — 加密庫：pyzipper（標準 WinZip-AES-256）
- 維護者選 **pyzipper**（而非已是相依的 `cryptography` + 自訂格式），換取「7-Zip/WinRAR 直接可開」的 UX。新增相依經 ADR-0071 鎖檔流程（`requirements.in` → `lock_requirements.py` 釘版＋雜湊；pyzipper 0.4.0 + pycryptodomex）。
- `services/backup_crypto.encrypt_backup_zip(plain_zip, passphrase)`：逐條目讀取明文 zip、以 `AESZipFile(WZ_AES, nbits=256)` **flat re-zip**（非巢狀 → 開啟即見 `.env`/`data/`/`config/`）寫入暫存檔；逐條目處理 → 記憶體上限為最大單一條目，大型備份不整包進 RAM。

### D3 — 端點與前端
- `POST /system/backups/{filename}/download`（admin、`verify_origin` CSRF、`validate_no_symlink_escape` + 僅 `.zip` 路徑守衛、body `passphrase` `min_length=8`）→ CPU 密集加密經 `run_in_threadpool` 移出事件迴圈 → `FileResponse`（`background=resource_manager.remove_file` 串流後清暫存）。
- 前端 `EnvSettings.tsx`：每列「下載」鈕 → 密碼短語對話框（≥8 才啟用確認）→ POST → `blob` → anchor 下載 `{name}_encrypted.zip`；明顯提示「含全系統機密、已加密、請妥善保存密碼」。

## 後果（Consequences）

- ✅ 下載產物加密（含 `.env`），明文 zip 永不出網；7-Zip/WinRAR 可開、無專屬工具。
- ✅ 對 Phase 1 還原/刪除/統計與內部備份流程零影響。
- ⚠️ 新增執行期相依 pyzipper（+pycryptodomex）；經鎖檔＋pip-audit（守衛 #22/#30）。
- ⚠️ 密碼短語遺失＝該下載檔無法開啟（設計如此；伺服器不持有）。密碼短語永不落地/不記 log。
- ⚠️ 加密為重新壓縮（CPU），僅 admin 隨需下載觸發，可接受。

## 對抗式安全複審

下載端點繞過/路徑穿越/CSRF/非-admin/passphrase 下限與 `backup_crypto` round-trip/錯密碼偵測經複審與測試覆蓋。詳見 SECURITY.md「軌1 Phase 2」。

## 替代方案（Rejected）
- **cryptography + `.zip.enc` 自訂格式 + 離線 `decrypt` CLI**：零新相依、AEAD 串流，但需專屬工具解密 → UX 較差，維護者否決。
- **加密儲存（所有備份恆加密）**：破壞內部無密碼的「備份優先」流程與既有明文備份，blast radius 過大。
