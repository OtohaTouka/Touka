# ADR-0324：子代理結果移出代理人寫得到的地方、看板擋檔案工具、SNS 驗證標記、遮罩缺口

- 狀態：🚧 進行中（PR 1：子代理結果、看板擋檔案工具、SNS 驗證標記；PR 2：遮罩缺口）
- 日期：2026-11-19
- 決策者：維護者（一頁計畫第 2.1 版，總覽板第 92 項回「甲：照 2.1 版做」；保留 7 天、不掛刪除與改名、看板只先擋檔案工具、SNS 不改成拒收，是計畫裡寫明的「我自己定的」）
- 相關：[ADR-0138](0138-parallel-fanout-and-resumable-delivery.md)（耐久結果）、[ADR-0051](0051-dispatch-callback-channel.md)（等結果的憑證）、[ADR-0293](0293-agent-folders-no-follow.md)（代理人資料夾）、[ADR-0255](0255-personal-drive-catalog.md)（個人空間目錄與 `.touka/` 拒寫）、[ADR-0137](0137-adversarial-review-subsystem.md)（互審關卡）、[ADR-0287](0287-hold-heartbeats-during-destructive-ops.md)（刪除時的子代理）、[ADR-0276](0276-heartbeat-session-scope.md)（SNS 外人）、[ADR-0319](0319-tool-records-carried-into-next-turn.md)（遮罩缺口）
- 修訂：ADR-0138（耐久結果的位置）、ADR-0051（耐久讀取的位置）、ADR-0287（已知限制：刪除後子代理才交結果會重建個人空間資料夾，已解決）、ADR-0255（`missions/` 也拒寫）
- 影響範圍：PR 1：`ToukaProxy/src/services/subagent_results.py`、`main.py`（啟動時清一次）、`services/agent_reset/primitives.py`（保留清單）、`services/gdpr_export_service.py`（`EXPORT_EXCLUDED`）、`tools/vault_tools.py`、`adapters/discord_bot.py`、`adapters/telegram_bot.py`、`models/sns.py`、`services/resource_manager.py`、`ToukaMemory/src/memory_server/services/personal_catalog.py`、`services/mission_manager.py`（`MISSIONS_DIR_NAME`）、`protocols.py`

## 背景

背景子代理做完時，結果交給記憶體的等候槽，同時另存一份耐久檔（ADR-0138），讓 Touka 重啟後還拿得回結果。這份檔以前放在**主代理自己的個人空間** `dispatch_results/<憑證>.json`，`mission_tools(action="await_dispatch_result")` 先讀這份檔，讀到就當成子代理交回的結果。

憑證本來就交給主代理，檔名就是憑證；個人空間是檔案動詞的 `personal` 範圍，範例權限把 `write`、`edit`、`insert`、`undo` 列為 `safe`，一般模式下不用人核准。所以主代理被工具結果裡的注入誘導時，可以先寫一份假結果，之後把它當成子代理查證過的結論回報，假檔還能帶 `system_notes`，顯示成「System note: …」，看起來像系統寫的；真的結果之後到了也沒人讀。

查現況時另外發現：任務看板 `missions/current_mission.json` 也在個人空間，直接改檔就能繞過看板規則（禁令 30、31），把 `review_status` 寫成 APPROVED 跳過互審，或改掉 `verification_cmd` 與它的鎖。

## 決定（PR 1）

### 1. 子代理結果存到系統暫存

- 位置：`resource_manager.get_temp_path("dispatch_results")`，也就是 data 底下的 `temp/dispatch_results`，不在任何代理人資料夾裡。前例是看板的鎖檔 `temp/locks/missions`。
- 碰不到的理由：data 根整個是受保護路徑；代理人的終端只拿到自己的個人空間、sandbox state 裡自己的 home 與 tmp、技能資料夾與公開設定檔；執行器容器只掛 `personal_drive` 與 `managed`；檔案動詞只認幾個範圍。`data/temp` 沒有人在清，備份也不收。
- 檔名：`sha256(代理人編號.casefold() + "\0" + 憑證)` 的十六進位。磁碟上看不到憑證；別的代理人拿同一個憑證讀不到；編號先 casefold，Windows 上不分大小寫的舊寫法照樣找得到。路徑穿越在構造上不可能。
- 寫入照舊原子寫入（`write_json` → `write_file_text`），失敗只記 log、不中斷交付。
- `ResourceManager.zone_read_json`／`zone_write_json`（與 `IResourceProxy` 的同名方法）只有這裡在用，搬走後沒有呼叫端，照禁令 46 刪掉；它們的測試一起拿掉，權限自癒「交給內層」的測試改用 `write_json` → `write_file_text`。
- 舊位置不再讀也不再寫，不搬（舊檔在代理人寫得到的地方，搬過來等於重新信任）。升級那次重啟前交出、還沒被讀的結果拿不到，之後等候回「憑證不明」。個人空間裡舊的 `dispatch_results/` 留著不刪，目錄快取照舊略過。

### 2. 保留 7 天

