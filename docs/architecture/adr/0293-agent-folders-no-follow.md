# ADR-0293：代理人資料夾的讀寫不跟隨連結（ZonePath；PR 1：個人空間）

- 狀態：✅ 已實作（PR 1：`PERSONAL_DRIVE`、`CUSTOM_DRIVE`；`MCP_SERVERS`、`DOCGEN_OUTPUT`、`SANDBOX_STATE` 在 PR 2）。ADR-0292 合併後的後續；一頁計畫與技術設計由維護者核准，設計與交件各經 4 位對抗式複審。
- 日期：2026-10-30
- 決策者：維護者
- 相關：ADR-0004（路徑 SSOT）、ADR-0013（時間）、ADR-0115（vault-jailed MCP；本 ADR 起這類伺服器的工具呼叫一律拒絕，D1 的注入不再使用）、ADR-0214（測試沙盒）、ADR-0255（個人空間目錄）、ADR-0270（重置）、ADR-0272、0275（隔離層）、ADR-0292（hook；決定 12 的不跟隨連結）。
- 影響範圍：ToukaMemory 新增 `utils/zone_paths.py`，擴充 `utils/no_follow.py`；修改 `protocols.py`（`IResourceProxy`）、`tools/core_file_ops.py`、`tools/diag.py`、`tools/__init__.py`、`services/personal_catalog.py`、`services/mission_manager.py`、`services/manager.py`，刪 `tools/personal_drive.py`。ToukaProxy 修改 `services/resource_manager.py`、`tools/terminal.py`、`tools/mission_tools.py`、`tools/editor.py`（刪 `text_editor`）、`tools/grok_imagine_server.py`、`services/upload_service.py`、`services/gdpr_export_service.py`、`services/agent_reset/`、`services/memory_scrub/orchestrator.py`、`services/review/`、`services/subagent_results.py`、`services/process_registry.py`、`services/tool_executor.py`、`services/mcp_vault_jail.py`、`services/isolation/`（`profiles.py`、`mcp_profiles.py`、`repair.py`、`status.py`、`home_wipe.py`）、`utils/mcp_origin.py`、`utils/permission_repair.py`、`utils/soul_io.py`、`utils/custom_io.py`、`routers/souls.py`、`routers/custom_models.py`、`routers/models.py`、`services/models/manager.py`、`routers/ui_sessions.py`、`routers/mcp.py`、`protocols/registry.py`（說明）、`routers/observability.py`、`main.py`、`scripts/manage_backup.py`，新增 `services/zone_file_response.py`；`errors.json.example`（1421 `AGENT_DRIVE_LINKED`）；ToukaDispatch `server.py`；ToukaChat `uploadClassify.ts`、`error_codes.ts`、翻譯；`scripts/lint_ssot.py`、`scripts/native_tool_contract_baseline.json`（刪 `text_editor`）；CI `native-tool-contracts.yml`（全量 pytest 開 `TOUKA_Z9_FULL_RUN`）。

## 背景

- 代理人的終端在 Landlock 裡可以寫自己的個人空間（`personal_drive/<id>`、`custom/<id>/drive`），在裡面建符號連結或 junction 都做得到；Auto 與 YOLO 下不一定經人核准。
- Touka 自己（檔案動詞、任務板、重置、刪除、備份、GDPR 匯出、回檔等）以 Touka 的權限讀寫這些資料夾。只要有一處跟隨了代理人放的連結，代理人就能讓 Touka 替它讀寫 Touka 自己的設定（例如 `config/hooks.json`、`users.json`）。
- ADR-0292 的決定 12 只讓代理人的檔案動詞不跟隨連結；Touka 其他的讀寫留到這次，所以 ADR-0292 請管理員先不要啟用 command hook。
- 呼叫端各自拼路徑、各自檢查時，漏一處就整個失效，靠審查找不完。這次改成「代理人資料夾裡的路徑只有一種型別、只有一組方法能碰」，再用靜態檢查與測試期間的絆線找出漏網的地方。

## 決定

