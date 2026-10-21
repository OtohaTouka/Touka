# ADR-0029: NOQA Registry Sync Guard — noqa 登記簿自動同步驗證

**狀態**: ✅ 已接受（時點性引用過時：文中「第 16 道守衛」序號已漂移，本守衛現為 Guard #19；NOQA 登記簿自動同步機制不變）  
**日期**: 2026-05-17  
**關鍵字**: NOQA_REGISTRY, guard, 自動化驗證, LaC, SSOT

---

## 背景與問題

ADR-0023 規定每個 `# noqa` 指令必須手動登記至 `NOQA_REGISTRY.md`，並在全系統統計表中更新數量。
但這是純文字規範，**缺乏自動化執法機制**：

- 開發者新增 `# noqa: RULE_ID` 後忘記更新登記簿 → 靜默失同步
- 移除 noqa 後未更新統計數字 → 登記簿數字過高
- 初始稽核發現 3 處真實差異（IDENTITY_SSOT: 5 vs 實際 17；LOCAL_IMPORT_BAN: 10 vs 8；TID251: 6 vs 5）

---

## 決策

新增 `scripts/guard_noqa_registry.py`（第 16 道 guard），整合至 `guard_all.py`。

### 掃描策略

**Python (.py)**：使用 `tokenize` 模組，只統計**行內 COMMENT token**（同行有程式碼的 `# noqa`），自動排除：
- 字串字面量中的 noqa（`"code  # noqa: RULE"` 作為測試資料）
- Docstring 中的格式說明（`"""# noqa: RULE 應被計入"""` 之類）
- 獨立說明型 comment（`# 例：# noqa: RULE_A [, RULE_B]`）

**TypeScript (.ts/.tsx)**：行級掃描，排除純 comment 行（以 `//`、`*`、`/*` 為行首）和 `__tests__/` 目錄。

### 追蹤範圍 (SSOT)

只驗證 `NOQA_REGISTRY.md` 全系統統計表中**有獨立具名行的規則**（regex: `` `RULE_ID` ``）。

自動略過複合行（「其他 (`S104`, ...)」）→ 不需另外維護一份「哪些規則算架構型」清單，registry 本身就定義了追蹤範圍。

### 驗證邏輯

1. **計數核對**：registry 中每條具名規則的登記數量必須等於實際掃描計數
2. **未登記規則偵測**：若代碼庫出現 `UPPERCASE_WITH_UNDERSCORE` 格式的 noqa 但 registry 未追蹤 → 報錯
3. **純 Ruff 規則豁免**：`B008`、`E501`、`S603` 等格式（字母+數字，無底線）→ 不強制追蹤

---

## 初始修正

實施 guard 後發現並修正以下 registry 差異：

| 規則 | 舊值 | 新值 | 原因 |
|------|------|------|------|
| `IDENTITY_SSOT` | 5 | 17 | 12 個 ToukaProxy router 層 noqa 未補錄（FastAPI URL path 參數） |
| `LOCAL_IMPORT_BAN` | 10 | 8 | `test_ssot_guard_local_import.py` L95, L98 的 noqa 在字串字面量中，非真實豁免 |
| `TID251` | 6 | 5 | 統計表人工多計 1 個 |

---

## 後果

**正向影響**：
- 開發者新增 noqa 後若忘記更新 registry → CI 失敗，強制同步
- 統計表與代碼庫保持 100% 一致，registry 具備文件可信度
- tokenize-based 掃描正確排除測試資料與說明型 comment，零假陽性

**限制**：
- 只驗證統計表中有具名行的規則，複合列「其他」無法逐一核查
- 不驗證 body 條目（每條 noqa 的詳細說明）是否完整，僅驗證數量
- TypeScript 行級掃描仍有邊緣案例（複雜巢狀字串），但不影響現有代碼庫
