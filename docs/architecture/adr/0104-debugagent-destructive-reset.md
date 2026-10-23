# ADR-0104：DebugAgent 破壞性重置 — 安全閘、備份優先與對抗式複審加固

- **狀態**：✅ 已接受
- **日期**：2026-06-21
- **影響範圍**：ToukaProxy（agent_reset 套件／debug router／system_backup／manage_backup／async_db／feedback_store／brain_persistence）＋ ToukaMemory（factual／vector／manager）＋ ToukaChat（ScorecardPanel／useDebugReset）
- **關鍵字**：DebugAgent, 破壞性重置, 安全閘, 備份優先, 完整性自檢, 對抗式複審, Owner/admin, 稽核保留, false-clean, 軌B Batch 6, 建基於 ADR-0101

## 背景（Context）

軌B 計分卡（ADR-0101）以 **DebugAgent** 為實機評測身分，反覆跑情境會在多個 store 累積資料（對話、回饋、記憶、產出、任務…）。為了能在評測之間把 DebugAgent 回到白紙、並對照「原始 vs 累積」，需要一個**破壞性重置**：清空 DebugAgent 的累積資料，但**保留其身分／註冊**（重置後仍可用）。

破壞性 × 跨子系統（ToukaProxy ＋ ToukaMemory）× 真實刪除 → 此功能的首要風險是**誤刪真實 agent（Jin/Yan/Code）**或**在無備份下不可逆刪除**。維護者明確要求：**清空前先執行全系統備份**。

## 決策（Decision）

### 核心設計

- **端點**：`POST /v1/debug/reset`，body 需 `confirm=true` 且 `confirm_agent_id` **字面為 "DebugAgent"**（雙重確認，防誤觸）；任一不符即 `VALIDATION_ERROR`（一筆都不刪）。
- **目標硬編碼**：端點以無參數呼叫 `reset_debug_agent_data()`，刪除目標恆為 "DebugAgent"；`confirm_agent_id` 只是確認 token，**永遠不是刪除目標**（攻擊者無法重導向刪除對象）。
- **安全閘 `assert_resettable`（fail-closed，兩道防線）**：① 正規化後須 `== "DebugAgent"`；② 目標不可擁有 soul 目錄或 custom `config.json`（真實 agent 的特徵）。任一不過即 `AUTH_UNAUTHORIZED_ACCESS`。每個 store 的 purge 入口都重過此閘。
- **備份優先**：`do_backup=True`（預設）時**先**跑全系統備份（`system_backup.create_backup()` → `manage_backup.py backup --no-docker`）；備份失敗即 `SYSTEM_BACKUP_FAILED` 中止，**絕不在無備份下刪除**。
- **逐 store best-effort 清除 ＋ 完整性自檢**：依 `ALL_STORES` registry 逐一 purge（單 store 失敗收進報告、不中斷其餘）；清完再以 `remaining()` 自檢，仍有殘留或查詢出錯 → `clean=False`（防漏 store 的 false-clean）。
- **只清資料、不動身分**：`ALL_STORES` 不含 identity_map／users／registry，重置後 DebugAgent 仍可用。
- **SQL 全 scoped**：所有刪除為參數化 `WHERE agent_id = ?`（永不 `DROP TABLE`／無 WHERE）；向量為 per-agent collection（`memory_<id>`）整個 drop＝單 agent 範圍；檔案刪除經 `resource_manager`（受 `is_protected_path` 保護）。

### 對抗式複審（7 個攻擊視角＋懷疑者驗證）後的加固

複審**證實安全核心無破口**：無任何路徑能誤刪真實 agent、安全閘對所有對抗輸入（casing/空白/unicode/`ToukaSoul` 前綴/`../` 穿越）皆 fail-closed、刪除全 scoped。以下為複審揭出、本 ADR 採納修復的真實缺陷：

