# Touka 本機安裝指南

本指南供部署者手動安裝整合產品；開發者依賴佈建見 [CONTRIBUTING](CONTRIBUTING.md)。支援範圍是 **Linux amd64 容器、本機 loopback 存取**，已使用過的主機是 Windows Docker Desktop 的 Linux containers；ARM64、macOS、公網及其他主機組合尚未驗證。

本指南的 docker 指令在 PowerShell 與 bash 寫法相同；只有第 2 節複製 `.env` 那一步分成兩種寫法。

## 快速開始（最短路徑）

1. 確認 Docker Engine 26.0 以上、Docker Compose 2.26 以上能執行 Linux amd64 容器，本機 8000、8082、8083、3001 沒被其他服務占用。
2. 從要安裝的版本取得 `docker-compose.ghcr.yml` 與 `.env.example`，放進一個全新的安裝目錄。
3. 在安裝目錄建立 `data`、`data/personal_drive`、`data/mcp_servers`、`heartbeat-config` 四個資料夾，把 `.env.example` 複製成 `.env`，設定 `TOUKA_VERSION`（第 2 節）。
4. 分三段啟動：先 `touka-proxy`，等它 healthy 後再 `touka-heartbeat touka-chat`，最後 `touka-executor`（第 3 節）。
5. 讀出初始管理員密碼，開 <http://localhost:3001> 登入並變更密碼（第 3 節）。

代理人指令能隔離到什麼程度，取決於 Linux 核心版本與網路設定；正式使用前請讀第 1 節的隔離說明。

## 1. 取得映像與前置條件