1. **區與錨點**（`memory_server/utils/zone_paths.py`，只用標準函式庫）：
   - 「區」是代理人寫得到的資料夾（錨點）底下。錨點 5 個：`PERSONAL_DRIVE`（`personal_drive`，區是 `<id>` 與底下）、`CUSTOM_DRIVE`（`custom`，區是 `<id>/drive` 與底下；`custom/<id>` 本身與裡面的設定檔不是區）、`MCP_SERVERS`（`mcp_servers/managed`）、`DOCGEN_OUTPUT`（`temp/docgen_output`）、`SANDBOX_STATE`（sandbox state 根）。錨點本身不算區內。
   - PR 1 只對前兩個執法（`ENFORCED_ANCHORS`）；隔離層與 Z10 已用全部 5 個（`ALL_ANCHORS`）。
   - 區內判斷只有一份：`zone_of`（回錨點與段落）、`contains_zone`（是錨點、錨點的上層或 `custom/<id>`）、`anchor_at`（恰好是錨點）。三者都比對字面、解析後與檔案身分（`st_dev`、`st_ino`），所以別名路徑也認得；錨點集合由呼叫端明確傳入。權限自癒另用 `zone_of(..., follow=False)` 只看字面，自己比解析前後兩次。
   - 「目前的 data 根」只有一個來源：RM 在模組載入時登記給 `current_roots()` 的提供函式（`settings.get_app_data_dir()` 與 state 根）。
2. **`ZonePath` 型別**：區裡的路徑只用 `ZonePath(anchor, parts)` 表示，只由 RM 的產生函式交出（`agent_drive`、`soul_drive`、`custom_drive`、`outputs_dir`、`uploads_dir`、`anchor_root`），不建資料夾、不解析。
   - 每一種「當成路徑用」的寫法（`os.fspath`、`/`、`+`、`str`、`format`、`bytes`、`Path` 的方法名）都先通知觀察者再丟錯，所以 `Path(z)`、`open(z)`、`os.path.join(z, …)`、`f"{z}"` 都會失敗。
   - `display()` 與 `repr` 是 `zone://personal_drive/Alice/x.txt`，拼回路徑一定失敗。
   - 不可信的名稱：程式給的單一名稱用 `child()`；模型或使用者給的相對路徑用 `join_rel()`，`..` 最多回到接收的那個 `ZonePath`（`../Bob/x` 不可能）；`custom` 的路徑不能高於 `<id>/drive`。
   - 建立、刪除、改名這些身分還在變的流程用 `soul_drive`／`custom_drive`，不用依執行期角色判斷的 `agent_drive`。
3. **RM 的區內方法**（`zone_*`，`IResourceProxy` 同步）：只收 `ZonePath`，從解析後的錨點往下逐段不跟隨連結（POSIX 以 `dir_fd` 開，Windows 開檔後比對身分）。錨點不存在時寫入方法先建（`ensure_anchor`），讀取方法回空。`zone_kind` 回 `missing`／`dir`／`file`／`link`／`other`。
4. **字串方法的拒絕**：RM 有 I/O 的字串方法先正規化路徑，`zone_of` 不是 `None` 就丟 `ZonePathRequiredError`。
   - 列、找、統計（`iter_files`、`stat_files`、遞迴的 list、glob）走訪時遇到區根剪掉、略過並計數；所以專案根在 data 根上層時照常可用，只是看不到代理人資料夾。
   - 刪、複製、打包：根含有區就拒絕，途中遇到區根也整個拒絕；本身也不跟隨連結（刪連結本身，複製與打包略過連結並計數）。
   - 有兩個路徑的方法（`rename`、`move_file`、`copy`）任一個在區裡或含有區就拒絕。
   - RM 以外的遞迴走訪（備份腳本）用 `no_follow.walk_pruned`，剪掉的區根以（錨點, 段落）回報，呼叫端再走區內方法。
