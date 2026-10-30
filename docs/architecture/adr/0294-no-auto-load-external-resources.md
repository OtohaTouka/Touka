# ADR-0294：模型輸出的外部資源不自動載入、本機連線資訊只給自己（PR A：ToukaChat）

- 狀態：✅ 已實作（PR A：ToukaChat 的開發終端伺服器、Markdown 圖片、流程圖；IDE 寫檔與連線資訊檔在 PR B 補一節）。對外存取盤點的後續；一頁計畫由維護者核准（外部圖片選甲），計畫經 3 輪審查。成果審查第 1 輪發現流程圖在畫圖當下就會載入外部圖片，改成在沙箱框裡畫（決定 4）。
- 日期：2026-10-30
- 決策者：維護者
- 相關：ADR-0014（錯誤碼）、ADR-0093（i18n）、ADR-0114（artifact 預覽的 sandbox 與 CSP）、ADR-0214（測試沙盒）、ADR-0250（ToukaChat 的 runtime 產生與 knip）、ADR-0293（不跟隨連結）。
- 影響範圍：ToukaChat 新增 `src/lib/markdownImagePolicy.ts`、`src/components/markdown/SafeMarkdown.tsx`、`src/lib/artifacts/mermaidConfig.ts`、`src/lib/artifacts/mermaidSandbox.ts`、`src/lib/artifacts/sanitizeMermaidSvg.ts`；修改 `components/chat/MessageItem.tsx`、`components/artifacts/ArtifactView.tsx`、`scripts/build-artifact-runtime.mjs`（多產生 `src/generated/mermaidRuntime.ts`）、`scripts/guard_design.ts`（不掃 `src/generated`）、`package.json`（`dev`）、`knip.config.ts`、`Dockerfile`、`Dockerfile.prod`（註解）、`README.md`、翻譯（`markdown.*`）；刪 `src/lib/terminal_server.mjs`；`docs/guides/deployment_guide.md`。

## 背景

- ToukaChat 的 `npm run dev` 會一起啟動一個終端伺服器（`terminal_server.mjs`，socket.io＋node-pty）：不用登入，任何網頁都能叫它開 shell。正式映像本來就不含它，前端也沒有程式連它。
- 聊天訊息與 artifact 的 Markdown 用 `react-markdown` 顯示，`![](https://…)` 會直接載入外部圖片。模型（或被模型讀進來的網頁、檔案）可以把對話內容放進網址，瀏覽器一載入就送出去，還帶著 Referer。
- 流程圖（mermaid）的圖裡可以寫設定指令改 `themeCSS`、`fontFamily`、`altFontFamily`；mermaid 11.16.1 只檢查大括號成對，`@import url(https://…)` 進得去。原本的 DOMPurify 設定擋不到樣式裡的網址，還把 `foreignObject` 整個拿掉，流程圖的文字標籤本來就顯示不出來。
- mermaid 畫圖時會先把圖放進頁面量尺寸。圖裡的外部圖片在這時就會載入：文字標籤裡的 `<img>`（流程圖、心智圖、狀態圖、類別圖都可以寫）、圖片節點（`@{ img: … }`）、sequence 角色圖示。這發生在拿到輸出之前，消毒輸出擋不到（在無頭 Edge 記錄網路請求實測確認）。

## 決定

1. **拿掉開發終端伺服器**：`dev` 只跑 `sync-api`、`gen:artifact-runtime` 與 `next dev`；刪 `terminal_server.mjs`。只有它用到的套件（`node-pty`、`socket.io`、`socket.io-client`、`xterm-addon-fit`、`concurrently`）留到下一批依賴升級一起刪：開發用 `Dockerfile` 的 `npm rebuild node-pty`（供應鏈測試還釘著）與 knip 的兩個 `ignoreDependencies` 也到那時才拿掉。
2. **Markdown 圖片的判斷只有一份**：`markdownImagePolicy.decideMarkdownImage(src, pageOrigin, backendBase)`，純函式。
   - 一律先 `new URL(src, pageOrigin)` 解析再比 `origin`，不比字串開頭（`//evil`、`/\evil`、夾 tab 或換行的網址解析後都是外部來源）。
   - 載入：和前端頁面同來源、和目前連線的後端同來源、後端是本機（`localhost`／`127.0.0.1`／`[::1]`）時同協定同埠號的另一種本機寫法、`data:image/*`、和頁面同來源的 `blob:`。
   - 其他不載入：http(s) 的回傳主機名稱與網址（給連結）；帶帳號密碼的只給主機名稱、不給連結；其他協定什麼都不給。
