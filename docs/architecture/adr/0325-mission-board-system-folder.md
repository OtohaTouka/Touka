# ADR-0325：任務看板搬到系統資料夾（`data/missions`）

- 狀態：✅ 已實作（2026-11-20）
- 日期：2026-11-20
- 決策者：維護者（一頁計畫第 3.1 版，總覽板第 94 項回「甲：照 3.1 版做」；舊看板要搬、搬完刪舊檔、搬不了不再試、一個 PR，是計畫裡寫明的「我自己定的」）
- 相關：[ADR-0324](0324-subagent-results-out-of-agent-reach.md)（看板先擋檔案工具）、[ADR-0293](0293-agent-folders-no-follow.md)（代理人資料夾不跟隨連結）、[ADR-0137](0137-adversarial-review-subsystem.md)（互審的落盤不變式）、[ADR-0044](0044-agent-dispatch-system.md)（督導跨身分讀 DispatchAgent 看板）、[ADR-0049](0049-cross-platform-mission-filelock.md)（看板的鎖）、[ADR-0113](0113-per-agent-data-reset.md)（重置）、[ADR-0245](0245-model-layer-stage-2a.md)（和靈魂同名的舊自訂模型）、[ADR-0250](0250-touka-leftovers.md)（只差大小寫的編號）、[ADR-0214](0214-standalone-smoke-data-and-metered-api-boundaries.md)（DebugAgent 範圍）
- 修訂 ADR-0324（決定 3 的已知限制「終端還改得到看板」已解決）、修訂 ADR-0255（`missions/` 只剩升級前的舊資料夾）、修訂 ADR-0293（決定 8 的任務板不再用區內方法、鎖檔改名）、修訂 ADR-0049（看板與鎖檔的位置）、修訂 ADR-0113（重置涵蓋的看板位置）、修訂 ADR-0044（DispatchAgent 看板的位置）、修訂 ADR-0257（取路徑不再建 `missions/`）
- 影響範圍：ToukaMemory `services/mission_manager.py`（看板位置、鎖檔名、`delete_board`／`has_board`／`move_board`／`import_legacy_board`、搬過了的記號）、`protocols.py`（刪 `agent_drive_for`）；ToukaProxy 新 `services/mission_boards.py`（搬舊看板、「不屬於任何代理人」的判斷）、`main.py`（啟動時搬一次）、`routers/souls.py`、`routers/custom_models.py`、`services/agent_reset/primitives.py`、`services/gdpr_export_service.py`、`services/resource_manager.py`（刪 `agent_drive_for`）、`utils/soul_io.py`（`personal_drive_names` 改公開）、`scripts/manage_backup.py`

## 背景

任務看板（AI 的目標、任務清單、哪些做完、哪些審過）以前存在代理人自己的個人空間 `missions/current_mission.json`。ADR-0324 只擋了檔案動詞；終端指令（YOLO 加隔離時不用核准）與能寫個人空間的第三方 MCP 伺服器還能直接改它：把任務寫成「做完、互審通過」、改掉鎖住的驗證指令，跳過看板規則（禁令 30、31）與互審（ADR-0137）。維護者在網頁看板看到的會是假的「已完成、審過」。

看板以前在個人空間裡，刪除、改名、重置、備份、匯出都是「順便」處理到的；搬出來之後要一個一個接上。

## 決定

### 1. 位置：`data/missions/<sha256(逐字編號)>.json`