5. **轉出**：要交給子行程或沙盒的路徑只能經 `zone_handoff(zp, kind)` 取得字串（解析後的錨點加字面段落），進入時登記、離開時取消。種類：`SUBPROCESS_CWD`（終端、互審驗證指令、清除沙盒 home）、`PROC_FD`（Linux 的 `/v1/outputs`：不跟隨地開好檔案，把 `/proc/self/fd/N` 交給 `FileResponse`，重開的是同一個 inode）、`WINDOWS_FILE_RESPONSE`（沒有 `/proc` 的系統依路徑重開，見界線）。登記表的函式是私有的，只有 `zone_handoff` 與 `ZoneFileResponse` 能呼叫。
6. **例外**：`ZonePathRequiredError(TypeError)`（程式錯誤，`except OSError` 接不到）、`ZoneSegmentError(ValueError)`、`ZoneLinkRefusedError(LinkRefusedError)`（區裡有連結；ADR-0292 的 `LinkRefusedError` 照舊給專案根）。區內方法丟出的所有例外，`filename` 都是 `display()`、訊息固定不帶路徑；絕對路徑只放在 `zone_abs`，只有權限自癒讀。三種例外與 `ZonePath` 的誤用會通知觀察者：正式環境記一行警告，測試期間記下來（決定 12）。
7. **破壞性操作**（重置、刪除靈魂、刪除與改名自訂模型）：
   - 任何副作用之前先查個人空間根；是連結（或 `custom/<id>` 本身是連結）就整個失敗、什麼都不刪，UI 回 `AGENT_DRIVE_LINKED`（1421，HTTP 409）。
   - 查完後停掉這個代理人的全部指令：`process_registry` 登記時帶 `agent_id`（沒有 session 的也登記），`terminate_agent` 跨 session 停；終端另外保留還沒 spawn 的與待審批的工作一起取消。停完到操作結束之間，終端與互審驗證指令啟動前查 `destructive_ops.is_held`，被持有就以 `agent_busy` 拒絕。刪除與改名自訂模型也進 `destructive_op`（別的代理人正在重置、清除或刪除時回 409 `AGENT_RESET_IN_PROGRESS`）；建立存檔失敗的回滾只標記這個 ID、不拿全系統的鎖（剛建的 ID 沒有別的操作會碰，撞上別人的重置也要清掉寫了一半的資料夾）。和靈魂同名的舊 `custom/<id>` 刪除時不停指令：這個 ID 登記的指令是靈魂的（只差大小寫的也算，它們可以改名，改名一樣不停；完全同名的不能改名，ADR-0254）。標記用的是同一個不分大小寫的 ID，所以刪除或改名期間靈魂的新指令回 `agent_busy`、心跳在下一個脈衝前停下。設定頁的 `DELETE /v1/models/{id}` 遇到自訂模型時改呼叫同一條刪除路徑，也一樣查擁有權（`ModelManager` 只刪登記與快取）；最底層的 `custom_io.delete_custom_dir` 沒有標記就拒絕。
   - 刪樹不跟隨連結、不因深度失敗；同一項被來回換的重試有上限，到上限以固定原因失敗（「可能有這個代理人留下的背景程式還在改檔案；請稍後再試，仍失敗請重啟 Touka」）。
   - 新的自訂模型 ID 要能當成區裡的一段（`is_zone_segment`），不然回 422。
   - 自訂模型改名只用 `rename_agent_container`：只做 `renameat`、不複製（跨磁碟失敗），舊名是連結、新名已存在都拒絕。刪除用 `remove_custom_model_dir`。
8. **改用區內方法的功能**：檔案動詞、個人空間目錄（`.touka`）、任務板（鎖檔搬到 `data/temp/locks/missions/<id>.lock`，代理人寫不到）、派工結果、互審蒐證、重置、刪除靈魂與自訂模型、改名、上傳（含配額走訪）、`/v1/outputs`、生圖、GDPR 匯出、診斷統計、終端的個人空間根、隔離狀態顯示、建立靈魂、儲存狀態檢查、ToukaMemory `manager`、ToukaDispatch 的看板、備份與還原（`manage_backup.py`：走訪不跟隨、寫回用 `write_stream_no_follow` 從根檢查整條路徑、成員名稱逐段檢查；其他成員與 `config/proxy/` 底下的名稱有空段、`.`、`..` 或從根、磁碟代號開始的略過並列出）。舊的字串產生函式（`get_vault_path("personal")` 一類）與 `text_editor`、`get_physical_path` 刪除。
9. **vault-jailed MCP**（docgen 一類，ADR-0115）：這類伺服器的 I/O 不經 RM，而且所有代理人共用同一個程序，交出任何根都無法保證不跟隨連結；`base_dir` 同時是讀與寫的根，交別的資料夾會讓產出落錯地方。所以設了 `vault_jail_arg` 的伺服器，工具呼叫在執行器前段（這一輪清單核對之後、政策與審批卡之前）一律以 `VaultJailedToolUnavailableError` 拒絕、不送出，給模型的是結構化 `permission_denied`（`reason: "vault_jailed_tool_unavailable"`）。上傳 PDF、Office 檔時給模型的提示不再叫它用這類工具讀檔。安全的讀寫做法另開任務。
10. **隔離層的形狀判斷**改呼叫 `zone_of`／`contains_zone`／`anchor_at`：`profiles._own_drive`（只收自己的 `PERSONAL_DRIVE/<id>` 或 `CUSTOM_DRIVE/<id>/drive`，路上有連結拒絕）、`_in_own_drive`（取代 `grantable_roots`）、`mcp_profiles` 的個人空間引用、`mcp_origin.agent_influenced`、`repair.match_whitelist`。結果和以前相同，只有一處變嚴：Windows 上 `custom/<id>/DRIVE` 以前因大小寫被判成代理人影響不到。隔離狀態顯示先看錨點有沒有掛進執行器，再看 `zone_kind`。
11. **靜態檢查**（`tests/lint/test_zone_path_guard.py`）：
    - T1：舊的產生函式不再出現；T2：回傳區內路徑的函式必須標註 `ZonePath`（資料流追蹤）；T3：測試不能替換這些函式與 data 根，換 data 根只能經 `zone_data_root`／`point_zone_roots`。
    - G1：轉出、整個錨點與錨點名稱的存取函式只出現在明列的「模組:函式」（有次數與理由）；G2：錨點資料夾名稱的字串只出現在明列的地方。
    - 產品程式不能用 `os.PathLike` 判斷路徑；`zone_paths.py` 列入 `lint_ssot.py` 的 I/O 白名單。
