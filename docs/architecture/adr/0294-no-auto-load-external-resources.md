# ADR-0294：模型輸出的外部資源不自動載入、本機連線資訊只給自己

- 狀態：✅ 已實作（PR A：ToukaChat 的開發終端伺服器、Markdown 圖片、流程圖；PR B：IDE 寫檔與連線資訊檔，決定 8、9）。對外存取盤點的後續；一頁計畫由維護者核准（外部圖片選甲），計畫經 3 輪審查。成果審查第 1 輪發現流程圖在畫圖當下就會載入外部圖片，改成在沙箱框裡畫（決定 4）。
- 日期：2026-10-30
- 決策者：維護者
- 相關：ADR-0014（錯誤碼）、ADR-0093（i18n）、ADR-0114（artifact 預覽的 sandbox 與 CSP）、ADR-0214（測試沙盒）、ADR-0250（ToukaChat 的 runtime 產生與 knip）、ADR-0293（不跟隨連結）、ADR-0202（IDE 的 MCP 與 discovery）。
- 影響範圍：ToukaChat 新增 `src/lib/markdownImagePolicy.ts`、`src/components/markdown/SafeMarkdown.tsx`、`src/lib/artifacts/mermaidConfig.ts`、`src/lib/artifacts/mermaidSandbox.ts`、`src/lib/artifacts/sanitizeMermaidSvg.ts`；修改 `components/chat/MessageItem.tsx`、`components/artifacts/ArtifactView.tsx`、`scripts/build-artifact-runtime.mjs`（多產生 `src/generated/mermaidRuntime.ts`）、`scripts/guard_design.ts`（不掃 `src/generated`）、`package.json`（`dev`）、`knip.config.ts`、`Dockerfile`、`Dockerfile.prod`（註解）、`README.md`、翻譯（`markdown.*`）；刪 `src/lib/terminal_server.mjs`；`docs/guides/deployment_guide.md`。ToukaIDE 新增 `src/mcp/noFollowFile.ts`、`src/mcp/discoveryFile.ts`，修改 `src/mcp/workspacePath.ts`、`src/mcp/diffManager.ts`、`src/mcp/handlers/FileHandlers.ts`、`src/extension.ts`、`README.md`；ToukaProxy 修改 `src/protocols/ide/discovery.py`。

## 背景

- ToukaChat 的 `npm run dev` 會一起啟動一個終端伺服器（`terminal_server.mjs`，socket.io＋node-pty）：不用登入，任何網頁都能叫它開 shell。正式映像本來就不含它，前端也沒有程式連它。
- 聊天訊息與 artifact 的 Markdown 用 `react-markdown` 顯示，`![](https://…)` 會直接載入外部圖片。模型（或被模型讀進來的網頁、檔案）可以把對話內容放進網址，瀏覽器一載入就送出去，還帶著 Referer。
- 流程圖（mermaid）的圖裡可以寫設定指令改 `themeCSS`、`fontFamily`、`altFontFamily`；mermaid 11.16.1 只檢查大括號成對，`@import url(https://…)` 進得去。原本的 DOMPurify 設定擋不到樣式裡的網址，還把 `foreignObject` 整個拿掉，流程圖的文字標籤本來就顯示不出來。
- mermaid 畫圖時會先把圖放進頁面量尺寸。圖裡的外部圖片在這時就會載入：文字標籤裡的 `<img>`（流程圖、心智圖、狀態圖、類別圖都可以寫）、圖片節點（`@{ img: … }`）、sequence 角色圖示。這發生在拿到輸出之前，消毒輸出擋不到（在無頭 Edge 記錄網路請求實測確認）。
- ToukaIDE 寫檔前確認路徑在工作區裡，找「最近存在的上層」用的是會跟隨連結的 `existsSync`：指向不存在位置的連結（懸空連結）被當成不存在而略過，寫檔時就跟著連結寫到工作區外面。按「接受」寫差異時沒有再檢查，寫檔也跟隨連結；差異預覽的暫存檔直接放在系統暫存區。讀檔（`read_file` 與差異預覽讀原檔）也跟隨連結，遇到 FIFO 會一直卡住。
- ToukaIDE 的連線資訊檔（含權杖）寫在系統暫存區的 `gemini/ide/`。Linux 的暫存區大家共用：檔案別人讀得到，資料夾也可能是別人先建的或是連結；Touka 讀的時候不看擁有者，別人放一個假的就能把 Touka 導去別處。

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
   - 同一張圖（同一個 `id`）一次只送一個請求進框：前一個還沒回來時，新的內容先等著、只留最新的一份，被取代的呼叫者和它拿同一個結果。串流時內容一直變，不這樣框裡會排隊排到逾時（PR A2 實測：120 行的圖每 20 毫秒長一截，最後的完整圖逾時沒畫成）。
   - 框載入最多等 60 秒（第一次可能很慢，實測無頭 Edge 偶爾要二十多秒）；每張圖最多等 15 秒，從送進框才開始算。載入失敗或某張圖逾時就移除框，下次畫圖重建；移除時其他還在等這個框的圖立刻結束，在新框重送一次（每個請求最多換一次框，載入失敗不重試）。
