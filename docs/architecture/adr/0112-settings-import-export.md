# ADR-0112：設定快照（Config Snapshot）— 可攜、零機密的設定匯入／匯出

- **狀態**：✅ 已接受（2026-06-24；**Phase 1 後端＋Phase 2 前端均經 TDD 完成並全綠驗證**，兩階段各經對抗式複審）
- **日期**：2026-06-24
- **影響範圍**：ToukaProxy（新 `services/settings_snapshot_service.py`＋`routers/system.py` 兩端點）＋ ToukaChat（Phase 2：EnvSettings 設定快照卡／i18n）
- **關鍵字**：settings import export, config snapshot, portable config, secrets-free, mask, merge vs replace, dry-run preview, pre-import backup, hot-reload, admin-only, CSRF, password re-entry, 設定頁路線圖

## 背景（Context）

設定頁路線圖列有「設定匯入匯出」：讓 admin 把系統的**可調設定**匯出成檔、再匯入到另一機器（克隆設定／版本化／還原已知良好基線）。Touka 已有兩個相鄰但**不同**的機制：

- **系統備份**（ADR-0108/9）＝全加密災難復原快照（含 `.env`＋資料庫＋靈魂＋使用者）。
- **GDPR 匯出**（ADR-0110）＝單一使用者自己的對話／記憶。

兩者皆非「可攜、跨機器的設定旋鈕」。研究（4 維平行 map）盤點 `ToukaProxy/config/` 約 20 個 JSON：約 14 個是 admin 可調的設定旋鈕，其餘為機密／使用者資料／本機綁定／執行期狀態。

## 決策（Decision）

新機制命名 **設定快照（Config Snapshot，`settings_snapshot`）**，與「備份」區隔。維護者決定三件事：**合併為主＋逐區可選覆蓋**、**純 JSON 為主（加密選用）**、**v1 只含 config JSON（不含 .env 微調）**。

### D1 — 範圍（單一 `SECTIONS` 註冊表 SSOT）
`settings_snapshot_service.SECTIONS` 宣告每區的：來源檔、合併策略、匯出脫敏／匯入清洗、熱載入器、是否需重啟。**納入（14 區）**：autonomy、permissions、auth_policies、sliding_window、group_whitelist、instruction_templates、model_pricing、model_context_windows、models_blacklist、models、connections、mcp_servers、skills_custom、backup_schedule。

**EXCLUDE 硬規則**（永不序列化）：`.env` 所有機密（`*_API_KEY`／`TOUKA_AUTH_SECRET`／心跳·派發 token）、`users.json`（PII＋密碼雜湊）、`identity_map` 與 `permissions.agent_mappings`（綁定**本機 agent ID**，跨機器是鬼參照）、`heartbeat_whitelist`、`errors.json`、靈魂／個人資料／資料庫、`mcp_hub.json`（80KB 公開快取、會過期）、執行期欄位（`models.last_seen`、`backup_schedule.last_run_epoch`、健康狀態）。

### D2 — 機密縱深防禦（遮罩 SSOT＋匯入跳過）
匯出時：MCP `env` 值與連線 `apiKey` 經 `secrets.mask_value`／null 化（保留鍵名，讓匯入端知道要填哪些）。匯入時：遇遮罩佔位值（`secrets.is_masked`）一律**跳過不覆寫**——絕不用 `••••` 假值蓋掉目標既有真金鑰；被跳過者列入回應 `skipped_secrets` 提示 admin 手動補填。

### D3 — 格式：單一 JSON envelope＋完整性指紋
設定極小且結構化 → **單一 JSON**（可 git diff／審閱／版本化）勝過 ZIP-of-files。`{schema_version, export_kind, manifest{exported_at, sections, excluded, sha256}, sections{key:{data}}}`。每區包 `{data}` 以利 v2 加欄不破 v1。`manifest.sha256` 對正規化 sections 取雜湊＝傳輸完整性／防竄改。**加密選用**：`encrypt=true` 時把該 JSON 封進 zip 後以 ADR-0108 `encrypt_backup_zip`（AES-256）串流下載。

### D4 — 匯入語意：合併為主、安全網齊備
- **合併（預設）**：dict 區深合併（保留未提及鍵）；list 區（models/connections/skills）依 id upsert；`models` 為 `only_existing`（只套設定到目標已存在的同 id 模型，未知 id 警告略過，不造幽靈條目）。**覆蓋（逐區可選）**：整區以快照內容取代。
- **驗證管線**（任一失敗即整批拒絕）：`schema_version ≤ 本機`、`sha256` 比對、體積上限（envelope 2MiB／區 512KiB）、結構檢查。沿用既有 `VALIDATION_ERROR`（不新增錯誤碼）。
- **乾跑預覽**（`dry_run=true`）：只驗證與計算（免密碼、唯讀、不寫入），供前端確認前預覽 applied／warnings／skipped_secrets／requires_restart。
- **匯入前自動備份**：套用前先 `create_backup()`（與排程共用執行鎖）作還原點，檔名回傳；**無自動回滾**（不滿意時手動經 `/system/restore/run` 還原）。
- **熱載入**：autonomy／permissions／models／sliding_window 套用後即時 reload；`mcp_servers`／`model_pricing`／`model_context_windows`（啟動載入）列入回應 `requires_restart`。