- `RESULT_RETENTION_SECONDS` 是 7 天：站內憑證在記憶體最多約 40 分鐘（子代理 10 分鐘加 30 分鐘寬限），耐久檔只為重啟後拿回結果，7 天涵蓋隔夜與週末。
- `prune_expired_results()` 刪掉修改時間超過期限的檔；資料夾不存在時什麼都不做，不建。觸發點：Touka 啟動時（lifespan 只跑一次，不做成定時迴圈；整段接住例外、失敗只記 log）、每次存、每次讀。
- 生命週期不另外掛：
  - 刪除靈魂、刪除或改名自訂模型時不清：要知道憑證才算得出檔名，最多 7 天後清掉。順帶解決 ADR-0287 的已知限制（刪除之後子代理才交結果，以前會把個人空間資料夾重新建出來）。
  - 重置時不清，列進 `RETAINED_STORES`：重置刻意保留舊對話庫，裡面若還留著憑證，7 天內代理人仍讀得回自己舊的子代理結果。
  - GDPR 匯出不含，列進 `EXPORT_EXCLUDED`（manifest 的 `excluded`）。

### 3. 看板先擋檔案工具

- 檔案動詞的寫入類動作（`write`、`append`、`str_replace`、`delete`；`edit`、`insert`、`undo` 都走這幾個）碰個人空間**根目錄**的 `missions/` 一律拒絕，不分大小寫，比照 `.touka/`。例外 `SystemFolderDeniedError`，Proxy 橋接層回結構化 `permission_denied`，建議改用 mission_tools。讀照常可讀；系統經 `MissionManager` 寫看板不經檔案動詞，不受影響。
- 資料夾名稱共用 `MISSIONS_DIR_NAME`（`mission_manager`），`personal_catalog.SYSTEM_ROOT_DIRS` 與重置都用它；鎖檔資料夾的 `missions` 意思不同，不併。
- 只擋這條是因為檔案工具不用核准、最安靜。終端（YOLO 加隔離時不用核准）與能寫個人空間的第三方 MCP 伺服器仍寫得到，要等看板搬到系統資料夾（另一個任務）。

### 4. SNS 驗證標記

- Discord（gateway）與 Telegram（long polling）都是用 bot token 建立、平台驗證過的連線，不是 webhook，建事件時寫 `is_verified=True`，不再每則訊息記一條「未驗證」警告。
- `is_verified` 的意思改成「訊息來源已由平台驗證：webhook 驗過簽，或來自用 bot token 建立的連線」。不代表發訊人可信，外人能做什麼照舊由 ADR-0276 管。
- 測試要求產品程式裡每個 `SNSIncomingEvent(...)` 字面寫出 `is_verified=`（展開的參數不算），不准用 `model_validate`／`model_construct` 繞過。之後加 webhook 型 adapter 時驗過簽才可寫 True。
- 不改成「沒驗證就拒收」：現在沒有 webhook 型 adapter，改了擋不到東西。

## 沒選的做法

- 結果檔加簽章、留在個人空間：要一把跨重啟的金鑰（拿登入金鑰另作他用，或多一個要持久化的密鑰），之後看板要用同一做法時 ToukaDispatch 也得拿到金鑰；搬位置比較單純。
- 刪除與改名時清結果資料夾：要改三條流程；檔名要憑證才算得出來，最多 7 天就清掉，代價小。
- 這次就把看板搬家：牽動 Proxy、Memory、ToukaDispatch 三個服務、督導跨身分讀、重置、刪除、改名，以及現有看板只搬一次，另開任務。

## 已知限制

- 隔離層級是 none 時（例如原生 Windows 沒有 Landlock），終端指令要人在「未隔離」卡上核准才會跑，跑起來寫得到 data；第三方 MCP 在隔離穩定不可用時會直接未隔離啟動（禁令 66 ⑩），也寫得到。
- 刪掉靈魂後，它的子代理結果最多在磁碟上多留 7 天。
- 被注入的子代理仍能在自己的摘要裡寫一行「System note:」，顯示在 `Result:` 那段裡。
- 外部派工的憑證會出現在 initialize 的回覆裡，可以用本機回呼送結果；隔離的終端封鎖 Touka 自己的埠擋得住，未隔離的 MCP 擋不住。外部派工目前凍結。
- 看板仍能經終端與能寫個人空間的 MCP 伺服器直接改（見決定 3）。

## 驗證（PR 1）

- 新測試：`tests/unit/test_subagent_result_forgery_security.py`（舊位置的假檔不被採用、憑證沒發過回憑證不明、真結果照樣拿到、新位置不在代理人資料夾、檔名不含憑證、別的代理人讀不到、編號大小寫、7 天清理的三個觸發點與不建資料夾、啟動時清一次且失敗不影響啟動、重置與匯出清單）、`tests/unit/test_mission_board_write_deny_security.py`（四種寫入動作與四個檔案動詞、絕對路徑、相似名稱照常可寫、讀照常、系統照常存看板）、`test_c4_c5_c6_c7_security.py` C7 段的靜態測試。
- `tests/zone_mutations.json`：`r14_g2` 照舊（`_SUBDIR` 那行保留）；`r14_t2` 改靶到重置的 `_mission_dir`；`z6_dispatch_results_read_follows` 拿掉（看板那條 Z6 還在）。兩條手動跑改壞執行器都會紅。
- `scripts/native_tool_contract_baseline.json` 記下 `SystemFolderDeniedError.__init__`（Guard #45）。
