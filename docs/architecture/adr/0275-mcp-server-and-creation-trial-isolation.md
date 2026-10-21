# ADR-0275：MCP 伺服器與創建試跑的隔離（C2-3）

- 狀態：✅ 已接受（iso-c2 C2-3；修訂 ADR-0069、ADR-0186、ADR-0191、ADR-0198、ADR-0273；補註 ADR-0195、ADR-0254、ADR-0272，八份均已回指；已接線，不代表已部署或完成整批驗收）
- 日期：2026-10-17
- 影響範圍：ToukaProxy 的 `sandbox_kernel/{spec,launcher,relay}.py`、`services/isolation/{mcp_launch,mcp_profiles,runners,status,executor_session,evolution_command,policy}.py`、`utils/{mcp_origin,mcp_secrets,isolation_policy}.py`、`services/mcp_isolation_store.py`、`models/mcp_isolation.py`、`protocols/manager.py`、`protocols/mcp/{client,environment,isolated_stdio}.py`、`services/creation/`、`tools/builtin/{creation,evolution,system}.py`、MCP／創建／狀態路由；ToukaChat 的 MCP 設定頁、領域 hooks、審批卡與側欄；相關沙盒測試與隔離 CI。
- 相關：ADR-0069（創建試跑）、ADR-0186（啟動參數與哨兵）、ADR-0191（restart 時限）、ADR-0195（政策 carry-over）、ADR-0198（環境與 principal 連線）、ADR-0207（單行程前提）、ADR-0254（MCP 檔案隔離缺口）、ADR-0272（隔離核心與執行器）、ADR-0273（連線生命週期）。

## 背景與沿用邊界

C2-1 讓代理人指令經 Landlock runner，沒有足夠隔離時只有人能核准；C2-2 增加執行器容器、控制通道、可信啟動器與權限自癒。原先 MCP stdio 只限制環境繼承，仍能用 Touka 的檔案權限執行；已部署的自建能力又可經演化覆寫，驗證成功字串也不足以證明 MCP 能運作。

C2-3 將正式 MCP 啟動與創建試跑接上相同隔離邊界。遠端 URL MCP 不由 Proxy 啟動子程序，不納入本次；IDE 終端、Dispatch 自動召喚也未因此隔離。角色權限、三模式、底線與工具廣告閘仍各自成立，MCP 伺服器隔離不代表工具呼叫取得額外授權。

## 決策

### 1. 來源、隔離紀錄與單向修改

`server_config.json` 維持原格式；來源、額外允許資料夾另存 `get_config_dir()` 下的 `mcp_server_isolation.json`。格式為 version 1、servers 按伺服器名索引；每筆有 `origin`、`sandbox_paths`、`bound`、`updated_at`。讀寫經 ResourceManager，bootstrap 只在受管初始化時播種出貨範本。

| 寫入路徑 | origin |
|---|---|
| Hub 安裝 | `hub` |
| 技能安裝的 MCP 接線 | `skill` |
| 人類設定頁新增 | `manual` |
| `create_mcp`／創建 HTTP | `creation` |
| Touka 模板 scaffold | `scaffold` |
| 演化部署 | `evolution` |
| 人類把第三方或未標示改成代理人建立 | `agent` |

來源不由模型 spec、前端任意欄位或匯入檔決定，也不加入 `POLICY_FIELDS` carry-over。設定頁只能往 `agent` 修改；不能把自建改成第三方。Hub 不能覆寫自建來源。匯入不採納來源或額外資料夾，不從外部快照取得較寬權限。

`bound` 是 command／args／url 正規 JSON 的 SHA-256，不含 env；人類直接換 token 不會讓資料夾授權失效。非人類存檔或快照匯入的 env 名稱、值或刪除有變動時，另行撤銷第三方／scaffold／未標示來源，改為 `agent` 並清除資料夾；既有自建來源保留。先回填遮罩再比較，只有遮罩回填不算變動；未提供 human 時按非人類處理。

讀取時若 bound 對不上，舊自建來源保留；第三方與 scaffold 紀錄當未標示，舊資料夾授權不使用。

