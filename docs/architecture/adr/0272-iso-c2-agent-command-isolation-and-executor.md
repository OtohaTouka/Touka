# ADR-0272：隔離第二批（iso-c2）第一、二段：代理人指令的 Landlock 隔離與執行器容器（C2-1＋C2-2）

> C2-3 現況補註：本文保留 C2-1／C2-2 的決策範圍；stdio MCP、創建試跑與部署凍結已在 C2-3 接線，不再以「合併前沒有 MCP 隔離」描述目前程式。容器 CI 觸發清單現為 29 個路徑模式，含隔離模型、隔離設定儲存、stdio 傳輸、來源分類與連線／創建／設定入口。本文原列待重跑的執行器突變已在 C2-3 拆成 9 項，全部因預期防線而失敗，已完成驗證。詳見 ADR-0275。

- 狀態：✅ 已接受（隔離第二批（iso-c2）第一、二段；維護者 2026-10-15 核准隔離計畫第二批；2026-10-16 決定 C2-1＋C2-2 合成這一個 PR，C2-3〔MCP 伺服器的隔離〕另開 PR、另一份 ADR、另做一次對抗式審查。本 ADR 只寫這個 PR 合併後的實際行為：C2-3 合併前，MCP 伺服器〔Hub 第三方、自建、演化部署的〕和合併前一樣沒有隔離，見「已知限制」）
- 日期：2026-10-15（C2-2：2026-10-16）
- 決策者：維護者（2026-10-15 核准隔離計畫第二批的五項決定〔見「背景」〕與 C2 細部設計的第 2～8 項，第 8 項是第 2 次對抗式複審的問題全部修好；C2-2 的行數上限 2026-10-15 放寬、2026-10-16 定為約 7,100；2026-10-16 同意在 AGENTS §11 加一句，並決定 C2-1＋C2-2 合成一個 PR、C2-3 另開 PR）；C2-2 的做法取捨在已核准範圍內經審查裁定（執行器維持和 proxy 同一個 uid 1001 加權限自癒、網路設定不安全時判 `none`、封鎖埠從連線推導且讀不到 fail-closed、自癒白名單、路由快照接上）
- 影響範圍：
  - 隔離核心（只用標準函式庫）：新 `ToukaProxy/src/sandbox_kernel/`（`launcher.py`、`landlock.py`、`spec.py`）
  - 隔離服務：新 `utils/isolation_policy.py`、`utils/touka_dirs.py`、`services/isolation/`（`status.py`、`policy.py`、`consent.py`、`profiles.py`、`project_scan.py`、`runners.py`、`agent_command.py`、`evolution_command.py`）；`config.py`（`is_container_runtime`、新設定鍵）
  - 政策：`models/safety.py`、`utils/hard_floor.py`、`services/permission_service.py`、`services/auto_reviewer.py`、`services/tool_executor.py`（兩處）
  - 接線：`tools/terminal.py`、`services/review/evidence.py`、`services/review/review_loop.py`、`tools/mission_tools.py`、`services/approval_flow.py`、`services/approval_service.py`、`utils/tool_events.py`、`services/evolution_security.py`、`tools/builtin/evolution.py`；Guard #45 基準
  - 前端：`ToolApprovalCard.tsx`、`stream_tree.ts`、`tree_history.ts`、兩語 `translation.json`
  - 部署：`ToukaProxy/Dockerfile`、`.dockerignore`、`docker-compose.yml`、`docker-compose.ghcr.yml`、`ToukaProxy/docker-compose.yml`（ST volume、proxy 的 `net.ipv4.tcp_fastopen: 0`）、`.gitattributes`、`pytest.ini`（marker）
  - 測試：`tests/conftest.py`（隔離替身）、`tests/support/`、`tests/docker/`、`tests/integration/test_isolation_linux.py` 與 `tests/unit`、`tests/lint` 的新模組（見 `tests/README.md`）
  - 文件：`AGENTS.md`（§2 隔離層級、§11「設定檔讀不到時不放寬」、禁令 66）、`SECURITY.md`、`PROXY_INTERFACE.md`、`CHAT_INTERFACE.md`、`SSOT_MASTER_MAP.md`、`NOQA_REGISTRY.md`、`tests/README.md`、`README_QUICKSTART.md`、`CONTRIBUTING.md`、`README.md`（「動態 MCP 調度」不是行程隔離）
  - C2-2 新增：kernel `framing.py`、`relay.py`、`owner_repair.py`（`spec.py`、`launcher.py` 改）；`services/isolation/` 的 `executor_server.py`、`executor_session.py`、`executor_client.py`、`block_ports.py`、`repair.py`（`status.py`、`runners.py`、`profiles.py`、`agent_command.py`、`evolution_command.py`、`consent.py` 改）；`utils/permission_repair.py` 與 `resource_manager.py` 的裝飾器；`child_env.py`（說明）；設定讀不到（`permission_service.py`、`autonomy.py`、`mcp_hub_service.py`、`utils/bootstrap.py`）；`GET /isolation` 與 `/health` 的 `system_notices`（`routers/observability.py`、`models/observability.py`、openapi、`api.ts`）；`models/manager.py`（`route_endpoints`、`reload_connections`）、`main.py`、`settings_snapshot_service.py`、`routers/system.py`；前端隔離橫幅、通用提示列與卡片文案；Dockerfile（git 層、預建 ipc 與 state）、三份 compose 的 `touka-executor`、`.env.example`、`.github/workflows/isolation.yml`、`scripts/ci/isolation_junit_check.py`、`tests/docker/` 端到端資產
- 修訂 ADR-0246：決策表多「隔離層級」一個維度；決定 4（YOLO 無人在場的 dangerous MCP 只有人能核准）；F5 多兩個演化舊別名；審查員的 `<environment>` 多 `isolation`
- 修訂 ADR-0239：子程序仍只經 ExecutionBroker 啟動，runner 只改寫啟動參數；exec 類呼叫只准出現在 `sandbox_kernel/launcher.py`（ratchet）；執行器伺服器也經 broker 啟動啟動器，ratchet 不放寬
- 修訂 ADR-0076：演化驗證的 rlimit 在隔離時由啟動器依 spec 套用，直接執行時仍由 preexec 套用；JailedWriter 照舊
- 修訂 ADR-0262：`child_python_env()` 回傳完整環境（含金鑰），隔離邊界（`sandbox_kernel/**`、`services/isolation/**`）不得使用它、`get_full_env` 或 `get_clean_env`（結構測試）
- 相關：ADR-0151（`host_root` 永不授權）、ADR-0254（演化走一般審批；MCP 啟動環境白名單。它的缺口 2「第三方 MCP 讀得到 `.env` 檔」寫「要等第二批」：本 ADR 沒有處理，MCP 伺服器的檔案隔離在 C2-3 的 ADR）、ADR-0255（`.touka` 目錄，見已知限制；C2-2 的自癒會修它的擁有者位元）、ADR-0069／禁令 52（`create_mcp`、`scaffold_mcp` 還沒接 runner；MCP 設定讀不到時建立在驗證關卡失敗）、ADR-0137（互審驗證指令）、ADR-0229（可信批准分類不變）、ADR-0019／禁令 47（Dockerfile 另加一段 COPY 與 git 層）、ADR-0214（Linux 隔離測試屬自動化沙盒測試）、ADR-0221（GHCR init 的四個角色不變，新 volume 靠映像 copy-up）、ADR-0208（路由快照只讀）、ADR-0253（提示列改成通用元件）、ADR-0270（記憶系統第二批第 7 段的方法列為自癒豁免、快照與記憶庫備份在白名單外）。這些 ADR 的決策都不變。

## 背景

- 合併前，代理人撰寫或指定的程式（終端 `execute_command`／`bash`、互審 `request_review` 的驗證指令、演化的建 venv、pip 與本機驗證）和 Touka 用同一個帳號、同一個容器執行：讀得到 `.env`（`/app/host_root`）、PID 1 的環境、設定、原始碼、整個 data（含其他代理人與靈魂），連得到 Touka 自己的埠與 Docker 遠端控制埠（有開的話），也能對 Touka 送信號。ADR-0254 只拿掉了環境變數裡的金鑰。
- 維護者核准的方向：決定 1 先用 Landlock、再加執行器容器（分兩步）；決定 2 沒有隔離時 Auto 與 YOLO 都只有人能核准、無人在場拒絕；決定 3 能上網但連不到 Touka 內部；決定 4 YOLO 無人在場時高風險 MCP 比照 Auto；決定 5 第三方 MCP 也隔離（由 C2-3 做，不在本 ADR）。另外選了多擋 Docker 遠端控制埠與改過的對外埠。
- 第二批分三段：C2-1 在 Touka 容器內用 Landlock 隔離代理人指令，加上 runner 與政策；C2-2 把代理人指令移到另一個容器（執行器），並修 C2-1 留下的封鎖埠、啟動器信任根、失敗代碼通道與設定讀不到的問題；C2-3 讓 MCP 伺服器也隔離。原計畫三段一個 PR；維護者 2026-10-16 決定改成 C2-1＋C2-2 一個 PR（本 ADR），C2-3 另開 PR、另一份 ADR、另做一次對抗式審查。理由：C2-3 的規模比原估大很多，而且要建立在 MCP SDK 2.2.0 上（2.0.0 驗證結構化內容時會去抓 schema 指定的外部網址或檔案，GHSA-rwrf-2pqf-9j8j；被隔離的伺服器可以藉 Touka 讀檔或連線）；已審過的兩段先進 dev，審查量與衝突都變小。代價：C2-3 合併前，MCP 伺服器和這個 PR 之前一樣沒有隔離（不比合併前差）。
- 開工探測（維護者的測試環境：Docker Desktop，核心 6.18、Landlock ABI 7）：data 掛載是 9p（drvfs `cache=0x5`），不 pin 時 9p 上的授權每次都失效（0/100），pin 模式 100/100；ext4 兩種都正常。

## 決策

### 隔離層級 SSOT

- 層級 `none`／`landlock`／`executor`（`executor` 見 C2-2）。常數與純函式在 `utils/isolation_policy.py`：要隔離的工具 `ISOLATION_REQUIRED_TOOLS`、真的接上 runner 的動作 `ISOLATION_WIRED_ACTIONS`、`normalize_isolation`（缺值或看不懂一律 `none`）、feature 對照、`TOUKA_TOPLEVEL_PROTECTED`。全是程式常數，設定檔改不鬆。
- 判定只在 `services/isolation/status.py` 的 `IsolationMonitor`，分兩個狀態：
  - `local_landlock`：用 kernel 目錄的啟動器做自測（pin 模式、在 data 所在的檔案系統上），`pending`／`available(abi)`／`unavailable(reason)`。
  - `level`：Linux 才預期 `landlock`（原生 Windows 一律 `none`／`native_platform`）；要 `local_landlock` 可用、ABI≥4、映像內 kernel 和原始碼雜湊相同、sandbox state 有設而且寫得進去，才判 `landlock`，否則 `none` 附原因。讀不到 kernel 時是 `isolation_check_failed`，讀得到後自動恢復（不用 `kernel_outdated`：它要使用者重建，9p 暫時 EIO 時這個指引是錯的）。`TOUKA_EXECUTOR_DIR` 有值時預期是執行器層，判定見 C2-2「層級判定與降級去處」。
  - 時序：第一次有工具呼叫時才開始探測，等自測最多 5 秒、健康檢查最多 2 秒，逾時照實判 `none`；成功結果快取 10 秒、失敗 1 秒；開機 60 秒內每 2 秒、低於預期每 30 秒、達到預期每 60 秒重查；背景重探一輪出錯只記 log、照低於預期的間隔繼續；本機 Landlock 不可用、或可用但網路設定不安全時，每 10 分鐘重做自測（網路原因在 ST 上重做，設定改好就回到 `landlock`）；因 9p 失敗時要通過一次 9p 自測才恢復（網路原因不走這條回復路徑）。
  - 收到啟動器失敗標記時請求重探、不直接改層級：只認 `FAILURE_CODES`（假代碼不算），不分原因 60 秒內最多一次。設計寫「同一原因 60 秒」，但指令自己印出代碼就能每個代碼各觸發一次，所以改成全域。C2-2 起只認獨立的狀態 fd，指令印出的代碼不算。
  - 自測資料夾是 `.touka-selftest-<隨機>`（data 的 `mcp_servers` 或 ST 的 `proxy` 底下），自測前清掉同前綴的殘留（只看直接子項、不跟連結、清不掉就留著），啟動器的 fixture 也是隨機名稱，上次被打斷留下的不會撞名；取消時先殺掉自測的啟動器並等它結束（只殺它本身：它不是新行程群組，killpg 會殺到 proxy）。
- 每次工具呼叫由 `services/isolation/policy.py` 的 `policy_level` 決定政策層級：專案根只來自 ctx 或 session 綁定的專案（`session_project_root`），不讀模型給的參數；專案放不進隔離時是 `none`；判定本身出錯時記 log、當 `none`／`isolation_check_failed`。隔離判定與同意帳本只看內建工具：MCP 等外部工具和隔離動作同名（例如叫 `verify`）也照不需記同意的工具處理（全域層級、不記帳本）。
- **沒接隔離的工具一律未隔離**：`ISOLATION_WIRED_ACTIONS`＝終端兩個名稱、`verify`、演化三個工具。要隔離但不在清單裡的（`create_mcp`、`scaffold_mcp`）一律 `none`／`runner_not_wired`。新增動作必須在同一個 PR 接上 runner，不能只改清單。這兩個工具的審批卡依工具判斷（`ISOLATION_UNWIRED_TOOLS`），一律 `fallback: direct`、features 空，不看帳本與原因（判定出錯時也一樣）；它們執行時不再核對同意、卡片是唯一關卡，所以帳本作廢或過期時核准不算數、不執行。各模式：Manual 照舊跳卡；Auto 本來就因底線 F5 只有人能核准；YOLO 從「直接執行」改成只有人能核准（層級 `none`），無人在場拒絕。核准後以 Touka 的權限執行：建立流程的試跑（SMOKE）會以淨環境啟動候選 MCP 伺服器（看不到金鑰類環境變數，ADR-0254），但沒有檔案隔離；建好的伺服器之後也是未隔離地跑（C2-3 之前）。

### Landlock 啟動器與 `/opt` 信任根

- 啟動器只用標準函式庫，以 `python -I -S <kernel>/launcher.py --spec-fd N -- argv` 執行。spec 經繼承的 pipe 傳（不放 argv，避開 128 KiB 單一參數上限），有版本號、上限 1 MiB、多出或重複的鍵一律拒絕。第 1 版加選用欄位 `parent_pid`（0＝不檢查）而不升版。理由（第 2 次複審後的補修正更正；原本寫「不同版本會被雜湊比對擋下」不成立：雜湊比對只擋政策層級那條路，人核准降級後，容器裡的 runner 不比對雜湊，照樣把 spec 交給 `/opt` 的啟動器）：舊 proxy 配新啟動器時，spec 沒有這個鍵，新啟動器當 0、照常執行，向後相容；新 proxy 配舊啟動器時（開發版 pull 之後還沒重建，`kernel_outdated`），舊啟動器的 `_TOP_KEYS` 沒有 `parent_pid`，以 `spec_invalid` 拒絕、指令不執行——升版也只是把代碼換成 `spec_version`，一樣不執行、不會變成沒隔離地執行，所以升版沒有安全上的差別，只會讓既有 spec 測試全部跟著改。之後 spec 每加一個欄位都會在這個空窗重演，所以 `kernel_outdated`（與讀不到 kernel）時核准後一律未隔離，卡片照實寫「未隔離」，原因那行寫要重建（見「降級要先經人核准」）。
- 映像在 `COPY . .` 之後**另加**一段把 `sandbox_kernel` 複製到 `/opt/touka-sandbox`（root 擁有、所有人唯讀），`ENV TOUKA_SANDBOX_KERNEL_DIR` 指向它。這不是白名單式 COPY，禁令 47 照舊。容器內只從這裡載入；不存在 → `kernel_not_installed`；開發版掛進來的原始碼和它雜湊不同 → `kernel_outdated`。所以核准過一次未隔離的指令也改不了下一次用的啟動器。原生 Linux 沒有唯讀副本，從原始碼載入，每次 spawn 前比對雜湊，不同就這次拒絕並重探。
- 啟動步驟：`NO_NEW_PRIVS`、`PDEATHSIG(SIGKILL)` 後比對 `getppid()` 和 spec 的 `parent_pid`（runner 帶自己的 pid；不符表示 spawn 端在 PDEATHSIG 生效前就結束，`process_setup`、不執行）、`oom_score_adj` 500、`umask` 022 → 查 ABI（<3 失敗、>7 當 7）→ **路徑層**：每條規則從 `/` 逐段 `O_PATH|O_NOFOLLOW` 開啟，依實際檔案型別給權利，ABI≥6 加上信號與抽象 unix socket 的 scope → **網路層最後套** → `setrlimit` → 關掉 stdio 與 pin 以外的 fd → `execvp`。失敗時 exit 125、stderr 一行 `TOUKA_SANDBOX_ERROR:<code>`（C2-2 起只給人看，代碼另寫進 `--status-fd`）。
- runner（`services/isolation/runners.py`）只改寫 argv、env、`pass_fds`，spawn 仍由呼叫端經 ExecutionBroker；spawn 前才 `acquire`，spawn 後（成功或失敗）`plan.release()`；所有 spawn `close_fds=True`、stdin 一律 `DEVNULL`（終端、驗證指令、演化、自測；原生 Linux 的 TTY 不會被 TIOCSTI 注入）。ratchet 讓 exec／fork／spawn 類呼叫只准出現在啟動器。

