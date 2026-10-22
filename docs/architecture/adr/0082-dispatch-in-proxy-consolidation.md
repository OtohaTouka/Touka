# ADR-0082: ToukaDispatch 併入 ToukaProxy 容器（單一映像 + proxy-spawn 監督）

**狀態 (Status):** ✅ 已接受（依賴機制取代 by [ADR-0117](0117-uv-workspace-single-lock-migration.md)：requirements.in/txt 依賴 SSOT＋Guard #16/#30＋deps-parity 測試→pyproject 單一 uv.lock 全面重寫；容器整併決策本身不變）
**日期 (Date):** 2026-06-12
**決策者 (Deciders):** 維護者

## 上下文 (Context)

為發佈容器映像（ghcr.io）盤點拓撲時發現三個結構性問題：

1. **ToukaMemory 源碼重複**：proxy 與 dispatch 都以「函式庫＋共寫 file-locked `data/`」方式內嵌 ToukaMemory（ADR-0049）。若各自打包成自足映像，同一份源碼要進兩顆 image。
2. **dispatch 的存在形態**：ToukaDispatch 是標準 FastMCP SSE 伺服器，真正用途是給**容器外的外部 AI**（被 ADR-0067 召喚的 claude）連任務看板——proxy 內部 agent 早有原生 `mission_tools.py`，proxy 對 dispatch 並**無** MCP client 連線（proxy 的 MCP client 是 stdio-only；`config/skills/touka-dispatch-coordinator/.mcp.json` 的 `${DISPATCH_URL}` 是給外部 claude 用、由 dispatch 注入子程序 env）。一個只服務外部回連的薄 MCP，不需要獨立容器與獨立映像。
3. **ADR-0051 回呼在 Docker 模式下本來就壞**：compose 從未給 dispatch 容器設 `TOUKA_PROXY_URL`（`server.py` 預設 `http://localhost:8000`，跨容器是錯的），且 `routers/dispatch_callback.py` 僅接受 localhost 來源——URL 與來源檢查雙重不通。同容器化後兩者**自然變正確**。

維護者決定：**proxy + ToukaMemory + ToukaDispatch 合併為單一映像**；dispatch 由 proxy 啟動時 spawn 並監督（proxy-spawn）；**功能與安全一律不減、外部召喚保留**。

## 決策方案 (Decision)

### 1. 源碼不搬家（SSOT）
`ToukaDispatch/` 仍是 dispatch 源碼唯一所在（`server.py`、`session_manager.py`、`config/dispatch_hooks.json`、`requirements.in/txt`——後者為 Guard #16/#30 硬性要求保留）。僅刪除其**打包工件**：`Dockerfile`、`.dockerignore`（standalone compose 原本即不存在）；`ToukaProxy/.dockerignore` 一併刪除（root context 下不再生效，由根版取代）。

### 2. DispatchSupervisor（proxy-spawn，SRP）
新增 `ToukaProxy/src/services/dispatch_supervisor.py`，只負責 dispatch 子程序生命週期：

- **spawn**：`asyncio.create_subprocess_exec([sys.executable, <server.py>])`，行程群組 kwargs 與 `tools/terminal.py` 同款（win32 `CREATE_NEW_PROCESS_GROUP`／POSIX `start_new_session`）；注入 env：`DISPATCH_BIND_HOST`、`DISPATCH_PORT`、`DISPATCH_TOKEN`、`TOUKA_PROXY_URL=http://127.0.0.1:<proxy_port>`、`MEMORY_APP_DATA_DIR`、`PYTHONPATH`（含 `<ToukaMemory>/src`）；cwd＝dispatch 目錄（`_invoke_external_ai` 解析 hooks 與子程序 cwd 需要）。
- **就緒探測**：TCP connect 輪詢（FastMCP `run(transport="sse")` 內部 uvicorn 綁 port 即就緒）；逾時**僅警告、不擋 proxy 開機**（看門狗會繼續重試）。
- **看門狗**：`spawn_logged()` 跑監視迴圈，崩潰→封頂指數退避重生；`stop()` 先停看門狗再 `terminate_process_tree(proc)`。
- **不註冊 `process_registry` session 表**：避免 dispatch 變成可被 `/stop` 端點殺掉的偽 session；supervisor 自持 proc 參考。
- **lifespan 接線**（`main.py`）：autonomy 掃描後、`bot_service.start_bots()` 前，`if settings.DISPATCH_AUTOSPAWN` 啟動；關閉順序 `stop_bots()` → `protocol_manager.cleanup()` → `supervisor.stop()`。
- **觀測**：`/stats` 增 `dispatch_supervisor: {status, pid, restarts}`（回應為 `dict[str,Any]`，OpenAPI 零變動）。