5. **流程圖的設定**（`mermaidConfig.MERMAID_CONFIG`，送進框裡套用）：`secure` 寫成「mermaid 預設 6 項（`secure`、`securityLevel`、`startOnLoad`、`maxTextSize`、`suppressErrorRendering`、`maxEdges`）＋`themeCSS`、`fontFamily`、`altFontFamily`」，框裡再併上執行中版本的預設。傳入的 `secure` 會整份取代預設清單，所以預設項目要一起寫；檢查會往下層遞迴，同名的巢狀欄位一起擋。其餘設定照舊（`securityLevel: "strict"` 等）。
6. **流程圖輸出的消毒**（`sanitizeMermaidSvg`）：專屬的 DOMPurify 實例（掛鉤不影響全域），設定 `svg`、`svgFilters`、`html` 三個 profile，加 `foreignObject` 並設為 HTML 整合點，所以標籤裡的 HTML 照 HTML 解析、文字留著。規則套用在所有元素與屬性上：
   - 會載入網址的屬性（名稱完全相符，`data-id` 這類不受影響）：`href`、`xlink:href`、`src`、`poster`、`background`、`action`、`formaction`、`data`、`cite`、`longdesc`、`usemap`、`codebase`、`archive`、`manifest`、`icon`、`profile`、`lowsrc`、`dynsrc` 只留 `#` 開頭的內部參照，其他值整個屬性移除。例外：`<a>` 的 http(s) `href`／`xlink:href` 保留（mermaid 的 `click` 連結，點了才導覽），並加 `target="_blank"`、`rel="noopener noreferrer"`。
   - 一個值可以列多個網址的屬性（`srcset`、`imagesrcset`、`ping`）一律移除。
   - 其他屬性的值含 `@import`、`image-set(`、反斜線，或不是 `#` 開頭的 `url(`，整個屬性移除。
   - `<style>` 與 `style` 屬性以 `;`、`{`、`}` 切段，只拿掉命中上一條的那一段，分隔符號與其他樣式照常（mermaid 的樣式全放在同一個 `<style>`，整段拿掉圖會變黑）。只刪不加，剩下的片段拼不出新的網址。
   - 比對不分大小寫；`url(` 後面先去掉空白與引號再看是不是 `#`。反斜線一律算命中，因為 CSS 跳脫可以把 `u\72l(` 讀成 `url(`。
7. **新文字**走 i18n：`markdown.external_image_blocked`、`markdown.external_image_open`（`zh-TW`、`en`）。
8. **IDE 讀寫檔不跟隨連結**：
   - `resolveWorkspacePath` 找最近存在的上層改用 `lstat`（不跟隨），懸空連結算存在，之後 `realpath` 解不開就拒絕；連到工作區裡現有資料夾的連結照常可用。
   - 讀寫只經 `noFollowFile.ts` 的 `readFileNoFollow`、`writeFileNoFollow`（`read_file`、`FileHandlers` 直接寫、`diffManager` 讀原檔與按接受都用）：POSIX 以 `O_NOFOLLOW|O_NONBLOCK` 開檔，開好後只讀寫一般檔案（FIFO、裝置拒絕）；Linux 開好後再從 `/proc/self/fd` 看檔案實際在哪裡，和解析好的路徑不同（中間的資料夾被換成連結）就不讀寫；寫入不在開檔時清空，確認之後才清空再寫。Windows 沒有 `O_NOFOLLOW`，讀寫之前確認最後一段不是連結。
   - `diffManager` 的暫存檔放在 `mkdtemp` 建的私人資料夾（0700），用完或中途出錯都整個刪掉；按接受時重新走一次 `resolveWorkspacePath`，結果和提出時相同才寫，否則不寫並回報原因。