| 編號 | 缺陷 | 決策 |
|------|------|------|
| **R2/R8** | 未帶 token 的同網段客戶端被 LDH 自動升為 `Debugger`，足以觸發此破壞性端點；且 reset 內含的全系統備份在他處為 admin-only | **收緊授權**：`/debug/reset` 改限 **Owner/admin**（`_require_reset_role`，排除 Debugger）。LDH 只自動發 Debugger、不發 Owner/admin → 擋掉無認證 LAN 觸發。計分卡 run/stream 維持 `_require_debug_role`（Debugger 自助評測）不變。 |
| **R1** | 「備份優先」對 sessions/feedback **形同虛設**：`ui_sessions.db`/`feedback.db` 坐落 data 根目錄，不在備份涵蓋的六個 SANCTUARY_FOLDERS 內 | **補全備份**：`manage_backup.collect_items()` 額外納入 data 根層 `*.db`，讓全系統備份名副其實（restore 的 `data/` 分支已能處理）。加回歸測試。 |
| **R11** | 記憶橋接不可用（降級啟動／Qdrant 鎖）時，purge 與 remaining 都靜默回 0 → `clean=True` 卻仍有記憶殘留磁碟（**false-clean**） | `_memory_manager()` 不可用即 **raise**（非回 None）→ orchestrator 收進 store error → `clean=False`，誠實回報「記憶無法確認清除」。 |
| **R7** | reset 無並行守衛：與計分卡或第二個 reset 互踩成 TOCTOU（清完→殘留→自檢仍報 clean） | orchestrator 加 `asyncio.Lock` 序列化；已有 reset 進行中時第二個直接拒絕。 |
| **R3/R4** | `project_audit` 表、`decisions.csv` 帳本含 DebugAgent 列，未被清除（未文件化缺口） | **刻意保留**（審計軌跡完整性）＋**透明揭露**：新增 `RETAINED_STORES`，報告 `to_dict` 列出 `retained`，使 `clean=True` 不被誤讀為「連審計也清光」。 |
| **R5** | `data/snapshots/DebugAgent.md`（情境匯出）未清 | **加 `snapshot` store** 清除（agent 衍生內容，非審計軌跡）。 |
| **R9** | 向量 purge 回 1/0 而非實際點數，低報稽核 | `drop_agent` 刪除前先 `count()`，回真實點數（自檢仍用 `agent_point_count`）。 |
| **R6/R10/R12** | mission filelock 空目錄殘留（info）、記憶同步 I/O 在事件圈（與既有 idiom 一致，非新增）、legacy gap 註解失準 | 文件化／修正註解；不改行為（皆 info）。 |

### 被否決的方案

- **清除 `project_audit`／`decisions.csv`**：兩者為審計軌跡，刪除有損稽核完整性。改採「保留＋報告透明列為排除項」（維護者決策）。
- **把 reset 收緊為 admin-only**：完全對齊 `/system/backup/run`，但若人類擁有者角色為 Owner（非 admin）會使前端重置鈕失效。改採 Owner/admin（排除 Debugger），兼顧安全與可用。

## 後果（Consequences）

- ✅ 真實 agent 永不受影響：硬編碼目標 ＋ fail-closed 雙閘 ＋ 全 scoped 刪除，複審證實無破口。
- ✅ 「備份優先」名副其實：全系統備份現涵蓋 sessions/feedback；備份失敗即中止。
- ✅ 報告誠實：記憶不可確認 → `clean=False`；刻意保留的審計 store 於 `retained` 透明揭露。
- ✅ 破壞性端點授權收斂為 Owner/admin，擋掉無認證 LAN 觸發路徑。
- ⚠️ 已知/刻意殘留（文件化）：legacy 對話庫（受保護不可刪）、project_audit、decisions.csv 為刻意保留的審計軌跡；mission filelock 空目錄為無害 OS 殘留。
- ⚠️ R2 的根因（LDH 對無 token 同網段自動發卡 ＋ `HOST=0.0.0.0` 預設）屬既有 LDH 架構面，本 ADR 僅針對新破壞性端點做縱深防禦，未重設計 LDH。

## 驗證

- 對抗式複審：7 個攻擊視角＋懷疑者驗證（refute＋confirm-by-trace）；確認缺陷全數修復或文件化。
- 全程於 **3.12 venv**（直譯器主權）：guard_all 37/37、pytest 全綠（含 reset guard/primitives/orchestrator/endpoint/backup ＋ 核心模組回歸）、vitest、smoke 24/24。
- 建基於 ADR-0101（計分卡生產保真、Debugger 角色）。
