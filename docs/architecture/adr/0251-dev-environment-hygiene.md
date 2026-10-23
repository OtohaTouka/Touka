# ADR-0251：開發環境整理：Windows 測試前置條件、結尾摘要、測試沙盒收尾、建置產物宣告、公開金鑰拒用

- 狀態：✅ 已接受（維護者 2026-10-14 核准計畫：五項做法、AGENTS.md 的 Windows 前置條件文字、建置產物宣告 7 行，以及第 5 項的產品行為改變；U1～U3 完成，任務分支 `252a19cc` 整體驗收通過，見文末）
- 日期：2026-10-14
- 決策者：維護者
- 影響範圍：`AGENTS.md`（直譯器主權段之後新增一段）、`CONTRIBUTING.md` 佈建步驟、`pytest.ini`、`tests/conftest.py`、新 `tests/sandbox_cleanup.py`、`tests/unit/test_installation_entrypoints.py`、根目錄新 `rebuildable-outputs.txt`、`ToukaProxy/src/config.py`、`ToukaProxy/src/core/security.py`、`ToukaProxy/src/services/dispatch_supervisor.py`、`ToukaProxy/src/services/setup_service.py`、`ToukaProxy/.env.example`、`tests/unit/test_sec_batch_delta.py`、`SECURITY.md` 三列
- 相關：ADR-0053（設定精靈的派工 token 提醒）、ADR-0064（派工 token 不離開 Touka 程序）、ADR-0193（測試沙盒與 repo 落盤哨兵）、ADR-0214（自動測試一律沙盒）、ADR-0230（心跳服務憑證）、ADR-0240（Guard #16 快取與工作資料夾清理）、ADR-0250（E1 本機 compose 失敗與上一輪的 8 個跳過）。這些 ADR 的決策都不變。
- 修正（[ADR-0254](0254-iso-c1-small-fixes-and-execution-gaps.md)）：已知限制「設定精靈的 `apply` 仍可寫入公開值」已修，精靈收到公開值回 422、`.env` 不寫

## 背景

- 代理在 Windows 本機每次都要自己摸索跑測試要先準備什麼。10/08～10/13 的驗證紀錄多次寫「17 個跳過」，其中 9 個是安裝腳本測試找不到 bash：從 Git Bash 跑時 PATH 上第一個 git 是 `Git\mingw64\bin\git.exe`，舊的推算得到不存在的 `Git\mingw64\bin\bash.exe`，測試就默默跳過。
- pytest 結尾只列失敗與錯誤，跳過的原因要另外找。
- 測試沙盒留在系統暫存：六天累積 734 個 `touka_test_session_*`（約 18 MB）。原因有三：Qdrant 本機模式的 client 沒關、鎖檔刪不掉；權限稽核的背景寫入晚於刪除，又把資料夾建回來；設定階段就結束的程序（`--basetemp` 被拒、`--help`、收集錯誤）根本沒跑到收尾。原本的重試解決不了。
- 開發流程的清理檢查清理工作資料夾時，前端建置產物每次都讓它停下來問人。
- 有個自動測試會打開真的根 `.env`。查下去發現三把自動持久化金鑰（`HEARTBEAT_TOKEN`、`TOUKA_AUTH_SECRET`、`DISPATCH_TOKEN`）都照單全收歷來公開過的值。其中 3 個值曾在 2026-05-21～07-04 出貨的根 `.env.example` 裡，而 `core/bootstrap.py` 在沒有 `.env` 時會把它整份複製成 `.env`，所以那段期間的新安裝會直接用上。登入簽章金鑰若是公開值，知道的人能自己簽出任何使用者（含管理員）的登入 token，以及心跳服務 JWT。

## 決策

### D1 Windows 本機跑後端測試的前置條件（AGENTS.md）

