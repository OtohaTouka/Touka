# ADR-0293：代理人資料夾的讀寫不跟隨連結（ZonePath；PR 1：個人空間；PR 2：其他三個錨點）

- 狀態：✅ 已實作（PR 1：`PERSONAL_DRIVE`、`CUSTOM_DRIVE`；PR 2：`MCP_SERVERS`、`DOCGEN_OUTPUT`、`SANDBOX_STATE`，見最後一節）。ADR-0292 合併後的後續；一頁計畫與技術設計由維護者核准，設計與交件各經 4 位對抗式複審（PR 2 的設計經三輪複審後改由測試與改壞工具判定，交件仍經 4 位複審）。
- 日期：2026-10-30
- 決策者：維護者
- 相關：ADR-0004（路徑 SSOT）、ADR-0013（時間）、ADR-0115（vault-jailed MCP；本 ADR 起這類伺服器的工具呼叫一律拒絕，D1 的注入不再使用）、ADR-0214（測試沙盒）、ADR-0255（個人空間目錄）、ADR-0270（重置）、ADR-0272、0275（隔離層）、ADR-0292（hook；決定 12 的不跟隨連結）。
- 影響範圍：ToukaMemory 新增 `utils/zone_paths.py`，擴充 `utils/no_follow.py`；修改 `protocols.py`（`IResourceProxy`）、`tools/core_file_ops.py`、`tools/diag.py`、`tools/__init__.py`、`services/personal_catalog.py`、`services/mission_manager.py`、`services/manager.py`，刪 `tools/personal_drive.py`。ToukaProxy 修改 `services/resource_manager.py`、`tools/terminal.py`、`tools/mission_tools.py`、`tools/editor.py`（刪 `text_editor`）、`tools/grok_imagine_server.py`、`services/upload_service.py`、`services/gdpr_export_service.py`、`services/agent_reset/`、`services/memory_scrub/orchestrator.py`、`services/review/`、`services/subagent_results.py`、`services/process_registry.py`、`services/tool_executor.py`、`services/mcp_vault_jail.py`、`services/isolation/`（`profiles.py`、`mcp_profiles.py`、`repair.py`、`status.py`、`home_wipe.py`）、`utils/mcp_origin.py`、`utils/permission_repair.py`、`utils/soul_io.py`、`utils/custom_io.py`、`routers/souls.py`、`routers/custom_models.py`、`routers/models.py`、`services/models/manager.py`、`routers/ui_sessions.py`、`routers/mcp.py`、`protocols/registry.py`（說明）、`routers/observability.py`、`main.py`、`scripts/manage_backup.py`，新增 `services/zone_file_response.py`；`errors.json.example`（1421 `AGENT_DRIVE_LINKED`）；ToukaDispatch `server.py`；ToukaChat `uploadClassify.ts`、`error_codes.ts`、翻譯；`scripts/lint_ssot.py`、`scripts/native_tool_contract_baseline.json`（刪 `text_editor`）；CI `native-tool-contracts.yml`（全量 pytest 開 `TOUKA_Z9_FULL_RUN`）。
- 影響範圍（PR 2）：ToukaMemory `utils/zone_paths.py`；ToukaProxy `services/resource_manager.py`、`services/evolution_security.py`、`tools/builtin/evolution.py`、`services/isolation/`（`evolution_command.py`、`profiles.py`、`mcp_profiles.py`、`mcp_launch.py`、`status.py`、`home_wipe.py`、`executor_session.py`、`executor_server.py`）、`protocols/mcp/isolated_stdio.py`、`services/scorecard/artifact_verify.py`、`utils/isolation_policy.py`、`utils/soul_io.py`、`scripts/manage_backup.py`；三份 compose（執行器的掛載）、CI `isolation.yml`；`scripts/lint_ssot.py`（`evolution_security.py` 移出 I/O 白名單）。

## 背景

- 代理人的終端在 Landlock 裡可以寫自己的個人空間（`personal_drive/<id>`、`custom/<id>/drive`），在裡面建符號連結或 junction 都做得到；Auto 與 YOLO 下不一定經人核准。
- Touka 自己（檔案動詞、任務板、重置、刪除、備份、GDPR 匯出、回檔等）以 Touka 的權限讀寫這些資料夾。只要有一處跟隨了代理人放的連結，代理人就能讓 Touka 替它讀寫 Touka 自己的設定（例如 `config/hooks.json`、`users.json`）。
- ADR-0292 的決定 12 只讓代理人的檔案動詞不跟隨連結；Touka 其他的讀寫留到這次，所以 ADR-0292 請管理員先不要啟用 command hook（PR 2 合併後解除）。
- 呼叫端各自拼路徑、各自檢查時，漏一處就整個失效，靠審查找不完。這次改成「代理人資料夾裡的路徑只有一種型別、只有一組方法能碰」，再用靜態檢查與測試期間的絆線找出漏網的地方。