3. **只有一個元件顯示模型輸出的 Markdown**：`SafeMarkdown` 包住 `ReactMarkdown`，`img` 固定交給 `MarkdownImage`（呼叫端傳的 `img` 會被蓋掉），不收 rehype 外掛（原始 HTML 不會顯示）。允許的圖片加 `referrerPolicy="no-referrer"`；不允許的顯示「外部圖片（未載入）＋網站名稱」，是 `target="_blank" rel="noopener noreferrer"` 的連結，點了才在新分頁開（維護者選甲）。`MessageItem` 兩處與 `ArtifactView` 的 `MarkdownRenderer` 改用它；`src/` 裡只有 `SafeMarkdown.tsx` 可以匯入 `react-markdown`，全庫不得出現 `rehype-raw`。
4. **流程圖在沙箱框裡畫**（`mermaidSandbox.renderMermaidInSandbox`）：
   - 隱藏的 iframe，沿用 artifact 預覽的 `ARTIFACT_SANDBOX`（`allow-scripts`，沒有 `allow-same-origin`，和 Touka 不同來源）與 `ARTIFACT_CSP`（圖片只准 `data:`／`blob:`、不准連線，ADR-0114）。畫圖當下的外部載入都被 CSP 擋下。
   - 框裡執行的是 mermaid 自己的瀏覽器版（`mermaid/dist/mermaid.min.js`），由 `gen:artifact-runtime` 原樣複製成 `src/generated/mermaidRuntime.ts`（不進版控），經 `escapeForScript` 放進框裡。頁面只在第一次畫圖時以動態匯入載入它；產品程式不匯入 `mermaid`（型別除外）。設計 token 檢查（Guard #4）和 ESLint、knip 一樣不掃 `src/generated`（第三方程式裡有寫死的顏色）。
   - 框只建一次、各圖共用。頁面只收這個框送來、請求編號對得上的回覆；框裡只收父頁面送來的請求。畫好的 SVG 字串交給 `sanitizeMermaidSvg`（決定 6）後才放進頁面。
   - 框載入最多等 60 秒（第一次可能很慢，實測無頭 Edge 偶爾要二十多秒），每張圖最多等 15 秒；載入失敗或某張圖逾時就移除框，下次畫圖重建；當時已經在等這個框的其他圖不會提早結束，仍各自等到 15 秒。
5. **流程圖的設定**（`mermaidConfig.MERMAID_CONFIG`，送進框裡套用）：`secure` 寫成「mermaid 預設 6 項（`secure`、`securityLevel`、`startOnLoad`、`maxTextSize`、`suppressErrorRendering`、`maxEdges`）＋`themeCSS`、`fontFamily`、`altFontFamily`」，框裡再併上執行中版本的預設。傳入的 `secure` 會整份取代預設清單，所以預設項目要一起寫；檢查會往下層遞迴，同名的巢狀欄位一起擋。其餘設定照舊（`securityLevel: "strict"` 等）。
6. **流程圖輸出的消毒**（`sanitizeMermaidSvg`）：專屬的 DOMPurify 實例（掛鉤不影響全域），設定 `svg`、`svgFilters`、`html` 三個 profile，加 `foreignObject` 並設為 HTML 整合點，所以標籤裡的 HTML 照 HTML 解析、文字留著。規則套用在所有元素與屬性上：
   - 會載入網址的屬性（名稱完全相符，`data-id` 這類不受影響）：`href`、`xlink:href`、`src`、`poster`、`background`、`action`、`formaction`、`data`、`cite`、`longdesc`、`usemap`、`codebase`、`archive`、`manifest`、`icon`、`profile`、`lowsrc`、`dynsrc` 只留 `#` 開頭的內部參照，其他值整個屬性移除。例外：`<a>` 的 http(s) `href`／`xlink:href` 保留（mermaid 的 `click` 連結，點了才導覽），並加 `target="_blank"`、`rel="noopener noreferrer"`。
   - 一個值可以列多個網址的屬性（`srcset`、`imagesrcset`、`ping`）一律移除。
   - 其他屬性的值含 `@import`、`image-set(`、反斜線，或不是 `#` 開頭的 `url(`，整個屬性移除。
   - `<style>` 與 `style` 屬性以 `;`、`{`、`}` 切段，只拿掉命中上一條的那一段，分隔符號與其他樣式照常（mermaid 的樣式全放在同一個 `<style>`，整段拿掉圖會變黑）。只刪不加，剩下的片段拼不出新的網址。
   - 比對不分大小寫；`url(` 後面先去掉空白與引號再看是不是 `#`。反斜線一律算命中，因為 CSS 跳脫可以把 `u\72l(` 讀成 `url(`。