設定頁存檔改變程式身分時，`_rebound_origin(human=True)` 保留原有第三方／scaffold 來源；非人類存檔則改成 `agent`，原有自建來源兩種情況都保留。存檔重新綁定 bound，舊 sandbox_paths 一律清除；人類修改沒有來源紀錄的既有條目時仍維持未標示。

快照逐筆拒絕非物件條目並回報 warnings；merge／replace 都保留該名稱既有有效設定，其餘合法條目繼續處理。修復壞的既有條目時撤銷舊來源與資料夾；mcpServers 本身不是物件則拒絕整份匯入。

首次不存在視為空紀錄；本行程讀到過之後消失、讀取失敗、JSON 或頂層結構錯誤則為 unreadable：不覆寫，所有 stdio 當自建，額外資料夾不用，寫入回 `PERSISTENCE_ERROR`。未知 origin 當自建；個別壞路徑項目丟棄，不讓整份設定失效。

新增／改程式先寫隔離紀錄再寫伺服器設定；刪除反向處理。中途失敗留下的 bound 不符紀錄不能給權。全體 store 物件共用程序內鎖，持鎖區不 await，原子寫入；與同意帳本、每個技能的鎖一樣，以單一 Proxy 行程為前提（ADR-0207）。多行程支援必須先換跨行程鎖。

### 2. 分類與啟動判定

`utils/mcp_origin.classify` 是分類來源：先判遠端，再套用通用、只會更嚴的引用規則，最後看紀錄。command、所有參數與 path-like env 指到代理人可影響的位置，一律自建；資料夾、普通資料參數、尚不存在的路徑也算，不依啟動器或套件來源旗標名單放寬。command／參數中的現存資料夾也從嚴判自建；UNC／裝置名稱同樣從嚴，macOS 路徑比對忽略大小寫。

分類與部署凍結共用 `mcp_classification_bases`：Proxy cwd、自建及第三方的 state home 都檢查，只計算位置、不建立目錄。任何基準命中就採自建，各基準找到的 managed 技能取聯集。沒有分隔符但帶副檔名的尚不存在檔名也檢查；npx／uvx／bunx 的 scoped package 規格在該位置不存在時保留套件語意。

command／args／env 的值含 `$`（含變數與命令替換）、`%…%` 或 `!…!`，都從嚴當自建。完整的非 `file:` URL 只做上述展開檢查；其餘值還檢查反引號、萬用字元與大括號 `* ? [ {`、單字內引號及可展開位置的 `~`。秘密值含 `$` 或 URL 編碼如 `token%2F%3A` 也會觸發，即使實際不會展開。其餘完整非 file URL 不當本機路徑。

分類只解析，不改寫啟動的 command／args。Docker image／volume、deno npm／jsr 規格、版本及模組名原樣傳入；既有哨兵解析與合法 scaffold 的 APPDATA profile 對映見 §3。改判自建後不能沿用第三方的 sandbox_paths；層級 none 時要逐次由人確認。

| 類別／狀態 | 行為 |
|---|---|
| 遠端 URL | 維持遠端傳輸 |
| 自建，層級 executor／landlock | 經對應 runner；取不到既定 runner 就拒絕，不自動換弱方式 |
| 自建，層級 none | `waiting_isolation`；只有人確認一次降級方案後可啟動 |
| 第三方／scaffold，本機 Landlock 可用 | 在 Proxy 本機包住；不因全域 executor 不可用而停用本機保護 |
| 第三方／scaffold，本機尚未判定或暫時故障 | `waiting_probe`，不先直接啟動 |
| 第三方／scaffold，本機穩定不可用 | 維持 SDK 直接啟動、標示未隔離 |

自建來源為 creation／evolution／agent；第三方來源為 hub／skill／manual。未標示依引用及 scaffold 形狀辨識；scaffold 來源但程式形狀不符也當自建。讀不到紀錄不影響遠端分類，stdio 一律從嚴。

第三方看本機判定而非全域政策層級：執行器故障不等於本機檔案保護失效。ABI 3 可包檔案但沒有 TCP 封鎖能力，網路設定或封鎖埠問題也可能削弱保護；features 如實呈現。

### 3. 三種 profile