## 決定

1. **區與錨點**（`memory_server/utils/zone_paths.py`，只用標準函式庫）：
   - 「區」是代理人寫得到的資料夾（錨點）底下。錨點 5 個：`PERSONAL_DRIVE`（`personal_drive`，區是 `<id>` 與底下）、`CUSTOM_DRIVE`（`custom`，區是 `<id>/drive` 與底下；`custom/<id>` 本身與裡面的設定檔不是區）、`MCP_SERVERS`（`mcp_servers/managed`）、`DOCGEN_OUTPUT`（`temp/docgen_output`）、`SANDBOX_STATE`（sandbox state 根）。錨點本身不算區內。
   - PR 1 只對前兩個執法（`ENFORCED_ANCHORS`）；隔離層與 Z10 已用全部 5 個（`ALL_ANCHORS`）。PR 2 起五個都執法（決定 13）。
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
    - 改壞清單 `tests/zone_mutations.json`（執行器 `tests/zone_mutations.py`，改到防線程式、改壞清單或連結掃描範圍內測試的 PR，驗證步驟都手動跑一次；只在 POSIX 判得出的到 Linux 容器跑、只在 Windows 判得出的在 Windows 跑）：每條故意改壞都要讓指定的測試失敗，判定只看 junitxml 失敗的 `message`（traceback 會印出測試原始碼，不算）。一般 pytest 裡另有測試釘住清單：每條的片段在目標檔裡剛好出現一次、列出的測試存在、R1–R14 每條都有（PR 2 起另加 P1–P10、Q10、Q11、G3、G4、TF）。改之前的樹上，連結測試以 `LINK_FOLLOWED` 標記失敗。

## 已知的界線

- **Windows 本機**：標準函式庫沒有 `dir_fd`，檔案動詞查連結和開檔不是同一個動作。只換一次連結的會被擋下（開檔後比對檔案識別碼）；不停來回換連結、剛好開檔時是連結、檢查時不是、比對時又是，仍可能讀寫到連結另一端的檔案（含截斷後寫入、追加、原子寫入蓋過去）。建立新檔、原子寫入的暫存檔與建上層資料夾可能在另一端留下空檔或空資料夾；刪除只在刪之前查一次；只讀時開檔前不查，路上現成的連結會先開到再拒絕（內容不讀出，但指向網路位置的連結可能先連上那台主機）。遞迴刪除、建資料夾、改名、補權限、走訪同樣有時間差；`/v1/outputs` 依路徑重開檔案，檢查與開檔之間有時間差。能利用的只有本來就有 Touka 權限的程式（Windows 本機沒有沙盒程序會寫區；代理人終端不經隔離、每條指令都要人核准；未隔離的第三方 MCP 伺服器的工具依模式執行）。8.3 短檔名可以寫進自己的 `.touka`，屬於下面「代理人自己的 JSON」那一項。
- **不經 RM 的程式**：hook 程式（ADR-0292）以 Touka 的權限執行，可以讀寫任何 Touka 權限到得了的地方；像「工具執行後跑 lint」這類 hook，lint 工具若會執行檔案裡的設定或外掛，就等於以 Touka 的權限跑代理人寫的東西，管理員要選不會執行專案內容的工具。hook 的 MCP 可寫資料夾禁區只來自 `mcp_server_isolation.json` 的 `sandbox_paths`：包在 Landlock 裡的第三方與 scaffold 伺服器引用的資料夾不在禁區，IDE 與未隔離的 MCP 伺服器能寫哪裡 Touka 不知道；管理員不要把 hook 程式放在這些地方，也不要讓它們寫得到 `config/hooks/`。執行器不載入 RM（規則路徑由 kernel 逐段不跟隨地開），以後在執行器加 I/O 不受這條規則保護。dispatch 自動啟動的外部 AI 與 IDE 在主機上，照它們自己的權限。
- **代理人自己留下的常駐行程**：proxy 本機（Landlock）用 `setsid` 留下的行程不在任何登記裡，停不到，可以讓重置或刪除一直以固定原因失敗，直到重啟 proxy（資料留著、不會刪到別處）。執行器裡的由 subreaper 收（最多約 30 秒）。proxy 本機的 subreaper 另排。
- **被入侵的第三方 MCP**：經 APPDATA 對映寫得到 `personal_drive` 底下，同樣能讓重置或刪除以固定原因失敗。
- **別名上層**：`contains_zone` 只認得錨點自己的上層鏈；從另一個掛載路徑進到 data 的遞迴動作靠逐層判斷。`st_ino == 0` 的檔案系統上身分比對不可用，只剩字面與解析後兩項。錨點底下另外掛的檔案系統，走訪與刪樹不跨過去（備份、GDPR、診斷不收，刪除失敗並說明）。
- **觀察者看不到的誤用**：`json.dumps(z)`（標準函式庫只看類別名稱就丟錯）。
- **硬連結不處理**：跨 bind mount 回 EXDEV、Landlock 的 REFER 有 no-gain 限制、執行器沒有掛設定資料夾。（PR 2 更正：proxy 容器另外掛了整個專案資料夾，在那個掛載裡 `config` 與 `data` 是同一個，EXDEV 的理由不成立；結論不變，理由見「已知的界線（PR 2）」的硬連結。）
- **代理人自己的 JSON**：任務板、派工結果、`.touka` 在代理人可寫的空間，代理人直接改 JSON 就能偽造狀態；不跟隨連結擋不住，另排。
- **改版前的備份**可能含連結另一端的內容。
- **改名失敗的原因**：`custom/<id>` 新舊其中一個是掛載點（跨檔案系統）或 Windows 上檔案還開著時，改名回 `SYSTEM_ERROR`，不另外說明原因。
- **絆線**看不到子行程裡的 I/O 與 `stat` 類呼叫；`os.open` 的 audit 事件不帶 `dir_fd`，以 `dir_fd` 開的相對名稱也看不到（產品程式裡這樣開的只有 `no_follow` 與隔離 kernel）。
- **其他三個錨點**：見最後一節「PR 2」的已知界線。