### pin 模式

預設 pin：規則路徑的 O_PATH fd 用 `F_DUPFD` 搬到 100 以上（不設 CLOEXEC，搬不動就 `process_setup`）帶過 exec，讓 9p 的 inode 一直被引用；shell 的 `exec 12>檔` 蓋不到。這些 fd 只能定位、不能讀內容，經 `/proc/self/fd` 重開仍要過 Landlock。原因是 Docker Desktop 的 data 掛載用完 dentry 就丟、下一次 lookup 配新 inode，規則綁在舊 inode 上。限制見「已知限制」。

### 路徑層先、網路層最後

網路規則是「允許清單」（擋 B 就要允許其餘約 65,000 個埠）。先路徑層、最後網路層，每條指令的核心記憶體約 4.7 MiB（反過來約 9.2 MiB，探測量測），封鎖效果相同。proxy 本機沒有共用網路規則集（介面已留，proxy 不提供；執行器有，見 C2-2），每條指令由啟動器自己建，約 0.1 秒【推測，依探測 84～91 ms】。

### Profile 與永不授權

- 代理人指令（`build_agent_command_profile`）：系統路徑唯讀可執行；`config/skills` 唯讀可執行；自己的資料夾（`personal_drive/<id>` 或 `custom/<id>/drive`）、sandbox state 底下自己的 `home`、`tmp`、可授權的專案根給讀寫與執行；cwd 與專案根每一層上層的九個公開設定檔唯讀（一般檔、`st_nlink==1`、不在 data、sandbox state、受保護的 `.env` 資料夾 E 底下，`.env*` 永遠不在內）。環境變數：HOME、TMP、XDG 指到 sandbox state；git `safe.directory=*` 加 `GIT_CEILING_DIRECTORIES`；拿掉 HF／uv／npm／pip 快取變數。`safe.directory=*` 是小放寬：讓 git 在擁有者不同的專案裡照常運作，能讀到的仍只有授權的資料夾。
- 演化（`build_evolution_profile`）：讀寫只給 `mcp_servers/managed/<skill>` 與 sandbox state 的 `proxy/evolution/<skill>`。
- 推導路徑（專案根）任何一段是符號連結或 junction（`absolute()` 和 `resolve()` 不同）一律 `protected_location`：連結可以被代理人改寫成指向別人的資料夾。其餘 resolve 後判斷，啟動器逐段不跟符號連結。順序：
  1. 可給根目錄 G 只有呼叫者自己的 `personal_drive/<id>`（依已驗證身分；共用的 `temp/docgen_output` 不給），直接放行；
  2. 固定受保護 P（data 根、設定、Proxy／Memory／Dispatch／Heartbeat、`backups`、`.env`、kernel、執行器目錄、sandbox state 根，以及不是 data 上層時的 E）與它們的上層、鏡像根 M 本身（工作區根、`host_root`）一律拒絕；容器裡安裝根底下除了工作區掛載的子資料夾全部拒絕；
  3. 其餘做 Touka 目錄偵測（`utils/touka_dirs.py`、`services/isolation/project_scan.py`）：往上每次都查到最近的 M；往下 3 層用 dir fd `O_DIRECTORY|O_NOFOLLOW` 掃。Touka 目錄＝有標記（Proxy／Memory／Heartbeat／Dispatch 資料夾、`heartbeat-config`、`docker-compose.ghcr.yml`）、有 `soul`、`personal_drive`、`ui_sessions.db` 任一個（資料根形狀；`ui_sessions.db` 是啟動時一定會建的，`legacy` 用到才建、名稱也常見，不列），或本身是 E、data 根、安裝根。Touka 目錄底下只有使用者自建的頂層資料夾能授權（不在 `TOUKA_TOPLEVEL_PROTECTED`、不是點開頭、不是檔案、本身不是 Touka 目錄）；E、data 根與資料根形狀底下沒有例外。名稱 NFC＋casefold 比對。原生模式 repo 裡的專案一律拒絕。
  - 掃描：前景最多等 2 秒，背景 30 秒、50,000 個資料夾、3 層，同一個根只掃一次。結果依根目錄快取 10 分鐘：每次都列第一層的真資料夾（名稱＋inode），根目錄 mtime 變了但第一層沒有新增或同名換掉的資料夾就沿用，有新增只補掃那些（補掃不延長 TTL），已拒絕的判定到 TTL 前一律維持。原因碼 `protected_location`、`scan_limit`。
  - `TOUKA_TOPLEVEL_PROTECTED` 由測試和 `git ls-files`、`.gitignore` 雙向同步：之後任何一線增刪 repo 頂層項目，都要同步改這份清單。

### 網路

- 封鎖 TCP connect 與 bind 的埠集合 B＝8000、8082、8083、3001、2375、2376，加上設定改過的埠（`PORT`、`DISPATCH_PORT`、`PROXY_PORT`、`HEARTBEAT_PORT`、`CHAT_PORT`、心跳與 UI 網址的埠），有設 SearXNG 時加它的埠。同一份規則集處理兩種權利，允許 0～65535 扣掉 B（埠 0 留給暫時埠）；代理人搶不到這些埠，自測多一項「bind 封鎖埠要 EACCES」。看的是埠號、不看目的主機。ABI≥4 才擋得住；ABI≥6 另外隔開信號與抽象 unix socket。實測讀不到 `/proc/1` 與父行程的 environ。
- Landlock 的 TCP 規則只管 connect 與 bind：TCP Fast Open（`sendto` 帶 `MSG_FASTOPEN`）與 MPTCP socket 的連線可能不經過它。這次**沒有實測繞過，直接當作能繞過來防**：原定在維護者測試環境的 Docker 實測，但實測程式沒有完成。做法：三份 compose 的 proxy 設 `net.ipv4.tcp_fastopen: 0`；自測在 ABI≥4（有 TCP 規則）時只讀 `/proc/sys/net/ipv4/tcp_fastopen` 與 `/proc/sys/net/mptcp/enabled`、不建 socket——Fast Open 的 client 位元開著、讀不到或看不懂是 `net_tcp_fastopen`，MPTCP 檔案存在而且不是 0 是 `net_mptcp`。讀的是 proxy 自己的網路命名空間，和代理人指令同一個。
- 讀到不安全時比照 ABI 3（第 2 次複審後的補修正，審查者③ N2）：自測照樣跑完檔案、bind 與一般 TCP 的檢查，網路問題另外回報在結果的 `net`，自測本身算通過；monitor 判本機 Landlock 可用、features 不含 `tcp`；層級仍是 `none`、原因是 `net_tcp_fastopen`／`net_mptcp`，所以 Auto／YOLO 照舊只有人能核准（HUMAN_ONLY 不變）。人核准後 `fallback` 是 `landlock`（features 不含 `tcp`），卡片寫「隔離降級」、說擋不住它連到 Touka 內部服務，原因那行寫要加的 compose 覆寫（原生 Linux 是主機的 sysctl）；實際執行時一般 TCP 的封鎖規則照樣套用（有比沒有好），只是擋不住這兩種連線。設定改好後背景重探（每 10 分鐘，在 ST 上）自動回到 `landlock`；網路原因不套用 9p 的回復路徑。第 2 次複審時原本的做法是整個本機 Landlock 判不可用，人核准後完全未隔離，比修正前還弱，所以改掉（見「考慮過、不採用的做法」）。
- MPTCP 的代價：compose 不能預設加 `net.mptcp.enabled: 0`，沒有 MPTCP 的核心加了，容器起不來（維護者測試環境的 WSL2 核心實測 exit 127）。有 MPTCP 的核心要在 compose 覆寫的 touka-proxy 自己加這行（GHCR 跑法的每一段指令都要另加 `-f <覆寫檔>`，見 README_QUICKSTART），否則層級是 `none`、每條指令都要人核准，核准後是「隔離降級」。原生 Linux 要改的是主機的 sysctl（`net.ipv4.tcp_fastopen=0`，有 MPTCP 時再 `net.mptcp.enabled=0`，寫進 `/etc/sysctl.d` 才會在重開機後保留）；不經出貨的 compose、直接 `docker run` 映像時 Fast Open 是開的，判 `none`。
- compose 帶 `sysctls` 的代價：proxy 改成 `network_mode: host` 的部署會起不來（Docker 不准在主機的網路命名空間設網路 sysctl），`tcp_fastopen` 還沒分網路命名空間的舊核心（4.12 以前）也是；以前這些環境 Touka 照樣啟動（只是沒隔離）。README_QUICKSTART 寫明，要用就自己拿掉 `sysctls`、接受判 `none`。

### Sandbox state（ST）

- named volume `touka-sandbox-state` 掛在 `/app/sandbox_state`（`TOUKA_SANDBOX_STATE_DIR`），三份 compose 都接；不在 data 裡、不進備份。映像預建成 appuser、0700（全新 volume 第一次掛載時照映像複製擁有者，已實測）；開發版 init 只對根目錄 chown／chmod，不遞迴。
- 版面 `proxy/{agents,evolution}/<id>/{home,tmp}`；ST 根受保護。沒設或寫不進去時判 `sandbox_state_unconfigured`（細節 `unset`／`not_writable`），程式不會崩。原生 Linux 用 data 暫存區的 `sandbox_state`。執行器有自己的 volume（C2-2）。

### 降級要先經人核准：同意帳本與 `isolation_ack`

- 決定 2：要隔離的動作在層級 `none`（缺值、看不懂也算）時，Auto、YOLO 改成 HUMAN_ONLY（`AuthPoliciesConfig.evaluate` 的最後一步，程式常數）；審查員與 YOLO 都不能放行，無人在場拒絕；Manual 照舊；DENY 不放寬；底線已是 HUMAN_ONLY 時保留底線的理由。
- 同意帳本（`services/isolation/consent.py`，記憶體）：政策評估時以（代理人、對話、call_id）記下層級與 subject（正規化參數的 canonical JSON 的 sha256，例如終端是指令加實際 shell）；人核准層級 `none` 的卡時，請求的 `payload.isolation_ack` 必須等於卡上的降級去處才記同意（端點 422；`approval_flow` 再核一次，不符就是 `approval_isolation_unacknowledged`）；執行前用自己的參數重算 subject 取回。只能用一次、10 分鐘過期、同一個鍵出現不同 subject 兩筆都作廢、同一個鍵重新記錄時清掉舊的同意、最多 4,096 筆。
- `RunnerBroker.acquire`：政策層級不是 `none` 時只能用同一層級，層級掉了就拒絕；`none` 有同意時用當下最強、不弱於卡片的方式（本機 Landlock 與 ST 都可用、kernel 雜湊一致〔`kernel_outdated` 或讀不到 kernel 時舊啟動器會拒絕新的 spec 鍵，一律當用不了〕、需要的路徑都能授權就用 Landlock；卡片寫「未隔離」才直接執行；卡片寫「隔離降級」但現在用不了就拒絕）；`none` 沒同意一律拒絕，回 `upstream_error`、原因 `isolation_unavailable`。終端的舊 ToukaUI 審批通道標不出隔離，`none` 時直接拒絕。profile 組不出來（ST 沒設或寫不進去）時，只有同意是 `direct` 才改用不需要 ST、標成不完整的 profile（broker 只會給 `DirectRunner`），所以沒重建時人核准「未隔離」就能跑；其他照舊拒絕，`isolation_reason` 帶真正原因 `sandbox_state_unconfigured`。
- 有隔離不放寬任何規則：審查員看得到層級，prompt 寫明只供參考。審查員看的層級由 `reviewer_level` 給：只有經 runner 的內建工具（`ISOLATION_WIRED_ACTIONS`）給政策層級，其餘（含所有 MCP 工具）一律 `none`，決策表的層級不變；給 `none` 而不拿掉欄位，因為 `ReviewRequest` 的預設就是 `none`，也和缺值當 `none` 一致。

### 決策表與決定 4

- YOLO 下 `mcp_server` 的 dangerous 工具在 `human_present` 不是 `True` 時改 HUMAN_ONLY（＝拒絕）。`isolation` 與 `human_present` 由 `tool_executor` 依身分、ctx 放進 `PolicyContext.environment`，模型參數改不動。
- F5 加 `write_mcp_implementation`、`verify_mcp_locally`。`classify_command`、`get_execution_strategy` 收 `isolation`；終端傳 grant 的層級，驗證指令用有看專案的 `command_level`；沒給時取代理人層級。

### ABI 對照

landlock 層最低 ABI 4（要擋 TCP），executor 層最低 ABI 3（ABI 3 時埠沒擋，見 C2-2）；features：fs 3、tcp 4、ioctl 5、信號與抽象 socket 6。`VERIFIED_ABI_MAX`＝7 是刻意保守：更新的 ABI 新增的權利驗證前不用。核心版本對照表見 `PROXY_INTERFACE.md` 與 `README_QUICKSTART.md`。

### pytest 以專案根為 rootdir

- 問題：專案在 Touka checkout 裡、自己沒有 pytest 設定時，pytest 以上層 `pytest.ini` 的位置為 rootdir 往下列資料夾，Landlock 沒給上層 READ_DIR 就中止。
- 不採用「上層逐層給 READ_DIR」：權利往下繼承，等於能列出整個 checkout（含 data、soul 的檔名）。
- 採用：代理人指令 profile 設 `PYTEST_ADDOPTS="--rootdir=<專案根或 cwd> --confcutdir=<同>"`（`shlex.quote`），指令原本的 `PYTEST_ADDOPTS` 接在後面，指令列自己給的選項仍最後生效。上層的 `pytest.ini` 照樣讀（公開設定檔），上層的 `conftest.py` 不載入。Docker 突變證實兩個選項都要。權限規則完全沒動。演化 profile 不設。只有 pytest 根的上層真的授權到公開設定檔時才注入（Touka checkout 裡的專案）；否則不注入，資料夾裡子專案自己的 `testpaths` 照常（Docker 裡代理人資料夾的上層在 data 底下，不會授權）。

### 演化接上 runner

合併工具與兩個舊別名的建 venv、pip、本機驗證都經 runner：工具開頭取一次 grant，同一次呼叫共用；用 `_evolution_grant` 物件交給舊別名（模型只能給 JSON，偽造不了）；spawn 在 `to_thread` 的 worker 裡做（PDEATHSIG 綁在建立子行程的執行緒）；拿不到 runner 回結構化錯誤、不往下驗證也不部署。RLIMIT、JailedWriter、淨化環境、部署卡、無人在場拒絕照舊。

## 決策（C2-2：執行器容器）

### 執行器