- 三個映像由 [GHCR 發布 workflow](.github/workflows/ghcr-publish.yml) 建置：`ghcr.io/otohatouka/touka-proxy`、`touka-heartbeat`、`touka-chat`。手動觸發（`workflow_dispatch`）：一律發布 `sha-<commit>`，填入 `version` 時另發布該版號與 `latest`。不用 tag 觸發，因為 ToukaIDE 的 Marketplace 發行使用 `v*` tag。建議在 `.env` 以 `TOUKA_VERSION` 釘固定 tag，避免 `latest` 更新後意外換版。
- 撰寫時（2026-10-22）公開 repo 還沒有 tag 或 release，GHCR 上的 `0.1.0` 與 `latest` 由公開 main 的 commit `62197042` 建置，等同 `sha-62197042…`；之後發布新版時 `latest` 會跟著換。也可以直接用 `sha-<commit>` 釘版，但只用這個公開 repo 裡找得到的 commit。無論選哪個，都從同一個 commit 取 `docker-compose.ghcr.yml` 與 `.env.example`。GHCR 上 `modern-*`、`rollback-*` 開頭的 tag 是開源前的舊發行，不要使用。
- 已安裝 Git；原始碼可直接取得：`git clone https://github.com/OtohaTouka/Touka.git`，不需要登入。
- Docker Engine 能執行 Linux amd64 容器，Docker Compose **2.26 以上**（使用 `env_file.required: false`；執行器掛 skills 用的 volume `subpath` 要 2.26，Docker Engine 也要 26.0 以上：Engine 26.0.0 的 release notes 寫「Add `Subpath` field to the `VolumeOptions`」，2026-10-16 查 <https://docs.docker.com/engine/release-notes/26.0/>）。由部署者確認自己的磁碟與掛載符合 local filesystem 政策；未知、遠端或不符合權限的儲存不可假填 local。
- 使用全新的安裝目錄，確認本機 8000、8083、8082、3001 未被其他服務占用。
- 代理人指令（終端、互審驗證指令、演化）在執行器容器 `touka-executor` 裡以 Landlock 隔離執行，要 Linux 核心開 Landlock（ADR-0272）。先用 `docker run --rm alpine:3.20 uname -r` 看核心版本（Docker Desktop for Windows 用的是 WSL 的核心）：

  | 核心 | 代理人指令 |
  |---|---|
  | 6.12 以上 | 完整隔離：看不到金鑰、設定、原始碼與其他代理人的資料（但看得到別的指令的指令列、改得到同帳號檔案的權限位元，見 `SECURITY.md`），連不到也搶不到 Touka 內部埠、Docker 遠端控制埠與從設定推得的本機與區網上游埠（80／443 除外；上游網址的主機要寫成 IP、`localhost`、沒有點的名稱，或 `.local`、`.internal`、`.localhost`、`.home.arpa` 結尾才算，`nas.lan` 這類區網網域名的埠不擋），信號也隔開。前提是下面的網路設定已關；主機能載入 AF_SMC（`smc` 模組）時，SMC 連線可能不經過埠的封鎖【推測】，見下方「SMC」 |
  | 6.7～6.11 | 同上，但信號沒隔開 |
  | 6.2～6.6（例如 WSL 預設核心 6.6） | 執行器只隔離檔案（埠沒擋、信號沒隔開，側欄會標示）；沒有執行器的跑法（例如原生 Linux）不算隔離：Auto／YOLO 每條都要人核准，核准後只隔離檔案 |
  | 更舊或沒開 Landlock | 不算隔離：核准後未隔離執行 |

  Docker Desktop 的「Expose daemon on tcp://localhost:2375 without TLS」保持關閉（預設關）。Podman 不在支援範圍（認不出是容器，會當成原生執行）。舊版 Docker Engine 的預設 seccomp 設定可能擋掉 landlock 的系統呼叫【推測】，這時一律是 `landlock_unavailable`（未隔離），請先更新 Docker。

  **網路設定（TCP Fast Open 與 MPTCP）**：隔離擋不住用這兩種方式送出的連線，所以它們開著時代理人指令不算隔離（審批卡原因 `net_tcp_fastopen` 或 `net_mptcp`）：Auto／YOLO 每條都要人核准，核准後仍在執行器（或 Touka 主容器的「隔離降級」）裡跑：看不到金鑰與別人的資料，一般連線仍會擋 Touka 的埠，但擋不住這兩種連線；設定改好後 Touka 會自己回到完整隔離（容器要重建，原生 Linux 最多等 10 分鐘）。

  - 出貨的 compose 已替 proxy 與執行器關掉 TCP Fast Open（`sysctls:` 的 `net.ipv4.tcp_fastopen: 0`）。不經出貨的 compose、直接 `docker run` 這個映像時 Fast Open 是開的。
  - 看核心有沒有 MPTCP：`docker run --rm alpine:3.20 sh -c 'if [ -e /proc/sys/net/mptcp/enabled ]; then echo MPTCP=yes; else echo MPTCP=no; fi'`，印出 `MPTCP=yes` 是有、`MPTCP=no` 是沒有。Docker Desktop 更新核心後可能會變，之後審批卡如果寫 `net_mptcp`，照卡片加那一行。
  - 有 MPTCP 時，在安裝目錄另建一個覆寫檔（例如 `touka-mptcp.override.yml`），`touka-proxy` 與 `touka-executor` **兩個服務都**加兩個 sysctl（兩個都寫，避免合併時漏掉 Fast Open）：

    ```yaml
    services:
      touka-proxy:
        sysctls:
          net.ipv4.tcp_fastopen: 0
          net.mptcp.enabled: 0
      touka-executor:
        sysctls:
          net.ipv4.tcp_fastopen: 0
          net.mptcp.enabled: 0
    ```

    沒有 MPTCP 的核心（例如 WSL 6.18）加了 `net.mptcp.enabled` 容器會起不來，所以出貨的 compose 不預設加。從原始碼跑、指令不帶 `-f` 時，把這個檔命名為 `docker-compose.override.yml` 放在 repo 根目錄，`docker compose` 會自動讀；有帶 `-f`（GHCR 跑法一定帶）時自動讀取不生效，**第 3 節的每一段指令（三段 `up`、`ps`、`exec`）都要在 `-f docker-compose.ghcr.yml` 後面再加 `-f touka-mptcp.override.yml`**，例如 `docker compose -f docker-compose.ghcr.yml -f touka-mptcp.override.yml up -d touka-proxy`；漏加的那一段會用沒有這兩行的設定重建 proxy 或執行器。
  - 不用 Docker、直接在 Linux 主機上跑時，要改的是主機的 sysctl，不是 compose：`sudo sysctl -w net.ipv4.tcp_fastopen=0`，有 MPTCP（`/proc/sys/net/mptcp/enabled` 存在）時再 `sudo sysctl -w net.mptcp.enabled=0`；要重開機後仍有效，把這兩行（`net.ipv4.tcp_fastopen = 0`、`net.mptcp.enabled = 0`）寫進 `/etc/sysctl.d/90-touka.conf`。這會影響整台主機（其他程式也不能用 Fast Open、MPTCP）。
  - compose 的 proxy 帶 `sysctls` 之後，有兩種部署會**起不來**：把 proxy 改成 `network_mode: host`（Docker 不准在主機的網路命名空間設網路 sysctl），以及 `tcp_fastopen` 還沒分網路命名空間的舊核心（4.12 以前）。要用這兩種部署就得自己拿掉 `sysctls`，代理人指令會照上面的「不算隔離」處理。
  - **SMC**：Touka 沒處理 AF_SMC。【推測】SMC 連線退回 TCP 時，核心內部的 socket 可能不經過 Landlock 的埠檢查；Docker 預設的 seccomp 不擋 AF_SMC，而 `smc` 模組在一般 Linux 發行版上通常是可載入的模組，容器裡的程式建 AF_SMC socket 就可能讓主機自動載入它。Docker Desktop（WSL 核心）沒有 SMC，不受影響。一般 Linux 主機（Ubuntu、Debian、Fedora 等）建議封鎖這個模組：建立 `/etc/modprobe.d/touka-no-smc.conf`，內容 `install smc /bin/false`，再用 `lsmod | grep smc` 確認沒有載入（已載入時重開機或 `sudo modprobe -r smc`）。

  合併隔離後，代理人指令只看得到自己的資料夾、隔離暫存區與對話綁定的專案：沒綁專案的對話（心跳的家對話、IDE、SNS、`/debug/execute`）看不到工作區裡其他資料夾，要碰某個專案就在對話裡綁定它。綁了專案的對話、DebugAgent 等自訂模型不在執行器裡跑：每條代理人指令都要人核准，核准後在 Touka 主容器內以檔案隔離執行。專案往下 3 層內有叫 `soul`、`personal_drive` 的資料夾，或叫 `ui_sessions.db` 的檔案（不分大小寫）時，那個專案會被當成 Touka 的資料，不能隔離：每條代理人指令都要人核准，核准後是未隔離。

