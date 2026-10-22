# ADR-0232：模式確認與登入結果

- 日期：2026-09-21
- 狀態：維護者授權修復，計畫經審查接受；完整來源驗證與最終審查見本批最終收據（未入版控）。
- 範圍：Chat 的模式／登入生命週期，Proxy 初始管理員密碼交付失敗；未發布、未部署。

## 問題

登入可由 `getActiveProxyUrl()` 使用預設或同源 Proxy，但模式檢查原先要求已保存的 connection，普通使用者因此可能登入後仍顯示 Standalone。任意 Memory 200 又可能被誤認可用；請求晚到可能覆蓋較新的身分或連線狀態。
登入服務原先丟失非成功回應分類；初始管理員密碼交付 OSError 被記錄後仍提交 users，形成無法取得初始密碼的帳戶。

## 決定

1. 模式 URL 與登入共用既有 SSOT；只有通過必要欄位驗證、非降級且有工具的 Memory 結果為 hybrid。明確降級才為 standalone；認證待就緒、401、403、不可用、malformed、零工具分開表示，不能冒稱任一可用模式。
2. SystemStatus 捕捉 origin、credential scope、request generation，每個 await（含 JSON body）後重驗；明示事件／手動檢查觸發，沒有輪詢。SidebarIdentity 僅呈現 i18n，既有 NavigationArea 以確證 hybrid 顯示能力入口，管理員／Agent 權限不變。
3. authService 的 AuthRequestError 只帶 generated ERRORS 的已知安全 code/message；未知 body／exception 使用固定 fallback。實際 `/login` 頁面不顯示或記錄原始錯誤，pending 防重複提交。
4. AuthContext 對 initial refresh／login／logout／profile 使用有限操作物件和既有 credential epoch。切 Proxy、明確換卡、登出、卸載或新操作使舊結果失效。相同 Proxy 設定更新不登出；同 principal 正常續期保留 epoch，profile 由 fetchTouka 使用 module card，成功後同步 React token。
5. 一般取消請求透過 `nonStreaming` 仍套既有 requestTimeoutMs；取得 Response 時原 timeout 即結束，JSON body 另由 scope 重驗，沒有宣稱整段 body 時限。AbortController 是最佳努力取消，不能回滾伺服器操作或 HTTP cookie 副作用。
6. 初始密碼受管寫入 OSError 在 append／users commit 前傳固定 PERSISTENCE_ERROR，不回 cause。既有 admin 不重設；密碼交付與 users 寫入不是跨檔案原子交易。

## 驗證與限制

G1–G9 的核准計畫與最終結果記錄在未入版控的原件。有效 RED 包含 default URL、Memory body／auth 分類、晚到 response/body、登入競態與初始交付失敗。頁面收到真 AuthRequestError 的接合測試與服務 wire 分類分開記錄，不冒稱人工 GUI。

維護者確認舊普通帳戶仍可用原密碼登入，管理員識別是 admin@touka.io；真實管理員登入原因仍 UNKNOWN。本批不增加 raw bcrypt fallback、username alias、帳戶重設或秘密 log。獨立唯讀診斷工具只在合成資料驗證；真讀取另依維護者的精確例外，與來源修復驗收分開。GUI 延期。

回復只對本批來源 diff 作人工審閱後普通 revert，不回復或修改真實 users／密碼／data；前批 f613ee73 已提交，本批無 Git 提交或部署授權。
