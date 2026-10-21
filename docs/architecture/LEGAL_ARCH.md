# 🛡️ 法規即代碼 (Legal Architecture, LaC)

> 此文件從 `SSOT_MASTER_MAP.md` 中抽出。定義所有靜態執法禁令與守衛規則。

## 1. 物理路徑禁令 (TID251)
*   嚴禁在業務代碼中使用硬編碼的磁碟代號或容器絕對路徑。所有路徑必須透過 `ResourceManager` 解析。

## 2. 引導與環境變數禁令 (Env SSOT)
*   **API 禁令**: 嚴禁在業務邏輯中調用 `os.environ`, `os.system`, `subprocess.run`, `os.chdir` 等敏感 API。**由 `lint_ssot.py` 進行靜態稽核。**
*   **特權白名單**: 僅限 `config.py`, `main.py`, `bootstrap.py`, `terminal.py` 與 `conftest.py` 擁有環境操作特權。
*   **配置唯一性**: 所有外部環境變數必須映射至 `src.config` 的 Pydantic 模型，禁止散落在代碼中直接調用。
*   **環境變數主權 (Frontend)**: 前端嚴禁直接存取 `process.env` 或 `import.meta.env`，必須透過 `src/lib/config.ts` 集中引用。**由 `guard_env.ts` 進行靜態稽核。**

## 3. 錯誤碼主權 (Error Code Sovereignty) ⭐
*   **執法**: 嚴禁在 Exception 或是 UI 提示中使用硬編碼字串。
    - **後端（HTTPException）**: 攔截 `HTTPException(detail="...")` 與 `ToukaException` 字串傳入。**由 `lint_ssot.py` 的 `ErrorSovereigntyRule` 執行。**
    - **後端（raise 硬編碼）**: 攔截 `raise ValueError("...")` / `raise RuntimeError("...")` 等對 `ValueError`, `RuntimeError`, `TypeError` 使用字串字面量或 f-string 的情形。**由 `lint_ssot.py` 的 `RaiseHardcodedStringRule`（RAISE_STRING_BAN）執行。**（ADR-0027）
    - **前端**: 攔截 `toast.error("...")` 與 `console.error("...")` 硬編碼內容。
*   **SSOT 接口**: 後端使用 `ErrorCode` 枚舉，前端使用 `ERRORS` 常數，兩者皆自動衍生自 `errors.json`。

## 3-B. 時間獲取禁令
*   **執法**: 嚴禁調用 `datetime.now()` 或 `time.time()`，必須統一使用 `src.utils.time_utils` 提供的 SSOT 接口。**由 `lint_ssot.py` 進行強制攔截。**

## 3-C. 日誌主權禁令 (Logging Sovereignty) ⭐
*   **`print()` 禁令**（PRINT_BAN）：業務層嚴禁直接調用 `print()`，必須改用 `logging.getLogger(__name__)`。**由 `lint_ssot.py` 的 `PrintBanRule` 執行，僅掃描 `/src/` 路徑。**（ADR-0025）
*   **`logging.basicConfig()` 禁令**（BASICCONFIG_BAN）：嚴禁在非初始化檔案中調用 `logging.basicConfig()`。唯一授權入口為 `src/core/logging_config.py`。**由 `lint_ssot.py` 的 `BasicConfigBanRule` 執行。**（ADR-0025）

## 3-D. HTTP 客戶端主權 (Fetch SSOT) ⭐
*   前端所有 HTTP 請求必須透過 `fetchTouka(proxyUrl, path, options?)` 發送，嚴禁直接調用原生 `fetch()`。**由 `guard_fetch.ts`（FETCH_SSOT）進行 AST 掃描，整合至 `guard_all.py`。**（ADR-0024）

## 3-E. localStorage 存取主權 (localStorage SSOT) ⭐
*   前端所有 localStorage 操作必須透過 `src/hooks/useLocalStorage.ts`（通用）或領域 Custom Hook 進行。**由 `guard_localStorage.ts`（LOCALSTORAGE_SSOT）掃描 `src/hooks/**`、`src/lib/**`、`src/app/**`、`src/contexts/**`。**（ADR-0026）
*   白名單：`useLocalStorage.ts`（SSOT 定義本體）、`connections.ts`（非 React 環境的 Auth Token 管理）。

## 3-F. 密碼安全禁令 (BCRYPT_IMPORT_SSOT) ⭐
*   **執法**：嚴禁在非授權檔案直接 `import bcrypt` 或 `from passlib import`。**由 `lint_ssot.py` 的 `BcryptImportRule` 執行。**（ADR-0028）
*   **風險**：繞過 `SHA256 → Bcrypt` 預雜湊鏈，bcrypt 原生 72-byte 截斷限制將導致長密碼靜默失效。
*   **唯一授權點**：`ToukaProxy/src/services/auth_service.py`（白名單）。所有密碼雜湊必須透過 `AuthService.hash_password()` 執行。
*   **passlib 完全禁止**：已從依賴移除，防止意外重新引入。

