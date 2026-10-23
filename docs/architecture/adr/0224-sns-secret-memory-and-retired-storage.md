# ADR-0224：SNS 憑證純記憶體表單與退役儲存清除

- 狀態：✅ SNS 13-path 修復切片與完整前端驗證已經審查接受；部署及舊 client 清除待確認
- 日期：2026-09-08
- 決策者：維護者（委託審查）
- 影響範圍：ToukaChat SNS 設定表單、領域 Hook、`useLocalStorage`、全 app 清除接線、Vitest
- 相關契約：[Chat Interface](../CHAT_INTERFACE.md)、repo 根目錄 `SECURITY.md` 的安全追蹤（站台外文件）

## 背景

SNS 表單曾將 raw token／secret 存入 localStorage，列為 Known Open HIGH。只移除新寫入仍會留下既有使用者的舊值；只在 SNS 分頁掛載時清除，沒有進該分頁的使用者仍保有持久化憑證。前端同時需遵守 storage 與領域 Hook 的單一責任邊界。

本決策依維護者 2026-09-08 的一次性授權審查；本 ADR 不自行授權，現行範圍見接續計畫（未入版控）。

## 決策

1. SNS 憑證輸入只放在表單生命週期的 React 記憶體。不能寫入 localStorage、sessionStorage、URL、日誌或可持久化 store，也不能從舊 storage 回填表單。
2. 一個領域 Hook 擁有表單值與送出生命週期，視圖只消費同一份 state／操作。移除重複 Hook 實例造成的狀態分裂；API 仍由既有領域接縫與 `fetchTouka()` 管理，不把 storage 或 HTTP 搬進視圖。
3. 在既有 `useLocalStorage` 定義本體提供狹義「退役 key 清除」接縫。集中精確舊 SNS key 清單，只做 remove，不讀出、不回傳或記錄舊秘密；不得 `clear()` 整個 storage，也不得清除無關設定。
4. 全 app 掛載的輕量元件呼叫清除接縫，使使用者不進 SNS 分頁也能清除舊值。SSR、storage 不可用及存取失敗不得讓 app 崩潰；無法刪除時不能宣稱已清除。實作不得新增業務檔案直接操作 localStorage 的白名單。
5. 送出成功、失敗與表單卸載時都清除應用程式持有的憑證狀態與相關保留引用；非同步完成不可在卸載後恢復已清除的值。一般非秘密欄位是否保留依既有 UX 契約，不擴大變更範圍。

## 驗證與交付

先以既有 Vitest mocks 驗證舊 key 清除但無關 key 留存、未進 SNS 分頁亦執行清除、storage 失敗不崩潰、輸入／送出不落盤、單一 Hook 狀態及成功／失敗／卸載 scrub。先保存 RED，再記錄聚焦 GREEN 與適用前端檢查；不使用真 SNS API 或真憑證。

實際 RED 7 failed／9 passed，清除接縫 RED 4 failed／26 passed；新增 28 cases。SNS 最終聚焦 40 passed／5 files，Node 修復後完整 Chat 1187 passed／233 files、TypeScript／Next 16.3.4 production build 通過；ESLint 0 errors／74 warnings，缺少逐筆舊 baseline，不能稱全部既有。原 full 1185 passed／2 舊預期 FAIL 保留，已修正另一舊測試的 storage 回填／未載入即儲存假設。

`RetiredSnsStorageCleanup` 掛於 I18nProvider 內、AuthGuard 外，登入前即移除精確 `touka_sns_bots`，只 remove、不 get／parse，並回應跨 tab 的同 key 重寫；失敗顯示翻譯 alert，不回顯例外或秘密。關閉／離線的舊 browser client 必須載入新版本後才執行清除；本批未部署或操作使用者 browser storage，不能宣稱既有秘密已全部清空。

實作與實測收據已經審查核對接受，SECURITY、CHAT_INTERFACE、SSOT 與測試清單同步記錄「程式缺口已修復，部署及既有 client 清除待確認」。此狀態不代表沒有曾經外洩，也不以文件代替部署證據。

## 回復與限制

JavaScript 字串不可證明實體記憶體歸零；此契約保證應用程式不再持久化並釋放可控 state／引用，不宣稱瀏覽器或 OS 層 secure erase。回復 UI 變更不得恢復 raw-token 持久化；發現未覆蓋的舊 key 時補精確遷移與反測試，不能以全域 storage 清除代替。後端 SNS secret 保存／認證與 DATA_PROTECTION 不在本次改動範圍。