- 架構是「proxy 端 relay＋執行器伺服器」：終端、驗證指令、演化三個呼叫點的呼叫方式不變，`RunnerBroker` 多一種 `ExecutorRunner`。它的 argv 是本機 relay 子行程（`<可信直譯器> -I -S <kernel>/relay.py --ipc-dir D --request-fd R --status-fd S --boot-id B`，沒有權杖也沒有指令），指令、cwd、env、spec 都在經 pipe 傳的 START 裡；所以 killpg、逾時、背景停止、逐行串流、stdin＝DEVNULL 都沿用 C2-1。
- 伺服器放 `services/isolation/`（`executor_server.py` 管啟動與連線、`executor_session.py` 一條連線一條指令），經 `execution_broker` 啟動啟動器，ratchet 不放寬。只准標準函式庫、`execution_broker` 與彼此；kernel 從 `--kernel-dir` 依路徑載入（具名的 `Kernel` 與 Protocol，執行時核對成員），不讀環境變數、不載入 `src.config`；`tests/lint/test_executor_server_imports.py` 執法，也禁止 `Any`／SimpleNamespace。伺服器版本靠 READY 的 `server_digest`：`EXECUTOR_CODE_FILES`（伺服器、session、broker）的雜湊，兩邊共用 `executor_code_digest`。
- 容器 `touka-executor`：和 proxy 同一個映像，`python -m src.services.isolation.executor_server --ipc-dir /app/ipc/executor --kernel-dir /opt/touka-sandbox --state-dir /app/executor_state --root rw:/app/data/personal_drive --root rw:/app/data/mcp_servers --root ro:/app/ToukaProxy/config/skills --require-foreign-peer`；`user 1001:1001`、`init`、唯讀 rootfs、`/tmp` tmpfs、`cap_drop: ALL`、`no-new-privileges`，資源上限 `TOUKA_EXECUTOR_MEM_LIMIT`／`_PIDS_LIMIT`／`_CPUS`（預設 2g／512／2，全部代理人共用）。不帶 `env_file`、沒有對外埠，只掛兩個 data 子資料夾、skills（唯讀）、ipc 與自己的 state，只接 `touka-exec-net`（一般 bridge、能上網，和 `touka-net` 分開）；`sysctls` 只有 `net.ipv4.tcp_fastopen: 0`；healthcheck 要 socket 在、而且 `alive` 一分鐘內更新過，`start_period` 70 秒（涵蓋兩次自測）。proxy 不 `depends_on` 執行器，靠背景重探。
- 啟動（任何一步出錯就**保持存活、不服務**：不建新的 socket、記 log、不退出，避免重啟循環；healthcheck 通常失敗，例外見已知限制第 21 點）：設定其他項都對時，先收回 state 與 ipc 兩個私有資料夾（見「執行器修自己的私有資料夾」）→ ipc 必須是自己的私有資料夾，拿到 `flock` 才刪上次留下的 socket 與 `alive` → 驗證設定（roots 存在、不經連結、不重複、不巢狀，第一層名稱用 kernel 的 `listing_kind` 判定、是 Touka 目錄或資料根形狀就不服務；socket 路徑 ≤100 bytes；state 是自己的、0700；可信直譯器與兩個 digest 算得出）→ 補擁有者位元 → 清自己的 tmp 與殘留自測資料夾（最多 4,096 個 owner、不跟連結）→ 在 data 的 9p 與 state 上各做一次自測（前綴 `.touka-executor-selftest-`，失敗時每 10 分鐘重做，9p 失敗要通過 9p 自測才恢復）→ `PR_SET_CHILD_SUBREAPER` → umask 讓 socket 是 0600 → 寫新權杖與 `alive`（mkstemp＋replace）→ listen；之後每 5 秒更新 `alive` 並收回私有 inode。背景工作（`alive` 與 ipc 修復、收孤兒、自測重試）遇到任何例外都記 log、下一輪照常，只有取消會停（第 3 次複審 F2；原本只接 `OSError`，開不了執行緒的 `RuntimeError` 會讓那條工作永久停掉）。
- START 驗證（全部 fail-closed）：argv 1～4,096 項、沒有 NUL、總長 ≤512 KiB；env 只收 kernel 的精確名稱允許清單 `EXECUTOR_ENV_ALLOWED`，`GIT_CONFIG_*` 只收 profile 那一組（`safe.directory=*`），不收 `TOUKA_EXEC_ID`，總長 ≤64 KiB；spec 先過 kernel 的 `parse_spec`，讀寫規則只能是這次的目標（`agents`→`<personal_drive>/<owner>`、`evolution`→`<mcp_servers>/managed/<owner>`）與 `state/<kind>/<owner>/{home,tmp}`，其他規則只能是系統路徑或唯讀 root，cwd 必須正好是目標；spec 的封鎖埠 ⊇ HELLO 的 ⊇ `DEFAULT_BLOCK_PORTS`、≤1,024；`max_seconds` 1～900（proxy 送本機逾時＋30 秒）；`io` 只收 `merged`。伺服器把 spec 的 `parent_pid` 改成自己的、建 `home`／`tmp`（0700），以新行程群組啟動 `<可信直譯器> -I -S <kernel>/launcher.py`，只傳 spec、狀態、規則集三個 fd，環境加新的 `TOUKA_EXEC_ID`。
- EXIT 之前連線斷掉就是 STOP（relay 被殺或收到 SIGTERM 都不另送）：殺掉整個行程群組並有界回收；STOP 與 `max_seconds` 到期也殺，但照樣回 EXIT。
- 共用網路規則集：以 spec 的 `block_ports` 為鍵，最多快取 2 份（B 只增不減，不設上限會一直累積）；借出時在快取鎖裡先 `dup`、spawn 完關掉，淘汰不會關到正在用的 fd。
- subreaper：只對特定 pid 呼叫 `waitpid`（不搶 asyncio 的子行程）；每條連線結束與每 30 秒掃 `/proc`，`pidfd_open` 釘住後看 `environ` 的 `TOUKA_EXEC_ID` 與 stat 的 sid，屬於仍在執行的指令就保留，其他殺掉並回收（最多 8 輪，和 spawn 同一把鎖）。
- 刪除靈魂時清家目錄（2026-10-21 補記，見已知限制 26）：`RunnerBroker.acquire_maintenance` 只給 `home_wipe` 的固定清除指令用（argv、env 由 Touka 固定，不是代理人撰寫或指定的程式），所以不需要同意；只用可用的執行器（只差網路設定也行，固定指令不連網），不退回 Landlock 或直接執行，不能用就擋下刪除。呼叫者由 `test_soul_delete_sandbox_home_security.py` 的 AST 測試限定。

### 協定與控制通道認證

- frame（SSOT 是 kernel 的 `framing.py`，proxy、relay、伺服器共用）：1 位元組類型＋4 位元組長度（big-endian）＋payload；控制 JSON 64 KiB、START 1 MiB、資料 1 MiB，看到標頭就檢查上限；看不懂的類型、截斷、重複鍵、NaN、不是物件 → ERROR（`protocol`／`auth`／`version`／`rejected`）後關閉。一條連線一條指令：HELLO → READY → START → STARTED → OUT／ERR → EXIT；`mode: "health"` 回完 READY 就關。
- 控制通道：volume `touka-exec-ipc` 掛在 `/app/ipc/executor`（proxy 與執行器共用），裡面是 `ctl.sock`、`token`、`alive`，資料夾 0700、socket 與權杖 0600、擁有者 1001。權杖是 `secrets.token_hex(32)`、每次啟動換新；proxy 與 relay 每次連線都從檔案重讀（不跟連結），argv、環境變數與 log 都沒有權杖，執行器重啟後自動恢復。
- accept 時**先**查 `SO_PEERCRED`：設了 `--require-foreign-peer` 而 peer 的 pid 不是 0（同一個 pid namespace，也就是執行器裡的程式）就馬上關閉、不計入上限；再算連線上限 256 與未驗證上限 8；HELLO 要在 2 秒內到；權杖空的、格式不對或 `hmac.compare_digest` 不符 → `auth`；版本不同 → `version`。
- 隔離內的程式連不進來：Landlock 的規則沒有 ipc，讀不到權杖；執行器裡的連線 pid 不是 0，被 peer 檢查擋掉；proxy 本機降級的指令也讀不到權杖。維護者測試環境的 Docker 實測：proxy 形狀的用戶端連得上（peer pid 是 0）；執行器裡的指令讀權杖得到 `PermissionError`、連 socket 被斷開。

### HELLO／READY 合約與 `block_ports_digest` SSOT

- HELLO `{v, token, block_ports, mode}`。health 模式送 monitor 這次實際要用的那份 B（溢位時是核心 B，所以溢位時健康檢查照樣通過）；exec 模式由 relay 送 request 裡 spec 的 `block_ports`（和 START 同一個物件，relay 不另外算），所以「START 的 B ⊇ HELLO 的 B」一定成立。
- READY 的必要鍵是 `v, abi, enforced, net, features, roots, state_root, kernel_digest, server_digest, boot_id, block_ports_digest, uid`，只容許多一個 `repairs`（自癒計數）。格式由 `executor_client.parse_ready` 驗、`ready_issue` 和預期逐項核對；伺服器端的合約測試直接呼叫這兩個函式。
- `block_ports_digest` 的唯一算法是 `sandbox_kernel/framing.block_ports_digest`（排序、去重、緊湊 JSON 的 sha256）：伺服器在 READY 回報，proxy 核對。
- relay：設 PDEATHSIG 並核對 `parent_pid`；READY 的 `boot_id` 和判定時不同 → `executor_restarted`、不送 START；握手上限 30 秒；每收到一塊就寫出；EXIT 帶遠端的 code（被信號殺掉是 128＋signal）；連不上或 EXIT 前斷線 → `executor_unreachable`；權杖讀不到或被拒 → `executor_auth_failed`；START 被拒 → `executor_rejected`。

### 層級判定與降級去處

- 預期是執行器（`TOUKA_EXECUTOR_DIR` 有值；只在 Linux 做健康檢查）時，`executor_client.check_health`（2 秒）取得 READY，下面全部成立才判 `executor`：連得上（否則 `executor_unreachable`，健康檢查本身丟例外也算）、權杖正確（`executor_auth_failed`）、READY 格式對而且版本相同（`executor_version_mismatch`）、`kernel_digest` 等於原始碼的（`kernel_outdated`）、`server_digest` 與 `block_ports_digest` 相同（`executor_version_mismatch`）、roots 和預期完全相同（`D/personal_drive` rw、`D/mcp_servers` rw、`C/skills` ro，否則 `executor_roots_mismatch`）、ABI≥3（`landlock_abi_too_old`）、`enforced`（`executor_selftest_failed`）；ABI≥4 時網路設定也要安全、封鎖埠不能擋不全（見下一節）。第一次健康檢查完成前是 `probe_pending`。成功快取 10 秒、失敗 1 秒；`boot_id` 變了一定通知。relay 經狀態 fd 回報的代碼（`RELAY_FAILURE_CODES`）讓 monitor 先照那個原因判 `none`，直到下一次健康檢查（只會更嚴）。
- `refresh()` 分兩段：磁碟 I/O（版面、kernel 雜湊、ST、兩個設定檔、定期自癒）在執行緒裡，合併與判定在事件迴圈上；同時只跑一次；映像裡的 kernel 只雜湊一次；原生 Windows 不做健康檢查與掃描。開機自測丟出任何例外都算失敗，資料所在的 9p 沒驗證過之前，背景一律做 9p 自測。
- `level_for`：層級是 executor 時，代理人資料夾必須在可寫的 root 裡（`custom/<id>/drive` 的代理人，例如 DebugAgent，判 `none`／`agent_dir_not_in_executor`），也不能綁專案（這批不掛工作區：`none`／`project_not_in_executor`）。原因排序：本機讓降級只能「未隔離」的原因 → 不在執行器範圍 → 執行器與網路原因。
- 降級去處（`fallback_for`，依序取當下最強的）：這次在執行器範圍內，而且執行器可用、只差網路設定或封鎖埠 → `executor`（features 不含 tcp）；否則本機 Landlock、ST、kernel 都正常、profile 完整 → `landlock`；否則 `direct`。卡片的去處在政策時就算好（`executor_eligible` 記進帳本），核准時一併記下卡片的 features。
- `RunnerBroker.acquire`：政策是 executor 時，snapshot 必須仍是 executor、這次在範圍內、READY 還在，否則拒絕，**不退回 Landlock**；有同意時，在「種類不低於卡片（executor＞landlock＞direct）、features 包含卡片的」runner 裡取當下最強的，都不符合就拒絕。profile 在選好位置之後才建（`ProfileSource`）：執行器位置的 HOME／TMP 在 READY 的 `state_root/<kind>/<owner>`（伺服器建，proxy 不建），只給目標、系統路徑與唯讀 root，不給公開設定檔與專案。
- 審批卡：`CardIsolation` 多 `expected`，有值才帶 `net`（降級去處那一層的網路設定問題）、`block_ports_issue`、`local_issue`；`fallback` 可以是 `executor`，`isolation_ack` 照卡片。主句看去處（在執行器裡、在 Touka 主容器內以檔案隔離、未隔離），開頭的原因子句寫決定去處的那個原因（不在範圍、執行器不能用、網路設定、讀不到連線設定、要封鎖的埠太多），結尾依 features 與原因寫擋不住什麼；原因那幾行都附補救方式，網路設定的覆寫要加在哪幾個服務依 `expected`。

### 網路設定不安全、`block_ports_incomplete` 與 `block_ports_overflow`

- 網路設定不安全（ABI≥4 而 Fast Open 或 MPTCP 開著）時，本機 Landlock 與執行器都判 `none`，Auto／YOLO 只有人能核准；核准後執行器優先，features 不含 tcp，擋不住 Fast Open／MPTCP 的連線。ABI 3 時本機 Landlock 判 `none`（同 C2-1），執行器判 `executor`、features 只有 `fs`，橫幅標示埠沒擋。理由：網路設定加一行覆寫就修得好，ABI 3 要升級核心才修得好。
- `block_ports_incomplete`（`connections.json` 還沒讀成功過或讀不到）與 `block_ports_overflow`（完整 B 超過 1,024）用同一套：判 `none`；降級去處仍是執行器或本機 Landlock，features 不含 tcp、`net_strict` 是 false，**不再降到未隔離**；B 用「核心 B ∪ 已知的上游埠」（讀不到）或只用核心 B（溢位）。溢位不論在 refresh 或組 profile 時發現，都會改層級並通知。
- 每次啟動讀網路設定：spec 的 `net_strict`（預設 true）等於「runner 的 features 含 tcp」；為真時啟動器在套限制前讀兩個 sysctl，不安全就以 `net_*` 拒絕執行。只有人核准、features 不含 tcp 的降級送 false。

### 封鎖埠 B 從連線推導與路由快照

- B 由 monitor 算（`IsolationMonitor.block_ports(location)`；組成的 SSOT 是 `block_ports.py`），profile、健康檢查、卡片共用一份，proxy 與執行器兩個位置各一份。核心 B＝`DEFAULT_BLOCK_PORTS`（搬進 kernel `spec.py`）、Settings 裡 Touka 自己的埠（SearXNG 只在 proxy 位置）與 Settings 的上游 URL（`SETTINGS_UPSTREAM_URLS`）。已知上游埠＝`connections.json` 全部條目的 `url`（不看 enabled、不看探測結果）、MCP registry（`server_config.json`）全部條目的遠端 url、`ModelManager.route_endpoints()`（設定裡全部連線加目前路由；唯讀，每次回新的 frozenset），行程存活期間**只增不減**。Settings 每個 `*_URL` 欄位、`route_url_attr` 都要在上游清單或明列的排除清單（結構測試）。
- 主機是「非公網」才取埠：IP 用 `not is_global`（也認 `127.1` 這類寫法），主機名是 localhost、`*.localhost`、`*.internal`、`*.local`、`*.home.arpa` 或單段名稱；不查 DNS，所以 `nas.lan` 這類區網網域名、指向區網的自訂網域不算，那個埠不擋（區網閘道要寫成 IP 或這些名稱才擋）。80、443、53 不加（B 只看埠號，加了就不能上網），記告警。連線與 bind 一起擋；上限 1,024，超過不截斷。
- `connections.json` 含 API 金鑰：只在執行緒那一段經 ResourceManager 的 `exists`／`read_file_text` 讀（不用會吞例外的 `read_json`），只取每條的 `url` 換成埠；不套 pydantic 模型；log 與狀態只記例外的型別名稱，不保存例外物件、不 `raise … from`。三種狀態：還沒讀成功（等同讀不到）、讀成功過（含 `exists()` 正常回 False 的「確認不存在」）、從沒讀成功而最近一次讀不到；`exists`／讀檔／解析的例外、結構不對（含任何一條 `url` 是空字串：可能代表用預設端點，不知道要擋哪個埠；連線頁存檔從 2026-06-18 起拒收空網址，設定快照匯入不檢查）、執行緒那一段失敗或被取消都算讀不到；讀成功過之後再讀不到時沿用已知集合、層級不變。第一次 `ensure_ready` 最多等 5 秒，在那之前都算讀不到，所以不會用到少了上游埠的 profile。
- 新增的連線立刻進 B：連線頁存檔、設定快照匯入（`after_save` 呼叫 `ModelManager.reload_connections()`）、啟動自癒補檔（`main.py` 在 `ensure_configs` 之後呼叫一次）、系統還原（成功與失敗都重讀；失敗時服務不重啟，`reload_connections` 同時關閉自訂模型路由，見已知限制第 13 點）。路由快照的來源由 `main.py` 以 `isolation_monitor.add_upstream_source(model_manager.route_endpoints)` 接上（避開循環匯入），每次組 B 都在事件迴圈上重讀；執行緒那一段只回傳讀到的埠，合併在事件迴圈上做。
- 終端的埠提示：層級不是 `none`、exit≠0、輸出有 Permission denied／EACCES、而且出現 B 裡的埠時，給模型看的輸出多一行英文提示，請它換埠。

### 執行器的 uid 與 sandbox state volume

- 執行器維持 uid 1001（和 proxy 同一個帳號），加權限自癒（見下一節）。維護者測試環境的 Docker 實測：9p 有 metadata，chmod 由核心實際執行。另一個 uid、維持、維持加自癒三案的讀、寫、刪的牆完全相同（都由 Landlock 擋），差別只在 chmod 這類中繼資料的可用性。
- 執行器有自己的 volume `touka-executor-state`（`/app/executor_state`，映像預建 appuser、0700，新 volume 第一次掛載時照映像複製擁有者），版面 `{agents,evolution}/<id>/{home,tmp}` 由伺服器在 START 前建；proxy 的 ST 沒有掛進執行器。proxy 開機時把 `ST/proxy/<kind>/<owner>/tmp` 原子改名成 `tmp.trash-<隨機>` 再在背景刪（不跟連結、不碰別的位置）；執行器啟動時清自己的 tmp。