### D5 — 端點與授權
`POST /system/settings/export`（admin＋CSRF；唯讀不洩漏機密故無密碼閘）、`POST /system/settings/import`（admin＋CSRF＋**套用時密碼重輸** DA25；`dry_run` 免密碼）。授權＝系統 `admin` 角色（沿用 `_require_admin`／`get_current_user`），非 `Owner` 使用者角色。

### 替代方案（Rejected）
- **ZIP-of-files**：設定小且需 diff → 單一 JSON 較佳（加密時才包 zip）。
- **一律加密**：設定快照零機密 → 加密是多餘儀式且失去可審閱性；改為選用。
- **含 .env 微調旋鈕**：需重啟、跨基礎設施風險最高 → v1 排除，未來選配。
- **自動回滾**：複雜且易半套用 → 改「匯入前自動備份＋手動還原」。

## 後果（Consequences）

- ✅ admin 可把系統設定克隆到新機器／版本化／還原基線，constraint-clean（零機密、零使用者資料、不碰靈魂）。
- ✅ 合併為主＋乾跑預覽＋匯入前自動備份＋密碼重輸 → 對非技術擁有者 safety-first。
- ⚠️ 跨版本 schema 漂移由 `schema_version` 擋未來版＋ sha256 完整性緩解；未知區忽略不阻斷。
- ⚠️ 部分區（MCP／定價／視窗）需重啟才完全生效，回應明列 `requires_restart`，不靜默 stale。

## 驗證
build/validate/apply 全路徑＋機密回填＋only_existing 警告＋replace＋dry_run＋subset＋端點 admin/密碼/dry-run 閘皆經測試覆蓋（Phase 1：20 測，3.12 venv）。openapi+api.ts 重生；`guard_all` 37/37、pytest 2358、smoke 24/24。

**Phase 1 對抗式安全複審（5 個維度；25 findings → 逐項對抗式驗證 → 修 11、其餘駁回/by-design）**：
- **🔴 High 機密遺失**：`replace` 模式與「env=null」會以遮罩假值/null 覆寫真金鑰 → 把 `import_clean_fn` 從「丟棄遮罩」改為「**從目標既有值回填**」（merge＋replace 兩模式皆不丟金鑰；非 dict env 還原既有）。
- **🔴 High 機密漏遮罩**：`secrets.py` 遮罩正則過窄（僅 `API_KEY|SECRET|TOKEN|PASSWORD`）→ 擴充 `_KEY|CREDENTIAL|BEARER|PASSWD`（涵蓋 `ANTHROPIC_KEY` 等 MCP env），全系統遮罩 SSOT 受惠、無測試回歸。
- **🟠 Med 連線 URL 內嵌帳密**：`_export_connections` 去除 `https://user:pass@host` 帳密。
- **🟠 Med 併發競態**：匯入端點加 `_settings_import_lock` 序列化「備份→套用」全程（防 TOCTOU 互相覆寫）。
- **🟠 Med 無聲損毀**：`validate_snapshot` 增驗 list 區包裹鍵須為 list（防 replace 寫損毀／merge 逐字元略過）；`_upsert_list` 對損毀的既有非-list 包裹鍵縱深防禦。
- **🟠 Med 分類修正**：`model_pricing`/`model_context_windows` 實為惰性讀檔（零停機）→ 移除誤標的 `requires_restart`；`connections` 路由快取於啟動探測 → 改標 `requires_restart`。
- **🟢 Low**：`skipped_secrets` 改報「遮罩且目標無值可回填」之真待補填項；`mode` 改 `Literal`；模型匯入欄位顯式常數。
- **駁回/by-design**：cross-instance 懸空參照（執行期已優雅處理）、reloader 失敗無回滾（刻意，有匯入前備份＋requires_restart 揭露）、partial-apply（匯入前備份已記入 AUDIT log）。

**Phase 2 前端（EnvSettings 設定快照卡）**：`SettingsSnapshotCard`——匯出鈕（＋加密選用 checkbox／密語、blob 下載）；匯入選檔 → **自動乾跑預覽**（顯示涵蓋區／警告／待補機密／需重啟）→ merge/replace 模式切換（replace 顯破壞性警告）→「套用」開 Radix 確認框（密碼重輸）→ 套用 → 結果摘要（pre_import_backup／applied／requires_restart）。i18n zh/en（28 鍵×2）。聚焦的對抗式複審證實密碼/dry-run 安全面正確、無機密外洩；修 2 Med 狀態陳舊（預覽失敗清 stale preview、套用後改模式清 result 重啟循環）。前端 4 測；guard 37/37、vitest 813。