從與映像相同的 source commit 取得 `docker-compose.ghcr.yml` 與 `.env.example`，放進安裝目錄；不要混用其他版本的 Compose。

## 2. 準備持久化位置

由使用者手動建立安裝目錄內的 `data`、`data/personal_drive`、`data/mcp_servers` 與 `heartbeat-config` **目錄**（執行器只掛 data 底下那兩個），不要建成檔案或連結。Compose 的 `create_host_path: false` 會拒絕缺席目錄；`touka-init` 只做權限初始化，不替你建立、搬移或覆寫資料。

只在 `.env` 不存在時複製範本。PowerShell：

```powershell
if (Test-Path ./.env) { throw '.env 已存在，請手動核對，不可覆寫' }
Copy-Item -LiteralPath ./.env.example -Destination ./.env
```

bash（Linux、macOS）：

```bash
if [ -e .env ]; then echo '.env 已存在，請手動核對，不可覆寫' >&2; else cp .env.example .env; fi
```

使用者編輯這份 `.env`：

```dotenv
TOUKA_VERSION=<選定的映像 tag>
SANCTUARY_DATA_PATH=./data
HEARTBEAT_CONFIG_DIR=./heartbeat-config
# 僅在已確認實際掛載符合 local policy 時設定：
TOUKA_MCP_OAUTH_WORKLOAD_FILESYSTEM_KIND=local
```

`WORKSPACE_ROOT` 未設定時使用 Compose 預設工作區。不要把 token 或 API key 貼在命令列、Issue 或終端輸出；範本的空 secret 留給產品 bootstrap 產生並持久化。

Proxy 的活體設定使用 `proxy-config` named volume，模型使用 `hf-cache`；兩者都需保留（`docker compose down -v` 會一起刪除）。

代理人的 HOME、暫存與快取放在 named volume `touka-sandbox-state`（不在 `data`、不進備份、沒有大小上限；`docker compose down -v` 也會刪掉它，快取要重新下載）。要清快取時先確定沒有代理人指令在跑，用 `docker volume ls` 確認實際名稱（以 compose 專案名開頭，例如資料夾叫 `Touka` 時是 `touka_touka-sandbox-state`；名稱打錯時 Docker 會另建一個空的 volume，什麼都不會清到），再執行：