- 自建 `mcp_self`：系統唯讀執行路徑與自己的 HOME／TMP 可用；演化部署的單一 `managed/<skill>` 只給唯讀＋執行。多個程式目錄或名稱不合法不授權。不給建立者個人資料夾、專案、公開設定檔與 sandbox_paths。
- 第三方 `mcp_third_party`：自己的 state，加上引用路徑與人設額外資料夾。一般目錄給讀寫；檔案只開所在目錄唯讀＋執行，先過永不授權。引用的所在目錄若是家目錄或檔案系統根，不直接放行；不往上尋找專案標記。
- scaffold `mcp_scaffold`：第三方 profile 加上經形狀核對的自身 `config/mcp_servers/<name>` 程式目錄唯讀＋執行；永遠在 Proxy 本機。

合法 scaffold 的 APPDATA 資料哨兵是分類的狹義例外：須通過程式形狀檢查，來源是 scaffold 或未標示；載入程式的 env 不適用。它的 args 在 profile 中對映到 personal_drive，試跑與正式啟動一致；除此之外，不因分類或解析相對路徑重寫 argv。

第三方 profile 的 `map_appdata` 仍能處理 APPDATA／等於資料根的絕對值與輸出哨兵，但分類先於 profile。一般第三方的 data 根參數會先改判自建，不能宣稱第三方的 args／env 都會改成 personal_drive；自建沿用既有哨兵解析。Hub Filesystem／docgen 同樣受分類規則約束。只有真正進入第三方／scaffold profile 的受管引用才有 personal_drive／docgen 輸出夾的精確開放。

profile 依實際 state home 授權，只認絕對、含分隔符或實際存在的引用，不因套件／模組名帶點而授權。依賴 Proxy cwd 的相對腳本須改成絕對路徑；分類時查過 Proxy cwd 不代表啟動時會切回去。

`sandbox_paths` 最多 16 筆、絕對資料夾路徑、`ro/rw`；只給第三方及 scaffold，存檔與啟動兩次驗證。啟動時拒絕的路徑不給權；第三方可能仍啟動但用不到該路徑，不能因此自動擴權。

每台隔離啟動的 cwd 是 state 的 home，HOME／TMP／快取由 profile 改寫。段名用類別標籤加伺服器名的 SHA-256 前 24 個十六進位字，避免名稱穿越或類別撞路徑。Proxy 用 `mcp_self`／`mcp_third`，執行器用 `mcp`，由既有 state root 動態解析。

### 4. exec_env 與相容性

把條目 env 放進啟動器環境會讓 loader、直譯器或套件工具在限制生效前讀取它。隔離啟動改以 spec 的 `exec_env` 傳遞，launcher 套完檔案／網路／資源限制、關閉多餘 fd 後，只在交給目標的環境合併，不改自己的 `os.environ`。

名稱限制為 `[A-Za-z_][A-Za-z0-9_]{0,127}`，最多 256 項、合計 64 KiB；不允許 NUL。`checked_exec_env` 先移除保留名稱與 `exec_env_stripped` 命中的名稱，只在 log 警告名稱、不記值；kernel 遇到未被先濾掉的保留名稱仍拒收。完整保留清單見 PROXY_INTERFACE 的 exec_env 說明，正本為 `sandbox_kernel/spec.py`；`XDG_`、`GIT_CONFIG_` 是前綴，其餘保留名稱精確比對，皆不分大小寫。

移除規則對名稱做不分大小寫的子字串比對：`PYTHON`、`NODE_`、`JAVA_`、`GEM_`、`PERL`、`LD_`、`DYLD_`。TOKEN／KEY 本身不觸發過濾，但 `MY_PYTHON_TOKEN`、`NODE_ENV`、`NODE_EXTRA_CA_CERTS` 仍會被移除。需要時改用伺服器支援的不衝突名稱，或原本規則允許的 direct 路徑，不能因此自動降級。過濾後仍不合格式的名稱才由 kernel 回 spec_invalid。

啟動器自己的環境只由 `LaunchEnvironmentPolicy.launcher_base()` 提供 SDK 預設加白名單。正式隔離啟動的條目 env 解析哨兵並加 `TOOL_BASE_URL`；創建試跑保留 clean 語意，不解析 env 哨兵、不加工具網址。direct 路徑繼續使用 SDK 的 production／clean 環境規則。

