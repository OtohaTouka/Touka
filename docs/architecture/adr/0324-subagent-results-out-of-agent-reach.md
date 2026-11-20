# ADR-0324：子代理結果移出代理人寫得到的地方、看板擋檔案工具、SNS 驗證標記、遮罩缺口

- 狀態：✅ 已實作（PR 1：子代理結果、看板擋檔案工具、SNS 驗證標記，2026-11-20；PR 2：遮罩缺口與多段內容的假標記，2026-11-20）〔[ADR-0325](0325-mission-board-system-folder.md) 修訂：看板搬到 `data/missions`，決定 3 的「終端還改得到看板」已解決〕
- 日期：2026-11-19
- 決策者：維護者（一頁計畫第 2.1 版，總覽板第 92 項回「甲：照 2.1 版做」；保留 7 天、不掛刪除與改名、看板只先擋檔案工具、SNS 不改成拒收，是計畫裡寫明的「我自己定的」）
- 相關：[ADR-0138](0138-parallel-fanout-and-resumable-delivery.md)（耐久結果）、[ADR-0051](0051-dispatch-callback-channel.md)（等結果的憑證）、[ADR-0293](0293-agent-folders-no-follow.md)（代理人資料夾）、[ADR-0255](0255-personal-drive-catalog.md)（個人空間目錄與 `.touka/` 拒寫）、[ADR-0137](0137-adversarial-review-subsystem.md)（互審關卡）、[ADR-0287](0287-hold-heartbeats-during-destructive-ops.md)（刪除時的子代理）、[ADR-0276](0276-heartbeat-session-scope.md)（SNS 外人）、[ADR-0319](0319-tool-records-carried-into-next-turn.md)（遮罩缺口）
- 修訂：ADR-0319（PR 2：決定 4 的遮蔽差異與已知限制、決定 7 的多段內容）、ADR-0138（耐久結果的位置）、ADR-0051（耐久讀取的位置）、ADR-0287（已知限制：刪除後子代理才交結果會重建個人空間資料夾，已解決）、ADR-0255（`missions/` 也拒寫）
- 影響範圍：PR 1：`ToukaProxy/src/services/subagent_results.py`、`main.py`（啟動時清一次）、`services/agent_reset/primitives.py`（保留清單）、`services/gdpr_export_service.py`（`EXPORT_EXCLUDED`）、`tools/vault_tools.py`、`adapters/discord_bot.py`、`adapters/telegram_bot.py`、`models/sns.py`、`services/resource_manager.py`、`ToukaMemory/src/memory_server/services/personal_catalog.py`、`services/mission_manager.py`（`MISSIONS_DIR_NAME`）、`protocols.py`；PR 2：`ToukaProxy/src/utils/secrets.py`、`utils/tool_records.py`、`utils/tool_step_collector.py`、前端 `ToukaChat/src/lib/chat/toolRecords.ts`、黃金案例檔 `ToukaChat/src/lib/chat/__tests__/toolRecords.golden.json`

## 背景

背景子代理做完時，結果交給記憶體的等候槽，同時另存一份耐久檔（ADR-0138），讓 Touka 重啟後還拿得回結果。這份檔以前放在**主代理自己的個人空間** `dispatch_results/<憑證>.json`，`mission_tools(action="await_dispatch_result")` 先讀這份檔，讀到就當成子代理交回的結果。

憑證本來就交給主代理，檔名就是憑證；個人空間是檔案動詞的 `personal` 範圍，範例權限把 `write`、`edit`、`insert`、`undo` 列為 `safe`，一般模式下不用人核准。所以主代理被工具結果裡的注入誘導時，可以先寫一份假結果，之後把它當成子代理查證過的結論回報，假檔還能帶 `system_notes`，顯示成「System note: …」，看起來像系統寫的；真的結果之後到了也沒人讀。

