# ADR-0043: React Hooks Reference Stability and UI Decoupling — React Hooks 參考穩定性重構與交互邏輯解耦

> **現況**：`installHubSkill` 隨 Skill Hub 退役刪除（ADR-0077）。

**狀態**: ✅ 已接受（具名標的修訂 by [ADR-0077](0077-retire-skill-hub-consolidate-into-catalog.md)：文中 installHubSkill 已隨 persona Skill Hub 退役刪除；Hooks 參考穩定性重構本身不變）
**日期**: 2026-05-24
**關鍵字**: React Hooks, useCallback, SOLID, Single Responsibility Principle, Reference Stability, View-Model Separation

---

## 背景與問題

在前端 React 應用開發中，自定義 Hooks 被廣泛用於封裝狀態與資料存取邏輯。然而，隨著組件規模的擴大，發現了以下兩項影響性能與測試的設計缺失：
1. **Hook 返回函式參考不穩定 (M5)**：
   - `useLocalStorage` 的 `setValue` 返回函式未進行 memoization。每次 Hook 所在的組件 re-render 時，`setValue` 都會生成一個全新的函式參考。
   - 這導致任何將 `setValue` 或其封裝方法作為依賴的 React `useEffect`、`useMemo` 或其它 Hook，都會被不斷地觸發，形成不必要的重複執行或無限渲染迴圈，同時引發 ESLint `react-hooks/exhaustive-deps` 警告。
2. **Data Hooks 與 UI 交互高度耦合 (M8)**：
   - 負責用戶管理 (`useUserSettings`) 與技能安裝 (`useSkillSettings`) 的 Data Hooks 內部，直接調用了瀏覽器的 `window.confirm()` 進行彈窗確認。
   - 這嚴重違反了 SOLID 的 **單一職責原則 (SRP)**。Hooks 作為邏輯與資料傳輸層（Model/Controller），不應承載具體的 UI 呈現與阻斷性對話框邏輯（View）。這也使單元測試時必須 mock 全域 `window.confirm`，極大增加了測試的複雜度與脆弱性。

---

## 決策

為提升 React Hook 的性能穩定性並實踐 SOLID 重構，我們實施了以下兩項重構措施：

### 1. 實現 Hook 返回值的參考穩定性 (M5)
- **useLocalStorage 記憶化**：
  - 使用 `useCallback` 封裝 `useLocalStorage` 的 `setValue`。
  - 將其依賴矩陣限制為僅有 `[key]`。
  - 內部狀態更新改為 React 的 **函數式狀態更新器 (Functional State Updater)** 模式：
    ```typescript
    const valueToStore = value instanceof Function ? value(storedValue) : value;
    ```
    這使得 `setValue` 內部不需要將狀態 `storedValue` 列入 `useCallback` 的依賴陣列中，保證在 `key` 保持不變時，`setValue` 的函式引用在 re-render 間絕對不變。
- **依賴警告修復**：
  - 更新 `useConnectionSettings` 及其它 Hooks 的內部依賴，確保所有傳遞給下層邏輯的 setter（如 `setConnections`）均具備穩定的參考，消除 ESLint 的依賴警報。

### 2. 資料 Hooks 與 UI Confirm 的徹底解耦 (M8)
- **Hooks 職責純化**：
  - 自 `useUserSettings.ts` 中移除 `deleteUser` 內部的 `window.confirm` 調用。
  - 自 `useSkillSettings.ts` 中將 `installHubSkill` 的重複存在判斷與 `window.confirm` 對話邏輯完全剝離。
  - 重構後的 Data Hooks 函式成為純粹的非同步 API 觸發器，只負責發送請求與更新狀態。
- **交互邏輯上移至 View 層**：
  - 在 `UsersTab.tsx` 和 `SkillsTab.tsx` 等 UI 元件層中，將 `confirm` 調用與業務判斷移至按鈕的點擊處理函式（例如 `handleDelete` 與 `handleInstall`）中。
  - 元件在獲得用戶確認後，才調用 Hook 提供的 API。這達成了 View（UI 提示）與 Model（資料請求）的清晰分離。

---

## 影響

- **性能提升與消除 Render 抖動**：Hooks 的傳回值參考保持絕對穩定，防止子組件因為 Prop 或 Dependency 變更而觸發無謂的重繪，提升了 UI 的運行效率。
- **提升測試性與 SOLID 規範合規**：Data Hooks 不再依賴瀏覽器全域環境，可以在 Node.js 或無頭測試環境（Vitest）中輕鬆進行單元測試，無須 Mock 視窗 API。
- **加固 UI 控制力**：UI 元件對操作確認與重複檢查擁有了 100% 的控制主權，便於未來將原生 `confirm` 升級為更美觀的 Modal 對話框，而不必修改底層的 Data Hooks。