12. **測試期間的機制**：
    - 觀察者紀錄帶測試 id，測試結束時有紀錄就失敗，產品程式接住了也一樣；預期有拒絕的測試用 `expect_zone_refusal`／`raises_zone` 宣告。
    - Z9 絆線（`tests/support/zone_tripwire.py`，conftest 安裝）：audit hook 記下測試期間對區內路徑的直接 I/O（不經 RM 的）。
      - 路徑：事件的字串、bytes、PathLike 參數都當路徑（相對的補上工作目錄），Linux 上 fd 經 `/proc/self/fd` 還原。子行程只看執行檔與工作目錄；`open` 只看第一個參數；socket 只看 AF_UNIX 的字串位址。參數確定不是路徑的事件（屬性存取、環境變數、`import`、`compile`、`exec`）與 Windows 裝置命名空間不看。
      - 呼叫者：往上第一個不是標準函式庫、也不是 site-packages 的 frame。在 `tests/` 底下的、已登記的轉出放行；明列放行的只有不跟隨連結的原語（`no_follow`）與 Linux 的權限自癒（經 `/proc/self/fd` 改剛核對過的 inode）。正式環境在別的行程跑的模組（執行器伺服器）不看；proxy 只從它拿程式雜湊，由測試釘住。
      - `TOUKA_Z9_FULL_RUN=1`（CI 的全量 pytest）時另檢查每個放行項目都命中過，只在 Linux 用得到的只在 Linux 檢查；旗標只給最外層的 session，測試再開的 pytest 不繼承。
    - Z10（`tests/unit/test_isolation_writable_roots.py`）：每個接上 runner 的動作與 MCP 種類都實際建出 profile，每條可寫規則都在區裡或是錨點本身，`SandboxProfile`／`PathRule` 的建構點釘住。
    - 改壞清單 `tests/zone_mutations.json`（執行器 `tests/zone_mutations.py`，每個 PR 的驗證步驟手動跑一次；只在 POSIX 判得出的到 Linux 容器跑）：每條故意改壞都要讓指定的測試失敗，判定只看 junitxml 失敗的 `message`（traceback 會印出測試原始碼，不算）。一般 pytest 裡另有測試釘住清單：每條的片段在目標檔裡剛好出現一次、列出的測試存在、R1–R14 每條都有。改之前的樹上，連結測試以 `LINK_FOLLOWED` 標記失敗。

## 已知的界線