### 權限自癒（白名單 W1～W9；proxy 端與執行器的掛載根、START 前的路徑鏈）

- Landlock 不管 chmod，同 uid 的指令走得到的路徑都改得了權限位元。自癒只在白名單內把**自己擁有**的 inode 的擁有者位元補回（資料夾 u+rwx；檔案 u+rw，原本有任何 x 再加 u+x），順便拿掉 setuid／setgid；不動群組與其他人（代理人資料可能刻意開放）、不 chown。執行器自己的私有 inode（W6）另外收回群組與其他人的位元，見下一節。kernel `owner_repair.repair_chain(root, target)`：只修一條路徑鏈、不遞迴，只走未 resolve 的原路徑，`O_PATH|O_NOFOLLOW` 逐段 fstat，遇到連結、換檔案系統、別人擁有、`nlink>1`、特殊檔就停，對 `/proc/self/fd/N` chmod（綁在同一個 inode），走到最後存在的那一段為止，權限位元真的有改才算「有修」，只在 Linux。
- 白名單（`services/isolation/repair.py`）：W1 掛載根 `personal_drive`、`mcp_servers`、`mcp_servers/managed`（只修那一層）；W2 `personal_drive/<id>/**`（含個人空間目錄的 `.touka/`）；W3 `mcp_servers/managed/<skill>/**`；W4 `custom/<id>/drive/**`；W5 `ST/proxy/**`；W6 執行器自己的 `state_root` 與 ipc（只由伺服器修；補擁有者位元，伺服器自己的私有資料夾與檔案另外拿掉群組與其他人的位元）；W7 `state_root`、W8 `custom` 只修那一層；W9 `custom/<id>` 只在走向 W4 的路上修。原路徑與 resolve 後的路徑都要落在同一條（含 id 那段）。data 根、記憶庫、`backups`、快照、soul、legacy、config、`.env` 所在資料夾、`/app`、`hf_cache`、工作區與專案一律不修。
- 判斷順序：裝飾器只處理 Linux 上 `errno == EACCES` 而且帶 `filename` 的 PermissionError（ResourceManager 的保護拒絕與個人空間目錄的 `CatalogPathDeniedError` errno 是 None，到不了掛鉤）→ 白名單 → `is_protected_path` → 頻率限制（同一目標 10 秒；每 60 秒全域 120 次，根另留 30 次；額度用完標 `permission_relock`，下一個時間窗自動恢復）→ kernel。掛鉤出錯、沒修到或 `retry=False` 時，原本的例外**物件**原樣往上丟；`retry=True` 修完重試一次；只有最外層的已裝飾呼叫處理（ContextVar）。
- `ResourceManager` 每個會碰檔案的公開方法恰好一個 `@permission_repair(retry=<字面 bool>)` 或 `@permission_repair_exempt(category, reason)`：唯讀與冪等 mkdir 重試，多步驟寫入、append、可能已部分完成的不重試，記憶系統依賴「原樣往上丟」的 8 個方法（`move_file`、`remove_file` 與記憶系統第二批第 7 段的 6 個）豁免。`tests/lint/test_resource_manager_permission_repair.py` 以 AST 遞移分析與兩份釘選清單執法，也禁止 from-import／別名與帶 errno 的 `PermissionError`／`OSError`。維護規則：之後要在白名單範圍內加依賴「權限錯誤原樣往上丟」的流程時，改用呼叫處豁免，由後合併的一方補。
- 時機：已裝飾方法的存取；`_state_issue` 判 ST 不可寫時先修 `ST/proxy` 再判；約每 60 秒定期修 W1、W7、W8 與 `ST/proxy`（monitor 第一次 `ensure_ready` 時安裝掛鉤，只在 Linux）；執行器約每 60 秒修讀寫掛載根與 `managed`、START 前修 home、tmp 與 cwd 的路徑鏈（都只補擁有者位元；W6 見下一節）。計數在 `GET /isolation` 的 `repairs`（不帶路徑與代理人 ID）；`refused_outside`／`refused_protected` 單次記 DEBUG，60 秒內滿 50 次記一則彙總告警（10 分鐘最多一則），`denied_burst` 只記、不上橫幅。

### 執行器修自己的私有資料夾（W6，第 3 次複審 F1）

- 和上一節分開：上一節是 proxy 端（與執行器 START 前、掛載根）替代理人資料補擁有者位元，不動群組與其他人；這一節只管伺服器自己的私有 inode——`state_root`、ipc 資料夾與 ipc 裡的 `ctl.sock`、`token`、`alive`。它們不是代理人資料，啟動檢查（`_private_problem`）要求伺服器擁有、群組與其他人沒有任何權限。
- 做法：kernel `owner_repair.repair_private(root, target)`（`private_mode`＝`owner_mode` 之後再拿掉 `0o077`），和 `repair_chain` 走同一條 `O_PATH|O_NOFOLLOW` 鏈、在同樣的地方停（連結、別人擁有、換檔案系統、多連結檔、特殊檔），所以不是 uid 1001 擁有或是連結的根不碰。只會變嚴；proxy 端的 `repair.py` 不用它。
- 時機：每 5 秒的維護先用 `repair_chain` 補擁有者位元，再用 `repair_private` 收回；`start()` 在 `config_problem` 是 None 或只有 `private_dir`（其他設定都對）時，先收回這兩個資料夾再做 `_claim_ipc` 與設定檢查，設定有別的錯時什麼都不修。
- 結果：代理人在執行器裡（同 uid、Landlock 不管 chmod）把根改成 777 或 000，5 秒內收回、服務不中斷；停機時被改，重啟時先收回再服務。子資料夾（`agents`、`agents/<id>`、HOME）不在範圍，改成 777 不收回、也不影響服務（代理人彼此靠 Landlock 隔開，不靠權限位元）。
- 不採用「啟動時也先修讀寫掛載根」：命令列設錯（例如指到 Touka 資料根）時，會在檢查之前先改到不該改的根（後果見已知限制第 24 點）。

### 獨立失敗 fd、可信直譯器、隔離邊界只用允許清單

- `--status-fd`：啟動器一拿到就設 CLOEXEC，失敗時把代碼寫進那條通道；runner 只認那條通道裡恰好一個已知代碼（本機 `FAILURE_CODES`，執行器另加 `RELAY_FAILURE_CODES`），stderr 那一行只給人看，指令自己印出的代碼不會觸發重探。終端、驗證指令、演化跑完（含逾時、取消、spawn 失敗）都讀完並關掉，演化也回報給 monitor。
- `trusted_interpreter()`（kernel SSOT）：`sys._base_executable` 解析後的實體路徑；容器裡檔案與每一層上層都要 root 擁有、群組與其他人不可寫、不是連結，不符就是 `interpreter_untrusted`（`none`，降級只能 `direct`）。用在 LandlockRunner、relay、自測與執行器 spawn；原生環境沒有信任根，只解析路徑。
- 隔離邊界（`sandbox_kernel/**`、`services/isolation/**`）不得匯入或引用 `child_python_env`、`get_full_env`、`get_clean_env`（`tests/lint/test_isolation_boundary_no_full_env.py`）：開機自測的環境改成空的，執行器的 START 只收允許清單。

### 設定檔讀不到時 fail-closed

- 7 個載入器只有兩個在讀不到時比檔案寬，都修了：`auth_policies.json` 啟動時讀不到（`OSError`）→ 政策維持未載入，走既有「政策未載入一律拒絕」（禁令 59(d)，決策表沒動）：所有工具和指令都拒絕、只能聊天，`/health` degraded、原因 `auth_policies_unreadable`；`autonomy.json` 讀不到 → 每一項取最嚴、停止自主執行（工具輪與心跳都停），`/health` degraded（`autonomy_config_unreadable`），審查員逾時用模型預設。壞 JSON 不在範圍。
- MCP 設定（`server_config.json`、`mcp_hub.json`）存在卻讀不到時，安裝、建立、同步改丟 `PERSISTENCE_ERROR`、檔案不變（原本當成空設定整份寫回，會刪掉其他伺服器、重新啟用停用的）；啟動健檢在設定資料夾列不出來時中止。permissions、identity、群聊白名單、MCP registry、users 本來就一樣或更嚴，只加鎖定測試。
- `/health` 與 `/system_health` 多 `system_notices`（只有代碼與等級），前端側欄的通用提示列寫出原因與恢復步驟。
- `.example` 只補不存在的設定檔；存在卻讀不到時不覆寫，也不退回較寬的預設。（維護者 2026-10-16 同意寫進 AGENTS §11，同一句。）

### 狀態端點與橫幅

- `GET /isolation`（含 `/v1`，`require_authenticated`）：走 `ensure_ready`，回層級、預期、原因、features、本機狀態、`local_issue`、執行器的 ABI 與網路設定、`block_ports_issue`、`repairs`；不帶路徑、roots、權杖、digest、boot_id、金鑰與代理人 ID。前端登入後每 30 秒問一次，層級低於預期、埠沒擋或自癒額度用完時在右側欄顯示隔離橫幅（原因與補救方式同卡片）。

### compose、GHCR 與 CI

- 三份 compose 都加 `touka-executor`、網路 `touka-exec-net`、volume `touka-exec-ipc` 與 `touka-executor-state`，proxy 加 `TOUKA_EXECUTOR_DIR` 與 ipc 掛載。開發版根目錄 compose 的 proxy 映像改名 `touka-proxy:local`（執行器 `pull_policy: never` 用同一個），`touka-init` 先 `mkdir -p` 兩個 data 子資料夾；`ToukaProxy/docker-compose.yml` 用 `touka-proxy:latest`；GHCR 的執行器用同一個映像表示式，data 子資料夾是 long syntax 加 `create_host_path: false`，skills 是 `proxy-config` 的 `subpath: skills`（`nocopy`、唯讀，需要 Compose 2.26 與 Docker Engine 26.0 以上；Engine 26.0.0 release notes：`VolumeOptions` 加 `Subpath`，2026-10-16 查 https://docs.docker.com/engine/release-notes/26.0/），`depends_on` proxy healthy、最後一段才建；init 的四個角色不變。禁止項目（`env_file`、對外埠、`privileged`、`cap_add`、`pid`／`ipc`／`network_mode`、裝置、`extra_hosts`，掛 `.env`、`host_root`、原始碼、其他設定、工作區、`docker.sock`）由 `test_executor_compose_security.py` 釘住。
- Dockerfile：git 另成一層，放在 `uv sync` 之後、`COPY . .` 之前（不讓 venv 那層的快取失效）；預建 `/app/ipc/executor`、`/app/executor_state`（appuser、0700）；`/opt` 那段不動。
- 核心有 MPTCP 時，覆寫檔要在 `touka-proxy` 與 `touka-executor` 兩個服務都加 `net.mptcp.enabled: 0`（兩行都寫）。
- CI（`.github/workflows/isolation.yml`）：`landlock` job 在原生 ubuntu-24.04 跑 Linux 模組兩次——pass A 不加 sysctl、不設 REQUIRE（必須 0 failed、skip 都要帶允許的標籤、網路設定不安全時不安全分支的案例必須 passed）；pass B 關 Fast Open（有 MPTCP 也關）、設 `TOUKA_REQUIRE_LANDLOCK=1`（層級測試必須 passed，ABI≥6 不准 skip、ABI 4／5 只准 `[skip:abi6]`）。`scripts/ci/isolation_junit_check.py`（只用標準函式庫）核對 junit，拒收 pytest 是 PID 1 的結果（才抓得到 subreaper）。`container` job 在 PR 改到隔離相關路徑時自動跑（第 4 次複審後改成路徑篩選，不靠標籤：`container-scope` job 以 `fetch-depth: 2` checkout PR 的 merge commit，用 `git diff --name-only --no-renames -z HEAD^1 HEAD`〔merge commit 對它的第一個 parent＝這個 PR 帶進來的改動〕列出改動，bash `case` 比對 17 項路徑〔`ToukaProxy/src/sandbox_kernel/*`、`ToukaProxy/src/services/isolation/*`、`ToukaProxy/src/services/execution_broker.py`、`ToukaProxy/src/utils/isolation_policy.py`、`ToukaProxy/src/utils/touka_dirs.py`、`ToukaProxy/src/utils/permission_repair.py`、`ToukaProxy/Dockerfile`、`.dockerignore`、`uv.lock`、`docker-compose.yml`、`docker-compose.ghcr.yml`、`ToukaProxy/docker-compose.yml`、`tests/docker/*`、`tests/network_guard.py`、`tests/smoke/lib/sandbox_env.py`、`.github/workflows/isolation.yml`、`scripts/ci/isolation_junit_check.py`；`case` 的 `*` 也比對 `/`〕，輸出 `isolation=true/false`；容器 job `needs: container-scope`，條件是 `isolation == 'true'`、手動觸發、PR 帶 `isolation` 標籤三者任一，不跑時顯示 skipped。只用 git，不用第三方 action，也不用 `on.pull_request.paths`〔會連 landlock job 一起篩〕；git 失敗時 `container-scope` 失敗、看得到。`tests/lint/test_isolation_ci_workflow.py` 釘住清單與條件，並自動推導必須在清單裡的檔，少一個就紅。push 到 main／dev 不跑容器 job〔和以前一樣〕；landlock job 每個 PR、push、手動觸發都跑。repo 當時沒有 required checks，所以跳過時不會讓 PR 一直等。這個 PR 本身改到這些路徑，容器 job 會自動跑，不用建標籤。清單以外但會影響映像的改動〔例如隔離程式匯入的 `src/config.py`〕不會自動觸發，要加標籤強制跑），`isolation` 標籤留作強制跑（沒改到那些路徑也跑）；手動觸發（`workflow_dispatch`）要等 `isolation.yml` 進 main 之後才能用（GitHub 只認預設分支上的 workflow 檔）：建映像、三份 compose `config -q`、`tests/docker/isolation.compose.yml` 端到端、proxy 本機 Landlock、GHCR 形狀的執行器起到 healthy，核心有 MPTCP 時帶覆寫；合併條件是審過的 head 上這個 job 成功過。

### 創建試跑隔離

創建的 SMOKE 與 EVAL 已接 MCP 隔離 runner，整次工具呼叫共用一次取出的 grant；`create_mcp` 用全域層級與 `mcp_self`，`scaffold_mcp` 用本機層級與 `mcp_scaffold`，同正式啟動的 profile。HTTP 不帶降級同意，none 時拒絕試跑，仍可跳過 SMOKE 登記設定；後置 EVAL 也不會繞過隔離。來源只從程式路徑傳給 COMMIT，不採模型或 spec 的值。

scaffold 在隔離試跑時將 APPDATA 參數對映到 personal_drive 根，與正式啟動一致；試跑 env 仍不解析哨兵、不加 `TOOL_BASE_URL`。試跑傳輸用 `on_eof="close"`，伺服器退出不取消創建呼叫端。

反向註記：上方「相關」中「還沒接 runner」、「決定」中「沒接隔離的工具一律未隔離」（含 YOLO 只有人能核准的說法），以及下方已知限制中「未接線」的 `create_mcp`／`scaffold_mcp` 部分，均由本節接線取代；其他隔離限制維持。有隔離時 YOLO 依層級執行；層級 none 時 Auto／YOLO 仍只有人能核准；Auto 原本就受底線 F5 約束。

## 考慮過、不採用的做法

- 不 pin：9p 上每次都失效。長壽行程持有 pin：要處理資料夾刪建與 Windows 端改名，列為之後的候選。
- 網路層先套：記憶體多一倍。單一合併規則集：每條指令多約 86 ms。
- spec 放 argv：大 profile 會撞 E2BIG。
- 上層逐層給 READ_DIR（見上）。Touka 資料夾裡的專案一律不授權：開發版預設工作區就是 Touka 資料夾，影響面大，維護者選擇修規則。
- 審批卡原因改通用範本：安全功能完整優先，每個原因各有說明。
- 資料根寫帶隨機 ID 的哨兵檔：多一個啟動寫入，`ui_sessions.db` 本來就由啟動建立。
- 網路設定不安全時整個本機 Landlock 判不可用（第 2 次複審 FC2 的做法）：Auto／YOLO 確實變嚴，但人核准後只能「未隔離」，用 Touka 的全權限跑、讀得到 `.env` 與所有資料，比修正前（埠可能被繞、檔案仍隔離）還弱，也跳過了「核准後用當下最強的方式」的中間那一層。第 2 次複審後的補修正改成比照 ABI 3 保留檔案隔離（見「網路」）。
- （C2-2）執行器改用另一個 uid（1002:1001）：讀、寫、刪的牆和現在相同；卻多出不需要惡意就會發生的日常失敗（proxy 讀不到執行器建的 0600 檔、進不去 700 資料夾），setgid 會讓 GHCR 的 `permission_init` 拒絕啟動，要改 ResourceManager 多條寫入路徑，對 proxy 本機的 Landlock 層無效，執行器建的檔反而變成 proxy 修不回。最後選維持 1001 加自癒。
- （C2-2）伺服器放 `sandbox_kernel/`：kernel 只准標準函式庫，ratchet 也只准少數地方啟動子行程；放 `services/isolation/` 才能沿用 broker、不放寬 ratchet。執行器和 proxy 共用 ST：執行器裡的代理人會碰到 proxy 降級時的 HOME，所以分開。
- （C2-2）網路設定不安全時執行器仍判 `executor`（第 1 版建議）：Auto／YOLO 會在擋不住內部埠時不經人就跑，違反決定 3；改成判 `none`、補「執行器」這個降級去處。讀不到連線設定或 B 溢位時降到未隔離（2.2 版）：比照網路設定不安全保留檔案隔離。
- （C2-2）B 超過上限就截斷（C2-1 的寫法）：會丟掉高號的埠，改成溢位 fail-closed。上游埠只取健康的路由快照：探測失敗、停用或刷新中的連線不在裡面，改從 `connections.json` 全部條目推導、快照與 registry 取聯集、只增不減。
- （C2-2）保護拒絕改成子類別 `ProtectedPathError`：要動記憶系統的 raise 處，errno、白名單、`is_protected_path` 三層已擋住。以呼叫處為單位的自癒豁免：白名單已擋住記憶系統第二批第 7 段的路徑，呼叫處參數漏傳時沒有測試會紅。自癒也修 Touka 自己的狀態（淺層版本）：記憶系統的保護語意要求 Touka 的資料照實報錯、不自動改權限，改列已知限制。
- （C2-2）healthcheck 只看 socket：volume 裡殘留的舊 socket 會騙過。proxy `depends_on` 執行器：改靠背景重探。開機自測與 START 用黑名單過濾的環境（`get_clean_env`）：漏得掉新名字，改成空環境與精確名稱允許清單。裝 mypy／pyright 做靜態型別檢查：是新依賴，依路徑載入的 kernel 改靠 Protocol 與執行時核對。

