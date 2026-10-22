# Security Policy

本文件說明 Touka 的支援範圍、弱點通報方式、安全架構，以及已知與已修復的產品弱點。發行流程已簡化為最小 GHCR 發布（[ADR-0238](docs/architecture/adr/0238-retire-release-qualification-machinery.md)），本文不記錄發版或驗收程序。

## Supported Versions

| 版本 | 狀態 |
|------|------|
| `.github/workflows/ghcr-publish.yml` 發布到 GHCR 的最新映像（`touka-proxy`／`touka-heartbeat`／`touka-chat`） | ✅ 支援 |
| 其他版本 | ❌ 不再維護 |

安全修補只進入之後發布的映像；請更新至 GHCR 上的最新映像。

---

## Reporting a Vulnerability

**請勿在公開 GitHub Issue 中揭露安全弱點。**

請透過 GitHub 的私下漏洞回報通報：

🔒 **<https://github.com/OtohaTouka/Touka/security/advisories/new>**

通報時請盡量提供：
- 弱點類型（例如：身分驗證繞過、路徑穿越、指令注入）
- 受影響的元件與路徑（例如：`ToukaProxy/src/routers/terminal.py`）
- 重現步驟或概念驗證（PoC）
- 潛在影響評估

**回應時間**：本專案由個人維護，會盡力確認並回覆修復計畫，但無法保證固定時限。

---

## Security Architecture

Touka 為本機部署系統（localhost），安全邊界設計如下：

| 層次 | 機制 |
|------|------|
| **身份驗證** | JWT（`TOUKA_AUTH_SECRET` 環境變數，未設定時自動生成隨機密鑰） |
| **授權** | RBAC + ABAC 四層授權（Owner / Debugger / Base / Subagent）；只有靈魂、DebugAgent、DispatchAgent 能有代理角色與能力，其他 ID 一律 Base（ADR-0245） |
| **角色權限解析** | `PermissionService.resolve_grants()` 是工具／技能／MCP 唯一解析器；角色 deny self-only、全域 deny 地板約束所有角色（含 Owner），無 Owner 硬編旁路（ADR-0196） |
| **權限設定生命週期** | `permissions.json`：bootstrap 才可用最小 fallback；啟動壞檔 fail-fast；熱重載壞檔保留 last-known-good＋AUDIT＋degraded health，成功後復原（ADR-0196） |
| **原生工具執行授權** | builtin／Memory 實際執行前核對 verified identity 的 RoleGrants，先於 PaC／HITL；終端／自帶批准例外只來自 builtin 註冊時封存的 callable，模型 `command` 不決定分類（ADR-0228、ADR-0229） |
| **工具 HITL（全家族）** | 三種模式 Manual／Auto／YOLO（ADR-0246；舊值讀取時對照，ReadOnly、SafeAuto→Manual，FullYOLO→YOLO；無痕對話一律 Manual）× 工具風險分類（`auth_policies.json` `tool_risk`：read_only/safe/dangerous，MCP/未列名預設 dangerous）決策表；CONFIRM **fail-closed**——互動會話走聊天內審批卡（統一 broker `approval_service`＋`POST /v1/tools/approve\|reject/{exec_id}`，**限人類**解算：admin／agent 擁有者／session 擁有者，agent token 不可自批；180s 逾時自動拒），headless（heartbeat/派工）在設定時事先定權、無通道即拒。分類 parity 由 Guard #32 棘輪執法 |
| **終端機執行** | 同上統一 HITL；終端一律算 dangerous（`safe_whitelist` 已拿掉），Auto 下先交審查員；黑名單攔截（恆拒、凌駕 YOLO）；headless 保留遺留 ToukaUI 通道語意 |
| **隔離執行環境** | 代理人撰寫或指定的程式（終端、互審驗證指令、演化）在執行器容器（`touka-executor`）裡經 Landlock 啟動器執行，看不到金鑰、設定、原始碼與其他代理人資料（但書：`/proc/*/cmdline` 讀得到，token 不要放在 MCP 的 args；同 uid 檔案的權限位元仍改得到，代理人工作區由自癒補回，見 ADR），連不到也搶不到 Touka 內部埠、Docker 遠端控制埠與從設定推得的本機上游埠；執行器的控制通道用檔案權杖加 `SO_PEERCRED` 認證；層級只由 `services/isolation` 判定，`none` 時 Auto／YOLO 只有人能核准、無人在場拒絕，卡片如實標示「在執行器裡／隔離降級／未隔離」並綁定核准；有隔離不放寬任何規則。C2-3 已涵蓋 stdio MCP 與創建試跑：自建依層級選 runner，第三方與 scaffold 看本機 Landlock；穩定的不可用原因仍容許第三方未隔離啟動，自建 none 只接受一次人類同意。完整邊界見下方 MCP 已知限制及 ADR-0275 |
| **底線與 Auto 審查員** | 程式常數底線 F1–F7 在 `evaluate` 最前面判斷、設定檔關不掉：刪改資料、讀金鑰、改權限設定、改原始碼、系統破壞三種模式都只有人能核准，F5、F6 在 Auto 下只有人能核准；Auto 審查員只收型別化判決、失敗一律不放行、不能推翻底線、無人在場不送審（禁令 64，ADR-0246） |
| **模式授權** | 只有 admin 或該靈魂的擁有者能把對話存成 Auto／YOLO，在 session 寫入時判定；請求參數不是模式來源（ADR-0246） |
| **檔案存取** | ResourceManager 路徑主權隔離（`is_relative_to()` + `validate_no_symlink_escape()` 雙重檢查）；**原子寫入**（temp+fsync+`os.replace`）防寫到一半損毀（ADR-0078） |
| **心跳重新觸發（UI）** | `POST /v1/heartbeat/retrigger` 限 admin 或人類擁有者（Debugger 不旁路）、對話須屬於該靈魂 + 能力閘（`can_awaken`）+ **強制 Auto**（無人在場不跑終端、不叫審查員），永不升級 YOLO（ADR-0078、ADR-0246） |
| **ToukaDispatch 認證** | 只提供 Streamable HTTP `/mcp`，舊 `/sse`＋`/sse/messages/` 回 404；DISPATCH_TOKEN／一次性 mission_token handshake 換 session token（TTL=3600s），每次 tool call 重驗 transport/audience/method/generation；store 只留 digest 與授權分面（ADR-0201） |
| **MCP 通訊** | ToukaIDE 只綁 loopback；`/mcp` 每個 request 都重驗啟動期隨機 Bearer，空 token fail-closed，Host／Origin 另驗證（ADR-0202） |
| **分級授權（IDE）** | Manual／YOLO 兩種（IDE 沒有審查員），黑名單在兩種模式都直接拒絕；人類在 Touka 卡片核准過的那一次呼叫帶 HMAC「由人核准」標記才免第二次確認（ADR-0246） |
| **ToukaIDE Webview** | script／style 只載入 VSIX 封裝內 `${cspSource}`，禁止 CDN、inline script/style 與 Webview connect；Extension→Webview 動態值一律以 `textContent` 顯示、保留換行，不解析 Markdown／HTML（ADR-0219） |
| **創建端點（Creation Engine）** | `/v1/creation/*` 限 Owner/admin（`_require_creation_role`）；scaffold 生成以識別字校驗 + `repr()` 轉義 + `ast.parse()` 兜底 + 單段 slug 防穿越；CREATE 不覆寫既有 id；由 Creation Sovereignty Guard #29 執法（ADR-0069） |
| **安全標頭** | 全域 `SecurityHeadersMiddleware`：X-Content-Type-Options / X-Frame-Options / Referrer-Policy |
| **請求體大小** | `RequestSizeLimitMiddleware`：超過 10MB 回 413，防 DoS |
| **Refresh Token Replay** | jti 輪換機制：重用舊 token 立即撤銷全部 session |
| **Access Token 撤銷（改密碼）** | access token 帶 `iat`；密碼變更（自助或 admin 代改）蓋 `User.password_changed_at`，兩道 Bearer 閘（`get_current_user`＋`verify_token`）一致拒絕改前簽發的 token——無 1h 殘活窗，被竊 token 隨改密碼即死（ADR-0144） |
| **LDH（本機優雅降級）** | 僅「系統未認領」（首裝：尚無任何完成真密碼設定的使用者）可降級發卡；認領後無卡本機請求一律 401 fail-loud、永不再鑄 DebugAgent 卡；前端「曾登入過的瀏覽器」永不採納降級卡（雙保險；ADR-0131→ADR-0144） |
| **依賴與映像** | Python／Node 依賴由 Guard #22／#23 稽核（HIGH／CRITICAL 阻擋，ADR-0222／0223）；映像的 Node／npm 取自 digest 釘版官方 node 映像，Debian 套件於建置時以 `apt-get upgrade` 取官方安全更新（ADR-0238） |
| **Docker 容器** | 三個服務均以非 root `appuser` 執行（USER directive） |
| **自動化測試網路封鎖** | pytest 與自動 smoke 以 audit hook 擋下非本機的解析與連線並讓測試失敗，並清掉代理變數（`NO_PROXY=*`）；只有先過 `preflight_live_api_cli` 的人工實測工具可開放（ADR-0241；子程序不在封鎖內） |
| **Python 套件冷卻期** | uv `exclude-newer = "7 days"`，只採用上架滿 7 天的套件；逐套件例外需 Owner 同意；npm 不在內（ADR-0241） |

> ⚠️ **雲端部署必讀**：Touka 預設設計為本機使用。若需暴露至公網 VPS，請務必：
> 1. 設定 `CLOUD_MODE=true`（停用 LDH 自動發卡，改用 X-Real-IP 識別真實客戶端）
> 2. 設定 `TOUKA_AUTH_SECRET`（≥32 字元隨機值）和 `HEARTBEAT_TOKEN`
> 3. 設定 `SECURE_COOKIES=true` 並在 nginx 反向代理層加入 TLS（HTTPS）
> 4. 設定 `ALLOWED_ORIGINS` 為你的前端域名（防止 CORS 繞過）
>
> 詳細部署步驟請參考 [`docs/DEPLOY_VPS.md`](docs/DEPLOY_VPS.md)。

---

## Known Open Vulnerabilities

### MCP 隔離的邊界與已知限制（C2-3）

- 第三方／scaffold 在本機 Landlock 穩定不可用時仍直接啟動（包含原生 Windows、舊 kernel 映像）；它們仍有 Touka 的檔案權限。檢查中或暫時故障時等待，不先放行。自建在層級 `none` 不自動啟動，人類確認後的 direct 同樣沒有隔離。
- 本機 ABI 3 可隔離檔案但不能封鎖 TCP 埠；Fast Open／MPTCP 或封鎖埠資訊不完整時，保護能力以 `features` 為準，不能把 Landlock 徽章當成完整網路保護。對外網路不是一律封鎖。
- 合法 scaffold 的 APPDATA 資料哨兵對映到所有代理人的 personal_drive 根，並非呼叫者專屬；DebugAgent 等 custom drive 不在其中。Filesystem／docgen 改判自建時不取得這份第三方授權；經人同意直接啟動則仍有 Touka 的檔案權限。自建 MCP 的工具可由獲授權的其他代理人共用，同一台共用 state。
- 任一參數或 path-like env 指向代理人可寫位置均當自建，包含普通資料夾與不存在的路徑；程式參數中的現存資料夾也從嚴分類。Filesystem／docgen 可能因此不能沿用第三方資料夾權限。分類器不會完整解譯任意 shell 程式、變數展開或動態產生的路徑。
- 路徑解析不改寫 argv；隔離 cwd 保持 state home，需要 Proxy cwd 的相對腳本須改用絕對路徑。分類與資料夾授權使用解析路徑，不能保證任意指令文字的執行語意。
- 自建只寫自己的 state，已部署程式目錄唯讀；`create_mcp` 指向代理人自己資料夾裡的腳本時，隔離下讀不到而起不來。請用演化部署程式。
- 每台各有 HOME／TMP／下載快取，無磁碟大小上限，首次 npx／uvx 下載可能超過握手期限。執行器的記憶體／行程／CPU 配額是共用的，不能保證單台不影響其他台。
- 隔離 shared stdio 的失聯（含 OOM、自己結束）會在 5／10／20 秒退避重試三次，之後要人按重啟；direct 與遠端不自動重連。人同意降級 Landlock 的自建 MCP 失聯後，若層級仍是 `none`，會回到等隔離，不重用同意。
- 原生 Linux 上，直接子行程有父死訊號，孫行程沒有；Proxy 被強殺可能留下孫行程，Docker 容器退出則由 PID namespace 收尾。群組長已被回收但孫行程仍持 stdout 時，EOF 不會到，不能安全地再按舊 pgid 殺群組；關閉 pipe 不代表殘留行程已終止。
- 執行器關閉 relay 後的遠端群組回收是非同步；同一 state 的新舊行程可能短暫重疊，沒有同 owner 排隊保證。
- `/proc/*/cmdline` 跨代理人可見，MCP args 不可放 token。`exec_env` 是執行目標及其子行程的環境，不是對該目標隱藏祕密；診斷遮罩亦不保證遮住短於四字元的值。
- 隔離啟動的 `checked_exec_env` 會先移除保留名稱與命中子字串規則的名稱，只記 log 警告（名稱、不含值）。保留名稱不分大小寫：`HOME`、`TMPDIR`、`TMP`、`TEMP`、`GIT_CEILING_DIRECTORIES`、`PYTEST_ADDOPTS`、`TOUKA_EXEC_ID`、`HF_HOME`、`HF_HUB_CACHE`、`TRANSFORMERS_CACHE`、`TORCH_HOME`、`PIP_CACHE_DIR`、`UV_CACHE_DIR`、`UV_TOOL_DIR`、`UV_PYTHON_INSTALL_DIR`、`NPM_CONFIG_CACHE`、`NPM_CONFIG_PREFIX`，以及 `XDG_`、`GIT_CONFIG_` 前綴；不是概括移除所有 `TMP*`、`UV_*`、`NPM_CONFIG_*`。
- `exec_env_stripped` 以不分大小寫的子字串比對 `PYTHON`、`NODE_`、`JAVA_`、`GEM_`、`PERL`、`LD_`、`DYLD_`，不限前綴或用途；`NODE_ENV`、`MY_PYTHON_TOKEN`、`NODE_EXTRA_CA_CERTS` 也會移除。伺服器若支援，改用不衝突的名稱；必須使用原名時，只能走既有規則允許的直接啟動路徑，不因此自動降級。
- 經過濾後仍留下、但不符合 `[A-Za-z_][A-Za-z0-9_]{0,127}` 的 env 名稱，才由 kernel 回 `spec_invalid`、不啟動；需改用英數與底線、首字不能是數字。格式錯誤與上述只記 log 的名稱移除是兩種不同處理。
- `temp/docgen_output` 不在權限自癒白名單；權限被改壞時 docgen 可能寫不出檔。遠端 URL MCP、IDE 終端與 Dispatch 自動召喚不在這套子程序隔離範圍。

### 演化部署的審批範圍與設定競爭

部署核准只綁定 `server.py` 與 `requirements.txt` 的原始位元組雜湊，沒有綁定整個 `managed/<skill>`。建置或驗證留下的 `.venv` 內 `.pth`、套件及同名遮蔽模組仍可能在部署後執行；兩檔雜湊相同不代表全部執行內容經過人工審查。部署未改為全目錄快照或乾淨重建。

`_save_deployment` 與設定頁沒有共用讀改寫鎖；同技能鎖只序列化演化工具，並行存檔仍可能覆蓋別人的設定。

Windows 的 JailedWriter 只提供可繞過的寫入配額，不是檔案讀取或程序隔離；唯讀開啟與整數 fd 直接通過，`os.open`／`io.open` 等亦可繞過。Windows 仍依未隔離執行的人類同意閘門控制。

### 演化建置與驗證的殘留程序

本機 Landlock 下，建置或驗證的孫行程若以 `setsid` 離開原程序群組，可能存活到雜湊核對或部署之後，並在退出前繼續持有 `managed/<skill>` 的寫入權。技能鎖與部署後凍結不撤銷既有程序的權限。

### Debian 系統套件（liblzma5）

映像內 Debian 套件改由建置時 `apt-get upgrade` 取得官方安全更新（ADR-0238）；先前對 liblzma5 `5.8.1-1+deb13u1`（TEMP-1147318-639065／GHSA-5qpq-xqfv-j9pg）的 `RISK_ACCEPTED_DEFERRED` 追蹤隨該管線退役而結束，之後以一般 Debian 更新重新確認。

### braces stack exhaustion（GHSA-vfj7-8cjw-p6xm，Chat／IDE；以防禦回補緩解）

`braces` ≤ 3.0.3 可因深層巢狀 pattern 造成 stack exhaustion DoS（HIGH）。Chat／IDE 維持 braces 3.0.3（lock URL／SRI 不變），以固定 PR72 head `28d440b5dd449dbf1fe6f3506cf94ecca4d02660` 的 depth 上限做同版本防禦回補：`ToukaChat/container-security/braces-backport/` 對實際安裝副本 apply 與唯讀 verify，未知、部分修補或額外副本一律拒絕。npm audit 的原始 HIGH 保留；Guard #23 只在完整可信修補內容與固定 GHSA 相依圖吻合時列為 covered，runtime HIGH、CRITICAL、其他 advisory 或未知相依圖仍阻擋。不宣稱上游已有修正版，也不執行漏洞重現（[ADR-0237](docs/architecture/adr/0237-braces-backport-installed-identity.md)）。

### Node 依賴的 moderate advisory

Guard #23 以 HIGH／CRITICAL 為阻擋門檻；最近一次記錄的 Chat／IDE audit 仍有少數 moderate advisory，隨上游相容修正版更新。

---

## Known Resolved Vulnerabilities

### 刪除靈魂的家目錄殘留、首裝密碼檔與密碼重輸（2026-10-21）

| 弱點 | 修正與驗證 |
|------|------|
| 刪除靈魂不清隔離暫存區的家目錄：執行器 `touka-executor-state` 與 proxy `touka-sandbox-state` 的 `agents/<id>/{home,tmp}` 留著，同 ID 的新靈魂沿用舊靈魂留在 `~` 的檔案（例如工具權杖；ADR-0272 已知限制 26） | 刪除在記憶檢查後、刪任何東西前清兩處：proxy 那一層直接刪（連結不跟）；執行器那一層走既有 START（kind agents、固定 `/bin/sh` 清除指令、不帶 proxy 的環境），不改協定、不碰執行器 volume。預期是執行器卻不能用、清不乾淨或逾時就 503 `SOUL_SANDBOX_HOME_NOT_CLEARED`、什麼都不刪，不降級。測試：`tests/unit/test_soul_delete_sandbox_home_security.py`、`test_executor_runner_security.py`（+4）、`tests/integration/test_isolation_linux.py::test_soul_delete_wipe_empties_its_executor_home` |
| 首裝一次性密碼檔 `ToukaProxy/config/INITIAL_ADMIN_PASSWORD.txt` 沒被 `.gitignore` 排除，`git add .` 會把管理員初始密碼收進 commit；私人開發期間曾被提交過一次，那組密碼對應的帳號早已不存在，那段歷史也不在這個公開 repo 裡 | `.gitignore` 加入；`tests/lint/test_dockerfile_dispatch.py` 以 `git check-ignore` 核對 |
| 密碼重輸打錯回 401，前端續期後重送：同一次點擊驗兩次密碼，還多觸發一次 refresh 輪替 | 重置（含清除連備份）、還原、設定匯入、清除／匯出我的資料、改密碼改回 403 `AUTH_REAUTH_FAILED`（比照靈魂步升），登入照舊 401。測試：`tests/unit/test_password_reentry_status_security.py`（`AUTH_INVALID_CREDENTIALS` 只准登入用）與各端點既有測試 |

### 心跳、排程與 SNS 對話歸屬（2026-10-17）

| 弱點 | 修正與驗證 |
|------|------|
| 靈魂同名重建、建立競態及覆蓋式還原讓預佔 UI 對話成為新靈魂歷史；前綴 ID 誤降其他靈魂標記；重置漏清舊別名 | `create_soul`／`update_soul` 補建在寫 config 前後各降正式 ID／別名綁定或家引用列 marker，失敗回 `SESSION_SAVE_FAILED`，第二次失敗回滾。拒絕 ToukaSoul 前綴的新／轉換 ID（不分大小寫、`VALIDATION_ERROR`），失效與搜尋共用正式化；重置只按 agent_id 清正式 ID／別名。Proxy 還原先原子寫持久標記，搜尋 ui／all 讀列前拒絕；成功或失敗均降現存對話庫全部 marker，成功才刪標記，失敗回 `SYSTEM_BACKUP_FAILED`、不重啟。啟動在 migration 後、bots 前補降，失敗保留標記並記固定 `SESSION_RESTORE_RECOVERY_FAILED`，不阻擋啟動；缺表／欄視為無列可降。還原 UI 對話永久不可搜尋。`test_soul_session_reuse_security.py`（38）涵蓋真 ZIP 覆蓋後中斷與啟動補降、搜尋封鎖及解除、啟動失敗保留標記、寫標記失敗不啟動子程序、缺表／欄、部分失敗、競態、回滾、前綴與正向對照。工具補建限同一靈魂；只保護當次還原資料根，還原 `.env` 改根、直接 CLI 還原、實體世代與 best-effort 回滾限制見 ADR-0277。 |
| 心跳／排程／SNS 可跨靈魂讀寫並重綁對話，家定位未驗 agent_id，一般 UI save 可改家欄位 | ADR-0276：共用純 scope 規則與交易內儲存閘；嚴格家快照；每靈魂頻道及退休 ID 拒寫；SNS 限同一人類擁有者的靈魂；僅授權人類 UI 可換綁、非空換綁 marker 降為 0；家欄只經 set_home。安全測試：`tests/unit/test_session_scope_storage_security.py`（44）、`test_session_scope_provenance_security.py`（11）、`test_heartbeat_session_scope_security.py`（18）、`test_sns_session_scope_security.py`（9）、`test_session_scope_dispatch_security.py`（15），涵蓋 HTTP 403、競態與拒絕後不寫／不外送。舊資料不回填、不遷移；marker 與並發建家的限制見 ADR。 |

### MCP 隔離連線與手動啟動

| 弱點 | 修正與驗證 |
|------|------|
| 非人類只改 env 或匯入同名設定就可沿用第三方來源與人類資料夾授權 | 在遮罩回填後比對 env 名稱及值，變動就按自建重綁來源、清掉資料夾，先寫隔離紀錄再寫設定；省略 human 時預設非人類。`test_mcp_origin_sandbox_paths_security.py`、`test_mcp_isolation_store_security.py` 驗證存檔、匯入、刪除、遮罩與人類換 token。 |
| MCP 可展開路徑與隔離 cwd 不同而漏判來源 | command／argv／env 的 tilde、`$`、反引號、`%xx%`、`!VAR!`、萬用字元、大括號與單字內引號從嚴當自建；秘密或 URL 編碼值亦可能觸發。分類檢查 Proxy cwd、自建及第三方 state home，尚不存在的無分隔符檔名（帶副檔名）也算；profile 依實際 cwd 授權，不改寫 argv。合法 scaffold 的 APPDATA 資料哨兵仍套用 personal_drive 對映。`test_mcp_origin_classification_security.py`、`test_mcp_launch_isolation_security.py`、`test_mcp_profiles_security.py` 與 `test_creation_isolation_grant_security.py` 覆蓋。 |
| 程式載入根與 state home 相對引用可漏過部署凍結 | command、程式 env 或選項指到 managed 根或祖先時凍結全部技能，只排除普通 data 根資料參數；凍結與啟動共用 cwd／state home，設定讀不到仍拒絕。`test_mcp_origin_classification_security.py` 與 `test_evolution_deployed_freeze_security.py` 驗證全面凍結、相對引用寫入前拒絕及 Filesystem 例外。 |
| 程式資料夾、套件索引與路徑別名可逃過來源分類 | 通用規則檢查 command、所有參數與 path-like env，代理人可寫位置不再只認檔案或已知啟動器；程式參數中的現存資料夾從嚴判定。涵蓋 shell、npm exec、pipx、poetry、pypy3、ts-node 與未知包裝器；UNC／裝置名稱從嚴處理，macOS 比對忽略大小寫。解析不改寫 argv。`test_mcp_origin_classification_security.py`、`test_mcp_profiles_security.py`、`test_mcp_launch_isolation_security.py` 覆蓋。 |
| MCP 不讀 stdin 時阻斷 STOP／斷線處理，執行器留住程序 | 控制讀取與 pipe 寫入分離、有界輸入佇列滿即終止；Windows 可跑的背壓測試檢查殺群組與登記清空，Linux 另有真 relay 斷線案例。`test_executor_server.py`、`test_isolation_linux.py` 覆蓋。 |
| 舊重載或重試可取得新版 generation、覆蓋新連線；一次同意可啟動多次 | 重載在等待前更新版本，快照與重試綁原版本，清理序列化且過期工作退出；帶同意的握手不重試。`test_mcp_isolation_reconnect.py`、`test_mcp_manual_start_wiring.py` 驗證交錯與失敗。 |
| stderr 控制字元可偽造日誌呈現；創建 smoke 缺 context 時自行取得授權環境 | stderr 遮罩後跳脫 C0／C1（tab 除外）與方向控制字元；smoke 缺 context 直接回隔離不可用。`test_mcp_isolated_transport_security.py`、`test_creation_isolation_grant_security.py` 覆蓋。 |
| MCP 程式能直接讀取 Touka 檔案，或假裝第三方取得較寬權限 | `mcp_origin` 先核對引用再分類；隔離紀錄與程式身分綁定、匯入不採納來源、單向標為自建。`mcp_launch` 是啟動判定來源，自建／第三方／scaffold 各用對應 profile，未判定不得連線。`test_mcp_origin_classification_security.py`、`test_mcp_profiles_security.py`、`test_mcp_launch_isolation_security.py`、`test_mcp_origin_sandbox_paths_security.py` 覆蓋。直接啟動例外見已知限制。 |
| 條目 env 在限制套用前影響啟動器 | `exec_env` 經 spec fd 傳遞，限制套完才合併進目標環境；保留名稱拒收、loader 控制變數移除，不寫啟動器的 `os.environ`。`test_launcher_exec_env_security.py`、`test_mcp_exec_env_no_leak_security.py` 覆蓋邊界及診斷遮罩。 |
| 未隔離審批卡未顯示完整執行內容卻仍可同意 | 卡片與後端都檢查內容完整性；任一參數超過 65,536 字元或無法提供執行程式時拒絕，重新取得去處與 features 核對。同意只用一次。`test_unisolated_card_completeness_security.py` 覆蓋。 |
| 已部署的 MCP 可被演化覆寫，或輸出成功字串冒充驗證通過 | 四入口與 runner 取得前以 ASCII 白名單驗單層名稱，拒絕 Windows 保留名稱及非字串；凍結涵蓋父子路徑、大小寫、尾點／空白及實體別名。同技能互斥、拒絕撞名，只對 `server.py` 與 `requirements.txt` 各算原始位元組 SHA-256；沒有完整快照或核對失敗一律不部署、不寫隔離紀錄，完整目錄未綁定（見已知限制）。小於 1 的正配額夾到 1，避免移除 OS 上限。驗證改用 modern MCP 回應，錯誤與缺少啟動均不通過；`test_evolution_deployed_freeze_security.py`、`test_evolution_verify_protocol_security.py` 及 Linux 真隔離案例覆蓋。 |
| 隔離 MCP 的執行期錯誤、例外鏈與健康資訊可能回顯解析後的環境值 | 傳輸、manager 與資料 API 共用多行與 Python／JSON 跳脫值遮罩，在原文合併重疊區間，避免漏尾；公開工具網址與 metadata 鍵不遮罩，其餘值保留四字元門檻。保留實際啟動環境值供失聯與健康診斷使用，工具及資料 API 例外不串接原始錯誤，關閉錯誤只記型別。`test_mcp_exec_env_no_leak_security.py` 覆蓋 log、traceback 與 API 顯示欄位。 |
| 手動啟動同意可能比確認畫面弱，或在等待鎖期間失效 | 重啟 body 必帶並核對 features；鎖內重驗自建等待狀態、generation、bound、去處與 features。`test_mcp_manual_start_security.py`、`test_mcp_manual_start_wiring.py` 覆蓋同意不符與競態。 |
| principal-scoped stdio 可由 lazy 連線繞過啟動判定 | 在 owner 建立及重試前拒絕，MCP 頁以 `MCP_STDIO_PRINCIPAL_UNSUPPORTED` 顯示原因與改法；五條入口的判定失敗均不啟動。非遠端 `connect_server` 另要求 `McpLaunch` 或 `_judge` 的直接啟動標記，拒絕未判定與偽造標記。`test_mcp_isolation_reconnect.py` 覆蓋工具列舉、工具呼叫與零 transport 呼叫。 |
| 隔離握手的 JSON-RPC 錯誤可能把 exec_env 值帶入重試與 manager log | 先取根因、遮罩 launch 的實際環境值，再換成不串接原始例外的 `ConnectionError`；manager 只記遮罩後的訊息。`test_mcp_isolation_reconnect.py` 以假憑證驗證三次握手失敗無外洩。 |