spec 版本未變、缺 exec_env 等同空。舊啟動器不認得新鍵時拒絕；容器 kernel 與程式雜湊不符也不宣稱隔離。更新必須重建映像，不能靠忽略未知鍵降級相容。

### 5. stdio 傳輸與收尾

SDK stdio_client 不能提供所需的 pass_fds／preexec／release 接點；包住的路徑使用 `isolated_stdio_client`，仍交出 SDK 的訊息 stream，並由原有 owner task 進出。spawn 前才取 plan，成功或失敗都 release，close_fds 為 True。

stdin／stdout／stderr 分流；stdout 單行上限 16 MiB。stderr 持續排空，先遮罩再截斷，每分鐘最多記 50 行、每行 2 KiB，尾段最多 4 KiB；解析錯誤不帶原始資料。共用遮罩涵蓋多行、Python／JSON 跳脫與重疊值，公開工具網址不當祕密遮掉。遮罩後跳脫 C0／C1（tab 除外）與文字方向控制字元，再截斷，避免 stderr 偽造日誌呈現。

manager 在 owner 裡使用 `on_eof="raise"`，非預期 EOF 轉失聯；演化驗證與創建試跑使用 `close`，讓 session 回連線結束，不取消上層任務。

正常關閉先停 stream、關 stdin，等 1.5 秒，再 SIGTERM 等 1.5 秒，最後 SIGKILL 等 1 秒；取消或錯誤在 await 前先殺群組，finally 有界回收並關 pipe／transport。只在群組長尚未被回收時對該 pgid 發訊號，避免殺到重用的群組。owner 另有 7 秒加 1 秒寬限。

Windows 的正式 direct 路徑沿用 SDK；若錯把隔離傳輸交給 Windows，直接拒絕。演化驗證在 Windows 使用 SDK stdio 與 JailedWriter，仍是未隔離、不具 POSIX rlimit。

### 6. 執行器常駐 MCP

START 增加 kind `mcp`；只允許自己的 state 讀寫，cwd 必須等於 home，程式規則最多是精確 managed skill 的唯讀＋執行。`io="stdio"` 將 stderr 另送 ERR；`max_seconds=0` 只准 kind mcp 加 stdio，演化驗證仍有時限。

relay 有 stdio 模式，雙向轉送到 frame 通道，stdin 結束才送 EOF；本機 relay 的 cwd 是唯讀 kernel 目錄，不使用只存在於執行器的 state 路徑。認證、SO_PEERCRED、READY 雜湊與根目錄驗證沿用 C2-2。

執行器把控制讀取與 stdin pipe 寫入分開，最多暫存 16 個輸入 frame；佇列滿視為協定錯誤並終止，不等待空位。STOP／relay 斷線不受 stdin 背壓阻擋；正常 stdin EOF 排在已接收的資料之後。

### 7. 開機、重連與一次同意

manager 的五條連線路徑在每台連線鎖內、重試外先判定；principal-scoped stdio 在 owner 建立前拒絕。非遠端沒有判定結果不得連線。等待不當作握手錯誤反覆重試；舊 generation 不得安裝成新 handle。

重載在第一個 await 前更新安裝版本、作廢舊同意，設定快照綁該次重載；清理序列化，過期工作退出，不能關閉或取代較新的掛載。握手重試綁原 generation、設定及生命週期版本，不能以舊設定借用新版 generation。帶人類同意時只做一次握手嘗試，不套一般握手重試。

熱掛載先同步 registry 與安裝版本，只有該版本的連線仍存活才回報 started。等待隔離、沒有 manager、握手中止或被重載取代時，技能安裝／演化部署回報已儲存但尚未啟動；並行重載可能稍後連上，以 MCP 清單為準。principal-scoped 遠端沒有 request context 時延後連線，不把登記成功說成已連線。

隔離 shared stdio 失聯（含 OOM、自己結束）在 5／10／20 秒後退避重試三次；穩定 600 秒、手動 restart 或重新初始化重設次數。direct／遠端不自動重連。關機先設旗標封住連線與排程，再取消背景工作，不能被後到的重試重新打開。