- AGENTS.md「直譯器主權」那段之後加一段：Node／npm 版本以兩個 `package.json` 為準（`ToukaChat` 的 `engines`、`ToukaIDE` 的 `devEngines`），只替這次指令切換、不改系統 Node；先在 `ToukaIDE` 跑 `npm run install:controlled`（companion 測試要用它的 esbuild），要跑 guard_all 時 `ToukaChat` 也跑一次，再跑 `npm run gen:artifact-runtime`；9 個安裝腳本測試用 Git for Windows 自帶的 bash；核對結尾摘要：已知的本機失敗只有 E1 的 6 個，條件都對時只剩 7 個環境跳過，比這多就查原因。
- 7 個＝這台機器不能建 symlink 的 4、只在 POSIX 跑的 1、要另外同意才跑的 Docker 部署與 ToukaIDE Marketplace 打包比對各 1（上一輪的 8 個減掉 D5 刪掉的讀真 `.env` 那 1 個）。
- `CONTRIBUTING.md` 的佈建步驟對齊 CI：`ToukaChat` 改用 `npm run install:controlled`（`npm ci --ignore-scripts` 之後還要回補 braces；只跑 `npm ci` 時下一步 `gen:artifact-runtime` 的 `braces:verify` 會回 `not-patched`），並補 `ToukaIDE` 的 `npm run install:controlled`。兩者都在拋棄式複本照 CI 實跑過。

### D2 結尾摘要列出失敗、錯誤與跳過原因；修好找 bash 的方法

- `pytest.ini` 加 `addopts = -rfEs`。`-r` 會整個換掉 pytest 預設的 `fE`：只寫 `-rs` 的話結尾就不再列失敗與錯誤（等於放寬），所以 `f`、`E`、`s` 三個都寫。
- `tests/unit/test_installation_entrypoints.py` 的 `_git_for_windows_bash(git)`：從 PATH 上 `git` 的位置推算 Git for Windows 的 `bin\bash.exe`；推到的上一層（不分大小寫）是 `mingw64` 時才多往上一層，`cmd`、`bin` 位置不多試；找不到回 `None`，不退回 WSL 的 `bash`。跳過訊息補一句「Windows 需要 Git for Windows」。
- 本機缺 bash 仍是跳過（不改成失敗），但現在會列在結尾。

### D3 測試沙盒收尾

- 新 helper `tests/sandbox_cleanup.py`（只用標準函式庫，不匯入產品）：`remove_session_sandbox(root, release_steps)` 依序跑釋放步驟（一步失敗只記 log、不中斷其他步驟），`gc.collect()`，`rmtree` 重試 3 次，最後 `ignore_errors=True`；回傳刪不掉的路徑或 `None`；資料夾已不存在時不做事，可以重複呼叫。
- `tests/conftest.py` 有具名清單 `SANDBOX_RELEASE_STEPS`，依序是：等稽核寫入 → 關 Qdrant client → 關 SQLite → 關 log handler。
  - 等稽核寫入：對 `PolicyAuditService._executor` 送一個空工作並等它（最多 30 秒）。直譯器結束時 `concurrent.futures` 已先收掉 worker、佇列已清空，這時的 `RuntimeError`（interpreter shutdown）當成已清空，不算失敗。產品程式不改。
  - 關 Qdrant：只在 `memory_server.vector` 已載入時呼叫 `VectorStore.close_all_clients()`；不在 conftest 頂部匯入向量模組，免得每次 pytest 多約 10 秒。
  - 關 SQLite：`FactualStore.close_all_connections()`；關 log handler：`close_sandbox_log_handlers`。
- 收尾有三個地方，都呼叫同一個可重複執行的 `_cleanup_session_sandbox()`：
  1. `mkdtemp` 後立刻 `atexit.register`：涵蓋所有正常結束的直譯器，包括設定階段就結束的程序。只有建立沙盒的程序會執行（PID 檢查；fork 出的子程序不刪父程序的沙盒）。
  2. session fixture `global_test_sandbox` 的收尾（仍寫在 `yield` 之後、不包 try/finally）。
  3. `pytest_sessionfinish(trylast=True)`：session fixture 沒跑時（`--collect-only`、沒選到測試）在這裡刪。
- 殘留回報：`pytest_terminal_summary` 在結尾印一行「測試沙盒沒刪乾淨：<路徑>」；atexit 時寫到 stderr。
- 沙盒外洩哨兵、外網違規檢查、fixture 的 `yield` 值與順序都不變。

### D4 建置產物宣告 `rebuildable-outputs.txt`