### 依賴弱點修補（2026-08～10）

| 套件（範圍） | Advisory | 版本變更 | 備註 |
|------|------|------|------|
| compression（Chat，經 serve 14.2.6） | CVE-2026-87776／GHSA-vc2v-76pw-4v95 | 1.8.1 → 1.8.2 | serve 最新版仍精確依賴 1.8.1，以 `overrides.serve.compression` 指定；新增官方相依 destroy 1.2.0 |
| source-map-js（Chat／IDE） | CVE-2026-93749／GHSA-68fv-2mgg-jv7q | 1.2.1 → 1.2.2 | 依原 semver 定向更新 lock |
| proxy-addr（Chat／IDE） | CVE-2026-90711／GHSA-jqcg-44mw-7w3h | 2.0.7 → 2.0.8 | 依原 semver 定向更新 lock |
| MCP TypeScript SDK（Chat shadcn dev；IDE 測試 client） | GHSA-6qxp-vccf-f47h／CVE-2026-104850 | SDK 1.29.0 → 1.31.0；client 2.0.0 → 2.2.0 | 產品來源未使用 SDK OAuth client；日後若接實際 OAuth provider，仍須依官方要求設定 expectedIssuer 並遷移舊 issuerless credential |
| sharp（Chat runtime） | GHSA-wq5f-xc86-pv6w（librsvg CVE-2026-96889） | 0.35.4 → 0.35.5 | 官方 prebuilt 含 librsvg 2.63.2 |
| shell-quote（Chat dev） | GHSA-pqg4-j6r4-53mv／CVE-2026-102422（CRITICAL） | 1.9.0 → 1.11.0 | 官方修正版 |
| multidict | CVE-2026-104874／GHSA-54p9-h82j-f925 | 6.7.1 → 6.9.1 | 不升 major 7 |
| PyJWT（ToukaProxy） | 官方安全修正版 | 2.14.0 → 2.15.1 | `PyJWT~=2.15.1`；認證程式不變 |
| AnyIO | GHSA-82r6-8w77-94w6、GHSA-5p39-cfhj-2xmp、GHSA-3w57-8xmc-8v26 | 4.14.1 → 4.14.2 | 只升 patch |
| next、eslint-config-next（Chat；連同 `@next/*` 共 12 個套件） | GHSA-cjq9-62q9-8jv4（HIGH，Image Optimization SSRF）、GHSA-3w37-wq28-93x7、GHSA-4jqv-mc3x-m676、GHSA-f87g-xv8r-7p7x、GHSA-mcj8-r9mp-w47p（moderate）、GHSA-39w2-rjm5-chcv（low） | 16.3.6 → 16.3.8 | 精確釘版只換值；鎖檔以 `--before=2026-10-05` 重生，只變 12 個 Next 套件（皆 2026-09-30 上架）；`sharp` 上游範圍仍為 `^0.35.4`；Guard #23 轉綠（ADR-0243） |
| smol-toml（Chat knip dev／build graph） | GHSA-7w5x-hrqm-74c2（HIGH） | 1.6.1 → 1.7.1 | lock 只改該節點 |
| httpx2／httpcore2 | CVE-2026-84378／84379／84380／84382（httpx2）、CVE-2026-84381（httpcore2） | 2.5.0 → 2.12.0 | 配對升級 |
| js-yaml（Chat；openapi-core 為其相依 finding） | GHSA-2883-xcg3-v3hh／CVE-2026-84375 | 4.3.1 → 4.3.2 | 空 merge 來源 CPU 耗盡；OpenAPI 輸出不變 |
| aiohttp | PYSEC-2026-3545、3546、3547 | 3.14.1 → 3.14.3 | — |
| cryptography | PYSEC-2026-3552 | 49.0.0 → 50.0.0 | — |
| h2 | PYSEC-2026-3628 | 4.3.0 → 4.4.1 | — |
| setuptools | PYSEC-2026-3447 | 81.0.0 → 83.0.0 | Torch 同步 2.12.1+cpu → 2.13.0+cpu 以解除 `setuptools<82` 約束（仍為 CPU 版） |
| mkdocs-material／pip／pymdown-extensions（build/test 工具） | build-test scope 的 pip-audit findings | → 9.7.7／26.2／11.0.1 | 定向更新 |
| Chat／IDE Node lock（27 rows：Chat 18／IDE 9） | 當時 npm audit 的 HIGH findings | 定向更新 | 僅新增 `@humanfs/types` |
| fast-uri、nanoid、socket.io-parser（HIGH）；dompurify、mermaid（moderate） | 2026-08-09 npm audit | 相容範圍內 exact safe version | official tarball SHA-512；未用 `npm audit fix --force` |
| mcp、mcp-types（Proxy／Dispatch，Python） | CVE-2026-59951／GHSA-84m7-p3x7-pcfv | 2.0.0 → 2.2.0 | Touka 實際不受影響：唯一的 Python HTTP MCP 伺服器 ToukaDispatch 以 `stateless_http=True` 執行，創建引擎產物與生圖伺服器是 stdio，ToukaIDE 用 TypeScript SDK，Proxy 只當客戶端；仍升級當縱深防禦。2.2.0 起 SDK 不看 `follow_redirects`、會自己跟同源轉址，Proxy 改由 `build_mcp_http_client` 的 response hook 照樣擋下所有 3xx（禁令 22；ADR-0199 dep1 補記；`test_mcp_redirect_security.py`） |
| mcp（Proxy 客戶端） | GHSA-rwrf-2pqf-9j8j（無 CVE；moderate 6.5，無人值守 7.5） | 2.0.0 → 2.2.0（同上一列） | Proxy 實際受影響：2.0.0 的客戶端驗證工具結果時，會依伺服器的 outputSchema 同步、無逾時地抓 http／https／file 的外部 `$ref`（遠端伺服器能讓 Proxy 卡住、發出盲 GET 或開本機檔）。2.2.0 修好；另加呼叫當下的工具清單核對，被隔離、不在清單或同名多筆的工具一律不呼叫（ADR-0203 dep1 補記）。這筆公告不在 OSV／PyPI，弱點掃描（Guard #22）看不到，只靠回歸測試 `test_mcp_external_ref_security.py` 守 |
| aiohttp（經 discord.py 2.3.2）、yarl | GHSA-2g87-pp6c-29x5（high，無 CVE）、CVE-2026-111064／GHSA-5wr8-gcqf-gqf3（high）、CVE-2026-111063／GHSA-494j-54xm-rwp3（low） | aiohttp 3.14.3 → 3.14.5；yarl 1.24.2 → 1.25.1（aiohttp 新版所需） | Touka 實際不受影響：Touka 程式沒有直接使用 aiohttp，只有 Discord 機器人的 discord.py 當客戶端使用；兩個 high 都是 aiohttp 當伺服器時的資源耗盡與缺少請求逾時，Touka 不跑 aiohttp 伺服器。仍升級當縱深防禦，Guard #22 轉綠。兩版都已過 7 天冷卻期（3.14.5 於 2026-10-11 上架），只跑 `uv lock --upgrade-package aiohttp`，不開例外 |
| @modelcontextprotocol/sdk（Chat dev：只由 shadcn CLI 帶入） | CVE-2026-111767／GHSA-22jm-h49p-29qw（high）、CVE-2026-111768／GHSA-46jr-4wx8-v453（high）、CVE-2026-111769／GHSA-6prh-2h8m-c8cw、CVE-2026-111771／GHSA-rvq5-wwqv-78pq（moderate） | 1.31.0 → 1.32.1 | Touka 實際不受影響：SDK 只在 shadcn 元件產生器裡，產品不跑 SDK 的 HTTP 伺服器、tasks、bearer 驗證或 HTTP 客戶端。`overrides.shadcn` 精確釘版只換值，lock 只變這個節點；1.32.1 於 2026-10-05 上架，已滿 7 天 |
| @modelcontextprotocol/server（IDE runtime，打包進擴充套件） | CVE-2026-111770／GHSA-jm63-cq47-j5jp（high）、CVE-2026-111768／GHSA-46jr-4wx8-v453（high）、CVE-2026-111771／GHSA-rvq5-wwqv-78pq（moderate） | server 2.0.0 → 2.3.1；core 2.0.0 → 2.3.1（server 新版所需） | Touka 實際不受影響：IDE 以 `createMcpHandler()` 搭每次呼叫都新建 `McpServer` 的 factory，正是 jm63 公告列為不受影響的寫法；伺服器只綁 loopback，請求先過 Bearer 與 localhost Host／Origin 檢查才進 SDK，`express.json()` 用預設 100 KB 上限；不使用 SDK 的 `requireBearerAuth`／`verifyBearerToken`。46jr 新增的 `maxToolInputElements` 預設關閉，這次沒有開，升級不改行為。精確釘版只換值（契約測試同步換值）；`@modelcontextprotocol/node` 維持 2.0.0（peer `^2.0.0`）。2.3.1 於 2026-10-05 上架。IDE 測試用的 client 2.2.0 另有 moderate GHSA-6prh-2h8m-c8cw，不擋 Guard，本批未處理 |
| vite（Chat、IDE dev：vitest 與 `@vitejs/plugin-react` 帶入，內含 launch-editor） | CVE-2026-112942／GHSA-587h-7wmj-h5pm（high）、CVE-2026-112943／GHSA-698v-4r3x-3vcv、CVE-2026-112944／GHSA-r825-w8mm-5pxw（moderate） | Chat 8.0.16 → 8.1.6；IDE 8.2.0 → 8.2.4 | Touka 實際不受影響：三筆都在 vite dev server「在編輯器開啟檔案」功能（launch-editor，Windows），Touka 只用 vitest CLI 跑測試、Chat 開發走 Next.js，不啟動 vite dev server。各取該系列最小修正版（皆 2026-10-06 上架），lock 以 `npm install --package-lock-only --ignore-scripts --before=2026-10-14` 重生；用暫時 override 定版後移除，移除後重解 lock 不變，package.json 不新增 override。連帶的必要節點：Chat rolldown 1.0.3 → 1.1.5（含 15 個平台 binding）、`@oxc-project/types`、wasm binding 要求的 `@emnapi/*`、`@tybys/wasm-util` 與其內層 `@napi-rs/wasm-runtime`、vite 內的 picomatch；IDE rolldown 1.2.1 → 1.2.13（上游拿掉 wasm32 binding 及其 5 個相依、新增 android-arm-eabi binding）、postcss 8.5.25 → 8.5.30、`@oxc-project/types` |
| markdown-it（IDE dev：只由 `@vscode/vsce` 打包工具帶入） | CVE-2026-111758／GHSA-gfr2-rv44-r23c（high）、CVE-2026-111759／GHSA-r7fv-28h4-cvq7（high） | 14.3.0 → 14.3.3（**待 2026-10-23 更新**） | 尚未修：唯一修正版 14.3.3 於 2026-10-16 上架，2026-10-23 09:47（台北）才滿 7 天，屆時只更新 lock 裡這一個節點；在那之前 Guard #23 的 IDE full scope 仍是紅的。Touka 實際不受影響：markdown-it 只在打包擴充套件時處理我們自己的 README，兩筆都是特製 Markdown 造成的 CPU 耗盡 |