- 檔名是代理人編號**逐字**（不先 casefold）UTF-8 的 sha256 十六進位。只差大小寫的兩個代理人（例如靈魂 `Alice` 與以前留下的自訂模型 `alice`）在任何磁碟上都各有各的看板；雜湊在構造上沒有大小寫、8.3 短檔名、Unicode 正規化的別名，也不必把 `missions` 加進名稱檢查。ADR-0324 的子代理結果先 casefold，這裡刻意不同：共用會讓不同代理人互改，分家（見已知限制）只是前後不一致。
- 人工找某個代理人的看板：算 `sha256(編號的 UTF-8)`，檔名就是它加 `.json`。
- 位置的唯一來源是 `mission_manager.mission_board_path`（和鎖檔資料夾用同一個 data 根），只看編號、和存取者是誰無關。督導跨身分讀不再需要查註冊身分，`ResourceManager.agent_drive_for` 與 `IResourceProxy` 的同名方法沒有呼叫端，照禁令 46 刪掉；權限照舊由 `_resolve_target_agent_id` 集中判斷（禁令 29）。
- 讀寫用 RM 的不跟隨連結檔案方法（`read_file_text_no_follow`、`write_file_text_no_follow`、`remove_file_no_follow`：原子寫入、0600、路上有連結就拒絕）。代理人在 `data/missions` 這條路上建不了連結，這是多一層保險；呼叫端對 `LinkRefusedError` 的處理照舊。
- 鎖檔也用同樣的雜湊命名：`data/temp/locks/missions/<雜湊>.lock`。以前用原始編號，Windows 上 `foo` 與 `Foo` 的鎖檔是同一個檔，只改大小寫的改名拿第二把鎖時會等到逾時。ToukaDispatch 子程序和 ToukaProxy 一起升級，不會有新舊鎖名並存。
- 為什麼碰不到：data 根整個是受保護路徑；終端沙盒只給代理人資料夾（`test_isolation_writable_roots`），特權身分的終端也一樣；有隔離的第三方 MCP 只拿到個人空間根與 docgen 輸出夾；執行器容器只掛 `personal_drive` 與 `managed`；本機小幫手不開放和 data 重疊的資料夾（ADR-0312 決定 9）。條件和 ADR-0324 相同（見已知限制）。
- `data/missions` 整個資料夾只歸 `MissionManager` 管：看板內容、刪除、搬移、「搬過了」的記號都經它（禁令 29 也要求 DispatchAgent 看板經 MissionManager）。
- ADR-0324 加的「檔案動詞不能寫個人空間的 `missions/`」與「目錄裡看不到 `missions/`」留著：沒有壞處，還會把 AI 導回 mission_tools。

### 2. 舊看板只搬一次

- 時機：Touka 啟動時在 lifespan 跑一次，放在事實庫準備好（`_prepare_memory_store`）之後、`protocol_manager.initialize_all()`（開外部 MCP 伺服器）之前，外部伺服器才改不到還沒搬的舊看板。不做成迴圈（§1）；整段接住例外、失敗只記錄（§11）。
- 記號：`data/missions/.migrated`。記號在 `missions` 裡、跟著備份走：升級後的備份還原到新的資料位置時記號一起回來，不會重新信任舊位置。記號已在就什麼都不做。
- 候選編號依序、去重，名稱不是單一段的略過：
  1. 靈魂的正式 ID（`soul_io.list_soul_ids()`）：舊版在 Windows 上可能用別的大小寫建出個人空間，用正式 ID 才找得到並搬給正式編號。
  2. 系統編號（DebugAgent、DispatchAgent）與 agents 登記表的編號：被設成 Owner 的身分看板在個人空間、不在靈魂清單上。
  3. `custom/` 第一層的資料夾名稱（不跟隨連結）。
  4. 最後才是只出現在個人空間錨點的名稱；前面的正式編號已經搬走並刪掉舊檔，非正式的寫法在這裡只會看到舊檔不存在。
- 每個編號用現在讀看板的同一個解析找舊檔（`agent_drive(身分)/missions/current_mission.json`），區內方法讀：
  - 舊檔不存在 → 略過。新位置已有看板 → 不覆蓋、舊檔不動。
  - 讀不到、是連結、不是合法的看板、不合落盤規則（做完的任務要審過、鎖住的驗證指令不能空）→ 略過、記 ERROR（只記編號、原因與例外類別，不記內容）、舊檔不動。正常經 MissionManager 寫的看板一定合規則，不合的只會是直接改檔來的。
  - 內容的 `agent_id` 和匯入的編號 casefold 後不相等 → 當成別人的看板略過；只差大小寫（以前在 Windows 上用小寫叫到時寫進去的）→ 改成匯入的編號（互審「不能自己簽自己」看的就是它）。
  - 都沒問題 → 用和平常存看板相同的序列化寫到新位置，讀回和這份一致後才刪舊檔；不一致就刪掉新檔、舊檔留著、記 ERROR。