| 行 | 路徑 | 被誰忽略 | 怎麼重新產生 |
|---|---|---|---|
| 1 | `ToukaChat/.next/` | `ToukaChat/.gitignore` `/.next/` | `npm run build` 或 `npm run dev` |
| 2 | `ToukaChat/out/` | `ToukaChat/.gitignore` `/out/` | `npm run build`（`next.config.ts` 設 `output: "export"`） |
| 3 | `ToukaChat/next-env.d.ts` | `ToukaChat/.gitignore` `next-env.d.ts` | `npm run build`／`npm run dev` 自動產生 |
| 4 | `ToukaChat/src/generated/` | `ToukaChat/.gitignore` `src/generated/` | `npm run gen:artifact-runtime`（`npm run test`、`dev` 也會先跑） |
| 5 | `ToukaChat/tsconfig.tsbuildinfo` | `ToukaChat/.gitignore` `*.tsbuildinfo` | 型別檢查（`tsconfig.json` 開 `incremental`） |
| 6 | `ToukaChat/tsconfig.guard.tsbuildinfo` | `ToukaChat/.gitignore` `*.tsbuildinfo` | `npm run guard:typecheck` |
| 7 | `ToukaIDE/dist/` | `ToukaIDE/.gitignore` `dist/` | `npm run compile`（或 `package`） |

- 檔案在 repo 根目錄，UTF-8、LF、沒有 BOM、沒有註解，一行一個從 repo 根算起、用 `/` 分隔的路徑。
- 讀取者是開發流程的清理檢查：它只讀 `origin/HEAD` 指到的預設分支上的這個檔；工作資料夾裡被 git 忽略的東西只有可重建快取與這 7 種時，直接清、不再問人。其他東西照舊擋下。任何一行不合法，它就整份不採用。
- 格式規則以清理檢查為準。`tests/unit/test_rebuildable_outputs_declaration.py` 先照同一套規則檢查（一般檔、UTF-8、沒有 BOM、沒有空行；沒有萬用字元、`..`、`\`、`#`、反引號、前後空白；不含 `.git`／`.toukadev` 段；不含可重建快取名稱；不重複；每行被 git 忽略、底下沒有被追蹤的檔），規則被改壞時 Touka 自己的測試先紅。
- 生效條件：合進 `main`，主資料夾 `git fetch origin` 之後。撤回：刪掉這個檔並合進 `main`。
- 不加進 `.dockerignore`（內容無害）。

### D5 三把金鑰拒用歷來公開過的值

- 單一對照表在 `ToukaProxy/src/config.py`：`PUBLIC_SECRET_VALUES: dict[str, frozenset[str]]`，三把金鑰共 9 個值（出處見下表）。比對看「鍵名＋值」、去掉前後空白（`is_public_secret`）；別的鍵（例如 `SEARXNG_SECRET`）用到同一個佔位字串不算。
- `usable_secret(key, current)`：
  1. 不是公開值就原樣回傳。
  2. 是公開值時讀 `.env` 已存的值（`read_persisted_secret_values`）；它非空、也不是公開值，就 `apply_runtime_env` 同步到執行中的程序並回傳它，不寫檔。容器只重啟、沒重建時程序環境仍是公開值，這樣才不會每次重啟都換一個值（登入金鑰則是每次登出所有人）。
  3. 否則回空字串，交給各解析點既有的「產生並寫回 `.env`」流程。
  - log 只寫鍵名、不寫值。
- 三個解析點都經它：`_resolve_heartbeat_token`、`AuthSecretManager.get_secret`、`DispatchSupervisor.build_child_env`。派工那邊最後再確保子程序環境不帶公開值，包括從父環境繼承來的。設定精靈 `SetupService._is_dispatch_token_default` 改用同一份對照表（空值也算），另外兩個派工公開值現在也會顯示「還是預設值」。
- 寫不回或 bootstrap 關閉時 fail-closed：心跳只剩 JWT、登入金鑰用臨時值（重啟後 token 失效）、派工子程序不帶 token（拒絕所有請求）。
- `ToukaProxy/.env.example` 的心跳註解改成 `# HEARTBEAT_TOKEN=`。
- 刪掉讀真 `.env` 的 `test_sec_batch_delta.py::test_env_heartbeat_token_cleared`（跳過數 8 → 7）；`test_env_example_has_no_real_secrets` 改成用 `git ls-files` 檢查所有版控中的 `.env*.example`（目前 4 個），新範本自動納入。
- 對人類的影響：設定剛好是公開值時，啟動後會自動換新；更新後要執行一次 `docker compose up -d`（只按重新啟動不夠，在那之前心跳喚醒和排程管理頁會失敗）；登入金鑰被換掉時所有人要重新登入一次。值本來就不是公開值時沒有副作用。`DISPATCH_TOKEN` 依 ADR-0064 不離開 Touka 程序，換掉人類感覺不到。