monitor 除層級變動外，本機變可用、本機原因變動、首次從 pending 得到結果與 executor boot_id 變動也通知。等待者重新判定；未隔離第三方在本機恢復時換成隔離，人同意的 direct 自建在層級恢復時也重新隔離。

自建 `waiting_isolation` 可取得 `manual_start={fallback,features,generation,bound}`。restart body 的 isolation_ack、features、generation、bound 必須與方案相符；人類請求者、privileged、CSRF 都要通過，取鎖後重驗並只用一次。none 下每次開機／重啟都要重新確認；人同意降級 Landlock 後失聯，重連不沿用同意，仍 none 就回等待。

restart 取鎖上限 5 秒，超過回 busy；取得鎖後關舊連線、握手預算 15 秒，逾時還有收尾，後面另有健康資訊查詢。因此不沿用約 22 秒的舊端點上限，也不承諾新的精確總上限。

### 8. 部署後凍結與審批範圍

四個演化入口與取得 profile 前都檢查原始伺服器設定，不靠 registry 的啟用清單。引用 `managed/<skill>` 時凍結該技能；command、載入程式的 env 或選項值涵蓋 managed 根或祖先時，全部技能都凍結。只有普通位置參數恰等於 data 根（含資料哨兵解析結果）不因此凍結全部技能，Filesystem 因此不阻擋未部署技能；此例外不改變它的自建分類。其他涵蓋 managed 根的程式引用仍全部凍結。

凍結與啟動使用 §2 的相同解析基準。停用、改名、巢狀路徑、大小寫、Windows 尾點／空白與實體別名不能解除已命中的凍結；遠端 URL 不作本機程式引用。設定讀不到、無法解析或結構錯誤時全部凍結，檔案不存在才視為空設定。技能名只收單段 ASCII 白名單、拒絕 Windows 保留名稱與非字串。

同一技能操作互斥，部署拒絕撞名。審批快照分別綁定 server.py／requirements.txt 的原始位元組 SHA-256，缺檔或雜湊改變不得部署；最後核對與寫入在同一 worker 完成。核准只涵蓋這兩檔。

不對整個 venv／部署目錄做雜湊鎖，也沒有乾淨重建；其他檔案及 .pth 仍可能執行，設定頁並行存檔亦未共用技能鎖。凍結不撤銷已存在行程的寫權，限制另列。

### 9. 演化驗證使用 MCP 協定

驗證包裝用 runpy 啟動 server.py 並保留 JailedWriter，再由 modern MCP 完成握手、呼叫指定工具，以非錯誤工具回應判定成功。只印成功字串、沒有 mcp.run()、工具回 error 或無法連線都不通過；程式 stdout 不作為成功標記。

POSIX 透過隔離 runner 保留資源上限，未隔離路徑以 preexec 套用；Windows 沒有相同 OS 限制。失敗回有界且遮罩過的 stderr 尾段，子程序精簡 traceback 保留錯誤原因。部署存檔與真正啟動分開回報，等隔離不能報成已連線。

### 10. 創建試跑

`CreationLaunchContext` 在 `services/creation/kinds.py` 定義，一次 grant 與程式指定的 origin 傳給 SMOKE／COMMIT／EVAL；模型不能注入可信欄位。create 看全域層級、scaffold 看本機層級，共用正式 profile。通過分類的合法 scaffold，試跑 args 的 APPDATA 也對映到 personal_drive。

有隔離時 YOLO 依層級執行；none 時仍要人類同意，Auto 的 F5 與 Manual 不放寬。HTTP 不產生降級同意，none 不試跑；SMOKE 回既有 HTTP 200 rejected、committed=false 與 isolation_unavailable。run_smoke=false 可登記，但 EVAL 仍不得在 none 啟動；activate=false 只產生 scaffold。缺 launch context 時 SMOKE 拒絕、EVAL 不啟動。

### 11. 未隔離卡的完整性

direct 卡每個參數值的預覽上限為 64×1024 字元；程式以 len 計算，並非整張卡的位元組上限。任一值截斷，或 verify_mcp_locally 的程式不在參數內可呈現時，complete=false，後端拒絕核准且不記同意；前端也不提供可通過的核准。其他去處沿用原上限與規則。

