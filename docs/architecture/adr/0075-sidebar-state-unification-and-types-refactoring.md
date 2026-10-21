# ADR-0075: 側邊欄折疊狀態管理提升與 TS 回呼合約型別重構

## 上下文 (Context)
在對 ToukaChat 前端進行 UI/UX 審查與代碼質量優化時，我們發現了以下問題：
1. **雙重狀態與 SSOT 違規**：側邊欄的折疊狀態（`isCollapsed`）同時在 `LeftSidebar` 元件內部使用 `useLocalStorage` 維護，又在 `page.tsx` 中使用本地 `isSidebarCollapsed` 狀態維護。這造成折疊狀態存在雙重真實事實來源，具有狀態不同步的潛在隱患。
2. **掛載時的級聯渲染警告**：在 `page.tsx` 中，為了讀取 localStorage 中的折疊狀態，原本程式碼是在掛載後的 `useEffect` 中讀取並呼叫 `setIsSidebarCollapsed(true)`。這會引起 React 在掛載後立即觸發狀態更新與級聯渲染（set-state-in-effect），導致不必要的性能損耗與靜態代碼檢查警告。
3. **選單關閉時刪除確認未重置**：在對話列表的 `SessionItem` 中，點擊刪除按鈕時，選單會停留在「確認刪除？」紅框狀態。然而，當使用者點擊選單外部將其關閉後，再次點開該 Session 的選單時，刪除按鈕仍停留在紅框「確認刪除？」狀態，不符合直覺且容易導致誤刪。
4. **TS 回呼合約不潔 (as unknown as)**：在 `SessionItem.tsx` 內部的 `onKeyDown`、`onBlur` 與 `onSelect` 中，將 `onSaveTitle` 與 `onDelete` 事件傳入時，使用了多處無謂的 `e as unknown as React.MouseEvent` 強制型別轉換。這係因原本的 Props 回呼定義不必要地依賴了 Event 參數。

## 決策方案 (Decision)
為了遵循 SOLID、TDD 與 SSOT 原則，我們決定對 ToukaChat 前端狀態與合約進行深度重構：
1. **側邊欄折疊狀態提升 (State Lifting)**：
   - 移除了 `LeftSidebar` 元件內部的 `useLocalStorage` 折疊狀態維護。
   - 將其統一提升至 `page.tsx` 中作為唯一的 Facts 來源進行生命週期管理，並直接以 `isCollapsed` Prop 單向傳遞給 `LeftSidebar`，實現無狀態展示元件設計。
2. **使用 useLocalStorage 作為 State 宣告以根治級聯更新**：
   - 在 `page.tsx` 中將 `isSidebarCollapsed` 宣告改為 `useLocalStorage("touka_sidebar_collapsed", false)`。
   - 移除 `useEffect` 中掛載後手動讀取並 `setIsSidebarCollapsed` 的邏輯。此改動能確保 React 於 Client 端初始化時即加載正確值，徹底根治 `set-state-in-effect` 警告與多餘渲染。
3. **DropdownMenu 關閉事件監聽與狀態即時重置**：
   - 在 `useSessionActions` 中新增並導出 `clearDeleteConfirm` 重置回呼。
   - 在 `SessionItem` 的 `DropdownMenu` 上綁定 `onOpenChange` 事件。當檢測到選單關閉（`open === false`）時，即時呼叫 `onClearDeleteConfirm` 以將 `deleteConfirmId` 狀態重置為 `null`，重置確認按鈕狀態。
4. **重構合約並消除強轉型別**：
   - 修改 `SessionItemProps`，將 `onSaveTitle` 與 `onDelete` 的簽章從吃 Event 修改為精簡的 `(id: string) => void`。
   - 彻底移除了 `SessionItem.tsx` 中所有不必要的 `as unknown as React.MouseEvent` 強轉。

## 後果與影響 (Consequences)
- **視覺與交互一致性**：對話刪除二次確認的重置行為符合 UX 直覺，消除了因殘留「確認刪除？」狀態引發的誤刪風險。
- **代碼品質與型別安全**：移除了前端的 type-drift 與強轉型別，前端 TypeScript 型別安全度大幅提升，並消除了 ESLint 的所有警告。
- **消除渲染冗餘**：消成了 `set-state-in-effect` 造成的頁面初次載入級聯渲染，加載性能更優。
- **測試完備**：新增了單元測試 `SessionItem.test.tsx` 與 `LeftSidebar.test.tsx`，完美完成了 TDD 改進，確保修復無 regression。
