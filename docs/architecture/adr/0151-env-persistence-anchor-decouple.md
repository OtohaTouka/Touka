# ADR-0151: 密鑰持久化 .env 錨定 compose 目錄（與 WORKSPACE_ROOT 解耦）＋幽靈檔守衛

## 狀態

✅ 已接受（07-11 認證事故餘波 #5；維護者決定採解耦（推薦），本案只改 compose／程式，既有 .env 由部署者自行處理）

**部分取代 [ADR-0118](0118-setup-wizard-reliability-and-embedding-lifecycle.md)** 的
`TOUKA_ENV_FILE=/app/workspace/.env` 決策（該路徑綁 `WORKSPACE_ROOT`，此處改錨 compose 目錄）。

## 日期

2026-07-12

## 背景

維護者回報「首裝精靈沒把 HEARTBEAT_TOKEN／DISPATCH_TOKEN／TOUKA_AUTH_SECRET 寫進 .env」。
源碼＋本機 Docker 實測定位根因——**不是持久化邏輯，是路徑耦合**：

- **讀**：compose `env_file: .env` 載入「compose 目錄」的 `<repo>/.env` → proxy 讀得正確。
- **寫**：`TOUKA_ENV_FILE=/app/workspace/.env`，而 `/app/workspace` ＝ `${WORKSPACE_ROOT:-.}`
  掛載（ADR-0118）。`WORKSPACE_ROOT` 指到 repo 的上層（例如整個磁碟根）時 → `/app/workspace`
  掛成那個上層 → `TOUKA_ENV_FILE` 解析成 **幽靈 `<上層>/.env`**，只會有自動生成的 token。

三個 token 的「生成即持久化（禁令 15）」＋首裝精靈 `SetupService.apply` **都**寫這份幽靈檔
→ compose 載入的真 .env 永遠是空 token → 每次重啟重生成、登入態不穩。**寫入的 .env 與載入的
.env 靜默分歧**是病根：`WORKSPACE_ROOT`（agent 工作區、可指任意目錄）被拿來兼任「.env 持久化
位置」（必須＝compose 目錄）。任何把 `WORKSPACE_ROOT` 指向 repo 以外的使用者都會中招。

## 決策

**把「.env 持久化目標」與 `WORKSPACE_ROOT` 解耦，改錨定 compose 目錄（`env_file` 實際載入
那份 .env 的所在）。** 維護者的選擇＝「解耦（推薦），支援任意 WORKSPACE_ROOT，另加 fail-loud
守衛」。

1. **穩定掛載 compose 目錄**：proxy 新增 `- ./:/app/host_root`。`./` 恆為 compose 目錄（compose
   ＋ .env 所在），與 `WORKSPACE_ROOT` 無關。
2. **持久化改指它**：`TOUKA_ENV_FILE=/app/host_root/.env`（＝ compose 目錄的 .env ＝ env_file
   載入的同一檔）。`WORKSPACE_ROOT` 恢復單一職責（agent 工作區、可指任意目錄）。
   - **為何是『目錄』掛載而非單檔**：`python-dotenv.set_key` 以 temp 檔＋`rename` 原子覆寫；
     若 .env 本身是 bind-mount 掛載點，rename 會失敗（跨掛載邊界）。故 .env 必須是「目錄掛載內
     的普通檔」——這正是舊制寫 `/app/workspace/.env`（目錄掛載內）能成功的原因，此處沿用。
3. **fail-loud 守衛**：啟動時 `verify_env_persistence_integrity()` 偵測「幽靈檔」——若目前生效
   的探針設定鍵（API 金鑰／WORKSPACE_ROOT／SANCTUARY_DATA_PATH 等，**只來自 .env、不由 proxy
   compose `environment:` 注入**）在持久化 .env 內**一個都沒有**，即 loud `logger.error`（附修
   復指引）。**不中止啟動**（認證仍可用臨時密鑰，中止反而把可用系統變不可用）。
   - **值相符豁免**（對抗式審查 #2 修）：自動持久化的三 token（TOUKA_AUTH_SECRET／DISPATCH_TOKEN
     ／HEARTBEAT_TOKEN）若「檔內值 == 目前生效值（非空）」＝該檔確實被 env_file 載入過 → 絕非
     幽靈、直接豁免。堵真實誤報：operator 以 `-e`／`environment:` 注入 API 金鑰（探針鍵不進
     .env）、.env 只有自動生成 token 的健康部署，原判準會誤報。值比對僅於進程內、不落日誌。

## 實作

- `docker-compose.yml`／`docker-compose.ghcr.yml`（proxy）：加 `- ./:/app/host_root` 掛載；
  `TOUKA_ENV_FILE=/app/workspace/.env` → `/app/host_root/.env`；更新 root-cause-A 註解。
- `ToukaProxy/src/config.py`：新增 `verify_env_persistence_integrity(env_path, environ)`＋
  `_ENV_PERSISTENCE_PROBE_KEYS`＋`_AUTO_PERSISTED_TOKEN_KEYS`（值相符豁免）＋`_read_env_keys`
  （鍵名解析、容忍非 UTF-8 不炸啟動）。判準＝「生效探針鍵全數不在持久化檔」才報（部分重疊不算）
  ＋ token 值往返一致即豁免；缺檔/無生效鍵→回空（全新首跑不誤報）。
  `resolve_env_file_path` docstring 範例路徑更新。
