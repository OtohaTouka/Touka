# ADR-0150: 心跳設定改掛『目錄』而非單檔 bind-mount（避開『目錄疊檔案』啟動崩潰）

## 狀態

✅ 已接受（使用者重啟遇到 OCI runtime mount 崩潰；源碼追蹤定位為單檔 bind-mount 的固有脆弱性）

## 日期

2026-07-12

## 背景

使用者重啟 Docker 時 `touka-heartbeat` 容器啟動失敗：

```
Error response from daemon: failed to create task for container: ... OCI runtime
create failed: ... error mounting ".../ToukaHeartbeat/config.json" to rootfs at
"/app/config.json": ... not a directory: Are you trying to mount a directory onto
a file (or vice-versa)?
```

源碼追蹤（未先修、僅蒐證）確認根因鏈：

1. **`config.json` 是 gitignore 的執行期產物**（`ToukaHeartbeat/.gitignore` 列 `config.json`、
   `git ls-files` 只有 `.example`）。任何 `git checkout`／`reset --hard`／fresh clone／wipe
   都**不還原**它 → 主機端該檔可能缺席。
2. **Docker 對「缺席」的單檔 bind-mount 來源，一律自動建成「目錄」**（它無從得知你要的是檔案）。
3. 容器映像內 `/app/config.json` 是**檔案**（`COPY ToukaHeartbeat/ .` 烤入）。runc 把主機那個
   自動建立的**目錄**掛到容器內的**檔案** → 型別衝突 `ENOTDIR`（「mounting a directory onto a file」）。

系統原有防呆——`touka-init` 一次性容器會 `rmdir` 掉誤建的目錄並從 `.example` 播種——但
**`depends_on: service_completed_successfully` 只在 `docker compose up` 建立容器時被 compose
尊重，不是 daemon 執行期的規則**。使用者「重啟」走的是 `restart: always` 的 daemon 自動拉起／
或 `docker compose restart`／或 Docker Desktop 重開，**皆跳過 `touka-init`**。且該 mount 崩潰
發生在**容器任何程式碼執行之前**，容器內的 entrypoint／`StorageService` 自癒都來不及救。

關鍵洞察：**單檔 bind-mount 才是脆弱點**。系統其他掛載（`touka-proxy` 的 `./ToukaProxy/config`、
`./data`）都掛**目錄**，故從無此症——因為 Docker 對缺席目錄來源「自動建成目錄」＝**正確型別**、
掛載恆成功。heartbeat 的單檔掛載是唯一的異類。

## 決策

**heartbeat 不再 bind-mount 單一 `config.json` 檔，改掛一個設定『目錄』`/app/config`。**

- 來源目錄缺席時 Docker 自動建成**目錄**（正確型別）→ 掛載恆成功 → 容器起得來。**ENOTDIR 崩潰
  在所有啟動路徑（up／restart／daemon 重啟）消除**——這是本案的核心保證。
- 設定內容雙重來源：`touka-init`（`up` 時）遷移舊單檔／否則從 `.example` 播種＋`chown 1001`（讓
  appuser 可寫、CI 檔案存在同步可測）；`StorageService.load_config()`（M6 fix：缺檔即自癒生成）為
  init 未跑時（restart／daemon 重啟）的補檔後盾。
- 容器內設定檔位置由 `HEARTBEAT_CONFIG_FILE` 環境變數指向 `/app/config/config.json`；`awaken.py`
  未設定時**回退 `BASE_DIR/config.json`**——非 Docker 執行、ghcr 以外情境、既有測試**行為零變動**。
- **持久化保證的邊界（誠實界定）**：崩潰消除是無條件的；「設定持久化」則需目錄可由 appuser(1001)
  寫入。`up`（跑 init chown）與「restart／daemon 重啟且目錄已存在（前次 up 建好）」皆可寫；唯
  **native Linux 上「刪掉 gitignore 的設定目錄後、以純 restart／daemon 重啟（跳過 init）啟動」**
  這個窄窗，Docker daemon(root) 自動建的目錄為 root:root，appuser 無法寫入 → 容器仍**啟動不崩潰**、
  但該窗內用 in-memory 設定、寫入被 `save_config` 吞掉（下次 `up` 即自癒）。Docker Desktop
  （Windows/Mac，本案回報者平台）不強制 bind-mount ownership，故無此現象。詳見「限制」。

## 實作

- `ToukaHeartbeat/awaken.py`：新增純函式 `_resolve_config_file(base_dir, override)`；
  `CONFIG_FILE = _resolve_config_file(BASE_DIR, os.environ.get("HEARTBEAT_CONFIG_FILE", ""))`
  （未設定回退 `BASE_DIR/config.json`，預設不變）。**刻意在 awaken.py 直讀 env、不經 config.py**——
  dev compose 只 live-mount `awaken.py`（`config.py` 已烤進映像），改 config.py 欄位需 `--build`；
  直讀 env 讓修復於 `docker compose up -d`（**免 rebuild**）即生效。`config.py`／`StorageService` 不動。