7. **新文字**走 i18n：`markdown.external_image_blocked`、`markdown.external_image_open`（`zh-TW`、`en`）。

## 已知的界線

- 外部圖片網址是使用者自己貼的也一樣不自動顯示，要點開。點開時網址照樣送出去，只是由人決定。
- `react-markdown` 預設的網址轉換會先拿掉 `data:` 與 `blob:`，所以 `SafeMarkdown` 實際上看不到這兩種網址，行為和改之前相同；判斷函式仍照規則處理，以後換掉轉換時不用另外改。
- 流程圖有被判定可疑的樣式時，只拿掉那一條宣告或那個選擇器；那條規則管的顏色或字型可能變回預設。任何含反斜線的樣式片段也會被拿掉。
- 圖裡的外部圖片（標籤 `<img>`、圖片節點、角色圖示）畫不出來：框裡載入被擋，輸出也拿掉。用外部圖片當圖片節點時，mermaid 會因為圖片載入失敗而整張圖畫不出來。
- 第一次顯示流程圖要先載入約 3.6 MB 的 mermaid 瀏覽器版並啟動框，通常一兩秒；實測無頭 Edge 偶爾要二十多秒才開始執行框裡的程式，所以最多等 60 秒，超過就算這次沒畫成（畫面留著上一張畫好的圖，沒有就顯示等待語法的提示）、下次畫圖重建框。某張圖 15 秒沒畫完也會重建框。
- 內容每變一次就送一次畫圖請求，框裡依序畫，排隊時間也算在 15 秒裡。實測無頭 Edge：40 行的流程圖每 30 毫秒長一截，最後的完整圖約 0.4 秒畫好；120 行、每 20 毫秒長一截時，最後的完整圖排隊超過 15 秒而沒畫成，畫面停在較早畫好的半成品，要切到別的 artifact 再切回來才會重畫。「同一張圖的舊請求由新的取代」列為之後的修正。
- 框整頁共用。如果哪天找到能在 mermaid 的 `strict` 模式裡執行程式的寫法，那段程式會留在框裡，看得到之後每張圖的文字，還能讓框自己導向外部網址把內容帶出去（CSP 擋不了框自己導覽）。前提是 mermaid 本身有漏洞；artifact 預覽框本來就執行模型寫的程式，有同樣的導覽問題。「每次對話重建框」與「artifact 預覽框自己導覽」都列為之後的候選。
- 這次不加整個 ToukaChat 的內容安全政策（CSP）；流程圖框與 artifact 預覽框用的是 ADR-0114 的 CSP。

## 後果

- 跑 `npm run dev` 只開網頁；需要終端的使用者用 IDE 或系統自己的終端。正式映像不受影響。
- 模型回覆與 artifact Markdown 裡的外部圖片變成一個小標籤（圖示＋「外部圖片（未載入）」＋網站名稱）。Touka 自己的圖片（生圖結果、上傳的檔案）照常顯示。
- 流程圖的文字標籤現在看得到了（以前被 DOMPurify 拿掉）；圖裡的 `click` 連結在新分頁開。
- mermaid 不再跟著頁面載入，第一次顯示流程圖時才載入。
- 不改後端、不改 `openapi.json`。撤回只能 revert。

## 替代方案

- **點了直接在聊天裡載入圖片**（乙）：載入當下網址一樣送出去，和自動載入只差一次點擊，標籤也看不出會送去哪裡。不採用。
- **只靠 mermaid 的 `secure`，不消毒輸出**：名稱不同的樣式欄位擋不到，mermaid 升級後可能多出新的。不採用。
- **只消毒輸出，mermaid 照樣在頁面上畫**：外部圖片在畫圖當下就載入了。不採用。
- **送進 mermaid 之前先擋掉 `<img>`、圖片節點這些寫法**：能放外部圖片的寫法很多（標籤、Markdown 字串、圖片節點、角色圖示、C4 的圖示），mermaid 升級還會再多，清單永遠補不完。不採用。
- **整段拿掉流程圖的 `<style>`**：mermaid 的樣式全在裡面，圖會變黑。不採用。

## 驗證