```shell
docker run --rm -v touka_touka-sandbox-state:/s alpine:3.20 sh -c 'rm -rf /s/*/*/*/tmp/* /s/*/*/*/tmp/.[!.]* /s/*/*/*/home/.cache'
```

萬用字元的層級是 `/s/proxy/<agents|evolution>/<id>/{tmp,home}`；`.[!.]*` 是 tmp 裡以 `.` 開頭的檔（`*` 刪不到）。Docker Desktop 的虛擬磁碟（VHDX）清掉之後不會自己縮小。

執行器另有兩個 named volume：`touka-exec-ipc`（proxy 和執行器之間的控制通道）與 `touka-executor-state`（代理人指令在執行器裡的 HOME、暫存與快取，層級是 `/s/<agents|evolution>/<id>/{tmp,home}`，清法同上、萬用字元少一層 `*`）；`docker compose down -v` 也會刪掉它們。執行器的資源上限是全部代理人共用的 2g 記憶體、512 個行程、2 顆 CPU，要調就取消 `.env` 裡 `TOUKA_EXECUTOR_MEM_LIMIT`、`TOUKA_EXECUTOR_PIDS_LIMIT`、`TOUKA_EXECUTOR_CPUS` 的註解，再重新 `up`。主機（或 Docker 分到的）CPU 少於 2 顆時，取消 `TOUKA_EXECUTOR_CPUS` 的註解並調低（例如 1），否則執行器可能建不起來【推測：沒實測】。

刪除靈魂時 Touka 會自動清掉它在 `touka-executor-state` 與 `touka-sandbox-state` 裡的家目錄；執行器沒在服務時刪不了靈魂，等它恢復再刪。這個功能之前就刪掉的靈魂，先確認沒有現存的靈魂或自訂模型用同一個 ID，再整個刪掉 `/s/agents/<id>`（`touka-executor-state`）與 `/s/proxy/agents/<id>`（`touka-sandbox-state`），例如 `docker run --rm -v touka_touka-executor-state:/s alpine:3.20 rm -rf /s/agents/<id>`。

## 3. 分階段啟動

核心有 MPTCP、建了第 1 節的覆寫檔時，本節**每一行**指令都要在 `-f docker-compose.ghcr.yml` 後面再加 `-f touka-mptcp.override.yml`。

先建立 Proxy 及其 init 相依服務：

```shell
docker compose -f docker-compose.ghcr.yml up -d touka-proxy
docker compose -f docker-compose.ghcr.yml ps -a
```

確認 `touka-init` 成功退出（0），`touka-proxy` 變成 healthy；有任何失敗就先處理原因，不繼續建立其他服務、不反覆重啟。Proxy bootstrap 會把產生的 `HEARTBEAT_TOKEN` 持久化到同一安裝目錄的 `.env`。

**之後用另一個 Compose invocation** 讀取已更新的 `.env`，建立 Heartbeat 與 Chat：

```shell
docker compose -f docker-compose.ghcr.yml up -d --no-deps touka-heartbeat touka-chat
docker compose -f docker-compose.ghcr.yml ps -a
```

不要把這兩階段合併成一次 `up`：`depends_on` 只約束啟動順序，不能保證同一次已解析的 `env_file` 會讀到 Proxy 稍後寫入的 token。

最後一段建立執行器（它的 skills 取自 Proxy 已播種好的 `proxy-config`）：

```shell
docker compose -f docker-compose.ghcr.yml up -d --no-deps touka-executor
docker compose -f docker-compose.ghcr.yml ps -a
```