查現況時另外發現：任務看板 `missions/current_mission.json` 也在個人空間，直接改檔就能繞過看板規則（禁令 30、31），把 `review_status` 寫成 APPROVED 跳過互審，或改掉 `verification_cmd` 與它的鎖。

## 決定（PR 1）

### 1. 子代理結果存到系統暫存

- 位置：`resource_manager.get_temp_path("dispatch_results")`，也就是 data 底下的 `temp/dispatch_results`，不在任何代理人資料夾裡。前例是看板的鎖檔 `temp/locks/missions`。
- 碰不到的理由：data 根整個是受保護路徑；代理人的終端只拿到自己的個人空間、sandbox state 裡自己的 home 與 tmp、技能資料夾與公開設定檔；執行器容器只掛 `personal_drive` 與 `managed`；一般身分的檔案動詞只認幾個範圍（例外見已知限制）。`data/temp` 沒有人在清，備份也不收。
- 檔名：`sha256(代理人編號.casefold() + "\0" + 憑證)` 的十六進位。磁碟上看不到憑證；別的代理人拿同一個憑證讀不到；編號先 casefold，Windows 上不分大小寫的舊寫法照樣找得到。路徑穿越在構造上不可能。
- 寫入照舊原子寫入（`write_json` → `write_file_text`），失敗只記 log、不中斷交付。
- `ResourceManager.zone_read_json`／`zone_write_json`（與 `IResourceProxy` 的同名方法）只有這裡在用，搬走後沒有呼叫端，照禁令 46 刪掉；它們的測試一起拿掉，權限自癒「交給內層」的測試改用 `write_json` → `write_file_text`。
- 舊位置不再讀也不再寫，不搬（舊檔在代理人寫得到的地方，搬過來等於重新信任）。升級那次重啟前交出、還沒被讀的結果拿不到，之後等候回「憑證不明」。個人空間裡舊的 `dispatch_results/` 留著不刪，目錄快取照舊略過。

### 2. 保留 7 天

- `RESULT_RETENTION_SECONDS` 是 7 天：站內憑證在記憶體最多約 40 分鐘（子代理 10 分鐘加 30 分鐘寬限），耐久檔只為重啟後拿回結果，7 天涵蓋隔夜與週末。
- `prune_expired_results()` 刪掉資料夾頂層修改時間超過期限的檔（包含原子寫入中斷留下的暫存檔；子資料夾不動）；資料夾不存在時什麼都不做，不建。觸發點：Touka 啟動時（lifespan 只跑一次，不做成定時迴圈；整段接住例外、失敗只記 log）、每次存、每次讀。
- 生命週期不另外掛：
  - 刪除靈魂、刪除或改名自訂模型時不清：要知道憑證才算得出檔名，最多 7 天後清掉。順帶解決 ADR-0287 的已知限制（刪除之後子代理才交結果，以前會把個人空間資料夾重新建出來）。
  - 重置時不清，列進 `RETAINED_STORES`：重置刻意保留舊對話庫，裡面若還留著憑證，7 天內代理人仍讀得回自己舊的子代理結果。
  - GDPR 匯出不含，列進 `EXPORT_EXCLUDED`（manifest 的 `excluded`）。

### 3. 看板先擋檔案工具

- 檔案動詞的寫入類動作（`write`、`append`、`str_replace`、`delete`；`edit`、`insert`、`undo` 都走這幾個）碰個人空間**根目錄**的 `missions/` 一律拒絕，不分大小寫，比照 `.touka/`。例外 `SystemFolderDeniedError`，Proxy 橋接層回結構化 `permission_denied`，建議改用 mission_tools。讀照常可讀；系統經 `MissionManager` 寫看板不經檔案動詞，不受影響。
- 資料夾名稱共用 `MISSIONS_DIR_NAME`（`mission_manager`），`personal_catalog.SYSTEM_ROOT_DIRS` 與重置都用它；鎖檔資料夾的 `missions` 意思不同，不併。
- 只擋這條是因為檔案工具不用核准、最安靜。終端（YOLO 加隔離時不用核准）與能寫個人空間的第三方 MCP 伺服器仍寫得到，要等看板搬到系統資料夾（另一個任務）。〔已解決：[ADR-0325](0325-mission-board-system-folder.md) 把看板搬到 `data/missions`；這裡的拒寫留著，把 AI 導回 mission_tools〕