- `ToukaProxy/src/main.py`：lifespan `validate_live_configs()` 後呼叫守衛（同 bootstrap 閘）。
- 文件：SSOT_MASTER_MAP／PROXY_INTERFACE／ToukaProxy README 的 `/app/workspace/.env` 更新為
  `/app/host_root/.env`＋解耦說明；ADR-0118 加 supersede 指標。

## 後果

- **正面**：持久化的 token/密鑰**恆落在 compose 載入的 .env**，與 `WORKSPACE_ROOT` 無關——
  使用者可自由把 agent 工作區指向任意目錄而不再丟金鑰；首裝精靈與自動生成走同一條正確路徑。
- **暴露面（誠實界定）**：`./:/app/host_root` 來源恆為 compose 目錄。**預設（`WORKSPACE_ROOT`
  未設）或指向 compose 目錄的超集（如本案的磁碟根 ⊇ `<repo>`）＝零新增暴露**（compose 目錄本就經
  workspace 掛載）。唯 `WORKSPACE_ROOT` 指向與 compose 目錄**互斥**的路徑（如 /mnt/other）時，
  host_root 才額外把 compose 目錄（含 .env）以檔案形式掛進容器——但**金鑰『值』本就經 env_file
  注入 proxy 程序環境**（os.environ 已含），故此為「檔案可見性」增量、非「金鑰值」新暴露。
- **三條 .env 寫入路徑一致受惠**：自動生成（`persist_generated_secret`）、首裝精靈
  （`SetupService.apply`）、設定頁（`routers/config.py` `POST /config/env`）全部經
  `resolve_env_file_path()` SSOT → 解耦一次到位，無殘留耦合的第二寫入路徑。
- **相容**：預設（`WORKSPACE_ROOT` 未設或＝compose 目錄）行為與過去一致；只有「指向 repo 以外」
  的使用者從『靜默丟金鑰』變成『正確持久化』。dev 即時掛載 `ToukaProxy/src` → 免 `--build`
  即生效；ghcr 需新映像（隨版本發佈）。
- **端到端驗證**：`docker compose config` 實測 `/app/host_root` 來源解析為 `<repo>`（compose
  目錄）、`TOUKA_ENV_FILE=/app/host_root/.env`＝真實 `<repo>/.env`（非上層的幽靈 `.env`）。

## 對抗式審查（18 項提出 → 5 項確認、全為 medium，其餘駁回）

- **已修**：守衛誤報（`environment:` 注入金鑰的健康部署被判幽靈）→ token 值相符豁免；
  非 UTF-8 .env 讀取炸例外 → `_read_env_keys`／`dotenv_values` 容忍；ghcr 標頭＋root README
  殘留 `/app/workspace/.env` 舊路徑 → 更新；heal 步驟缺 heartbeat 半邊 → 補（見下）。
- **誠實界定（native Linux 寫入邊界，pre-existing 非本案引入）**：proxy 以 appuser(1001) 執行，
  `set_key` 需在 `.env` **所在目錄**建 temp 檔＋rename——native Linux 上 compose 目錄若為宿主
  他人 uid 所有（典型 755），寫入 PermissionError → `persist_generated_secret` 捕捉、loud log、
  回 False → token 該窗不持久（**症狀同病根不同**；守衛只抓「寫錯檔」模式、抓不到「寫不進」
  模式——後者已由 persist 自身 log）。**舊制同界限**（`${WORKSPACE_ROOT:-.}` 預設＝同一 compose
  目錄），Windows/Docker Desktop（維護者的平台）不受影響。緩解＝operator 給 compose 目錄 uid 1001
  寫權或 `user:` 覆寫；根治（root entrypoint chown 後 drop）與 ADR-0150 同款、屬另案。

## 限制與邊界（誠實界定）

- 守衛為 **log-only**（不中止啟動）；且採「全數不符才報」＋值相符豁免的保守判準——若使用者把
  「部分」金鑰放 compose `environment:`、「部分」在 .env，可能不觸發（避免誤報的取捨）。解耦
  本身才是治本，守衛是安全網。
- **既有部署的收拾（部署者自行處理）**：本案只改 compose／程式，既有的 .env 由部署者自行處理。步驟：
  1. `docker compose up -d`——proxy 配置變更（新掛載＋env）會自動重建，三 token 自動生成並寫入
     **真** `<repo>/.env`（一次性：TOUKA_AUTH_SECRET 由臨時轉持久＝該次既有登入態失效、需重登）。
  2. `docker compose up -d touka-heartbeat --force-recreate`——heartbeat 的 env_file 值是「容器
     建立時」快照，步驟 1 當輪它拿到的仍是舊（空）HEARTBEAT_TOKEN；重建一次讓它讀到新 token，
     proxy／heartbeat 兩端才對齊（對抗式審查 #5）。
  3. 上層的幽靈 `.env` 可刪；指到上層的 `WORKSPACE_ROOT` 可保留（解耦後已安全）或改回 `<repo>`
     （建議，縮小 workspace 掛載面）。

## 相關

- [ADR-0118](0118-setup-wizard-reliability-and-embedding-lifecycle.md)（本案部分取代其
  `TOUKA_ENV_FILE=/app/workspace/.env` 決策；`resolve_env_file_path` SSOT／`apply_runtime_env`
  即時生效沿用不變）
- [ADR-0150](0150-heartbeat-config-dir-mount.md)（同一類病根：gitignore 的執行期檔在乾淨環境
  缺席／路徑錯配；本案為其姊妹修復）
- 07-11 認證事故餘波清單 #5
