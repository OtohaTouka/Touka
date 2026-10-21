# ADR-0113：Per-Agent 資料重置 — 清執行期資料、保留身分（ADR-0104 的安全泛化）

- **狀態**：✅ 已接受（2026-06-24；後端＋前端經 TDD 完成並全綠驗證，經對抗式安全複審）
- **日期**：2026-06-24
- **影響範圍**：ToukaProxy（`agent_reset/{guard,primitives,orchestrator}.py` 泛化、`routers/admin.py` 端點）＋ ToukaChat（`AgentDataResetCard` 置於 UsersTab／i18n）
- **關鍵字**：per-agent reset, data reset, identity preservation, is_protected_path, ownership guard, fail-closed, backup-first, password re-entry, type-to-confirm, ADR-0104 generalization, 設定頁路線圖

## 背景（Context）

設定頁路線圖列有「per-agent 資料重置」：讓 admin 把**單一指定代理人**累積的執行期資料重置回乾淨狀態，但**保留其身分**（重置後仍可用，故與「刪除代理人」區別）。研究發現系統已有 **ADR-0104 DebugAgent 破壞性重置**的上線加固堆疊（`guard`/`primitives`/`orchestrator`：`ALL_STORES` 註冊表、`ResetReport`、序列化鎖、備份優先、逐店完整性自檢），per-agent 重置是其**薄泛化**而非新建破壞性機器。

## 決策（Decision）

### #1 不可違反鐵則（雙重結構性防護）
**永不觸及代理人身分、永不波及其他代理人**，靠兩道獨立地板：① 每個清除原語以 `agent_id` 嚴格限定（SQL `WHERE agent_id=?`、per-agent Qdrant collection、沙箱路徑）；② **`is_protected_path` 檔案系統地板**（`is_relative_to`）物理性拒絕刪除 `data/soul/*`、`data/legacy/*`，不可繞過。

### D1 — 範圍（重用 `ALL_STORES`；保守不碰上傳檔）
**重置**（執行期，按 agent_id）：對話/工作階段、訊息回饋、大腦選擇、統計遙測、任務板、docgen 產出、**記憶 facts＋向量**（經 importlib 橋接 `MemoryManager.purge_agent`）、context 快照——即既有 `ALL_STORES` 八項，皆已 agent_id 化＋protected-path-aware。**保留**：靈魂 `soul/`（受保護路徑）、`legacy/` 歸檔（受保護路徑）、**自訂模型 `system_prompt.md`＋`config.json`（Level-2 身分，由清除原語範圍排除、非保護路徑→須回歸測試）**、專案稽核、代理人註冊。**擁有者裁示：保留個人硬碟「使用者上傳檔案」**（既有原語只清 `outputs/`／`missions/` 子目錄，天然不碰上傳檔，故無需新增 `purge_personal_drive`）。

### D2 — 安全架構：反轉守衛＋共用核心（不弱化 DebugAgent 路徑）
- **唯一新關鍵碼＝反轉的擁有權守衛 `assert_agent_resettable(candidate, actor)`**：要求目標**存在**（`_is_real_persona_strict`，**fail-closed**：列舉失敗→拒絕，與 DebugAgent 守衛的「不確定→拒絕重置」反向但同樣保守）＋**非系統保護**（`SYSTEM_PROTECTED_AGENTS`＝DebugAgent…）＋**被 actor 擁有**（`is_owner`；admin 絕對放行，保留檢查以利未來非-admin Owner 路徑＋稽核）。**刻意不與 `assert_resettable` 合併成可配置函式**（單一可被告知「誰可重置」的函式會擴大全系統最危險路徑；兩個窄函式更安全）。
- **清除原語加 `guard` 參數（預設＝`assert_resettable`）**：DebugAgent 路徑零變動（預設閘、既有測試全綠）；per-agent 由 orchestrator 傳入綁定 actor 的 `assert_agent_resettable` 閉包，**每個原語仍各自過閘**（縱深防禦不變）。orchestrator 抽 `_execute_reset(normalized, guard, do_backup)` 共用核心（鎖／備份／清除／自檢），兩入口僅各自驗證後委派。`role` 固定非-Owner，避免 `is_toukasoul` 的 Owner 捷徑誤判 vault 路徑類型（經實機驗證：soul/custom 皆以物理路徑正確解析）。
- **端點 `POST /admin/agents/{target}/reset`（admin only）**：比 DebugAgent 端點**更嚴**——admin 角色 → **CSRF Origin** → **密碼重輸（DA25）** → **雙重確認**（`confirm=true` 且 `confirm_agent_id` 與 target 字面相符；前端打字輸入）→ `assert_agent_resettable` → **備份優先**。任一不符即零刪除中止。