`touka-executor` 剛起來的一分多鐘是 starting（要先做兩次自我檢查），之後要變成 healthy。一直不是 healthy 時，`docker compose -f docker-compose.ghcr.yml logs touka-executor` 會有一行 `Executor is not serving: <原因>`：`root_path` 是 `data/personal_drive` 或 `data/mcp_servers` 不存在；`touka_root` 是掛進去的資料夾第一層有 Touka 資料根或標記的名稱（例如 `soul`、`ui_sessions.db`），或那個資料夾本身讀不到（權限被改掉也算）：名稱衝突就把那一項改名或移走；讀不到時，`data/personal_drive`、`data/mcp_servers` 只是權限位元被改的話 Touka 約 60 秒內會修好，其他情況（技能資料夾、擁有者不對）照第 4 節「權限被改壞時」跑 `touka-init`；修好後執行 `docker compose -f docker-compose.ghcr.yml restart touka-executor`（執行器啟動檢查沒過就一直不服務，不會自己重試）；`private_dir` 是執行器自己的兩個 volume（`touka-executor-state`、`touka-exec-ipc`）的根不是 uid 1001 擁有（例如舊映像先建過的 `0:0 755`、有人用 root 容器 chown 過）、是連結或不存在——只是權限位元被改（例如 777、000）時，執行器每 5 秒與重啟時都會自己收回，不會停。擁有者不對時先用 `docker volume ls` 確認名稱（例如 `touka_touka-executor-state`；名稱打錯時 Docker 會另建一個空的 volume，什麼都沒改），對有問題的那個執行 `docker run --rm -v <volume 名稱>:/s alpine:3.20 chown 1001:1001 /s`，再 `docker compose -f docker-compose.ghcr.yml restart touka-executor`（權限位元由執行器自己收回）；ipc 那個 volume 擁有者不對時，`docker compose ps` 可能先顯示 healthy 約一分鐘（舊的 socket 還在），要以這行 log 或 Touka 側欄的橫幅為準；`interpreter_untrusted` 是映像裡的 Python 被改過（重新拉映像）。執行器沒起來時 Touka 照常運作，只是 Auto／YOLO 的代理人指令每條都要人核准，也不能刪除靈魂。

在本機讀取初始管理員憑證（不要把輸出分享出去）：

```shell
docker compose -f docker-compose.ghcr.yml exec touka-proxy cat /app/ToukaProxy/config/INITIAL_ADMIN_PASSWORD.txt
```

開啟 <http://localhost:3001>，依初始憑證登入、變更密碼，透過產品 UI 完成連線／API key／模型設定。MCP 客戶端使用 <http://localhost:8083/mcp>；舊 `/sse` 已退役。四個 host ports 都只綁 `127.0.0.1`。

### 從原始碼建置

在 repo 根目錄使用開發版 `docker-compose.yml`，同樣分段啟動：

```shell
docker compose up -d --build touka-proxy
docker compose up -d --build --no-deps touka-heartbeat touka-chat
docker compose up -d --no-deps touka-executor
```

開發版的 Proxy 映像叫 `touka-proxy:local`，執行器用同一個映像（不另外建、也不拉）。之後 `.env` 已經有 token 時，`docker compose up -d --build` 一次就好。以後每次更新程式也一律帶 `--build`：開發版的 Proxy 直接讀資料夾裡的程式，執行器跑的是映像裡的程式，只重啟或沒加 `--build` 時，更新若改到隔離或執行器的程式，兩邊就不一致（`kernel_outdated` 或 `executor_version_mismatch`）：代理人指令全部要人核准、心跳裡的指令被拒，卡片可能寫「未隔離」。

### MCP 隔離更新後要注意的事

C2-3 已把 MCP 伺服器與創建試跑接上隔離；決策見 ADR-0275。從原始碼升級須在 repo 根執行一次 `docker compose up -d --build`，重建 Proxy 與執行器共用的映像；GHCR 安裝照第 4 節更新同版映像與 compose，再依第 3 節分段啟動。不要只 restart：舊啟動器不能提供新隔離，自建會等隔離，第三方可能未隔離啟動並提示。