官方公告：[compression](https://github.com/advisories/GHSA-vc2v-76pw-4v95)、[source-map-js](https://github.com/advisories/GHSA-68fv-2mgg-jv7q)、[proxy-addr](https://github.com/advisories/GHSA-jqcg-44mw-7w3h)、[MCP SDK／client](https://github.com/advisories/GHSA-6qxp-vccf-f47h)、[sharp](https://github.com/advisories/GHSA-wq5f-xc86-pv6w)、[shell-quote](https://github.com/advisories/GHSA-pqg4-j6r4-53mv)、[multidict](https://github.com/aio-libs/multidict/security/advisories/GHSA-54p9-h82j-f925)、[smol-toml](https://github.com/advisories/GHSA-7w5x-hrqm-74c2)、[js-yaml](https://github.com/advisories/GHSA-2883-xcg3-v3hh)、[mcp CVE-2026-59951](https://osv.dev/vulnerability/CVE-2026-59951)、[mcp GHSA-rwrf-2pqf-9j8j](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-rwrf-2pqf-9j8j)、[aiohttp GHSA-2g87-pp6c-29x5](https://github.com/advisories/GHSA-2g87-pp6c-29x5)、[aiohttp GHSA-5wr8-gcqf-gqf3](https://github.com/advisories/GHSA-5wr8-gcqf-gqf3)、[aiohttp GHSA-494j-54xm-rwp3](https://github.com/advisories/GHSA-494j-54xm-rwp3)、[MCP TS SDK GHSA-22jm-h49p-29qw](https://github.com/advisories/GHSA-22jm-h49p-29qw)、[MCP TS SDK GHSA-46jr-4wx8-v453](https://github.com/advisories/GHSA-46jr-4wx8-v453)、[MCP TS SDK GHSA-6prh-2h8m-c8cw](https://github.com/advisories/GHSA-6prh-2h8m-c8cw)、[MCP TS SDK GHSA-rvq5-wwqv-78pq](https://github.com/advisories/GHSA-rvq5-wwqv-78pq)、[MCP TS server GHSA-jm63-cq47-j5jp](https://github.com/advisories/GHSA-jm63-cq47-j5jp)、[vite／launch-editor GHSA-587h-7wmj-h5pm](https://github.com/advisories/GHSA-587h-7wmj-h5pm)、[vite／launch-editor GHSA-698v-4r3x-3vcv](https://github.com/advisories/GHSA-698v-4r3x-3vcv)、[vite／launch-editor GHSA-r825-w8mm-5pxw](https://github.com/advisories/GHSA-r825-w8mm-5pxw)、[markdown-it GHSA-gfr2-rv44-r23c](https://github.com/advisories/GHSA-gfr2-rv44-r23c)、[markdown-it GHSA-r7fv-28h4-cvq7](https://github.com/advisories/GHSA-r7fv-28h4-cvq7)。

dep1 順帶修好一個既有錯誤：被隔離的 MCP 工具（標頭政策）原本被包成 `upstream_error` 並計入斷路器，同一個工具叫 3 次就讓整台伺服器熔斷 30 秒；現在回 `permission_denied`，不計入斷路器、也不寫健康頁的錯誤。模型給的標頭參數超過上限那一半當時沒修，2026-10-21 補上（見「2026-09～10 產品修復」）。

### 2026-08～09 產品修復

| ID | 元件 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **MR1** | `ModelManager`／dispatcher factory／chat schemas | 控制面自我路由＋端點／憑證分離覆寫 | 🔴 Critical | `provider=proxy` 排除上游探索；endpoint＋key 改由不可變記憶體路由原子綁定，空 key 不回退正式 key；公開 chat 只接受省略／`null` 的 `base_url`，非空與 `baseUrl` 等語意別名均在真 router 以 422 拒絕；JWT／LDH 不放寬（ADR-0208） |
| **SMK-1** | `tests/smoke/`／runner | standalone import／初始化寫入實體 app-data／config | 🟠 High | 24 支自動 smoke 全部在任何產品 import 前啟用逐程序 temp write-root sandbox；runner 以 manifest 明列分類、per-child root 與 fail-loud cleanup，manual live 永不自動執行（ADR-0214） |
| **SMK-2** | `smoke_chat.py`／live eval CLI | 自動測試真 provider、額度消耗與吞例外假綠 | 🟠 High | 自動 chat 改 deterministic mock；人工 live 入口在 provider-capable import 前要求逐次旗標，下層再驗 exact scope capability、request budget、token cap 及零 fallback／retry（ADR-0214） |
| **SMK-3** | Memory read／cleanup lifecycle | logical read 隱含 SQLite／Qdrant／lock／migration 寫入與 Windows handle 殘留 | 🟡 Medium | 自動 Memory smoke 全 sandbox＋deterministic embedding；Factual SQLite deterministic close／失敗 handle 可重試、Vector client 明確 shutdown，並修正兩個同步 resolve 的錯誤 `await`（ADR-0214） |
| **IDE-WV1** | `ToukaIDE/src/webview/index.html` | 未釘版遠端 CDN JavaScript／過寬 CSP | 🟠 High | 移除 jsDelivr `marked.min.js`；CSP 收斂為封裝內 script/style、`connect-src 'none'`，並移除 inline style；不新增依賴（ADR-0219） |
| **IDE-WV2** | `ToukaIDE/src/webview/main.js` | 不可信訊息／串流／plan／mention DOM XSS | 🔴 Critical | 移除 Markdown parser 與全部 HTML sink；動態值只用 `createElement()`＋`textContent`，清空用 `replaceChildren()`，role 固定映射，CSS 保留換行；Node `vm` hostile-payload Vitest 鎖定零 sink 不變式（ADR-0219） |
| **CVE-2026-75604** | `ToukaChat`／`next` | Windows hosted Next.js server RCE | 🔴 Critical | `next` 與 `eslint-config-next` 精確對齊 16.3.4（之後隨上游更新，現行 `next` 為 16.3.8）；由 `test_cve_2026_75604_nextjs_security.py` 及供應鏈回歸鎖定 |
| **GHSA-2xp9-vwfh-vxw4** | `next`／`sharp` | AVIF image optimization RCE | 🔴 Critical | Next 16.3.4 上游 optional range 已為 `sharp ^0.35.4`，移除專案 root `sharp` override |

自動 smoke 另移除 credential-shaped environment、設定模型下載 offline flags 並 mock 受測 provider 路徑；child 仍在一般 OS 網路命名空間，這不是 network egress jail。Debug Center scorecard 尚未消費 ADR-0214 backend capability／request hard budget，不得由自動化真 runner 呼叫。

### 2026-09～10 產品修復

| 範圍 | 問題 | 修復 | ADR |
|------|------|------|-----|
| 原生工具角色授權 | 三 provider 共用的 react_core→ToolExecutor native 分支只有 PaC 風險判斷、沒有 RoleGrants 執行核對，猜測工具名可繞過廣告面的角色過濾 | builtin（優先）與 Memory 實際執行前核對 verified identity 的 name grant；無角色／無 grant／deny 在 PaC 與 HITL 前拒絕，PaC／HITL 不可覆蓋角色拒絕；MCP／plugin 既有閘保留 | ADR-0228 |
| 任務產出物欄位 | 公開 `artifact_refs` 與 legacy `artifact_ref` 的路徑驗證與錯誤回顯 | 兩者皆沿 Task 路徑 validator；錯誤只回安全 field／reason，不回顯輸入；`manage_dups` 全 action 保守列 dangerous | ADR-0228 |
| Memory bootstrap `.env` 來源 | 未遵循顯式 `TOUKA_ENV_FILE` | 明示來源優先且不 fallback | ADR-0228 |
| 可信批准分類 | 模型 `command` 可取得終端／自帶批准例外 | 例外只來自 builtin 註冊時封存的 callable binding；RoleGrants→PaC→HITL 保留；批准等待期間 registry 變動不替換本次已批准 handler | ADR-0229 |
| 心跳設定與排程 | 缺檔與損壞未區分、損壞設定可能被覆寫 | 缺檔與損壞分開；原子保存與精確 jobs 補償；不可恢復時 degraded | ADR-0230 |
| 真人排程橋接 | 排程變更的擁有權與併發寫入 | fresh 人類身分＋old/new agent 擁有權、If-Match 交易 CAS、固定 origin 單送且不跟隨 redirect、validation 錯誤安全化；不重新啟用 Proxy 排程器 | ADR-0230 |
| 服務憑證 | Heartbeat／Proxy token 解析分歧 | 同一 provider 解析；fallback 僅供本機管理；shared setting 未就緒時不建立工作、不發 HTTP | ADR-0230 |
| 自動化隔離 | 測試 seed／守衛可能讀到私有設定 | pytest seed 與 Guard #32／#37 只用 tracked 出貨範本；smoke 在 Heartbeat import 前強制 owned env／data／config 與 logging 邊界；inventory 失敗即拒絕，不回退掃描本機設定 | ADR-0230 |
| 會話保存與 stream buffer | 保存／續傳的擁有權與 revision 競態 | 明示 UI 保存綁 owner／revision（strict revision CAS）；persistent buffer 每次 state／resume／stop 重驗所有權，刪除只退休精確舊 stream；client scope 退役舊 credential 內容與 Debug／artifact 快取；typed 錯誤不反射原輸入 | ADR-0231 |
| 登入錯誤與 client scope | 非成功回應分類遺失；晚到回應覆蓋新身分 | 未知 body／exception 固定安全分類；晚到登入／profile 不覆蓋新 token／user | ADR-0232 |
| 初始管理員交付 | 密碼檔寫入 OSError 後仍提交 users，形成無法取得初始密碼的帳戶 | OSError 在 users commit 前回固定 `PERSISTENCE_ERROR`，原 users 不變；既有 admin 不重設 | ADR-0232 |
| Gemini 顯式快取副本 | Touka 為對話（含無痕）在 Google 端另建 1 小時顯式快取 | 全停：一般與無痕、初次與迴圈中都不建顯式快取，每輪照常送 system_instruction／tools（Google 仍可能依其政策保留請求） | ADR-0241、ADR-0243 |
| 未登入聊天（HIGH） | `/messages`、`/chat/completions`（含 `/v1`）沒帶憑證時改用 Guest 情境照樣進 pipeline：指定靈魂名字就套上它的人設、注入家對話，並用伺服器金鑰呼叫模型；外部無卡與「系統認領後的本機無卡」都成立 | 兩個端點改掛授權閘道 `require_authenticated`（Depends），沒有已驗證身分一律 401 `AUTH_INVALID_TOKEN`、進不了 pipeline；刪除 `_build_guest_context`，免登入清單移除 4 筆。首裝前本機無卡請求照舊領卡（LDH：帶 X-Agent-ID 時照那個 ID 發，否則發 DebugAgent；程式未改）；SNS 程式內部路徑不經 HTTP 閘、行為不變。回歸測試 `tests/unit/test_unauthenticated_chat_security.py`。這一列只擋「未登入」；「已登入的非擁有者」原列為已知殘留，已由下一列修掉 | ADR-0245 |
| 非擁有者指定別人的靈魂（HIGH） | 已登入但不是擁有者的使用者（含 `/auth/register` 自助註冊、註冊後立即可用）或別的代理 token，把 `model` 或 `X-Touka-Soul-ID` 指向別人的靈魂，就會被套上人設、注入家對話與前情摘要，並用伺服器金鑰呼叫模型；群聊 `@別名`、子代理 `sub_model=<靈魂>` 同一根因（MonitorLayer 認出靈魂後只是不換身分，SoulLayer 注入時不看擁有權） | 授權閘道 `require_soul_access` 驗兩次：MonitorLayer 解析出靈魂目標後，與 SoulLayer 注入前（對真的要注入的靈魂 ID）。人類要擁有該靈魂或是 admin，代理只能是靈魂本身或 DebugAgent（admin 經 X-Agent-ID 代打保留 admin 權），否則 403 `AUTH_PERMISSION_DENIED`。再審補上的繞法：CustomModelLayer 跳過靈魂請求，同名自訂模型（標頭指自己的靈魂＋model 指別人同名的自訂模型）換不掉目標；自訂模型建立、改名拒絕靈魂名稱（409）；子代理組裝時去掉繼承的靈魂與 session 標頭，臨時身分指定靈魂一律拒絕；計分卡 `model` 是靈魂時路由先用真正的呼叫者驗。群聊每位成員同樣檢查（`can_drive_soul`），一位不行整輪不派發。SNS 程式內部路徑沒有身分、不經 HTTP 閘，照舊。殘留：首裝前領到的 DebugAgent 卡效期 7 天，系統認領後這段期間仍可指定任何靈魂。回歸測試 `tests/unit/test_soul_target_ownership_security.py`、`tests/unit/test_soul_target_bypass_security.py` | ADR-0245 |
| 自訂模型路徑穿越（HIGH） | `custom_soul` 路徑直接拼 `custom/<ID>`、不擋穿越：聊天 `model="../soul/<靈魂>"`（Windows 另有 `..\soul\<靈魂>`）會把靈魂目錄的設定當成自訂模型讀進來；經 `POST /ui/custom_models/<ID>`（Windows 用反斜線）：Owner 代理或 admin 建立新的 `..\..\<名稱>` 會在資料夾外建目錄、寫檔（沙盒測試修補前實際寫到沙盒外）；admin（或通過擁有權檢查的代理）「更新」`..\soul\<靈魂>` 會改寫靈魂的 `config.json` | `custom_soul` 路徑必須留在 `custom/` 底下（禁令 17，`validate_no_symlink_escape`／`is_relative_to`），否則回 None、讀寫都不做；建立與改名時新 ID 含 `/`、`\` 或 `..` 回 422 `VALIDATION_ERROR`。回歸測試 `tests/unit/test_soul_target_bypass_security.py` | ADR-0245 |
| session 歸屬（MEDIUM） | 靈魂對話的前情摘要只用 session_id 查、不看 session 屬於誰；沒帶 `ui_persistence` 的舊串流路徑也只用 session_id 把回覆寫回。擁有 Bob 的人送「model／標頭 Bob＋別人 session 的 session_id」，就讀到對方的前情摘要，回覆還寫進對方的 session、連 session 綁的 agent 都被改成 Bob；用一般模型帶別人的 session_id，回覆一樣會寫進去；SNS 的 session ID（`sns_{platform}_{channel}`）可預測 | SoulLayer 判定 session 綁定：session 綁的 agent（經 `identify_agent` 正規化）等於要注入的靈魂、且呼叫者能驅動它（`can_drive_soul`），才注入前情摘要；舊串流路徑的靈魂回覆也只在綁定成立時寫回；一般模型對話寫回前照路由的 session 存取規則（`can_access_session`，路由與 dispatcher 共用）。`can_access_session`（R-U2-SESSION 修正）：admin／Debugger 全放行、人類是 session owner 本人放行、**非人類代理只接受 session 綁的 agent 等於自己**（不走 `is_owner` 的無擁有者／30 天分支）、其餘人類走 `is_owner`。沒有權限就不建 buffer、不寫回、不觸發滑動視窗、不回 403。`connect_mcp_server`／`activate_skill` 寫 session 前要求綁定等於執行中的 agent 且 upsert 不改綁定（R-U2-SESSION attack_b）；`set_home`（R-U2-SESSION 再審第 2、3 次 attack_d／前綴標籤）檢查只加不減：復原「呼叫者能用 `req.agent_id`」（擋 `ToukaSoul<X>`／`ToukaGroup` 無人擁有的標籤）＋目標 session 存在（404）＋`identify_agent` 正規化後綁定等於 `req.agent_id`＋呼叫者能存取目標 session，否則 403。MonitorLayer 清中斷旗標移到授權之後。`ui_persistence` 路徑（已驗擁有者）不變；SNS 請求不帶 session_id，照舊。回歸測試 `tests/unit/test_session_binding_security.py`、`tests/unit/test_session_binding_attack_security.py` | ADR-0245 |
| 跨廠商備援 | 主要模型失敗時只比名稱就改用上次能用的模型，請求可能被送到使用者沒選的廠商 | 只有兩者路由家族相同且可判定才備援，否則回原錯誤 | ADR-0241 |
| 心跳 token 公開值（H5 後續） | `HEARTBEAT_TOKEN` 是歷來公開過的值（出貨範本的佔位字串、舊預設值或文件範例）時，Proxy 照樣採用，知道這個值的人就能叫醒代理。H5 只拿掉程式預設值，`.env` 裡已經填了公開值的安裝不受影響 | 歷來公開值連同鍵名列在 `config.py` 的單一對照表 `PUBLIC_SECRET_VALUES`，比對看「鍵名＋值」、去掉前後空白；三個解析點都把公開值當成沒設定（`usable_secret`）：`.env` 已存非公開值就沿用並同步到執行中的程序、不寫檔（容器只重啟、沒重建時不會每次換值）；否則產生新值寫回 `.env`；寫不回或 bootstrap 關閉時留空，只剩 JWT。`ToukaProxy/.env.example` 拿掉示範值，所有版控中的環境範本由測試檢查。更新後要執行一次 `docker compose up -d`，兩個容器才讀到新值。殘留：容器重建前 Heartbeat 自己的排程 API（只綁本機）仍接受原值；設定精靈寫入公開值的殘留已由下方「設定精靈寫入公開金鑰值」一列修掉（ADR-0254）。回歸測試 `tests/unit/test_public_secret_values_security.py`、`tests/unit/test_sec_batch_delta.py` | ADR-0251 |
| 登入簽章金鑰公開值（HIGH） | `TOUKA_AUTH_SECRET` 是歷來公開過的值時照樣用來簽發與驗證 JWT，知道這個值的人能自己簽出任何使用者（含管理員）的登入 token，或心跳服務 JWT | `AuthSecretManager.get_secret` 經同一份對照表：沿用 `.env` 已存的非公開值，否則產生新值寫回；寫不回時用臨時隨機值（重啟後 token 失效）。換掉時所有人要重新登入一次；這把金鑰只用在 JWT 簽發與驗證，沒有用它加密的資料 | ADR-0251 |
| 派工 token 公開值 | `DISPATCH_TOKEN` 是歷來公開過的值時照樣放進 dispatch 子程序的環境，知道這個值的人能通過 handshake | `DispatchSupervisor.build_child_env` 經同一份對照表，最後再確保子程序環境不帶公開值（包括從父環境繼承來的）；寫不回時子程序不帶 token，派工拒絕所有請求。設定精靈的「還是預設值」改用同一份對照表。殘留：不經 Proxy、單獨執行 ToukaDispatch 時不經這份對照表 | ADR-0251 |
| 設定精靈寫入公開金鑰值（LOW） | `POST /v1/admin/setup/apply` 只擋空白值，admin 把歷來公開過的值填進 `dispatch_token`／`auth_secret` 時照樣寫進 `.env`（ADR-0251 列的殘留：要到下次解析才換掉） | `SetupService.apply` 在寫任何東西之前，兩欄都經 `config.is_public_secret`（ADR-0251 的同一份對照表）；是公開值就回 422 `VALIDATION_ERROR`，`.env` 一個字都不寫（另一欄是合法值也不寫），錯誤訊息只寫欄位名、不寫值。非公開值照舊寫入。回歸測試 `tests/unit/test_public_secret_values_security.py`（設定精靈 apply 一段） | ADR-0254 |
| 自訂模型改名失敗仍搬擁有權（MEDIUM） | `POST /ui/custom_models/{id}/rename` 不看搬資料夾的結果：來源不見、目標已被占用、沙箱檢查不過或搬移出錯時照樣回 200，並把 `users.json` 的擁有權從舊 ID 搬到沒有資料夾的新 ID，使用者失去原模型；Windows 上只差大小寫的改名也走這條路 | `CustomModelService.rename_model` 搬失敗就丟例外、`config.json` 不改，路由回 404 `MODEL_NOT_FOUND`（來源不見）、409 `MODEL_ALREADY_EXISTS`（目標已被占用）、500 `SYSTEM_ERROR`（其他）；擁有權只在搬成功後才搬。只差大小寫時 `custom_io.move_custom_dir` 用 `samefile` 判斷後直接改名，擁有權照舊跟著改。回歸測試 `tests/unit/test_custom_model_rename_security.py` | ADR-0254 |
| Manual 下演化建置不問人（HIGH） | `evolution_implement_and_verify` 在註冊時被綁成「自帶審批」，Manual 的 CONFIRM 不跳卡；工具自己只在最後部署才問人，前面的寫檔與建 venv 不問人就跑，無人在場（心跳）也跑。後面的 `pip install`、本機驗證（import 代理人寫的 `server.py`）與部署同樣不問人，只是當時 venv 的 Python 路徑錯（相對路徑，venv 實際在 `.venv` 底下），這幾步一律 FileNotFoundError、從沒真的跑起來；H3 修好路徑的同時，缺口 1 讓它改走一般審批。Auto 下它屬 F5（只有人能核准），所以 Manual 反而比 Auto 鬆 | 拿掉這個綁定（`tools/builtin/__init__.py` 的 `BUILTIN_EXECUTION_BINDINGS`，Guard #45 基準同步），工具回到一般分類：Manual 有人在場先跳一般審批卡、核准才建置，拒絕就什麼都不跑；無人在場由 executor 既有路徑拒絕。部署卡（`request_mcp_creation`）與無人在場時部署 fail-closed 照舊；Auto 照舊兩張卡、YOLO 不變；`request_skill_install`、`request_mcp_creation` 仍是自帶審批。殘留：第一張是一般審批卡，很長的程式碼參數可能只預覽前段，部署卡仍顯示全文。回歸測試 `tests/unit/test_evolution_manual_approval_security.py`，翻轉 `test_evolution_deploy_approval.py`、`test_tool_execution_classification.py` | ADR-0254 |
| MCP stdio 伺服器繼承全部金鑰（HIGH） | 已安裝的 MCP stdio 伺服器啟動時拿到 `os.environ.copy()`（`settings.get_full_env()`），含 `TOUKA_AUTH_SECRET`、`DISPATCH_TOKEN`、`HEARTBEAT_TOKEN` 與全部廠商金鑰，任何第三方 MCP 套件都能順手帶走；創建引擎試跑走 `get_clean_env` 黑名單，漏得掉 `*_PASS`、`DATABASE_URL` 一類 | `LaunchEnvironmentPolicy`（`protocols/mcp/environment.py`）改用白名單底，模組常數 `LAUNCH_ENV_ALLOWLIST`（語系、時區、暫存目錄、Windows 系統目錄、XDG 目錄、uv／npm 的非機密設定；`PATH`、`HOME`、`SYSTEMROOT` 一類由 MCP SDK 的 `get_default_environment()` 帶；Windows 名稱不分大小寫）。正式＝白名單內的父環境＋條目自己的 `env`（解析哨兵，名稱含 TOKEN／KEY 的照給）＋`TOOL_BASE_URL`；創建引擎試跑（`clean`）＝同一個白名單＋spec 宣告的 `env`（不解析哨兵，見 SP7）。**MCP 伺服器不再拿到 `PYTHONPATH`、`HF_HOME`、`HTTP_PROXY`／`HTTPS_PROXY`、Windows 的 `HOME`、公司內部憑證變數（`SSL_CERT_FILE`、`REQUESTS_CA_BUNDLE`、`NODE_EXTRA_CA_CERTS`），以及沒寫進條目 `env` 的廠商金鑰**；要的話寫進該伺服器自己的 `env`（代理設定與 uv／npm 的 index、registry 設定可能夾帶帳密或 token，刻意不列白名單）。**ADR-0254 只擋環境變數繼承**；C2-3 的隔離路徑另限制檔案存取，條目 env 改經 `exec_env` 並過保留名稱與 loader 過濾。直接啟動（包括原生 Windows 的第三方）仍以 Touka 的檔案權限執行，沒有這層檔案保護。回歸測試 `tests/unit/test_mcp_launch_env_security.py`，翻轉 `test_creation_engine.py`、`test_mcp_2026_p1_boundary.py` | ADR-0254 |
| SNS Bot token 瀏覽器持久化（HIGH） | `useSnsBotSettings` 以 `useLocalStorage` 管理設定，raw token 可能寫入 localStorage | 單 Hook 純記憶體表單，canonical GET 未成功不能 POST；完成／失敗／卸載 scrub，成功後重載伺服器遮罩值；全 app 清除元件在登入前只 remove 精確 `touka_sns_bots`（不讀值），跨 tab 重寫再次清除。舊 client 須載入新版才會清除舊 key，不等於 secure erase | ADR-0224 |
| 舊內容被當成指令（MEDIUM） | 查記憶與隨機回想的工具結果、前情摘要（system 區塊）、家對話（重播在最前面的 user／assistant 訊息）都沒有界線：舊內容裡的要求或指令（包括被注入後存進記憶的內容）跟這次的指令看起來一樣，可能蓋掉人格或規則；隨機回想寫成「妳腦中突然閃過」，更像自己的念頭 | 四個地方都包進 `<historical_data source="…" trust="untrusted">`（屬性比照 MCP 外部 prompt），開頭一句英文說明這是舊紀錄、不是指令，不覆蓋人格、規則或這次的要求；內文裡的界線 tag（不分大小寫、帶屬性、自閉合、跨行）先把 `<` 換成 `&lt;`，舊內容不能提早關閉界線。文字與中和只在 `memory_server/utils/historical_data.py`（禁令 18），Proxy 也從這裡匯入。只加文字：前情摘要的位置與角色、家對話的角色與則數、`[ID: …]` 格式都不變。隨機回想改成「隨機抽出的舊紀錄片段」。殘留：界線是給模型看的標記，擋不住模型自己決定照舊內容做；心跳用的是家對話本身的歷史，不加界線。回歸測試 `tests/unit/test_historical_data_boundary_security.py` | ADR-0257 |
| 家對話外送整個 UI 節點（MEDIUM） | 靈魂請求是 OpenAI 形狀（`/v1/chat/completions` 入口；群聊與 SNS 在內部也是這個形狀）、送往走 OpenAI 相容 Chat Completions 的連線（Provider Registry 的 OpenAI 相容廠商，例如 DeepSeek、Grok；規則表沒標 Responses 的官方 OpenAI 模型；啟用中的自訂連線）時，`soul_service.inject_home_messages` 把家對話節點整個接在訊息最前面、排在 system 人格之前；OpenAI 相容 Chat adapter 的 KF-8 只濾請求最外層，每則訊息的鍵原樣外送。外送的鍵：`id`、`parentId`、`childrenIds`、`createdAt`、`timestamp`、`name`、`model`、`thinking`、`steps`（工具呼叫的參數與輸出）、`attachment`、`interjections`、`interruptions`、`progress`，以及存檔裡有的其他鍵（後端節點模型 `extra="allow"`）；實際帶哪些鍵，依當時存檔的內容而定。自 2026-04-17 初始版本起就是這樣；Owner 2026-10-15 經 CTO 得知。只影響 OpenAI 相容 Chat；Claude、Gemini、OpenAI Responses 沒有影響：各自的轉換器逐則重建訊息、只取規格欄位，system 抽到獨立欄位，Claude 原生入口的家對話本來就只取 role／content；Anthropic 形狀入口（`/v1/messages`）轉送 OpenAI 相容廠商與心跳也沒有影響 | 家對話注入一律只取 `role`／`content`（插話標註照舊在 content 裡），OpenAI 形狀插在開頭連續的 system 之後。第二道：送出邊界逐則白名單（`handlers/adapters/base.only_keys`，白名單是模組常數，禁令 18），OpenAI 相容 Chat 只留 `role`、`content`、`tool_calls`、`tool_call_id`，Claude 原生入口只留 `role`、`content`；Gemini、Responses、Claude 的 OpenAI 入口本來就逐則重建，不動。`name` 也整個濾掉，沒有路徑需要：Touka 自己的呼叫端組請求時都只用 role／content，心跳與 SNS 的 `name` 只寫進對話樹。行為改變：外部 OpenAI 相容呼叫者在訊息裡帶的 `name` 不再轉送給廠商。迴圈中途寫回的內容照原形送（raw 透傳不變式），不在白名單範圍（adapter 自己寫回的 tool 訊息照原本帶工具名）。殘留：已經送出去的內容收不回來。回歸測試 `tests/unit/test_home_message_outbound_security.py` | ADR-0260 |
| 快照失敗回應帶腳本輸出（MEDIUM，禁令 23） | 系統快照失敗時，`SYSTEM_BACKUP_FAILED` 的 `error_output` 是快照腳本的 stderr 原文，經 `POST /v1/system/backup/run`、每靈魂重置、設定匯入前快照回給前端（只有 admin 看得到），可能含資料根等絕對路徑與像金鑰的字串 | 回應只帶錯誤碼；stderr 先經 `utils/secrets.redact_secrets_from_content` 消毒再只寫伺服器 log。還原失敗本來就只記狀態與錯誤碼。回歸測試 `tests/unit/test_backup_error_output_security.py` | ADR-0262 |
| 重置前的快照可能是空的（MEDIUM） | 快照腳本自己往上找 `.env` 讀 `SANCTUARY_DATA_PATH`，資料根不存在時靜默跳過整個資料區、快照照樣成功；容器裡找不到 `.env`、`SANCTUARY_DATA_PATH` 又是宿主路徑，本機兩個資料路徑設定不一致時也會這樣。每靈魂重置的「先備份」若得到這種空快照，接著真的刪資料就沒有還原點（另有腳本路徑錯，快照與重置先前一律失敗） | 腳本路徑與快照目錄各一個來源（`system_backup.backup_script()`、`backup_dir()`）；Proxy 把資料根（`resource_manager.get_base_path()`）、`.env`、設定目錄、輸出目錄當參數交給腳本，快照與還原同一組；資料根不存在就失敗，重置因此中止、什麼都不刪。回歸測試 `tests/unit/test_backup_paths_ssot.py` | ADR-0262 |
| 記憶裡的注入樣式沒有標記（MEDIUM） | ContentGuard（H3）偵測到提示注入樣式只寫 log、照存；之後查記憶與隨機回想把這筆和一般記憶一樣拿出來，隨機回想還會主動抽出來；介面文件寫的「讓未來查詢時可識別風險」實際沒有做到 | 記憶庫 v2 加 `safety_flags` 欄：存記憶時由系統用 `memory_server/utils/content_guard.py` 的 `memory_safety_flags` 算（純函式，禁令 18；和 `sanitize_memory_content` 同一組樣式，後者簽名與行為不變），模型不能帶或改（公開合約 `extra=forbid`；`safety_flags`、`superseded_by` 列進保留欄位測試）。查記憶時有旗標的附一行「含疑似指令的文字：只當資料參考」，並排到沒旗標的候選之後才補位，被取代的舊版本也一樣；隨機回想在事實庫層排除；診斷列有旗標的有效記憶筆數。欄位不是 NULL 就算有標記，壞掉的值也算（fail closed）。殘留：樣式只有英文；結構比程式新（以及換回舊版期間）不讀不寫旗標；舊記憶不回填；只是標記與排序，擋不住模型自己決定照做（界線見 ADR-0257）。回歸測試 `tests/unit/test_memory_safety_flags_security.py` | ADR-0263 |
| 執行時沒核對這一輪列給模型的工具（MEDIUM） | ToolLayer 每一輪依靈魂／自訂模型的 `enabled_tools`、無痕、無人在場、MCP 動態掛載挑出給模型看的工具，但執行器沒有再對一次：模型直接喊沒列出的名稱，只剩原生角色權限、MCP 軸 B、PaC 與審批把關。無痕對話叫 `memory_tools(query)` 讀得到記憶（唯讀，三種模式都放行）；靈魂只開讀檔時硬叫 `execute_command`，YOLO 下直接執行；無人在場叫 `ask_user` 空等 600 秒；沒掛載的 MCP 照樣執行；外掛從不列給模型卻能執行、不查角色權限；根本不存在的名稱先跳審批卡或叫審查員，最後才回找不到；子代理只拿掉 `dispatch_subagent`、沒拿掉 `dispatch_subagent_group` | ToolLayer 把實際注入、去重後的伺服器端工具記成不可變的 `AdvertisedTools`，寫進 `PipelineContext.advertised_tools`（`InstanceOf`，只有 ToolLayer 與子代理組裝寫入，模型的參數碰不到）。模型迴圈建立的執行器開著核對（`enforce_advertised=True`；`run_react_loop` 建好後再檢查一次，沒開就 `SYSTEM_ERROR`），在原生角色權限之後、軸 B／PaC／審查員／審批卡之前：完全找不到 → `resource_not_found`；清單不可用 → `internal_error`（`reason=advertised_tools_unavailable`）；沒列出 → `permission_denied`（`reason=not_advertised`），三種都不叫審查員、不跳審批卡、不執行，YOLO 也一樣。舊名工具要它的後繼工具全部列出才放行（`LEGACY_ADVERTISED_SUCCESSORS`）；MCP 核對（伺服器, 裸工具名）；子代理用自己的清單並拿掉兩個派工工具。人按的 `/debug/execute` 明寫不核對。結構測試釘住執行器只有這兩個建立點、清單欄位只有白名單檔案讀寫。殘留：使用者各自授權的 MCP、還沒連線的共享 MCP、SoulStudio 存短名的 MCP 工具與外掛本來就不列給模型，改後一律被擋（另開 T1、T2）；被擋的記憶工具仍算「用過」，記憶承諾提醒不跳（另開 T3）；動態取得類別或屬性的寫法（算出來的字串配 `getattr`／`importlib`、`type(x)(...)`）結構測試抓不到，執行期檢查只保護 `run_react_loop`。回歸測試 `tests/unit/test_advertised_tool_list_security.py`，`tests/integration/test_tool_loop_budget.py` 2 個測試的授權補上 `vault_tools` 的 7 個後繼檔案動詞（斷言不變） | ADR-0269 |
| 重置後備份仍含靈魂的記憶（MEDIUM） | 每靈魂資料重置只清線上資料；記憶庫升級備份與設定頁清單上的每一份系統快照（含重置前剛做的那份）仍含這個靈魂的事實與語意索引，還原任何一份就拿回來 | **預設不勾選時，重置仍保留備份裡的記憶**（和以前一樣，畫面照實寫）。勾選「一併清除備份裡的記憶」並完成第二段確認後（只給非代打的人類 admin 重輸密碼，路由與協調器各確認一次），重置完全成功時才逐份清除：記憶體副本刪列再 `VACUUM INTO`、獨立驗證（非目標列多重集合、zip 依常數版面重算、不看分類器與對照表）後原子取代；拿掉目標的 Qdrant collection 與 `meta.json` 項目；線上庫 VACUUM 並核對磁碟上沒有目標的 collection 資料夾；事實庫每條連線開 `secure_delete`。任何一步失敗原檔不動、逐檔回報；每一種結局都有一行稽核（不帶內容與路徑、檔名 repr）；清除進行中擋還原與重置，還原中擋任何重置。保證的是**記憶庫檔與備份檔裡讀不到**，不是硬碟層的鑑識抹除。殘留：快照裡的對話、活動時間軸、統計、任務、產出、長期記憶、舊版 `facts.jsonl`、舊前綴 ID 寫的記憶、不在清單上的備份、已下載的備份都不清；本功能上線前命令列做的舊快照可能留有「空閒頁 0、又找不到 ID 位元組」的溢出頁殘片；快照目錄本身是連結時清單看不到、清除記失敗。回歸測試 `tests/unit/test_memory_scrub_*.py`、`tests/unit/test_snapshot_listing.py`、`tests/unit/test_resource_manager_archive.py` | ADR-0270 |
| 向量 collection 撞名（MEDIUM） | collection 名稱把非英數字元換成底線，`a-b` 與 `a_b` 共用一個 collection；不分大小寫的檔案系統上只差大小寫的 ID 也落到同一個資料夾。一般重置會刪掉整個共用 collection（連別人的向量一起），刪除靈魂前的檢查會被別人的向量擋住 | 撞名判定 `memory_collection_guard`（`names_fold_equal`：相等、casefold、upper 任一相同；已知 ID＝靈魂、自訂模型、registry、事實庫四個來源，registry 讀不到就拒絕）。撞名時一般重置只刪目標自己的點、殘留只數不屬於別人的點，判定出錯絕不退回整個刪除；刪除前檢查同一套判斷；勾選清除時直接拒絕。建立靈魂、建立與改名自訂模型時拒絕撞名的 ID（409 `AGENT_ID_COLLECTION_COLLISION`，改名時舊 ID 的記憶照算），讀不到事實庫回 503 `AGENT_ID_CHECK_FAILED`。不改命名。回歸測試 `tests/unit/test_memory_collection_collision_security.py` | ADR-0270 |
| 代理人指令與 Touka 同權限（HIGH） | 終端（`execute_command`／`bash`）、互審驗證指令、演化的建 venv／pip／本機驗證都和 Touka 用同一個帳號、同一個容器執行：讀得到 `.env`（`/app/host_root`）、PID 1 的環境、設定、原始碼與整個 data（含其他代理人與靈魂），也改得到 Touka 的程式；ADR-0254 只拿掉了子程序環境變數裡的金鑰 | spawn 前才經 runner 改由 Landlock 啟動器執行（`sandbox_kernel/launcher.py`，只用標準函式庫、`python -I -S`；容器內只從映像裡唯讀的 `TOUKA_SANDBOX_KERNEL_DIR` 載入，和原始碼雜湊不同就不用）。讀寫只給自己的資料夾、可授權的專案根與隔離暫存區；data（自己的資料夾等可給根目錄除外）、設定、原始碼、`backups`、`.env`、`host_root`、kernel 與暫存區根永不授權，Touka 目錄底下只有使用者自建的頂層資料夾能當專案（往上查到鏡像根、往下掃 3 層，不跟連結、名稱不分大小寫）；專案根只來自 session，不讀模型參數；所有 spawn `close_fds=True`，spec 經 pipe 傳、spawn 後關掉。範圍補充：MCP 伺服器與創建試跑已由 C2-3 接線；IDE 終端、Dispatch `auto_invoke` 仍不隔離；pin 模式在 9p 上的限制見 ADR；Landlock 不擋 chmod／chown／utime／setxattr，隔離內和 Touka 同 uid，走得到路徑就改得到任何同 uid 檔案（含設定與別的代理人資料）的權限位元（DoS），C2-2 的執行器仍是同一個 uid，代理人工作區改由自癒補回（見下）；`/proc/*/cmdline` 跨代理人讀得到（不要把 token 放在 MCP 的 args）；`config/skills` 所有代理人都讀得到。回歸測試 `test_isolation_profiles_security.py`、`test_isolation_project_scan_security.py`、`test_project_root_single_source_security.py`、`test_sandbox_fd_hygiene_security.py`、`test_evolution_isolation_security.py`，Linux 真核心 `tests/integration/test_isolation_linux.py` | ADR-0272 |
| 代理人指令連得到 Touka 內部與 Docker 遠端控制（HIGH） | 代理人指令連得到 Touka 自己的 8000／8082／8083／3001 與 Docker 遠端控制埠 2375／2376（有開的話），也能對 Touka 行程送信號 | 啟動器最後套網路層，擋 TCP 連到這些埠、設定改過的對外埠與 SearXNG 的埠（ABI≥4 才擋得住）；ABI≥6 另外隔開信號與抽象 unix socket；讀不到 `/proc/1` 與父行程的 environ。殘留：看的是埠號，代理人自己起在這些埠的服務自己也連不上；不擋 UDP，主機其他埠與區網照常（決定 3）。回歸測試 `test_isolation_profiles_security.py`（埠集合），真核心連線與信號在 `tests/integration/test_isolation_linux.py` | ADR-0272 |
| 沒有隔離時仍自動執行（HIGH） | 隔離不可用時（原生 Windows、舊映像、核心太舊、暫存區沒設）代理人指令在 Auto 經審查員、在 YOLO 直接以 Touka 的權限執行 | 要隔離的動作在層級 `none`（缺值、看不懂也算）時，Auto、YOLO 都只有人能核准、無人在場拒絕（程式常數，`AuthPoliciesConfig.evaluate` 最後判斷；DENY 不放寬、底線理由保留）；拿不到 runner 就不執行；沒接上 runner 的工具（`create_mcp`、`scaffold_mcp` 現已接線，見「創建試跑隔離」）與判定出錯一律當 `none`。回歸測試 `test_isolation_policy_security.py`、`test_isolation_unavailable_fail_closed_security.py` | ADR-0272 |
| YOLO 無人在場執行高風險 MCP（MEDIUM） | YOLO 下心跳、SNS、子代理、派工等無人在場的執行，dangerous 的 MCP 工具直接執行 | `human_present` 不是 `True` 時改 HUMAN_ONLY（＝拒絕）；`human_present` 由 executor 依 ctx 判斷，模型參數改不動；有人在場照舊；`tool_risk` 標成 safe／read_only 的工具不受影響。回歸測試 `test_yolo_headless_mcp_security.py` | ADR-0272 |
| 審批卡與審查員的隔離標示不實（HIGH，對抗式第 2 輪） | `create_mcp`、`scaffold_mcp` 當時不經 runner（現已接線，見「創建試跑隔離」），卡片卻照本機 Landlock 寫「隔離降級」、核准送回 `landlock`，實際以 Touka 的權限執行；不經 runner 的工具（含所有 MCP 工具）交給審查員的層級是 `landlock`；和隔離動作同名的 MCP 工具會進隔離判定與同意帳本；同一個呼叫重新記錄時保留舊同意 | 卡片依工具判斷（`ISOLATION_UNWIRED_TOOLS`）一律 `fallback: direct`、features 空，不看帳本與原因，帳本作廢或過期時核准不執行；審查員的層級由 `reviewer_level` 給，只有經 runner 的內建工具給政策層級，其餘 `none`（決策表不變）；隔離判定與帳本只看內建工具；重新記錄清掉同意。`kernel_outdated` 或讀不到 kernel 時，`/opt` 的舊啟動器會拒絕新的 spec 鍵，卡片也一律寫「未隔離」（`fallback: direct`），不再寫核准後必定執行不了的「隔離降級」。回歸測試 `test_isolation_unavailable_fail_closed_security.py`、`test_isolation_policy_security.py`、`test_unisolated_consent_security.py`、`test_isolation_status.py` | ADR-0272 |
| 專案根經連結改寫、資料根認不出（HIGH，對抗式第 2 輪） | 可給根目錄是整個 `personal_drive` 與共用的 `docgen_output`，專案根先 resolve 再授權：代理人把自己資料夾裡的專案換成指向別的代理人資料夾的符號連結（或 junction），下一條指令就拿到對方資料夾的讀寫；全新的資料根還沒有 `legacy/` 時認不出，從工作區的另一個路徑走到真正的 data 會被授權 | G 只給呼叫者自己的 `personal_drive/<id>`（依已驗證身分，`docgen_output` 不給）；專案根任何一段是符號連結或 junction 就 `protected_location`；資料根改成 `soul`、`personal_drive`、`ui_sessions.db`（啟動一定會建）任一個就算。殘留：會多擋、部署資料夾改名後認不出，見 ADR。回歸測試 `test_isolation_profiles_security.py`、`test_isolation_project_scan_security.py`，真核心 `test_isolation_linux.py` | ADR-0272 |
| 封鎖埠可被搶占或繞過（HIGH，對抗式第 2 輪） | 網路規則只處理 connect：代理人可以 bind Touka 的埠（在 Dispatch 或 proxy 重啟的空檔搶 8083、8000，收走 mission token）；TCP Fast Open 與 MPTCP 的連線可能不經 Landlock 的 TCP 規則 | 同一份規則集同時擋 bind 與 connect，自測多一項 bind 要 EACCES；三份 compose 的 proxy 設 `net.ipv4.tcp_fastopen: 0`；自測在 ABI≥4 時只讀兩個 sysctl，Fast Open 開著（或讀不到）、MPTCP 開著時層級是 `none`（`net_tcp_fastopen`／`net_mptcp`，fail-closed：Auto／YOLO 只有人能核准、無人在場拒絕）；自測照樣跑完檔案與一般 TCP 的檢查，本機 Landlock 保留檔案隔離（比照 ABI 3：features 不含 `tcp`，核准後是「隔離降級」，封鎖埠的規則照樣套用），不會因此變成「未隔離」。Fast Open 與 MPTCP 沒有實測繞過，直接當作能繞過來防。殘留：有 MPTCP 的核心要在 compose 覆寫加 `net.mptcp.enabled: 0`（原生 Linux 改主機的 sysctl）；proxy 改成 `network_mode: host` 或核心早於 4.12 時帶 `sysctls` 的 compose 起不來；AF_SMC 沒處理（一般 Linux 主機建議用 modprobe 設定封鎖 `smc` 模組，見 README_QUICKSTART）；封鎖埠清單不含 Touka 當客戶端去連的 localhost 上游埠，C2-2 已修（見下）。回歸測試 `test_landlock_kernel_pure.py`、`test_sandbox_state_compose.py`、`test_isolation_status.py`，真核心 `test_isolation_linux.py` | ADR-0272 |
| 隔離判定與自測的韌性（MEDIUM，對抗式第 2 輪） | 背景重探遇到一次例外就永久停止（之後一直 `none`）；上次被打斷的自測資料夾讓下一次開機必定撞名；取消時自測的啟動器沒被殺；指令印出任何格式合法的代碼都能觸發自測；spawn 端在 PDEATHSIG 生效前結束時指令變成孤兒；子行程繼承 proxy 的 stdin（原生 Linux 是 TTY 時可 TIOCSTI 注入）；shell 的 `exec 12>檔` 蓋掉 pin | loop 每輪包 try、出錯照 30 秒繼續，讀不到 kernel 判 `isolation_check_failed`；自測資料夾與 fixture 改隨機名稱、自測前清掉殘留（不跟連結）；取消時先殺掉並等自測的啟動器；只認 `FAILURE_CODES`、不分原因 60 秒最多重探一次；spec 帶 `parent_pid`，啟動器在 PDEATHSIG 之後比對，不符就 `process_setup`；所有 spawn 的 stdin 一律 DEVNULL；pin 搬到 fd 100 以上。回歸測試 `test_isolation_selfcheck_security.py`、`test_isolation_status.py`、`test_sandbox_runner_contract.py`，真核心 `test_isolation_linux.py` | ADR-0272 |
| 代理人指令在 Touka 主容器內執行（HIGH，C2-2） | C2-1 的 Landlock 在 proxy 容器內：指令和 Touka 同容器、同帳號，看得到 proxy 的行程，走得到 `.env`、data 根、soul、config 的路徑（讀寫被 Landlock 擋，但改得到權限位元） | 指令移到另一個容器 `touka-executor`（同映像、唯讀 rootfs、`cap_drop: ALL`、`no-new-privileges`、不帶 `env_file`、沒有對外埠，只掛 `personal_drive`、`mcp_servers`、唯讀 skills、ipc 與自己的 state，只接 `touka-exec-net`）；proxy 只跑本機 relay，指令在 START 裡經 unix socket 交給執行器。控制通道權杖每次啟動換新、只從 ipc 檔讀（argv、環境變數、log 都沒有）；accept 先查 `SO_PEERCRED`（同 pid namespace 的連線直接關）、HELLO 2 秒、連線與未驗證上限；START 全部 fail-closed（讀寫只有自己的目標與 state、env 精確名稱允許清單、封鎖埠包含關係、`parent_pid` 由伺服器改寫）；EXIT 前斷線就殺掉整個行程群組，subreaper 收孤兒。判 `executor` 要 READY 的版本、kernel、伺服器程式與封鎖埠雜湊、roots、自測、ABI 全部相符，不符就 `none`（要人核准、不退回 Landlock）；`GET /isolation` 只露層級與原因代碼。殘留：綁專案的對話與 `custom/<id>` 的代理人仍在 proxy 本機 Landlock（要人核准）；ABI 3～5 時代理人能讓執行器伺服器停掉；同 uid 的 ipc 可被改權限（fail-closed）。回歸測試 `test_executor_server.py`、`test_executor_client.py`、`test_executor_runner_security.py`、`test_executor_compose_security.py`、`test_executor_server_imports.py`、`test_isolation_status_endpoint_security.py`，真核心 `test_isolation_linux.py`，端到端 `tests/docker/isolation.compose.yml` | ADR-0272 |
| 封鎖埠不含本機上游埠、超過上限截斷（HIGH，C2-2） | B 只有固定埠與設定改過的埠：Touka 去連的本機模型閘道（LiteLLM、Ollama 等）隔離內連得到也佔得到（花用 API 額度、外送資料、攔下 Touka 送出的 Bearer）；B 超過 64 個時排序後截斷，丟掉高號的埠 | B 由 monitor 算：核心 B 加上 Settings 的上游 URL、`connections.json` 全部條目、路由快照（`ModelManager.route_endpoints()`）、MCP registry 全部條目裡非公網主機的埠，行程存活期間只增不減；新增連線（存檔、快照匯入、啟動補檔、系統還原）立刻進 B；上限 1,024，超過不截斷、判 `none`／`block_ports_overflow`；`connections.json` 還沒讀成功或讀不到（任何一條 `url` 空白也算）判 `none`／`block_ports_incomplete`（兩者核准後仍保留檔案隔離，只擋核心 B 或已知的部分）；讀檔只取 `url`，log、狀態與 `GET /isolation` 都不帶金鑰（只記例外型別名稱）。殘留：80／443／53 不擋；非公網只看字串、不查 DNS（IP、`localhost`、單段名稱與 `.localhost`／`.internal`／`.local`／`.home.arpa` 結尾才算），`nas.lan` 這類區網網域名的上游埠不擋；拿掉的連線要重新啟動才解除；手動改檔最多慢約 10 秒。回歸測試 `test_isolation_monitor_block_ports_security.py`、`test_isolation_block_ports_security.py`、`test_model_route_isolation.py`、`test_settings_snapshot.py` | ADR-0272 |
| 啟動器信任根與失敗代碼可被指令影響（MEDIUM，C2-2） | 啟動器用 `sys.executable`（appuser 擁有的 venv），核准過一次的未隔離指令可以換掉它；失敗代碼從合併輸出解析，指令自己印出合法代碼就能觸發重探；網路設定只在自測時讀；開機自測用黑名單過濾的環境 | `trusted_interpreter()`：`sys._base_executable` 的實體路徑，容器裡整條擁有者鏈要 root、群組與其他人不可寫，不符判 `interpreter_untrusted`；失敗代碼只認獨立的 `--status-fd`（一拿到就 CLOEXEC）；`net_strict` 時每次啟動讀 Fast Open／MPTCP 設定、不安全就拒絕；開機自測用空環境，隔離邊界禁用 `child_python_env`／`get_full_env`／`get_clean_env`。殘留：原生 Linux 沒有信任根。回歸測試 `test_landlock_kernel_pure.py`、`test_relay.py`、`test_isolation_selfcheck_security.py`、`test_sandbox_runner_contract.py`、`test_isolation_boundary_no_full_env.py`，真核心 `test_isolation_linux.py` | ADR-0272 |
| 代理人鎖住工作區的權限（MEDIUM，可用性，C2-2） | Landlock 不擋 chmod：同 uid 的指令能把代理人資料夾、`mcp_servers` 或暫存區改成 000，Touka 的檔案動詞就讀不到 | ResourceManager 碰檔案的方法遇到 Linux 的 EACCES（帶 filename）時，只在白名單（代理人工作區、`managed/<skill>`、`custom/<id>/drive`、暫存區與幾個掛載根）內補回自己擁有的 inode 的擁有者位元（不跟連結、不修多連結檔、拿掉 setid），再依方法決定是否重試一次；受保護路徑與 Touka 自己的狀態一律不修，原例外物件原樣往上丟；有頻率限制（用完標 `permission_relock`）；定期修掛載根；執行器在啟動時與每 5 秒補 ipc 與 state 的擁有者位元，並拿掉這些私有 inode 的群組與其他人位元（`repair_private`；只修自己擁有、不是連結的），代理人把它們改成 777 或 000 不會讓執行器重啟後不服務。殘留：Touka 自己的狀態被鎖不會自動修回（要 `touka-init`），反覆重鎖會間歇失敗，見 ADR。回歸測試 `test_permission_repair_security.py`、`test_owner_repair_pure.py`、`test_resource_manager_permission_repair.py`，真核心 `test_isolation_linux.py` | ADR-0272 |
| 設定檔讀不到時放寬（MEDIUM，C2-2） | `auth_policies.json` 啟動時讀不到退回預設、黑名單是空的，YOLO 下黑名單擋的指令直接放行；`autonomy.json` 讀不到退回「不限輪數、總量上限關閉」；MCP 設定讀不到時安裝或建立以空設定整份寫回（刪掉其他伺服器、重新啟用停用的、id 檢查失效）；設定資料夾列不出來時啟動健檢直接通過 | auth_policies 啟動讀不到改走「政策未載入一律拒絕」（只能聊天）；autonomy 讀不到每一項取最嚴、停止自主執行；兩者 `/health` degraded，`system_notices` 給畫面提示；MCP 寫入端讀不到丟 `PERSISTENCE_ERROR`、檔案不變；健檢列不出設定資料夾就中止啟動。殘留：修好權限要重新啟動；壞 JSON 不在範圍；autonomy 讀不到時在安全設定頁按存檔，會用暫代的最嚴值覆寫原檔並清掉提示（只會更嚴，但原因從畫面上消失）。回歸測試 `test_config_unreadable_fail_closed_security.py`、`test_health_system_notices.py` | ADR-0272 |
| MCP 連線途中失效或從別的 task 關閉會取消建立者（MEDIUM，可用性） | Touka 在建立連線的 task（lifespan、聊天請求、restart／存設定／Hub 安裝請求、工具呼叫）裡進入 SDK 的 Client 與 transport 後一直不退出。遠端 MCP 伺服器在呼叫途中斷線、逾時或回 3xx（dep1 的轉址 hook 擋下），或從別的 task restart、重載、重加這條連線，anyio 都把取消送回建立者，並遞迴殺掉它之後建立的連線：lifespan 被取消後，所有 MCP 連線、Dispatch 監督與背景任務停擺，畫面看起來仍正常，要重開容器才恢復 | 每條連線一個專屬 owner task（`protocols/mcp/owner.py`），連線的進入與退出都只在 owner 內：傳輸出事只結束那一條、記錯誤並停訂閱，不取消呼叫端、不自己重連（仍要手動 restart）；關閉期限 7 秒，到期只取消 owner 自己的 anyio scope，SDK 的 stdio 收尾照常殺行程樹；放進 pool 收斂成 `_install_handle`（先換再關；被換掉的舊連線一定會在期限內關閉（SDK 收尾會殺行程樹））；關機並行關、總期限 7＋1 秒。殘留：stdio 伺服器自己結束時不即時通知（下次呼叫才記錯誤）；transport 子 task 仍帶 workload bootstrap 票（既有，啟用 workload OAuth 前必修）；Proxy 被強殺時，原生 Linux 仍會留下 stdio 子行程（Docker 內隨 PID namespace 收掉；life1 之前就有，交 C 線 Transport）；只在 Windows 實測。回歸測試 `tests/unit/test_mcp_connection_isolation_security.py`、`tests/unit/test_mcp_midflight_transport_failure.py`（dep1 的三個 xfail 轉綠） | ADR-0273 |
| 無痕對話讀寫得到舊對話庫（MEDIUM，加嚴） | 無痕不列 `memory_tools`，但檔案動詞 `read／list／find／grep` 與寫入家族的 `scope` 在無痕仍有 `legacy`，唯讀類在 Manual 直接放行，無痕對話讀得到 `legacy/<agent>/` 的原始檔、也寫得進去（寫入會把無痕內容存下，edit 也能拿來反推舊內容）；舊的 `vault_tools` shim 在九個動詞都列出時依 ADR-0269 後繼規則也算列出，同樣走得到 | 無痕時 ToolLayer 改列拿掉 `legacy` 的檔案動詞 schema（共用 schema 不動）；硬叫時九個動詞的入口與 Proxy `vault_tools` 橋（動詞與 shim 共用）都回 `permission_denied`（`denied_scope="legacy"`、`reason="incognito"`），讀取與寫入家族都擋，記憶引擎不被呼叫；判斷是純函式 `utils/auth_mode.incognito_hides_vault`。測試 `tests/unit/test_incognito_legacy_scope_security.py`（先寫紅燈） | ADR-0274 |
| MCP 錯誤訊息遮罩弄壞網址（LOW，可讀性；禁令 23） | 通用路徑規則把 `http://` 的 `p:/` 當磁碟代號（`http://localhost:8000` → `htt<path>`），Unix 規則也吃掉網址路徑，MCP 頁與 log 的連線錯誤讀不出是哪台伺服器 | 遮罩搬到 `utils/mcp_secrets.py` 的 `sanitize_diagnostic`。網址先切出來（結尾同舊規則：空白或引號），http(s) 只顯示合法主機、埠與白名單路徑段（小寫英數與 `._~-`、15 字內、8 字以上不含數字、不是本機根目錄名），第一個不符的段或 `?`、`#` 之後整段 `<redacted>`；帳密、主機不合法、截斷切在網址中間都整段遮。`file:` 的路徑就是本機路徑、其他 scheme 語意不明，一律整段遮。網址以外照舊跑 UNC／Win／Unix，最後整串照舊跑 secret 規則，env 精確遮罩不變。緊貼網址的單段本機路徑補哨兵斜線再遮，維持舊規則借網址斜線遮住的效果。殘留：第一個不符段之前的短小寫段（7 字內，或 15 字內不含數字）會顯示，原本整段被 Unix 規則順帶遮掉；遠端設定網址的各段沒有加進精確遮罩（要另從設定的 `url` 拆段，這次不做）。`test_mcp_error_url_sanitize_security.py` | — |
| 模型帶超長標頭參數讓整台 MCP 熔斷（MEDIUM，可用性） | dep1 只把清單核對的拒絕移出斷路器；模型給的參數放進標頭後超過上限（單值 2048、總和 8192 bytes）仍當上游故障：寫健康頁、計入斷路器、回 `upstream_error`，同一工具錯 3 次整台伺服器停 30 秒 | 改丟 `McpToolArgumentRefusal`，回 `validation_error`（`reason=mcp_header_argument_invalid`；點名標頭、不含參數值；建議提醒非 ASCII 會 base64）。呼叫前的拒絕（清單、參數）共用 `McpToolCallRefusal`，對斷路器中立（`BreakerNeutralError`：不記失敗也不記成功，dep1 的清單拒絕也一樣）；原本記成功會讓穿插的拒絕把真的失敗計數歸零、把半開的斷路器關回去。未處理：伺服器回 JSON-RPC `-32602` 或 HTTP 413 仍計入斷路器。`test_mcp_call_time_catalog_security.py` | ADR-0203 |

### 2026-05-18 第一～二輪審計（C1–N12）

以下弱點已於 commit `7c35f7e` 修復。

| ID | 元件 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **C1** | `ToukaProxy/src/core/security.py` | 硬編碼 JWT 預設密鑰 | 🔴 Critical | 移除公開已知預設值；未設定時自動生成隨機密鑰並記錄警告 |
| **C2** | `ToukaProxy/src/services/auth_service.py` | 預設管理員密碼 | 🔴 Critical | 將 `"admin_password_change_me"` 改為 `secrets.token_urlsafe(16)` 隨機生成 |
| **C3** | `ToukaProxy/src/routers/proxy.py` | Header 注入身份繞過 | 🔴 Critical | 移除 `X-Agent-ID` Header fallback；未驗證請求固定為 `agent_id="Guest"` |
| **C4** | `ToukaProxy/src/routers/editor.py` | 路徑穿越（Path Traversal） | 🔴 Critical | 將 `startswith()` 字串比對改為 `Path.is_relative_to()` 正確邊界檢查 |
| **C5** | `ToukaProxy/src/tools/terminal.py` | Shell 指令注入 | 🔴 Critical | 偵測鏈式 metachar（`; \| && $(...)`）；SafeAuto 下自動升級為 CONFIRM |
| **C6** | `ToukaProxy/src/tools/builtin/evolution.py` | 演化沙箱程式碼注入 | 🔴 Critical | 提取 `_build_jail_code()`；`tool_name` 改用 `json.dumps()` 轉義 |
| **C7** | `ToukaProxy/src/services/sns_gateway.py` | Webhook 無驗簽框架 | 🔴 Critical | 新增 `SNSIncomingEvent.is_verified` 欄位與 `_warn_if_unverified()` 警告 |
| **C8** | `ToukaProxy/src/routers/terminal.py` | 終端機審核端點無驗證 | 🔴 Critical | `approve` / `reject` 端點加入 `Depends(get_verified_identity)` 與角色檢查 |
| **C9** | `ToukaHeartbeat/awaken.py` | 排程端點無驗證 | 🔴 Critical | `GET/POST/DELETE /v1/schedules` 加入 `verify_schedule_token()` Bearer Token 驗證 |
| **H1** | `ToukaProxy/src/routers/auth.py` | Cookie 缺 Secure 旗標 | 🟠 High | 新增 `SECURE_COOKIES` 設定；生產環境啟用 HttpOnly + Secure；抽出 `_set_refresh_cookie()` SSOT helper |
| **H2** | `ToukaProxy/src/routers/auth.py` | 認證端點無速率限制 | 🟠 High | 新增 `RateLimiter`（IP 滑動視窗）；`/auth/login` 失敗計數，超限回 429；成功後重置 |
| **H3** | `ToukaMemory/src/memory_server/services/manager.py` | 記憶體內容注入 | 🟠 High | 新增 `content_guard.py` `sanitize_memory_content()`；偵測 8 種 Prompt Injection 模式；超長內容截斷 |
| **H4** | `ToukaProxy/src/services/evolution_security.py` | 沙箱環境變數洩漏 | 🟠 High | 擴充黑名單加入 `PYTHON_*`/`LD_*`/`DYLD_*` 等模式；顯式移除 7 個已知危險變數 |
| **H5** | `ToukaProxy/src/config.py` | Heartbeat Token 硬編碼 | 🟠 High | 移除公開預設值 `"touka_heartbeat_secret_2026"`；預設改為空字串 |
| **H6** | `ToukaProxy/src/services/auth_service.py` | Refresh Token 無輪換 | 🟠 High | `refresh_access_token()` 改為同時簽發新 refresh token；`/auth/refresh` 端點設置新 cookie |
| **M1** | `ToukaProxy/src/routers/terminal.py` | CSRF 無防護 | 🟡 Medium | 新增 `csrf_guard.py` `verify_origin()`；approve/reject 端點驗證 `Origin` 標頭，跨域回 403 |
| **M2** | `ToukaProxy/src/routers/terminal.py` | 審核佇列無上限 | 🟡 Medium | 新增 `MAX_PENDING_APPROVALS = 50`；`request_approval()` 在佇列滿時立即返回，防 OOM |
| **M3** | `ToukaProxy/src/services/sns_gateway.py` | ReDoS 風險 | 🟡 Medium | `_ALIAS_PATTERN` 量詞 `+` → `{1,50}`；新增 `_guard_content_length()`（10K 字元上限） |
| **M4** | `ToukaProxy/src/routers/mcp.py` | MCP Config 反序列化未驗證 | 🟡 Medium | `MCPServerItem.command` 加入 `@field_validator` 拒絕 shell metachar；GET 端點透過 Pydantic 驗證 |
| **M5** | `ToukaChat/src/lib/connections.ts` | 前端 Header 注入 | 🟡 Medium | 新增 `sanitizeHeaderValue()` 函式；剝除 `\r \n \x00` 後才設定 `X-Touka-Soul-ID` |
| **M6** | `ToukaHeartbeat/storage_service.py` | Heartbeat Token 明文儲存 | 🟡 Medium | `StorageService._initial_config()` 首次啟動時生成隨機 token 並持久化；重啟後復用 |
| **H5-残留** | `ToukaHeartbeat/config.py`、`routers/heartbeat.py` | Heartbeat Token 殘留硬編碼 | 🟠 High | 兩處殘留 `"touka_heartbeat_secret_2026"` 補修：config.py 預設改 `""`；heartbeat.py 移除 `or` fallback 並加入空 token 早回 |
| **N1/N2** | `ToukaProxy/src/routers/connections.py` | `/ui/connections` 端點無驗證 | 🔴 Critical | 全端點（GET/POST/status）加入 `_require_connections_role()` dependency；`/status` 回傳消毒後結果防憑證外洩 |
| **N3** | `ToukaProxy/src/routers/mcp.py` | MCP Hub Install 無驗證 | 🔴 Critical | `POST /mcp/hub/install` 加入 `Depends(get_verified_identity)` + Owner/admin 角色檢查 |
| **N4** | `ToukaProxy/src/services/auth_service.py` | users.json TOCTOU 競爭條件 | 🟠 High | 新增 `_users_file_lock: threading.Lock`；抽出 `_atomic_users_write(mutate_fn)` 原語；5 個 read-modify-write 方法全部採用原子 pattern |
| **N5** | `ToukaProxy/src/routers/terminal.py` | 終端機審核佇列競爭條件 | 🟠 High | 新增 `_approval_lock: asyncio.Lock`；`approve_command`、`reject_command`、`request_approval` 的狀態修改在鎖內執行 |
| **N6** | `ToukaProxy/src/routers/mcp.py` | NPM Package SSRF | 🟠 High | 新增 `_validate_npm_package_name()` 嚴格正則驗證；`fetch_npm_downloads()` 驗證失敗直接回 0 不發 HTTP；加入 `follow_redirects=False` |
| **N7/N9** | `ToukaProxy/src/services/models/manager.py` | 連線錯誤憑證洩漏 | 🟡 Medium | 新增 `_sanitize_connection_error()` 消毒函式；存入 `_connection_errors` 前呼叫，消除 Bearer/API key 模式 |
| **N8** | `ToukaProxy/src/services/models/fetcher.py` | HTTP follow_redirects=True 允許 SSRF | 🟡 Medium | 改為 `follow_redirects=False`；合法 API 端點不需要重定向 |
| **N10** | `ToukaProxy/src/routers/ui_sessions.py` | Project IDOR | 🟡 Medium | `GET /ui/projects/{id}/overview` 加入 `Depends(get_verified_identity)` + Owner/admin 角色檢查 |
| **N11** | `ToukaProxy/src/services/user_service.py` | user_service TOCTOU 競爭條件 | 🟡 Medium | `delete_user()` 和 `_update_user_in_list()` 改用 `_atomic_users_write(mutate_fn)` 透過鎖定原語操作 |
| **N12** | `ToukaProxy/src/utils/async_db.py` | 動態 SQL placeholder 組裝 | 🟢 Low | `get_home_session_id()` 將 `f"IN ({placeholders})"` 改為固定字面量 `IN (?, ?)`（`possible_ids` 永遠為 2 個元素）|

> 共修復 **33** 項：11 Critical + 10 High + 11 Medium + 1 Low。

### 2026-05-21 第三輪審計（P1–P34）

| ID | 元件 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **P1** | `ToukaProxy/src/main.py:235` | 路徑穿越（Sovereign Output） | 🔴 Critical | `str(...).startswith()` → `Path.is_relative_to()`；與 `routers/editor.py` 保持一致 |
| **P2** | `ToukaChat/src/components/artifacts/ArtifactView.tsx` | Mermaid XSS | 🔴 Critical | `securityLevel:'strict'`；`dangerouslySetInnerHTML` 改為 `DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true } })`；`dompurify ^3.4.5` 安裝 |
| **P3** | `ToukaProxy/src/routers/terminal.py` | `/terminal/stream` SSE 無認證 | 🔴 Critical | 加入 `Depends(get_verified_identity)`；從 `endpoint_auth_allowlist.py` 移除 |
| **P4** | `ToukaChat/src/lib/connections.ts` | Access Token 存 localStorage | 🔴 Critical | 移除所有 `touka_auth_token` localStorage 操作；改為模組級 `let _accessToken: string \| null = null`；export `setAccessToken/getAccessToken/clearAccessToken`；`AuthContext.tsx` 同步使用 in-memory 函式 |
| **P5** | `ToukaProxy/src/utils/async_db.py` | `append_message_to_session` TOCTOU | 🔴 Critical | 模組級 `_session_locks: dict[str, asyncio.Lock]`；`append_message_to_session` 在鎖內執行 |
| **P6** | `ToukaProxy/src/routers/auth.py` | `/auth/register` 與 `/auth/refresh` 無速率限制 | 🟠 High | 新增 `register_rate_limiter` + `refresh_rate_limiter`（IP 滑動視窗，複用 `RateLimiter`） |
| **P7** | `ToukaProxy/src/services/identity_router.py` | localhost 可 assume 任意 Debugger | 🟠 High | LDH Debugger 分支加入 `owned_agents` 擁有權檢查；admin 免檢 |
| **P8** | `ToukaProxy/Dockerfile`、`ToukaHeartbeat/Dockerfile`、`ToukaChat/Dockerfile` | Docker 以 root 執行 | 🟠 High | 加入 `useradd appuser` + `chown -R appuser /app` + `USER appuser` |
| **P9** | `ToukaProxy/src/routers/connections.py` | `save_connections` 無鎖 | 🟠 High | 模組級 `_connections_lock = threading.Lock()`；POST handler 在鎖內執行 |
| **P10** | `ToukaChat/src/components/PlanTracker.tsx` | postMessage 無 origin 驗證 | 🟠 High | `handleMessage` 開頭加入 `if (event.origin !== window.location.origin) return;`；拒絕所有跨域 postMessage |
| **P11** | `ToukaProxy/src/main.py` | 無安全標頭 | 🟠 High | 新建 `SecurityHeadersMiddleware`；注入 `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: strict-origin-when-cross-origin` |
| **P12** | `ToukaChat/src/components/artifacts/ArtifactView.tsx` | iframe CDN 無 SRI | 🟠 High | 移除 `srcdoc` 中的外部 CDN script（Tailwind runtime）；改為內聯最小化 CSS reset；artifact 預覽不需完整 Tailwind runtime |
| **P13** | `ToukaProxy/src/services/auth_service.py` | Refresh Token 輪換不廢止舊 token | 🟡 Medium | Refresh token 加入 `jti` claim（UUID）；`users.json` 新增 `refresh_token_jti` 欄位；舊 token 重用觸發全面撤銷；新 ErrorCode `AUTH_REFRESH_TOKEN_REUSE`（HTTP 401） |
| **P14** | `ToukaProxy/src/routers/debug.py` | Debugger 可執行任意工具 | 🟡 Medium | `/debug/execute` 對 Debugger 角色檢查 `permission_service.get_capabilities_by_id()` 的 `allowed_tools`；Owner/admin 免檢 |
| **P15** | `ToukaProxy/src/config.py` | `SECURE_COOKIES` 預設 False | 🟡 Medium | 預設改 `True`（生產安全）；`.env` 明確覆蓋 `SECURE_COOKIES=false`（開發 HTTP 用） |
| **P16** | `ToukaProxy/src/utils/path_security.py` | Vault 路徑不檢查 symlink | 🟡 Medium | 新建 `validate_no_symlink_escape(path, allowed_root)` — 透過 `Path.resolve()` 解析 symlink 後再做 `is_relative_to()` 邊界檢查 |
| **P17** | `ToukaProxy/src/adapters/gemini_to_openai.py:51` | `ast.literal_eval` 取代 `json.loads` | 🟡 Medium | 改用 `json.loads()`；移除 `import ast` |
| **P18** | `ToukaProxy/src/services/autonomy.py` | 配額讀寫無鎖 | 🟡 Medium | 模組級 `_autonomy_lock = threading.Lock()`；`check_quota` / `consume_quota` 抽出 `*_unlocked` 私有方法並在鎖內呼叫 |
| **P19** | `ToukaProxy/src/main.py` | 請求 Body 無大小限制 | 🟡 Medium | 新建 `RequestSizeLimitMiddleware`；讀取 `Content-Length`，超過 `MAX_REQUEST_BODY_BYTES`（預設 10MB）回 413；新 ErrorCode `REQUEST_TOO_LARGE` |
| **P20** | `ToukaProxy/requirements.txt`、`ToukaHeartbeat/requirements.txt` | 套件版本寬鬆 `>=` | 🟡 Medium | 改為 `~=`（compatible release）；每項固定至目前安裝版本（後由 ADR-0117 uv 單一鎖檔取代） |
| **P21** | `ToukaProxy/src/services/permission_service.py` | 配置熱重載競態 | 🟡 Medium | 模組級 `_config_reload_lock = threading.Lock()`；`_auto_reload_check()` 抽出 `_auto_reload_check_unlocked()` 並在鎖內呼叫 |
| **P22** | `ToukaProxy/src/routers/observability.py` | 觀測端點過多公開 | 🟡 Medium | `/logs`、`/logs/recent`、`/logs/proxy`、`/memory`、`/memory/list`、`/memory/health`、`/tools`、`/tools_stats`、`/system_logs` 全加 `Depends(get_verified_identity)`；從 allowlist 移除 5 條 |
| **P23** | `.env`、`.env.example` | 硬編碼 Heartbeat token | 🟡 Medium | `.env` 改 `HEARTBEAT_TOKEN=`（空值）；`.env.example` 改為 placeholder 說明 |
| **P24** | `ToukaProxy/src/services/identity_router.py` | LDH 自動發卡無速率限制 | 🟢 Low | 模組級 `_ldh_limiter = RateLimiter(max_attempts=30, window_sec=60)`；LDH 分支超限跳過發卡（仍回傳 identity） |
| **P25** | `ToukaProxy/src/services/user_service.py` | 刪除使用者不廢止 session | 🟢 Low | `delete_user()` 在刪除前設 `refresh_token_exp=0`、`refresh_token_jti=None`，立即讓 Cookie 失效 |
| **P26** | `ToukaProxy/src/routers/heartbeat.py:372` | Heartbeat owner 過期邊界競態 | 🟢 Low | `now_sec > exp` → `now_sec >= (exp + 30)`（30 秒容許時鐘偏差的 grace period） |
| **P27** | `ToukaProxy/src/routers/admin.py` | Admin 建立使用者無審計 | 🟢 Low | `create_user()` 與 `update_user()` 成功後以 `logger.warning("AUDIT: ...")` 記錄操作者、目標帳號與角色 |
| **P28** | `ToukaProxy/src/utils/async_db.py` | Session 無 TTL | 🟢 Low | ⚠️ **已撤回（ADR-0259，2026-10-15）**：原修補（本批 2026-05-21，`c8fdd646`）加了 `last_activity_at` 欄位與遷移、`cleanup_expired_sessions()`（lifespan 啟動呼叫）與 `SESSION_TTL_DAYS=90`，但截止值用秒算、`last_activity_at` 存毫秒，上線後從沒刪過任何列；修好單位等於每次啟動把 90 天沒寫入的非家對話整列刪掉、不轉存（非家對話不會被滑動視窗剪進舊對話庫，原文永久消失）。Owner 決定正式拿掉：刪 `cleanup_expired_sessions()`、啟動呼叫與 `SESSION_TTL_DAYS`，對話不再自動刪除；`last_activity_at` 欄位、遷移與寫入保留（對話存檔合約仍在用）。刪除改由人類手動刪除或靈魂重置。 |
| **P29** | `ToukaProxy/src/routers/heartbeat.py:387` | DEBUG prefix 日誌洩漏驗證狀態 | 🟢 Low | `logger.info(f"DEBUG: Agent ... passed all checks.")` → `logger.debug("Heartbeat checks passed for %s", ...)` |
| **P30** | `ToukaProxy/src/services/auth_service.py` | Refresh Token Rotation 多 Tab 並發競態（Race Condition） | 🟠 High | `User` model 新增 `previous_refresh_jti` 與 `refresh_jti_rotated_at` 欄位；`_mutate_refresh` 三路徑判斷：(1) 正常輪換 (2) `previous_jti` 在 `AUTH_REFRESH_GRACE_PERIOD_SEC`（30 秒）內的並發請求 (3) 其他 JTI 不符觸發撤銷；`config.py` 新增常數 `AUTH_REFRESH_GRACE_PERIOD_SEC=30`（可環境變數覆蓋） |
| **P31** | `ToukaMemory/src/memory_server/services/mission_manager.py` | 自主任務狀態轉換缺乏有限狀態機約束 | 🟡 Medium | 實作自主任務狀態 FSM 轉換矩陣，防止跳過 `DOING` 步驟直接進入 `DONE` 的邏輯越軌行為，保證狀態流轉一致性 |
| **P32** | `ToukaMemory/src/memory_server/services/mission_manager.py` | FAILED 狀態的特權重置繞過 | 🟠 High | 限制只有具備 `can_supervise_dispatch` 特權標籤或管理員角色（Owner/Debugger/admin）的 Touka Agent 身分才能將狀態重設為 `TODO`，防外部 AI 代理人自我掩蓋失敗 |
| **P33** | `ToukaMemory/src/memory_server/models/mission.py` | 產出物欄位型別混用與覆蓋踩踏 | 🟡 Medium | 將單一產物 `artifact_ref` (str) 與多產物列表 `artifact_refs` (list[str]) 徹底解耦，各自獨立處理，防範互相覆蓋踩踏，並強化路徑穿越與絕對路徑防禦 |
| **P34** | `ToukaProxy/src/routers/observability.py`（`/ui/mission`） | `/ui/mission` API 回傳 model 觸發 500 崩潰 | 🟡 Medium | 將回傳值由 `MissionState` 實體改為 `mission.model_dump()` 字典，消除 FastAPI `ResponseValidationError` 導致的 500 崩潰問題 |

> 共修復 **34** 項：5 Critical + 10 High + 13 Medium + 6 Low。

### 2026-05-26 第四輪審計（D1–D6）

| ID | 元件 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **D1** | `ToukaDispatch/server.py` | 身分偽冒（Agent Identity Spoofing） | 🔴 Critical | 移除所有操作工具的 `agent_id` 參數；新增 `dispatch_handshake` 工具以 `DISPATCH_TOKEN` 換取短命 session token；所有 session 固定綁定 `DispatchAgent` 身分，呼叫方無法透過參數宣稱任意身分取得越權 |
| **D2** | `ToukaDispatch/server.py` | 路徑穿越（Path Traversal via agent_id） | 🔴 Critical | 同 D1 修復：`agent_id` 已從所有工具移除，不再進入路徑拼接（`Path(...) / "personal_drive" / agent_id / "missions"`），路徑穿越攻擊面完全消除 |
| **D3** | `ToukaDispatch/server.py` | MCP 呼叫參數洩漏（DISPATCH_TOKEN in tool params） | 🟠 High | ⚠️ 已由 ADR-0050 重新評估：D3 修復（移除 dispatch_token 參數）造成 D4 零認證缺陷，屬錯誤取捨，已撤回。DISPATCH_TOKEN 在 handshake 時透過參數傳遞，視為已知接受的輕量風險。 |
| **D4** | `ToukaDispatch/server.py` | dispatch_handshake 完全無認證（Auth Bypass） | 🔴 Critical | 恢復 `dispatch_token: str` 參數；新增 `_verify_dispatch_token(provided)` 使用 `secrets.compare_digest` 進行 timing-safe 驗證；空 token 或不符合立即拒絕。（ADR-0050）|
| **D5** | `ToukaDispatch/session_manager.py` | bearer 明文留在 session store／跨 transport 重播 | 🟠 High | bearer 明文不再作 dict key；store 改存 SHA-256 digest，驗證以 `secrets.compare_digest`，並把 session 綁 transport/audience/method。呼叫方自述名稱／版本不保存；跨 transport 使用一律拒絕。（ADR-0201） |
| **D6** | `ToukaDispatch/session_manager.py` | mission credential 重播與非原子 rotation | 🟠 High | mission token 在鎖內原子驗證＋消耗，第二次 handshake 拒絕；新 mission 於同一鎖內升 generation、清舊 credential、撤銷舊 external session。程序重啟不恢復 ephemeral credential。（ADR-0201） |

### Structural Security Improvements（非 CVE，架構層加固）

| 項目 | 說明 |
|------|------|
| **SecurityHeadersMiddleware** | 全域安全標頭注入（nosniff / DENY / referrer-policy）；所有 response 自動附加 |
| **RequestSizeLimitMiddleware** | 請求體大小守門員；超過 10MB 回 413；防止記憶體耗盡攻擊 |
| **uv workspace 單一鎖檔** | root＋各 member `pyproject.toml` 以 `~=`／`==` 宣告，單一 `uv.lock` 固定全圖與 artifact identity（ADR-0117） |
| **Docker non-root** | 三個服務容器（Proxy / Heartbeat / Chat）均加入 `USER appuser`，符合 CIS Docker 基準 |
| **jti Replay Detection** | Refresh token 輪換加入 jti 機制，重用舊 token 立即撤銷全部 session |
| **Guard #22** (pip-audit) | 從 frozen 單一 lock 建立 repo 外 runtime＋build/test 兩 scope；invalid evidence、tool error 與無權威 severity fail-closed，缺 tool 不自動安裝；合法 skip row 保留名稱／原因並以 INCOMPLETE 停止，不放行未查核依賴（ADR-0222） |
| **Guard #23** (npm audit) | ToukaChat／ToukaIDE 各掃 runtime＋full graph；直接 Node＋npm-cli argv、public-registry lock（official registry＋SHA-512）與無憑證 child env，invalid evidence／high／critical 阻斷；braces 回補 coverage 見 ADR-0237（ADR-0222） |
| **Guard #24** (Endpoint Auth Dependency Guard) | AST 掃描所有 FastAPI router 端點，強制每個非豁免端點必須帶有至少一個認證 Depends；grandfather 清單（52 個歷史技術債端點）全部補修完畢（ADR-0034） |
| **SOLID-SRP Heartbeat Token** | `/heartbeat/awaken` 的所有 JWT 驗證邏輯移入獨立的 `verify_heartbeat_token()` Depends 函式；`awaken_agent` body 不再包含任何 token 驗證邏輯 |
| **Guard #25 Native NPM Build Environment** | `scripts/guard_native_npm.py` 靜態掃描前端 `dependencies` 中的 native module（`KNOWN_NATIVE_MODULES` SSOT），驗證 Dockerfile 備妥 `python3 make g++` |
| **測試 fixture 身分匿名化** | 所有測試 fixture 移除真實 Agent ID；production code 中的硬編碼 Agent 名單全數移除（`soul_service.py`、`agent_detector.py`、`brain_persistence.py`） |
| **AGENT_TOKENS 匿名化** | 設計系統 token 鍵名由真實 Agent 名稱改為顏色主題名（Amber/Rose/Emerald），不暴露 Agent 身分 |

### 2026-05-30～06-09 架構安全設計（A1–A25）

A1–A3 依 ADR-0064（Token 隔離）、A4–A6 依 ADR-0067（自動召喚進程隔離）、A7–A8 依 ADR-0066（Skill URL 安裝與解壓）、A9–A13 依 ADR-0069（創建引擎）、A17–A20 為冒煙隔離與匿名化、A21–A25 依 ADR-0074／ADR-0079（策展技能目錄與 MCP 安裝）。

| 編號 | 位置 | 弱點/設計 | 等級 | 緩解措施 |
|------|------|-----------|------|----------|
| **A1** | `ToukaDispatch/session_manager.py` | 委派 Token 隔離 | 🔵 Architecture | `DISPATCH_TOKEN` 主金鑰永遠不離開 Touka 進程邊界；外部 AI 只接收一次性 `mission_token`。ADR-0201 再把它綁定 mission generation、首次 handshake 原子消耗，且 rotation 同時撤銷舊 credential 與 external session。 |
| **A2** | `ToukaDispatch/server.py` | 外部 Session 權限限制 | 🔵 Architecture | `DISPATCH_TOKEN` → internal session；`mission_token` → external session。外部 method allowlist 不含 `dispatch_initialize_mission`，並在每次 request 重驗，而非只信任握手時狀態。（ADR-0064、ADR-0201）|
| **A3** | `ToukaDispatch/server.py` | Board 完整性檢查 | 🔵 Architecture | external session 同時受 `bound_mission_id` 看板一致性與 manager generation 雙閘約束；mission 不符、已替換或跨 transport 使用皆拒絕。（ADR-0064、ADR-0201）|
| **A4** | `ToukaDispatch/server.py` `_build_child_env` | 環境變數隔離（whitelist） | 🔵 Architecture | 自動召喚外部 AI 時，子進程環境採 **whitelist 預設拒絕**：只保留執行必需的系統 env（PATH/SYSTEMROOT/...）+ 三個 DISPATCH_*。全域 `DISPATCH_TOKEN` 主金鑰與所有密鑰（含不帶關鍵字者）一律不傳；外部 AI 只收到一次性 mission_token |
| **A5** | `ToukaDispatch/server.py` `_invoke_external_ai` | 指令注入防護 | 🔵 Architecture | command 全來自 `config/dispatch_hooks.json` 白名單 JSON list，直傳 `subprocess.Popen` + **shell=False**，零字串拼接 → 零指令注入面。fire-and-forget + DEVNULL + close_fds。靜態測試禁 `shell=True` |
| **A6** | `ToukaDispatch/config/dispatch_hooks.json` | 召喚白名單竄改 | 🟠 Operational | 新增外部 AI executor 只改設定檔（CaC）。fail-closed：缺檔/壞檔/未列入 → 不召喚。**部署須限 Owner 寫**（Docker 唯讀層；本機限檔案權限），防竄改注入 command |
| **A7** | `ToukaProxy/src/services/skill_service.py` `install_skill_from_url` | Skill URL 安裝 SSRF | 🟠 High | 由 URL 安裝 `.skill` 僅允許 `ALLOWED_SKILL_DOMAINS` 白名單網域；`follow_redirects=False`；下載大小上限 20MiB。安裝以 `.bak` 原子覆寫 + `finally` 回滾，失敗不留半成品。端點限 Owner/admin |
| **A8** | `ToukaProxy/src/services/resource_manager.py` `extract_archive` | `.skill`/ZIP 解壓路徑穿越與 zip bomb | 🟠 High | 解壓前對每個 entry 以 `Path.resolve()` + `parents` 檢查，拒絕 `../` 路徑穿越與絕對路徑逃逸；zip bomb 防護：entry 數上限 2000、單檔/總解壓上限 50MiB |
| **A9** | `ToukaProxy/src/routers/creation.py` `_require_creation_role` | 創建端點權限提升 | 🔵 Architecture | `/v1/creation/{skill,mcp,scaffold}` 全端點限 Owner/admin；無 token→401 `AUTH_INVALID_TOKEN`、角色不符→403 `AUTH_PERMISSION_DENIED`。agent 對話工具入口同樣先 `_gate(identity)`；驗證/SMOKE 失敗回 200+errors（不洩漏堆疊、不報 500） |
| **A10** | `ToukaProxy/src/services/creation/scaffold.py` | Scaffold 程式碼生成注入 / 路徑穿越 | 🟠 High | 決定性 stub 生成（零 LLM）；識別字須 `isidentifier()` 且非關鍵字；自由文字僅以 `repr()` 轉義為字面值嵌入；型別限白名單；最終 `ast.parse()` 兜底；server 名正規化後須為**單段 slug**（`^[a-z0-9][a-z0-9_-]*$`），擋 `../` 逃逸 `config/mcp_servers/`；已存在或已註冊即拒（不覆寫） |
| **A11** | `ToukaProxy/src/services/creation/`（CreationEngine VALIDATE） | 既有產物被創建流程覆寫竄改 | 🔵 Architecture | CREATE 嚴禁覆寫既有 id——id 衝突於 VALIDATE 關卡擋下；只寫新產物，`data/soul\|custom\|personal_drive\|legacy\|vault` 完全不碰 |
| **A12** | `ToukaProxy/src/services/creation/smoke.py` | SMOKE 候選 server 看見 Touka 密鑰 | 🟠 High | 註冊前沙箱試跑以**淨環境**（`EvolutionPathGuard.get_clean_env()`）啟動候選 MCP server；`asyncio.wait_for` 逾時 + `async with` 保證拆除（重用 ADR-0033 演化沙箱）。2026-10-15 起（ADR-0254）試跑環境改用和正式安裝的 MCP 同一個白名單 `LAUNCH_ENV_ALLOWLIST`＋spec 宣告的 env，不再用 `get_clean_env()` |
| **A13** | `ToukaProxy/src/services/creation/model_judge.py` | LLM 評分意外付費 / 阻斷上線 | 🔵 Architecture | `ModelJudge` 採**雙閘預設關**：`TOUKA_EVAL_JUDGE_ENABLED` ＋ 呼叫時 `eval_scoring=True`，缺一即不計分、零付費呼叫；任何例外/解析失敗 fail-safe 回 0.0，EVAL 後置非阻斷 |
| **A17** | `tests/smoke/smoke_creation_engine.py` | 創建引擎冒煙測試污染配置 | 🟠 High | 測試前後經 `resource_manager` 清除臨時註冊的 MCP server 配置，防止污染 `server_config.json` 與高危測試 server 殘留 |
| **A18** | `docs/` 與 `tests/` | 歷史殘留硬編碼真實 ID 洩漏 | 🟡 Medium | 全面清查並以中性化模擬 ID（Alice、Bob、DebugAgent 等）替代真實 Agent ID |
| **A19** | `ToukaChat` / `ModelManager` | 動態模型發現預設值 fallback 降級洩漏 | 🟢 Low | 釐清發現源為空/出錯時 SSOT 降級至含 Model 1/Model 2 的 default，建立安全發現防線，防止無效模型干擾生產環境 |
| **A20** | `tests/smoke/smoke_group_dispatcher.py` | 參數型別不符 | 🟢 Low | `request_id` 由錯誤的 bool 改回 `str \| None` |
| **A21** | `ToukaProxy/src/services/skill_service.py` `install_skill_from_catalog` ＋ `skill_catalog_service.py` | 真技能逐檔安裝 SSRF／任意來源 | 🟠 High | 只接受策展 `config/skill_catalog.json.example` 內已審 `catalog_id`（使用者不自填 URL/repo）；逐檔抓 `raw.githubusercontent.com/<repo>/<釘 40-hex commit>/<file>`（runtime 零 `api.github.com`）；`follow_redirects=False`、單檔 20MiB／總量 50MiB／檔數 50 上限、逐檔 `resolve()`-containment；驗證通過才 `.bak` 原子上線、失敗 `finally` 回滾 |
| **A22** | 真技能含可執行腳本（`scripts/`） | 安裝即引入可執行碼 | 🟠 High | **信任模型（誠實揭露）**：根本防線是「只裝人工已審、commit-pinned 的策展來源」＋只收 `{Apache-2.0, MIT, BSD-3-Clause}`＋前端「⚠️ 含可執行腳本」警示＋二次確認＋Owner/admin，而非執行期掃描 |
| **A23** | `skill_catalog_service.py` `CatalogFile` ＋ `install_skill_from_catalog` | catalog 內容完整性（GitHub 帳號被黑／MITM／儲存損毀） | 🟠 High | 逐檔釘 `sha256`（`CatalogFile{path,sha256}`）：下載後比對雜湊，不符即 `VALIDATION_ERROR`、不落地；`test_skill_catalog_seed_integrity` 驗每檔 64-hex |
| **A24** | `ToukaChat` `McpInstallModal.tsx` ＋ `mcp/MyServersView.tsx` | MCP 安裝／刪除 UI 警示過輕 | 🟠 High | 安裝 modal 改紅色危險警示＋「我了解風險，安裝」；伺服器刪除前加二次確認。後端 Owner/admin RBAC、`/mcp/scan`、`is_command_safe` 不變 |
| **A25** | `ToukaProxy/src/services/mcp_hub_service.py` ＋ `config/mcp_hub.json(.example)` | 第一方 curated 文件 MCP（mcp-docgen）收錄 | 🟢 Low | 誠實標示 `is_official=false`；命令過 `is_command_safe`；`sync()` 不覆寫 curated 條目；`@TOUKA_OUTPUT@` 解析為受管輸出夾；mcp-docgen 自身有輸出 jail（ADR-0079） |

> 召喚授權鏈：使用者 → RBAC → session_token → `dispatch_initialize_mission`（內部 session only，授權完成點）→ subprocess 召喚是已授權動作的後果，非新授權決策點。
>
> A14–A16（ADR-0072 Skill Hub 同步來源收斂）隨 2026-06-07 persona Skill Hub 子系統移除而退役，對應程式已不存在。

### 2026-06-05 深度對抗式審計（DA1–DA28）

| ID | 元件 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **DA1** | `ToukaProxy/src/routers/config.py` | `/config/env` 無角色檢查、洩漏 JWT 簽章金鑰 | 🟠 High | `GET/POST /config/env`（讀寫 `.env`，含 `TOUKA_AUTH_SECRET`）原僅 `get_current_user`；配合開放註冊，任何使用者可讀簽章金鑰並偽造 admin。改掛 `_require_config_role`（`role=="admin"`），非 admin 回 403 `AUTH_ADMIN_REQUIRED` |
| **DA2** | `ToukaProxy/src/routers/identity.py`、`models/identity.py` | `POST /identity` 角色提升（Owner→admin） | 🟠 High | `POST /identity` 收緊為 admin-only，GET 維持 Owner 可讀；`IdentityMapUpdateRequest` 以 `ASSIGNABLE_AGENT_ROLES` 白名單驗證 role（排除 admin/System；ADR-0141） |
| **DA3** | `ToukaProxy/src/routers/ui_sessions.py` | UI 會話 IDOR（越權讀/寫/刪他人對話） | 🟠 High | 新增 `_can_access_session`（重用 `resource_manager.is_owner`）：admin/Debugger 全見、其餘僅及自身擁有的 agent；list 過濾、單筆越權回 403 |
| **DA4** | `ToukaProxy/src/routers/ui_sessions.py`（projects／legacy）、`utils/async_db.py` | `/ui/projects*` 與 legacy export IDOR | 🟠 High | 擁有權由「專案下會話的 agent」推導：`_can_access_project`（讀）／`_can_modify_project`（寫）；legacy export 限呼叫者擁有的 agent；空專案僅 admin 可讀 |
| **DA5** | `ToukaProxy/src/routers/ui_sessions.py` | `POST /ui/settings/workspace` 無角色檢查 | 🟡 Medium | 寫入全域 workspace 根目錄限 admin；GET 維持已認證可讀 |
| **DA6** | `ToukaProxy/src/tools/terminal.py` | terminal 子進程繼承全部密鑰環境變數 | 🟠 High | 終端子進程改傳 `env=EvolutionPathGuard.get_clean_env()`（名稱含 SECRET/KEY/TOKEN/AUTH/… 及 runtime-control 變數一律剝除，保留 PATH 等功能性變數）。修補當時是 `bash()` 的 3 個 `subprocess.run`；現為 `_execute_core` 經 `execution_broker.spawn_exec`（ADR-0239）的單一非同步子程序 |
| **DA7** | `ToukaProxy/src/services/identity_service.py`、`routers/auth.py` | JWT type 混淆：refresh token 可當 Bearer 用 | 🟡 Medium | `verify_token` 與 `get_current_user` 加 `type not in (None, "access")` 拒絕；refresh 只能在 /auth/refresh 使用 |
| **DA8** | `ToukaDispatch/server.py` | `dispatch_update_task` 缺看板完整性檢查 | 🟠 High | `update_task` 補上與 get_board 相同的 `is_external` ＋ `bound_mission_id` 一致性檢查，陳舊 external session 不能寫入已被替換的 mission |
| **DA9** | `ToukaDispatch/server.py`、`ToukaProxy/src/routers/dispatch_callback.py` | 外部 AI 派發文字未消毒進 agent LLM 脈絡 | 🟡 Medium | `dispatch_update_task` 與 `dispatch_callback` 接上 H3 `sanitize_memory_content`（8000 字截斷＋注入偵測）。**揭露**：H3 擋不住短惡意提詞，真正防線仍是只信任策展／受控來源 |
| **DA10** | `ToukaChat/src/components/artifacts/ArtifactView.tsx`（`WebRenderer`） | Web 產出物 iframe 可被用於釣魚/下載/beacon 外洩 | 🟡 Medium | sandbox 收緊為僅 `allow-scripts`；`srcdoc` 注入 CSP `default-src 'none'; script-src/style-src 'unsafe-inline'; img/font/media-src data: https:; connect-src 'none'; form-action 'none'; base-uri 'none'`。取捨：預覽中的產出物不會發出網路請求 |
| **DA11** | `ToukaProxy/src/routers/heartbeat.py` | 心跳 token 以 `==` 比對（非常數時間） | 🟢 Low | 改 `secrets.compare_digest(service_token.encode(), HEARTBEAT_TOKEN.encode())` |
| **DA12** | `ToukaProxy/src/services/resource_manager.py`（`is_owner`） | 多用戶下 Owner-agent 跨用戶資源主權誤判 | 🟢 Low | `Owner` 角色存取非自身 target 時，target 有明確且不共享的人類擁有者即拒；無主／公共 agent、自身存取與 `System` 角色不受影響 |
| **DA13** | `ToukaProxy/src/tools/editor.py`（`_get_safe_path`） | agent-tool 沙箱以 `startswith()` 判邊界 | 🟢 Low | 改用 `target_path.is_relative_to(drive_path)`（兩者皆已 resolve） |
| **DA14** | `ToukaProxy/src/tools/builtin/evolution.py`（`verify_mcp_locally`） | model 可用 override 超出 SSOT 沙箱配額 | 🟢 Low | `_clamp_quota`：override 僅在「正數且嚴格小於 SSOT ceiling」時採用，否則回退 ceiling |
| **DA15** | `ToukaProxy/src/services/skill_service.py` | skill/MCP metadata 無跳脫進系統提示 XML | 🟢 Low | dispatcher sync 與每請求注入路徑 `apply_skills_to_request` 以 `quoteattr`／`escape` 跳脫；只封結構越界，殘留純文字提詞注入仍需於安裝時權衡 |
| **DA16** | `ToukaProxy/src/services/evolution_security.py` | `verify_ownership` 死碼 | ⚪ Info | 移除零呼叫的安全劇場方法；演化授權由工具派發層 RBAC／ability 把關 |
| **DA17** | `ToukaProxy/src/routers/debug.py`、`routers/souls.py`（`update_soul`） | 跨用戶 IDOR：讀/匯出/覆寫他人 soul/context | 🔴 High（多用戶） | 三端點加 `resource_manager.is_owner(target, caller)`（admin/Debugger 旁路）：跨擁有者 read→空、export/update→403 |
| **DA18** | `ToukaProxy/src/utils/soul_io.py`、`utils/custom_io.py` | soul/custom 檔案沙箱 `startswith` 繞過＋缺檢查 | 🟡 Medium | 兩檔共 9 個檔案操作改走 `_safe_target` → `validate_no_symlink_escape`（resolved `is_relative_to`，亦擋 symlink/`..` 逃逸） |
| **DA19** | `ToukaProxy/src/tools/builtin/evolution.py`（`request_mcp_creation`） | 演化部署工具永遠失敗（kwarg 錯誤） | 🐛 功能 bug | 改傳 `identity=kwargs.get("identity") or AgentIdentity(agent_id=agent_id)`，對齊 `request_approval` 簽章 |
| **DA20** | `ToukaProxy/src/routers/debug.py`（`debug_execute_tool`） | Debugger 工具白名單一執行就崩潰 | 🐛 功能 bug | 改用 Gate-1 SSOT `get_allowed_tools(identity)` ＋ `is_tool_allowed(tool, allowed)` |
| **DA21** | `ToukaProxy/src/services/auth_service.py`（`ensure_admin_user`）、`user_service.py`、`config.py` | bootstrap admin 密碼明文寫進持久日誌 | 🟠 Medium | 改寫一次性檔 `config/INITIAL_ADMIN_PASSWORD.txt`（經 `resource_manager`），WARNING 只記路徑；首次改密與啟動時（admin 非 temp）自動清理 |
| **DA22** | `ToukaProxy/src/routers/admin.py` | admin 端點回傳完整 `User` 模型（含 bcrypt 雜湊＋refresh JTI） | 🟢 Low | 四個端點改 `response_model=UserOut`（排除敏感欄位）；`openapi.json`／前端型別同步 |
| **DA23** | `auth_service.py`、`identity_service.py`、`routers/auth.py`、`models/user.py` | 帳號停權（`status="suspended"`）從未強制 | 🟠 Medium | 新增 `User.is_active`，在 `verify_token`、`get_current_user`、`authenticate_user`（密碼驗證後檢查、不可枚舉）、`refresh_access_token` 強制 |
| **DA24** | `ToukaProxy/src/routers/heartbeat.py`（`/awaken`） | `security_level` 預設升級 FullYOLO ＋ `init_prompt` 未消毒 | 🟠 Medium | `_resolve_auth_level` 允許清單：僅 `"full"` 給 FullYOLO，未知值 fail-safe 回 SafeAuto；`init_prompt` 經 H3 消毒後才注入 |
| **DA25** | `ToukaProxy/src/routers/auth.py`、`services/user_service.py` | 無自助改密碼端點 | 🟢 Low | 新增 `POST /auth/change-password`（驗當前密碼→設新密碼→清 `is_temp_password`→撤銷 refresh token），僅能改自己 |
| **DA26** | `ToukaProxy/src/services/auth_service.py`（`refresh_access_token`） | refresh JTI 用 `==`/`!=` 比對 | 🟢 Low | 三處 JTI 比對改 `secrets.compare_digest` |
| **DA27** | `ToukaProxy/src/services/resource_manager.py`（`is_protected_path`） | soul/legacy 保護用 `startswith`（sibling 誤判） | 🟢 Low | 改 `p.is_relative_to(soul/legacy_prefix)`，只認真正子路徑 |
| **DA28** | `ToukaProxy/src/services/evolution_security.py`、`tools/builtin/evolution.py` | 演化沙箱無 OS 級資源限制 | 🟠 Medium | POSIX `preexec_fn` 套用核心級 `RLIMIT_FSIZE`／`RLIMIT_CPU`／`RLIMIT_AS`；Windows 無 `resource` 模組時優雅降級回逾時＋JailedWriter（ADR-0076） |

> DA1–DA28：7 High ＋ 9 Medium ＋ 9 Low ＋ 1 Info ＋ 2 功能 bug；DA17–DA28 由 DA11–16 對抗複審補抓。後端每項配 `test_*_security.py`（低風險群收於 `tests/unit/test_low_findings_security.py`），前端 DA10 配 vitest。

### 2026-06-09～12 韌性加固與 Dispatch 併入 Proxy（R1–R4 依 ADR-0078，T1–T4 依 ADR-0082）

| 編號 | 位置 | 弱點/設計 | 等級 | 緩解措施 |
|------|------|-----------|------|----------|
| **R1** | `ToukaProxy/src/services/resource_manager.py`（`write_file_text`/`read_json`） | 非原子寫入導致資料損毀 | 🔵 Architecture | 同目錄 `tempfile.mkstemp`→`flush`+`os.fsync`→`os.replace`，失敗清暫存並拋出；`read_json` 對壞檔記明確 corrupt 告警，不再靜默歸零 |
| **R2** | `ToukaProxy/src/routers/heartbeat.py`（`POST /v1/heartbeat/retrigger`） | 新增「UI 觸發背景自主 AI」的攻擊面 | 🔵 Architecture | 人類擁有者身分（`get_verified_identity` + `is_owner`，非 service token）＋ `can_awaken` ＋ 會話須存在；強制 `SafeAuto`、`pulse_count` 夾 1..5、仍受每日配額約束 |
| **R3** | `ToukaProxy/src/services/process_registry.py`、`tools/terminal.py` | bash 孫進程於逾時/停止後成孤兒 | 🟢 Low | 新行程群組啟動；逾時與 `/stop` 以 `os.killpg`／`taskkill /F /T` 連坐殺整個群組 |
| **R4** | `ToukaProxy/src/utils/async_db.py`（`upsert_session`）、`utils/task_utils.py`（`spawn_logged`） | 併發寫入丟欄位、背景例外被吞 | 🟢 Low | `upsert_session` 以 `_get_session_lock` 串行化；`spawn_logged` 以 `add_done_callback` 記錄背景任務例外 |
| **T1** | `ToukaDispatch/server.py`（`_bind_host()`） | dispatch 原硬編綁定 `0.0.0.0` | 🟢 Low | 預設改 `127.0.0.1`；需對外時以 `DISPATCH_BIND_HOST` 顯式升開。supervisor 以真 modern probe 驗 readiness，不因 localhost 省略 application auth（ADR-0201） |
| **T2** | 根 `.dockerignore`＋`.gitignore` | `.gitignore` 排除 `.dockerignore`，fresh clone 建置時 `COPY . .` 會把 `.env`／活體 config／資料烤進映像 | 🟠 Medium | 建立入版控的根 `.dockerignore`（排除 `.env*`、`ToukaProxy/config/*.json`（保留 `.example`）、`data/`、`backups/` 等）並自 `.gitignore` 移除忽略規則；`tests/lint/test_dockerfile_dispatch.py` 釘住契約 |
| **T3** | `ToukaProxy/src/services/dispatch_supervisor.py` | proxy 內 spawn 子程序的注入面 | 🔵 Architecture | spawn 命令固定為 `[sys.executable, <server.py 解析路徑>]`（無 shell、無外部輸入拼接）；env 由 supervisor 單點注入；新行程群組＋`terminate_process_tree` 關機連坐殺。proxy 重啟會終止 dispatch 整棵樹，看板狀態仍落盤 |
| **T4** | `ToukaProxy/src/routers/dispatch_callback.py` | Docker 模式回呼 URL 錯誤 | 🟢 Fix | supervisor 注入 `TOUKA_PROXY_URL=http://127.0.0.1:<port>`，回呼成立且維持 localhost-only 來源閘（ADR-0051） |

### 2026-06-17 Gemini 金鑰移出 URL query string（禁令 23 同類）

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **GK1** | `ToukaProxy/src/routers/models.py`（`check_model_health`） | Gemini 金鑰落 URL query string（日誌/例外洩漏） | 🟠 High | 改走 `headers={"x-goog-api-key": api_key}`、URL 不帶金鑰；例外只回穩定機器碼 |
| **GK2** | `ToukaProxy/src/services/models/fetcher.py`（`fetch_gemini_models`） | 同上（動態模型發現） | 🟠 High | URL 去金鑰 ＋ header；`follow_redirects=False` |
| **GK3** | `ToukaProxy/src/services/models/health.py`（`validate_model_health`） | 同上（背景健康輪詢） | 🟠 High | URL 去金鑰 ＋ header；補 `follow_redirects=False`（含同函式 xai 分支） |
| **GK4** | `ToukaProxy/src/handlers/gemini_handler.py`（`generateContent` 串流） | 同上（主要對話路徑） | 🟠 High | URL 去金鑰 ＋ header；補 `follow_redirects=False`；503 重試日誌不再含金鑰 |
| **GK5** | `ToukaProxy/src/services/gemini_cache_service.py`（`get_or_create_cache`） | 同上（Context Cache 建立） | 🟠 High | URL 去金鑰 ＋ header；補 `follow_redirects=False`（相關檔已隨 ADR-0243 刪除） |
| **GK6** | `ToukaProxy/src/tools/builtin/research.py`（`_search_google`） | `params={"key":...}` 仍併入 `request.url` | 🟡 Medium | 改走 header、徹底移出 URL；補 `follow_redirects=False` |

> 全 repo grep 確認無遺漏；回歸測試 `tests/unit/test_gemini_key_header_security.py` 驗金鑰進 header 不進 URL，並靜態鎖死不再以 `?key=`／`params={"key"}` 帶金鑰。

### 2026-06-21 DebugAgent 破壞性重置複審（ADR-0104）

上線前對抗式複審確認安全核心無破口（無誤刪真實 agent 路徑、安全閘 fail-closed、刪除全 scoped），並修復下列縱深缺陷。

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **R2/R8** | `ToukaProxy/src/routers/debug.py`（`_require_reset_role`） | 破壞性端點授權過寬 | 🟠 High | `POST /v1/debug/reset` 收緊為 Owner/admin（排除 Debugger），擋掉 LDH 無認證觸發 |
| **R1** | `ToukaProxy/scripts/manage_backup.py`（`collect_items`） | data 根層 `ui_sessions.db`／`feedback.db` 未納入備份 | 🟠 High | 備份額外納入 data 根層 `*.db`，加回歸測試 |
| **R11** | `ToukaProxy/src/services/agent_reset/primitives.py` | 記憶橋接不可用時 false-clean | 🟡 Medium | 不可用即 raise → `clean=False`，誠實回報 |
| **R7** | `ToukaProxy/src/services/agent_reset/orchestrator.py` | reset 無並行守衛（TOCTOU） | 🟡 Medium | `asyncio.Lock` 序列化；進行中時第二個 reset 直接拒絕 |
| **R3/R4** | `agent_reset/primitives.py`（`RETAINED_STORES`） | 審計表與帳本含 DebugAgent 列未清 | 🟢 Low | 刻意保留審計軌跡，報告透明列出 `retained` |
| **R5** | `agent_reset/primitives.py`（`purge_snapshot`） | `data/snapshots/DebugAgent.md` 未清 | 🟢 Low | 加 snapshot store 清除（經 `is_protected_path` 保護的 `remove_file`） |
| **R9** | `ToukaMemory/src/memory_server/vector.py`（`drop_agent`） | 向量 purge 回傳值低報 | 🟢 Info | 刪除前先 `count()`，回真實點數 |

### 備份與自助資料匯出（軌1／軌2，2026-06-23～24；ADR-0108、ADR-0109、ADR-0110）

BK1–BK3 為備份運維強化，BK4 為下載時即時加密（ADR-0108），BK5 為自動備份排程（ADR-0109），BK6 為使用者自助 GDPR 匯出（ADR-0110）。

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **BK1** | `ToukaProxy/src/routers/system.py`（`run_restore`） | 破壞性還原無密碼確認 | 🟠 High | 改採密碼重輸閘（`get_current_user` 需人類 admin JWT ＋ `verify_password`）；被竊 access token 單獨無法觸發。`verify_origin` CSRF 與 `validate_no_symlink_escape` 維持 |
| **BK2** | `ToukaProxy/src/routers/system.py`（`delete_backup`） | 刪除端點的範圍/路徑安全 | 🟢 Low | admin-only ＋ CSRF ＋ `validate_no_symlink_escape`（擋 `../`、`%2f`）＋ 僅允許 `.zip` ＋ 經 ResourceManager 刪除 |
| **BK3** | `ToukaProxy/src/routers/system.py`（`backup_stats`） | `shutil.disk_usage` 未處理 OSError | 🟡 Medium | admin-only；磁碟不可量測時優雅降級 `free_disk_mb=0` |
| **BK4** | `ToukaProxy/src/routers/system.py`（`download_backup`）＋ `services/backup_crypto.py` | 備份下載會把全系統機密以明文外送 | 🟠 High | 下載時即時加密成 WinZip-AES-256，明文 zip 永不出網；admin ＋ CSRF ＋ 路徑守衛 ＋ passphrase ≥12 ＋ `Semaphore(2)` 限併發；加密中途失敗經 ResourceManager 清除半成品 |
| **BK5** | `ToukaProxy/src/routers/system.py`＋`services/backup_scheduler_service.py` | 排程設定端點與自動備份循環的授權/併發/失敗韌性 | 🟡 Medium | `GET/POST /system/backup/schedule` admin-only ＋ POST CSRF ＋ pydantic 驗證（`extra=forbid`）；備份失敗仍消耗窗口、`backup_execution_lock` 序列化手動與排程備份、`_config_lock` 序列化設定 read-modify-write |
| **BK6** | `ToukaProxy/src/services/gdpr_export_service.py`＋`routers/auth.py`（`export_my_data`）＋ ToukaMemory `tools.export_facts` | 自助 GDPR 匯出：個資範圍化、機密排除、唯讀、加密 | 🟠 High | 身分取自 JWT（僅本人）＋ CSRF ＋ 密碼再驗 ＋ 強制 AES-256（密語 ≥12、串流後即刪、取消亦清）；profile 白名單脫敏；facts 經唯讀 `export_facts` 依 `agent_id` 範圍化；純唯讀 |

> 自動與手動備份在伺服器端皆為明文 zip（僅下載時加密）；排程器為單實例最佳努力，停機或時鐘回跳會略過窗口。
>
> BK6 對抗式複審後的修正：①個人硬碟打包逐檔 `validate_no_symlink_escape`，阻斷 symlink／junction 把 `.env`、`users.db` 打包進匯出；②單檔 100 MiB／總量 1 GiB 上限防 OOM；③淨化 arcname 片段防 zip-slip；④`_collect` 移入 `EXPORT_ENCRYPT_SEMAPHORE`。

### 首次設定精靈上線的安全改動（ADR-0118，2026-07-02）

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **E17** | `ToukaProxy/src/main.py`（CORS 中介層） | 本機/區網 UI 存取遭 CORS 全面封鎖（可用性） | 🟡 Medium | 新增 `allow_origin_regex` 涵蓋 localhost／`127.0.0.1`／`[::1]`／私有區網任意埠 ＋ `vscode-webview://.*`；公網網域仍走顯式 `ALLOWED_ORIGINS` 白名單 |
| **E21** | `ToukaProxy/src/routers/config.py`（`update_env` 500 分支） | 500 回應洩漏原始例外字串 | 🟢 Low | 移除 `error_output=str(e)`，細節只經 `logger.exception` 進伺服器日誌 |
| **EnvSSOT** | `routers/config.py`＋`src/config.py`（`resolve_env_file_path`）＋`docker-compose.yml` | 精靈寫入密鑰的持久化路徑主權 | 🔵 Architecture | 密鑰只寫入 `resolve_env_file_path()` 解析出的 `.env`，移除裸相對 `".env"`；新增最高優先 `TOUKA_ENV_FILE`；絕不烤進映像；`apply_runtime_env()` 即時同步（compose 掛載點後由 ADR-0151 改為穩定的 `/app/host_root/.env`） |

### 認證挾持與無痕可靠性批（ADR-0131，2026-07-05）

根因為認證身分無聲翻轉：過期人類會話被降級成 DebugAgent。

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **L1** | `ToukaProxy/src/services/identity_router.py`（`get_optional_identity`） | LDH「壞卡當無卡」→ 身分挾持 | 🔴 High | 帶失效 token 一律 401 `AUTH_INVALID_TOKEN`、嚴禁降級發卡；LDH 僅保留給真正無卡的本機請求（禁令 60） |
| **L2** | `ToukaChat/src/lib/connections.ts` | 前端無條件採納 `X-New-Auth-Token` | 🔴 High | 僅未登入時採納；搭配送出前 exp 檢查 → single-flight `refreshAccessToken()` |
| **L3** | `ToukaChat/src/hooks/useChatStream.ts`＋`ChatProvider.tsx` | 無痕旗標 wipe 競態 → 無痕訊息以一般模式送出 | 🟠 Medium | 無痕 session id 誕生即登記，發送時旗標＝清單 ∪ 登記；`refreshSessions` 改 merge（ADR-0086） |
| **L4** | `ToukaChat/src/lib/providers/SessionListProvider.tsx`（`syncSession`） | 儲存失敗靜默 | 🟠 Medium | 補 `res.ok` 檢查：失敗 `logSSOTError` ＋ `toast.error`，網路例外同樣可見 |

詳見 [ADR-0131](docs/architecture/adr/0131-auth-renewal-and-ldh-containment.md)。

### 靈魂資料管理步升驗證（ADR-0142，2026-07-10）

靈魂資料的人類面變更（`POST /v1/souls/{id}` 編輯／`DELETE /v1/souls/{id}` 刪除）即使持有有效 admin JWT 也須重新輸入密碼（`AUTH_REAUTH_FAILED` 403＋AUDIT log，不記密碼本身）；agent／assumed-admin 代打結構性無法通過。合法寫入走 `soul_io`（`_safe_target`＋原子寫＋自動備份），直呼 `resource_manager` 寫/刪 soul/legacy 仍被 Soul Protection 擋下；`DebugAgent` 不可經端點刪除。詳見 [ADR-0142](docs/architecture/adr/0142-soul-crud-repair-and-admin-stepup.md)。

### 認證縫三合一批（ADR-0144，2026-07-11）

前置修復：`admin_update_user` 原看鍵不看值，編輯表單送出 `password:""` 會把 admin 密碼覆蓋成 `bcrypt("")`；已由伺服器端 `validate_new_password` ＋「空/缺＝保持不變」修復。同一條縫上的三個缺陷：

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **A1** | `auth_service.py`＋`user_service.py`＋`identity_service.py`＋`routers/auth.py`＋`models/user.py` | 改密碼不撤 access token | 🔴 High | access token 加 `iat`；密碼變更於 `_atomic_users_write` 鎖內蓋 `User.password_changed_at`；`access_token_is_stale` 掛上兩道 Bearer 閘（無 iat 舊 token 在戳記已設時 fail-closed；同秒放行） |
| **A2** | `ToukaChat/src/lib/connections.ts`＋`contexts/AuthContext.tsx` | refresh 判死後前端死循環 | 🟠 Medium | `refreshAccessToken` 對 401/403 清卡並導回登入；網路錯誤/5xx 保留卡；帶模組卡被判 401 → single-flight 續期一次重送一次 |
| **A3** | `identity_router.py`＋`auth_service.py`；`ToukaChat/src/lib/connections.ts` | LDH 跨面發卡（殭屍身分擋登入） | 🔴 High | 後端：LDH 僅限系統未認領（`any_claimed_user()`）；前端：`touka_has_logged_in` 持久旗標，曾登入瀏覽器一律不採納 `X-New-Auth-Token` |

> 刻意代價：iat 秒級解析度留 ≤1s 同秒窗；已認領系統上依賴 LDH 的本機腳本需改用正規憑證。詳見 [ADR-0144](docs/architecture/adr/0144-auth-seam-hardening.md)。

### 權限表示法與設定生命週期收斂（ADR-0196，2026-07-27）

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **PRC-P2/P4/P5** | `permission_grants.py`、`permission_service.py`、`permission_defaults.py`、`routers/observability.py` | 多入口各自解析 wildcard／Owner 旁路造成授權分歧；角色 deny 沿繼承誤傳；損壞設定可能抹除明示 deny | 🟠 High | `resolve_grants()` 統一工具／技能／MCP，移除 Owner 硬編；角色 deny self-only＋全域地板約束 Owner；fallback 僅 Owner 修復通道並納 Guard #32。啟動期壞設定 fail-fast；熱重載失敗保留 last-known-good、記 `AUDIT` 並回報 degraded。詳見 [ADR-0196](docs/architecture/adr/0196-permission-representation-convergence.md) |

### ToukaIDE MCP 2026 遷移安全修復（ADR-0202，2026-07-30）

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **IDE-P4B-1** | `ToukaIDE/src/utils/authMiddleware.ts`、`mcp/httpBridge.ts` | 空 Bearer 使 MCP endpoint 退化為無認證；跨 Origin 本機請求缺協定層限制 | 🟠 High | 空 server token 回 503 fail-closed；每個 request 統一 Bearer middleware＋constant-time 比對，只綁 `127.0.0.1` 並驗 localhost Host／Origin |
| **IDE-P4B-2** | `ToukaIDE/src/mcp/handlers/FileHandlers.ts`、`mcp/workspacePath.ts` | MCP 檔案工具接受工作區外絕對路徑與 traversal；symlink/junction 可逃逸 | 🟠 High | 先做 workspace lexical jail，再以 nearest-existing-ancestor realpath 驗證；外部絕對路徑、`..`、工作區外 symlink/junction 與無 workspace 全部 fail-closed |
| **IDE-P4B-3** | `ToukaProxy/src/protocols/ide/discovery.py`、`handler.py` | discovery 可攜任意 endpoint；handler 手刻未協商 JSON-RPC | 🟡 Medium | discovery 只接受 loopback base 下的絕對 path、合法 port／mode／非空 token；handler 改走共用 v2 conservative client，關閉時保證 teardown |
| **MCP-P5-1** | `protocols/mcp/catalog.py`、`sdk_adapter.py` | 惡意 resource URI 被誤當本機路徑；超大／偽造 MIME、base64 或 prompt payload | 🟠 High | URI 僅作不可信識別字，拒絕 `file:`／相對／Windows path；MIME、encoding、page/item/bytes/base64 全面限額；文字經 DLP＋prompt-injection 標記，prompt 永遠 untrusted |
| **MCP-P5-2** | `protocols/mcp/schema_validation.py` | 外部 `$ref` SSRF；深層或大量 subschema 耗盡資源 | 🟠 High | 預設 Draft 2020-12；只允許 internal `$ref`／`$dynamicRef`；驗證前 bytes/depth/subschema 上限並施加 validation-time budget，不合法 tool 隔離且不廣告 |
| **MCP-P5-3** | `routers/mcp.py`、`services/permission_service.py` | 管理頁權限者繞過角色的 MCP server grant 讀取第三方 resource／prompt | 🟠 High | Resources／Prompts data plane 除 Owner／Debugger／admin 閘外再查 `is_mcp_server_allowed()`；read/get POST 要求 CSRF Origin |

> 遷移當時以 `npm audit fix` 加 esbuild 0.28.1 將 ToukaIDE advisory 11→2；剩餘為 `@modelcontextprotocol/node@2.0.0` 鎖住、無相容修法的 moderate `@hono/node-server` advisory，未使用 `--force` 或跨主版 override。

### MCP 互動式 OAuth 安全邊界（ADR-0206，2026-08-01）

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **MCP-P8A-1** | `protocols/mcp/oauth/store.py`、`keyring.py`、`service.py` | token/state 明文持久化、跨 principal replay、callback 重放 | 🔴 High | AES-256-GCM envelope＋AAD、HMAC(state) lookup、stable slot/full grant tuple、transaction/grant version CAS、refresh/DCR lease；raw state 在 token exchange 前原子單次消耗 |
| **MCP-P8A-2** | `oauth/outbound.py`、`http_client.py`、`client.py` | metadata/token/DCR/revocation/bearer SSRF、DNS rebinding、redirect 或 cleartext credential egress | 🔴 High | 專用 HTTPS-only `httpx2/httpcore2` transport；每次 connection 驗全部 A/AAAA 並 IP-pin，保留 hostname SNI/certificate/Host；禁 redirect、env proxy、HTTP/2 coalescing；私網例外需 exact installation＋origin＋CIDR |
| **MCP-P8A-3** | `routers/mcp_oauth.py`、ToukaChat `useMcpOAuth` | callback code/state 經 log、history、storage 或 postMessage 外洩 | 🔴 High | callback 精確綁 route/method/response mode 且早期限流；nginx `access_log off`；HTML `no-store`/`no-referrer`/nonce CSP＋`history.replaceState`，只向 exact opener origin 傳 opaque status；token 永不進 browser |
| **MCP-P8A-4** | `oauth/profile.py`、`rotation.py`、`routers/system.py`、`manage_backup.py` | stale profile/key worker egress、備份外洩或 restore 覆寫現有 credential | 🔴 High | per-profile mutation/egress fence＋persisted updating intent；managed key rotation FSM；一般備份排除 OAuth DB/WAL/SHM 與 key env；restore collision 回 `MCP_OAUTH_RESTORE_CONFLICT`，不自動覆寫 |

保證範圍限定於 Touka 可控制處：raw state/code/token 不留在 Touka storage、log、trace、API、popup message、browser storage 或 callback 當前 history entry。授權伺服器／瀏覽器管理的跨來源 history 不在控制範圍，以高熵短 TTL、單次 CAS 與立即清理降低殘餘風險。

### MCP workload OAuth 安全邊界（ADR-0207，2026-08-03）

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **MCP-P8B-1** | `oauth/workload/topology.py`、`permit.py` | 多 worker/process 無法共享 in-memory permit，撤權誤把 TTL 當完成 | 🔴 High | 啟用需 local filesystem＋lifespan exclusive lock，第二 process/remote/unknown fail-start；permit 綁 boot/authority epoch/task/ticket/payload，完成須等 send task exit/cancel acknowledge |
| **MCP-P8B-2** | `workload/store.py`、`profile.py`、`delegation.py` | client secret／token endpoint 外洩或被竄改、JSON/SQLite split-brain、舊 generation authority 重用 | 🔴 High | credential 與 metadata snapshot 以 AES-256-GCM＋binding AAD 保存；generation CAS、SQLite digest mirror 與 transition/recovery fence；indeterminate state 不自動修成 active |
| **MCP-P8B-3** | `authority*.py`、`manager.py`、`runtime.py` | foreground/dispatch 誤用 workload credential、tool/args/authority mix-up | 🔴 High | server-derived HEARTBEAT execution kind＋origin 雙重檢查；request-aware HTTP auth 驗實際 JSON-RPC body，tool ticket 單次使用；permit issue 後、begin-send 前再重驗 policy |
| **MCP-P8B-4** | `delegated_authority.py`、P8-A `runtime.py`、`control.py` | owner delegation 複製 token 或 source grant 撤銷後仍可送出 | 🔴 High | delegation 只引用 server-derived exact P8-A active grant，不落 bearer；source disconnect 先 tombstone、cancel/drain 衍生 delegation，durable finalize 前拒絕斷開 source |
| **MCP-P8B-5** | `audit.py`、`exchange.py`、`routers/mcp_oauth_workload.py` | credential egress 無稽核、secret 進 API/UI/backup | 🔴 High | token exchange、delegated refresh、transport bootstrap 與 final MCP bearer 各有 durable audit＋獨立 permit；API 只回 minimal status，前端 secret one-shot；備份排除 workload DB 與 key env |
| **MCP-P8B-6** | `mutation.py`、`control.py`、`rotation.py`、`store.py` | mutation lock 分散、epoch advance 後誤 reopen、restart blocker 未進 admission | 🔴 High | 單一 coordinator；fence 先 claim durable operation ID＋kind；commit 後直到 durable finalize＋task drain 才 complete；permit issue/begin-send 皆讀 `runtime_authority_ready()`，任一 blocker 均 fail-closed |
| **MCP-P8B-7** | `oauth/models.py`、`service.py`、`workload/exchange.py`、`runtime.py` | permit 綁 tool args 而非 OAuth request、ticket replay set 無界 | 🔴 High | 先建立 immutable final OAuth `POST` request/binding，以 secret-free digest 做 audit/permit/begin-send；consumed ticket index 有固定上限與 expiry heap |
| **MCP-P8B-8** | `src/config.py`、`src/core/bootstrap.py`、`tests/conftest.py` | 測試 collection 觸發 import-time production bootstrap 寫入 | 🔴 High | `TOUKA_BOOTSTRAP_ENABLED=false` 阻止 import-time bootstrap；tracked config examples 只 hydration 到 session temp，既有目標不覆寫（ADR-0209） |
| **MCP-P8B-9** | `ToukaChat/.npmrc`、`package*.json`、`src/app/layout.tsx` | mutable 依賴、implicit lifecycle、npm env/proxy override 與 build-time Google Font 外連 | 🔴 High | lock 限 official registry＋SHA-512；install 一律 ignore-scripts；advisory package 採 exact safe pin；display font 改 system stack；npm offline gate 拒絕 inherited override |
| **MCP-P8B-10** | `oauth/models.py`、`service.py`、`outbound.py`、`workload/bootstrap.py` | delegated refresh permit／HTTP wire split-brain、暫時性 audit sink 錯誤永久殺死 maintenance task | 🔴 High | Basic／Post refresh 先建立同一個 frozen、secret-masked prepared request，permit 與 outbound 共用；audit maintenance 對一般 sink 例外只記 type 並固定間隔重試，cancellation 仍傳播 |

Enterprise/SSO、`private_key_jwt`、DPoP、JWT bearer、foreground 與 dispatch workload authority 目前未支援且不會降級。實際啟用受單一 Proxy process、local filesystem、feature flags 與 admin/owner gates 約束；single-process lock 為本機 file lock，不是 distributed lease。

### 子程序執行加固（2026-10-09，ADR-0239 後續；ADR-0137 補記）

ADR-0239 唯讀盤點找到的子程序缺口。每項先有失敗測試，回歸測試集中在 `tests/unit/test_subprocess_hardening_security.py`；子程序一律經 `services/execution_broker.py`。SP3（`/mcp/scan`）由 Owner 決定直接移除，回歸測試在 `tests/unit/test_mcp_scan_removed_security.py`。

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **SP1** | `services/review/evidence.py`、`review_loop.py`、`tools/mission_tools.py` | 互審 `verification_cmd`（agent 撰寫、Auto／YOLO〔當時的 SafeAuto／FullYOLO〕下不經人類審批）繼承完整 Proxy 環境＝繞過 DA6；無行程群組、取消不殺子程序、無逐指令政策；vault 不可用時退回 Proxy 行程 cwd | 🔴 High | 改用 `get_clean_env()`；經 `spawn_exec(..., new_process_group=True)` 以新行程群組啟動，逾時與取消皆 `terminate_process_tree`；`collect_evidence` 必帶 `command_gate`，正式路徑 `policy_command_gate` 沿用終端政策（ALLOW 執行、CONFIRM 需人工核准（2026-10-12 起改為逐條跳卡，見下方 B2）、DENY／未知拒絕；未帶 auth_level＝Manual 無人核准；2026-10-12 起 Auto 的 REVIEW 先問審查員、底線 HUMAN_ONLY 只有人能核准，見下方 FL1、FL5）；vault 不可用直接回錯誤 |
| **SP2** | `tools/terminal.py`（`execute_command`、`_execute_core`） | 政策只會回 `DENY`，程式卻只擋 `BLOCK`——繞過 Gate 2 的直接呼叫端會把 DENY 指令當安全指令立即執行；取消時只註銷不殺子程序 | 🟡 Medium | 只有 ALLOW／CONFIRM 可往下走，其餘一律 `permission_denied`；`CancelledError` 時連坐殺行程樹後再拋出。指令分類（含 C5 升級）收斂為 `permission_service.classify_command`，terminal 與互審共用 |
| **SP3** | `routers/mcp.py`（`POST /mcp/scan`）、Hub 與演化安裝視窗 | 以 `npx -y mcp-scan@latest` 每次執行未釘版、單人維護的第三方套件；子程序繼承完整 Proxy 環境（`TOUKA_AUTH_SECRET`、`DISPATCH_TOKEN`、provider 金鑰），使用者剛輸入的 MCP env 值以明文寫入暫存檔，逾時或失敗時不刪；同步執行阻塞事件迴圈最多 30 秒。該套件只掃本機 AI 工具設定、不讀傳入的檔案，一般掃描也不以 exit code 表示發現問題，「通過」沒有意義 | 🟠 High | 移除端點、Hub 與演化安裝視窗的掃描按鈕、翻譯 key 與 OpenAPI 路徑；安裝前改由視窗中的完整指令／端點供人工審核，Owner／admin 閘與 `is_command_safe` 不變 |
| **SP4** | `routers/system.py`（還原背景任務） | 用 PATH 上的 `python`（Windows 會落到全域直譯器）且無逾時 | 🟢 Low | 抽成 `_execute_restore`：經 `run_sync` 以 `sys.executable` 執行，失敗記錄、不結束進程；硬逾時已由 ADR-0241 移除（還原逐檔就地覆寫，中途殺掉會半新半舊，卡住時重啟服務） |
| **SP5** | `services/agent_reset/orchestrator.py` | reset 前置備份在事件迴圈內同步執行、未取 `backup_execution_lock`，可與手動／排程備份併發 | 🟢 Low | `system_backup.run_backup_job`：獨立 task 持 `backup_execution_lock`、在執行緒備份、呼叫端以 shield 等待，取消時鎖持有到備份真的結束；手動／匯入前／排程備份共用（ADR-0241） |
| **SP6** | `services/evolution_security.py`、`tools/builtin/evolution.py` | 呼叫端 `env` 合併在淨化結果之後，可把密鑰／`LD_PRELOAD` 加回來；venv 建立與 pip install 無逾時（POSIX 也就沒有 `RLIMIT_CPU`）；三個沙箱子程序（venv、pip、`verify_mcp_locally`）都在 async 工具內同步阻塞事件迴圈 | 🟡 Medium | 呼叫端 env 過同一道 `strip_sensitive_env`（黑名單模式＋顯式移除清單，禁令 19）；venv 120 秒、pip 600 秒逾時；三者皆移入 `asyncio.to_thread` |
| **SP7** | `protocols/mcp/environment.py`（`LaunchEnvironmentPolicy.clean`） | `clean` 不解析 env 哨兵、`production` 會解析 | ⚪ Info | 確認為 ADR-0198 §5 的刻意設計：Creation 候選 server 只拿 spec 明示值，不得經哨兵取得 app_data 根路徑。以測試釘住，未改程式。2026-10-15 起（ADR-0254）啟動環境的底改成白名單：正式安裝的 MCP stdio 伺服器與創建引擎試跑共用模組常數 `LAUNCH_ENV_ALLOWLIST`，`clean` 不再收淨化 base（不再用 `get_clean_env`），試跑的候選伺服器不會拿到比正式伺服器寬的環境；哨兵規則不變（見 2026-09～10 產品修復表「MCP stdio 伺服器繼承全部金鑰」） |
| **SP8** | `AGENTS.md` 禁令 42、本檔 DA6 | 文件漂移：禁令 42 宣稱 `lint_ssot.py` 攔 `subprocess.run`，實際只有 ruff TID251（Guard #44）；DA6 仍描述已不存在的 3 個 `subprocess.run` | ⚪ Info | 禁令 42 已由 ADR-0239 改寫為 broker／TID251／ratchet 分工；DA6 改述現況 |

### 互審驗證指令逐條審批與審批卡完整指令（2026-10-12，ADR-0137 補記）

修掉上一節 SP1 留下的已知殘留（核准 `request_review` 時卡上看不到 `verification_cmd`）。編號沿用 PR #72 複審清單（B＝審核問題、A＝白名單複審），與上方 A1–A25 不同表。每項先有失敗測試。

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **B2** | `services/review/evidence.py`、`review_loop.py`、`tools/mission_tools.py`、新 `services/approval_flow.py` | Manual 互動會話核准 `request_review` 卡（卡上只有 `task_id`／`producer_claims`）後，`_hitl_preapproved` 透傳到 `policy_command_gate`，任務板上 CONFIRM 級的 `verification_cmd` 直接執行——人沒在卡上看過那條指令；initialize 卡的 `tasks` 也被截斷，看不到後面任務的指令 | 🟠 High | `policy_command_gate` 改 async、不再接受預核准旗標，改收可選 `approver`：CONFIRM 時有人在場就為**這一條指令**跳一張卡（每條自己的步驟 id `<request_review id>:verify:<n>`，卡上是完整指令），核准才執行；拒絕／逾時不執行；無人在場與子代理（子 loop 收不到卡）不跳卡、不執行；核准後審查中途失敗時該步驟以 `internal_error` 收尾。審批流程抽成 `approval_flow`，`ToolExecutor._await_human_approval` 與互審共用。測試：`tests/unit/test_review_command_approval_security.py` |
| **B1** | `services/review/evidence.py`、`review_loop.py`、`tools/mission_tools.py`、`services/approval_flow.py` | 無人在場（背景心跳、子代理），或有人在場但指令卡逾時／被拒時，CONFIRM 級 `verification_cmd` 沒跑，這一輪照樣送 critic、扣 `review_attempts_total`，扣到上限就永鎖——人沒核准也被鎖死；無人在場時回給代理人的訊息還叫它改用 `execute_command` 自己跑、把輸出貼進 `test_log`（以自填證據取代系統蒐證） | 🟡 Medium | `CommandApprover` 改回審批結果（不再只回 bool），政策閘依結果回固定理由（無人在場 `AWAITING_HUMAN_APPROVAL`、逾時／佇列滿 `NO_HUMAN_DECISION`、被拒 `REJECTED_BY_HUMAN`）；`run_review_round` 見到任一條就把本輪標 `BLOCKED_FOR_HUMAN`，不呼叫 critic、不扣次數、不上鎖、不佔 mission 子 loop 名額；`request_review` 回結構化 `permission_denied`，`review_locked=false`：無人在場與逾時 `reason=human_approval_required`（請等人回來後在對話裡重跑，重跑走逐條跳卡），被拒 `reason=human_rejected_command`（請先問人類怎麼驗證）。往返達上限的永鎖仍回一般審查結果。測試：`tests/unit/test_review_awaiting_human_security.py` |
| **A7** | `services/approval_flow.py`（原 `tool_executor._approval_args_preview`）、`ToukaChat` `ToolApprovalCard.tsx` | 審批卡看不到完整指令：參數預覽把每個字串截到 200 字，前端再單行省略 | 🟡 Medium | `command` 完整上卡，超過 8 KB（模組常數 `APPROVAL_COMMAND_PREVIEW_LIMIT`）才截斷並附 `…[truncated: N more chars not shown]`；其他大載荷仍截到 200 字。前端改換行顯示（`whitespace-pre-wrap break-all`）。測試：`tests/unit/test_approval_card_full_command_security.py`、`ToolApprovalCard.test.tsx` |

### Auto 與地板：三種模式、硬地板與 Auto 審查員（2026-10-12，ADR-0246）

模型層 2c 段。每項先有失敗測試，各列附回歸測試檔；FL1–FL2 的回歸測試在 `tests/unit/test_hard_floor_security.py`。

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **FL1** | `models/safety.py`、新 `utils/hard_floor.py`、`services/tool_executor.py`、`services/approval_flow.py` | 刪改資料、讀金鑰、改權限設定、改原始碼、系統破壞全靠設定檔黑名單：黑名單錨在開頭（`cd x; shutdown` 就能繞過），不認 `rm -fr`、`Remove-Item -Recurse`、`docker compose`；FullYOLO 下其他一律放行；設定檔本身也能被改寫 | 🟠 High | 程式常數地板在 `evaluate` 最前面判斷、設定檔關不掉；F1–F4、F7 三種模式都只有人能核准（新效果 `HUMAN_ONLY`：有人在場跳卡，無人在場拒絕；terminal 只認人類審批卡核准後的可信 `_hitl_preapproved`，互審逐條問人，審查員核准不能代替），黑名單與 ReadOnly 的 DENY 照舊優先；F5、F6 先回類別 |
| **FL2** | `services/permission_service.py`（`_load_policies`） | `auth_policies.json` 熱重載解析失敗時整份換成預設，黑名單變空——代理人寫壞規則檔，2 秒內黑名單就消失 | 🟡 Medium | 解析或驗證失敗時保留上一份可用的黑名單，白名單與風險表退回最嚴的預設；啟動時沒有上一份才用預設 |
| **FL3** | `middleware/layers/monitor.py`、`middleware/layers/tool.py`、`routers/ui_sessions.py`、新 `utils/auth_mode.py` | 「只有 admin 或擁有者能開 SafeAuto／FullYOLO」的降級閘在 MonitorLayer，結果會被 ToolLayer 用 session 的值覆寫、實際無效；session 寫入只驗能不能存取對話，不驗能不能開自動模式 | 🟡 Medium | 模式授權移到 session 寫入：存檔後是 Auto／YOLO 的對話只有 admin 或該靈魂的擁有者能寫，其他人存成 Manual；看存檔後的模式與靈魂（改掛靈魂、前擁有者沒帶 auth_level 存檔也重驗）；請求參數不再是模式來源；舊值讀取時對照（ReadOnly、SafeAuto、看不懂→Manual，FullYOLO→YOLO），寫入只收新名稱。測試：`tests/unit/test_auth_mode_authority_security.py`、`tests/unit/test_auth_mode_three_modes.py`、`tests/unit/test_auth_mode_not_wider_security.py` |
| **FL4** | 新 `utils/presence.py`、新 `utils/user_intent.py`、`tools/subagent_tools.py`、`routers/heartbeat.py`、`protocols/manager.py` | `heartbeat_run`／`dispatch_run` 從來沒被設定，「有沒有人在場」只能靠 `resumable_stream`；互動派生的子代理遇到要核准的工具會跳一張送不到人的卡、等 180 秒逾時；心跳的 MCP 呼叫被標成前景 | 🟡 Medium | 心跳 `/awaken`、`/retrigger` 設 `heartbeat_run`，子代理設 `subagent_run`＋`dispatch_run`；`is_human_present` 只看伺服器設的標記；子代理一律無人在場、立刻拒絕、MCP 互動直接 decline；修正分類後 ADR-0207 的 workload OAuth 第一次走得到，以預設關閉的常數維持原本不連線。測試：`tests/unit/test_user_intent_presence_security.py` |
| **FL5** | 新 `services/auto_reviewer.py`、`services/tool_executor.py`、`tools/terminal.py`、`tools/execution_context.py`、`services/review/evidence.py` | 新增「模型代替人放行」的攻擊面：提示注入、偽造核准欄位、審查失敗時放行、審查員推翻底線 | 🔵 Architecture | 審查員只經 `complete_once`、只送到所選模型的廠商、不重試；輸入只有原意／完整參數／環境三個 JSON 跳脫區塊；只收 `{"verdict","reason"}` 型別化判決，任何失敗都轉人或拒絕；核准只以可信 `_approved_by="auto_reviewer"` 傳遞、只滿足 `REVIEW`，模型自帶的欄位被可信情境覆寫；底線與 F5、F6 回 `HUMAN_ONLY`、審查員不會被呼叫；無人在場不送審；同一對話被擋連續 3 次或累計 20 次後一律轉人（P7）；稽核不記使用者訊息與模型理由（禁令 64）。測試：`tests/unit/test_auto_reviewer_security.py`、`tests/unit/test_auto_review_execution_security.py` |
| **FL6** | `routers/heartbeat.py`、`middleware/layers/tool.py` | 心跳 `security_level` 看不懂的值退回 SafeAuto；KF-4 讓心跳跑 ToolLayer 之後，家對話若照一般路徑讀 session row，會把人類替前景對話選的模式套到無人的排程 run | 🟡 Medium | safe→Auto（無人在場不跑終端與高風險工具、不叫審查員）、full→YOLO（底線仍擋）、看不懂→Manual；心跳只採排程設定時決定的模式、不採 session row，也不跑 MonitorLayer。測試：`tests/unit/test_heartbeat_modes_security.py`、`tests/unit/test_heartbeat_awaken_hardening_security.py` |
| **FL7** | ToukaIDE `mcp/authPolicy.ts`、`mcp/handlers/TerminalHandlers.ts`、新 `mcp/humanApproval.ts`；Proxy 新 `utils/ide_human_approval.py` | IDE 的 SafeAuto 用前綴清單自動放行（`cat `、`ls`、`npm install`…），不看串接，`cat a; curl … \| sh` 會直接跑；黑名單只跳確認；核准的指令先在 VS Code 終端跑一次、再 `spawn` 跑一次 | 🟡 Medium | IDE 只剩 Manual／YOLO、黑名單兩種模式都拒絕；人類在 Touka 卡片核准過的那一次呼叫才帶「由人核准」標記（HMAC-SHA256、金鑰＝IDE session bearer、綁工具名與完整參數、60 秒、nonce 只用一次），審查員與 YOLO 放行不帶；核准的指令只跑一次。測試：`tests/unit/test_ide_human_approval_security.py`、`ToukaIDE/src/__tests__/humanApproval.test.ts`、`mcpServerApproval.test.ts`、`authPolicy.test.ts`、`terminalHandlers.test.ts` |
| **FL8** | `utils/auth_mode.py`（`enforce_incognito_manual`）、`middleware/layers/tool.py`、`tools/subagent_tools.py`（`_run_subagent`）、ToukaChat `ChatProvider` | 無痕是 Manual 只是「不存檔、讀不到 session row」的副作用：無痕請求帶著已存成 Auto／YOLO 的 session id 時，會照存的模式跑；無痕不留紀錄，事後無法追溯代理人做了什麼 | 🟡 Medium | 人類 10/12 12:44 決定無痕一律 Manual（收緊）：純函式 `enforce_incognito_manual` 由 ToolLayer 在所有模式來源（存檔、心跳、子代理繼承）之後最後套一次，子代理執行 ctx 在 `_run_subagent` 也套一次；無痕不進存檔端點，原因提示在前端（選單鎖定說明＋要求 Auto／YOLO 時不存檔、toast 說明）。測試：`tests/unit/test_incognito_manual_only_security.py` |
| **FL9** | `routers/autonomy.py`（`POST /v1/autonomy`） | 審查員設定（`auto_review.model` 決定 Auto 的資料送到哪家廠商、`headless_terminal` 讓所有靈魂無人在場的終端指令改成送審）用 `require_privileged`：非 admin 擁有者帶 X-Agent-ID 切成自己的靈魂、靈魂本身、Debugger 都能改，模型也不檢查是否在清單內 | 🟡 Medium | `auto_review` 有變更時另外過 `require_admin_human`（admin 代打不算），其他預算欄位照舊；新設定的模型必須空白或在 reviewer-models 清單內，否則 422；`headless_terminal` 預設關、打開會變寬，需人類決定（R-2c）。測試：`tests/unit/test_autonomy_reviewer_authority_security.py`、`tests/unit/test_auth_mode_not_wider_security.py` |
| **FL10** | `routers/heartbeat.py`（`POST /v1/heartbeat/retrigger`） | 重跑一律以 Auto 執行，但 `is_owner` 對 Debugger 一律放行，也不檢查對話屬於誰：Debugger 能讓任何靈魂以 Auto 跑；擁有靈魂 A 的人拿到別人的 session id，就能讓 A 在那個對話跑（讀到別人的對話、拿別人最後一則訊息當原意、存檔時把對話改掛到 A） | 🟡 Medium | 只在這條路徑收緊、`is_owner` 不改：過 session 寫入同一個模式授權（只有 admin 或人類擁有者）、對話的 `agent_id` 必須是這個靈魂、呼叫者要通過 `_can_access_session`（R-2c）。測試：`tests/integration/test_heartbeat_retrigger.py` |
| **FL11** | `services/sns_gateway.py`、`utils/user_intent.py`、`tools/subagent_tools.py` | SNS home 模式把外人訊息存成普通 user 節點，人類之後在家對話用 Auto 時會被當成人類最近的話送審查員，外人可替代理人授權；前端內嵌的文字附件也一樣；子代理把父代理寫的派工目標當原意送審查員 | 🟡 Medium | SNS 訊息存檔與送出都加固定開頭 `【SNS 外部訊息】`，原意擷取時排除；附件從第一個標記起整段排除；子代理只帶父代理的人類原意（R-2c）。測試：`tests/unit/test_user_intent_presence_security.py`、`tests/unit/test_sns_gateway.py`、`tests/unit/test_auto_reviewer_security.py` |
| **FL12** | `routers/proxy.py`、`middleware/layers/tool.py`、`utils/auth_mode.py` | 無痕強制只看 `request.incognito`：`ui_persistence.mode="incognito"` 但 `incognito=false` 的請求會照存檔的 Auto／YOLO 跑（HTTP 請求會被 schema 擋下，只有內部組出的請求碰得到） | 🔵 Low | 純函式 `request_is_incognito`：兩欄任一是無痕就算無痕，proxy 與 ToolLayer 都看（R-2c）。測試：`tests/unit/test_incognito_manual_only_security.py` |
| **FL13** | `utils/hard_floor.py`、`models/safety.py` | F1、F2、F4 只看路徑裡的目錄名，不認受保護目錄的上層：預設 compose 下 `/app/workspace` 是 repo 根（含 data、`.env`、原始碼），對它或 `/app/ToukaProxy`、`/app/host_root` 這類上層刪除、搬移、覆寫、`git -C`、遞迴讀取，YOLO 下直接放行 | 🟠 High | 動態算出受保護根目錄（資料、repo 根、`.env` 所在目錄、設定目錄），兩句規則：甲，寫入類指令（含 `git -C`、`tar -C`、解壓縮、`find -delete`／`-exec`）的目標是它們本身、底下或上層就是底線（資料 F1、其他 F4），遞迴讀放金鑰的目錄或它的上層算 F2；乙，沒有 Touka 的使用者工作區（`resource_manager.get_workspace_mount()`），本身和上層只擋刪除、搬走或清空（`{ws}/*`、`{ws}/**`、`*.*`、`rsync --delete`、`robocopy /MIR`／`/PURGE`、`git clean`、直譯器的 `rmtree`／`rmSync`／`rmdirSync`／`remove_tree`／`rimraf` 類，以整句含管線判斷；出現刪除呼叫就算寫入）。不在 Docker 時兩邊先 resolve（別名工作區換成真實位置，resolve 失敗就整片保護），重疊就交給甲；Docker 掛載點只有 `/proc/self/mountinfo` 證明工作區與它底下每個子掛載都和錨點分開才豁免「在 repo 根底下」（同裝置 root 不分大小寫比上下層；不同裝置只在兩邊都是實體區塊裝置 major≠0 時假設不重疊，major 0 的 9p／virtiofs／FUSE／overlay 證明不了；唯一例外是錨點為實體區塊裝置上的 Docker named volume〔root 符合 `…/volumes/<名稱>/_data`〕、工作區在另一個裝置，算分開，即 ghcr compose 跑在 Docker Desktop 的設定目錄；理由只在 Docker Desktop 成立〔主機共享看不到 VM 磁碟〕，Linux 原生上靠其他 bind 錨點仍要各自證明分開，每個錨點都要各自證明），證明不了就當成受保護根目錄的上層（R-2c，第 5～6 輪；U2c-R1c）。測試：`tests/unit/test_hard_floor_roots_security.py`、`tests/unit/test_floor_executor_wiring_security.py` |
| **FL14** | `utils/hard_floor.py`、`utils/command_safety.py`、`services/tool_executor.py`、`services/approval_flow.py`、`tools/terminal.py` | F7 被繞：前綴指令只去掉單字、沒去掉參數（`nice`、`timeout`、`env` 等）；`find` 的刪除旗標、經管線交給刪除指令、PowerShell 不寫 `-Command` 直接接指令都漏判；F5 是否成立取決於設定檔有沒有 `action_param`；告訴審查員的 shell 和實際不同；Windows 路徑寫法能讓 F2 失效；IDE 標記在卡片截斷參數時照樣產生 | 🟡 Medium | 已知前綴（sudo／doas／env／nice／ionice／stdbuf／timeout／time／xargs／nohup／exec／command／busybox／setsid／chrt／taskset）連同參數一起跳過；`find -delete`／`-exec rm` 算刪除；`A \| xargs rm`／`Remove-Item` 沿用 A 的目標；powershell／pwsh 沒帶 `-File` 時整段當指令判斷；F5 動詞從 `args["action"]` 取；審查員與執行共用 `effective_shell`（非 Windows 一律 bash）；Windows 路徑去掉 `\\?\`／`\\.\` 前綴、資料串流與結尾的點和空白；審批卡沒截斷任何參數才產生 IDE 標記（R-2c）。測試：`tests/unit/test_hard_floor_roots_security.py`、`tests/unit/test_floor_executor_wiring_security.py` |
| **FL15** | `middleware/layers/tool.py`、`utils/auth_mode.py`、ToukaChat `ChatProvider` | 存下的 Auto／YOLO 不看綁的是哪個 agent：照自己的靈魂 A 存下 Auto 的空對話，第一句前換成另一個認得的 agent B（或自己組請求點名 B），這一輪照 Auto 跑 B；存檔時才重驗降回 Manual | 🔵 Low | ToolLayer 用正式 ID 比對 session 綁的 agent 與這次執行的 agent（前景代打後的 ctx），一致才採用 Auto／YOLO，不一致或沒綁一律 Manual；心跳、子代理、無痕的覆蓋順序不變；前端送出第一句前先重綁空對話並顯示後端存下的模式（R-2c）。測試：`tests/unit/test_auth_mode_authority_security.py`、ToukaChat `ChatProvider.authLevel.test.tsx` |

> 已知限制：
> - shell 文字地板只能盡力而為：先寫檔再執行（先寫腳本、再 `python x.py`）、shell 變數（`$X`、`$env:`）與萬用字元（`.e*v`）、編碼、字串拼接、`cd` 後的相對路徑都能繞過；檔案工具的路徑不經地板（靠 `is_protected_path` 與 vault 監獄）。規則檔被清空或刪掉時仍退回預設。根治要等隔離執行環境（第 5 段）。
> - FL13／FL14 之後仍偏嚴的誤判（只是多問人）：用絕對路徑寫進 repo 底下（mountinfo 證明不重疊的 Docker 工作區掛載點除外，例如 `python /app/ToukaProxy/scripts/x.py`）、寫進 `/app/hf_cache` 這類程式裡沒有 SSOT 的快取掛載點（仍算 F4）、Docker 讀不到 mountinfo 時掛載點整片保護、`/Projects` 和 `/projects` 被當成同一個、Docker Desktop 上工作區和 repo 在不同磁碟機時寫入專案要人核准、btrfs 子卷、ZFS dataset 上的工作區或錨點會判成證明不了、整片保護（推論：它們在 mountinfo 是 major 0 的匿名裝置號，屬偏嚴、不是漏洞）、不在 Docker 時把工作區設在 Touka 資料夾裡面（整片保護，甲的直接結果）、程式不在段首（`sudo /venv/python`）、`find . -type f -delete` 沒有檔名篩選、`Copy-Item -Force` 或 `grep -a` 加受保護目錄。仍抓不到：WSL 的 `/mnt/x` 路徑、8.3 短檔名、自訂 `HEARTBEAT_CONFIG_DIR`、不在表上的帶值前綴選項、直接執行受保護目錄裡的腳本；不在 Docker 時，第一次非同步讀取 DB 之前工作區用的是 `HOST_WORKSPACE_ROOT`；「不同裝置不重疊」只限兩顆實體區塊裝置；mountinfo 只讀一次、重新掛載要重啟；先 `cd` 再用相對路徑清空工作區；單獨 `/` 的覆寫（例如 `cp -r x/. /`）；直譯器刪除呼叫只靠字詞比對，混淆寫法抓不到；掛載點裡原本就有、指向 Touka 的連結看不到；工作區本身是 Docker 資料區的 FUSE／NFS 視圖這種極端設定抓不到；例外只看 mountinfo root 字樣，使用者自己的 bind 來源剛好叫 `…/volumes/<x>/_data` 時也會被當成 volume，前提一樣極端（U2c-R1c）。
> - `/v1/debug/execute` 在 YOLO 下也拒絕地板指令（比原本嚴）。
> - 審查員的準確率、拒答率、解析失敗率與延遲都沒有實測（禁令 61），只用假的 Completer 驗證。
> - workload OAuth 刻意維持關閉（`HEARTBEAT_WORKLOAD_OAUTH_ENABLED = False`），要不要打開待人類決定。
> - 無痕對話一律 Manual（人類已決定）：無痕不能用 Auto／YOLO，因此 ④ 的資料外送不涵蓋無痕。
> - IDE 的「由人核准」標記要等 Proxy 轉送 IDE 工具才會在正式環境生效；IDE 終端不再顯示核准的指令；標記金鑰就是 discovery bearer，能讀 discovery 檔的同一使用者行程也能算出標記（與 bearer 本身的信任界線相同）。
> - `auto_review.headless_terminal` 預設關；打開後所有靈魂無人在場的終端指令都送審、審查員放行就執行，超出事先核准的變寬，要不要打開由人類決定（只有人類 admin 能改，FL9）。
> - 心跳在 YOLO 下會在無人在場時用 MCP 工具（MCP 工具不經底線分類）；Auto 心跳的未列名 MCP 直接拒絕。
> - Auto 下 F5 的自帶審批工具會出兩張卡（只是多問一次，不放寬）。
> - ④甲的資料外送：有人在場的 Auto 對話會把最近 3 則人類訊息（各截到 1,000 字）、動作的完整參數與執行環境送到所選模型的廠商；本機只記用量。
> - 上方舊表 C5、DA24、R2 記的是當時的等級名稱：C5 的串接升級（ALLOW→CONFIRM）現在只在 YOLO 碰得到，Auto 的終端一律送審查員、動態 shell 寫法只有人能核准；DA24 看不懂的值現在退回 Manual；R2 的「強制 SafeAuto」現在是 Auto。
>
> 詳見 ADR-0246。

### 只留靈魂（2026-10-12，ADR-0245）

「未登入聊天（HIGH）」一列（2026-09～10 產品修復表）是本段修掉的既有漏洞。同段一起收緊的代理權限：

- **Guest 退役**：原本同時給未登入訪客、自訂模型與子代理用的 Guest 角色移除；子代理改用專屬的 `Subagent`，工具清單逐項照抄原 Guest（`test_guest_role_retirement.py`）。
- **非靈魂沒有代理權限**：只有靈魂、DebugAgent、DispatchAgent 套用覆寫、類型規則與能力；自訂模型、群聊或任意 ID 一律 Base、沒有工具與能力，因此也不能被心跳喚醒（`test_soul_only_agent_roles.py`）。
- **X-Agent-ID 只切靈魂**：擁有者或管理員用標頭切向非靈魂 ID 一律 403。`POST /identity` 拒絕為其他 ID 寫覆寫（422），`Subagent`／`System` 不可指派。
- **群聊只限靈魂**：別名只解析到靈魂，原始模型 ID 與 regex 不再展開；SNS `@別名` 同樣只到靈魂（`test_group_soul_only.py`）。
- **非擁有者不能指定別人的靈魂**（後續修補）：見產品修復表「非擁有者指定別人的靈魂（HIGH）」「自訂模型路徑穿越（HIGH）」「session 歸屬（MEDIUM）」三列；群聊每位成員同樣檢查（`test_soul_target_ownership_security.py`、`test_soul_target_bypass_security.py`）。
- **心跳白名單降級錯誤**：把靈魂加進白名單時不再寫成 `Base` 覆寫（`HeartbeatWhitelist.test.tsx`）。

> 已知殘留：live 權限設定檔已改成新版面（人類 10/12 同意）；其他 tracked live 設定不改檔，舊值只在讀取時略過並記 AUDIT 警告——`identity_map.json` 裡非靈魂的舊覆寫畫面上看不到、存檔時保留。要清掉須 Owner 手動改檔。

### 插話入口（2026-10-13，ADR-0249 第 4a、4b 段）

新的輸入入口：使用者在 AI 工作時送出的補充，會在工具邊界（4a）或打斷正在接收的那一輪之後（4b，Ctrl+Enter）放進模型的 context，屬於提示注入面。防護如下（回歸測試 `tests/unit/test_interjection_endpoint_security.py`、`tests/unit/test_interjection_queue.py`、`tests/unit/test_interjection_interrupt.py`）：

- **只有對話擁有者或 admin**：端點先過 `require_human_requester`（人類或 admin 代打；非 admin 的 X-Agent-ID 代打、靈魂 token、DebugAgent 一律 403），再過 `_authorized_stream_state`（串流發起者＋目前能存取這個對話＋同一串流代），和停止端點相同。
- **不能核准、不能改模式**：請求只有 `text`、`urgent`，多送的欄位（核准、模式、工具、角色）一律 422；佇列不碰 HITL、auth_level 或工具政策。
- **跨對話、跨串流不可讀寫**：佇列鍵是 (session_id, stream_id)；端點要求 `stream_id` 等於目前這一代，加入還要串流進行中；串流結束或同一對話開新串流時清空，上一個串流的插話不會漏到下一個。
- **上限**：模組常數，每個串流 10 則、每則 4,000 字、queued 有效 600 秒（過期改成未送達，不交給模型）。
- **存放**：佇列只在記憶體、串流結束即清；已送達的插話存進那一則助理節點的 `interjections[]`（U4a-3，走原本的對話存檔與授權）；無痕對話完全不存。
- **Ctrl+Enter 打斷（4b）**：只在 UI 路徑、只打斷正在接收的上游串流；打斷不了審批等待，`tool_choice=none` 繞不過審批卡或底線；不重試、不算一步。
- **Claude 中途 system**：規則表 `mid_conversation_system` 為真的型號，插話放在 system 角色，使用者文字因此有 system 權威，可能蓋過系統提示的規則；HITL、模式與工具權限由程式強制，不受影響（ADR-0249 已知限制）。
- **外部內容進不來**：佇列唯一的加入入口是這個端點，工具輸出與檢索結果沒有路徑可以加入。

### Claude SDK 1.x 傳輸加固（2026-10-12，ADR-0247）

anthropic SDK 0.83 → 1.11（傳輸改為 httpx2）時一併補上。回歸測試：`tests/unit/test_anthropic_sdk_transport_security.py`、`tests/unit/test_anthropic_base_url_pinned.py`、`tests/unit/test_anthropic_sdk_env_security.py`、`tests/unit/test_model_layer_redirects.py`（AST 守衛）、`tests/unit/test_network_guard.py`。

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **SDK1** | `main.py`（主迴圈 client）、`scripts/run_tool_eval_episode.py`、`tests/smoke/manual/live_chat_probe.py` | Claude SDK client 沿用 SDK 預設 http client（`follow_redirects=True`）；遇到轉址會把 `x-api-key` 送到轉址目標（httpx 跨來源只拿掉 `Authorization`） | 🟡 Medium | 建構點注入 `DefaultAsyncHttpxClient(follow_redirects=False)`（`completion.py` 已於 ADR-0244 注入）；factory 的 `with_options(api_key=…)` 沿用同一 client；禁令 22 AST 守衛擴到整個 `ToukaProxy/src`＋`scripts`，並要求 SDK 建構子帶這種 client |
| **SDK2** | `main.py`（主迴圈 client）、`services/completion.py`、`scripts/run_tool_eval_episode.py`、`tests/smoke/manual/live_chat_probe.py` | Claude SDK client 沒有顯式傳 `base_url`；SDK 會改連環境變數 `ANTHROPIC_BASE_URL`，`x-api-key` 一起送到那台主機 | 🟡 Medium | 每個建構點都傳 `src.config.ANTHROPIC_API_BASE_URL`（固定常數，刻意不做成會被環境變數覆寫的 Settings 欄位）；factory 的 `with_options` 沿用原本的 `base_url`；禁令 22 AST 守衛要求 SDK 建構子明寫 `base_url=`，不得是 `None` 或讀環境變數 |
| **SDK3** | `config.py`、`main.py`（主迴圈 client）、`dispatchers/factory.py`（`with_options` 換金鑰）、`services/completion.py` | SDK 1.11 建構 client（含 `with_options` 副本）時讀 `ANTHROPIC_CUSTOM_HEADERS`，能換掉 `x-api-key`、加任意標頭；`ANTHROPIC_LOG=debug` 把完整請求（含無痕內容）寫進日誌；另讀 `ANTHROPIC_AUTH_TOKEN`／profile／身分聯合等變數 | 🟡 Medium | 環境變數已在啟動時清除：`config.py` 載入（`.env` 灌進環境之後）pop 掉 `ANTHROPIC_SDK_ENV_OVERRIDES`（保留 `ANTHROPIC_API_KEY`），三個建構點送出前再清一次（ToukaMemory 匯入時會把 `.env` 再灌回環境）；SDK 已依 `ANTHROPIC_LOG` 調高的 logger 層級一併還原；回歸測試 `tests/unit/test_anthropic_sdk_env_security.py` |

### OpenAI SDK（Responses）傳輸加固（2026-10-13，ADR-0248）

openai SDK 3.24 為 Responses API 新加入（3b）；建構前就照 Claude 的規則加固，以下是預防性措施，沒有已發生的事故。回歸測試：`tests/unit/test_openai_sdk_env_security.py`、`tests/unit/test_responses_request.py`（傳輸層契約，先設好 5 個 `OPENAI_*` 環境變數證明程式沒讀）、`tests/unit/test_model_route_resolution.py`、`tests/unit/test_model_layer_redirects.py`（AST 守衛）。

| 編號 | 位置 | 類型 | 嚴重度 | 修復摘要 |
|----|------|------|--------|---------|
| **SDK4** | `config.py`、`handlers/adapters/responses_adapter.py`、`core/logging_config.py` | openai 3.24 建構或 import 時讀 `OPENAI_CUSTOM_HEADERS`（能塞任意標頭，含 Authorization）、`OPENAI_ORG_ID`／`OPENAI_PROJECT_ID`（加標頭）、`OPENAI_ADMIN_KEY`、`OPENAI_WEBHOOK_SECRET`；`OPENAI_LOG=debug` 會把請求內容寫進日誌 | 🟡 Medium | `OPENAI_SDK_ENV_OVERRIDES` 在 config 載入時與每次建構 openai client 前移除；openai 與 httpx2 logger 釘在 WARNING（`pin_sdk_loggers`，`setup_logging` 也會套）；`OPENAI_API_KEY`／`OPENAI_BASE_URL` 是 Touka 自己的設定名，保留但建構點顯式傳值。清除是整個 Proxy 行程生效（3a 清 `ANTHROPIC_*` 也是）：正式安裝的 MCP 子程序拿不到這些值，需要的 MCP 要在條目的 `env` 裡明寫（2026-10-15 起 MCP 子程序只拿白名單底＋條目 `env`，見產品修復表「MCP stdio 伺服器繼承全部金鑰」）；`.env` 檔本身不改 |
| **SDK5** | `handlers/adapters/responses_adapter.py`、`config.py`、`services/dispatchers/route_family.py` | SDK 拿到 `api_key=None`／`base_url=None` 會改讀環境變數，金鑰可能送到別的主機；預設 http client 跟隨轉址 | 🟡 Medium | 建構點顯式傳 `api_key`、`base_url`（來自路由）與 `DefaultAsyncHttpxClient(follow_redirects=False)`，缺金鑰或 base URL 時不建 client；官方 OpenAI 的目的地是 `config.OPENAI_RESPONSES_BASE_URL` 字串常數，不經會被同名環境變數覆寫的 `OPENAI_BASE_URL` 設定；禁令 22 AST 守衛擴到 openai 建構子（三個參數都要在、不得 None、不得讀環境變數） |
| **SDK6** | `handlers/adapters/responses_adapter.py` | `prompt_cache_key` 若用靈魂名或 session id 原文，會把 Touka 的內部識別送給廠商 | 🟢 Low | 送 `touka-`＋sha256(agent＼0session) 的前 32 碼；一律 `store=false`。殘留：這只是去識別、不是不可逆——沒有 session 時只靠靈魂名，用名字字典猜得出來；R-3b ① 判定不改 HMAC（OpenAI 本來就收到含人格的 instructions） |

### 創建試跑隔離

| 位置 | 原有缺口 | 修復摘要 |
|------|----------|----------|
| `services/creation/`、`tools/builtin/creation.py`、`routers/creation.py` | 候選 MCP 的 SMOKE／EVAL 未接隔離 runner，HTTP 入口可直接試跑 | 工具從帳本取一次 grant 並共用；HTTP 沒有降級同意，none 不啟動。試跑沿用正式自建／scaffold profile 與 clean env；來源由程式路徑決定。缺少 launch context 時 SMOKE 拒絕、EVAL 不啟動候選程式，不重新取得層級。有隔離時 YOLO 依層級執行；層級 none 時 Auto／YOLO 仍只有人能核准；Auto 原本就受底線 F5 約束。測試：`test_creation_isolation_grant_security.py`。 |

## Out of Scope

以下項目**不在**本專案的安全維護範圍內：

- 本機網路環境的整體安全性（防火牆、作業系統設定）
- 上游依賴套件的弱點（請直接向 PyPI / npm 套件維護者通報）
- ToukaProxy 未對外暴露時的網路層攻擊
- 使用者在 YOLO 模式（原 FullYOLO）下主動執行的危險指令（底線 F1–F4、F7 仍只有人能核准）