## 實作偏離

- 系統路徑遇 `ENOTDIR` 或符號連結就略過（設計寫 ELOOP，照寫每次 spawn 都會失敗）；單檔規則由啟動器 `fstat` 檢查 `S_ISLNK`、`st_nlink`。
- （C2-1）執行器健康檢查從 C2-1 移到 C2-2（已做，見「層級判定與降級去處」）；`landlock_unavailable` 獨立成原因；新增 `isolation_check_failed`、`runner_not_wired`、`isolation_ack`、`PYTEST_ADDOPTS`。
- 底層掃描放 `utils/`（`services/` 的 I/O 規則擋 `os.open`，不加 noqa）；加 NFC 正規化；原生模式 repo 裡的專案一律拒絕；演化接線另開 `evolution_command.py`（避開循環匯入）。
- 第 2 次對抗式複審後的修正：G 改依身分；資料根改三個名稱任一；失敗標記改全域限頻；新增 `net_tcp_fastopen`、`net_mptcp`；Fast Open 與 MPTCP 沒有照計畫實測（見「網路」）。
- 第 2 次複審後的補修正（差異複審審查者③、②）：網路設定不安全時改成比照 ABI 3（自測結果多 `net`、`LandlockState` 多 `net`、features 扣掉 `tcp`）；`kernel_outdated` 或讀不到 kernel 時 `fallback_for` 一律 `direct`；審批卡「沒有 `tcp`」那句不再寫「核心太舊」，`net_*` 原因多一句原生 Linux 改主機 sysctl；Linux 隔離測試在沒設 `TOUKA_REQUIRE_LANDLOCK`、網路設定不安全的主機（例如 GitHub contracts job 原生執行）照實跳過並寫原因，設了就失敗。
- （C2-2，都更嚴或等價）B 沒做指紋快取：每次從記憶體重算，`connections.json` 每次 refresh 都在執行緒重讀；原始碼的 kernel 每次 refresh 都雜湊（映像的只一次）；版面每次 refresh 在執行緒重算、事件迴圈只用快取；refresh 加單一飛行。url 非空但解析不出埠的那一條不給埠、不判讀不到。演化在執行器層也看身分的 personal drive（自訂模型身分是 `agent_dir_not_in_executor`）。MCP registry 讀檔（記憶體裡的只有啟用的條目），讀不到不改層級。relay 本身不帶環境，指令環境只在 START。卡片的 `executor_eligible` 在政策時記進帳本，沒動 `tool_executor.py`。
- （C2-2）執行器伺服器 3 處 `# noqa: IO_PRISON_VIOLATION`（flock 鎖檔的 `os.open`、刪殘留 socket 與 `alive` 的 `unlink`、建私有資料夾的 `mkdir`；伺服器在 proxy 行程之外拿不到 ResourceManager），CI checker 1 處 `# noqa: S314`（只解析同一個 job 剛產生的 junit），都登記在 `NOQA_REGISTRY.md`。`check_start` 的檢查順序和設計不同（都拒絕，只差 log 的原因碼）。
- （C2-2）compose：healthcheck 寫 `$$(find …)`（`$(` 會被當成插值）；GHCR 的 skills 加 `nocopy`；開發版 init 只 mkdir、不 chown 新 volume；`/app/ipc` 由 root 擁有；GHCR 執行器的 `TZ` 寫死。CI 容器 job 的 MPTCP 覆寫也涵蓋測試用的服務（規則是「每個關 Fast Open 的服務都另關 MPTCP」）。
- （C2-2）卡片：預期執行器時，不是範圍、不是埠的原因一律以「執行器目前不能用」開頭；網路設定與封鎖埠同時不安全時改寫成不互相矛盾的句子；執行器降級的結尾寫「和 Touka 在不同容器、共用同一個核心」；橫幅不列 `local_issue`（開機一定有「還在檢查」）；ipc 的說明只接在 `executor_unreachable`、`executor_auth_failed` 後面。
- （C2-2）設定讀不到：設計推測 auth_policies 啟動時「比照熱重載、風險表取最嚴」，但啟動時沒有上一份黑名單，照做仍比檔案寬，改走「政策未載入一律拒絕」；`/health` 不新增欄位，只算進 `status`；畫面提示另做通用提示管道（`system_notices`），不沿用記憶提示列。
- （第 3 次複審後，都更嚴或等價）W6 從設計 v2.2 的「只補擁有者位元」改成另外收回群組與其他人的位元（v2 §2.3 原本就寫每 5 秒把 ipc 權限重設回去），`start()` 在其他設定都對時先收回再檢查；執行器背景工作改接所有例外；突變清單加 `server_subreaper_off`（33 項）；CI 容器 job 改成帶標籤的任何 PR 事件都跑；`.gitignore` 只忽略 repo 根的 `docker-compose.override.yml`，`TOUKA_TOPLEVEL_PROTECTED` 同步加上這個名稱（Touka 目錄頂層叫這個名稱的不能授權）。
- （第 4 次複審後）CI 容器 job 從「帶 `isolation` 標籤的任何 PR 事件」改成依路徑自動跑、標籤強制跑（見「compose、GHCR 與 CI」）：這個 PR 開 PR 時不用先建標籤，之後改到隔離的 PR 也不會因為忘了加標籤而沒跑。
- （C2-2）系統還原失敗那條路徑原本列為「等記憶系統第二批合併後再補」的已知限制：已補上（`routers/system.py` 的還原成功與失敗都重讀連線），不再是限制。

## 已知限制

- pin 只在持有 fd 的行程活著時有效：指令自己關掉繼承的 fd（`closefrom`、daemon 化）、父行程先結束後孫行程才存取、Windows 端在執行中改名或刪除資料夾（沒測），在 9p 上會被誤拒（不會多放行）。ST（ext4）不受影響。
- 掃描快取：第一次完整掃描後，第一層以下、或已存在的第一層資料夾裡的變動，要等 10 分鐘 TTL 才看到（真正的 Touka 與 data 由固定 P 保護，不靠掃描）。往下只看 3 層；大型專案第一次綁定多半先看到一次 `scan_limit`（9p 上 2 秒約 1,500 個資料夾），之後只有第一層新增或同名換掉資料夾時補掃那些（前景仍只等 2 秒）；TTL 到期時整個重掃，大型專案會再看到一次（背景重新驗證沒做）。
- 會多擋：專案往下 3 層或往上到掛載根之間有叫 `soul`、`personal_drive` 的資料夾或叫 `ui_sessions.db` 的檔就被拒；原生 Linux 的家目錄經符號連結（例如 `/home → /var/home`）時那裡的專案都被拒，Windows 8.3 短檔名也是。認不出：部署資料夾改過 compose 檔名、`HEARTBEAT_CONFIG_DIR` 也移走時。
- 硬連結：公開設定檔要 `st_nlink==1`；代理人寫不到 Touka 目錄，`.env` 又在另一個掛載上，做不出指向它的硬連結。
- 使用者在 shell 裡整個重設 `PYTEST_ADDOPTS` 時 pytest 回到 EACCES（fail-closed）；原生 Linux 的 data 在 checkout 裡時，代理人資料夾的上層會授權到 checkout 的設定檔，仍會注入。monorepo 子資料夾當專案時 git 找不到上層 `.git`；真防線是 Landlock 讀不到上層 `.git`，`GIT_CEILING_DIRECTORIES` 只是讓 git 不報錯。
- `kernel_digest` 依 checkout 的換行而不同（同一個 checkout 一致，不影響判定）。
- Podman 沒有 `/.dockerenv`，會被當成原生執行，不在支援範圍。
- （C2-1 時）封鎖埠清單 B 不含 Touka 當客戶端去連的 localhost 上游埠（例如本機的 LiteLLM），隔離內的指令連得到、也佔得到，可能花用使用者的 API 額度或攔下 Touka 送出的 Bearer。C2-2 已修：B 從設定、連線、路由快照與 MCP registry 推導（見「封鎖埠 B」），連線和 bind 一起擋。
- 不擋 UDP；封鎖看埠號，代理人自己起在這些埠的服務自己也連不上；主機其他埠與區網連得到（決定 3）。`/proc/*/cmdline` 跨代理人讀得到（別的代理人的指令列、第三方 MCP 的參數，token 不要放在 MCP 的 args）；沒有 pty；寫死 `/tmp` 的工具與 multiprocessing semaphore 會失敗；自己 `setsid` 出去的孤兒行程不會被行程群組一起殺掉（proxy 本機；執行器裡由 subreaper 收掉）。
- Landlock 不擋 chmod、chown、utime、setxattr：隔離內和 proxy 同一個 uid，走得到路徑就能改任何同 uid 檔案的權限位元（例如把設定或別的代理人資料夾設成 000，造成 DoS）。C2-2 的執行器仍是同一個 uid（另一個 uid 不採用，見「考慮過」），改用白名單自癒補回代理人工作區的擁有者位元；殘留見下面「C2-2 的已知限制」第 1～11 點。
- 執行器沒有掛 data 根、soul、config，所以能鎖住 Touka 自己狀態的只剩 proxy 本機 Landlock 這一層：人核准、卡片寫「在 Touka 主容器內以檔案隔離執行」的那一次（含 ABI 3、網路設定不安全、綁專案、執行器不能用時），原生 Linux（沒有執行器）的 Auto／YOLO（人核准的「未隔離」指令本來就做得到）。MCP 伺服器不在這一層：C2-3 合併前它們完全沒有隔離（見本節「這個 PR 沒有隔離的」），用 Touka 的帳號跑，本來就做得到。沒套新 compose 的部署不在這一層：沒有 `TOUKA_SANDBOX_STATE_DIR`，判 `sandbox_state_unconfigured`，Auto／YOLO 要人核准，核准後是未隔離。
- （C2-1 時）啟動器的直譯器是 `sys.executable`（`/app/.venv`，appuser 擁有），核准過一次的未隔離指令可以換掉它、繞過 `/opt` 信任根；C2-2 已改用 root 擁有的基底直譯器（`trusted_interpreter`）。原生 Linux 根本沒有信任根：每次 refresh 把當下的雜湊當基準，只抓得到檢查之後、spawn 之前被改的。
- 「未隔離」卡在內容被截斷時仍可核准：演化程式碼只顯示 200 字、`verify_mcp_locally` 的 subject 不含程式碼、指令超過 8 KB 截斷。這個 PR 沒有改；列在 C2-3 的範圍（另一個 PR）。
- P 不含 proxy 自己的 HOME 與 `/tmp`（admin／Debugger 把專案根設在工作區外時，未隔離指令存在 `~` 的憑證不受保護）；`config/skills` 所有代理人都讀得到，`.mcp.json` 的 env 不要放金鑰。
- 原生 Linux 用 systemd-resolved 時，`/etc/resolv.conf` 指到沒授權的 `/run/...`，隔離內沒有 DNS（pip、npm 失敗）；Docker 不受影響。【推測】舊版 Docker Engine 的預設 seccomp 可能擋掉 landlock 系統呼叫，一律 `landlock_unavailable`。Docker Desktop 的 VHDX 清掉 ST 後不會自己縮小。
- AF_SMC 沒處理：沒有可靠的只讀判斷（`/proc/net/protocols`、`/proc/sys/net/smc` 只反映已載入的模組，smc 可能被自動載入；`/proc/config.gz` 不是每台都有）。【推測】Docker 預設 seccomp 不擋 AF_SMC，SMC 退回 TCP 時核心內部的 socket 不經 Landlock 的 connect 檢查。維護者測試環境的核心沒載入 smc，`/proc/config.gz` 也沒有 `CONFIG_SMC`。一般 Linux 主機的對策寫在 README_QUICKSTART：用 modprobe 設定封鎖 `smc` 模組（`/etc/modprobe.d/touka-no-smc.conf` 寫 `install smc /bin/false`），README「6.12 以上完整隔離」那列也加了 SMC 的但書。
- 殘留自測資料夾的清理假設同一份 data 只有一個 proxy 行程（兩個共用時可能刪到對方的，結果是 `fs_9p` 10 分鐘，fail-closed）。C2-2 起失敗代碼只認獨立的狀態 fd，指令自己印出的代碼不再觸發自測。
- 代理人自己資料夾裡的 `.touka`（ADR-0255）仍可經終端改寫，和以前一樣，只影響該代理人。
- HOME 搬到 ST（執行器裡是 `touka-executor-state`）：`~` 裡的舊東西看不到、快取要重新下載；兩個 volume 都沒有大小上限；`docker compose down -v` 會刪掉它們。
- **這個 PR 沒有隔離的**（和合併前一樣，C2-3 另開 PR 處理 MCP）：
  - MCP 伺服器：Hub 第三方、`create_mcp`／`scaffold_mcp` 建的、演化部署的自建 MCP，都由 Touka 在主容器以同一個帳號啟動，只有 ADR-0254 的啟動環境白名單：自己的環境變數裡沒有金鑰，但讀得到 `.env` 檔（`/app/host_root`）、PID 1 的環境（ADR-0254 已寫）、data、設定與原始碼，連得到 Touka 內部埠，也能 chmod Touka 的檔案（見本節「Landlock 不擋 chmod」與 C2-2 第 1、12 點）。決定 4（YOLO 無人在場時 dangerous 的 MCP 工具只有人能核准）在這個 PR 生效，管的是「誰能呼叫」，不是伺服器本身的權限。
  - 演化部署的自建 MCP：建 venv、pip、本機驗證在隔離裡跑，部署後那台伺服器未隔離。部署要人在部署卡上核准（卡上有原始碼；無人在場拒絕），但之後**再演化同一個 skill** 會直接改寫那台伺服器的 `server.py`：`EvolutionPathGuard.write_tool_file` 對任何 `managed/<skill>` 都可寫，不看是不是已部署、也不看是哪個代理人建的；Auto 下因底線 F5 只有人能核准，YOLO 下層級是 `executor`／`landlock` 時自動放行（含無人在場）。改過的程式在那台伺服器下次啟動時以 Touka 的權限執行，不會再跳部署卡（第 2 次對抗式複審審查者① MEDIUM-5；合併前 YOLO 的終端本來就做得到同樣的事，所以不比合併前差）。另外，驗證是否通過是比對輸出裡的字串 `"status": "success"`（`_VERIFY_SUCCESS_MARKER`），受測程式自己印得出來；只影響要不要提出部署卡，部署仍要人核准。兩件都是 C2-3 開工必做①（部署後唯讀、拒絕再演化）與必做②（驗證改走 MCP 協定）的範圍。
  - `create_mcp`、`scaffold_mcp`：判 `runner_not_wired`，卡片寫「未隔離」，YOLO 下改成要人核准（Auto 本來就是底線 F5）；第三方工具剛好同名時卡片也寫「未隔離」、核准不執行（內建優先，正式環境碰不到）。
  - IDE 自己的終端、Dispatch 的 `auto_invoke`。