- Hub、技能市集及自己在設定頁新增的第三方 MCP，在本機 Landlock 可用時只取得允許的資料夾；Filesystem 與文件產生器取得各代理人的 personal_drive，並非每位呼叫者各自隔開。網路照常，封鎖埠是否有效以保護能力為準；核心太舊或網路設定不安全時不保證擋得住。
- 每台 MCP 有自己的 HOME、暫存與 npx／uvx 快取，第一次啟動要重新下載，可能比以前慢甚至超過連線期限；快取沒有大小上限。Proxy 暫存位置新增 `mcp_self`／`mcp_third` 類別，執行器新增 `mcp`，與原有 state volume 共用。
- 第三方讀不到需要的檔案時，到 MCP 設定的「額外允許的資料夾」加入精確資料夾，選唯讀或讀寫；最多 16 筆。Touka 的資料根、設定、原始碼、金鑰位置不能加入。**Git 伺服器要另外加入想讀的專案**；引用單一檔案只開它所在的資料夾，不會自動開整個專案。
- 代理人建立的 MCP 在可用的執行器或本機 Landlock 裡跑；隔離層級為 `none` 時不自動啟動。設定頁有常駐「啟動…」，確認框會寫這次的去處；「未隔離」代表用 Touka 的全部權限執行。每次開機或重啟都要重新同意，不能核准一次永久放行。
- 第三方還在檢查隔離或遇到暫時故障時會等待，故障等滿 660 秒才標暫停；初次檢查不計時。自測失敗約每十分鐘重試，其他原因照設定頁的恢復說明處理；確認為穩定的不可用原因時才未隔離啟動。
- 隔離連線失聯（包括記憶體不足被收掉、自己結束）會在 5／10／20 秒重試三次，之後要人按重啟；未隔離與遠端連線不自動重試。人同意降級 Landlock 的自建 MCP 若再次失聯、層級仍是 `none`，會回到等隔離。
- **代理人不能再更新已部署的自建 MCP**。請使用新名稱，或由你在設定頁移除舊伺服器後重新部署；停用或改顯示名稱不會解除凍結。驗證要真的啟動 MCP（有 `mcp.run()`）並成功回應工具呼叫，印出成功字樣不算。
- `create_mcp` 隔離下看不到建立者自己的資料夾，適合套件指令；要執行代理人寫的腳本請走演化。兩個創建工具有隔離時，YOLO 會依層級執行；`scaffold_mcp` 看本機層級。沒有隔離時仍只有人能核准，Auto 仍須人核准。HTTP 創建入口不能同意降級，只能等隔離或選擇不試跑。
- 未隔離審批卡的任一參數超過 65,536 字元，或無法完整顯示實作程式時，不能核准。部署只綁定 `server.py` 與 `requirements.txt`，不是整份目錄的人工審查保證。
- 設定頁來源只能從第三方或未標示改成「代理人建立」，不能反向放寬。匯入設定不帶來源與額外允許資料夾；更改 command／args／url 會讓原資料夾授權失效，改 env 不會。
- 包住的 MCP 若回 `spec_invalid`，檢查環境變數名稱：只用英數與底線、首字不能是數字、最多 128 字元；例如含連字號的名稱不能用。token 請放條目 env，不要放會被其他行程看見的 args。
- 隔離啟動會先移除部分條目 env，只在 log 警告被移除的名稱、不記值。保留名稱不分大小寫：`HOME`、`TMPDIR`、`TMP`、`TEMP`、`GIT_CEILING_DIRECTORIES`、`PYTEST_ADDOPTS`、`TOUKA_EXEC_ID`、`HF_HOME`、`HF_HUB_CACHE`、`TRANSFORMERS_CACHE`、`TORCH_HOME`、`PIP_CACHE_DIR`、`UV_CACHE_DIR`、`UV_TOOL_DIR`、`UV_PYTHON_INSTALL_DIR`、`NPM_CONFIG_CACHE`、`NPM_CONFIG_PREFIX`，以及所有以 `XDG_`、`GIT_CONFIG_` 開頭的名稱；不是所有 `TMP*`、`UV_*` 或 `NPM_CONFIG_*` 都會移除。
- 名稱任何位置含 `PYTHON`、`NODE_`、`JAVA_`、`GEM_`、`PERL`、`LD_`、`DYLD_` 也會移除，不分大小寫；例如 `NODE_ENV`、`MY_PYTHON_TOKEN`、`NODE_EXTRA_CA_CERTS` 都不會傳給目標。若伺服器支援，改用不衝突的名稱；若一定要用這些名稱，只能在原本規則允許的直接啟動路徑使用，填入 env 不會讓隔離自動降級。
- **原生 Windows 開發模式**：第三方照舊未隔離啟動，設定頁會標示；只有這種未隔離台數時，側欄沒有 MCP 橫幅那一行。自建不自動啟動，有等待／暫停仍會提示。遠端 URL MCP 與 IDE 的 MCP 連線不受這套子程序隔離影響。

## 4. 升級與備份

