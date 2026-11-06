# Chat規則

範圍：`ToukaChat/**`：網頁介面。

從根 `AGENTS.md` 搬來的全文；禁令 9、50、51 之後依 ADR-0298 更正過。跑 `python scripts/applicable_rules.py --print-rules` 看這次改動適用哪幾條，不必讀完整份。改規則見 [README](README.md)。

## 禁令

*   **禁令 9**：**嚴禁**在前端對 `localStorage.*` 直接呼叫（`guard_localStorage.ts` 掃 `src/hooks/**`、`src/lib/**`、`src/app/**`、`src/contexts/**`；`src/components/**` 由 `guard_hooks.ts` 擋），必須使用 `useLocalStorage<T>()` 或領域 Hook。唯一白名單：`useLocalStorage.ts`（定義本體）；`connections.ts`（Auth Token 非 React 函數）使用它匯出的 `getLocalStorageItem()`、`setLocalStorageItem()`、`replaceLocalStorageItem()`。
*   **禁令 13**：**嚴禁**在前端生產代碼中直接呼叫 `console.log()`、`console.warn()`、`console.error()`，必須透過 `logSSOTError()` 或 ERRORS 常數封裝層。`guard_console.ts`（Guard #15）會自動攔截。（CONSOLE_BAN，ADR-0031）
*   **禁令 45（HTTP 客戶端主權 / Fetch SSOT）**：前端所有 HTTP 請求必須透過 `src/lib/connections.ts` 的 `fetchTouka()` 發送，**嚴禁**直接調用原生 `fetch()`。**由 `guard_fetch.ts`（FETCH_SSOT）AST 掃描，整合至 `guard_all.py`。**（ADR-0024）
*   **禁令 49（設計主權 / Design Token Sovereignty）**：透過 Guard #4（`ToukaChat/scripts/guard_design.ts`），強制所有視覺樣式**必須**出自 `tokens.ts` 或系統定義，嚴禁任意值（arbitrary value）。
*   **禁令 50（組件主權 / Component Sovereignty）**：**嚴禁**直接引用未經封裝的底層 UI 組件（如 Radix UI 原件）。目前由 ESLint 擋 `@radix-ui/*`、`radix-ui`（Guard #35），其他底層元件沒有規則。（ADR-0298）
*   **禁令 51（合約完整性監督）**：**禁止**開發者在前端手動 re-declare 領域關鍵模型（前端型別必須來自後端 Pydantic 生成的合約）。Guard #2 以 AST 檢查，合約別名以外的同名宣告會紅；既有 10 個凍結在 `ToukaChat/scripts/contract_type_baseline.json`，不得增加；改名就抓不到。（ADR-0298）
*   **禁令 56（前端側邊欄狀態提升與 SSOT）**：**嚴禁**在 `LeftSidebar` 內部自行維護折疊狀態的 `useLocalStorage` 狀態。側邊欄折疊狀態必須由 `page.tsx` 作為單一 facts 來源進行 `useLocalStorage` 持久化，並將 `isCollapsed` 狀態單向向下傳遞給 `LeftSidebar` 展示元件，消除雙重 State 不同步隱患。（ADR-0075）
*   **禁令 58（前端國際化主權 / i18n Translation Catalog SSOT）**：前端**使用者可見字串嚴禁硬編碼**，必須經 react-i18next 的 `t()` 渲染（元件用 `useTranslation()`；純函式由呼叫端注入 `t`）。翻譯目錄唯一 SSOT 為 `ToukaChat/src/locales/<lng>/translation.json`（`zh-TW` 來源語言／`en` 國際參考／其餘社群語言），採語意 key；新增語言經 `src/locales/index.ts` 註冊表（放檔＋一行）。錯誤訊息必須用 `errorMsg(code)`（橋接 `errors.json` 的 `i18n` key，見禁令 44 / ADR-0014）。**嚴禁**開 `nonExplicitSupportedLngs`（會把 `zh-TW` 收斂成不存在 bundle 的 `zh`，致 `t()` 全回原始 key）；測試固定 `zh-TW`（`src/test/setup.ts`）；`fallbackLng='en'`。由 **`guard_i18n.ts`（Guard #33）** 以 zh-TW 為基準驗證跨語言 key 同步（多餘/拼錯＝錯誤、`en` 不完整＝錯誤、社群語言缺 key＝警告），併入 `guard_all.py`。資料預設值／Server Component 字串／語言 endonym 刻意不抽。（ADR-0093）

## 小節

### 4. 訊息呈現與 UX 韌性 (UX Continuity) ⭐
*   **換行保留 (Newline Preservation)**：前端渲染必須確保原始輸入中的換行符號得以保留（使用 `whitespace-pre-wrap`）。Agent 在產生回應時應善用換行提升可讀性.
*   **事件攔截主權 (UX Sovereignty)** ⭐：下拉選單等具備「確認」邏輯的 UI 交互，必須正確處理 `onSelect` 事件並視情況呼叫 `preventDefault()`，確保交互鏈不因元件預設行為（如自動關閉）而中斷。同時，必須監聽選單關閉事件即時重置內部的臨時狀態（如將 `deleteConfirmId` 歸於 `null`），防止狀態殘留。
