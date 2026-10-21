# ADR-0055: Frontend Test Scope — vitest.config.ts 作為測試發現 SSOT

**狀態**：✅ 已接受（機制細節修訂：測試發現 SSOT 已自 vitest.config.ts 內嵌 include 陣列搬至 orphaned_tests_logic.ts 的 TEST_INCLUDE_GLOBS（與 Guard #31 共用）；測試發現 SSOT 原則本身不變）  
**日期**：2026-05-29  
**影響範圍**：ToukaChat、ToukaChat/vitest.config.ts、ToukaChat/package.json  
**關鍵字**：vitest, test-coverage, SSOT, npm-test-scope, jsdom, Node.js-22

---

## 背景

`ToukaChat/package.json` 的 `test` 腳本為：

```json
"test": "vitest run tests/auth"
```

此設定只跑 `tests/auth/` 下的 4 個測試檔案（AuthGuard、AuthPages、authService、AuthContext），完全略過：

- **24 個元件測試**：`src/**/__tests__/**/*.test.{ts,tsx}`
- **21 個 unit 測試**：`tests/unit/`
- **3 個 guard 測試**：`tests/guard/`

共計 50 個測試檔案（共 200 個測試）在開發期間永遠不被執行。

**違反 SSOT 原則**：測試發現邏輯硬編碼在 `package.json` 而非 `vitest.config.ts`，導致兩個地方都需要維護（`vitest.config.ts` 已有 `environment`、`globals` 等設定，但不含 `include`）。

---

## 問題症狀（發現順序）

### 1. 擴展覆蓋範圍後：localStorage 在 Node.js 22+ 為 `undefined`

```
ExperimentalWarning: localStorage is not available because --localstorage-file was not provided.
TypeError: Cannot read properties of undefined (reading 'clear')
```

Node.js 22+ 新增了內建 `localStorage`，但在未傳入 `--localstorage-file` 旗標時回傳 `undefined`。jsdom 環境在此版本下無法自動覆蓋此行為，導致所有直接存取 `localStorage` 的測試失敗。

**受影響檔案**：`tests/unit/hooks/settings/useSnsBotSettings.test.ts`、`src/components/settings/tabs/__tests__/ThemeSettingsTab.test.tsx`、`src/components/layout/__tests__/SoulStudio.test.tsx`

### 2. SoulStudio POST 測試在全 suite 下 `waitFor` 超時

```
AssertionError: expected undefined to be defined  ← waitFor({ timeout: 3000 })
```

單一執行時 67ms 通過，完整 54 個 worker 並行時 CPU 競爭導致 async handler 排程超過 3000ms。

---

## 決策

### 1. `vitest.config.ts` 加入 `include`（SSOT）

```typescript
test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: [
        'src/**/__tests__/**/*.test.{ts,tsx}',
        'tests/**/*.test.{ts,tsx}',
    ],
    alias: { '@': path.resolve(__dirname, './src') },
}
```

`vitest.config.ts` 成為測試發現的唯一真相來源。

### 2. `package.json` 改為 `"test": "vitest run"`

委派測試發現給 `vitest.config.ts`，不重複路徑邏輯。

### 3. `src/test/setup.ts` 加入 Node.js 22+ 相容 Storage 實作

```typescript
function makeStorage(): Storage { /* in-memory Map-based Storage */ }

Object.defineProperty(globalThis, 'localStorage', {
    value: makeStorage(), writable: true, configurable: true,
});
Object.defineProperty(globalThis, 'sessionStorage', {
    value: makeStorage(), writable: true, configurable: true,
});
```

無論 Node.js 版本或 `--localstorage-file` 旗標是否存在，測試環境都有可靠的 Storage API。

### 4. `SoulStudio.test.tsx` `waitFor` timeout 從 3000ms → 10000ms

明確表達此為整合式測試，需要容忍 CI / 並行執行下的資源競爭延遲。

---

## SSOT 對齊

| 元素 | SSOT 來源 | 說明 |
|------|-----------|------|
| 測試發現路徑 | `vitest.config.ts` `include` | 從 `package.json` 遷移至此 |
| Storage API | `src/test/setup.ts` | 每個 worker 的唯一 Storage 初始化入口 |

---

## 後果

### 正面

- `npm run test` 現在覆蓋全部 54 個測試檔案，204 個測試
- 新增元件測試在 CI 路徑中被執行（ADR-0053 的 setup wizard 元件）
- Node.js 22+ 環境中 Storage API 可靠運作
- TDD 結構性測試（`test_npm_test_scope.py`）確保未來不退化

### 負面 / 權衡

- `SoulStudio.test.tsx` 的 `waitFor` timeout 從 3000ms 增加至 10000ms，在超時前會多等待時間（當測試實際失敗時，反饋較慢）
- `setup.ts` 的 Storage mock 覆蓋 jsdom 提供的 Storage，若 jsdom 版本更新提供更完整實作則可移除

### 不受影響

- `tsconfig.guard.json` 和 Guard #25（ADR-0054）的生產型別檢查
- 後端 ToukaProxy / ToukaMemory（Python）
- 所有已存 agent 資料（`data/soul/`、`data/legacy/`）

---

## 相關 ADR

- ADR-0054：Frontend TypeScript Type Check Guard（守衛型別安全）
- ADR-0053：First-Run Setup Wizard（新增的 setup 元件測試）