9. **連線資訊檔只給自己**：
   - IDE 寫（`discoveryFile.writeDiscoveryFile`；判斷是純函式 `isSafeTempRoot`、`privateDirVerdict`）：POSIX 上，上層暫存資料夾（`realpath(os.tmpdir())`）的擁有者要是 root 或自己，而且有 sticky 位元或別人不能寫；`gemini`、`ide` 兩層不存在就以 0700 建立，存在就不跟隨連結開啟，要是自己的資料夾，別人能寫就先拿掉群組與其他人的寫入權。檔案先刪舊的，再以 0600、`O_EXCL|O_NOFOLLOW` 建新的；開兩個視窗時被另一個先建，就再刪一次、再試一次。不符合就不寫，跳警告寫明是哪個資料夾。
   - 為什麼檢查後不會被換掉：上層有 sticky 位元（或別人不能寫），別人不能改名或刪除屬於自己的 `gemini`；先收緊 `gemini` 再看 `ide`，`ide` 就在別人不能動的資料夾裡。檢查與收緊都在開好的資料夾上做（`fstat`／`fchmod`），沒有先查再改的時間差。macOS 的暫存資料夾本來就是每人一份。
   - Touka 讀（`protocols/ide/discovery.py`）：POSIX 上從解析過連結的暫存資料夾起，`gemini`、`ide` 兩層要是自己的、別人不能寫、不是連結，否則整個略過；每個檔案以 `open_read_no_follow` 開（路上不跟隨連結、遇到 FIFO 不卡住），用開好的檔案的 `fstat` 確認是自己的、別人不能寫的一般檔案，否則略過。資料夾進不去（例如別人先建了只有他能進的 `gemini`）也是略過，不讓搜尋 IDE 失敗。都只記一行警告。上層不查 sticky：就算 `gemini` 被換掉，開到的檔案擁有者也不會是自己。
   - Windows 的暫存區每人一份，兩邊都照舊（同一個使用者的程式本來就讀得到，跟隨 junction 不會多給它什麼）。Docker 部署不受影響：proxy 容器沒有掛主機的 `/tmp` 或 `gemini/ide`，本來就看不到 IDE 的連線資訊檔。

## 已知的界線

- 外部圖片網址是使用者自己貼的也一樣不自動顯示，要點開。點開時網址照樣送出去，只是由人決定。
- `react-markdown` 預設的網址轉換會先拿掉 `data:` 與 `blob:`，所以 `SafeMarkdown` 實際上看不到這兩種網址，行為和改之前相同；判斷函式仍照規則處理，以後換掉轉換時不用另外改。
- 流程圖有被判定可疑的樣式時，只拿掉那一條宣告或那個選擇器；那條規則管的顏色或字型可能變回預設。任何含反斜線的樣式片段也會被拿掉。
- 圖裡的外部圖片（標籤 `<img>`、圖片節點、角色圖示）畫不出來：框裡載入被擋，輸出也拿掉。用外部圖片當圖片節點時，mermaid 會因為圖片載入失敗而整張圖畫不出來。
- 第一次顯示流程圖要先載入約 3.6 MB 的 mermaid 瀏覽器版並啟動框，通常一兩秒；實測無頭 Edge 偶爾要二十多秒才開始執行框裡的程式，所以最多等 60 秒，超過就算這次沒畫成（畫面留著上一張畫好的圖，沒有就顯示等待語法的提示）、下次畫圖重建框。某張圖 15 秒沒畫完也會重建框。
- 框整頁共用。如果哪天找到能在 mermaid 的 `strict` 模式裡執行程式的寫法，那段程式會留在框裡，看得到之後每張圖的文字，還能讓框自己導向外部網址把內容帶出去（CSP 擋不了框自己導覽）。前提是 mermaid 本身有漏洞；artifact 預覽框本來就執行模型寫的程式，有同樣的導覽問題。「每次對話重建框」與「artifact 預覽框自己導覽」都列為之後的候選。
- 這次不加整個 ToukaChat 的內容安全政策（CSP）；流程圖框與 artifact 預覽框用的是 ADR-0114 的 CSP。
- IDE 讀寫：路徑檢查到開檔之間，中間的資料夾可能被換成連結；一個在背景不停對調資料夾與連結的程式可以一再贏得這個時間差。Linux 開檔後核對實際位置，不符就不讀寫、也不清空；但要建新檔時，檔案可能已經在外面建好（空的）才被發現，建立缺少的上層資料夾（`mkdir` 會跟隨連結）也可能在外面建出空資料夾。macOS 沒有 `/proc`，這個時間差仍在；Windows 連最後一段也只能先查再讀寫。硬連結擋不了（工作區裡指向外面同一個磁碟上檔案的硬連結，寫進去就改到外面那個檔案）。能利用的都是本來就能在工作區裡建連結、執行程式的程式，和 ADR-0293 的界線同類。
- 同一台 Linux 上，`/tmp/gemini` 已經被別的使用者建了，IDE 會拒絕寫連線資訊檔，Touka 就找不到 IDE；警告寫明是哪個資料夾，要請那位使用者或管理員刪掉。
- 後端的 `ide_open_diff` 工具（`tools/editor.py`）讀的是 Touka 自己的 `data/temp/ide`，沒有程式寫那裡，不在這次範圍。

