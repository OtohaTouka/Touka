# ADR-0026: localStorage SSOT Guard — 前端存取主權

**狀態**: ✅ 已接受（時點性引用過時：文中違規清單／noqa 五檔清單已隨程式碼演進（現多已清零）；localStorage SSOT 守衛規則本身不變）  
**日期**: 2026-05-16  
**關鍵字**: localStorage, SSOT, useLocalStorage, guard_localStorage, LaC

---

## 背景與問題

AGENTS.md 明文要求「請將 `fetch`、`localStorage` 操作封裝成領域專屬的 Custom Hooks」，但前端缺少靜態執法機制。`src/hooks/`、`src/lib/`、`src/app/` 等路徑中存在多處直接的 `localStorage.getItem/setItem` 呼叫，繞過了 `useLocalStorage` hook 的封裝，造成：
- localStorage key 命名無法統一追蹤
- 狀態更新難以測試（無法 mock）
- JSON 序列化邏輯分散

`src/components/**` 已由 `guard_hooks.ts` 覆蓋（DIP：組件不應直接存取儲存層），本 ADR 補齊其餘路徑。

---

## 決策

建立靜態守衛 `ToukaChat/scripts/guard_localStorage.ts`，掃描 `src/hooks/**`、`src/lib/**`、`src/app/**`、`src/contexts/**`。

**偵測方式**：AST 找出 `CallExpression` 中 `expression.getText()` 包含 `localStorage.` 的呼叫（涵蓋 `localStorage.*` 與 `window.localStorage.*`）。

**白名單（檔案級）**：
- `useLocalStorage.ts`：通用 Hook 的核心定義
- `connections.ts`：非 React 環境的 Auth Token 管理（JWT 送卡/領卡）

**違規處置結果**：
- **重構**（JSON 相容）：`useConnectionSettings.ts`、`useSnsBotSettings.ts`、`connections/page.tsx` 改用 `useLocalStorage<T>`
- **noqa**（純字串/架構必要）：`ThemeContext.tsx`、`SystemStatusContext.tsx`、`useStudio.ts`、`useSettingsTab.ts`、`app/page.tsx`

---

## 後果

**正向影響**：
- localStorage 存取統一走 `useLocalStorage` hook，Key 命名集中管理
- JSON 序列化/反序列化邏輯集中在一處
- 組件和 Hook 可 mock localStorage 進行測試

**限制**：
- `useLocalStorage` 使用 `JSON.parse/stringify`；儲存純字串值（如 `"true"/"false"`、tab 名稱）的使用者若切換至 hook 會因格式不匹配而丟失既有資料，因此這些保留 noqa 標記

**架構依據**：`guard_fetch.ts`（ADR-0024）相同模式，guard_all.py 整合。