### 3. 設定（ENV SSOT＝`src/config.py` pydantic Settings）
新增 4 欄：`DISPATCH_AUTOSPAWN: bool = False`（**預設關**——1500+ 測試的 TestClient lifespan 不得連環 spawn；compose 顯式開）、`DISPATCH_PORT: int = 8083`、`DISPATCH_TOKEN: str = ""`、`DISPATCH_SERVER_PATH: str = ""`（空值→`<repo根>/ToukaDispatch/server.py`，native 與容器同一條規則）。執行路徑一律讀 `settings.X` 屬性（不用 legacy 模組常數匯出，保測試可 monkeypatch）。

### 4. server.py 最小改動（secure by default）
抽 `_bind_host()`：env `DISPATCH_BIND_HOST`，**預設 `127.0.0.1`**（原硬編 `0.0.0.0`）。`# noqa: S104` 移除、新增一處 `ENV_SSOT_VIOLATION`（registry 同步：計數列 14→15；dispatch 條目措辭由「獨立容器入口點」改「獨立進程入口點」）。

### 5. 單一映像與 compose 拓撲
- **`ToukaProxy/Dockerfile`**：build context 改 repo 根；維持 `COPY . .`（禁令 47：黑名單不挑檔）；`WORKDIR /app/ToukaProxy`；`COPY ToukaProxy/requirements.txt` 先行保 layer cache。映像同時含 ToukaProxy／ToukaMemory／ToukaDispatch。
- **新 root `.dockerignore`**（安全關鍵）：排除 `.git`、`node_modules`、`ToukaChat/`、`ToukaHeartbeat/`、`docs/`、`tests/`、`data/`、`backups/`、`.venv`、快取、**`.env`**、**`ToukaProxy/config/*.json`（保留 `*.json.example`）**——密鑰與活體 config 絕不烤進映像；`ToukaDispatch/config/`（dispatch_hooks.json）必須隨映像出貨。
- **root build-context 可讀性邊界**：本 ADR 的 root context 會在 Dockerfile 執行前由 BuildKit 讀取；Guard #16 依 [ADR-0019](0019-secure-session-management-and-deployment-parity-guard.md) 先做窄範圍 pytest-residue preflight，實際 image build 才是整體 context 的第二道證明。守衛不得因此遍歷 `data/` 或宣稱已完整探測 build context。
- **`.gitignore` 修正（實作中發現的既有潛在漏洞）**：原 `.gitignore` 把所有 `.dockerignore` 排除在版控外——黑名單**從未進過 git**，fresh clone 上 build 會把 `.env`/活體 config 烤進映像。已自根與 ToukaProxy 的 `.gitignore` 移除該規則，並以 `tests/lint/test_dockerfile_dispatch.py` 釘住「黑名單存在＋排除密鑰＋不得被 gitignore」契約（詳 `SECURITY.md` T2）。
- **compose**：整段刪除 `touka-dispatch` 服務（含 ADR-0050 後即無用的 `IDENTITY_MAP_PATH` 殘留）；proxy 服務加 `DISPATCH_AUTOSPAWN=true`、`DISPATCH_TOKEN` passthrough、`DISPATCH_BIND_HOST=0.0.0.0` 並把 **8083 發布移到 proxy**（保留「主機原生外部 AI 連 Docker dispatch」既有流程；全內網部署可自行移除發布＝攻擊面再縮）；dev volumes 重指 `/app/ToukaProxy/*`、`/app/ToukaMemory`、`/app/ToukaDispatch`。
- **依賴**：`filelock` 由 proxy 的轉依賴升格為直接依賴（`requirements.in` 加 `filelock~=3.29.0`，重鎖）；新增 deps-parity 測試（dispatch 的 `requirements.in` 套件名 ⊆ proxy 鎖檔釘版）防未來漂移。