- 本機 Landlock 在 ABI 3（例如 WSL 預設核心 6.6）：層級 `none`，核准後只有檔案隔離（埠沒擋、信號沒隔開；執行器在 ABI 3 照樣判 `executor`，見 C2-2）；ABI≥4 但 Fast Open 或 MPTCP 沒關：層級 `none`，核准後檔案隔離、一般 TCP 照樣擋封鎖埠，但擋不住這兩種連線；ABI<3 或沒有 Landlock：核准後未隔離。網路設定在自測時讀，C2-2 起啟動器每次啟動也讀（`net_strict`）：原生 Linux 上主機管理者之後重新打開時，下一條指令就以 `net_*` 拒絕執行並觸發重探。原生 Windows 一律 `none`：Auto／YOLO 每條代理人指令都要卡；心跳裡的終端、演化、驗證指令在所有模式都被拒；IDE 聊天面板不顯示審批卡，要等 180 秒才被拒（以前 YOLO 直接執行）。
- 決定 4 影響所有無人在場的執行（心跳、SNS、子代理〔你人在聊天裡也算〕、派工、`/debug/execute`、IDE 協定工具）：YOLO 的 SNS bot 會失去沒列名（預設 dangerous）的 MCP 工具，例如 Fetch；可在 `auth_policies.json` 的 `tool_risk` 標成 safe 或 read_only 保留。
- 審查員看得到隔離層級，Auto 的放行率可能改變（量不到）。

### C2-2 的已知限制

自癒的殘留（設計 §N11）：

1. **Touka 自己的狀態不修**：data 根、soul、legacy、config、記憶庫、`ui_sessions.db`、backups、快照、`/app`、`hf_cache` 被改權限時，不會自動修回（誰能觸發見上一節最後那點；會怎樣、怎麼恢復見「給部署者」）。C2-1 就存在（第 2 次複審審查者①的 MEDIUM-2），C2-2 沒有讓它變大；compose 部署下觸發的機會反而變小，因為那一層只在人核准時才用到。
2. **反覆重鎖**：會有間歇失敗；超過頻率上限時停到下一個時間窗（橫幅標示）。
3. **只修擁有者位元**：群組與其他人的位元不修（只影響原生 Linux 的多使用者主機；執行器自己的 W6 例外，會收回）；不 chown；s 位元只在修路徑時順便拿掉，對自己的正常檔 `chmod u+s` 不會觸發。
4. **只在這些時機修**：已裝飾方法的存取、`_state_issue`、定期檢查、執行器 START 前。其他路徑（sqlite、`async_db`、其他模組直接 open）與豁免方法（`move_file`、`remove_file` 與記憶系統的方法）不修；代理人資料夾被鎖時，搬移與刪除會照實失敗一次。
5. **不是 Linux 就不修。**
6. **GHCR 的 `permission_init` 會拒絕啟動**：資料裡只要有符號連結（代理人建 Python venv 或 `npm install` 就會產生）、FIFO 或 socket、setuid／setgid、xattr、多連結的一般檔、超過 10 萬項或 64 層、或 60 秒做不完，`touka-init` 就失敗、Touka 起不來。這是既有問題，另案處理；用 dev compose 的部署不受影響。（2026-10-23 補記：由 ADR-0279 處理，這些項目改成略過、不再拒絕啟動。）
7. **白名單裡刻意設成唯讀的檔會被改回可寫。**
8. **ipc 被反覆改權限**：執行器裡任何一個代理人反覆改執行器控制通道的權限，執行器就對所有代理人變成「不能用」，之後全部指令都要人核准（fail-closed，不是放寬）；伺服器每 5 秒補回擁有者位元並收回群組與其他人的位元，所以只會在被改到下一輪之間間歇失敗。
9. **原生 Linux 的 `data/temp` 被鎖時**，ST 修不了，原生 Linux 上的降級就只剩未隔離。
10. **B 只增不減**：從設定拿掉的上游連線，它的埠要等 Touka 重新啟動才解除封鎖；完整 B 超過 1,024 之後，刪掉連線也不會恢復，要重新啟動。溢位期間的狀況同下面第 12 點的讀不到，而且自訂連線與 MCP 的上游埠一律不擋（只擋核心 B）。
11. **原生 Linux 的 data 根本身是符號連結時**：白名單由 resolve 過的 data 根推導，ResourceManager 收到的原路徑沒有 resolve，「兩條路徑都要落在同一條白名單」一律不成立，這種主機上自癒等於沒有作用（fail-safe，不修）。Docker 部署不受影響。
12. **`connections.json` 讀不到**：誰能觸發——能 chmod Touka 設定的程式（主容器 Landlock 層：人核准的降級、原生 Linux 的 Auto／YOLO；以及未隔離的指令與 MCP 伺服器——C2-3 合併前 MCP 伺服器都沒有隔離）把這個檔或 config 目錄改成讀不到再等 Touka 重新啟動，或手改這個檔時把 JSON 寫壞、有一條連線的 `url` 是空的（2026-06-18 以前存的檔、手改或匯入的設定快照；不需要惡意）；執行器裡的代理人不行（config 沒有掛進執行器）。會怎樣——判 `none`，Auto／YOLO 每條代理人指令都要人核准、無人在場拒絕；核准後仍在執行器（或本機 Landlock）裡執行，Touka 自己的埠照樣擋，但自訂連線的上游埠可能沒擋。怎麼恢復——還沒讀成功過時每一輪背景檢查都會重試，修好內容或權限後約半分鐘內自動恢復，不用重新啟動（空網址：在連線頁補上網址或刪掉那條再存檔）；權限要用 `touka-init` 修時照「給部署者」的恢復步驟做完（那一步已含重新啟動）。畫面寫「讀不到 connections.json」，空網址時也是這句，容易誤以為是權限。

其他：

13. 手動改 `connections.json`、或代理人經人核准後寫入（底線 F1 與 `_SECRET_FILES` 有保護）的新連線，只能靠 monitor 讀磁碟補上，最多慢一輪（約 10 秒）。匯入含連線的設定快照後，自訂模型路由先關閉，要重新掃描或重啟才恢復（和連線頁存檔一樣，不沿用已停用的連線）。系統還原**失敗**時也一樣：`routers/system.py` 的 except 分支呼叫 `reload_connections()`，`connections.json` 沒變也關閉自訂模型路由，而這時服務不會重啟，用自訂連線的靈魂與心跳開始失敗、沒有畫面提示，要重新掃描或重啟（官方供應商不受影響；沒有測試釘住這個行為）。
14. ABI 3～5 時信號沒隔開，執行器裡的代理人可以讓執行器伺服器停掉（DoS，之後一律要人核准）。
15. 綁專案的對話、`custom/<id>` 資料夾的代理人（例如 DebugAgent）不在執行器裡跑：Auto／YOLO 每條要人核准，核准後在 Touka 主容器內以檔案隔離執行；心跳與無人在場時拒絕。
16. 上游埠是 80、443、53 時不擋。執行器與 proxy 降級時的 HOME、快取各一份。執行器的允許清單不帶代理伺服器與自訂憑證（`HTTP(S)_PROXY`、`NO_PROXY`、`SSL_CERT_FILE`、`REQUESTS_CA_BUNDLE`、`PIP_INDEX_URL` 等）：需要它們才能上網的環境，執行器裡的 pip／npm／git 可能連不出去。
17. 執行器的 fail-closed 邊角：state 根或 ipc 根不是 uid 1001 擁有、是連結或不存在時不服務（見第 21 點）；`personal_drive` 第一層出現資料根名稱（`soul`、`personal_drive`、`ui_sessions.db`）時判成 Touka 形狀、不服務；佔著 stdout 的殘留行程會讓那條指令跑到 `max_seconds`。執行器停掉或重啟時，進行中的指令失敗，健康檢查恢復前之後的指令都要人核准（IDE 聊天面板不顯示審批卡，這段時間 Auto／YOLO 的指令要等 180 秒才被拒）。代理人指令被 OOM 殺掉時拿到 137、伺服器與 `boot_id` 不變，但 Docker 的 `State.OOMKilled` 會變成 true（容器內任何行程被 OOM 殺就標，容器沒停、照常服務；U2j 實測）。
18. 設定讀不到（U2i）：修好權限後不會自動恢復（auth_policies 只看 mtime、autonomy 沒有熱重載），要重新啟動；autonomy 讀不到時聊天寫「已達總量上限（累計成本）」，實際原因是設定讀不到（側欄提示有寫）；autonomy 讀不到時 `GET /v1/autonomy` 回暫代的最嚴值，安全設定頁整份送回，`update_config` 照寫並清掉 degraded：原檔（含審查員設定）被暫代值蓋掉、提示消失，之後一直「已達總量上限」沒有原因說明（只會更嚴；恢復要從備份還原或逐項改回再存；程式防呆留後續）；identity 讀不到時全部 Base，比 Base 更少權限的自訂角色會被抬到 Base；auth_policies 熱重載失敗時預設風險表把 research 當唯讀；壞 JSON 不在範圍（auth 啟動壞檔是空黑名單預設、autonomy 壞檔是預設）；群聊白名單與 MCP 設定讀不到時設定頁顯示空白，照著存會蓋掉原檔；MCP 設定讀不到時前端只顯示通用錯誤；權限熱重載失敗和 auth_policies 讀不到同時發生時，畫面提示被蓋掉。
19. 驗證的缺口：GitHub 上的 `isolation.yml` 還沒真的跑過（要等 PR 推上去），GHCR 形狀的實際啟動（subpath＋nocopy、`create_host_path: false`）與 MPTCP 覆寫的正向只能在那裡驗；從 repo 根目錄的真 compose 起執行器沒做過（U2j 用 `tests/docker/isolation.compose.yml`，執行器服務和正式設定逐鍵相同，由 `test_executor_e2e_assets.py` 核對）；維護者測試環境的核心是 ABI 7、沒有 MPTCP，執行器在 ABI 3 只有 `fs` 那條分支沒有真核心證據；經演化 API 在執行器裡建置（只有驗證段有 Linux 測試、代理人指令形狀的 `python3 -m venv` 有 U2j 實測）、`sys.prefix` 不一致的反向案例沒有端到端（有單元測試），執行器連外網（pip、npm、git clone）與每條指令多出的時間沒有實測；沒有靜態型別檢查（環境沒有 mypy／pyright），依路徑載入的 kernel 靠執行時符合性測試（第 3 次複審起也核對參數名稱與種類）。突變工具的路徑長度問題由 R4c 修好；C2-3 的 U3b 有 4 項突變的執行器測試部分要在 C2-3 整合時重跑。U2j 已補上的：本機 Docker 帶 `--init` 時 pytest 不是 PID 1，「沒設 subreaper」的突變會紅；`boot_id` 換掉後重連、升級順序、OOM 都在維護者測試環境的 Docker 實測過（見「驗證」）。
20. C2-1 的已知限制都延續（SMC、UDP、`/proc/*/cmdline`、pty、`.touka`、原生 Linux 沒有信任根、DNS 等）；執行器裡看不到 proxy 的行程，`/proc` 這點改善。
21. **執行器的 state 根（`executor_state`）或 ipc 根判 `private_dir`**（設計 §N11 漏列，U2c 實作時已列；第 3 次複審 F1 後縮小）：權限位元被改（777、000、加群組或其他人位元）已不會造成，執行器在啟動時與每 5 秒自己收回（見「執行器修自己的私有資料夾」）。還會出現的只有：(a) 根不是 uid 1001 擁有（舊映像先建的 `0:0 755` volume、有人用 root 容器 chown 過）；(b) 根是符號連結（只有命令列設錯才會，named volume 的掛載點不會）；(c) 根不存在；(d) ipc 根有群組或其他人位元，同時設定還有別的錯（這時什麼都不修，而且 log 寫的是 `private_dir`、不是那個別的錯：`_claim_ipc` 先判；修好別的錯後重啟就會先收回再服務）。執行器裡的代理人做不到 (a)(b)：同 uid、沒有 chown 能力、根是 volume 掛載點。會怎樣——保持存活但不服務；Touka 判 `executor_unreachable`，Auto／YOLO 每條指令都要人核准、無人在場拒絕，核准後在 Touka 主容器內以檔案隔離執行（fail-closed，不是放寬）。healthcheck 的角落：ipc 根是 (a) 或 (b) 時伺服器不碰不可信的 ipc，舊的 `ctl.sock` 與 `alive` 留著，`docker ps` 會先顯示 healthy 約一分鐘（R3a 實測 `0:0 755`：第 6～67 秒）才變 unhealthy，所以要看 log 或側欄橫幅，不能只看 healthy；state 根是 (a) 時不會先顯示 healthy。怎麼恢復——`docker compose logs touka-executor` 會看到 `Executor is not serving: private_dir`；先 `docker volume ls` 確認名稱，對有問題的 volume `docker run --rm -v <volume>:/s alpine:3.20 chown 1001:1001 /s`，再重啟執行器，權限位元由伺服器自己收回（R3a 實測：只 chown、權限留 755，重啟第 6 秒 healthy、變 0700、在服務）。不寫「刪掉 volume 重建」這條路：停掉的容器仍占用 volume，`docker volume rm` 會回 `volume is in use`，而且沒有實測（第 4 次複審審查者③）。
22. 【推測】Docker Desktop 上 `./data` 是 9p（drvfs）：對「已刪除但還開著」的檔 `ftruncate` 回 ENOENT（U2j 探測：9p 上 `TemporaryFile().truncate()` 得 `FileNotFoundError`，tmpfs 正常），代理人在自己資料夾用這種暫存檔（例如 `tempfile.TemporaryFile(dir=".")`）會失敗。這是 9p 本來的限制，不是這一批造成；預設 TMPDIR 在執行器的 state（named volume），平常不會遇到。U2j 的 Linux 模組在 9p 上有 8 項因此（與 9p 不分大小寫）失敗，全部是更嚴（指令做不完或測試資料建不起來），改成不經這兩個前提的診斷版都通過。
23. 執行器部署下綁專案的卡片：原因一律是 `project_not_in_executor`（`level_for` 在範圍不合時不掃專案）；卡片的降級去處另外依專案能不能授權算（`card_isolation` 的 `authorize_derived_path`），專案往下有 `soul` 等資料根名稱、在 Touka 目錄的受保護位置、或大專案掃描逾時（`scan_limit`）時去處是「未隔離」，但原因那行不會寫是哪一種。只影響說明文字，核准與執行都照卡片的去處。
24. 【從程式讀出，沒實測】執行器重啟那一刻，讀寫掛載根（`personal_drive`／`mcp_servers`）剛好讀不到（例如代理人改成 000）：`config_problem` 列不出內容就當成 Touka 資料夾，判 `touka_root`；啟動時不先修讀寫掛載根（見「執行器修自己的私有資料夾」最後一點），而且啟動失敗後不會自己重試，要再重啟一次執行器。proxy 的定期自癒約 60 秒內會把那個根的擁有者位元補回，所以等一分鐘再 `restart touka-executor` 就好；執行中被改成 000 時伺服器照常服務，START 前的路徑鏈修復、伺服器約每 60 秒的掛載根修復與 proxy 都會補回。
25. 網路分開（決定 3 的結果，第 4 次複審審查者② M2）：執行器只接 `touka-exec-net`、proxy 只接 `touka-net`，兩個網路不相通。合併前終端指令和 MCP 伺服器都在 proxy 容器、共用同一個 `localhost`；現在代理人在執行器裡起的服務（例如開發伺服器），proxy 端的工具與 MCP（例如 Hub 的 Fetch、瀏覽器類 MCP）連 `localhost:埠` 會被拒，反過來也一樣，模型可能以為服務沒起來而重試。另外，指令一結束，它留下的行程（含 `nohup`、`&`）就被 subreaper 收掉（U2j 端到端「孤兒被收」），所以這類服務只活在啟動它的指令裡（終端背景模式最多 600 秒）；合併前在 proxy 容器裡會一直跑到重啟【推測：從程式讀出】。現在可行的只有在執行器裡的指令之間測（同一條指令起服務再 curl，或背景模式起服務、另一條指令 curl）【推測：從程式讀出，沒實測】；讓 proxy 端看到代理人的服務目前不支援，C2-3 之後再評估。不接通兩個網路：接通後執行器連得到 Touka 內部服務。
26. 【推測：從程式讀出】執行器裡的家目錄依 agent ID 放在 `touka-executor-state`（proxy 本機那一層在 `touka-sandbox-state`），不在 data、不進備份、`up --build` 也不清：刪除靈魂、重置或清除記憶的流程都不碰這兩個 volume，同 ID 的新靈魂會沿用舊的家目錄（例如命令列工具存在 `~` 的權杖）。要清就照 README_QUICKSTART 第 2 節清快取；自動清理留後續。**補記（2026-10-21）**：刪除靈魂時已會清（`services/isolation/home_wipe.py`，決定見「執行器」一節最後一點）：記憶檢查後、刪任何東西前，proxy 那一層直接刪（唯讀的也刪、連結不跟）；執行器那一層走既有 START 跑固定清除指令；預期是執行器卻不在 READY 或沒清乾淨就 503 `SOUL_SANDBOX_HOME_NOT_CLEARED`、什麼都不刪（fail-closed）。選刪除時、不選建立前：權杖不比靈魂活得久；刪除只有一個入口，已有 admin 密碼步升；建立前清的話 ID 沒人重用時檔案一直留著，建立也要依賴執行器。仍留著的：重置與清除記憶不清（重置報告的保留清單照實列出）；補記之前刪掉的 ID 要手動整個刪（先確認沒有現存的靈魂或自訂模型用同一個 ID）：`docker run --rm -v touka_touka-executor-state:/s alpine:3.20 rm -rf /s/agents/<id>`，proxy 那一層換成 `touka_touka-sandbox-state` 與 `/s/proxy/agents/<id>`（volume 名稱先用 `docker volume ls` 確認，見 README_QUICKSTART）；刪除當下還在跑的指令可能寫回，proxy 那一層補權限時也有檢查與動作之間的時間差；從沒在執行器跑過指令的靈魂會在執行器留下空的 `agents/<id>`；名稱不是安全路徑段的 ID 本來就沒有家目錄，直接略過；自訂模型（只用 proxy 那一層）刪除與改名時不清，而且和同 ID 的靈魂共用同一個家目錄（原本就有）。