- `docker-compose.yml`（開發版）：heartbeat volume `…/config.json:/app/config.json` →
  `${HEARTBEAT_CONFIG_DIR:-./ToukaHeartbeat/config}:/app/config`；加 `HEARTBEAT_CONFIG_FILE=
  /app/config/config.json`。`touka-init` 由「rmdir＋cp .example 單檔播種」改為「`mkdir -p` 目錄
  ＋**遷移舊單檔**（既有 `config.json` 搬進新目錄以保留使用者排程）＋否則 cp `.example` 播種
  ＋`chown -R 1001`」（兩個平坦 `if` 區塊、無巢狀——折疊標量下巢狀 if 易折壞）。同步播種讓 CI
  fresh-volume 閘可確定性 `test -f`、且 appuser 可寫。
- `docker-compose.ghcr.yml`（pull 版）：同構——`./heartbeat-config.json:/app/config.json` →
  `${HEARTBEAT_CONFIG_DIR:-./heartbeat-config}:/app/config`＋env＋init 目錄化/遷移＋標頭註解。
- `.github/workflows/release-images.yml`：fresh-volume 啟動閘 `test -f heartbeat-config.json` →
  `test -f heartbeat-config/config.json`（驗證「掛目錄→容器啟動→StorageService 補檔」全鏈路）。
- `.env.example`：`HEARTBEAT_CONFIG_PATH=…config.json` → 註解化 `HEARTBEAT_CONFIG_DIR`（留空即用
  compose 相對預設；舊鍵停用可刪）。
- `.gitignore`（root 加 `heartbeat-config/`）＋`ToukaHeartbeat/.gitignore`（加 `config/`）。

## 後果

- **正面**：heartbeat **啟動不再因「設定檔缺席」崩潰**（fresh clone／wipe／restart／daemon 重啟
  皆能起）；掛載形狀與系統其他服務（proxy config/data）一致；設定路徑改為相對預設，比舊 `.env`
  的絕對 `D:/Touka/...` 更可攜。
- **相容/低風險**：`awaken.py` 預設路徑不變 → 非 Docker、ghcr 未設 env 的情境、既有 heartbeat
  單元測試（`StorageService(tmp_path)`）**零行為變動**。舊 `HEARTBEAT_CONFIG_PATH` 淪為死鍵、被
  忽略（compose 改讀 `HEARTBEAT_CONFIG_DIR`），使用者無須改 `.env` 即可運作。
- **端到端驗證**：本機 Docker 實測——(1) `up` 後 heartbeat 由當機轉為 running、config 遷移進
  `config/config.json`、`/status` 200；(2) 刪目錄後純 `docker start`（跳過 init）＝原崩潰路徑，
  現**啟動成功**（restarts=0、200）；(3) 目錄已存在時純 restart（＝Docker Desktop 重啟路徑）全乾淨。

## 限制與邊界（誠實界定）

- **升級啟動請走 `docker compose up`（勿用 `docker compose restart`）**：`restart` 不重建容器、
  不重讀已編輯的 compose → 沿用**升級前**的舊容器定義（舊單檔掛載），既不啟用新目錄掛載、也不
  觸發遷移。要套用本案改動與遷移舊排程，必須 `up`（或 `up -d --force-recreate`）。舊單檔留存不刪
  ＝無害孤兒（不再被掛載）。
- **native Linux 的窄窗持久化邊界**：見「決策」末——刪掉設定目錄後以純 restart／daemon 重啟
  （跳過 init）啟動時，daemon(root) 自動建的目錄 appuser 無法寫入 → 容器仍啟動、但該窗內設定不
  持久化（`save_config` 靜默吞例外）、下次 `up` 自癒。Docker Desktop 無此現象。**根治**（未納入
  本案，屬另案）＝改 root entrypoint chown 後 drop 至 appuser，或讓 `save_config` 對寫入失敗
  fail-loud 以利診斷。
- **自訂 `HEARTBEAT_CONFIG_DIR`**：`touka-init` 只 chown **預設**設定目錄；若覆寫成自訂路徑，該
  路徑需自行確保 uid 1001 可寫（否則同上 native Linux 寫入邊界）。

## 相關

- [ADR-0140](0140-ghcr-prebuilt-distribution.md)（ghcr pull 版 compose／CI fresh-volume 啟動閘——
  本案調整其 heartbeat 播種驗證與 init）
- 診斷：OCI runtime mount 崩潰的完整根因鏈蒐證（單檔 bind-mount 缺席 → Docker 自動建目錄 →
  目錄疊檔案 ENOTDIR；up-only init 擋不住 restart 路徑）
