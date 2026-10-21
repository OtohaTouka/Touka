# ADR-0074: Skill 探索中心真技能策展目錄與釘版安裝（Curated Real-Skill Catalog & Pinned Install）

## 狀態

✅ 已接受（Phase A 後端＋Phase B 前端 UI＋Phase C 策展內容/工具 皆已實作；2026-06-07 新增「版本追蹤」擴充，見下節）

## 日期

2026-06-04

## 背景

ADR-0072 讓「探索中心」能管理**人格提示詞預設**（persona `system_prompt`）。但官方 [anthropics/skills](https://github.com/anthropics/skills) ＋社群的**真能力型 `.skill`**（含 `scripts/` 可執行腳本）是另一回事：它們走 ADR-0066 的真安裝路徑（`install_skill_from_url` 下載單一 `.skill` zip → 驗證 → 落地 `config/skills/<id>/`）。問題是官方 skill 是 **repo 裡的資料夾**（`tree/main/skills/<name>`，含 `SKILL.md`+`scripts/`+`references/`），非單一 zip → 現有安裝器不夠。

## 問題

1. **真技能含可執行腳本** → 安裝是信任層級的躍升（裝＝腳本進系統、agent 用到時在伺服器執行）。
2. 官方 skill 為 repo 資料夾、非單一 `.skill` → 需「資料夾 → skill」能力。
3. 若開放「貼任意 GitHub 資料夾 URL」→ 需把 `api.github.com` 加入白名單以列舉資料夾、任何 repo folder（含腳本）都能裝，攻擊面大。

## 決策

採「**策展＋逐檔釘 commit-SHA**」最安全路線，鏡像 ADR-0072 釘版模式、最大化重用 `install_skill_from_url` 全套防護。

- **策展目錄 SSOT**：`config/skill_catalog.json.example`（tracked 種子，bootstrap 自癒植入；`SkillCatalogService.get_catalog()` 讀 `.json`→fallback `.example`→空）。每筆＝`{id,name,category,description,repo,commit(40-hex),base_path,files[],author,license}`，**列出確切檔案＋釘 commit**。
- **安裝器 `SkillService.install_skill_from_catalog(catalog_id)`**：只接受 catalog 內已審 id（**使用者不自填 URL**）→ 逐檔抓 `raw.githubusercontent.com/<repo>/<commit>/<base_path>/<file>`（**已在 allowlist、不可變、runtime 零 `api.github.com`**）→ 組裝 staging（逐檔 `resolve()`-containment 穿越檢查）→ 重用 `_validate_skill_dir` ＋抽出的共用 `_atomic_install_from_staging`（`.bak` 原子上線＋失敗回滾）落地。沿用 `follow_redirects=False`＋單檔 20MiB＋總量 50MiB＋檔數 50 上限。
- **Route**：`GET /v1/skills/catalog`（`_require_skills_role`）、`POST /v1/skills/catalog/install`（`_require_owner_role`）。
- **授權準入**：只收開源許可 `{Apache-2.0, MIT, BSD-3-Clause}`；**避開** source-available 的文件類（pdf/docx/pptx/xlsx）；每筆 entry 的 `files[]` 含該 skill 的 `LICENSE`（保留署名）。初始策展（mcp-builder / skill-creator / webapp-testing / gh-address-comments）皆 Apache-2.0。
- **策展工具**（Phase C，dev-time `scripts/`）：用 `api.github.com` git-trees API 列檔產出 catalog 條目；**嚴禁進 runtime `src/`**（runtime 仍只 raw）。

## 後果

- **正面**：真技能可一鍵安裝；攻擊面從「任意網域任意路徑」縮到「策展白名單 id ＋ 釘 commit 的固定 raw host」；100% 重用既有真安裝防護（DRY，`_atomic_install_from_staging` 兩安裝器共用）。
- **信任模型（誠實揭露）**：真技能含**可執行腳本**，根本防線**不是**執行期掃描，而是「**只裝人工已審、commit-pinned 的策展來源**」＋前端「⚠️ 含可執行腳本」警示＋Owner/admin 二次確認。
- **取捨**：runtime 零 `api.github.com`（換得零新 SSRF 面），代價是 catalog 須列出確切 files（由 dev-time 策展工具產出）。
- **Phase A 狀態**：`.example` 先為空 `{"catalog": []}`（不列無法安裝的項目）；Phase C 充實真內容＋真 SHA。

## 版本追蹤擴充（Provenance & Update Detection）（2026-06-07）

catalog 安裝後需能判斷「已裝版本是否落後於目錄釘的新版」並一鍵升級。設計（TDD/SOLID/SSOT/DRY）：

- **安裝來源 SSOT（provenance sidecar）**：`install_skill_from_catalog` 於落地前在技能目錄寫入 `.touka_source.json`＝`{catalog_id, commit, repo, installed_at}`（隨 `_atomic_install_from_staging` 原子上線）。**刻意用 sidecar 而非改寫上游 `SKILL.md`**，保持目錄檔案對 pin 的忠實（利於日後逐檔雜湊）；自建/URL 安裝無此檔。
- **暴露**：`list_skills()` 增 `source_catalog_id` / `source_commit`（`load_config` 連 sidecar 一併快取）；端點仍回 plain dict，**不動 OpenAPI**。
- **狀態衍生（前端純函式）**：`computeCatalogStatus(entry, installed)`（`ToukaChat/src/lib/skill_version.ts`）比對「已裝 `source_commit`」與「目錄 `commit`」→ `not_installed` / `up_to_date` / `update_available`（無 provenance 視為最新、不催更新）。狀態由兩 SSOT 衍生、不另存。
- **升級＝重裝**：UI 對 `update_available` 顯示「可更新」徽章＋「升級至新版」鈕，呼叫既有 `install_skill_from_catalog`（覆寫＝升級，sidecar 更新為新 commit）。DRY，無新安裝路徑。
- **資料主權**：僅寫 `config/skills/<id>/`，不觸碰任何 agent 的 `data/`。

## 內容完整性擴充（Per-File SHA-256）（2026-06-07）

釘 commit 保證來源「不可變」（前提：GitHub 誠實）。為加上防禦縱深（防 GitHub 帳號被黑、MITM、儲存損毀），catalog 改為**逐檔釘 sha256**（TDD/SOLID/SSOT/DRY）：

- **Schema（SSOT-clean）**：`SkillCatalogEntry.files` 由 `list[str]` 改為 `list[CatalogFile]`，每檔 `{path, sha256}`——路徑與雜湊同處一筆，無重複來源；`sha256` 由 `SHA256_RE`（64-hex）驗證格式。
- **安裝驗證**：`install_skill_from_catalog` 逐檔下載後，`hashlib.sha256(data).hexdigest()` 須等於釘值，不符即 `VALIDATION_ERROR`、不落地（於路徑穿越檢查之後、寫檔之前；失敗不留半成品）。
- **策展工具**：`scripts/curate_skill_catalog.py` 逐檔抓 raw 內容算 sha256，emit `files:[{path,sha256}]`（dev-time，仍只 raw、不進 runtime `src/`）。
- **種子**：`.example` 4 entry 全 40 檔已填真實 sha256；`test_skill_catalog_seed_integrity` 驗每檔 64-hex。
- **取捨**：commit pin 已是主要保證，逐檔 sha256 為其上的縱深；代價是 catalog 須含每檔雜湊（由策展工具自動產出）。

## 相關 ADR

- **ADR-0066**（Skill Anthropic 標準、真 `.skill` 安裝路徑）——本 ADR 擴充其安裝來源。
- **ADR-0072**（Skill Hub 釘版同步）——釘 commit／`.example` 種子／bootstrap 自癒的母版。
- **ADR-0033**（InfoSec：SSRF／`follow_redirects=False`）、**ADR-0069**（創建主權 Guard #29）。