## 給部署者：升級與回復（白話）

以下假設從原始碼用 docker compose 跑（repo 根目錄的 `docker-compose.yml`）。C2-1 與 C2-2 是同一個 PR，下面是兩段合起來的步驟；C2-3（MCP 的隔離）是之後另一個 PR，有自己的步驟。指令都在 Touka 資料夾（平常跑 compose 的地方）執行，帶上你平常會加的 `-f` 檔。

**升級**（合併時還沒從 repo 根目錄的正式 compose 起過執行器；第 4 步的 log 一定要看）：

1. 先備份 `data`、`.env`、`ToukaProxy\config`、`ToukaHeartbeat\config`，再取得新版程式。核心沒有 MPTCP 時，Touka 資料夾的 `docker-compose.override.yml` 裡不要有 `net.mptcp.enabled`（容器會起不來）。
2. 看核心版本：`docker run --rm alpine:3.20 uname -r`。6.12 以上完整；6.7～6.11 可以隔離、但信號沒隔開；6.2～6.6 埠擋不住（執行器照樣跑，側欄會標示）；更舊的不算隔離，代理人指令在 Auto／YOLO 都要你核准。核心有 MPTCP，或之後卡片或側欄寫 `net_mptcp`（Docker Desktop 更新核心後可能會變）時，在覆寫檔的 `touka-proxy` 與 `touka-executor` 兩個服務的 `sysctls` 都加 `net.mptcp.enabled: 0`。
3. `docker compose up -d --build`。這一步會：重建映像（名稱改成 `touka-proxy:local`，隔離程式放在映像裡唯讀的位置）；多一個常駐容器 `touka-executor`（代理人的指令改在這裡跑）；多三個 volume——`touka_touka-sandbox-state`、`touka_touka-exec-ipc`、`touka_touka-executor-state`（代理人的 HOME、暫存、快取與執行器的控制通道；不在 data 裡、不進備份）；多一個網路 `touka_touka-exec-net`。舊映像 `touka-touka-proxy`（約 2.5 GB）從此不用，要不要刪由你決定。`.env` 裡還沒有 `HEARTBEAT_TOKEN`（從沒啟動過）時，照 `README_QUICKSTART.md`「從原始碼建置」分段。以後每次更新也一樣用 `docker compose up -d --build`：proxy 直接讀資料夾裡的程式、執行器跑映像裡的程式，只重啟或沒加 `--build` 時，若那次更新改到隔離或執行器的程式（`kernel_outdated`、`executor_version_mismatch`），代理人的指令全部要你核准、心跳裡的指令被拒，卡片可能寫「未隔離」。
4. 確認：約 90 秒內 `docker compose ps` 看到 `touka-executor` 是 healthy（剛起來顯示 starting，實測約 6 秒變 healthy，最久一分多鐘：它要先做兩次自我檢查），**而且** `docker compose logs touka-executor` 沒有 `Executor is not serving`（ipc volume 擁有者不對時會先假 healthy 約一分鐘；有那一行照下面「出問題時」）；【推測】萬一報找不到 `touka-proxy:local`，再跑一次 `docker compose up -d`。聊天分頁先重新整理（舊頁沒有隔離橫幅，核准任何有隔離標示的卡都會回 422），再看側欄沒有紅色的隔離橫幅，在 Manual 下讓代理人跑 `id -u; echo $HOME`，看到 `1001` 和 `/app/executor_state/agents/…` 才算在執行器裡。卡上沒有標示不能當證據。
5. 不用改任何設定檔。執行器的資源上限（全部代理人共用 2g／512／2）要調時，取消 `.env` 裡 `TOUKA_EXECUTOR_MEM_LIMIT` 等三行的註解改值，再 `docker compose up -d`。

**合併後會不一樣的地方**：代理人的指令在執行器裡跑，看不到金鑰、設定、原始碼和別的代理人資料夾，連不到 Touka 的內部服務與 Docker 控制埠；連不到也搶不到封鎖的埠——Touka 自己的 8000、8082、8083、3001、Docker 的 2375／2376，以及從設定推得的本機與區網模型閘道與 MCP 設定裡的遠端伺服器（例如 LiteLLM 4000、Ollama 11434；不論有沒有啟用、Touka 開機時它有沒有在跑；網址的主機要寫成 IP、`localhost`、沒有點的名稱或 `.local` 這類結尾才算，`nas.lan` 這類區網網域名不擋），自己起在這些埠的服務要換埠（終端會提示），80／443 不擋；`~` 換到執行器的暫存區，第一次用 npx／uvx 會重新下載；執行器裡沒有代理伺服器與自訂憑證的設定（見已知限制第 16 點）。綁專案的對話、DebugAgent 等自訂模型：Auto／YOLO 每條都要你核准，核准後在 Touka 主容器內以檔案隔離執行，心跳與無人在場時被拒；專案往下 3 層有叫 `soul`、`personal_drive` 的資料夾或 `ui_sessions.db`、專案在 Touka 資料夾裡卻不是你自己建的頂層資料夾、或大專案第一次綁定時 2 秒內掃不完（`scan_limit`，Docker Desktop 上約 1,500 個資料夾；背景掃完後恢復，10 分鐘的快取過期後可能再遇到一次）時，核准後是「未隔離」（卡片標題會寫，原因那行只寫專案沒掛進執行器）。沒綁專案的對話（心跳的家對話、IDE、SNS）只看得到代理人自己的資料夾；`/debug/execute` 只有代打成靈魂時才在執行器裡跑，用人類自己或 DebugAgent 的身分時被拒（資料夾不在執行器裡、又算無人在場）【推測：從程式讀出】。代理人在執行器裡起的服務，Touka 的工具與 MCP（例如 Fetch）連不到（已知限制第 25 點）。合起來：心跳、SNS 這些你不在場的執行裡，代理人不能在任何專案跑指令（測試、git、建置都不行），只能在自己的資料夾；讀寫檔案的工具不經隔離，照常可用。YOLO 無人在場（心跳、SNS、`/debug/execute`、子代理——即使你人在聊天裡）時高風險的 MCP 工具會被拒；建 MCP 的兩個工具卡片寫「未隔離」，YOLO 下也要你核准（Auto 本來就要）。**MCP 伺服器（Hub 第三方、自建、演化部署的）在 C2-3 合併前和今天一樣沒有隔離**：環境變數裡沒有金鑰，但讀得到 `.env` 檔與 Touka 的資料；演化部署過的 MCP，代理人之後再演化同一個能力就能改寫它的程式（YOLO 下不用你核准），下次那台 MCP 啟動時用 Touka 的權限跑。代理人工作區被改權限時 Touka 會自己補回；Touka 自己的資料夾不會（見下面「出問題時」）。安全規則檔（`auth_policies.json`）啟動時讀不到時，所有工具和指令都拒絕、代理人只能聊天；自主設定（`autonomy.json`）讀不到時自主執行停止（這時不要在安全設定頁按存檔，見下面）。兩種情況側欄都會寫原因與恢復方法。

**出問題時**（在 Touka 資料夾執行，帶上平常的 `-f`）：

- data、soul、config、`hf_cache` 被改成讀不到（只有你核准、卡片寫「在 Touka 主容器內以檔案隔離執行」的指令做得到）：`docker compose run --rm touka-init`，再 `docker compose restart touka-proxy`。容器自動重啟不會跑 `touka-init`；在 Windows 檔案總管改檔案屬性沒有用。
- `/app` 被鎖：`docker compose up -d --force-recreate touka-proxy`（`/app` 的權限在容器裡，只有重建容器才修得到）。
- 安全規則檔或自主設定讀不到：修好或還原那個檔（權限用上一條的 `touka-init`），再 `docker compose restart touka-proxy`。自主設定讀不到時，安全設定頁顯示的是暫代的最嚴值，**不要按存檔**：會用這些值蓋掉原檔（審查員設定等一起不見）、提示消失，之後一直「已達總量上限」；已經存了就從備份還原這個檔，或在安全設定頁逐項改回再存。`connections.json` 讀不到或寫壞：修好之後約半分鐘自動恢復，不用重新啟動；有一條連線的網址是空的也算讀不到，在連線頁補上網址或刪掉那條再存檔。
- 系統還原失敗後（Touka 沒有自己重啟）自訂模型不能用：按一次重新掃描，或 `docker compose restart touka-proxy`。
- 執行器一直不是 healthy：`docker compose logs touka-executor` 會有一行 `Executor is not serving: <原因>`；照卡片或側欄寫的做，通常是 `docker compose up -d --build`。`docker ps` 顯示 healthy 不一定代表在服務（控制通道的 volume 擁有者不對時，前一分鐘左右會先顯示 healthy），以 log 或側欄橫幅為準。
  - 原因是 `private_dir`：代理人改權限造成的，執行器會自己收回；剩下的是 volume 的擁有者不對（例如舊映像先建過、或有人用 root 容器 chown 過）。先用 `docker volume ls` 確認名稱（打錯時 `docker run -v` 會新建一個空的 volume，什麼都沒改），對有問題的那個（`touka_touka-executor-state` 或 `touka_touka-exec-ipc`）執行，再重啟執行器（實測有效）：

    ```powershell
    docker run --rm -v touka_touka-executor-state:/s alpine:3.20 chown 1001:1001 /s
    docker compose restart touka-executor
    ```

  - 原因是 `touka_root`、第一次啟動：多半是 `data/personal_drive` 或 `data/mcp_servers` 第一層有 Touka 資料的名稱（`soul`、`personal_drive`、`ui_sessions.db`，不分大小寫），把那一項改名或移走，再 `docker compose restart touka-executor`。
  - 原因是 `touka_root`、而你剛重啟過：可能是代理人剛好在那一刻把自己的資料夾根改掉了權限。等一分鐘（Touka 會修好）再 `docker compose restart touka-executor`。

**沒有重建時**：只有 proxy 重啟、compose 沒重新套用時，proxy 沒有執行器的設定，回到第一段的行為：原因是 `kernel_not_installed` 或 `kernel_outdated`（舊容器沒有暫存區時是 `sandbox_state_unconfigured`），而這時聊天頁一定還是舊版（`touka-chat` 是建好的映像）：舊版卡片沒有隔離標示、看起來和以前一樣，按核准卻回 422（`approval_flow` 對層級 `none` 的卡一律要求 `isolation_ack`）、指令不執行；Auto／YOLO 也要核准、心跳裡的指令被拒，等於代理人指令在所有模式都不能跑。新的 compose 搭舊映像時，執行器起不來或一直重啟，但不影響 Touka（原因 `executor_unreachable`）。重建一次並重新整理分頁就好。

**回到這一批之前**：

1. 切回舊版程式。
2. `docker rm -f touka-executor`（不要用 `--remove-orphans`，理由同 C2-1）。
3. `docker compose up -d --build`。
4. 會留下三個 volume、網路 `touka_touka-exec-net` 與映像 `touka-proxy:local`，裡面只有代理人的快取與控制通道。要刪先用 `docker volume ls`、`docker network ls`、`docker image ls` 確認名稱，再 `docker volume rm …`、`docker network rm touka_touka-exec-net`、`docker image rm touka-proxy:local`；要不要刪由你決定。
5. `data/` 的格式沒有改，不用處理（自癒只補過代理人工作區的擁有者權限位元）。回復後代理人的指令回到和 Touka 同權限執行。

**其他跑法**：GHCR 要 Docker Compose 2.26、Docker Engine 26.0 以上，把 `docker-compose.ghcr.yml` 和 `TOUKA_VERSION` 一起換，先建好 `data/personal_drive`、`data/mcp_servers`，分四段啟動（`touka-init` 隨 proxy → proxy → heartbeat 與 chat → 執行器最後），核心有 MPTCP 時覆寫要同時加在 `touka-proxy` 與 `touka-executor`、每段指令都帶那個 `-f`（見 README_QUICKSTART）。只換版本號時沒有暫存區與執行器、Fast Open 也沒關（`sandbox_state_unconfigured` 或 `net_tcp_fastopen`），情況同「沒有重建時」。原生 Windows 開發模式一律未隔離：Auto／YOLO 每條代理人指令都跳卡，心跳裡的指令、演化、驗證指令在所有模式都被拒，IDE 聊天面板要等 180 秒才被拒。

## 驗證