- **Windows 本機**：標準函式庫沒有 `dir_fd`，檔案動詞查連結和開檔不是同一個動作。只換一次連結的會被擋下（開檔後比對檔案識別碼）；不停來回換連結、剛好開檔時是連結、檢查時不是、比對時又是，仍可能讀寫到連結另一端的檔案（含截斷後寫入、追加、原子寫入蓋過去）。建立新檔、原子寫入的暫存檔與建上層資料夾可能在另一端留下空檔或空資料夾；刪除只在刪之前查一次；只讀時開檔前不查，路上現成的連結會先開到再拒絕（內容不讀出，但指向網路位置的連結可能先連上那台主機）。遞迴刪除、建資料夾、改名、補權限、走訪同樣有時間差；`/v1/outputs` 依路徑重開檔案，檢查與開檔之間有時間差。能利用的只有本來就有 Touka 權限的程式（Windows 本機沒有沙盒程序會寫區；代理人終端不經隔離、每條指令都要人核准；未隔離的第三方 MCP 伺服器的工具依模式執行）。8.3 短檔名可以寫進自己的 `.touka`，屬於下面「代理人自己的 JSON」那一項。
- **不經 RM 的程式**：hook 程式（ADR-0292）以 Touka 的權限執行，可以讀寫任何 Touka 權限到得了的地方；像「工具執行後跑 lint」這類 hook，lint 工具若會執行檔案裡的設定或外掛，就等於以 Touka 的權限跑代理人寫的東西，管理員要選不會執行專案內容的工具。hook 的 MCP 可寫資料夾禁區只來自 `mcp_server_isolation.json` 的 `sandbox_paths`：包在 Landlock 裡的第三方與 scaffold 伺服器引用的資料夾不在禁區，IDE 與未隔離的 MCP 伺服器能寫哪裡 Touka 不知道；管理員不要把 hook 程式放在這些地方，也不要讓它們寫得到 `config/hooks/`。執行器不載入 RM（規則路徑由 kernel 逐段不跟隨地開），以後在執行器加 I/O 不受這條規則保護。dispatch 自動啟動的外部 AI 與 IDE 在主機上，照它們自己的權限。
- **代理人自己留下的常駐行程**：proxy 本機（Landlock）用 `setsid` 留下的行程不在任何登記裡，停不到，可以讓重置或刪除一直以固定原因失敗，直到重啟 proxy（資料留著、不會刪到別處）。執行器裡的由 subreaper 收（最多約 30 秒）。proxy 本機的 subreaper 另排。
- **被入侵的第三方 MCP**：經 APPDATA 對映寫得到 `personal_drive` 底下，同樣能讓重置或刪除以固定原因失敗。
- **別名上層**：`contains_zone` 只認得錨點自己的上層鏈；從另一個掛載路徑進到 data 的遞迴動作靠逐層判斷。`st_ino == 0` 的檔案系統上身分比對不可用，只剩字面與解析後兩項。錨點底下另外掛的檔案系統，走訪與刪樹不跨過去（備份、GDPR、診斷不收，刪除失敗並說明）。
- **觀察者看不到的誤用**：`json.dumps(z)`（標準函式庫只看類別名稱就丟錯）。
- **硬連結不處理**：跨 bind mount 回 EXDEV、Landlock 的 REFER 有 no-gain 限制、執行器沒有掛設定資料夾。
- **代理人自己的 JSON**：任務板、派工結果、`.touka` 在代理人可寫的空間，代理人直接改 JSON 就能偽造狀態；不跟隨連結擋不住，另排。
- **改版前的備份**可能含連結另一端的內容。
- **改名失敗的原因**：`custom/<id>` 新舊其中一個是掛載點（跨檔案系統）或 Windows 上檔案還開著時，改名回 `SYSTEM_ERROR`，不另外說明原因。
- **絆線**看不到子行程裡的 I/O 與 `stat` 類呼叫；`os.open` 的 audit 事件不帶 `dir_fd`，以 `dir_fd` 開的相對名稱也看不到（產品程式裡這樣開的只有 `no_follow` 與隔離 kernel）。
- **PR 2 之前**：`mcp_servers`、`docgen_output`、`sandbox_state` 維持今天的行為；演化的工具檔寫入仍有檢查與寫入之間的時間差。PR 2 必須在下一次 dev 進 main 前合併；兩個都合併之前，ADR-0292「暫時不要啟用 command hook」仍然有效。

## 後果