## 後果

- 人類自己把 `personal_drive/<id>`、`custom/<id>`、`custom/<id>/drive` 做成連結時，那個代理人的檔案功能一律拒絕，重置、刪除與改名失敗並說明原因。錨點本身是人做的連結照常可用。
- 專案根在某個代理人的個人空間底下時，專案的檔案功能拒絕；專案根在 data 根上層時，list、find、grep 照常但看不到代理人資料夾，遞迴刪除、複製、打包拒絕；專案裡指向代理人資料夾的檔案連結，打包與複製略過。
- docgen 一類的工具在 PR 1 不能用（任何平台）；今天經隔離啟動時本來就讀寫不到代理人資料夾，主要影響 Windows 本機經人同意直接啟動的。
- 重置、刪除靈魂、刪除與改名自訂模型會先停掉那個代理人的全部指令（設定頁模型分頁的刪除也一樣）；和靈魂同名的舊資料夾不停，但期間靈魂的新指令與心跳一樣被擋。別的代理人正在重置、清除或刪除時，刪除與改名自訂模型回 409（`AGENT_RESET_IN_PROGRESS` 的訊息改成「另一個重置、記憶清除，或刪除、改名代理人的操作正在進行」）。設定頁模型分頁刪除自訂模型時查擁有權，不是擁有者的一般使用者回 403（以前刪得掉）。
- 還原時 `data/` 與 `config/proxy/` 底下名稱有空段、`.`、`..` 或從根、磁碟代號開始的成員略過並列出（以前雙斜線開頭的會把 zip 的內容寫到 data 或設定資料夾以外，含 `..` 的會把既有的檔案搬出去）。
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

## PR 2：`mcp_servers/managed`、docgen 輸出、sandbox state（2026-10-30）

### 背景

- PR 1 之後，演化的工具資料夾（`managed/<skill>`）、文件產出（`temp/docgen_output`）與 sandbox state（以下 ST）仍由 Touka 以一般路徑讀寫：演化先解析判斷再寫工具檔，兩者之間有時間差；計分卡依工具軌跡裡的路徑讀產出檔、不限位置；ST 的建立、清除、改名與自測用字串方法。
- 隔離中的程式只拿到 `managed/<skill>`、自己的 `ST/proxy/<kind>/<owner>/{home,tmp}` 與輸出區底下的權利，`<skill>`、`<owner>` 本身與上層換不掉；危險在 Touka 進到這些資料夾底下的那一段。
- 另外兩處由對外存取盤點找到、屬 PR 1 漏掉的呼叫點：靈魂檔讀寫（`utils/soul_io.py`）與還原備份時 data 成員的落點。

### 決定（接續 1–12）

