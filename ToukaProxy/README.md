# 🌫️ Touka Proxy — 跨模態智能代理與長期記憶系統

**版本**: 1.3.0 (2026-06-09)

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.12-blue.svg)
![Docker](https://img.shields.io/badge/docker-ready-green.svg)

**Touka Proxy** 是一個高性能、具備長期記憶能力的 AI 代理中間層。它能將標準的 OpenAI 請求轉發至 Anthropic (Claude)、Google (Gemini) 與 xAI (Grok) 模型，並無縫整合多種通訊協議以及一套基於向量資料庫的「大腦」系統。

---

## ✨ 核心特色

### 0. 🪟 滑動視窗對話紀錄 (Sliding Window History) ⭐ *New in 1.0.0*
*   **自動維護對話長度**：心跳全部 pulse 完成後（PostHeartbeat hook），對家對話執行滑動視窗剪斷，防止 context 無限增長。
*   **ACID 安全歸檔**：先寫 Legacy JSON → 確認成功 → 再更新 Session，任一失敗安全跳過。
*   **可設定**：透過 `GET/POST /v1/system/sliding_window` 管理 `enabled`、`keep_rounds`、`upper_limit_rounds`。
*   **子對話排除**：僅對家對話（`session_mode="home"`）生效，子對話完全不受影響（ADR-0035）。

### 0.5 🗂️ AI 工作分派看板中心 (AI Agent Dispatch Center) ⭐ *New in 1.0.0*
*   **中介分派機制**：引入系統級虛擬中介 `DispatchAgent`，作為內部 Agent 與外部 AI 工具 (如 Claude Code、Aider) 的協作看板。
*   **跨身分代理授權**：配置 `"can_supervise_dispatch"` 能力的 AgentIdentity (Supervisor 角色) 可繞過物理路徑隔離，代理操作 `DispatchAgent` 看板。
*   **結構化 Task Schema 與產物解耦**：任務模型深度擴展以支援 `verification_cmd`（驗證命令）、`boundary_rules`（邊界法律規則）、`test_log`（測試結果），並將產出物解耦為獨立的單一產物 `artifact_ref` (str) 與多產物列表 `artifact_refs` (list[str])，互不踩踏覆蓋。
*   **有限狀態機 (FSM) 狀態流轉矩陣**：狀態更新強制執行 FSM 轉換矩陣（例如 `TODO -> DOING -> DONE`，禁止 `TODO -> DONE` 等越過執行的行為，`DONE` 狀態為終端不可再次變更）。
*   **FAILED 特權重置**：當任務失敗進入 `FAILED` 狀態後，只有特權身分（具有 `can_supervise_dispatch` 特權標籤或角色為 `Owner`/`Debugger`/`admin` 的 Touka Agent）才能將狀態重設為 `TODO`，外部 AI 代理人無法自行重設。
*   **專用派發伺服器 (ToukaDispatch, Port 8083)** ⭐ *C1 安全修復 → ADR-0082 拓撲合併*：外部 AI 工具的任務派發請求由 `ToukaDispatch` FastMCP 服務處理，透過 `DISPATCH_TOKEN` Bearer Token 認證，不含任何自動特權注入。`ToukaMemory/server.py` 的自動自癒授權機制已移除（ADR-0046）。**ADR-0082 後**：dispatch 隨 proxy **單一映像**出貨、由 `DispatchSupervisor` 於 lifespan spawn 為 localhost 子程序（看門狗退避重生、`/stats` 觀測 `dispatch_supervisor` 欄位）；綁定預設 `127.0.0.1`，8083 由 touka-proxy 服務對外發布。
*   **Mission Token 安全隔離** ⭐ *ADR-0064*：`DISPATCH_TOKEN`（全域主金鑰）永遠不離開 Touka 進程邊界。外部 AI 只接收由 `dispatch_initialize_mission` 生成的一次性 `mission_token`（與 `mission_id` 一對一綁定），等同 AWS STS 臨時憑證委派模式。
*   **任務分派協調員 Skill** ⭐ *ADR-0065 → 0066/0067*：`touka-dispatch-coordinator` skill 內建作業流程（建立 mission → 自動召喚外部 AI → 等待 callback → 回報結果）。經 ADR-0066 遷移為 Anthropic 標準 `SKILL.md`、MCP 由同目錄 `.mcp.json` 自動掛載；經 ADR-0067 改用 `dispatch_initialize_mission(..., auto_invoke="claude-code")` 自動召喚（依 `dispatch_hooks.json` 白名單），Agent 不再手寫 bash。

### 0.6 🧩 Skill 系統 Anthropic 標準化 ⭐ *New in 1.1.0*
*   **開放標準遷移（ADR-0066）**：全系統 Skill 從單一 `skills.json` 遷移至 [Anthropic Agent Skills 開放標準](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)——目錄式 `config/skills/<id>/SKILL.md`（YAML frontmatter + body）+ 選填 `.mcp.json`/`scripts/`/`references/`/`assets/`。Touka 擴充欄位收於 frontmatter `metadata:` 子欄位，通過標準 `quick_validate`，對外完全相容、可雙向分發。
*   **三層 Progressive Disclosure**：metadata 常駐 → body 觸發 → resources 按需，降低 context 佔用。
*   **MCP 自動掛載**：`mcp_servers` 由同目錄 `.mcp.json` 的 top-level keys 推導，同一檔案同時服務 Touka 內部掛載與外部 AI 安裝 plugin 的客戶端 MCP 註冊。
*   **Pull 模式 auto-trigger**：`system-skill-dispatcher` 常駐列出 skill 清單，Agent 自主呼叫 `activate_skill(skill_id)` 寫入 `session.active_skills`，下一輪生效（仿 `connect_mcp_server`）。
*   **打包分發**：`GET /v1/skills/{id}/export` 匯出 `.skill`（ZIP）；`POST /v1/skills/install`（Owner/admin）由 URL 安裝，受 `ALLOWED_SKILL_DOMAINS` SSRF 白名單 + `follow_redirects=False` + zip-bomb 防護 + `.bak` 原子覆寫保護。
*   **Dispatch 自動召喚（ADR-0067，CaC）**：`dispatch_initialize_mission` 加 `auto_invoke` 參數，mission 建立後依 `ToukaDispatch/config/dispatch_hooks.json` 白名單以 `subprocess`（`shell=False`、env-only 傳一次性 `mission_token`、whitelist 淨化子環境）fire-and-forget 召喚外部 AI。新增 executor 只改設定不改程式碼；全域 `DISPATCH_TOKEN` 主金鑰絕不外洩。

### 0.7 🏭 原生創建引擎 (Creation Engine) ⭐ *New in 1.2.0*
*   **一引擎兩產物（ADR-0069）**：MCP 與 Skill 收斂為同一條創建流水線——SPEC → VALIDATE → 🆕SMOKE（沙箱試跑）→ COMMIT（原子寫入＋上線）→ 🆕EVAL（後置、非阻斷評測）。引擎只編排，每種產物以 `CreationKind` 策略插入（SOLID-OCP），重用既有 SSOT（skill 委派 `SkillService`、MCP 委派 `MCPHubService.register_server`），**CREATE 不覆寫既有 id**（引擎層硬約束，保護既有 agent 資料）。
*   **SMOKE 沙箱試跑**：註冊前以白名單底（`LAUNCH_ENV_ALLOWLIST`，和正式安裝的 MCP 同一份，ADR-0254）＋spec 宣告的 env 真啟動 MCP 握手（`initialize` + `tools/list ≥ 1`）+ 逾時 + 保證拆除；skill 做 SKILL.md round-trip 乾跑（不執行碼）。失敗即不 commit。
*   **SCAFFOLD 決定性生成（零 LLM、零付費）**：`McpScaffolder` 由高階 `McpScaffoldSpec` 生成可執行 FastMCP server stub 至 `config/mcp_servers/<name>/server.py`，餵入既有 create 流水線；注入防護（識別字校驗 + `repr()` 轉義 + 型別白名單 + `ast.parse()` 兜底 + 名稱單段 slug 防穿越）。
*   **對外開放（Owner/admin）**：`POST /v1/creation/{skill,mcp,scaffold}` 路由＋ agent 對話工具（`create_skill`/`create_mcp`/`scaffold_mcp`），共用薄門面 `services/creation/api.py`（DRY/SSOT）。驗證/SMOKE 失敗回 **200+errors（不報 500）**、無 token→401、錯角色→403。
*   **EVAL / ModelJudge（雙閘預設關、零成本）**：commit 後生成唯讀評測基線 `config/evals/<kind>/<id>.eval.md`；LLM 計分需 `TOUKA_EVAL_JUDGE_ENABLED` ＋ `eval_scoring=True` 兩閘全開才花 token，fail-safe 0.0 不阻斷 commit。
*   **主權守衛**：Creation Sovereignty Guard #29（`scripts/guard_creation_sovereignty.py`）強制路由 Owner/admin 把關＋禁繞引擎直呼 `update_skill`/`register_server`＋門面不依賴 fastapi。

### 0.8 🧙 首次設定精靈與嵌入模型生命週期 (Setup Wizard & Embedding Lifecycle) ⭐ *New 2026-07-02 · ADR-0118*
*   **即時金鑰存取 (registry.get_key_by_id) ⭐（延伸 ADR-0099）**：新增 `registry.get_key_by_id(provider_id)` SSOT 便捷方法，供分派器即時讀取供應商金鑰。`dispatchers/factory.py` 的 gemini 分支改用 `registry.get_key_by_id("gemini")`（即時），取代過期的 `from src.config import GEMINI_API_KEY`；claude 分支改以 `anthropic_client.with_options(api_key=registry.get_key_by_id("anthropic"))` 逐請求套用（共用 httpx pool）。使精靈寫入的金鑰免重啟即時生效。OpenAI 相容供應商原已透過 `get_provider_info → registry.get_key` 即時取用。
*   **嵌入模型生命週期三動作嚴格分離（延伸 ADR-0070）**：新增 `src/services/embedding_service.py`，將三個動作徹底切分——(1) **DOWNLOAD** 僅將模型檔抓取至 HF cache（背景任務＋輪詢狀態、逐檔 `hf_hub_download` byte 進度、以 `snapshot_download local_files_only` 做 cache-skip；純追加、絕不 `force_download`、不刪任何模型），不觸碰任何向量／facts；(2) **SWITCH** 寫入 `EMBEDDING_MODEL` 並重置 `MemoryManager` singleton（免重啟），`evaluate_switch` 偵測既有記憶（`FactualStore.query_all_facts`）且模型不同時**要求使用者確認**（`force` 旗標）；(3) **RE-EMBED** 僅離線維運（`reembed_memory.py`），絕不自動觸發、絕不觸碰 `core_memory.db`（facts 為無損 SSOT，向量為衍生索引）。
*   **新增管理端點（Owner/admin）**：
    *   `POST /v1/admin/setup/embedding/download` — 觸發模型下載背景任務。
    *   `GET /v1/admin/setup/embedding/download-status` — 輪詢下載進度／狀態。
    *   `POST /v1/admin/setup/embedding/switch` `{model, force}` — 切換嵌入模型；存在既有記憶且模型不同時需 `force=true`。
*   **HF cache 具名磁碟區（延伸 ADR-0038）**：`docker-compose.yml` 新增頂層具名 volume `hf-cache` 掛載於 touka-proxy 的 `/app/hf_cache`，新增 `HF_HOME=/app/hf_cache`（同一變數同時涵蓋精靈下載與 `vector.py` runtime 的 SentenceTransformer 載入）；`touka-init` 掛同一 volume 並 `chown /app/hf_cache` 至 `1001:1001`（全新具名 volume 為 root 所有，未 chown 前 appuser 無法寫入）。模型跨 `docker compose up --build` 持久化；`docker compose down -v` 清除。

### 1. ❤️ 自主心跳與節律 (Autonomous Heartbeat & Pulse) ⭐
*   **智慧節律器**：整合 `ToukaHeartbeat` (Pacemaker)，定時喚醒靈魂執行任務。
*   **多回合自主行動 (Multi-Turn Pulse)**：支援 `pulse_slots` 參數，讓靈魂在無人介入下自主執行多輪思考與研究。

### 2. 📂 專案管理與主權隔離 (Sovereignty) ⭐
*   **多層級檔案工具**：引入 `vault_tools` (記憶庫) 與 `project_tools` (專案源碼)。
*   **路徑主權隔離 (Prison Principle)**：**強制實施**。Agent 的所有檔案操作皆透過 `ResourceManager` 物理鎖定在自身 ID 目錄下。
*   **ToukaMemory 整合（ADR-0116：整合模式單一部署）**：記憶系統與工具邏輯已下沉至 `ToukaMemory` 模組。Proxy 扮演「薄橋接器」，負責身份錨定、權限透傳，並在同行程內注入真實 `ResourceManager`；`get_resource_proxy()` 取不到該實作時必須 fail-loud。獨立 MCP 與 `_FallbackResourceProxy` 已退役。

### 3. 🛡️ 身份物件化與 ABAC 安全矩陣 (Identity & ABAC) ⭐
*   **身份物件化 (Identity SSOT)**：全系統（Router, Service, Resource 層）全面實施驗證後的 `AgentIdentity` 物件化，嚴禁使用原始 `str` ID。
*   **多使用者 RBAC**：區分人類管理員 (`admin`) 與一般使用者。使用者透過 `users.json` 綁定其擁有的代理人清單。
*   **三種模式 (Manual／Auto／YOLO，ADR-0246)**：Manual 每次問人；Auto 的安全類動作自動放行、終端與高風險工具先交審查員；YOLO 除黑名單與底線外全自動。底線（刪改資料、讀金鑰、改權限設定、改原始碼、系統破壞）寫在程式裡、只有人能核准。
*   **管理員代理優化 (Assumption)**：確保管理員在 Assume 身分時保留管理特權，解決管理 API 的權限斷鏈。
*   **法規即代碼 (LaC)**：實施 **TID251 (Physical Path Ban)**。嚴禁業務邏輯調用 `os.environ`、`subprocess` 或硬編碼路徑。由 `lint_ssot.py` 強制稽核。
*   **環境與時間主權與環境感知載入 (Full-stack & Env Selection)**：遷移至 Pydantic `BaseSettings`。嚴禁直接訪問原生環境變數。後端（含 `ToukaProxy` 與 `ToukaHeartbeat`）支援環境感知（Environment-Aware）的 `.env` 互斥載入機制：整合執行時僅加載根目錄 `.env`，獨立執行時僅加載子專案自身的 `.env`，防配置交叉污染。前端透過 `guard_env.ts` 攔截 `process.env` 調用。
*   **`.env` 路徑單一事實來源 (resolve_env_file_path SSOT) ⭐ *2026-07-02 · ADR-0118*（延伸 ADR-0052 / 禁令 38）**：`.env` 的讀寫路徑統一由 `src.config.resolve_env_file_path()` 解析，`routers/config.py` 不再使用裸相對 `".env"`。解析優先序為 **`TOUKA_ENV_FILE` 環境變數覆寫（最高優先）** → 整合／獨立模式自動偵測。`docker-compose.yml` 設定 `TOUKA_ENV_FILE=/app/host_root/.env`（**ADR-0151**：`./:/app/host_root` 恆掛 compose 目錄＝compose `env_file` 讀取的同一 .env，與 `WORKSPACE_ROOT` **解耦**；舊制綁 `WORKSPACE_ROOT` 的 `/app/workspace/.env`，使用者把 `WORKSPACE_ROOT` 指向 repo 以外即寫進沒人讀的幽靈 .env），使首裝精靈與自動生成的金鑰在容器重建後仍然**持久化**並於重啟時重新注入。`apply_runtime_env()` 同步 `os.environ`、`settings` 與 module-level `src.config` 常數，達成免重啟即時生效。
*   **會話與部署主權 (Session & Deployment SSOT)** ⭐：統一 Cookie `path="/"` 管理。實施 **Dockerfile 物理等價性守衛**，確保容器環境與開發代碼強一致。
*   **密碼安全禁令 (BCRYPT_IMPORT_SSOT)** ⭐：嚴禁在 `auth_service.py` 以外直接 `import bcrypt` 或 `passlib`，由 `lint_ssot.py` 的 `BcryptImportRule` 靜態攔截。確保 `SHA256 → Bcrypt` 預雜湊鏈完整，避免 bcrypt 72-byte 截斷漏洞。（ADR-0028）
*   **資料庫存取禁令 (SQLITE3_IMPORT_BAN)** ⭐：業務層嚴禁直接 `import sqlite3`，必須透過 `async_db.SessionService` 以維護非同步佇列與鎖定機制，由 `lint_ssot.py` 的 `Sqlite3ImportRule` 攔截。（ADR-0030）
*   **NOQA 登記簿自動同步 (guard_noqa_registry.py)** ⭐：`scripts/guard_noqa_registry.py` 自動驗證 `NOQA_REGISTRY.md` 統計表與代碼庫實際 `# noqa` 計數一致，整合為第 16 道 guard。（ADR-0029）
*   **系統操作 API 禁令 (ENV_SSOT 擴充)** ⭐：`EnvSovereigntyRule` 擴充攔截業務層的 `os.system()`、`os.chdir()`、`sys.path.append()`（限 `/src/` 路徑），補齊 AGENTS.md 第 18 節的聲明。
*   **前端 Console 禁令 (CONSOLE_BAN)** ⭐：`guard_console.ts` 掃描前端生產代碼中的 `console.log/warn/error()` 直接呼叫，整合為第 17 道 guard。與後端 PRINT_BAN 對稱。（ADR-0031）
*   **死碼偵測 (guard_dead_code.py)** ⭐：整合 `vulture`（Python）與 `knip`（TypeScript），偵測未使用的函式、export 與檔案。整合為第 18 道 guard。初次執行清除 5 個未使用 TS 檔案及多個死碼函式。
*   **模組邊界守衛 (guard_module_boundaries.py)** ⭐：使用 `import-linter` 強制 ToukaMemory 不直接依賴 ToukaProxy 內部模組，整合為第 21 道 guard（現行編號，序號 SSOT＝`guard_all.py`）。（ADR-0032）
*   **InfoSec C1~N12 安全加固** ⭐：完成二次審計，共 33 項弱點修復（11 Critical + 10 High + 11 Medium + 1 Low），詳見 [SECURITY.md](../SECURITY.md) 與 [ADR-0033](../docs/architecture/adr/0033-infosec-hardening-c1-m6.md)。
    1. **密鑰主權**：`AuthSecretManager`、`StorageService._initial_config()`、`ToukaHeartbeat/config.py` — 系統無公開已知硬編碼預設密鑰（C1、C2、H5殘留、M6）
    2. **身份主權**：聊天端點 `require_authenticated`（ADR-0245）、`_require_connections_role()`、N3/N10 端點驗證 — 沒登入的聊天回 401；連線管理/MCP hub install/project overview 均需角色驗證（C3、C7、M5、N1/N2/N3/N10）
    3. **結構化邊界校驗**：`is_relative_to()`、`_classify_command_strategy()`、`_build_jail_code()`、`MCPServerItem.command` validator、`_validate_npm_package_name()`、固定 SQL `IN (?, ?)`（C4、C5、C6、M4、N6、N12）
    4. **統一安全守衛層**：`csrf_guard.verify_origin()`、`RateLimiter`、`_approval_lock`（asyncio）、`_users_file_lock`（threading）、`_sanitize_connection_error()`（C8、C9、H1、H2、H6、M1、M2/N5、N4、N7/N9）
    5. **沙箱與重定向防護**：`get_clean_env()` 黑名單+顯式移除、`sanitize_memory_content()`、`follow_redirects=False`（C6、H3、H4、N8）
*   **CORS 動態來源正則 (allow_origin_regex) ⭐ *2026-07-02 · ADR-0118*（E#17）**：`main.py` 的 CORS 改用 `allow_origin_regex` 涵蓋 localhost／`127.0.0.1`／`[::1]`／私有網段（10/8、172.16–31、192.168/16）任意 port 及 `vscode-webview://.*`，同時保留 `allow_origins` 供 `ALLOWED_ORIGINS` 明列的公開網域。修正：先前經由 `127.0.0.1:PORT` 或 LAN IP（手機／第二裝置）開啟 UI 時所有 API 呼叫遭 CORS 阻擋的問題。另 `routers/config.py` `update_env` 的 500 回應不再回傳原始例外字串（`error_output`），僅寫入 `logger.exception`，避免洩漏檔案系統路徑（E#21）。
*   **深度對抗式審計 DA1~DA28（2026-06）** ⭐：多代理對抗式獵捕＋對抗複審補抓、三輪舊審計與 31 道守衛皆漏掉的 28 項（7 High＋9 Medium＋9 Low＋1 Info＋2 功能 bug）——授權邊界（`/config/env`、`POST /identity`、會話/專案 IDOR、debug/souls 跨用戶）、JWT type 驗證、帳號停權強制、bootstrap admin 密碼移出日誌、admin 端點 `UserOut`、心跳 `/awaken` 加固、演化沙箱 **OS 級資源限制（ADR-0076，POSIX `setrlimit`）** 等。每項 TDD-Red 先行，詳見 [SECURITY.md](../SECURITY.md) §DA1–DA28。

### 5. 🌍 靈魂群聊 (ToukaGroup) ⭐
*   **多人協作分發員**: 支援透過 `@別名` 點名機制。系統會自動解析別名、注入身分感知的「自我提示」並協調多模型接續發言。
*   **模型自癒保護**: 整合 `ModelManager` 的 `proxy` 擁有者鎖定，確保群聊引擎在自動清理過程中始終保持線上。

### 6. 🆔 ID 歸一化與靈魂復原 (ID Unification & Soul Protection) ⭐
*   **ID 歸一化**: 全系統統一使用簡潔 ID（如 `Alice`），捨棄 `ToukaSoul` 前綴。支持舊 ID 自動映射糾偏。
*   **發現即繼承**: 動態發現代理人後，自動合併 `models.json` 中的 UI 設定（名稱、開關、可見性）。
*   **靈魂硬保護**: 強制豁免存在物理實體的靈魂代理人於自動清理機制之外。
*   **Gemini 模型動態篩選**: 重構 `fetch_gemini_models`，改用正規表達式 `gemini-(\d+(?:\.\d+)?)` 解析模型主版本號，僅放行 `>= 2.0` 的新模型，動態排除已停用的 `1.5` 系列。

---

## 🚀 安裝與開發入口

一般使用者依 [根目錄安裝指南](../README_QUICKSTART.md) 使用已資格的 Proxy／Heartbeat／Chat 映像；Proxy 映像已包含 ToukaMemory 與 ToukaDispatch，不另外安裝 Memory server，不從本子目錄啟動另一組容器。

開發者先依 [CONTRIBUTING](../CONTRIBUTING.md) 佈建 root workspace。既有 `setup.ps1`／`setup.sh` 只執行受鎖 uv 同步並驗 root venv，從任何 CWD 均可定位專案；不建立 data、.env、config 或啟動 Docker。舊 torch 選單已退役，CPU torch 由 `uv.lock` 管理，不以裸 pip 覆寫。

Windows 明確選擇 native 開發時，可執行本目錄 `run_proxy.ps1`，使用 root venv、loopback 與 `--no-access-log`；`PORT` 若設定須為 1–65535。此入口會運行產品並按受管設定啟動；使用者自行準備隔離的開發資料與設定，不能在現行部署旁碰運氣啟動，也不能把開發通過當成發版資格。本批自動化只以假命令／sandbox 驗證。

既有 `ToukaProxy/docker-compose.yml` 是歷史單模組開發組態，與根 GHCR profile 的 env_file、掛載與綁定位址不同；不作目前產品安裝入口。細節見 [Proxy 安裝說明](../docs/proxy/installation_guide.md)。

---

## 🧪 驗證與測試 (Verification)

> **韌性與失效可觀察性（ADR-0078）**：`ResourceManager.write_file_text` 採原子寫入（temp+fsync+`os.replace`）防損毀；工具/網路/API 錯誤與系統降級皆可見；當機後 `GET /v1/stats` 的 `interrupted_runs` 揭露未跑完的背景脈衝，可經 `POST /v1/heartbeat/retrigger`（強制 Auto、僅擁有者）一鍵重跑。詳見 [ADR-0078](../docs/architecture/adr/0078-resilience-and-failure-observability.md)。

本專案實施 **SSOT 大一統測試架構**。所有測試均已遷移至專案根目錄，請確保在 **WORKSPACE ROOT** 執行。

```powershell
# 1. 執行全系統全量檢測 (推薦)
python -B -m pytest tests/

# 2. 執行 Proxy 專屬單元測試
python -B -m pytest tests/unit/

# 3. 前端 ToukaChat 測試
cd ToukaChat
npm run test
```

### 📜 合約維護 (Contract Maintenance)
當修改了 `src/models/` 下的 Pydantic 模型時，必須更新 OpenAPI 格式：
```bash
python scripts/update_openapi.py
```

---

## 📚 說明文件 (Documentation)
- [標準化介面文件 (API Endpoints)](../docs/architecture/PROXY_INTERFACE.md)
- [全球單一事實來源 (SSOT Master Map)](../docs/architecture/SSOT_MASTER_MAP.md)
- [全系統測試指南](../docs/guides/testing_guide.md)

---
*Generated by Antigravity for the Harmony of Touka Ecosystem.*