### 12. 設定頁、狀態與橫幅

GET／PUT `/mcp/{name}/isolation` 回來源、class、record_state、sandbox_paths、bound、can_mark_agent；PUT 需人類及 CSRF，存檔成功排背景重連，applies_on=background_relaunch。條目變動或同意不符沿用 422 代碼，讀不到紀錄回持久化錯誤。一般設定回 isolation_cleared 提醒因程式或非人類 env 變動而清除的資料夾授權。

`/mcp/list` 提供每台 isolation；自建等待才有 manual_start。`/isolation` 提供 mcp 的 unisolated／waiting／suspended／store_issue，不公開名稱或路徑。unisolated 計的是帳本上准許直接啟動的第三方／scaffold，不是即時健康連線數。暫時故障等待 660 秒後轉 suspended，三種計數互斥；probe_pending 不計時。

設定頁有隔離徽章、依原因的恢復說明、常駐「啟動…」確認入口。確認框重新讀 bound 並核對方案，說明未隔離權限與缺 tcp 的風險，送出的正是畫面方案；送出後關閉，同意不沿用。

側欄只顯示台數；expected=none 時不計未隔離第三方。因此原生 Windows 只有這些台數時不顯示 MCP 橫幅，但等待／暫停仍顯示；不能承諾 Windows 永遠沒有 MCP 提示。無效計數各自歸零，其餘警告保留。store_issue 走通用提示。

### 13. docgen 輸出不自癒

temp/docgen_output 沒有加入權限自癒白名單；它是所有代理人共用的第三方輸出位置，不把修復權擴到這裡。權限被改壞可能無法產檔，由人處理。

## 考慮過而不採用

- 把 origin／sandbox_paths 塞進 server_config：改用分檔，保留既有 MCP 設定格式與政策 carry-over。
- venv／整目錄雜湊鎖：本次只有兩檔審批快照與部署凍結，沒有完整目錄保證。
- 從引用檔案往上找專案標記：只開所在資料夾；需要更多範圍由人加精確授權。
- 自建給建立者個人資料夾：自建是共用服務而非每位呼叫者的身分，僅給自己的 state。
- Proxy 行程內直接實作執行器 socket transport：沿用受管 relay 與狀態 fd，避免多一條程序邊界。
- 用 SDK stdio_client 加檔案傳 spec：無法完整提供 spawn、fd 與收尾控制，隔離路徑改用自訂傳輸。
- 第三方在所有不可用情況都停用：穩定不可用保留直接啟動並告知；pending／暫時故障仍等待。

## 已知限制