### 4. SNS 驗證標記

- Discord（gateway）與 Telegram（long polling）都是用 bot token 建立、平台驗證過的連線，不是 webhook，建事件時寫 `is_verified=True`，不再每則訊息記一條「未驗證」警告。
- `is_verified` 的意思改成「訊息來源已由平台驗證：webhook 驗過簽，或來自用 bot token 建立的連線」。不代表發訊人可信，外人能做什麼照舊由 ADR-0276 管。
- 測試要求產品程式裡每個 `SNSIncomingEvent(...)` 字面寫出 `is_verified=`（展開的參數不算），不准用 `model_validate`／`model_construct` 繞過。之後加 webhook 型 adapter 時驗過簽才可寫 True。
- 不改成「沒驗證就拒收」：現在沒有 webhook 型 adapter，改了擋不到東西。

## 決定（PR 2）

`utils/secrets.py` 的樣式有兩種用途：組「做過的事」紀錄時遮參數與結果開頭（後端給心跳，前端 `lib/chat/toolRecords.ts` 複製一份給網頁），以及 `redact_secrets_from_content` 給 MCP 資源與提示送給 AI 前的遮蔽、備份錯誤輸出、稽核。鍵名那組另外給設定頁、MCP 環境變數與設定快照。

### 5. 樣式只認英數，空白寫成明確字元集

- 鍵名與內容樣式一律加 `re.ASCII`：`\b` 與不分大小寫只認英數，和前端（JS 不帶 `u` 旗標）一致。以前 Python 把中文當字母，`金鑰sk-…`、`權杖eyJ…結束` 這種前後直接接中文的金鑰後端不遮，MCP 遮蔽也漏。
- 副作用：`ſecret`、`toKen`（克耳文符號）這類鍵名後端不再當成金鑰，和前端一致；環境變數名不會用這種字。`désk-…` 這種罕見寫法會被遮，接受。另有兩種極端寫法改成不遮或遮短：`Bearer` 權杖裡含 `ı`、`İ`、`ſ`、克耳文符號（以前 Python 把它們當英文字母）；`sk-`、JWT 剛好最小長度、以 `-` 結尾又直接接中文。真的權杖不會有這些字（審查員排除這四個字後隨機比對 30 萬筆，新樣式少遮 0 筆）。
- 空白不用 `\s`／`\S`：Python 加 `re.ASCII` 後 `\s` 只認英文空白，JS 的 `\s` 認全形與不換行空白；MCP 那條路拿的是原文，`Bearer` 後接全形空白的權杖會漏。兩邊寫成同一個字元集 `_WS`（兩種 `\s` 的聯集）。測試照規格逐字核對：Python 的 `\s` 加上 ECMAScript 的空白（Unicode Zs，加 `\t\n\v\f\r`、U+FEFF、U+2028、U+2029），正好等於 `_WS`。
- pytest 核對前端樣式時要求後端帶 `re.ASCII`、樣式本體不准出現 `\s`、`\S`。前端不用 lookbehind（編譯目標 ES2017）。

### 6. 共用的樣式與鍵名多認幾種