## 3-G. 資料庫存取禁令 (SQLITE3_IMPORT_BAN) ⭐
*   **執法**：嚴禁在非授權檔案直接 `import sqlite3`。**由 `lint_ssot.py` 的 `Sqlite3ImportRule` 執行。**（ADR-0030）
*   **風險**：繞過非同步佇列與物理鎖定，可能導致並發寫入競態與身分追蹤失效。
*   **授權白名單**（三個 SSOT 入口）：
    - `async_db.py`：ToukaProxy 的非同步資料庫 SSOT（含佇列鎖定）
    - `factual.py`：ToukaMemory 自有事實庫 SSOT
    - `legacy_store.py`：ToukaMemory 自有遺產庫 SSOT
*   **合規路徑**：ToukaProxy 業務層使用 `async_db.SessionService`；observability 端點透過 `get_manager().get_factual_store().query_all_facts()` 整合層存取。

## 3-H. 系統操作 API 禁令 (ENV_SSOT 擴充) ⭐
*   **執法**：嚴禁在業務層（`/src/` 路徑）直接調用 `os.system()`、`os.chdir()`、`sys.path.append()`。**由 `lint_ssot.py` 的 `EnvSovereigntyRule.check_Call()` 擴充執行，限 `/src/` 路徑。**
*   **豁免**：引導者白名單（`bootstrap.py`、`terminal.py` 等）及全域白名單（`is_whitelisted`）自動豁免。

## 3-I. 前端 Console 禁令 (CONSOLE_BAN) ⭐
*   **執法**：前端生產代碼嚴禁直接呼叫 `console.log()`、`console.warn()`、`console.error()`。**由 `guard_console.ts`（CONSOLE_BAN）掃描 `src/hooks/**`、`src/lib/**`、`src/app/**`、`src/contexts/**`。**（ADR-0031）
*   **唯一授權點**：`src/lib/error_handler.ts`（`logSSOTError()` SSOT 封裝層，白名單）。
*   **與後端對稱**：與 PRINT_BAN（ADR-0025）設計原則一致。