### D3 — 前端（UsersTab，admin 管理面）
`AgentDataResetCard`：列出可重置代理人（`is_agent` 且非 DebugAgent）→ 每列「重置資料」→ Radix 對話框明列**將清除 vs 將保留**（透明）＋**打字輸入代理人 ID**（防點錯列，遠優於核取方塊）＋**密碼**→ 套用 → 逐店報告＋「刻意保留」揭露。

### 替代方案（Rejected）
- **統一守衛加 `allowed_set` 參數**：擴大最危險路徑 → 否決，改兩個窄函式。
- **新增 `purge_personal_drive`（全清個人硬碟）**：擁有者選保守（保留上傳檔）→ v1 不需。
- **記憶新增 `purge_facts` MEMORY_ACTION**：重置走 `MemoryManager.purge_agent` 直接橋接（已 agent_id 化、不可用即 raise）→ 多餘且增破壞性面 → 否決。
- **admin＋非-admin Owner**：開啟跨使用者波及面 → v1 admin-only（保留 `is_owner` 以利日後一行擴充）。

## 後果（Consequences）

- ✅ admin 可把單一代理人重置回乾淨狀態、保留身分、constraint-clean；薄泛化使新碼面最小。
- ✅ 身分保護為**結構性強制**（protected-path 物理地板＋per-agent 範圍＋原語各自過閘），非僅意圖。
- ✅ DebugAgent 路徑（ADR-0104）零行為變動、既有測試全綠。
- ⚠️ 自訂 `config.json`/`system_prompt.md` 非保護路徑，僅靠原語範圍排除 → 以**回歸測試**鎖死「重置後身分存活」。
- ⚠️ 同 ADR-0104：reloader 不在此範圍；重置只清資料，服務於下次讀取/重啟反映。

## 驗證
守衛（接受/拒絕 DebugAgent·未知·未擁有/正規化/fail-closed）、協調器（驅動 stores·false-clean）、端點（admin/CSRF/密碼/雙重確認）、**🛡️ 關鍵回歸：custom config.json／system_prompt.md 於重置後存活、僅 drive/outputs 被清**皆經測試覆蓋（14 後端＋3 前端）；既有 DebugAgent reset 測試全綠。openapi+api.ts 重生；`guard_all` 37/37、pytest 2372、vitest 816、smoke 24/24。

**對抗式安全複審（5 維×13 agent；8 findings → 逐項對抗式驗證 → 修 2、駁回 6）**：最高優先的三大關切——**身分摧毀、跨 agent 波及、守衛繞過——全數確認無破口**（清除原語逐 agent_id 範圍化＋`is_protected_path` 物理地板＋custom config 範圍排除，結構性成立）。修：①**【Med】重置報告的 store 錯誤訊息**直接回傳原始例外（可能含絕對路徑/DB 細節）→ 改回傳泛用碼（`purge_failed`/`verify_failed`），完整細節僅進伺服器日誌（同時惠及 DebugAgent 路徑）；②**【Low】`ResetReport.agent` docstring 過時**（「恆為 DebugAgent」）→ 更正為兩路徑通用。駁回 6（路徑逃逸、prefix 碰撞、role 誤判、refactor 弱化 DebugAgent 路徑等經驗證均不可達）。