1. 直接啟動的第三方與人同意的自建仍用 Touka 的檔案權限；有隔離也不是對外網路封鎖器。ABI 3 的本機第三方不擋 TCP，其他網路缺口看 features。
2. 合法 scaffold 的 personal_drive 開放是整個根，不是呼叫者專屬；DebugAgent 等 custom drive 不在其內。Filesystem／docgen 改判自建時不取得這份第三方授權，若經人同意 direct 則仍有 Touka 的檔案權限。其他獲授權代理人可用同一自建 MCP、共用 state。
3. 每台下載快取獨立、無大小上限；第一次下載可能超時。執行器配額是全部工作共用，非每台保證。
4. 隔離失聯重試三次後需人重啟；direct 與遠端不自動重連。none 下手動降級同意不能供下次使用。
5. 原生 Linux 的孫行程無父死訊號，Proxy 強殺可能殘留；群組長已回收但孫行程握著 stdout 時沒有 EOF，不能安全地殺舊 pgid。Docker 容器退出才由 PID namespace 收尾。
6. 本機 Landlock 的演化孫行程可 setsid 脫離群組，可能在部署後仍持原寫權；凍結與技能鎖不能撤銷。執行器 relay 關閉後的遠端回收非同步，同一 state 新舊行程可能重疊。
7. server.py／requirements.txt 雜湊不涵蓋 .venv、.pth 等其他內容；部署與設定頁未共用讀改寫鎖，並行存檔仍可能覆蓋。
8. 分類不是任意 shell／程式碼的完整分析器，不保證任意指令文字的執行語意；普通資料參數、動態字串及含特殊字元的秘密也可能從嚴變自建。解析不改 argv，Proxy cwd 的相對腳本可能在 state home 找不到；自建 create 指向代理人資料夾的腳本也無權讀取。
9. `/proc/*/cmdline` 跨代理人可見，token 不放 args。exec_env 對目標與子行程可見；遮罩值少於四字元不保證涵蓋。
10. 隔離啟動先移除保留名稱與命中子字串規則的 env，只記 log；範圍及相容性影響見 §4。過濾後留下的不合法名稱由 kernel 以 spec_invalid 拒絕；需改英數與底線、首字不能是數字、最多 128 字元。
11. 原生 Windows 自建不自動啟動，JailedWriter 只是可繞過的寫入配額，沒有檔案讀取或程序隔離；遠端 MCP、IDE 終端與 Dispatch 自動召喚不在這次隔離內。docgen 輸出權限不自癒。
12. Linux 真核心測試、容器端到端與放在 Windows 掛載（drvfs 9p）上的 Linux 模組，都只在開發機的 Docker 跑過，不代表部署環境。9p 上有 8 項失敗，原因是 drvfs 對已刪除仍開啟的檔不能 truncate、名稱不分大小寫，都是更嚴、沒有放行，與 C2-2 相同。容器 CI 觸發清單已有 28 個路徑模式，含來源分類、manager、client／environment、創建 smoke、演化與設定入口。

## 使用者看得到的變化與部署告知

- 更新後要重建：原始碼安裝用 `docker compose up -d --build`；GHCR 換同版映像與 compose，依 README_QUICKSTART 分段啟動。舊啟動器會讓自建等待、第三方可能未隔離啟動。
- 第三方首次下載會較慢；仍分類為第三方／scaffold 的伺服器，讀不到檔案時可在人類設定頁加唯讀／讀寫資料夾。Git MCP 需加要讀的專案，Touka 自己的設定、金鑰、原始碼與資料根加不進去。
- 普通資料夾參數、Filesystem／docgen、含展開語法的 args 或 env 也可能改判自建；不能再用第三方額外資料夾，none 下每次啟動都要人確認。秘密含 `$`、URL 編碼含 `%…%` 也可能觸發，分類器不會替你展開或改寫指令。依賴 Proxy cwd 的相對腳本請改用絕對路徑；合法 scaffold 的 APPDATA 資料哨兵仍映到 personal_drive。
- 非人類存檔或快照匯入改 env，會清除第三方來源與額外資料夾；人類直接換 token、只有遮罩回填則保留。匯入非物件條目會列警告，既有有效設定保留。
- 隔離不能用時，自建先等，設定頁「啟動…」只同意這一次；寫未隔離代表 Touka 的全部權限。第三方檢查中／暫時故障也等，自測失敗約十分鐘重試，超過 660 秒顯示暫停；穩定不可用才直接啟動。
- 已部署的自建 MCP 不能再演化更新；用新名稱，或由人移除舊伺服器。停用、改顯示名稱不解除凍結；程式載入位置涵蓋 managed 根或上層會凍結全部技能，普通 Filesystem data 根資料參數不會。驗證必須真的啟動 MCP 並成功呼叫。
- 安裝回報「已儲存、尚未啟動」時，到 MCP 清單查等待隔離、延後連線或重載狀態；這不表示設定沒有儲存。
- 創建工具有隔離時 YOLO 依層級執行，scaffold 看本機；none 還是只有人能核准，Auto 也仍須人。HTTP 不能同意降級。
- 隔離連線失聯會退避重試三次；未隔離與遠端需手動重啟。降級 Landlock 的一次同意在失聯後不沿用。
- 原生 Windows 的第三方只在設定頁標未隔離，不單因這些台數顯示 MCP 橫幅；有自建等待仍提示。URL MCP 與 IDE 的 MCP 連線不受影響。
- create_mcp 不取得建立者資料夾，腳本請走演化；未隔離卡任一參數超過 65,536 字元或看不到程式時不能核准。env 名稱改用英數與底線，token 不放 args。