- 內容樣式加：`Authorization:` 後接 `Basic` 或 `Bearer` 的整段標頭（任何長度）、Stripe `sk_live_`／`rk_live_`、GitHub `github_pat_`、Slack `xox[abposr]-`、Google `AIza` 後接 35 字。都是很特定的開頭；MCP 文件裡的「Authorization: Bearer <token>」範例也會被遮，接受。
- `Authorization:` 標頭的值照 RFC 7235 的 token68 字元集（`[A-Za-z0-9._~+/-]+=*`），遇到別的字就停，不吃後面的中文、標點與 JSON 結構；前面不要 `\b`，`HTTP_AUTHORIZATION:` 這種接在底線後面的也遮。
- 鍵名加 `API[-_]?KEY`（`apiKey`、`api-key`、`x-api-key`），設定頁裡這類名字本來就是金鑰。
- 紀錄、MCP 遮蔽、備份、稽核、設定頁一起多遮。

### 7. 只在紀錄裡另外遮的

- 鍵名 `Authorization`（`_RECORD_EXTRA_KEY_RE`）：不放進共用名單，否則設定頁會把 `OAUTH_AUTHORIZATION_URL` 這類不是金鑰的設定也遮掉。參數鍵名與巢狀物件的鍵名都套用（`is_record_secret_key`、`mask_record_secret_keys`）。
- 字串裡 `"鍵名": "值"` 的寫法，鍵名要遮時值換成遮罩（`mask_record_key_values`）：序列化後鍵與值中間夾引號，內容樣式比對不到。結果開頭的 `"password": "…"` 也遮。跳脫後面接的字寫成 `[^\n]`，不用「.」（JS 的「.」另外不認 `\r`、U+2028、U+2029，文字一樣意思會不同）。
- 後端收集工具步驟時，太長的參數要截成 JSON 字串存檔，截之前也用紀錄專用的鍵名遮（`tool_step_collector`）：存成字串之後，物件值的 `Authorization` 紀錄就遮不到了。
- 前端複製同樣兩條樣式，pytest 逐字核對。黃金案例檔多一組 `secrets_masked_more`（緊貼中文、全形空白、新樣式、怪字母鍵名、數字值的巢狀金鑰）；原本 `secrets_masked` 裡巢狀 `Authorization` 的值改成遮罩。

### 8. 多段內容裡的假標記也換

- 後端換假「〔工具紀錄」時，內容是多段（list）的逐段換文字部分；前端存成 JSON 字串的多模態內容先還原成多段再換（跳脫過的換行、tab 在原字串裡比對不到），換完照原形式放回（緊湊分隔、`ensure_ascii=False`，和前端 `JSON.stringify` 一致）。前端自己送出的本來就先換再序列化。
- 沒換到東西時原字串一字不差地回傳，提示快取不失效。只改送出的副本。
- 字串裡沒有「〔」也沒有它的跳脫寫法 `\u3014` 時直接原樣回傳，不解析（多模態 JSON 裡的 base64 圖片很大）。
- 摘要器在換完標記之後才攤平內容，list 裡的假標記也不再進摘要器的提示。

## 沒選的做法

- 結果檔加簽章、留在個人空間：要一把跨重啟的金鑰（拿登入金鑰另作他用，或多一個要持久化的密鑰），之後看板要用同一做法時 ToukaDispatch 也得拿到金鑰；搬位置比較單純。
- 刪除與改名時清結果資料夾：要改三條流程；檔名要憑證才算得出來，最多 7 天就清掉，代價小。
- 單獨的 `Basic …` 樣式：「Basic configuration」這種一般英文會被遮。
- `Authorization` 放進共用鍵名：設定頁會遮掉不是金鑰的設定。
- 這次就把看板搬家：牽動 Proxy、Memory、ToukaDispatch 三個服務、督導跨身分讀、重置、刪除、改名，以及現有看板只搬一次，另開任務。

## 已知限制