#### 9 個公開值的出處

由 `git log --all -S`／`-G` 查出（排除 `data`、`site`、`tests` 的產品歷史）。

| # | 金鑰 | 公開值 | 出處（加入 → 移除） |
|---|---|---|---|
| 1 | HEARTBEAT_TOKEN | `touka_heartbeat_secret_2026` | `4be1d6c5`（2026-04-17）根 `.env.example` 與程式預設；`26f7c40c`（05-06）成為 Settings 預設；`0f69308e`（05-20）移除 Proxy 與 Heartbeat 兩處預設；`c8fdd646`（05-21）移出根 `.env.example`；`ToukaProxy/.env.example` 的註解留到本次才拿掉 |
| 2 | HEARTBEAT_TOKEN | `<your-generated-heartbeat-token>` | `c8fdd646`（05-21）根 `.env.example` 出貨 → `b11d5744`（07-04）移除 |
| 3 | HEARTBEAT_TOKEN | `your_secure_token` | `afdbf113`（05-06）`ToukaHeartbeat/README.md` 範例 → `b136c91c`（09-14）移除；文件範例，沒有出貨進範本 |
| 4 | TOUKA_AUTH_SECRET | `TOUKA_INSECURE_DEFAULT_SECRET_FOR_DEV` | `4be1d6c5`（04-17）`core/security.py` 沒設定時的退回值 → `7c35f7ec`（05-18）移除 |
| 5 | TOUKA_AUTH_SECRET | `touka_default_secret_2026` | `f32f3722`（04-23）`config.py` 程式預設；`26f7c40c`（05-06）成為 Settings 預設 → `7c35f7ec`（05-18）移除 |
| 6 | TOUKA_AUTH_SECRET | `<your-random-secret>` | `c455936e`（05-22）根 `.env.example` 出貨 → `b11d5744`（07-04）移除 |
| 7 | DISPATCH_TOKEN | `<your-generated-dispatch-token>` | `9f81633a`（06-12）根 `.env.example` 出貨 → `b11d5744`（07-04）移除 |
| 8 | DISPATCH_TOKEN | `touka_dispatch_secret_2026` | repo 歷史裡查不到它當過程式預設或出貨值；只出現在 `9fd354d3`（05-29）起的設定精靈「已知預設值」清單、ADR-0053 的說明與 `test_setup_endpoints.py`。【推測】是 repo 建立前或別處的預設 |
| 9 | DISPATCH_TOKEN | `your_secret` | `0aa6011c`（05-26）`ToukaDispatch/README.md` 範例 → `b136c91c`（09-14）移除；文件範例 |

- 第 2、6、7 個是 2026-05-21～07-04 新安裝時會被自動複製進 `.env` 的實際值。第 3、9 個是文件範例，照著文件做的人會設成這個值；真正的隨機值不可能等於它們，列進去沒有副作用。

## 規則鬆緊

- D1 加嚴；D2 加嚴（計畫早期寫的 `-rs` 會放寬，已改成 `-rfEs`）；D3 不變；D5 加嚴。
- D4 是唯一的放寬：清理檢查原本看到這 7 種建置產物就停下問人，生效後工作資料夾只有這 7 種（加上可重建快取）時直接清。其他東西照舊擋下。

## 決策落點表

