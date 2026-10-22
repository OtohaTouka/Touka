# ADR-0096: live 設定檔啟動健檢（fail-fast，補 Guard #34 守不到的真檔）

**狀態 (Status):** ✅ 已接受
**日期 (Date):** 2026-06-16
**決策者 (Deciders):** 維護者

## 上下文 (Context)

ADR-0095（Guard #34）把「設定範本品質」納入 CI，但留下一個**結構性缺口**：它只守得到
被追蹤的 `.example` 範本，守不到**實際運作中的 live 設定檔**——live `config/*.json` 是
gitignore 的，CI 全新 checkout 上根本不存在，無法靜態守衛。

而 live 檔正是會「靜默壞掉」的那一層：

1. **`autonomy.json` 載入時完全沒有 schema 驗證**——`services/autonomy.py._load_config` 直接
   `resource_manager.read_json()` 取**原始 dict**、不過 `AutonomyConfigModel`。這正是 ADR-0094
   修的 cap/quota「載入了卻從不生效」的同源結構：原始 dict、缺鍵就一直缺。
2. **`resource_manager.read_json` 會把壞 JSON 當空字典靜默吞掉**（`JSONDecodeError` → 記一筆
   warning → 回 `None` → 業務層退回預設）。於是一個手滑改壞的 live 設定，不會讓系統報錯，而是
   **悄悄以預設值啟動**——對安全相關設定（auth_policies/permissions/identity_map）尤其危險。

維護者要求 live 檔也要加上守衛。由於 live 檔在 CI 不存在，**靜態守衛做不到**；正確解法是
**在每次啟動時，對真實存在的 live 檔做自我健檢**。

## 決策方案 (Decision)

新增 `utils/bootstrap.validate_live_configs()`，於 `main.py` lifespan **緊接 `ensure_configs()`
之後**呼叫（缺檔已被還原），對 **live 真檔** fail-fast：

- **檢查（僅針對「已存在」的檔；缺檔交由 `ensure_configs` 還原，不在此報錯）**：
  1. **所有 `config/*.json` 為合法 JSON**——直接 `json.loads`，**刻意不經會靜默吞錯的
     `read_json`**（否則壞 JSON 偵測不到，正是要回歸守的點）。
  2. **有 pydantic 模型者通過 `model_validate`**：`autonomy`／`permissions`／`identity_map`／
     `auth_policies`／`sliding_window`（型別/必填正確）。
- **fail-fast**：任一檔壞 JSON 或不符 schema → 拋 `ConfigValidationError`（CRITICAL 日誌，逐檔
  原因＋修復指引：「修正內容；若有 `.example` 範本亦可刪除後重啟自動重建」），**啟動中止**。
  勝過「靜默退回預設」——寧可大聲擋下，不要帶著壞設定（或預設安全策略）默默開機。
- **extra 鍵容忍**：模型用 `extra="ignore"`，故 live 檔帶模型未定義的鍵**不**視為錯（與範本守衛
  「擋過時鍵」取捨相反）——live 檔可能領先模型，模型演進期間不該誤殺正常運作中的真檔。
- **與測試環境隔離**：驗證置於 `if TOUKA_BOOTSTRAP_ENABLED` 區塊內；測試（conftest 設
  `TOUKA_BOOTSTRAP_ENABLED=false`）連同 `ensure_configs` 一起跳過，不影響既有以壞設定測韌性的
  測試。機制本身改由專屬單元測試（注入 tmp 目錄）覆蓋＝CI 守得到。

### 為何不做成 Guard #N（守衛數維持 34）
`guard_all` 在 CI 跑、live 檔不存在 → 任何「live 守衛」在 CI 都是 no-op，登記成 Guard #N 只會
讓「34/34 全綠」失真。**啟動健檢才是 live 檔的正確守衛**（每次啟動、對真檔、在真環境）；其
*機制*由 `test_config_live_validation.py` 在 CI 守住。

## 後果與影響 (Consequences)

- **補上 Guard #34 守不到的層**：範本（靜態 CI）＋ live 真檔（啟動時）兩層互補，設定漂移/壞檔
  不再能靜默開機。`autonomy.json` 首次獲得啟動期 schema 驗證（先前完全沒有）。
- **行為改變（刻意）**：先前「壞設定 → 靜默退回預設」改為「壞設定 → 大聲擋下、啟動中止」。
  復原路徑明確（修正檔案，或刪除有範本者重啟自動重建）；預設 `TOUKA_BOOTSTRAP_ENABLED=true`
  時生效，顯式停用 bootstrap 者一併 opt-out（一致）。
- **誠實限制**：① 仍**不檢查 `.example` ↔ live 比對**（live gitignored、CI 不存在，結構性限制
  不變）；② 只驗 5 個有模型的設定 + 全體 JSON 合法性，無模型者（bots/models/mcp_hub…）僅檢
  JSON、不檢語意；③ 與範本守衛一致地**不擋 extra 鍵**，故「live 帶過時鍵」不會被擋（容忍模型
  演進，避免誤殺）。
- 守衛數**維持 34**（理由見上）；`guard_all` 34/34、lint_ssot 全綠。

## Action Items
1. [x] `utils/bootstrap.py`：`validate_live_configs()`＋`ConfigValidationError`＋`_LIVE_MODELED`
   對照表（5 模型）；JSON 合法性直接 `json.loads`（不經 `read_json`）。
2. [x] `main.py` lifespan：`ensure_configs()` 後呼叫 `validate_live_configs()`（fail-fast；置於
   `TOUKA_BOOTSTRAP_ENABLED` 區塊內）。
3. [x] `tests/unit/test_config_live_validation.py`（6 例：合法通過、壞 JSON fail-fast 點名、
   schema 型別錯 fail-fast、缺檔略過、不存在目錄回空、extra 鍵容忍）。
4. [x] ADR-0096 ＋ `adr/README`；`README_HANDOVER`／`CHANGELOG`／`tests/README` 同步。冒煙
   17/17（真環境 bootstrap 啟用，已實跑驗證 validator 對真檔通過）；guard 34/34；後端全量綠。