- 寫記號：候選來源都列完整才寫（靈魂清單、登記表、`custom/`、個人空間錨點任何一個列不出來或讀不出來時不寫，下次啟動重跑，已搬的因為新位置已有看板而略過）；個別看板失敗照樣寫（不然之後每次啟動都會再搬，等於又信任舊位置）。
- 舊位置的 `missions/` 資料夾留著（可能是空的）：寫檔工具照舊拒寫、目錄照舊看不到，重置會清掉它。
- 舊看板要搬、不是丟掉重來：看板是進行中的工作，丟掉會讓 AI 做到一半的任務消失。升級前寫進去的內容本來就被系統當成真的在用，搬過去不會比現在更危險；升級後的第一次啟動之後放的才擋。

### 3. 看板跟著代理人走

- **刪除靈魂**：在清儲存之前刪看板；刪不掉回 `SOUL_VAULT_ERROR`，靈魂的儲存與註冊都不刪（隔離家目錄在這之前就清了，和以前一樣）。
- **刪除自訂模型**：刪資料夾之前刪看板；刪不掉回 False → `MODEL_DELETE_FAILED`、資料夾不動。編號**逐字**是某個靈魂的 ID（ADR-0245 之前留下的同名資料夾）時不碰看板，那是靈魂的；只差大小寫的舊模型雜湊不同，刪的是它自己的看板。建立失敗的回滾不碰看板（編號可能屬於別的身分）。
- **改名自訂模型**：在破壞性操作鎖裡、還原檢查與停指令之後：先判斷新編號屬不屬於任何代理人；屬於別的身分而且它有看板 → 任何寫入之前回 `MODEL_ALREADY_EXISTS`；之後改資料夾 → 新編號不屬於任何人時清殘留看板 → 搬看板（新編號已有看板就不覆蓋、丟例外；刪舊檔失敗時新檔也刪掉）。後兩步失敗就把資料夾與 config 的 id 改回、回 `SYSTEM_ERROR`，改不回記 AUDIT。擁有清單寫不進去的回滾也把看板搬回，搬不回記 AUDIT。只改大小寫的改名雜湊不同、鎖也不同，照常搬。
- **建立新代理人時清殘留看板**：刪除那一刻還在跑的對話或互審之後才寫看板，會在刪除後建回一份看板檔。位置一律是「所有會拒絕請求的檢查都通過之後、第一個寫入動作之前」（撞名與別名檢查都不擋完全相同、已存在的編號，所以不能只排在它們後面）：建立靈魂在建資料夾、撤銷舊對話之前；用編輯建立靈魂（新資料夾）在模型驗證與名稱前綴檢查之後、建資料夾之前，轉換既有資料夾不清；建立自訂模型在撞名檢查之後、存檔之前。清不掉就回錯誤、不建立。
- **「不屬於任何代理人」的判斷**（`mission_boards.is_unclaimed`）：逐字的編號不在靈魂清單、不是 `custom/` 第一層的資料夾名稱（含沒有 config 的個人空間，例如人類、DebugAgent）、不是系統編號、不在 agents 登記表、不是人類帳號。屬於某個身分時一律不清（那是它自己的看板）。讀不到登記表或使用者清單時回 `AGENT_ID_CHECK_FAILED`、不建立，不能當成「不屬於」去清。建立或改名自訂模型時，編號屬於別的身分而且它有看板 → `MODEL_ALREADY_EXISTS`（以前那個身分寫看板時會建出 `custom/<id>`，撞名檢查就會擋，搬家後要明著擋）；建立靈魂不這樣擋（靈魂接手同一個編號的登記表身分是同一個代理人升級）。
- **重置**：清新位置的看板，也清個人空間的舊 `missions/`；剩餘計數兩邊加總。
- **備份與還原**：`SANCTUARY_FOLDERS` 加 `missions`（還原清單跟著它）。App 內還原跑完腳本後重啟，下次啟動才看記號。
- **個人資料匯出**：每個自有代理人經 `MissionManager.get_current_mission`（有鎖、有驗證）讀，有看板就加 `agents/<編號>/mission_board.json`；manifest 的 `included` 加 `agent_mission_board`，匯出 README 的內容清單加一行。
- **DebugAgent**：看板 `data/missions/<DebugAgent 的雜湊>.json` 屬於 ADR-0214 的 DebugAgent 範圍（不在它的個人空間裡了）。

## 沒選的做法

- 雜湊前先 casefold（和子代理結果一樣）：只差大小寫的兩個代理人會共用一份看板、互相改到。
- 放在 `data/temp`：看板要備份，暫存資料夾不備份。
- 舊看板丟掉重來：會讓進行中的任務消失。
- 搬完留著舊檔：AI 用讀檔工具會讀到過期的看板。
- 分成幾個 PR：分開合進 dev 時中間會有「搬了但刪除沒跟上」的狀態。

