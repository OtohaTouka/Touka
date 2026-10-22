# Soul Studio 儲存回饋與返回原頁面實作計畫

日期：2026-09-14。狀態：**計畫通過審查**；當時僅有計畫，未實作、測試、提交或發布，這個結論也不是最終審查或 release acceptance。核准後的實作狀態見文末。P11 的舊授權不適用本計畫的實作。

## 需求、基準與證據

需求：輸入密碼後要顯示驗證正確或錯誤；驗證正確且修改完成時，跳回原頁面。本計畫假設「原頁面」是**同一靈魂進入編輯前的唯讀詳細頁**，保留選取、頁籤與捲動；不跳聊天、不呼叫 history.back。

基準為已發布 S55 `927ce096f9fe297d6aca329b6cc8359993da1278`；以下行號均屬此基準：

- `SoulStudio.tsx:228–283,634–682`：單一 POST 同送變更與密碼；成功已有關框／退出編輯，失敗僅 toast、保留視窗。
- `app/layout.tsx:48` 已掛 Toaster；未證實被遮蔽。`connections.ts:252–286` 的前置 refresh 不在原 timer 內，未設連線 timeout 時為 0。
- `routers/souls.py:36–55,219–284`：後端重驗人類 admin 密碼，完成寫入才回 `{status:"success",soul_id}`。本次沒有 fresh runtime 請求證據，不能指定唯一現場根因或判定資料是否已寫入。

## 場景與驗收

| 前提／動作 | 必須可見的結果 |
|---|---|
| 編輯後輸入密碼，服務端確認保存成功 | 關密碼框、退出編輯、回同一靈魂唯讀頁；持續顯示「密碼驗證通過，修改已儲存」，不只 toast、不自動消失。 |
| 回傳 canonical `AUTH_REAUTH_FAILED` | 密碼框內 inline alert 說明驗證失敗；保留草稿、清空密碼、焦點回密碼欄，可重新輸入。 |
| 權限不足、欄位驗證錯誤、儲存錯誤 | 顯示各自 SSOT 訊息；不統稱密碼錯誤，不自行宣稱「密碼正確」。保留草稿、清空密碼。 |
| 網路／逾時／回應格式不符，結果不明 | 顯示「尚無法確認是否已儲存」與核對提示；不自動重送、不宣稱未寫入。 |
| 保存已確認，後續重新整理失敗 | 保留成功區塊，另顯示「修改已儲存，但重新整理未完成」；可手動重讀，不能退回保存失敗。 |

成功／警告位於返回頁面的固定回饋區，使用 `role="status"`；錯誤用 `role="alert"`、`aria-describedby`，密碼錯誤加 `aria-invalid`，處理中有可讀文字與 `aria-busy`。成功後焦點回編輯入口並避免焦點捲動改變原位置。

## 設計與邊界

