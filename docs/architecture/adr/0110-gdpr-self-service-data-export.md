# ADR-0110：使用者自助 GDPR 完整資料匯出 — 加密 ZIP、僅本人、跨子系統唯讀事實匯出

- **狀態**：✅ 已接受（2026-06-24；Phase 1 後端 + Phase 2 前端經 TDD 完成並全綠驗證：guard 37/37、後端 pytest 2317（含 export_facts 動作/脫敏/加密 roundtrip/symlink 逃逸略過/端點密碼閘）、vitest 771（GeneralTab 匯出 UI + hook）、smoke 24/24、openapi+api.ts 重生；對抗式安全複審（5 個維度，修 4 項））
- **日期**：2026-06-24
- **影響範圍**：ToukaMemory（`tools/__init__.py` 新增唯讀 `export_facts` 動作）＋ ToukaProxy（`services/gdpr_export_service.py`、`routers/auth.py` `/auth/export-my-data`）＋ ToukaChat（Phase 2：GeneralTab 匯出 UI／i18n）
- **關鍵字**：GDPR, data portability, self-service export, encrypted zip, AES-256, pyzipper, JWT self-only, password re-entry, CSRF, read-only, 跨子系統記憶匯出, 設定頁路線圖 軌2, 建基於 ADR-0107（自助清空）/ADR-0108（加密）

## 背景（Context）

ADR-0107（自助清空個人資料）已將「使用者自助資料匯出（GDPR 可攜權）」明列為**獨立未來項**——即本軌。GDPR Art.20 資料可攜要求：資料主體可取得其個人資料的機器可讀副本。

研究（5 探子）關鍵發現：
- 使用者個資散落多處：profile（users.json）、頭像、UI 對話（`ui_sessions.db`，以 `agent_id ∈ owned_agents` 範圍化）、專案（`owner_user_id`）、各自有 agent 的記憶。
- **記憶橫跨 ToukaMemory 子系統**（行程內函式庫，非跨網路服務）：legacy（對話歷史）由 vault 直讀；**facts（語意事實記憶）與 vector（嵌入）為 ToukaMemory 私有 DB**，ToukaProxy 僅能經 `memory_tools()` 閘道存取。
- 軌1 的 `encrypt_backup_zip`（AES-256）、`FileResponse + BackgroundTask` 清理、信號量、時間 SSOT 皆可複用。

## 決策（Decision）

### D1 — 端點：`POST /auth/export-my-data`（自助、無 IDOR、密碼閘、強制加密）
身分取自 JWT（`get_current_user`，僅本人、無路徑參數→無 IDOR），`verify_origin`（CSRF），密碼再驗（`verify_password`，被竊 access token 無法單獨觸發——DA25 模式）。回傳 `FileResponse`（`application/zip`）、串流後 `BackgroundTask` 清理暫存。**強制加密**（密語 ≥12，沿用 ADR-0108）——匯出含對話與記憶，屬高敏個資；明文永不離開伺服器。

### D2 — 服務：`gdpr_export_service.py`（收集 → 明文 ZIP → 加密；**純唯讀**）
SRP 抽出於 auth.py 外。`create_encrypted_export()`：async 收集 → `run_in_threadpool` 建明文 ZIP（檔案以 `zf.write` 由磁碟串流，不整檔入記憶體）→ 信號量下 `encrypt_backup_zip` → **明文暫存即刪（即使加密失敗）**。完全不寫入/修改任何既有資料（含 agent/soul），符合常駐約束。

### D3 — 匯出範圍（「完整」定義；含/排除明確化）
**含**：`profile.json`（脫敏**白名單**——顯式列舉可攜欄位，將來新欄位不會意外洩漏）、頭像、`conversations.json`（僅本人自有 agent）、`projects.json`（僅本人擁有）、每個自有 agent 的 `facts.json`（語意記憶）與 `files/`（個人硬碟）、`manifest.json` + `README.txt`（GDPR 知情）。
**排除**（README/manifest 載明理由）：帳號機密（密碼雜湊／所有 token/JTI——安全，非可攜個資）、vector 嵌入（衍生、非人類可讀；其來源 facts 已含）、agent 人格設定 soul/system-prompt（系統配置）、全域系統設定（非個人資料）。

### D4 — 跨子系統：ToukaMemory 新增唯讀 `export_facts` 動作
facts 為 ToukaMemory 私有 DB → 在 `memory_server/tools/__init__.py` 加 `export_facts` 動作（`MEMORY_ACTIONS`），經既有 `memory_tools()` 閘道存取（SSOT，非硬耦合 DB schema）。依 `identity.agent_id` 嚴格範圍化（含已封存），**唯讀、不修改任何既有資料**。記憶系統不可用時，ToukaProxy 端優雅降級（該 agent 略過 facts，其餘照常匯出）。

## 後果（Consequences）

- ✅ 滿足 GDPR 資料可攜；使用者一鍵取得本人完整資料的加密副本。
- ✅ 複用軌1 加密/串流/清理機制；跨子系統走閘道 SSOT，無硬耦合。
- ✅ 白名單脫敏 + 僅本人範圍化（對話/專案/agent）→ 杜絕機密與他人資料外洩。
- ⚠️ 大型個人硬碟 → 加密時單條目讀入記憶體（與 ADR-0108 下載同特性）；信號量(2) 限併發。
- ⚠️ legacy 對話歸檔與 vector 嵌入不另行匯出（前者為 UI 對話的衍生副本、後者為衍生資料）。

## 對抗式複審
身分/IDOR/密碼閘/CSRF/脫敏/範圍化/加密/暫存清理經測試覆蓋；Phase 1 後完整對抗式複審見 SECURITY.md「軌2」。

## 替代方案（Rejected）
- **不加密純 ZIP**：含高敏對話/記憶 → 否決（經維護者決定加密）。
- **直接讀 ToukaMemory 私有 DB**：硬耦合其 schema、繞過閘道 SSOT → 改用 `export_facts` 閘道動作。
- **僅對話不含 facts**：不符「完整」 → 經維護者決定納入 facts。