## 後果

- 跑 `npm run dev` 只開網頁；需要終端的使用者用 IDE 或系統自己的終端。正式映像不受影響。
- 模型回覆與 artifact Markdown 裡的外部圖片變成一個小標籤（圖示＋「外部圖片（未載入）」＋網站名稱）。Touka 自己的圖片（生圖結果、上傳的檔案）照常顯示。
- 流程圖的文字標籤現在看得到了（以前被 DOMPurify 拿掉）；圖裡的 `click` 連結在新分頁開。
- mermaid 不再跟著頁面載入，第一次顯示流程圖時才載入。
- IDE 不會再寫到工作區外面的懸空連結；按「接受」時路徑變了就不寫並回報原因。
- Linux／macOS 上連線資訊檔只有自己讀得到；資料夾有問題時 IDE 跳警告、Touka 找不到 IDE，不會去讀別人的檔案。
- 不改 API、不改 `openapi.json`。撤回只能 revert。

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
  - `src/lib/artifacts/__tests__/mermaidSandbox.security.test.ts`：框的 sandbox 與 CSP（寫死要求的值，共用的 `ARTIFACT_CSP` 放寬或指令重複時會紅）、執行檔轉義、框裡只收父頁面的請求並併上執行中版本的保護清單、頁面只收這個框且請求編號對得上的回覆、共用一個框、錯誤與逾時、載入失敗或逾時後重建；同一張圖一次一個請求、新內容取代等著的舊內容且每個呼叫者都有結果（成功或錯誤）、15 秒從送出才算、失敗後不卡住、框被移除時其他圖立刻在新框重送且新框上的請求不受影響、每個請求最多換一次框、等到的框已被移除就改用新框、計時器與監聽器都清掉。
  - `src/lib/artifacts/__tests__/sanitizeMermaidSvg.security.test.ts`；`tests/unit/ArtifactView.security.test.tsx`（畫圖經沙箱框，框回傳的外部網址不會進頁面）。
- 流程圖測試資料（`fixtures/mermaid/`）是 mermaid 11.16.1 在無頭 Edge 以 `MERMAID_CONFIG` 渲染的真實輸出（網路請求全部擋掉）：
  - 流程圖、sequence、class、gantt、狀態圖、ER、心智圖七種正常輸出，消毒前後樣式完全相同、文字與標籤數都在。
  - 兩份用沒有加保護欄位的設定、以 frontmatter 的 `themeCSS`／`fontFamily` 實際帶進外部網址，消毒後只少了外部網址。
  - 三份同樣的注入（frontmatter 與 `%%{init}%%`）改用 `MERMAID_CONFIG`，輸出沒有外部網址。
- 以產品程式（`mermaidSandbox.ts`＋`sanitizeMermaidSvg.ts`＋產生的 mermaid 瀏覽器版）在無頭 Edge 實測 16 種圖、連跑 5 次：外部網址寫在標籤、圖片節點、角色圖示、設定指令與樣式裡，一個外部請求都沒有、消毒後沒有外部網址；正常的五種圖文字都在。
- 串流實測（產品程式，無頭 Edge，先畫一張讓框載入好，再讓內容一截一截變長、每次變化都畫）：PR A 的做法下，120 行每 20 毫秒、200 行每 15 毫秒兩種情況最後的完整圖都逾時；PR A2 之後三種情況（另有 40 行每 30 毫秒）都沒有逾時，最後的完整圖分別在 0.06、0.22、0.6 秒內畫好。
- Guard #4：不排除 `src/generated` 時掃到產生的 mermaid 瀏覽器版而變紅；排除後通過，在 `src/lib` 放一個寫死顏色的檔案仍會擋。
- ToukaIDE Vitest（junction 在 Windows 與 Linux 都建得了；建檔權限、檔案連結、裝置與 FIFO 只在 POSIX 跑，GitHub CI 的 `toukaide` 工作在 Linux 跑；Linux 容器裡 153 個全過）：
  - `workspacePathSecurity.test.ts`：最後一段或路上是懸空連結（指向外面或工作區裡）都拒絕；連到工作區裡現有資料夾的連結、還沒建立的資料夾照常可用。
  - `noFollowFile.test.ts`：讀檔與新建、覆寫；最後一段是連結或懸空連結時明確拒絕、目標不變也不被建立；資料夾與開得了但不是一般檔案的裝置（`/dev/null`）拒絕；讀寫 FIFO 都不卡住；Linux 上中間的資料夾是連結時不讀不寫、開到的檔案也沒被清空；讀不到 `/proc` 時明確拒絕，不回「檔案不存在」。
  - `diffManagerSecurity.test.ts`：暫存檔在私人資料夾（POSIX 為 0700）、用完或打不開差異畫面時都刪掉；原檔是連結時不顯示連結另一端的內容、是 FIFO 時不卡住；接受時重新檢查一次，失敗或路徑變了就不寫；接受前被換成連結時不寫。
  - `fileHandlersWrite.test.ts`：`read_file` 經不跟隨讀檔、直接寫經不跟隨寫檔；交給差異預覽的重新檢查是真的重新解析（之後被換成懸空連結就拒絕）。
  - `discoveryFile.test.ts`：純函式（上層的 sticky、擁有者、別人能寫，資料夾是連結、不是資料夾、別人的、要收緊）全平台跑；POSIX 實檔：檔案 0600、新建資料夾 0700、自己的 0775／0777 資料夾被收緊、上層別人能寫又沒有 sticky 就不寫、資料夾是連結就不寫、舊檔是連結時不跟隨、EEXIST 再試一次、一直 EEXIST 就放棄。