1. 新增領域 hook `useSoulSave` 管理單一 operation、回饋、期限及取消；元件只負責表單／呈現與資料映射，不增加一組散落的 busy/error states。API 仍由 `fetchTouka` 發送；輸入／輸出 alias 引用生成的 `types/api.ts`，置於 `lib/types.ts`，不重宣告後端模型。UI 狀態可用局部 discriminated union。
2. 只有 HTTP 成功且解析為既有 string-map 契約、`status === "success"`、`soul_id` 等於送出目標，才標成功；不以 2xx、密碼非空或前端比對當認證結果。不新增 verify-password 端點、不拆兩次提交。
3. click／Enter 共用提交入口，ref 在第一個 await 前互斥，避免同一事件輪雙送；busy 期間鎖編輯、確認與框內取消／Escape／外點關閉，明示處理中。未送出時取消只清密碼與提示，保留草稿。
4. 綁定 Proxy、soul ID、operation generation。切魂／換 Proxy／unmount 立即使操作失效、abort 並清密碼；舊回應不能關新視窗、覆寫新草稿或發成功提示。返回原頁面的成功回饋只屬目前目標，不跨靈魂殘留。
5. 保存 hook 設單一命名的 **30 秒 UI 等待上限**；若既有連線配置有更短的有效期限，取較短者。以 `getSystemEpoch()` 計算，timer 只作排程。局部 AbortSignal 加整段 deadline 涵蓋前置 refresh 等待、POST 與成功／錯誤 body 解析；不能只 Promise.race 而讓延期 POST 繼續送出。每個 continuation 都核 generation／signal，清 timer、處理底層 rejection。共用 single-flight refresh 可能仍自行完成，不擅自取消其他使用者，也不聲稱 abort 能撤銷伺服器寫入；瀏覽器暫停時依恢復後 deadline 判斷，不宣稱即時系統硬期限。
6. canonical 成功一旦確認即鎖定「已保存」。先以本次已確認的提交值更新同魂唯讀呈現，再獨立刷新；讀回成功才用服務端值更新，不假稱本地值是 fresh GET。刷新另有局部 30 秒等待，成功提示不等待它。`refreshAgents` 現為 void／吞錯（`AgentProvider.tsx:19,34–49`），窄改成 `Promise<boolean>` 如實回報，原其他呼叫可忽略結果；`loadSouls` 同樣提供結果與 generation 檢查。成功後使用背景刷新，不沿原 `setIsLoading(true)` 以全頁 loading 取代詳細頁、掩蓋成功區或重置捲動。刷新失敗／逾時只改同步警告，晚到結果不得改目前表單。保留選取、頁籤與原捲動，不 remount 整個詳細頁。
7. Provider 最小改動維持無參數呼叫及原刷新副作用；非 2xx／throw 回 false，不再被保存流程誤認同步成功。現有呼叫只有 Provider 初始化／auth-change（54、60 行）及 SoulStudio 建立／共用確認（190、274 行）：前兩者、建立與刪除可沿原行為忽略 boolean，保存分支才消費結果；不重做全域 refresh。以現有呼叫端及測試 mocks 的型別檢查確認相容，回歸既有 `AgentProvider.authSelfHeal.test.tsx` 與 create/delete 行為，不增加全域認證改動。
8. 結果未知時保留草稿，提供使用者明確觸發的唯讀重讀與核對；不自動覆蓋草稿或重試 POST。成功後重讀失敗的按鈕也只能 GET，不能第二次 POST。刪除仍走原 delete 語意，本次保存狀態不得污染共用密碼框的刪除分支。

## 未來精確檔案清單

以下是實作候選範圍，**本輪均不修改**：

| 路徑 | 用途 |
|---|---|
| `ToukaChat/src/components/layout/SoulStudio.tsx` | hook 接線、inline 回饋、返回與 loadSouls 結果 |
| `ToukaChat/src/hooks/useSoulSave.ts`（新） | 局部保存與生命週期 |
| `ToukaChat/src/hooks/__tests__/useSoulSave.test.ts`（新） | 回應、期限、競態 |
| `ToukaChat/src/components/layout/__tests__/SoulStudio.crud.test.tsx` | 真正 UI 結果及 delete 回歸 |
| `ToukaChat/src/lib/types.ts` | 生成契約 alias |
| `ToukaChat/src/lib/providers/AgentProvider.tsx` | 僅 refresh 結果回報 |
| `ToukaChat/src/lib/providers/__tests__/AgentProvider.test.tsx` | refresh 結果與既有選取行為 |
| `ToukaChat/src/locales/zh-TW/translation.json`、`ToukaChat/src/locales/en/translation.json` | 同步語意 key |
| `docs/architecture/CHAT_INTERFACE.md`、`tests/README.md`、`HANDOVER.md` | 實作契約、實際測試、當前交付狀態 |

不改共用 `connections.ts`／auth refresh、後端端點、schema、鎖檔、Dockerfile 或頁面導航。若局部設計無法滿足測試，帶具體缺口回到計畫審查調整範圍，不擴大重構。

## TDD、SSOT 與安全