| 決策 | 檔案 | 函式／設定 | 測試 |
|---|---|---|---|
| D1 前置條件 | `AGENTS.md` | 「直譯器主權」blockquote 之後、「每次任務完成後必須執行」之前的「🪟 Windows 本機跑後端測試的前置條件」段 | `test_documentation_contracts.py`（禁令編號與範圍不受影響） |
| D1 佈建對齊 CI | `CONTRIBUTING.md` | 「環境設置」第 2 步：`ToukaChat`、`ToukaIDE` 各 `npm run install:controlled` | 拋棄式複本實跑（見驗證） |
| D2 結尾摘要 | `pytest.ini` | `addopts = -rfEs` | `test_pytest_skip_reasons.py` |
| D2 找 bash | `tests/unit/test_installation_entrypoints.py` | `_git_for_windows_bash()`、`BASH`、`_bash()` 的跳過訊息 | 同檔新增 4 組 8 個 |
| D3 刪沙盒 | `tests/sandbox_cleanup.py` | `remove_session_sandbox()`、`ReleaseStep`、`RMTREE_ATTEMPTS` | `test_session_sandbox_cleanup.py` |
| D3 釋放步驟 | `tests/conftest.py` | `SANDBOX_RELEASE_STEPS`（`_drain_policy_audit_writes` → `_close_qdrant_clients` → `FactualStore.close_all_connections` → `close_sandbox_log_handlers`） | `test_session_sandbox_cleanup.py::test_conftest_release_steps_cover_every_known_handle` 等 |
| D3 收尾點 | `tests/conftest.py` | `atexit.register(_cleanup_session_sandbox_at_exit)`（`_SANDBOX_OWNER_PID`）、`global_test_sandbox` 收尾、`pytest_sessionfinish(trylast=True)`，三者都呼叫 `_cleanup_session_sandbox()` | `test_session_sandbox_no_residue.py`（子程序 pytest，探針 `sandbox_child_probe.py`） |
| D3 殘留回報 | `tests/conftest.py` | `pytest_terminal_summary`、`SANDBOX_RESIDUE_MESSAGE`；atexit 寫 stderr | `test_session_sandbox_cleanup.py` |
| D3 文件 | `docs/architecture/MEMORY_INTERFACE.md` | 關閉接縫那條補「pytest session 結束時也會呼叫」 | — |
| D4 宣告 | `rebuildable-outputs.txt` | 7 行（上表） | `test_rebuildable_outputs_declaration.py` |
| D5 對照表 | `ToukaProxy/src/config.py` | `PUBLIC_SECRET_VALUES`、`is_public_secret()`、`usable_secret()` | `test_public_secret_values_security.py` |
| D5 心跳 | `ToukaProxy/src/config.py` | `_resolve_heartbeat_token()` 開頭經 `usable_secret` | 同上（含 `verify_heartbeat_token` 拒絕公開值） |
| D5 登入金鑰 | `ToukaProxy/src/core/security.py` | `AuthSecretManager.get_secret()` | 同上（含公開金鑰簽的心跳 JWT 被拒） |
| D5 派工 | `ToukaProxy/src/services/dispatch_supervisor.py` | `DispatchSupervisor.build_child_env()`（經 `usable_secret`，最後刪掉繼承來的公開值） | 同上 |
| D5 設定精靈 | `ToukaProxy/src/services/setup_service.py` | `SetupService._is_dispatch_token_default()` | 同上、`test_setup_endpoints.py` |
| D5 範本 | `ToukaProxy/.env.example`、`tests/unit/test_sec_batch_delta.py` | `# HEARTBEAT_TOKEN=`；`test_env_example_has_no_real_secrets`（`git ls-files` 抓 `*.env*.example`）；刪 `test_env_heartbeat_token_cleared` | `test_sec_batch_delta.py` |
| D5 文件 | `SECURITY.md`、`docs/architecture/SSOT_MASTER_MAP.md` | Known Resolved 三列；對照表登記為 SSOT 元件 | — |

## 考慮過、不採用的做法