- pytest `tests/unit/test_ide_discovery_security.py`：純函式；流程測試替換目前使用者與 stat 結果（跟隨與不跟隨連結看到的分開給，程式改成跟隨連結就會放行而變紅），略過連結、別人的、別人能寫的資料夾與檔案、經連結開到的檔案，進不去的資料夾略過而不是失敗，正常的檔案照常被發現；Windows 不查擁有者。`tests/unit/test_ide_discovery.py` 的資料夾與檔案改成明確的權限，在 Linux CI 上走真的檢查。
- 故意改壞 82 項各自讓指定的測試因對的原因變紅，改回後全綠：
  - 第 1 批 12 項：`dev` 加回伺服器、改成字串開頭判斷、拿掉 `referrerPolicy`、`MessageItem` 改回 `ReactMarkdown`、`secure` 少三個欄位或只寫三個欄位、拿掉樣式規則、整段拿掉 `<style>`、`data` 改開頭比對、只看 `href`／`src`、比對分大小寫、擋 `foreignObject`。
  - 第 2 批 13 項：框拿掉 CSP、框加 `allow-same-origin`、框裡或頁面不檢查來源、不比對請求編號、執行檔不轉義、每張圖改用載入的時限、載入改用每張圖的時限、框裡不併預設保護清單、載入失敗不重建、`secure` 拿掉樣式欄位、`ArtifactView` 加回 `mermaid`、`ArtifactView` 不消毒。
  - 第 3 批 6 項：逾時不移除框、逾時移除但不重建、共用 CSP 的圖片放行 `https:`、共用 CSP 多一條指令、共用 sandbox 加 `allow-same-origin`、共用 CSP 開頭多一條放行 `https:` 的重複圖片指令。
  - 第 4 批 10 項（PR A2）：不分線、被取代的請求拿不到結果、移除框不結束其他等待、拿到已移除的框不換新框、失敗後不清線、換框沒有上限、框被移除時不重送、結束等待時不拿掉監聽器、失敗時只通知最後一個呼叫者、載入失敗也重試。
  - 第 5 批 41 項（PR B；ToukaIDE 項目在 Windows 與 Linux 容器各跑一次，只有一個平台有的分支只在那個平台跑）：找上層改回跟隨連結、Windows 讀寫前不查連結、POSIX 開檔拿掉 `O_NOFOLLOW` 或 `O_NONBLOCK`、開好後不查一般檔案、Linux 不核對實際位置、讀不到 `/proc` 時不另外拒絕或丟原本的錯誤、開檔時就清空、接受時不重新檢查、不刪暫存資料夾（用完時、打不開差異畫面時）、暫存資料夾別人能寫、差異預覽讀原檔或 `read_file` 跟隨連結、直接寫不經不跟隨寫檔、重新檢查只回原路徑；上層暫存資料夾不查擁有者、不查 sticky 與別人能寫、不認 sticky、不查是不是資料夾、寫檔時不查上層；兩層資料夾不查擁有者、不收緊（判斷與實際收緊各一）、不查連結、開資料夾跟隨連結、新建不是 0700；檔案不是 0600、EEXIST 不再試；後端不查擁有者、別人能寫、種類、`gemini` 那層、檔案、資料夾，開檔或查資料夾跟隨連結，預設資料夾不先解析連結，先用會丟例外的 `exists()`、進不去的資料夾讓搜尋失敗。