13. **執法範圍**：`ENFORCED_ANCHORS = ALL_ANCHORS`（兩個名稱保留，值相同）。字串方法的拒絕、走訪的剪枝、絆線都擴大到五個錨點。ST 底下全部算區（含只有 Touka 寫得到的 `ST/proxy`），代理人深度是 `proxy/<kind>/<owner>`（3 段）。區內的刪除方法拒絕短於代理人深度的路徑（整個錨點、`ST/proxy`、`ST/proxy/<kind>` 刪不掉；`CUSTOM_DRIVE` 照 PR 1）。
14. **通則**：產生函式與存取函式回 `None` 只代表一件事（寫在函式說明裡），其他拒絕一律是帶原因的例外；每道檢查都有只有它擋得到的輸入，並有直接打這一層的單元測試；改寫舊判斷時逐一對應舊的特殊值（「全部」「沒設定」「讀不到」），各有測試。
15. **產生函式**：`tool_root(skill)`（`None` 只代表名稱不合格）、`sandbox_state_proxy()`、`sandbox_state_dir(kind, owner)`（`None` 只代表 ST 沒設；名稱不能當一段時丟 `ZoneSegmentError`）。`anchor_root` 只給 `PERSONAL_DRIVE` 與 `DOCGEN_OUTPUT`。建出 `ZonePath` 只在 `zone_paths.py` 與 RM。
16. **區字串 `zone_text(zp)`**：解析後的錨點加字面段落，不做 I/O、不登記交接。只用來交給別人：沙盒規則與 profile 的 cwd／`HOME`／`TMPDIR`、子行程的 argv、寫進設定的部署條目；也可以拿來比對，但比對結果只能讓判斷更嚴：跟隨連結算出的結果只准用來拒絕，不准用來授與（演化沙盒規則的讀、寫、執行與 cwd 只用字面位置；MCP 來源分類與隔離 profile 的兩處授與目前不符，見已知的界線）。venv 的 python 在 POSIX 本來就是連結，所以 argv 與部署條目用它，不用 `zone_handoff`。`get_tool_root` 回 `str`。
17. **新的區內方法**：
    - `zone_rename`：同一個資料夾裡改最後一段，路徑必須嚴格深於代理人深度（唯一用途是開機把 `<owner>/tmp` 改名後刪）。
    - `zone_access`：最後一段不是連結、存在，而且 `os.access` 成立；只供狀態判斷，答錯只會變成「不可寫」。
    - `handoff_cwd(text)`：包住的 MCP 交出 cwd。不在任何區裡、不是錨點也不含區，就交出判斷用的字串（絕對、以字面消掉 `..`；`連結/..` 實際走到哪裡都不算）；字面在區裡而且段落都合格，經 `zone_handoff` 交出，交出的字串要和計畫的 cwd 相同；其他（只有解析後才在區裡、是錨點或含區、和計畫不同）一律拒絕。字面比對同時看錨點的字面與解析後寫法，所以 data 根或 ST 經過人做的連結時照常可用。
18. **演化**（`EvolutionPathGuard`、`tools/builtin/evolution.py`、`isolation/evolution_command.py`）：
    - 五個檔案方法改走區內方法，遇到連結或不合格段落轉成 `EvolutionRefused("skill_changed")`；`EvolutionRefused` 移到 `evolution_security.py`（`evolution_command` 照舊匯入同一個類別）。
    - 指令的 cwd 經 `zone_handoff`，`run_sandbox_command` 拿掉 `cwd` 參數。
    - `refuse_deployed` 先驗名稱；部署設定涵蓋整個 `managed` 時拒絕（`skill_deployed`）。
    - 兩個技能資料夾的重疊判斷遇到 `OSError`（含連結迴圈）一律當重疊；技能鎖的鍵是 `normcase` 後的區字串。
    - `evolution_security.py` 不再直接做 I/O，移出 `lint_ssot.py` 的 I/O 白名單。
19. **技能名稱只經一個函式**（`isolation_policy.is_skill_segment`）：原本的樣式之外，另擋 Windows 保留名與執行器會判成 Touka 資料夾的 7 個名字（`soul`、`personal_drive`、`toukaproxy`、`toukamemory`、`toukaheartbeat`、`toukadispatch`、`heartbeat-config`，不分大小寫）；名單直接引用 kernel 那一份。`validate_skill_id` 與 `tool_root` 都只經它。
20. **ST 只有一個來源**：
    - RM 的 `_state_setting_or_default()`（設了環境變數用它，沒設用 `data/temp/sandbox_state`）與 `sandbox_state_root()`（容器裡沒設環境變數時回 None）。
    - Touka 要在 ST 讀寫的路徑只經 `profiles.proxy_state_zone`／`proxy_state_owner`：版面沒設 ST 回 None（和以前相同）；版面有設而 RM 沒設、或兩邊解析後不同，在建任何資料夾之前丟 `ProfileError`；`owner` 要同時過舊的安全名稱與區段標準；刪除靈魂時名稱不能當一段（例如 Windows 上結尾是點的 id）就沒有 home 可清，不擋刪除。
    - 丟 `ProfileError` 時：MCP 啟動以 `sandbox_state_unconfigured` 不啟動、狀態顯示「不可寫」、清除沙盒 home 回 `proxy_state_unconfigured`；都不是 stable，第三方 MCP 不會因此未隔離啟動。
    - 只把 `state_root` 當資料用的地方（沙盒規則的保護與封鎖清單、分類的區根、權限自癒的根）照舊。