## 安全／功能等價性論證

| 項目 | 之前 | 之後 |
|---|---|---|
| DISPATCH_TOKEN 握手＋固定 DispatchAgent 身份＋ContentGuard H3＋子程序 env 白名單＋非 root | ✅ | ✅ 完全不變（即使 localhost 也保留 token 握手，defense-in-depth） |
| 外部 AI 自動召喚（ADR-0067） | native 可用；Docker 實際不可用（dispatch 容器無 node/claude CLI） | native 不變；Docker 同前（不在映像裝 claude CLI，列未來增強）→ **不減** |
| 主機原生外部 AI 連 Docker dispatch | 8083 發布於 dispatch 容器（容器內綁 0.0.0.0） | 8083 發布於 proxy 容器＋compose 顯式 `DISPATCH_BIND_HOST=0.0.0.0` → **等價**；程式碼預設改 127.0.0.1 → native 預設**更安全** |
| ADR-0051 完成回呼（Docker） | ❌ 壞（URL 跨容器錯 + 來源檢查拒收） | ✅ **修復**（同容器 localhost 雙雙成立） |
| 映像密鑰 | —（映像不自足，靠掛載） | root `.dockerignore` 排除 `.env`／活體 config／`data/` → 映像不含密鑰 |

## 後果與影響 (Consequences)

- **映像 4 → 3 顆**（proxy[含 memory+dispatch]、heartbeat、chat）；ToukaMemory 源碼單份；跨容器 token 傳遞消失。
- **取捨（接受並記錄）**：proxy 重啟時 `terminate_process_tree` 會連坐 dispatch 整棵程序樹（含進行中的外部 claude 任務）——此為一致的監督語意；任務看板狀態在 `data/` 落盤，不丟。
- **守衛**：Guard #30 Check 2 對 Dockerfile 是 `if exists()` → 刪 `ToukaDispatch/Dockerfile` 即跳過，守衛免改；Guard #16 依 ADR-0019 增加 repo-local pytest-residue fail-fast preflight，既有依賴與 Dockerfile parity 行為不變；Guard #30 因 `ToukaDispatch/requirements.*` 保留而不變；Guard #19 NOQA registry 同步（ENV_SSOT 14→15、S104 敘述移除）；Guard #17 不受 `/stats` 加欄影響（dict 回應）。
- **測試**：既有 dispatch 行為測試（unit×8＋integration×2）**零修改**續綠；新增 supervisor／binding／deps-parity／lifespan 四測試檔。打包契約 lint 測試隨拓撲更新：`tests/lint/test_dockerfile_dispatch.py` 改驗「dispatch 隨單一映像出貨＋映像密鑰黑名單」、`test_dockerfile_base_images.py` 的服務清單移除 ToukaDispatch（無獨立 Dockerfile）。
- **native 模組化模式不變**：`DISPATCH_AUTOSPAWN` 預設 False，手動跑 `python ToukaDispatch/server.py` 照舊。

## Action Items
1. [x] ADR-0082 + 索引。
2. [x] 紅燈測試（supervisor／binding／deps-parity／lifespan）+ conftest `DISPATCH_AUTOSPAWN=false` 安全線。
3. [x] config.py 4 欄、`_bind_host()`、`dispatch_supervisor.py`、main.py 接線、`/stats` 欄位、NOQA registry 同步。
4. [x] `filelock~=3.29.0` 升格 + 重鎖。
5. [x] root `.dockerignore`、Dockerfile root context、compose 拓撲、刪 4 打包工件。
6. [x] 文件同步（README／SSOT map／部署文件／SECURITY.md／.env.example）。
7. [x] ruff＋ESLint＋lint_ssot＋guard_all 31/31＋pytest 全量＋smoke 17 全綠。
8. [ ]（未來增強）映像內建 claude CLI 使 Docker 模式 auto-invoke 可用；全內網部署移除 8083 發布的 compose overlay 範本。