- 本機缺 bash 時直接失敗（`_bash()` 在非 CI 時 `pytest.fail`）：沒有 Git 的機器整片紅，還要再加開關。改成列在結尾摘要。
- 只加 `-rs`：會把預設的 `fE` 整個換掉，結尾不再列失敗與錯誤。
- 只靠 `pytest_sessionfinish` 或 `pytest_unconfigure` 收尾：`pytest_configure` 丟 `UsageError`（`--basetemp` 被拒）或 `--help` 時都不會跑到。改用 atexit。
- 下次執行時清掉以前留下的沙盒（延後清除）：要分辨並行的其他 pytest，還會自動刪 repo 外的東西；修完後只剩強制結束的少數，列待排。
- 改產品程式加 flush 方法給稽核服務：conftest 對 executor 送一個空工作就能等到佇列清空，產品不必改。
- 在 conftest 頂部匯入向量模組：每次 pytest 多約 10 秒。
- 宣告也列 `scripts/.cache/`、`site/`、沒有腳本產生的 `out/`／`build/`／`coverage/`、根 `.cache/`、Playwright 輸出：工作資料夾不會產生、重建方式沒驗證，或裡面有不能重建的東西。資料、設定、金鑰永遠不列；可重建快取名稱本來就允許刪，列了反而整份失效。
- 宣告檔加進 `.dockerignore`：內容無害，不加。
- 第 5 項只刪讀真 `.env` 的測試、產品不改：等於放寬。
- 第 5 項只擋心跳 token：登入金鑰若是公開值，仍可簽出心跳 JWT，只擋一半。
- 對照表放在 `routers/heartbeat.py`：既有測試斷言那個檔不含公開字串，而且四個模組要共用。

## 實作偏離

- U1（單位驗收時經審查接受）：Qdrant 那步不在 conftest 頂部匯入，只在向量模組已載入時關；`pytest_sessionfinish` 也呼叫清理（計畫原本只讓它記錄殘留）；atexit 多一個 PID 檢查與寫 stderr 的那一行。
- U2：宣告測試判斷「有沒有被 git 忽略」時，只認非空、非否定（不是 `!` 開頭）的規則。原因：Windows 的 CRLF checkout 裡，`.gitignore` 的空白行會變成空 pattern，`git check-ignore` 對任何沒被追蹤、寫成結尾 `/` 的路徑都回「被忽略」；照計畫只看結束碼的話，拿掉 `ToukaIDE/.gitignore` 的 `dist/` 測試仍是綠的。改成只認真正的規則後這個突變會紅。
- AGENTS.md 前置條件（經審查核可）：附錄 A 原文漏了 `ToukaChat` 的 `npm run gen:artifact-runtime`，最終驗收時新工作資料夾的 guard_all 因此在 Guard #27 型別檢查失敗，所以在「前端依賴」那一條補上這一步。維護者核准時看過的版本沒有這一句，其他字不變。
- NOQA_REGISTRY：刪掉明細表裡過時的 `config.py` L60、L64 `S105` 那一列（`config.py` 現在沒有 `S105` 的 noqa，不在掃描結果裡，統計數字不變）。
- NOQA：U1 新增 1 個 `S603`（`tests/integration/test_session_sandbox_no_residue.py` 的 `_run_child_pytest`），統計 29 → 30；conftest 的 `E402` 加 2 個、刪 1 個（格式類，不在統計表）；U3 刪掉 1 個 `S105`（隨刪除的測試），「其他」列照 Guard #19 的掃描法實際清點：`S105` 19 → 18、合計 327 → 326。

## 已知限制

- 強制結束（工作管理員、kill、當機）的 pytest 仍會留下沙盒；延後清除列待排。【推測】734 個舊沙盒裡約 352 個只有沙盒自己的設定、沒有測試產物，來源沒有證實；修正已涵蓋所有正常結束的程序。
- 宣告只在合進 `main`、主資料夾 `git fetch origin` 之後生效；建置產物底下若有 junction 或連結，清理檢查仍會擋。
- 清理檢查判斷宣告路徑有沒有被忽略時也試加結尾 `/` 的寫法，所以在 CRLF checkout 上會把沒被忽略的路徑當成被忽略（拋棄式複本實測：多一行沒被忽略的 `scratch-not-ignored/`，宣告仍被採用）。它仍用 `git status` 擋下沒被忽略的檔，不會因此多刪；宣告的這類錯誤由本 ADR 的宣告測試先擋。
- Heartbeat 自己的 8082 排程 API（`ToukaHeartbeat/awaken.py`，經 `StorageService.get_token`、程序環境優先）在容器重建前仍接受原值；這個埠只綁本機，執行一次 `docker compose up -d` 後就關上。
- ~~設定精靈的 `apply` 仍可寫入公開值，下次解析時才換掉~~（ADR-0254 已修：回 422、`.env` 不寫）。
- 單獨執行 ToukaDispatch（不經 Proxy 啟動）時不經這份對照表。
- fail-closed 時公開值仍留在 `os.environ`（解析點不採用它，但沒有清掉）。
- 登入金鑰被換掉時所有人要重新登入一次。
- 把 A 金鑰的公開值填在 B 金鑰不會被擋（判定看「鍵名＋值」，這點刻意不改）。

