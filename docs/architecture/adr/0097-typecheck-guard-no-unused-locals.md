# ADR-0097: Guard #27 擴充 noUnusedLocals — 未使用匯入／區域變數靜態執法

**狀態**：✅ 已接受
**日期**：2026-06-17
**影響範圍**：ToukaChat（`tsconfig.guard.json`、`eslint.config.mjs`、~80 元件檔）
**關鍵字**：noUnusedLocals, Guard #27, tsconfig.guard.json, 未使用匯入, react-jsx, argsIgnorePattern, 死碼, 建基於 ADR-0054

---

## 背景

對抗式複審發現一個守護缺口：**「檔案內未使用的匯入／區域變數」沒有任何守衛在管**。

- **knip（Guard #20）** 只偵測模組圖層級（未使用的*檔案*／*export*／依賴），其問題類型不含「import 進來卻在檔內沒用到」。
- **TypeScript 型別檢查（Guard #27，見 ADR-0054）** 當時未開 `noUnusedLocals`（`strict` 不含此旗標），故不報未使用匯入。
- **ESLint** 的 `@typescript-eslint/no-unused-vars` 僅為 **warn**，且 **ESLint 未納入 `guard_all.py`** → 未使用匯入／變數**靜默通過全部 34 道守衛**。

實測當時 `src/` 已累積 52 個未使用匯入／變數警告，全數溜過守衛；只有人工 `npm run lint` 才看得到。

## 決策

擴充既有 Guard #27（**不新增守衛、不動 `tsconfig.json`**）：於 `tsconfig.guard.json` 開啟 `noUnusedLocals`。

### 1. 僅改守衛專用設定

```json
"compilerOptions": { "noUnusedLocals": true }
```

**只改 `tsconfig.guard.json`、不碰 `tsconfig.json`**：guard.json 已排除測試檔、且不被 IDE／`next build` 使用 → blast radius 最小（IDE 不會突然滿江紅、`next build` 不會因測試檔或全 src 未用變數而爆）。守衛才是 gate。

### 2. 連帶清理：移除 79 個多餘的 `import React`

`noUnusedLocals` 比 ESLint 嚴格：在自動 JSX runtime（`jsx: "react-jsx"`，本專案已啟用）下，`import React` 為非必要。ESLint（React-aware）**故意忽略**，TS 則正確標為未使用。據此移除 79 檔的多餘 React 預設匯入（33 個整行刪、46 個去掉 `React,` 保留具名匯入），保留 66 檔仍用 `React.X` 的合法匯入。

### 3. ESLint 尊重底線慣例

於 `eslint.config.mjs` 為 `@typescript-eslint/no-unused-vars` 加 `argsIgnorePattern` / `varsIgnorePattern` / `caughtErrorsIgnorePattern: '^_'` —— 底線前綴＝刻意未使用（與 TS `noUnusedLocals` / `noUnusedParameters` 原生的底線豁免一致）。

## 對齊原則

| 原則 | 落實方式 |
|------|---------|
| **SSOT** | `tsconfig.guard.json` 維持守衛型別設定唯一來源；未新增第二套機制 |
| **最小驚訝** | 僅守衛 gate 行為改變，開發期（IDE／build）零變動 |
| **DRY** | 重用既有 Guard #27 與 `guard:typecheck` 腳本，零新增守衛 |

## 後果

### 正面

- 未使用的匯入／區域變數在 `guard_all.py` 的 Guard #27 即被攔截。**已用假匯入實測**：種一個 unused import → #27 由綠轉紅（`TS6133`）→ 移除後復綠。
- 連帶清掉 79 個 React cruft；未來再混入多餘 `import React` 也會被 #27 擋下。
- `npm run lint` 的 unused-vars 警告由 52 歸零。

### 不受影響 / 權衡

- **函式參數未納管**：`noUnusedParameters` 刻意未開（未使用參數較吵、需全面導入 `_` 前綴慣例）；底線前綴參數由 ESLint 規則（上述第 3 點）視為刻意未使用。
- **測試檔**仍排除於 `tsconfig.guard.json` 之外（沿襲 ADR-0054 的設計）。
- IDE 與 `next build` 行為不變；後端、所有 agent 資料不受影響。

### 建基於

- **ADR-0054**（Frontend TypeScript Type Check Guard，Guard #27 本體）。本 ADR 僅**擴充**其涵蓋範圍，未取代之。