## 已知限制

- 只有管理員擁有的靈魂，或管理員、Debugger 這類不受專案限制的身分，若把專案登記在涵蓋 data 的位置，專案範圍的檔案動詞寫得到看板（ADR-0292 決定 12 的界線，由管理員自己不這樣登記）。
- 沒有隔離的情況：隔離層級是 none 時終端要人在「未隔離」卡上核准才會跑、核准後寫得到 data；第三方 MCP 在隔離穩定不可用時未隔離啟動也寫得到（禁令 66 ⑩，和 ADR-0324 相同）。
- ToukaDispatch 依設定自動叫起的外部 AI 在 Touka 的容器裡沒有隔離地跑，寫得到整個 data。這是外部派工本來的設計。
- 還原升級前做的備份：那份備份裡的舊看板會回到個人空間，但記號在，不會被搬、不會被讀；現在的看板留著（還原不刪多出來的檔）。還原到全新的資料位置時照常會搬。
- 升級後再退回舊版，看板是空的；退版期間建的看板，再升級時也不會被搬（記號已經在）。
- 個別舊看板的暫時性錯誤（例如 Windows 防毒占用）也會讓那份看板不再被搬，舊檔留在個人空間。
- 在不分大小寫的磁碟上（Windows、macOS，包括 Windows 的 Docker Desktop 掛載），自訂模型被用不同大小寫叫到（例如外部程式送 `@alice`、模型叫 `Alice`）時，身分的編號照輸入的寫法，看到的是另一份空看板；統計與記憶照舊共用同一份。要統一得改「認出是哪個自訂模型」的那段，影響不只看板，列進路線圖候選。
- 新代理人沿用舊看板的例外（都很少見）：清完之後、建立完成之前，被刪代理人還沒跑完的那一輪剛好寫進看板；不經建立流程自動登記的編號（權限設定裡的代理人自動補進登記表）不會清；編號還被某個身分占著時不清、沿用那份看板（例如刪除靈魂時取消註冊沒做完之後重建同名靈魂，或 `custom/<編號>` 還留著沒有設定檔的資料夾時建立同名靈魂）。
- 用編輯建立靈魂時，不像「建立靈魂」那樣擋「和既有自訂模型同名」（本來就有）；這次不會因此刪掉模型的看板，但新靈魂之後用同一個編號，看得到也改得到那個模型的看板。那道撞名檢查列進候選。

## 驗證

- 新測試：`tests/unit/test_mission_board_out_of_agent_reach_security.py`（新位置不在任何代理人資料夾、檔名與鎖名是逐字編號的雜湊、不是單一段的編號拒絕、舊位置的假看板不被讀、路上有連結時讀寫都拒絕、督導照舊讀得到 DispatchAgent 看板、只差大小寫的兩個編號各有各的看板與鎖、刪除與搬移看板）、`tests/unit/test_mission_board_migration.py`（搬一次、記號、不覆蓋、壞看板與別人的看板與連結略過、`agent_id` 只差大小寫時改寫、讀回不一致、列不完整不寫記號、系統編號被設成 Owner、候選順序、Windows 上用別的大小寫建的個人空間、啟動順序與失敗不擋啟動）、`tests/unit/test_mission_board_lifecycle.py`（刪除靈魂與模型、同名與只差大小寫的舊模型、改名與各種失敗的回滾、建立時清殘留與不清的情況、別的身分有看板時 409、判斷讀不到時不建立、重置、備份與還原、匯出）。
- 改寫：`test_mission_board_links_security.py`（換成連結的個人空間碰不到看板、看板資料夾是連結時拒絕）、`test_mission_manager_cross_identity_path.py`（路徑只看編號）、`test_mission_persistence_failures.py`（改攔新的讀寫方法）；刪 `test_resource_manager_zone.py` 裡 `agent_drive_for` 的測試。
- 守衛：`tests/lint/test_zone_path_guard.py` 拿掉 `agent_drive_for`，看板位置的四個函式當成「由 data 根推導的路徑」釘進 T3 受保護名單（測試不能替換）；`tests/zone_mutations.json` 的 `z6_mission_board_read_follows` 改靶到新位置的不跟隨讀取，手動跑改壞執行器會紅。