- 人類自己把 `personal_drive/<id>`、`custom/<id>`、`custom/<id>/drive` 做成連結時，那個代理人的檔案功能一律拒絕，重置、刪除與改名失敗並說明原因。錨點本身是人做的連結照常可用。
- 專案根在某個代理人的個人空間底下時，專案的檔案功能拒絕；專案根在 data 根上層時，list、find、grep 照常但看不到代理人資料夾，遞迴刪除、複製、打包拒絕；專案裡指向代理人資料夾的檔案連結，打包與複製略過。
- docgen 一類的工具在 PR 1 不能用（任何平台）；今天經隔離啟動時本來就讀寫不到代理人資料夾，主要影響 Windows 本機經人同意直接啟動的。
- 重置、刪除靈魂、刪除與改名自訂模型會先停掉那個代理人的全部指令（設定頁模型分頁的刪除也一樣）；和靈魂同名的舊資料夾不停，但期間靈魂的新指令與心跳一樣被擋。別的代理人正在重置、清除或刪除時，刪除與改名自訂模型回 409（`AGENT_RESET_IN_PROGRESS` 的訊息改成「另一個重置、記憶清除，或刪除、改名代理人的操作正在進行」）。設定頁模型分頁刪除自訂模型時查擁有權，不是擁有者的一般使用者回 403（以前刪得掉）。
- 還原時 `data/` 與 `config/proxy/` 底下名稱有空段、`.`、`..` 或從根開始的成員略過並列出（以前雙斜線開頭的會寫到 data 或設定資料夾以外）。
- 不能當成一段的名稱：兩個平台都是含 `/`、`\`、NUL 與 `.`、`..`；Windows 另外是含 `:`、保留裝置名、結尾的點或半形空白（系統會去掉，變成別的名稱的別名）。開頭的空白、全形空白這類名稱照常可用。Linux 上檔名含 `\` 的既有檔案，檔案動詞與 `/v1/outputs` 都碰不到（`/v1/outputs` 回 403）；走訪時略過並記下相對路徑（備份與還原逐筆印出，GDPR 與重置只記數量）；不合格的資料夾整個不進入、只記資料夾本身。上傳的檔名是 Windows 保留名時加前綴 `_`。
- 個人空間裡另外掛的資料夾（例如自己掛的 NAS），備份、GDPR、診斷統計不收，刪除時失敗並說明。
- 每次字串方法多一次區內判斷（解析每個錨點一次、每段一次 `stat`）。5,000 個檔案的專案 grep 量起來和改之前相同（1.87 秒對 1.88 秒）；測試裡的絆線另外讓同一個 grep 變成約 4 秒，只在測試。
- 不改 `openapi.json`。撤回只能 revert。

## 替代方案

- **每個呼叫點各自檢查連結**：今天的做法，漏一處就失效，也無法用靜態檢查證明沒有漏。不採用。
- **只在 Linux 用 `openat2(RESOLVE_NO_SYMLINKS)`**：Windows 沒有對應，Python 也沒有包裝；仍要先找到所有呼叫點。不採用。
- **讓 docgen 讀一個空資料夾或每個代理人一個程序**：前者讓產出落進共用資料夾；後者要重做伺服器的生命週期與權限邊界審查。另開任務。

## 驗證

- `tests/unit/test_zone_paths.py`、`test_resource_manager_zone.py`、`test_resource_manager_string_zone.py`、`test_no_follow_zone.py`、`test_zone_file_response.py`：型別與誤用、區內判斷、區內方法、字串方法的拒絕與剪枝、刪樹與權限自癒（深度、來回被換、Windows 唯讀檔）。
- 各功能的連結測試（`zone_data_root` 底下建連結指到假的 `config/hooks.json`、`config/hooks/`、`users.json` 與另一個代理人的資料夾，斷言另一端沒有被讀寫）；破壞性操作的前置檢查與停指令；備份與還原的往返；GDPR 的「收集後才換」。
- `tests/unit/test_isolation_zone_shapes.py`：隔離層的形狀判斷和改之前相同（大小寫那列除外）。
- `tests/lint/test_zone_path_guard.py`（T1–T3、G1、G2）、`tests/unit/test_zone_records.py`（觀察者紀錄）、`tests/unit/test_zone_tripwire.py`（Z9）、`tests/unit/test_isolation_writable_roots.py`（Z10）、`tests/unit/test_zone_mutations_manifest.py`（改壞清單的釘住）。
- `tests/unit/test_mcp_vault_jail.py`：設了 `vault_jail_arg` 的伺服器一律拒絕、不交出任何根。
- 連結測試在改之前的 `50ff01b7` 上執行一次，失敗訊息含 `LINK_FOLLOWED`（刪除與改名類的測試靠 `CHANGED_ANYWAY`）。