升級前先備份 `data`、`.env`、`heartbeat-config` 與 `proxy-config` volume。之後在 `.env` 改 `TOUKA_VERSION`，**同時換成和映像同一個 commit 的 `docker-compose.ghcr.yml`**，依第 3 節分段重新 `up`（執行器最後）。只改版本號、沿用舊 compose 時，新映像找不到隔離暫存區與執行器、Fast Open 也沒關（原因 `sandbox_state_unconfigured` 或 `net_tcp_fastopen`）：卡片寫「未隔離」，Manual 與人核准後照常執行，Auto／YOLO 每條都要人核准，心跳裡的指令被拒（從原始碼跑、沒有 `--build` 時也一樣；那時聊天頁還是舊版，核准任何有隔離標示的卡〔「未隔離」或「隔離降級」〕都會回 422、不執行）。審批卡寫「暫存區權限不對」時照卡上的步驟處理；之後卡片如果寫 `net_mptcp`（Docker Desktop 更新核心後可能會變），照卡片在覆寫檔的兩個服務都加那一行（見第 1 節「網路設定」，GHCR 每段指令都要帶那個 `-f`）。升級後先重新整理聊天分頁，再請代理人跑 `id -u; echo $HOME`：看到 `1001` 和 `/app/executor_state/agents/…` 才算在執行器裡（卡上沒有標示不能當證據）。不要用 `down -v` 來「重裝」，那會刪掉設定與模型快取。回復時先 `docker rm -f touka-executor`（不要用 `--remove-orphans`），再把 `TOUKA_VERSION` 與 compose 檔一起換回舊版、還原備份；留下的 `touka-exec-ipc`、`touka-executor-state`、`touka-sandbox-state` volume 與 `touka-exec-net` 網路由你決定要不要刪（先用 `docker volume ls`、`docker network ls` 確認名稱）。從原始碼跑時升級是 `docker compose up -d --build`，回復同樣先 `docker rm -f touka-executor`。

權限被改壞時（只有人核准、卡片寫「在 Touka 主容器內以檔案隔離執行」的指令做得到）：data、設定、`hf_cache` 讀不到時執行 `docker compose -f docker-compose.ghcr.yml run --rm touka-init`，再 `docker compose -f docker-compose.ghcr.yml restart touka-proxy`（容器自動重啟不會跑 `touka-init`；資料裡的符號連結、FIFO、socket、多連結檔、setuid／setgid 的一般檔、帶延伸屬性的項目，以及 `data/personal_drive`、`data/mcp_servers` 裡 `node_modules`、`.venv` 這類工具資料夾，`touka-init` 不碰也不跟進，結果的 `skipped` 會寫出各類件數，這些項目的擁有者要自己處理；四個掛載的根目錄、`data/personal_drive`、`data/mcp_servers` 本身是連結、帶延伸屬性（例如 SELinux 標籤）或 setuid／setgid 時照舊拒絕，總數超過 10 萬項或 64 層也照舊拒絕，ADR-0279）；`/app` 被鎖時 `docker compose -f docker-compose.ghcr.yml up -d --force-recreate touka-proxy`。`connections.json` 讀不到時修好之後約半分鐘自動恢復（有任何一條連線的網址是空的也算讀不到：在連線頁補上網址或刪掉那條再存檔；連線頁從 2026-06-18 起不收空網址，只有更早的檔、手改或匯入的設定快照會有）；`auth_policies.json` 或 `autonomy.json` 讀不到時修好再重啟 `touka-proxy`。`autonomy.json` 讀不到時，安全設定頁顯示的是暫代的最嚴值（每日 0 次、工具 1 輪、總量上限 0 元），這時**不要按存檔**：會用這些值蓋掉原檔（原本的審查員設定等也一起不見）、提示消失，之後代理人一直「已達總量上限」卻看不到原因；已經存了就從備份還原這個檔，或在安全設定頁逐項改回再存。

## 5. 子系統與開發入口

- [ToukaProxy](ToukaProxy/README.md)：核心映像，包含 Memory 與 Dispatch。
- [ToukaMemory](ToukaMemory/README.md)：同行程函式庫；沒有獨立安裝或 MCP server。
- [ToukaDispatch](ToukaDispatch/README.md)：由 Proxy supervisor 啟動；不新增獨立容器。
- [ToukaHeartbeat](ToukaHeartbeat/README.md)：排程服務。
- [ToukaChat](ToukaChat/README.md)：Web UI。
- [ToukaIDE](ToukaIDE/README.md)：VS Code 擴充，不是 runtime image。

參考：[Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)、[env_file 與版本前提](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/)。