21. **ST 裡的讀寫**：建 `home`／`tmp`、清除沙盒 home、開機改名清 `tmp`、狀態檢查與自測都走區內方法。清除沙盒 home 時 `<owner>` 本身是連結只刪連結；上層是連結回原本的 `proxy_state_link`。state 自測移到 `ST/proxy/selftest/<前綴+隨機>`，交出時經 `zone_handoff`；`selftest` 不是 kind 名稱。
22. **計分卡**（`scorecard/artifact_verify.py`）：產出檔只在 `temp/docgen_output` 裡經 RM 不跟隨地讀；在輸出區外、路上有連結、名稱不能當一段時回固定文字，不丟例外；路徑只從 `create_<kind>` 的軌跡項目取，不再掃整份軌跡。
23. **執行器只掛 `data/mcp_servers/managed`**（以前是整個 `mcp_servers`）：掛載根本身不能被改名或刪除，執行器裡的程式換不掉錨點。執行器的資料根只認名稱是 `managed` 的讀寫根，沒有就以 `data_root` 不服務（以前退回第一個讀寫根）。執行器在 data 上的自測資料夾跟著移到 `managed/` 底下。
24. **靈魂檔名**（`utils/soul_io.py`）：只收單層名稱；目標在代理人資料夾裡或含代理人資料夾就拒絕（自訂模型的靈魂根 `custom/<id>` 底下的 `drive` 是區）。
25. **還原**（`scripts/manage_backup.py`）：data 成員的第一段只收備份會寫的資料夾（`SANCTUARY_FOLDERS`）與 data 根的 `*.db`，其他略過並列出；比對鍵和代理人資料夾的分流相同（`normcase` 後完全相等）。
26. **靜態檢查**（`tests/lint/test_zone_path_guard.py`）：
    - TF：從 `zone_text` 出發，由程式算出回傳區字串的函式（產生函式）與收區字串的函式（接收函式）；兩個集合分別釘住，新增一個就紅。呼叫只認解析得到的寫法，解析不到又同名的另列名單；`zone_text` 與產生函式的區域別名（`zt = rm.zone_text`、`root_of = …get_tool_root`）經名稱解析也算。
    - G4：區字串只准去列名的地方：純字串操作、比較、接收函式（本體照樣檢查）、`G4_DESTINATIONS`（沙盒規則與 profile、子行程的 argv、部署條目、記錄）、`G4_ALLOWED`（每項標固定類別：只會更嚴〔結果只用來拒絕〕、交給逐段不跟隨的 kernel 原語、只當字串用不做 I/O〔鍵、雜湊、嵌進程式碼〕，或已知界線〔會放寬，必須在本 ADR 的已知界線列名，名單釘住〕）。沒列名的呼叫一律紅；存到任何屬性（含 `self`）都算出口；G4 是 `stat` 類唯一的靜態防線（絆線看不到 `stat`）。`**` 只展開字面字典（或同一函式只指定一次、之後沒改過、沒有別名、沒放進容器、沒被閉包用到的字面字典；讀取與交給呼叫可以，被呼叫的函式改參數內容時在那裡另算出口），其他 `**` 算萬用槽；`launch`、`logger` 一類以變數名稱列名的去處，名稱必須只綁定過列名的呼叫結果。
    - 名稱解析（G3、T3、連結掃描、G4 的 `launch`／`logger` 去處、TF 的種子與產生函式別名共用一個解析器）：import 別名、同一範圍的指定（含 walrus、拆解）、`class`／`def` 綁定。預設不明：只有綁定鏈的終點是產品或標準函式庫的 import、本模組的 `def`／`class` 或字面時才可以確定；參數、不是從產品或標準函式庫 import 的名稱、基底不是靜態 import 的屬性、`with … as x` 進入的結果，以及呼叫這些東西的結果（`request.getfixturevalue(…)`、`d.get(…)`、`type(x)()`）都記成「不明」。`x.__class__` 當成 `type(x)`；`__dict__`、`vars(x)`、`__globals__`、`__func__`、`__wrapped__`、`__self__` 保留底下物件的身分。替換呼叫本身也經同一個解析器認：替換函式的別名（`ms = m.setattr`、`P.object`）、關鍵字參數、`*args`／`**kw`（認不出的位置記不明）、`type.__setattr__(X, …)`，以及經 `__dict__`、`vars()`、`__globals__` 改防線的字典。連結掃描：擁有者或被換的名稱是防線，或換進去的值裝著防線就紅；擁有者不明時，換掉的是防線類別的成員名稱或名稱也不明才紅。T3：擁有者不明時，換掉 `zone_paths` 類別的成員也算（和其他產品程式的函式或屬性同名的除外，例如 `start`）。
    - 連結測試的執行期核對（`tests/support/link_defenses.py`；conftest 在每個測試的 setup、本體與 teardown 之後核對）：session 開始時記下四個防線模組、它們的類別、函式（`__code__`、`__defaults__`、`__kwdefaults__`）與 RM 實例的狀態。monkeypatch 與 `unittest.mock` 的替換照物件身分認，做替換的程式不在 `LINK_SCAN_ALLOWED` 就紅（範圍同靜態掃描：改壞清單列出的測試檔、`*_links_security.py`、conftest、`tests/support/` 與它們匯入的測試模組）；經這兩者的替換照物件身分認，不管怎麼寫出來；不經這兩者的改動（直接指定、改函式的 `__code__`），在 setup、本體或 teardown 結束時還沒換回的不分範圍一律紅；改函式的 `__code__`、`__defaults__`、`__kwdefaults__` 另經 audit hook 記下，範圍內的測試在同一個階段換回也紅。核對的對象與狀態是列舉出來的，看不到的列在已知的界線。直譯器自己寫的快取（`__warningregistry__`、`__annotations__`、`__slotnames__`）不比對；做替換的程式依解析後的路徑認（經 junction、subst 或短檔名進 repo 時一樣）；範圍裡的檔讀不到或有語法錯誤時只是不追它匯入的模組，不讓 pytest 啟動不了（DEV-B 第 3 輪）。靜態掃描保留當提早提醒，兩者共用同一份放行清單。
    - G3：`ZonePath`、`agent_root` 兩個名稱只在 `zone_paths.py` 與 RM 出現；`__class__`、`__setattr__`、描述器的 `__set__`／`__delete__`（屬性或字串）與呼叫 `type(x)` 的結果（含存成變數或經函式回傳）逐處釘住。
    - G1、G2、T3 跟著擴大（`zone_text`、交接與兩份名單的呼叫點，新的產生函式與 ST 存取函式，錨點名稱 `docgen_output`、`sandbox_state`、`managed`）。
    - 改壞清單的清單測試另要求 PR 2 新增的方法都被某一項涵蓋；連結測試、`tests/conftest.py` 與 `tests/support/` 不准替換 `no_follow`、`zone_paths`、RM 與 `evolution_security` 的屬性。