- 第 2 次複審後的修正單位（FA～FD，各自從 `c27699db`）：全量 pytest 都只多出自己的新測試（基準 10086 passed／8 skipped／6 failed，6 個 E1）；Docker 上 Linux 真核心 hardened 與開發版全過，突變全部變紅。
- 整合後（`75a3306f`）：全量 pytest 10201 passed／8 skipped／6 failed（6 個 E1；比基準多 115＝FA 21、FB 29、FD 18、FC 43，加兩項突變搬進共用清單多出的 4 個參數化案例）；Linux 真核心（`--sysctl net.ipv4.tcp_fastopen=0`、直接指定 `tests/integration/test_isolation_linux.py`）hardened 與開發版各 44 passed；Docker 突變 27 項，基準綠、全部變紅；ToukaChat Vitest 全過（`ToolApprovalCard.test.tsx` 46 個）。差異複審與 `guard_all` 的結論在 FE 之後補上（見下一點與「對抗式與設計審查」）。
- 第 2 次複審後的補修正 FE（從 `7b9ec7a5`）：全量 pytest 10221 passed／8 skipped／6 failed（6 個 E1；多 20＝status 8、啟動器純函式 2、突變清單 5 項的參數化 10）；Linux 真核心加 sysctl、設 REQUIRE 的 hardened 與開發版各 46 passed，Docker 突變 32 項基準綠、全部變紅；不加 sysctl、沒設 REQUIRE（模擬 GitHub contracts job 原生執行；只是模擬，不能代替真的 runner）29 passed／17 skipped，跳過都寫了原因，網路相關的新案例（自測回報 `net`、原因不是 `fs_9p`、核准的隔離降級讀不到受保護路徑）都通過；不加 sysctl、設 REQUIRE 時 17 failed；ToukaChat Vitest 1713 passed（`ToolApprovalCard.test.tsx` 49 個）。C2-1 收尾（`189035d1`）：`guard_all` 全綠、全量 10221／8／6、Linux 46 passed、突變 32 項全紅。
- C2-2（各單位先寫會紅的測試，每項附突變）：整合 10（`afaf46d7`）全量 pytest 12422 passed＋1 個已知不穩（單獨重跑通過）／8 skipped／6 failed（6 個 E1）；之後合入 U2i、U2e 各一個補件（`d1de93b4`）。Linux 真核心加 sysctl、設 REQUIRE 112 passed，不加 sysctl 0 failed／94 passed／18 skipped（跳過都帶標籤）；維護者測試環境的 Docker 照 workflow 原文跑端到端：執行器 11 項（層級、uid 與 HOME、控制通道碰不到、讀不到別人資料夾、連不到內部服務、封鎖埠連線與 bind 都是 EACCES、沒有 docker.sock、relay 被殺後遠端 2 秒內停、`max_seconds`、孤兒被收、git 沒有 dubious ownership）與 proxy 本機 Landlock 2 項都通過；ToukaChat Vitest 1831 passed。整合 9 合入 dev `01174358`（h2〔ADR-0269〕與記憶系統第二批）時補了 ADR-0269 要求的 D1（被擋的呼叫不進 `policy_level`、不寫 `consent_ledger.record_policy`，4 個測試）與突變 M25（核對移到 `policy_level` 之後，D1 三格轉紅）。第 3 次對抗式複審見「對抗式與設計審查」。
- 證據來源（照實）：維護者測試環境的核心沒有 MPTCP、Landlock ABI 是 7，所以 MPTCP 的「網路設定不安全」與「執行器在 ABI 3 照樣判 `executor`、只有 `fs`」在那台機器上**沒有真核心證據**，只有單元測試、Linux 模組在不加 sysctl 時的 Fast Open 案例與 CI checker；Fast Open 那條由 U2j 在執行器打開 `tcp_fastopen` 模擬過（見下）。從連線推導的上游埠（B）由 U2j 在維護者測試環境的 Docker 補上真核心證據。`.github/workflows/isolation.yml` 還沒在 GitHub 上跑過。
- U2j 結果（維護者測試環境的 Docker，`d1de93b4`，程式和 `60956fda` 相同；用 `touka-iso-*` 的一次性容器與測試 compose，沒有 Touka 容器、沒掛 data 與 `.env`、沒打付費 API、下載約 0.5 MB）：
  - Linux 模組：tmpfs 上 hardened 與開發版各 112 passed；帶 `--init`（pytest pid 7）112 passed，伺服器 `PR_SET_CHILD_SUBREAPER` 改 0 的突變當時記為讓兩個 setsid 案例變紅（pytest 是 PID 1 時同一個突變存活）；這一項的突變 id（`server_subreaper_off`，20 字）讓 basetemp 變長、執行器 socket 可能超過 100 bytes 而不服務，可能是假紅，改以 R3a 直接 `docker run` 的 `plain-subreaper-off-init` 與 R4c 的重跑為準（見下）。C2-1 的 32 項突變在 C2-2 程式上 32/32 變紅（R4c 修好工具後，清單 33 項重跑全紅）。
  - 9p（`./data` 那種掛載）上 104 passed／8 failed（112 項的版本；第 3、4 次複審後新增的 8 項沒在 9p 跑）：8 項都是更嚴（指令做不完或測試資料建不起來，沒有一項放行），原因只有兩個，都是 9p（drvfs）本身的特性——對已刪除但還開著的檔 `ftruncate` 回 ENOENT（見已知限制第 22 點）、名稱不分大小寫；把受測程式複製到 tmpfs、只改這兩個前提的診斷版在 9p 上 8 passed（含全部安全斷言）。U2n 自癒、上游埠、記憶系統呼叫處、U2c、U2d、U2e 的 Linux 案例在 9p 上共 52 項全過。
  - 執行器重啟：進行中的指令約 2.1 秒後結束（125、`executor_unreachable`）；健康檢查快取還沒過期時下一條是 `executor_restarted`、不送 START；refresh 後 `boot_id` 換新、層級 executor、指令 exit 0。
  - 升級順序：執行器先用舊映像時起不來，兩個 volume 根是 `0:0 755`，client 判 `none`／`executor_unreachable`；換新映像 5 秒內 healthy，volume 依映像 copy-up 成 1001:1001 0700，層級 executor。
  - OOM（記憶體與 swap 各 256 MiB）：代理人指令（`oom_score_adj` 500）約 448 MB 時被殺、code 137；伺服器（0）與 `boot_id` 不變，下一條指令 exit 0，容器 Running、RestartCount 0；Docker 的 `State.OOMKilled` 變 true。
  - 啟動器三項設定在隔離內實測：NoNewPrivs 1、`oom_score_adj` 500、umask 0022；經 proxy 本機 Landlock（client 自己 nnp 0、umask 077）也一樣，證明是啟動器設的、不是從容器繼承。
  - peer：跨容器連進來 `peer_pid` 0（同容器 8），和執行器同一個 pid namespace 的 client 被擋（`executor_unreachable`）。READY 的兩個 digest 和 proxy 算的相同；兩邊 `sys.prefix` 相同；proxy 的可信直譯器整條鏈 uid 0、0755；proxy 的 ST 沒有多出 `agents`。
  - 網路設定不安全（執行器覆寫 `tcp_fastopen: 1`）：`none`／`net_tcp_fastopen`；在範圍內的去處是 executor（features 沒有 tcp）、綁專案的是 landlock；卡片要 tcp 時拒絕，只要 fs 時核准後在執行器裡跑。
  - 封鎖埠：registry 裡停用的遠端 `10.77.0.5:19123` 在 B，隔離內連 19123 回 EACCES（對照 19124 是 ECONNREFUSED）；寫入新連線 `192.168.77.7:24567` 後 10.0 秒進 B、連線 EACCES，monitor 狀態沒有 apiKey。執行器裡起 8000 埠時終端多一行換埠提示。
  - `private_dir`（F1 之前的程式）：state 根與 ipc 根改 777 後照常服務、維護不收回；重啟後 log 出現 `Executor is not serving: private_dir`、client 判 `executor_unreachable`；兩行 `chmod 700` 加重啟恢復有效。只改子資料夾（`agents`、HOME）時照常服務，伺服器不改回。全新 volume 是 1001:1001 0700。
  - healthcheck：SIGSTOP 伺服器後第 71 秒 unhealthy、SIGCONT 後 3.5 秒回 healthy；SIGKILL 時容器 exit 137（正式 compose 是 `restart: unless-stopped`，測試 compose 是 `no`，沒驗重啟那一步）；F1 之前只有 ipc 根 777 時重啟後第 6～73 秒顯示 healthy 但不服務（只有 state 根 777 時第 82 秒直接 unhealthy）。
- 第 3 次複審後的修正 R3a（`a53d493d`，維護者測試環境的 Docker，下載 0）：Linux 模組加 sysctl、設 REQUIRE 118 passed（pytest pid 1），再加 `--init` 118 passed（pid 7），不加 sysctl、不設 REQUIRE 0 failed／100 passed／18 skipped；R3a 自己的主突變清單 14 項全紅（hardened 加 `--init`，跑一次；這份不是 repo 的 33 項清單，後者由 R4c 修好工具後全跑、33 項全紅），拿掉 NoNewPrivs 在開發模式紅；`server_subreaper_off` 在突變腳本裡有沒有 `--init` 都紅（腳本是 PID 1），直接 `docker run` pytest 沒加 `--init` 時存活、加了紅（`plain-subreaper-off-init`）。R4c 盤點：`f1_nofollow_removed`、`f5_server_subreaper_off` 整項，`f1_private_strip_removed`／`f1_start_precheck_removed`／`f1_maintain_private_removed` 的執行器測試部分，以及對照項 `expect_survive_subreaper_without_init`（突變腳本自己是 PID 1、pytest 是它的子行程，所以這一項在腳本裡本來就預期會紅），當時 basetemp 帶突變 id、執行器 socket 超過 100 bytes 而不服務，可能是假紅；R4c 修好後重跑全紅、結論不變（`f1_nofollow_removed` 紅的是兩個符號連結案例，不是自己擁有的兩案照樣綠）。F1 測試 compose 端到端：執行中把兩個根改成 777，7 秒後都是 700、仍在服務；停機時改成 777（兩個或只改一個），重啟第 6 秒 healthy、沒有 not-serving log、client 判 executor；ipc 根是 `0:0 755` 時仍判 `private_dir`，第 6～67 秒顯示 healthy 但不服務；只 `chown 1001:1001`、權限留 755，重啟第 6 秒 healthy、根變 0700、在服務。全量 pytest 12456 passed／8 skipped／6 failed（6 個 E1）。
  - 其他：三份 compose `config -q` exit 0；runner 加 git 層後 `test_git_does_not_climb_into_the_touka_checkout` 的 git 那一半在 tmpfs 與 9p 都有跑到並通過；代理人指令形狀在執行器裡 `python3 -m venv` 成功（pip 下載因為沒網路做不到）。
  - 沒驗的（只能在 GitHub CI 或要補資產）：GHCR 形狀實際啟動、MPTCP 覆寫的正向；演化 API 在 compose 形狀的建置、`sys.prefix` 不一致的反向案例。
- 第 4 次複審後的修正 R4a（維護者測試環境的 Docker）：Linux 模組 +2——重啟時 state 或 ipc 是 000 也先改回 0700 再服務（以前會是 `ipc_lock`、不服務），執行中 `ctl.sock` 被改成 666 時 5 秒內收回 0600；加 sysctl、設 REQUIRE 120 passed，再加 `--init` 120 passed，不加 sysctl、不設 REQUIRE 0 failed／102 passed／18 skipped。000 只有這組真核心證據，測試 compose 的端到端只做過 777。CI 容器 job 改路徑篩選（`tests/lint/test_isolation_ci_workflow.py` 15 → 18 個案例，先紅後綠）；`SystemNoticeBar` 多一個案例（不要按存檔的提示）。全量 pytest 12459 passed／8 skipped／6 failed（6 個 E1）。R4a 另外發現突變工具有路徑長度問題（見下一點）；R4a 的 a3 三項以短 id 的 mut2 與 R4c 的長 id 重跑為準。R4b 只改 `.md`。
- 第 4 次複審後的修正 R4c（突變工具，`bcefe537`）：突變腳本每一輪的 basetemp 原本帶突變 id，id 長的突變讓執行器 socket 路徑超過 100 bytes、伺服器不服務，執行器相關的測試因此失敗，看起來像「變紅」。改成每一輪的 basetemp 只用序號、長度和 id 無關，執行器 socket 留 8 bytes 餘裕仍不超過 100（`tests/unit/test_isolation_docker_assets.py` +2，先紅後綠）。在綠的基準上重跑：清單 33 項全紅（hardened 加 `--init`，跑一次），Linux 模組 120 passed；第 3、4 次複審後受影響的項目（f1 三項的執行器測試部分、`f1_nofollow_removed`、`f5_server_subreaper_off`、R4a 的 a3 三項、對照項 `expect_survive_subreaper_without_init`〔突變腳本是 PID 1，本來就預期會紅〕）重驗都紅、結論不變；長 id 的對照組現在是綠的。C2-3 的 U3b 有 4 項突變的執行器測試部分也受同一個問題影響，要在 C2-3 整合時重跑（屬 C2-3）。
- merge dev（P4b-1，`9eb573ee` 合入 dev `ef0bb04f`：r55〔ADR-0271〕與 dep1〔mcp 2.2.0〕）：產品程式自動合併；文件衝突在本分支解；`openapi.json`、`api.ts` 在沙盒重產。r55 的送前最壞花費估計（`would_exceed`）在 autonomy 讀不到、進 degraded 時回 True（最嚴值退回表把上限閘開在 0），比 dev 自己讀不到時更嚴（經審查確認）；`test_config_unreadable_fail_closed_security.py` +2 釘住（`aa7d5d1d`）。突變 M1～M3 紅；M4（拿掉 `complete_once` 的 `would_exceed` 閘）預期存活、不算防線缺口：degraded 時第一道 `is_over_budget`（已花 ≥ 0.0）先擋、回 `over_budget`，`would_exceed` 這一層由 M2 的單元斷言（`would_exceed` 一律回 False 會紅）覆蓋。全量 pytest 12794 passed／8 skipped／6 failed（6 個 E1）／3 xfailed（dep1 留給 life1 的 `test_mcp_midflight_transport_failure.py` strict xfail）；ToukaChat Vitest 1852 passed。Linux 模組在 dev 合入後的程式（`aa7d5d1d`）上跑 hardened 120 passed，用既有的 `touka-iso-test-runner:u2j`（映像的 venv 是 mcp 2.0.0；隔離行為不經 mcp，合入後的程式在 mcp 2.0.0 的映像裡匯入正常〔conftest 會匯入 `src.main`〕），不需要重建映像；`--init` 120、不加 sysctl 0 failed／102／18 與突變清單 33 項全紅是 dev 合入前的程式（R4a `a5c21f2a`、R4c `bcefe537`；這段 dev 沒動隔離程式）。
- ADR 建檔與換號（P4b-2，只改文件與註解、docstring）：全量 pytest 12796 passed／8 skipped／6 failed／3 xfailed（`299c3dd4`）：6 個 failed 是 E1，8 個跳過與 AGENTS 列的相同，3 個 xfailed 是 dep1 留給 life1 的 strict xfail；比 P4b-1 的 12794 多的是 R4c 的 2 個。
- 第 3 次複審後的文件修正 R3b（`37075b55`）、R3c（`93f4b262`，合入後是 `e5e15157`）只改 `.md`：lint_ssot、#19、#28、#38～#40 exit 0，文件相關 20 個測試檔 944 passed。R3c 之後程式沒變，`e5e15157` 本身沒有另跑全量（程式的全量是 R3a 的 `a53d493d`：12456／8／6；R4a 之後見上面 R4a 那一點）。

### 對抗式與設計審查

審查者都是全新上下文、唯讀，分三個角度：①找繞過、②原本能用的功能、③跨平台／取消／部署。審查找到的問題都已修正，結果如下：

- 細部設計（C2-1）與 C2-2 設計在寫程式前就經多次審查，嚴重問題都在寫程式前抓到；「永不授權」規則因此修了三版（第 1 版太寬、第 2 版太窄、第 3 版讓 Touka 資料夾裡的專案跑不了 pytest），第 4 版通過。
- C2-1 實作（`c27699db`，第 2 次複審）：實測證實「`create_mcp`、`scaffold_mcp` 卡片寫隔離降級、其實未隔離」與「專案資料夾換成連結就拿到別人資料夾」兩個問題，已修正。修正後的差異複審中，攻擊角度（①）沒有得出判決，攻擊面只由防線對照表與③的獨立核對支持，**這一次沒有獨立的攻擊角度審查**。
- C2-2 實作（`60956fda` 第 3 次複審、`e5e15157` 第 4 次複審）：①改用防禦清單核對（先自列約 90 條防線再對照 146 列的防線表，另做 19 項與 18 項突變；第 4 次的 18 項是 16 紅、1 等價、1 只有 Linux 擋得住）；三個角度都沒有 BLOCKING／HIGH，也確認沒有放寬已核准的決定、翻轉的既有期望都更嚴或等價。指出的問題已修正：R3a 修執行器收回自己私有資料夾的權限、背景工作接所有例外、補測試與突變、CI 容器 job 條件、`.gitignore`；R4a 修 CI 路徑篩選、說明與註解、Linux 案例、提示文案、`.env.example`；R3b、R3c、R4b 改文件（MCP 被誤會已隔離的句子、網路分開的告知、刪 volume 那條恢復方式）。
- merge dev 已完成（見「驗證」；dep1 改的 `protocols/manager.py` 是 MCP manager，這個 PR 改的是 `services/models/manager.py`，不同檔）。`guard_all` 43/43 通過（`20e59c73`，2026-10-16；P4c 只改文字與兩句翻譯，開 PR 前在新 head 上再跑一次）。合併前還要：GitHub 上 `isolation.yml` 的 `landlock` 與 `container` 兩個 job 在審過的 head 上成功（這個 PR 改到隔離路徑，`container` 會自動跑；`landlock` 會在全新的 venv 上再跑一次 Linux 模組）。dev 合入後的 Linux 模組已經跑過（見「驗證」），不需要重建本機測試映像。

## 回復

不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。這個 PR 的兩段要從後往前回：C2-2 → C2-1；C2-3 的 PR 已經合併時，先照 C2-3 的 ADR 撤 C2-3（它建立在執行器與 runner 上）。C2-1 的產品、測試、Dockerfile 與 compose（含 `sysctls`）一起撤；只撤 runner 接線、留下決定 2 的規則時，Auto／YOLO 的代理人指令全部要人核准。決定 4 與 F5 別名可以單獨撤。C2-2 撤時，執行器的程式、compose 的 `touka-executor` 與兩個 volume、Dockerfile 的 git 層與預建目錄一起撤；只拿掉 compose 的 `touka-executor` 與 proxy 的 `TOUKA_EXECUTOR_DIR` 時，判定回到 C2-1 的本機 Landlock 層（封鎖埠推導、狀態 fd、可信直譯器、自癒、設定讀不到 fail-closed 仍在）。設定讀不到的 fail-closed 可以單獨撤，但 auth_policies 啟動讀不到時會回到空黑名單。資料格式沒改，不搬資料；留下的 volume、網路與映像由部署者決定是否刪除。撤回時下面的反向註記改回。

## 反向註記（和本檔同一個 commit 補；撤回時改回）

被修訂的四份（狀態區加一句，`adr/README.md` 該列的狀態欄同步加註，照 ADR-0195 那列的先例）：

- ADR-0246：「隔離層級（`none` 時 Auto／YOLO 只有人能核准）、決定 4（YOLO 無人在場的 dangerous MCP 只有人能核准）、F5 的兩個演化舊別名、審查員 `<environment>` 的 `isolation`，見 ADR-0272」。
- ADR-0239：「runner 只改寫啟動參數、啟動器是唯一能 exec 的地方；執行器伺服器也經 broker 啟動啟動器，ratchet 不放寬，見 ADR-0272」。
- ADR-0076：「隔離時 rlimit 由啟動器依 spec 套用，直接執行時仍由 preexec，見 ADR-0272」。
- ADR-0262：「隔離邊界不得使用 `child_python_env`／`get_full_env`／`get_clean_env`，見 ADR-0272」。

相關、但有本 ADR 新增的約束（狀態區加一句）：

- ADR-0255：「`.touka/` 在權限自癒白名單內（只補擁有者位元），見 ADR-0272」。
- ADR-0270：「第 7 段的 6 個 ResourceManager 方法列為自癒豁免，快照與記憶庫備份在白名單外，見 ADR-0272」。
- ADR-0254（第 4 次複審審查者①②要求必做）：「缺口 2（第三方 MCP 讀得到 `.env` 檔）寫的『第二批』指的是 MCP 的隔離（C2-3，另一份 ADR）；ADR-0272 只隔離代理人指令，這個缺口在 C2-3 合併前仍在」。理由：ADR-0254 寫「要等第二批的隔離執行環境」，讀者會以為本 ADR 已經補上。

索引：`adr/README.md` 新增一列。