- 隔離層級是 none 時（例如原生 Windows 沒有 Landlock），終端指令要人在「未隔離」卡上核准才會跑，跑起來寫得到 data；第三方 MCP 在隔離穩定不可用時會直接未隔離啟動（禁令 66 ⑩），也寫得到。
- 刪掉靈魂後，它的子代理結果最多在磁碟上多留 7 天。
- 被注入的子代理仍能在自己的摘要裡寫一行「System note:」，顯示在 `Result:` 那段裡。
- 外部派工的憑證會出現在 initialize 的回覆裡，可以用本機回呼送結果；隔離的終端封鎖 Touka 自己的埠擋得住，未隔離的 MCP 擋不住。外部派工目前凍結。
- ~~看板仍能經終端與能寫個人空間的 MCP 伺服器直接改（見決定 3）。~~ 已解決（[ADR-0325](0325-mission-board-system-folder.md)）。
- 不受專案限制的身分（管理員、Debugger、只有管理員擁有的靈魂）若把專案登記在涵蓋 data 或個人空間的位置，能用專案範圍的檔案動詞寫結果資料夾與看板（檔名用的編號與憑證它都知道）；和 ADR-0292 決定 12 的界線相同，由管理員自己負責不這樣登記。
- 沒有 `Authorization:` 標頭的短 Bearer（少於 12 字）不遮：「Bearer short」這種一般英文分不出來。`Authorization: token …`、`Digest` 與沒寫 scheme 的值也不遮；不合規格的權杖在第一個規格外的字之後會露出。
- 鍵名夾看不見的字（`API` 加零寬字元加 `_KEY`）兩邊都不遮：鍵名判斷在換掉看不見的字之前；鍵名是模型自己寫的，值在那一輪已經完整給它看過。
- 不是字串的參數寫成 JSON 時數字寫法前後端不同（`30.0` 對 `30`、整數樣式鍵的順序），只差紀錄文字；遮罩在序列化之前依鍵名做，兩邊遮的結果相同（黃金案例鎖住）。

## 驗證（PR 1）

- 新測試：`tests/unit/test_subagent_result_forgery_security.py`（舊位置的假檔不被採用、憑證沒發過回憑證不明、真結果照樣拿到、新位置不在代理人資料夾、檔名不含憑證、別的代理人讀不到、編號大小寫、7 天清理的三個觸發點、只清頂層與不建資料夾、啟動時清一次且失敗不影響啟動、重置與匯出清單）、`tests/unit/test_mission_board_write_deny_security.py`（四種寫入動作與四個檔案動詞、絕對路徑、相似名稱照常可寫、讀照常、系統照常存看板）、`test_c4_c5_c6_c7_security.py` C7 段的靜態測試。
- `tests/zone_mutations.json`：`r14_g2` 照舊（`_SUBDIR` 那行保留）；`r14_t2` 改靶到重置的 `_mission_dir`；`z6_dispatch_results_read_follows` 拿掉（看板那條 Z6 還在）。兩條手動跑改壞執行器都會紅。
- `scripts/native_tool_contract_baseline.json` 記下 `SystemFolderDeniedError.__init__`（Guard #45）。

## 驗證（PR 2）

- 新測試：`tests/unit/test_secret_mask_cjk_security.py`（緊貼中文的金鑰、各種空白後的 Bearer、標頭的值不吃後面的文字、`HTTP_AUTHORIZATION:`、空白字元集照規格核對、新樣式各一個會遮、一般文字不遮、共用鍵名、`Authorization` 只在紀錄、怪字母鍵名兩邊都不遮、字串裡的鍵值、各層的 `Authorization`）。
- `tests/unit/test_tool_records.py`：旗標核對改成要求 `re.ASCII`、禁 `\s`；紀錄專用兩條樣式的前後端核對；多段內容、JSON 字串（含 `ensure_ascii` 的跳脫寫法）、沒換時一字不差、沒有標記開頭時不解析、摘要器。`tests/unit/test_tool_step_collector.py`：截成字串前用紀錄專用的鍵名遮。黃金案例檔 pytest 與 vitest 都讀，兩邊一字不差。
- 改壞驗證（先跑沒改壞的基準）：28 處各改壞一次，對應的測試都會紅（前端的改法另跑 vitest）。