## 3-J. 模組邊界禁令 (Module Boundary Guard) ⭐
*   **執法**：`ToukaMemory` 嚴禁直接 import `ToukaProxy` 內部模組（`src.services`、`src.utils` 等）。**由 `scripts/guard_module_boundaries.py`（`import-linter`）執行，整合至 `guard_all.py` 第 21 道關卡 (Guard #21)。**（ADR-0032）
*   **合法整合點**：`memory_server.config_utils` 使用 `try-import` 回退機制（ADR-0022），屬白名單豁免。
*   **違規修復**：`diag.py` 直接 import `src.services.resource_manager` 已修復為使用 `get_resource_proxy()`。

## 3-K. 資訊安全守衛層 (InfoSec Guards) ⭐
*   **背景**：2026-05 完成 C1~N12 共 33 項弱點修復（詳見 `SECURITY.md` + ADR-0033）；並建立 Guard #24 端點認證守衛（ADR-0034），清零 52 個技術債端點。
*   **守衛模組**（純函式，可獨立單元測試）：
    - **`ToukaProxy/src/core/security.py`** `AuthSecretManager` — JWT 密鑰主權，無設定時 `secrets.token_hex(32)` 自動生成
    - **`ToukaProxy/src/utils/csrf_guard.py`** `verify_origin()` — CSRF Origin 標頭白名單驗證
    - **`ToukaProxy/src/utils/rate_limiter.py`** `RateLimiter` — IP 滑動視窗速率限制
    - **`ToukaProxy/src/tools/terminal.py`** `_classify_command_strategy()` — Shell metachar 偵測與升級
    - **`ToukaProxy/src/tools/builtin/evolution.py`** `_build_jail_code()` — 演化沙箱用 `json.dumps()` 轉義動態程式碼
    - **`ToukaProxy/src/services/evolution_security.py`** `get_clean_env()` — 沙箱環境變數黑名單+顯式移除雙保險
    - **`ToukaProxy/src/services/auth_service.py`** `_atomic_users_write()` — users.json 原子 read-modify-write 原語（threading.Lock）
    - **`ToukaProxy/src/routers/terminal.py`** `_approval_lock` — terminal approval asyncio.Lock 防競爭條件
    - **`ToukaProxy/src/routers/mcp.py`** `_validate_npm_package_name()` — npm package name 嚴格正則驗證防 SSRF
    - **`ToukaProxy/src/services/models/manager.py`** `_sanitize_connection_error()` — 連線錯誤 API key 消毒
    - **`ToukaMemory/src/memory_server/utils/content_guard.py`** `sanitize_memory_content()` — 記憶 Prompt Injection 偵測
    - **`ToukaMemory/src/memory_server/utils/historical_data.py`** `wrap_historical_data()`、`neutralize_historical_tags()` — 查記憶、隨機回想、前情摘要、家對話的歷史資料界線與界線 tag 中和（ADR-0257）
    - **`ToukaHeartbeat/storage_service.py`** `_initial_config()` — Heartbeat token 首次啟動隨機生成並持久化
    - **`ToukaHeartbeat/config.py`** `HEARTBEAT_TOKEN: str = ""` — 移除公開硬編碼預設值
    - **`ToukaChat/src/lib/connections.ts`** `sanitizeHeaderValue()` — 前端 Header 注入消毒
*   **端點認證守衛 (Guard #24 — `scripts/guard_endpoint_auth.py`)** ⭐：
    - AST 掃描所有 `ToukaProxy/src/routers/` 端點；豁免清單：`endpoint_auth_allowlist.py`
    - Grandfather List 初始 52 項，分三批次清零後維持 `GRANDFATHER = []`（不可再新增）
    - **批次一**：`system`, `identity`, `permissions`, `scheduler`, `autonomy`, `skills`, `souls` 22 個端點補修
    - **批次二**：`bots`, `custom_models`, `mcp`, `ui_sessions`, `settings` 18 個端點補修
    - **批次三**：`editor`, `models`, `terminal/history`, `stats`, `group_settings` 12 個端點補修
*   **verify_heartbeat_token SSOT** ⭐：`ToukaProxy/src/routers/heartbeat.py` — `/heartbeat/awaken` 的 service token 驗證唯一事實來源；其餘端點嚴禁重複實作 JWT/token 驗證邏輯（ADR-0034 SOLID-SRP）
*   **跨身分分派代理與邊界防護** ⭐：`MissionManager` 中的 `can_supervise_dispatch` 校驗 — 系統嚴格執行物理路徑監獄。唯一合法的跨身分目錄讀寫，僅限於呼叫方 AgentIdentity 在 `identity_map.json` 中配置有 `"can_supervise_dispatch"` 能力且目標為 `DispatchAgent` 看板。任何其他未獲授權的跨身分存取皆會被 ResourceManager 阻斷。
*   **端點驗證守衛（已補修代表）**：
    - `routers/connections.py` `_require_connections_role()` — `/ui/connections` Owner/Debugger/admin only
    - `routers/mcp.py` hub/install — Owner/admin only
    - `routers/ui_sessions.py` project overview — Owner/Debugger/admin only（N10 IDOR fix）
    - `routers/models.py` `_require_models_mgmt()` — refresh/patch/delete/health
    - `routers/editor.py` `_require_editor_role()` — /files, /read
    - `routers/terminal.py` — history 端點認證 + None bypass 修復
*   **安全常數 SSOT**（位於 `ToukaProxy/src/config.py` Pydantic Settings）：
    - `TOUKA_AUTH_SECRET`（C1）、`HEARTBEAT_TOKEN`（H5，預設空字串）
    - `SECURE_COOKIES = True`（H1/P15，**預設生產安全**）、`AUTH_RATE_LIMIT_MAX = 10`、`AUTH_RATE_LIMIT_WINDOW_SEC = 900`（H2）
    - `MAX_REQUEST_BODY_BYTES = 10 * 1024 * 1024`（P19，請求體大小上限）
*   **第三輪安全加固模組**（2026-05-21，P1~P29）：
    - **`ToukaProxy/src/middleware/security_headers.py`** `SecurityHeadersMiddleware` — 全域安全標頭注入（P11）
    - **`ToukaProxy/src/middleware/request_size_limit.py`** `RequestSizeLimitMiddleware` — 請求體大小守門員（P19）
    - **`ToukaProxy/src/utils/path_security.py`** `validate_no_symlink_escape()` — Symlink 逃逸防護（P16）
    - P28 的 Session TTL 清理（`cleanup_expired_sessions()`、`SESSION_TTL_DAYS`）已撤回（ADR-0259），對話不再自動刪除；`last_activity_at` 欄位保留
    - **`ToukaProxy/src/utils/async_db.py`** `_session_locks` dict — per-session asyncio.Lock（P5）
    - **`ToukaProxy/src/routers/connections.py`** `_connections_lock` — 連線設定 threading.Lock（P9）
    - **`ToukaProxy/src/services/autonomy.py`** `_autonomy_lock` — 配額讀寫 threading.Lock（P18）
    - **`ToukaProxy/src/services/permission_service.py`** `_config_reload_lock` — 熱重載 threading.Lock（P21）
    - **`ToukaProxy/src/services/identity_router.py`** `_ldh_limiter` — LDH token 發卡速率限制（P24）
*   **資源上限 SSOT**：
    - `MAX_PENDING_APPROVALS = 50`（M2/N5，`terminal.py`）
    - `MAX_SNS_CONTENT_LENGTH = 10_000`（M3，`sns_gateway.py`）
    - `MAX_CONTENT_LENGTH = 8000`（H3，`content_guard.py`）
*   **SQL 結構安全（N12）**：
    - `async_db.py:get_home_session_id()` — 動態 f-string placeholder 改為固定 `IN (?, ?)` 字面量
*   **前端基礎設施加固（2026-05-22）**：
    - **Storybook 完整移除**：`ToukaChat` 移除 6 個 Storybook 套件、20 個 story 檔及 MSW worker；`knip.config.ts` 更新；消除 vite 8 peer dependency 衝突，Docker build 可正常完成
    - **node-pty Docker 支援**：`ToukaChat/Dockerfile` 加入 `python3 make g++`（node-pty native 編譯用）；EXPOSE 修正為 3001；`docker-compose.yml` port 同步更新
    - **AGENT_TOKENS 鍵值匿名化**：`tokens.ts` 的主題鍵名由真實 Agent 名稱改為 Amber/Rose/Emerald，不再暴露真實 Agent 名稱
    - **Guard #25**（`scripts/guard_native_npm.py`）：靜態掃描前端 `package.json` 的 native module（`KNOWN_NATIVE_MODULES` SSOT）；驗證 Dockerfile 備妥 `REQUIRED_BUILD_TOOLS`（python3/make/g++）；15 個 TDD 測試

## 4. 依賴主權禁令 (Dependency Sovereignty) ⭐
*   **決策**: 依 ADR-0117，root 與各 workspace member 的 `pyproject.toml` 宣告依賴，root 單一 `uv.lock` 固定完整解析圖與套件來源／artifact hash；舊 `requirements.txt` 管線已退役。開發環境同步使用 `uv sync --all-packages`；產品映像仍只安裝所屬 runtime 依賴。
*   **執法**: 透過 `scripts/guard_dependencies.py` 建立隔離沙盒 (Venv) 模擬生產環境安裝與匯入，確保 100% 依賴完整性。
*   **部署義務**: 在執行 `docker-compose build` 前，必須通過依賴守衛檢查。
*   **部署等價性**: 嚴禁在 `Dockerfile` 中手動白名單式 COPY 容易遺漏的子模組。推薦使用 `COPY . .` 並搭配 `.dockerignore` 排除非必要的資料夾 (data/, backups/)。

## 5-A. 來回計算主權 (Round Count SSOT) ⭐

全系統「對話則數」以**來回（round = user+assistant pair）**為唯一單位（ADR-0036）。

- **後端 SSOT**：`ToukaProxy/src/utils/history_manager.py` `count_rounds_from_linear(messages)`
  - 跳過 system/nudge 等非對話角色
  - 末尾未配對的 user 訊息不計入
  - 所有需要計算來回數的地方必須呼叫此函式，嚴禁自行實作
- **前端對等函式**：`ToukaChat/src/lib/export-utils.ts` `countRounds(messages)` — 邏輯完全對應
- **已統一的三處計算**：
  1. `async_db.py` session `round_count` → `count_rounds_from_linear(path)`
  2. `legacy_store.py` `message_count` → `COUNT(*) WHERE role='user'`（SQL 近似值）
  3. `export-utils.ts` `splitSessionMessages` keepCount → `countRounds()` 為單位

## 5. 戰術性豁免主權 (Tactical Suppression) ⭐
*   **原則**: Linter 規則為強執法，若核心基礎建設開發需要合法存取底層資源，嚴禁使用 AST 規避 (如 `getattr`)。
*   **執法**: 必須使用行級註解 `# noqa: [RULE_ID]` 精確豁免。
*   **防護**: `lint_ssot.py` 具備 Regex 解析能力，能支援動態豁免而不破壞全域白名單安全。
*   **豁免同步驗證 (NOQA Registry Guard)** ⭐：`scripts/guard_noqa_registry.py` 自動掃描代碼庫中所有架構型 `# noqa` 指令，與 `NOQA_REGISTRY.md` 全系統統計表比對，不一致時 CI 失敗。整合至 `guard_all.py` 第 19 道關卡 (Guard #19)。（ADR-0029）
    - **Python**：使用 `tokenize` 僅計行內 COMMENT token，正確排除測試資料與說明型 comment。
    - **TypeScript**：排除純 comment 行與 `__tests__/` 目錄。