先於 S55 寫 Vitest RED：持續成功／inline 密碼錯誤、非密碼錯誤分類、畸形或錯 soul ID 的 2xx、refresh 失敗不降格、雙擊／Enter 單 POST、取消與切魂 late response、未完成 refresh／body 的 deadline；mock 保持 `fetchTouka(proxyUrl,path,options)` 與 canonical 回應。至少一組以真 `fetchTouka` 加 mock transport 驗證 refresh 延遲解除後不補送已 abort 的 POST。RED 應因現有 toast-only、僅查 res.ok、缺乏失效／期限而失敗，不因漏 provider 或假回應介面。再 GREEN、全前端回歸及 delete 正反控制；不預填案例數或 PASS。

訊息沿 `errors.json`／`errorMsg`；只由已知 canonical code 判別密碼錯誤，未知 body／exception 不直接展示。新增說明／成功文案走翻譯目錄；無新跨端錯誤碼。沿 ADR-0078、0093 的可觀察性／i18n 原則與既有安全邊界，無新跨模組 authority，**不新增 ADR 編號**。

本次涉及密碼與敏感表單：只留記憶體；密碼在終態／取消／離開清除，草稿在失敗／結果未知時保留，成功或明確放棄編輯才釋放。兩者均不落 localStorage、URL、log、分析事件或測試快照，不宣稱 secure erase。原 AuthService 的 SHA256→Bcrypt、角色／所有權與速率限制保持，不新增前端登入繞路、token 或資料庫操作。所有測試用假密碼／sandbox，不直接讀寫真 data、靈魂或 .env。

## 實作、驗證與發布界線

取得實作授權後，由已發布 S55 建立乾淨的新分支；不帶入既有工作樹未提交的變更與 index，也不改凍結的 S55。驗證需在新候選綁定來源：Node/npm 依鎖定版本跑 `npm run test`、ESLint／typecheck／i18n 守衛與 build；3.12 venv PATH first 跑 `python -B -m pytest tests/`、`python scripts/guard_all.py` 及既有 sandbox smoke。網路 audit／發版等另按當次範圍處理，不用舊 P11 綠燈替代新結果。

風險為 STANDARD 的 UX／非同步變更，認證與未知寫入結果須專項核對。候選回復採撤回本次精確 source patch；不回復使用者已成功保存的靈魂內容。部署回復另走當次已驗證且獲授權的映像流程，不自行以舊 compose 管理現場。計畫階段只做審查；實作、最終審查、Git、qualification、發布與 live 操作均未執行，也不由此計畫自動授權。

---

## 核准後實作狀態（2026-09-14）

維護者後續核准開始實作。上述計畫階段文字保留不改；本節記錄之後的執行狀態。實作在以 S55 為基準的獨立分支進行（工作目錄與原始測試證據未入版控）。

實作完成、待最終審查：原 12 檔加本計畫，以及經核准的 `ToukaChat/src/components/layout/__tests__/SoulStudio.test.tsx` 既有 mock 契約調整，合計 14 檔。沒有後端、認證／發版 authority、schema、lock 或共用 fetch 變更。跨 Proxy 的來源訂閱沿既有事件，普通／背景 GET 一律 generation 隔離；未知結果核對呈現草稿與服務端值的一致／差異欄位，固定成功區位於 scroll area 外。

新增 39 案例，focused55P、最終實體依賴下全 Chat1226P／234檔、完整 tests tsc／build9pages／ESLint0error；唯一一個 numeric generation cleanup warning 經審查接受。Python full9313P10skip、smoke24/24；42non-audit guards 原 rc0，22/23另作fresh原audit，不能合稱單次44aggregatePASS。原生成450CR漂移已精確restore，全部失敗保留。Python full 後僅 TS test fixture defaults 與文件變更，適用性依 exact closure 明示 REUSED_NOT_FRESH。

記錄當時尚未 commit、push、發 PR、qualification、發布或部署，也沒有本修正的現場保存測試；已發布的 S55 不變。後續操作仍受授權邊界約束。