27. **演化連結測試的根**：演化的連結測試用測試 session 的 data 根，不用 `zone_data_root`：舊程式的技能根在 import 時就固定成 session 根，新舊程式要指到同一處，連結測試才能在改之前的樹上紅。這些測試用不重複的技能 id，結束時不跟隨地清掉。其他測試照決定 11 只用 `zone_data_root`／`point_zone_roots`；自己組版面的隔離測試用 `tests/support/isolation.match_state_root` 讓 RM 的 ST 指到同一處。
28. **絆線看子行程的執行檔**（調整決定 8 的 Z9）：執行檔在 `data/mcp_servers/managed/<skill>/.venv` 底下時當成交給子行程的資料、不記（`EXEC_DATA_ANCHORS` 列錨點與技能底下的資料夾）。那裡會被執行的只有技能 venv 的 python（`_venv_python` 的區字串，P4 列為子行程 argv）；POSIX 上它本身就是連結、無法交接。隔離時執行檔是啟動器，未隔離要人逐次核准。技能資料夾的其他地方、其他錨點裡的執行檔與所有子行程的 cwd 照舊要交接。

### 已知的界線（PR 2）

- **Windows 本機**沒有 Landlock：演化以 Touka 的權限跑模型寫的程式，那段程式本身什麼都做得到；區內方法只保證 Touka 自己的讀寫不跟隨，時間差同上面的「Windows 本機」。
- **執行器自己的 I/O**（自測、清殘留、準備資料夾）不經 RM，照 PR 1。
- **硬連結**：隔離中的程式只能在自己可寫的資料夾之間建，指到的檔案本來就寫得到。未隔離的程式（人核准的，或依禁令 66 ⑩ 直接啟動的第三方 MCP）留下的硬連結在結束後仍有效，之後 Touka 在區裡就地寫入（上傳、生圖、追加）會寫進那個檔案；隔離中的代理人若在自己可寫的資料夾碰到這種硬連結，也能直接寫進那個檔案。這和那次未隔離執行本身的能力等價，不另外處理，也不加 `st_nlink` 檢查（會改 PR 1 原語的語意、誤拒正常的多重連結檔）。原子替換的寫入不受影響。
- **部署後程式內容被改**：部署雜湊只含 `server.py` 與 `requirements.txt`，之後的啟動不重核內容；部署後還在跑的程式改過的內容，下次啟動會被執行。這是內容、不是連結，列為後續。
- **MCP 來源分類**用解析後的位置授與唯讀＋執行，少數寫法下自建條目可能拿到另一個技能資料夾的讀與執行（拿不到寫）。觸發的也可以是隔離中的 MCP 自己在 ST home 放的連結（不限條目的寫法，原本判成第三方的條目也算）。修法是授與只用字面候選（決定 16），修法小，排在下一個；`G4_ALLOWED` 裡這四項標成已知界線：`src.utils.mcp_origin:_item_paths`、`src.utils.mcp_origin:referenced_paths`、`src.utils.mcp_origin:_candidates`、`src.utils.mcp_origin:is_file_reference`（測試核對名單和這裡一致）。
- **`layout.state_root`**（隔離版面的 ST）是另一個讀取點：只當資料用（沙盒規則的保護與封鎖清單、分類的區根、權限自癒的根），P7 存取函式在兩個來源不同時拒絕；TF／G1 不把它當 ST 來源，以後有人直接拿它組路徑做 `stat` 類 I/O，靜態檢查看不到。
- **守衛不追的寫法**：測試替換 `os.lstat` 一類作業系統函式不算替換防線（防線本身沒被換掉，只影響那個測試）；靜態掃描不檢查測試類別繼承防線類別後對 `self` 屬性的替換，也不追從產品模組另存的別名再替換（這兩種經 monkeypatch 或 mock 時，執行期核對照物件身分抓得到）。執行期核對看不到同一個階段（setup、本體或 teardown）裡直接改了又換回的（例如類別屬性、`vars(mod)[k] = v`），這部分只靠靜態掃描；typing `Protocol` 類別第一次建立實例時才換上的 `__init__` 不比對。G3 不計對 `type(x)`、`x.__class__` 結果的其他屬性存取（不是呼叫，也不是 `__set__`／`__delete__`）。保護名單不做遞移封閉（經包裝函式碰到產生函式的公開方法不會自動列入 T3）：T3 管產生函式與 data 根，檔案方法的替換由連結掃描把關，測試因替換包裝層而變空洞由改壞清單兜底（每條防線規則都要有測試會紅）。
- **執行期核對守的範圍是列舉的**（DEV-B 第 3 輪；都要刻意寫才碰得到，下一個 PR 補）：裝飾器包住的原函式（經 `__wrapped__` 才拿得到，例如 `functools.wraps`、`lru_cache`）不算防線函式；防線模組或類別裡的可變容器，內容被改不比對；別處對防線的參照用 `setitem`／`patch.dict` 換掉時不核對被換掉的值（`setattr` 一類有核對）；`LINK_GENERIC_NAMES` 的名稱不比對；經記下的替換打開的位置整個 session 有效，替換外洩（測試結束沒換回）時，之後在那裡的直接改不報，每個測試開始時也會清掉之前留下的違規；放行清單只綁做替換的程式，不綁換的目標，也不綁哪個測試用它；範圍不追 `from tests.pkg import 子模組` 匯入的子模組（`tests/support/` 本來就整個在範圍內）。
- **靜態掃描的殘留**（只影響提早提醒；經 monkeypatch 或 mock 時執行期照物件身分抓得到）：`cast`、`unwrap`、`copy` 一類回傳參數的呼叫結果、`getattr(X, "__dict__")`、`functools.partial(m.setattr, …)`、參數的 `patcher.object(…)` 認不出。T3 擁有者不明時比對的成員名單排掉和其他產品程式撞名的名稱，這份排除沒有釘成常數、會自己變多；另一方面 `name`、`parent`、`parts`、`key` 這種常見屬性名可能在無關的測試誤報（會紅、看得到）。
- **G4 不算「寫進呼叫端的容器」**：參數上的 `+=`、`|=`、重新綁定過的參數上的寫入、`setdefault` 不算出口。G4 是 `stat` 類唯一的靜態防線；根本做法是測試期間包住 `os.stat` 一類函式、對代理人資料夾的字串記一筆，讓 G4 也降成提早提醒，另排。
- **G3 不計對已建立的 `ZonePath` 再呼叫 `__init__`**：這樣能改掉它的錨點與段落。修法是 `ZonePath.__init__` 遇到重複初始化就丟 `ZoneSegmentError`（產品程式一行），排在下一個 PR。
- **隔離 profile 另兩處授與用了跟隨連結的結果**（PR 2 以前就有，沒有讓 PR 2 的範圍變寬）：`public_config_rules` 用解析後的錨點找上層資料夾的公開設定檔授與唯讀（G4 經 `src.services.isolation.profiles:_resolve` 看得到，列成已知界線）；代理人的專案根解析後授與讀寫執行，解析和授與之間有很短的時間差（專案根不是區，G4 不管）。下一個 PR（MCP 來源分類）照同一條規則一起修：授與只用字面計算的值，解析結果只出現在「不符就拒絕」的判斷。
- **`touka-init`**（docker 的權限初始化）以路徑遞迴、遇到連結不跟隨，但檢查與動作之間有時間差；初始化時有代理人的程式同時在跑，可能改到初始化範圍內其他資料夾的擁有者與權限（不改內容、不改設定以外的寫入落點）。修法屬部署元件，另排。
- **靈魂 id** 也會成為執行器掛載根的第一層，建立靈魂時沒有套用決定 19 的名單；只有管理員做得到、C2-2 起就有，另排。
- **`lint_ssot.py` 的白名單**用檔名整檔豁免：同名的新檔也會被豁免。PR 2 釘住每個檔名在 repo 裡對到的路徑；改成逐條規則或比對 repo 相對路徑另排。
- **TF／G4 追不到**：值進了列名去處建出的物件以後（例如 profile 的欄位，交給啟動器逐段不跟隨地開）、`eval`／`exec` 與動態匯入。
- 以 Touka 權限執行的程式（人核准的未隔離指令、直接啟動的第三方 MCP）能把 `<skill>`、`<owner>` 甚至錨點本身換成連結；它們當下就能直接改設定，能力等價，不另外處理。
- 計分卡讀 zip 沒有解壓縮大小上限（以前就是這樣，只有有權限的人能跑計分卡）。
- 演化寫的工具檔權限是 0600（以前是 umask 預設）。