- `tests/lint/test_chat_dev_terminal_security.py`：`dev` 只啟動 `next dev`、`terminal_server.mjs` 不存在、`src/` 沒有程式開 socket 伺服器或 pty。
- `tests/lint/test_chat_markdown_render_security.py`：`src/` 裡只有 `SafeMarkdown.tsx` 匯入 `react-markdown`（`from`、`import()`、`require()` 都算），它不收 rehype 外掛，`package.json` 與 `src/` 沒有 `rehype-raw`；產品程式不匯入 `mermaid`（型別除外），`mermaidRuntime` 只由 `mermaidSandbox.ts` 匯入，`ArtifactView` 經 `renderMermaidInSandbox` 畫圖；直接塞 HTML 只有流程圖一處、而且經 `sanitizeMermaidSvg`。
- ToukaChat Vitest：
  - `src/lib/__tests__/markdownImagePolicy.security.test.ts`（外部、`//evil`、`/\evil`、tab／換行、大寫協定、帳密、`javascript:`、`data:text/html`、不同埠的本機、別處的 `blob:` 不載入；相對路徑、`data:image/png`、同來源、後端、本機另一種寫法載入）。
  - `src/components/markdown/__tests__/SafeMarkdown.test.tsx`、`components/chat/__tests__/MessageItem.test.tsx`、`components/artifacts/__tests__/ArtifactView.markdown.test.tsx`（標籤與連結、`no-referrer`、帳密與非 http(s) 不給連結、呼叫端換不掉 `img`）。
  - `src/lib/artifacts/__tests__/mermaidSandbox.security.test.ts`：框的 sandbox 與 CSP（寫死要求的值，共用的 `ARTIFACT_CSP` 放寬或指令重複時會紅）、執行檔轉義、框裡只收父頁面的請求並併上執行中版本的保護清單、頁面只收這個框且請求編號對得上的回覆、共用一個框、錯誤與逾時、載入失敗或逾時後重建。
  - `src/lib/artifacts/__tests__/sanitizeMermaidSvg.security.test.ts`；`tests/unit/ArtifactView.security.test.tsx`（畫圖經沙箱框，框回傳的外部網址不會進頁面）。
- 流程圖測試資料（`fixtures/mermaid/`）是 mermaid 11.16.1 在無頭 Edge 以 `MERMAID_CONFIG` 渲染的真實輸出（網路請求全部擋掉）：
  - 流程圖、sequence、class、gantt、狀態圖、ER、心智圖七種正常輸出，消毒前後樣式完全相同、文字與標籤數都在。
  - 兩份用沒有加保護欄位的設定、以 frontmatter 的 `themeCSS`／`fontFamily` 實際帶進外部網址，消毒後只少了外部網址。
  - 三份同樣的注入（frontmatter 與 `%%{init}%%`）改用 `MERMAID_CONFIG`，輸出沒有外部網址。
- 以產品程式（`mermaidSandbox.ts`＋`sanitizeMermaidSvg.ts`＋產生的 mermaid 瀏覽器版）在無頭 Edge 實測 16 種圖、連跑 5 次：外部網址寫在標籤、圖片節點、角色圖示、設定指令與樣式裡，一個外部請求都沒有、消毒後沒有外部網址；正常的五種圖文字都在。
- Guard #4：不排除 `src/generated` 時掃到產生的 mermaid 瀏覽器版而變紅；排除後通過，在 `src/lib` 放一個寫死顏色的檔案仍會擋。
- 故意改壞 31 項各自讓指定的測試因對的原因變紅，改回後全綠：
  - 第 1 批 12 項：`dev` 加回伺服器、改成字串開頭判斷、拿掉 `referrerPolicy`、`MessageItem` 改回 `ReactMarkdown`、`secure` 少三個欄位或只寫三個欄位、拿掉樣式規則、整段拿掉 `<style>`、`data` 改開頭比對、只看 `href`／`src`、比對分大小寫、擋 `foreignObject`。
  - 第 2 批 13 項：框拿掉 CSP、框加 `allow-same-origin`、框裡或頁面不檢查來源、不比對請求編號、執行檔不轉義、每張圖改用載入的時限、載入改用每張圖的時限、框裡不併預設保護清單、載入失敗不重建、`secure` 拿掉樣式欄位、`ArtifactView` 加回 `mermaid`、`ArtifactView` 不消毒。
  - 第 3 批 6 項：逾時不移除框、逾時移除但不重建、共用 CSP 的圖片放行 `https:`、共用 CSP 多一條指令、共用 sandbox 加 `allow-same-origin`、共用 CSP 開頭多一條放行 `https:` 的重複圖片指令。