## 驗證

本機 Windows、Python 3.12 venv、fnm Node 24.21.0，從 PowerShell 跑，PATH 前面依序放 `.venv\Scripts`、`Git\bin`。本任務沒有打任何付費 API，也沒有啟動 Touka；自動化測試都在沙盒裡，沒有讀 `.env`。

- 基準（`ff7e2855`）：固定順序與隨機順序（seed 20261014）都是 8765 passed／8 skipped／6 failed，6 個是 E1。
- U1（第 2、3 項，`c33eca55`）：新測試 27 個先紅後綠；突變 15 個都紅（拿掉 mingw64 那層、每個位置都多試一層、ini 改回 `-rs` 或拿掉、拿掉關 Qdrant、拿掉等稽核、拿掉 atexit、清單少任一步、拿掉結尾那行、拿掉 PID 檢查、拿掉 stderr 那行等）；9 個 bash 測試在 Git Bash 與 PowerShell 各跑一次都 PASS；全量固定順序 8792 passed／8 skipped／6 failed（E1），跑完系統暫存沒有新增 `touka_test_session_*`。
- U3（第 5 項，`76b91241`）：新測試 92 個先紅後綠；突變 18 個都紅（拿掉任一解析點的檢查、拿掉派工刪除繼承公開值那一步、拿掉沿用 `.env` 那一步、只比值不比鍵名、不去空白、範本改回公開值、精靈改回舊清單、對照表少任一個值）；受影響的既有測試 689 passed、6 failed（E1，與基準相同）。
- U2（第 1、4 項與文件）：宣告測試 4 個先紅（檔案不存在）後綠；突變 3 個都紅（加一行 `.venv`、加一行沒被忽略的 `ToukaChat/src/lib/`、拿掉 `ToukaIDE/.gitignore` 的 `dist/`）。拋棄式複本（`origin/HEAD` 指到含宣告的分支、工作資料夾放 7 種假產物）：清理檢查印「已採用宣告」並通過；多放一個沒宣告、被忽略的 `backups/x` 就擋下；`origin/HEAD` 指到沒有宣告的 `dev` 時 7 種都擋下。`CONTRIBUTING.md` 的佈建在同一個複本照 CI 實跑：只跑 `npm ci` 時 `gen:artifact-runtime` 回 `not-patched`，`install:controlled` 後通過；`ToukaIDE` 沒裝依賴時 companion 測試 7 個 ERROR，`install:controlled` 後 7 passed。
- 任務分支整體（`252a19cc` 的工作資料夾）：
  - guard_all 43／43。第一次 Guard #27 失敗，是新工作資料夾沒有 `ToukaChat/src/generated/artifactRuntime`（環境，不是程式問題）；在 `ToukaChat` 跑 `npm run gen:artifact-runtime` 之後全過。
  - pytest 固定順序與隨機順序（seed 20261014）都是 8888 passed／7 skipped／6 failed，6 個是 E1 的 `test_local_compose_bindings`。失敗清單和基準逐一相同；跳過清單＝基準減掉被刪的 `test_sec_batch_delta::test_env_heartbeat_token_cleared`。
  - 兩次各自的 session 沙盒跑完都已不存在，這段時間系統暫存沒有新增任何 `touka_test_session_*`。

## 回復

- 不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）；各項可獨立 revert。
- D4 撤回：刪掉 `rebuildable-outputs.txt` 並合進 `main`。
- D5 撤回不會把已換掉的 `.env` 值改回公開值，也不該改回。
