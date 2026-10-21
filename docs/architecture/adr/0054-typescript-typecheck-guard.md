# ADR-0054: Frontend TypeScript Type Check Guard

**狀態**：✅ 已接受  
**日期**：2026-05-29  
**影響範圍**：ToukaChat、scripts/guard_all.py  
**關鍵字**：TypeScript, tsc, 守衛, tsconfig.guard.json, 型別安全, Guard #27

---

## 背景

ADR-0053（First-Run Setup Wizard）實作完成後，`SetupPromptBanner.tsx` 引入了一個 TypeScript 循環型別參照（Circular Type Reference）：

```typescript
// 違規：自我參照型別（已修復）
function hasSetupIssues(status: Parameters<typeof hasSetupIssues>[0]): boolean {
```

此錯誤導致 `npm run build` 在型別檢查階段崩潰，Docker 映像無法建置。

**問題根源**：`guard_all.py` 的 26 道守衛沒有任何一道執行 `tsc --noEmit`（TypeScript 型別檢查）。

- **vitest** 使用 esbuild 轉譯，預設**跳過型別檢查**，測試仍然通過
- **`npm run test`** 僅跑 `tests/auth`，覆蓋範圍有限
- **ESLint** 不檢查 TypeScript 型別層面問題
- 結果：型別錯誤**靜默通過所有開發期守衛**，直到 Docker build 才爆出

---

## 決策

新增第 27 道守衛：**Frontend TypeScript Type Check Guard**。

### 1. 建立 `ToukaChat/tsconfig.guard.json`（守衛專用設定）

```json
{
  "extends": "./tsconfig.json",
  "include": ["next-env.d.ts", "src/**/*.ts", "src/**/*.tsx", ".next/types/**/*.ts"],
  "exclude": ["node_modules", "**/__tests__/**", "**/*.test.ts", "**/*.test.tsx", "tests/"]
}
```

**為何需要單獨設定**：`tsconfig.json` 的 `include: ["**/*.ts"]` 包含所有測試檔案，而測試檔案有大量既有 TypeScript 錯誤（型別 Mock 不完整）。使用主 `tsconfig.json` 守衛會永遠失敗，且修復所有測試型別錯誤超出本次範圍。

`tsconfig.guard.json` 繼承主設定（保留 `strict`、`paths`、`noEmit` 等），只縮小 `include` 範圍至生產原始碼 `src/**`。

### 2. 新增 npm 腳本

```json
"guard:typecheck": "npx tsc --project tsconfig.guard.json --noEmit"
```

與所有其他 `guard:*` 腳本一致（使用 `npx`，在 `ToukaChat/` 目錄下執行）。

### 3. 新增 Guard #27 至 `scripts/guard_all.py`

```python
{
    "name": "Frontend TypeScript Type Check Guard [Guard #27]",
    "command": "npm run guard:typecheck",
    "cwd": frontend_dir
}
```

---

## 對齊三大開發原則

| 原則 | 落實方式 |
|------|---------|
| **TDD** | 先建立 `tests/unit/test_typecheck_guard_registration.py`（3 個結構性測試，Red → Green） |
| **SOLID-SRP** | `tsconfig.guard.json` 單一職責（僅描述生產原始碼型別設定）；守衛腳本與主 tsconfig 職責分離 |
| **SSOT** | `tsconfig.guard.json` 為守衛型別設定唯一來源；`package.json scripts.guard:typecheck` 為呼叫入口唯一來源 |

---

## 後果

### 正面

- 生產原始碼的 TypeScript 型別錯誤在 `guard_all.py` 階段即被攔截，不再等到 Docker build
- 守衛數從 26 增至 27（`guard_all.py` 27/27）
- `tsconfig.guard.json` 可作為未來「僅生產程式碼型別嚴格模式」的擴展基礎

### 負面 / 權衡

- 測試檔案的 TypeScript 錯誤**不受**此守衛覆蓋（設計如此）；測試型別改善留待未來 ADR
- `tsc` 在首次執行時需要完整分析，約 10–30 秒（`incremental: true` 可加速後續執行）

### 不受影響

- `tests/`、`__tests__/` 下的測試檔案（排除在 `tsconfig.guard.json` 之外）
- 後端 ToukaProxy / ToukaMemory（Python，不涉及 TypeScript）
- 所有已存 agent 資料（`data/soul/`、`data/legacy/`）