### 後果（PR 2）

- **升級**：
  - 執行器的掛載改成 `data/mcp_servers/managed`。GHCR 安裝要在升級前建好這個資料夾（擁有者與 `data` 相同；`create_host_path: false`，舊安裝沒用過演化就沒有），否則 `up` 會報 bind 來源不存在、執行器不會建立；`docker-compose.yml` 的 `touka-init` 會自動建。單獨開發用的 `ToukaProxy/docker-compose.yml` 沒有 `touka-init`：Linux 上升級前先建好這個資料夾（擁有者 1001），否則 Docker 以 root 建出它，演化與執行器自測會因權限失敗（執行器判 `none`）。新映像配舊 compose、舊映像配新 compose 都判 `none`（fail-closed）；執行器起不來時 Touka 照常運作，只是 Auto 與 YOLO 的代理人指令每條都要人核准。
  - 技能名稱是決定 19 那 7 個名字（不分大小寫）的，之後再演化以 `invalid_skill_id` 拒絕；沒有執行器的部署裡已部署的照常啟動；有執行器時，PR 2 起執行器以 `touka_root` 整個不服務（以前可寫根是 `mcp_servers`，這些名字在第二層不會觸發；現在所有代理人指令與自建 MCP 都受影響），要先把設定裡引用它的條目移除再改名。
  - 舊版留下的 `ST/proxy/.touka-selftest-*` 與 `data/mcp_servers/.touka-executor-selftest-*`（自測中途中斷才會有）新版不再清，可以手動刪除。
