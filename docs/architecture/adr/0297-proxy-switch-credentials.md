# ADR-0297：登入憑證只送回發它的後端，換後端要確認並真的登出

- 狀態：✅ 已實作。一頁計畫由維護者核准，計畫經 3 輪審查。
- 日期：2026-11-05
- 決策者：維護者
- 相關：ADR-0131（本機領卡限定）、ADR-0144（續期與 session 死亡）、ADR-0295（實際驗證）。
- 影響範圍：`ToukaChat/src/lib/connections.ts`、`ToukaChat/src/lib/healthResponse.ts`（新）、`ToukaChat/src/lib/SystemStatusContext.tsx`、`ToukaChat/src/contexts/AuthContext.tsx`、`ToukaChat/src/hooks/settings/useConnectionSettings.ts`、`ToukaChat/src/hooks/useActiveProxyUrl.ts`（新）、`ToukaChat/src/components/layout/SoulStudio.tsx`、`ToukaChat/src/app/connections/page.tsx`、`ToukaChat/src/app/login/page.tsx`、`ToukaChat/src/app/register/page.tsx`、翻譯。後端、API 與 `openapi.json` 不變。

## 背景

瀏覽器的後端網址放在 `touka_connections` 這份清單裡，憑證放在記憶體。查證時用沙盒測試攔實際請求（假的新舊後端、真的登入與畫面程式），得到：

- 設定頁存檔會把整份清單送到新網址，含本機還沒遮罩的金鑰；新網址拒絕時顯示「儲存失敗」，其實網址已換、人已登出。
- 打開設定頁時，後端存的清單可以改掉這個瀏覽器的網址，人還是登入狀態，下一個請求就帶著憑證到那裡。後端預設範本就有一筆 `localhost:8000`，所以不是用這個網址連後端的管理者，打開設定頁就會碰到。
- 別的分頁改了網址時，這個分頁還是登入狀態，下一個請求同樣帶著憑證到新網址。
- 設定頁改網址時憑證目前有清掉，但只是表單裡第三方下拉內部寫法的副作用；換成普通元件，事件就早於寫入，憑證留著並送到新網址。

只把「先寫入再發事件」修好不夠：擋不住後兩條，還會讓每次改網址都走到送清單那條路。

## 決定

1. **憑證綁發它的後端**：`connections.ts` 記下發卡的網址（`_tokenOrigin`），`fetchTouka`、`fetchChatStream` 送出前用 `cardFor(proxyUrl)` 比對，不符就不帶、不先續期、401 也不續期重送。登入與續期綁回應它的網址，本機領卡綁發它的網址。比對都經 `normalizeProxyUrl`。這一條擋住所有路，包含以後新增的寫入與通知還沒到的空檔。
2. **換後端是這個瀏覽器自己的決定**：`keepActiveProxy` 讓後端的清單算出的網址和目前不同時，Proxy 項目留本機的，其他連線用後端的。`fetchConnections` 的回應到達時網址已換就丟棄。
   - Proxy 項目一律用本機的，同網址存檔時會覆蓋後端那筆；`ToukaProxy/src/services/models/manager.py` 把 Proxy 當控制面、不當上游，沒有路由損失。
   - 隔離的封鎖埠（`services/isolation/block_ports.py`）會讀清單全部條目，所以寫上後端的 Proxy 項目（含切換後留下的舊項目）的埠也會被封鎖（只算非公網主機，80、443、53 除外）。同一次執行只多不少；重啟後依清單重算，被蓋掉的舊 Proxy 項目的埠就不在其中。後端自己的埠由 `profiles.blocked_ports` 固定涵蓋。
3. **存檔換網址改成明確的流程**（維護者 2026-11-05 決定）：確認 → 探測新網址 → 向舊後端登出 → 切過去 → 回登入頁。
   - 探測不帶任何憑證、不跟隨轉址，要連得上而且回應符合健康檢查的形狀才算（`isProxyReachable`，5 秒）。
   - 這段時間可能已登出或別的分頁已換網址，確認後與探測後都再核對一次（`isRequestScopeCurrent`），不符就停。
   - 先登出再切：舊後端刪掉 cookie，新網址第一個請求就不帶。舊後端連不上時最多等 5 秒照樣切。
   - 切換時清單不送給任何後端。
   - 同網址存檔走原流程；`localhost` 換 `127.0.0.1` 這種寫法不同的同一台後端也算換網址。
4. **換網址就是真的登出**：等於按一次登出，這個帳號在舊後端所有裝置都要重新登入。舊憑證本來就不能用在新後端；只有真的登出，同主機換埠時才不留長效 cookie。
5. **別的分頁改網址時，這個分頁也登出**：`AuthContext` 聽 `storage`（連線清單變更，或整個 storage 被清掉）。設定頁的流程已先登出，這裡沒有卡就不多打。
6. **先寫再發事件**：`publishConnections` 是寫入清單與通知的唯一入口，收到事件的人讀到的一定是新網址。
7. **登入頁和註冊頁顯示目前的網址**：打密碼前看得到密碼會送去哪，換完也看得到結果。
8. **不把 Toaster 移出 AuthGuard**：登出時整棵樹含 Toaster 卸載，切換成功的提示看不到。但移出去會讓確認提示活過登出與換帳號，有人按下去就用新身分執行舊畫面問的動作（全系統有二十多個這種提示）。所以依既有做法（`ReloginNotice.tsx`）把結果放在登入頁。

## 沒有做

- **沒讀到清單就存檔會蓋掉後端的連線與金鑰**：`GET` 失敗或還沒回來時存檔，後端只收到本機那份。修之前就有這個問題，另外修。
- **登入頁加改網址的地方**：新後端之後掛掉、或連得上但沒帳號時，未登入的人只能清瀏覽器資料。第 3 點只擋得住「當下就連不上或不是 Touka」。
- `hooks/useAgentSSE.ts` 用 `getAccessToken()` 加原生 `fetch`，不走統一出口；目前沒有生產呼叫端，提議另外刪掉。
- 沒有入口的 `/connections` 舊頁只修寫入順序，不加確認與探測。

## 已知限制

- 舊後端連不上時登出不生效，長效 cookie 留著；新網址和舊的在同一台主機、只差埠號時，之後的請求會帶過去。
- 別的分頁與舊頁只靠 `changeOrigin` 的登出，新網址的第一個請求可能早於登出完成；同主機換埠才有影響。
- 切換時別的分頁也改了網址：探測階段有擋；登出階段這個分頁不再蓋掉，但可能多打一次登出，沒有壞處。
- 切換後舊連線項目暫留本機；新後端回非空清單就被換掉，新後端清單是空的或讀取失敗時，之後在新後端存檔可能把它們一起送過去。舊 Proxy 項目本身會留在清單。清單裡還沒遮罩的金鑰仍存在瀏覽器本機。
- `session_expired` 的提示同樣隨 Toaster 卸載（讀程式碼推論，沒有實測）。

## 驗證

`ToukaChat/src/contexts/__tests__/AuthContext.proxy-switch.security.test.tsx` 用真的 AuthProvider、設定頁與 `connections.ts`，只假 fetch 與提示框，攔每個請求的憑證與內容；`ToukaChat/src/lib/__tests__/connections.token-origin.security.test.ts` 測統一出口與純函式；`ToukaChat/e2e-real/connection-switch.spec.ts` 用真瀏覽器、真後端與一支放行 CORS 的小伺服器，看新後端實際收到什麼，包含同主機換埠時的 cookie。