- 演化的技能資料夾或 ST 裡出現連結時，該動作以固定原因拒絕（`skill_changed`、`proxy_state_link`），不讀寫連結另一端。
- 計分卡只讀輸出區裡的產出檔，其他位置一律判失敗。
- 靈魂檔名只能是單層（今天的 `soul.md`、`config.json` 不受影響）；Windows 上另拒保留名與結尾的點、空白。
- 還原時備份不會寫的 data 成員略過並列出。
- ADR-0292 的「暫時不要啟用 command hook」解除。

### 驗證（PR 2）

- `tests/unit/test_resource_manager_anchors.py`、`test_sandbox_state_access.py`、`test_zone_paths.py`：新產生函式、區字串、新的區內方法、ST 的單一來源與存取函式。
- 連結測試：`test_evolution_links_security.py`、`test_sandbox_state_links_security.py`、`test_soul_io_zone_security.py`、`test_scorecard_artifact.py`、`test_manage_backup_links_security.py`；`test_skill_names_security.py`（決定 19）。
- 執行器：`test_executor_server.py`、`test_executor_mcp_start_security.py`、`test_executor_compose_security.py`、`test_isolation_writable_roots.py`、`tests/lint/test_isolation_ci_workflow.py`。
- `tests/lint/test_zone_path_guard.py`（TF、G3、G4 與擴大的 G1、G2、T3）、`tests/unit/test_zone_mutations_manifest.py`；改壞清單在 Windows 本機與 Linux 容器各跑一次，每項都被列名的測試殺死。
- 連結測試在改之前的 `2529ab60` 上執行一次，失敗訊息含 `LINK_FOLLOWED`。
