> 本檔是給 AI 程式代理人（例如 Codex、Claude Code）與貢獻者的專案規則；個人的溝通偏好不放在這裡。

## DATA_PROTECTION（資料保護）
嚴禁開發者或代理人以任何形式自動化修改、覆寫或「管理」位於 data/下的資料，僅限使用者手動維護和擁有者更改。

開發者與代理人沒有必要時不讀取 data/；不小心讀到或搜尋到不算違規，照實回報即可。

**既有 Owner 產品授權界線**：以上限制開發代理直接修改／管理實體資料；Owner 已授權 Touka 正常 startup/runtime（含其受管初始化、migration、session、marker 與記憶寫入），屬產品行為。此區分不授權開發代理直接操作 data/ 或 soul/，也不授權任意 live probe；所有自動化測試仍一律 sandbox/mock，人工 probe 仍依下列狹義例外與當次授權。P11 歷史發布委託不構成永久 Git／部署授權。

**受管測試狹義例外（Owner 2026-08-14；ADR-0214）**：本例外只適用於經產品受管介面執行的測試／人工 probe；開發者與代理人仍不得直接修改、清理或管理 `data/`。

- 測試可以讀取實體資料，但必須能證明完整路徑為**物理唯讀**；初始化、migration、index／cache／lock／log 建立皆算寫入。不能證明時，必須改用 sandbox／mock，或停下詢問 Owner。
- 實體寫入只允許精確的 `DebugAgent` 邏輯範圍，且只限人工啟動的 live probe：由 `AgentIdentity("DebugAgent")` jail 解析出的專屬路徑、帶強制 `agent_id="DebugAgent"` predicate 的共享資料列，以及精確 `memory_DebugAgent` collection。全域 config／setting、schema／migration、registry、其他 Agent 與任何 `soul/` 寫入都不在例外內。
- `pytest`、Vitest、CI、guard 與 `run_all_smoke_tests.py` 的全部自動化測試一律使用 sandbox／mock；不得套用實體讀取或 `DebugAgent` 寫入例外。
- `DebugAgent`-scoped 表示可拋棄的受管測試資料，不等於 filesystem sandbox，也不授權代理人自行檢查或清理既有資料。

## 程式碼修改規則
- 在修改任何 Python 檔案後，必須檢查**型別標註（Type Hinting）**的匯入：
- 確認 List, Dict, Any, Optional, Union 等是否已從 typing 正確匯入。
- 若系統環境有安裝 ruff、ESLint 或其他語法檢查工具，交付前必須執行一次靜態檢查，確保沒有 undefined name（未定義名稱）報錯。
- 嚴禁在交付代碼後，讓 Docker 容器因為語法或匯入錯誤而陷入重啟循環（Restart loop）。

## SOLID 重構守則
- **前端元件拆分 (SRP & OCP)**: 嚴禁建立「上帝元件」(God Components，如超過 20 個 states 的單一檔案)。React 視圖應拆分為多個領域專屬的小元件，並透過設定檔或註冊表 (Registry) 達成 OCP，新增分頁或功能時不應修改核心佈局代碼。
- **資料層抽離 (DIP)**: UI 不應直接與 API 綁定，應將 `fetch`、`localStorage` 操作封裝成領域專屬的 Custom Hooks (如 `useXxxSettings`)。
- **依賴反轉 (DIP)**: 在後端優先使用 `typing.Protocol` 定義合約。高層邏輯不應直接依賴具體實作，應依賴抽象介面。
- **匯入規範 (Import SSOT)**: 嚴禁在方法或函數內部使用區域匯入 (Local Imports)。所有依賴必須在模組頂部定義，以確保測試時可精確進行 Patch 與 Mock。

## SSOT原則
- **禁止使用硬編碼**: 嚴禁在代碼中出現硬編碼的磁碟代號 (如 `D:/`, `C:/`) 或容器絕對路徑 (如 `/app/data`)。
- **動態解析優先**: 所有路徑必須透過 `get_app_data_dir()`、`ResourceManager` 或基於 `Path(__file__)` 的動態探測獲取。
- **查詢與諮詢**: 如果沒有確定的 SSOT 來源，可以查詢 SSOT 文件，或是詢問使用者。

## TDD 與 測試主權 (TDD & Test Sovereignty)
- **測試優先**: 增加功能或修改錯誤時，必須先建立/執行測試。
- **測試主權**: Agent 在修改核心代碼後，具備執行測試並確保 **100% Green** 的義務。
    - **後端 (ToukaProxy/Memory)**: 使用 `pytest tests/`。
    - **前端 (ToukaChat)**: 使用 `npm run test` 或 `vitest`。
- **沙盒隔離**: 所有自動化測試必須遵循 `conftest.py`、Vitest Mocks 或 standalone smoke sandbox，禁止繞過沙盒直接讀寫實體 `data/`。人工 live probe 僅能使用本檔 `DATA_PROTECTION` 的受管狹義例外，且仍須逐次取得人類同意。

## 以上不可以更改，如需更改，必須徵求使用者同意。
---
## 🔄 文件協議：任務前讀 (a) → 任務中查 (b) → 任務後更新 (c)

每個任務的三個階段都有對應的文件動作（對齊標準開發循環）：
- **(a) 任務前讀**：見下方「任務前文件閱讀協議」——動手前先讀對的文件。
- **(b) 任務中查**：實作過程中遇到 SSOT / 介面 / 架構疑問，**立即查對應文件或詢問使用者，嚴禁自行假設**；文件有衝突或不清楚時當場提出。
- **(c) 任務後更新**：見下方「任務後文件更新協議」——改動完成後同步更新文件，並由守衛執法。

**ToukaDev 流程問題紀錄**（只適用於使用 ToukaDev 開發方法論 Skill 的維護者；外部貢獻者不需要。這是開發方法論的 Skill，不是產品的 `config/skills/`）
- 流程或規則出問題時（照做或沒照做而出重大錯誤或差點出事、問題到合併後或由人類才發現、人類表示流程造成困擾或對同一件事第二次糾正、某個步驟花了成本卻沒有作用），記錄到主要專案資料夾的本機 `.toukadev/lessons/`，格式依 improvement-loop Skill；紀錄不是授權，依紀錄修改規則只能經人類啟動並逐條核准的改進任務進行。

---

## 📋 任務前文件閱讀協議 (a — Pre-Task Reading Protocol)

接到任務後，先按類型確認需要閱讀的文件：

| 任務類型 | 必讀文件 |
|---------|---------|
| **所有任務** | 本檔 **§18 開發禁令**（禁令 1–67，全系統鐵則）＋ `HANDOVER.md`（當前交班現況） |
| 新增/修改端點或認證 | `docs/architecture/PROXY_INTERFACE.md` TL;DR 區塊 |
| 安全弱點修復 | `SECURITY.md`、`docs/architecture/LEGAL_ARCH.md` |
| 記憶/工具/I/O | `docs/architecture/MEMORY_INTERFACE.md` TL;DR |
| 心跳/排程 | `docs/architecture/HEARTBEAT_INTERFACE.md` TL;DR |
| 前端（Chat） | `docs/architecture/CHAT_INTERFACE.md` TL;DR |
| SSOT 疑問 | `docs/architecture/SSOT_MASTER_MAP.md` |
| 架構決策疑問 | `docs/architecture/adr/README.md`（影響範圍欄） |

> 介面文件的 TL;DR 區塊在每份文件的第一段，閱讀 3~5 行即可獲得關鍵約束。

## 📝 任務後文件更新協議 (c — Post-Task Update Protocol)

依變動類型，完成任務後必須更新以下文件：

| 變動類型 | 必須更新 |
|---------|---------|
| 新增 `# noqa: RULE_ID` | `docs/architecture/NOQA_REGISTRY.md` 對應統計數字（Guard #19 自動驗證） |
| 修復安全弱點 | `SECURITY.md` Known Resolved Vulnerabilities 表 |
| 新增/修改 FastAPI 端點 | `openapi.json`（`python scripts/update_openapi.py`） |
| 架構層變動（新 SSOT、新守衛等） | 在 `docs/architecture/adr/` 新建 ADR，並更新 `adr/README.md` |
| 配給 ADR 編號（在程式/測試/文件引用 `ADR-NNNN`） | **ADR 編號主權**：必須**同步**建立 `docs/architecture/adr/NNNN-*.md` 並補 `adr/README.md` 索引列；編號連續無跳號，**嚴禁**引用尚未建檔的編號（Guard #28 `guard_adr_integrity.py` 自動執法） |
| API 行為變化（影響前端整合） | 對應 `*_INTERFACE.md` 的相關章節 |
| 重大功能（影響禁令或核心守則） | `AGENTS.md` 對應章節與 `HANDOVER.md` 對應章節；若是歷史記錄則寫入 `docs/CHANGELOG.md` |
| 新增測試檔 | `tests/README.md` 加一列（檔名、說明；不寫案例數，ADR-0278） |

> ⚠️ **直譯器主權（Windows / Interpreter Sovereignty）**：裸 `python` 可能解析到系統安裝的其他版本（例如全域 Python 3.14），而非專案鎖定的 **3.12 venv**（`.venv`，由 uv 管理；`.python-version` / `pyproject.toml` / `uv.lock` 維持 `==3.12.*`；Python 服務與 Chat builder 使用 digest 釘版的 `python:3.12-slim-trixie` 基底，tagged FROM 供 Guard #30 核對 Python 3.12）。**所有驗證（pytest、guard_all、lint_ssot）必須在 3.12 venv 下執行**，否則是在錯的直譯器＋錯的套件版本（例如 starlette 0.52 vs 1.1）上測，綠燈不可信。正確調用任一即可：
> - **PowerShell 先 `.\.venv\Scripts\Activate.ps1` 再執行**（**推薦**；啟用後 `.venv\Scripts` 自動置於 PATH 最前，連 guard_all 的 shell 子命令也走 3.12）
> - `.venv\Scripts\python.exe <cmd>`（直接指定；但跑 guard_all 仍須另把 `.venv\Scripts` 置於 PATH 最前）
> - `uv run <cmd>`（ADR-0117 後可用：workspace 單一 uv.lock 已含全部依賴；uv run 為 inexact 同步、不剝除既有套件）
> - **★ 佈建/更新依賴一律 `uv sync --all-packages`**：root 為虛擬協調者（無依賴），**裸 `uv sync` 會剝除全部成員依賴**（exact 同步至空 root；數秒可用 --all-packages 復原，但誤剝後測試全紅）。（ADR-0117）
>
> 注意：`guard_all.py` 以 `shell=True` 跑子命令，若沒先把 `.venv\Scripts` 置於 `PATH` 最前面（啟用 venv 即自動滿足），內部的 `python` 仍可能落到系統的其他版本（例如全域 3.14）。

> 🪟 **Windows 本機跑後端測試的前置條件（pytest、guard_all）**：GitHub CI 已在 workflow 裡備好，這段只管本機。
> - **Node／npm 版本**：PATH 上的 `node`、`npm` 要等於 `ToukaChat/package.json` 的 `engines` 與 `ToukaIDE/package.json` 的 `devEngines`（目前 Node 24.22.1、npm 11.19.1）。系統是別的版本時不改系統 Node，只替這次指令切換，例如 `fnm exec --using 24.22.1 -- python -m pytest tests/`。版本不對時 `test_mcp_modern_companion` 會 ERROR，guard_all 的前置檢查直接擋下。
> - **前端依賴**：用上面的 Node 先在 `ToukaIDE` 跑一次 `npm run install:controlled`（companion 測試要用它的 esbuild，沒裝會 ERROR）；要跑 guard_all 時 `ToukaChat` 也要跑過一次，再跑 `npm run gen:artifact-runtime`（沒有產生檔時 Guard #27 型別檢查會失敗）。
> - **Git for Windows**：9 個安裝腳本測試用 Git for Windows 自帶的 `bin\bash.exe`（從 PATH 上 `git` 的位置推算，不用 WSL 的 `bash`）；沒裝時這 9 個以「Bash is unavailable on this host」跳過。
> - **核對結尾摘要**：pytest 結尾會列出失敗、錯誤與每個跳過的原因（`pytest.ini` 的 `-rfEs`）。
>   - 已知的本機失敗：Docker Desktop 下 `test_local_compose_bindings` 有 6 個 failed（找不到 compose 外掛），以同一個 commit 的 GitHub CI 為準（ADR-0250 E1）。
>   - 條件都對時只剩 8 個環境跳過：這台機器不能建 symlink 的 4、只在 POSIX 跑的 1、只在 Linux 跑的隔離測試模組 1（`tests/integration/test_isolation_linux.py`，整個模組算一個跳過；在 Docker 跑的方法見 `tests/README.md`）、要另外同意才跑的 Docker 部署與 ToukaIDE Marketplace 打包比對各 1。
>   - 比這多就查原因，不能當成通過；新增或移除這類環境失敗或跳過時同步改這裡。

**每次任務完成後必須執行（先啟用 3.12 venv；佈建見上方直譯器主權——`uv sync` 必帶 `--all-packages`）：**
```powershell
.\.venv\Scripts\Activate.ps1
python scripts/guard_all.py   # 全綠（目前 43 guards；#42／#43 已退役）
python -m pytest tests/       # 全綠
```

**修改 ToukaChat 前端時額外執行：**
```bash
cd ToukaChat && npm run test  # vitest auth tests
```

**修改 ToukaIDE 時額外執行（compile／package 必須串行，避免共寫 `dist/`）：**
```powershell
cd ToukaIDE
npm ci --ignore-scripts
npm test
npm run typecheck
npm run compile
npm run package
```

## 🛡️ Touka系統安全與核心守則 (Core Mandates)

### 1. 脈衝驅動架構 (Pulse-Driven Architecture)
* **發布前來源契約（ADR-0230）**：Heartbeat 的 storage／排程交易／TokenProvider 共用明確失敗狀態；真人排程經 Proxy fresh human 擁有權與 If-Match CAS，不重新啟用 Proxy 排程器。fallback 只供本機管理，shared setting 未就緒時不建立工作或 outbound；兩服務以共同持久設定重啟，不承諾 hot rotation。Guard32／37 只驗必要出貨範本／明列宣告；smoke 必須在 HB import 前強制 owned data/config 與 logging 邊界。
*   **ToukaHeartbeat (Port 8082)**：系統的**「智慧節律器 (Smart Pacemaker)」**。獨立負責排程、計時與脈衝強度 (pulse_count) 決策。
*   **ToukaProxy (Port 8000)**：**「執行大腦」**。被動接收 `/awaken` 指令，執行序列化多脈衝循環。絕對禁止在 Proxy 內加回主動排程邏輯。

### 2. 身分憑證化與能力的 SSOT 解耦 (Identity & JWT Authentication) ⭐
*   **多使用者主權 (Human & Agent Dual-Track)**：系統區分「人類使用者 (Human User)」與「代理人 (Agent)」。人類擁有系統入口權，並透過 `users.json` (SSOT) 綁定其所屬的 Agent 擁有權。
*   **憑證化認證 (Gate 1)**：全系統採用 JWT (JSON Web Token) 認證機制。
*   **Bcrypt 安全盾 (Pre-hashing)**：密碼驗證實施 `SHA256 -> Bcrypt` 流程，解決 bcrypt 原生 72-byte 限制並確保密碼處理的強健性。
*   **Refresh Token 自主閥**：Agent 的背景脈衝 (Heartbeat) 受人類授權效期控制。若使用者超過 30 天未登入，系統將暫停 Agent 的離線自主權。
*   **Refresh Token Replay 防護 (P13 fix)** ⭐：Refresh token 包含唯一 `jti` claim（UUID）。`users.json` 儲存當前合法 `refresh_token_jti`；重用舊 token 時 jti 不符，系統立即撤銷全部 session，並回傳 `AUTH_REFRESH_TOKEN_REUSE`（HTTP 401）。
*   **自動領卡握手 (Auto-Handshake)**：只在系統尚未認領（首次安裝前）時，對完全沒帶 Authorization 的受信任私有網路 (含 Docker 內網) 請求實施「優雅降級」：當場核發通行證（預設 DebugAgent），放在回傳標頭 `X-New-Auth-Token`，讓首裝流程能建立認證鏈。沒登入的聊天見禁令 16。
*   **身分主權物件 (AgentIdentity)**：認證通過後，系統會生成不可變的 `AgentIdentity` 物件。所有權限判定與路徑解析必須依賴此物件，而非原始字串 ID。
*   **能力的深度繼承 (Capability Inheritance)**：身分物件不僅代表 ID，還必須同步承載該 Agent 實體 `config.json` 中的技能列表 (`enabled_tools`)。Service 層負有將物理配置資料完整透傳至 JWT 憑證中的 SSOT 義務。
*   **身分識別對映 (Identity Mapping)** ⭐：Agent ID 與 RBAC 角色的對映由 `identity_map.json` 統籌。
    - **動態角色指派**：只有靈魂、DebugAgent、DispatchAgent 能有代理角色，管理員只能為這三種 ID 指派 `Owner`、`Base` 或 `Debugger`（`Subagent` 只給子代理、`System` 只供內部）。其他 ID（自訂模型、一般模型、群聊、任意字串）一律 `Base`、沒有能力，`POST /identity` 收到這類 ID 回 422；live 設定檔裡的舊覆寫讀取時略過並記警告、不改檔，存檔時原樣保留（ADR-0245）。
    - **身分標籤 (Capabilities)**：個體可擁有 `heartbeat_awakening` (自主喚醒) 等特權標籤。授權管理已全面轉向身分能力體系，廢除過時的白名單端點。
*   **身分代理權限繼承 (Identity Assumption Optimization)** ⭐：管理員透過 **X-Agent-ID 顯式代打**（管理／除錯）模擬某個靈魂時，系統必須保留其「人類管理員」的 `admin` 角色權限（`human_role="admin"`／不繼承軀體角色，ADR-0141 §8-1 Option A）。這確保了開發者在代理模式下依然能正常存取管理 API。X-Agent-ID 只能切進靈魂：擁有者或管理員切向自訂模型、群聊、DebugAgent、DispatchAgent 或任意 ID，一律 403（ADR-0245）。
*   **前景對話身分代打 (Foreground Soul Assumption) ⭐**：**前景 UI 對話**跟「人類擁有（或 admin）的靈魂」交談時，執行身分（工具授權／記憶／vault／payload 歸屬）**必須切換為該靈魂本身**（Owner，由 `MonitorLayer._maybe_assume_soul_identity` 於 target 解析後換 `ctx["identity"]`，與心跳 `identity_service.get_identity` 同源）；人類請求者的角色僅作 **Gate-1 授權**（`owned_agents`／admin 閘，防越權代打他人靈魂）。**嚴禁**只改 `ctx["agent_id"]` 字串而留 `ctx["identity"]` 為人類（身分裂腦→`get_allowed_tools` 讀人類 `agent_role=None` 使靈魂看不到工具、`get_vault_path` 把記憶落 `custom/{human}/drive` 與心跳 `personal_drive/{soul}` 分家）。**與上一條刻意有別**：X-Agent-ID 顯式代打（管理）保 admin 權；前景交談讓 admin 亦變 Owner（去權 fail-closed），因對 admin 須變 Owner 才能統一 vault 至 `personal_drive/{soul}`。（ADR-0154）
*   **ID 歸一化 (ID Unification)** ⭐：系統全面移除 `ToukaSoul` 前綴。正式 ID 必須與物理目錄名（`soul/`）強一致。路由層與 `AgentService` 負責對舊有前綴 ID 進行糾偏映射，確保 ID 的全球唯一事實來源。
*   **角色基礎存取控制 (RBAC)** ⭐：具體權限定義於 `permissions.json` 與 `users.json`。
    - **多使用者主權**：系統區分 `admin` 與 `user` 角色。管理員具備存取 `/v1/admin/*` 的權利。
    - **權限維度**：包含原生工具 (`allowed_tools`)、MCP 伺服器 (`allowed_mcp_servers`) 與 核心技能 (`allowed_skills`)。
    - **授權閘道（ADR-0239）**：router 的角色檢查一律使用 `services/authorization_gateway.py` 的共用函式（`require_privileged`／`require_owner_or_admin`／`require_admin_authority`／`require_authenticated`／`require_admin_human`／`require_human_requester`（ADR-0249）），router 只定義保留原名的別名，不得自行實作；`Depends` 與函式內呼叫兩種形式不可互換（錯誤 body 分別回 403／422）。
*   **安全執行等級 (Security Enforcement - Gate 2 / ABAC)** ⭐：
    - **執行層攔截**：所有工具執行前必經經過驗證的身分對象校驗，確保權限與實體身分深度連動。
    - **三種模式 (Manual／Auto／YOLO，ADR-0246)**：根據環境屬性 `auth_level` 進行即時決策；唯讀類工具三種模式都直接放行。
        1. **Manual**: 其餘動作都要人在審批卡上核准 (HITL)。
        2. **Auto**: 安全類動作自動放行；終端指令與高風險工具先交給**審查員**，審查員放行才執行，轉人或審查失敗時有人在場跳卡、無人在場預設直接拒絕（不送審）；含動態 shell 寫法的指令只有人能核准。
        3. **YOLO**: 除了黑名單與底線外全自動執行。
        - 舊值讀取時對照：`ReadOnly`、`SafeAuto` 與看不懂的值當 `Manual`，`FullYOLO` 當 `YOLO`；寫入只收新名稱。**無痕對話一律 `Manual`**（無痕不留紀錄、無法追溯做過什麼）。
    - **底線 (Hard Floor)**：刪改 Touka 資料、讀金鑰、改權限設定、改原始碼、系統破壞（F1–F4、F7）三種模式都只有人能核准、無人在場直接拒絕；能力擴張與對外發言（F5、F6）在 Auto 下只有人能核准（禁令 64）。
    - **模式變更權限 (Auth Mode Authority)**：在 session 寫入時判定（`ui_sessions`）——存檔後是 `Auto` 或 `YOLO` 的對話，只有管理員或該靈魂的擁有者能寫，其他人的寫入存成 `Manual`。請求參數（前端、IDE 帶的 `auth_level`）不是模式來源；例外兩個，都受底線約束：`/debug/execute` 採用 payload 的 `auth_level`，`/v1/heartbeat/retrigger` 固定以 Auto 執行。
    - **優先級**：黑名單攔截 (Regex) > 底線 > 模式決策表。
    - **隔離層級（ADR-0272）**：代理人撰寫或指定的程式（終端、互審驗證指令、演化的建置與驗證、MCP 伺服器與創建試跑）依隔離層級執行，層級只由 `services/isolation` 判定，缺值或看不懂一律當 `none`（第三方／scaffold MCP 的直接啟動例外見禁令 66 ⑩）；依實際執行的內建工具判斷，MCP 工具和隔離動作同名也不算。
        - 層級 `none` 時，Auto 與 YOLO 都只有人能核准（審查員與 YOLO 都不能放行），無人在場直接拒絕；Manual 照舊。
        - 審批卡必須如實標示這次會怎麼執行：「在執行器裡」＝執行器容器（埠擋不全時才會出現）、「隔離降級」＝Touka 容器內的 Landlock，「未隔離」＝Touka 自己的權限；沒接 runner 的工具依工具一律標「未隔離」（創建工具已接線，見禁令 66 ⑩）；核准必須送回卡上的標示（未隔離是 `direct`）才算同意，同意綁定被核准的參數、只能用一次。
        - 有隔離不放寬任何規則：審查員看得到層級，只供參考；不經 runner 的工具（含所有 MCP 工具）審查員看到的是 `none`。
        - YOLO 無人在場時，dangerous 的 MCP 工具只有人能核准（＝拒絕）。
        - 層級 `executor`（C2-2）：代理人指令在另一個容器 `touka-executor` 裡執行；政策是 `executor` 時，這次不在執行器範圍（綁了專案、代理人資料夾不在執行器裡）或執行器不見了就拒絕，不退回 Touka 容器內的 Landlock；網路設定不安全或封鎖埠擋不全時判 `none`，經人核准後仍優先在執行器裡跑。

### 3. 來回計算主權 (Round Count SSOT) ⭐
*   **定義**：1 round = 1 個完整的 user+assistant pair。跳過 system/nudge；末尾未配對 user 不計入。
*   **唯一 SSOT**：見禁令 27。
*   **前端對等**：見禁令 27。
*   **執法**：三處計算（`async_db.py` round_count、`legacy_store.py` message_count、`export-utils.ts` keepCount）已統一，不得各自為政。（ADR-0036）
*   **插話**：插話存在 AI 回覆裡，不算回合。（ADR-0249）

### 4. 時間主權 (Time SSOT) ⭐
*   **基準定址**: 全系統時間獲取操作必須通過 `src.utils.time_utils` (後端) 或 `src/lib/time_utils.ts` (前端) 的專屬窗口。
*   **底層標準**：系統內部邏輯運算與資料庫標記一律遵守 **UNIX Epoch Time (int/ms 或 float/s)**。
*   **執法機制**：由 `lint_ssot.py` (後端) 與 `guard_time.ts` (前端) 進行靜態檢查，嚴禁調用原生 `Date` 或 `datetime` API。
*   **呈現標準**：僅在 UI、日誌輸出或 Prompt 注入時轉換為台北時間格式。嚴禁在業務邏輯中處理時區偏移。

### 4. 訊息呈現與 UX 韌性 (UX Continuity) ⭐
*   **換行保留 (Newline Preservation)**：前端渲染必須確保原始輸入中的換行符號得以保留（使用 `whitespace-pre-wrap`）。Agent 在產生回應時應善用換行提升可讀性.
*   **事件攔截主權 (UX Sovereignty)** ⭐：下拉選單等具備「確認」邏輯的 UI 交互，必須正確處理 `onSelect` 事件並視情況呼叫 `preventDefault()`，確保交互鏈不因元件預設行為（如自動關閉）而中斷。同時，必須監聽選單關閉事件即時重置內部的臨時狀態（如將 `deleteConfirmId` 歸於 `null`），防止狀態殘留。

### 4. 角色交替原則 (Role Sequence)
*   為滿足 Claude API 要求，多脈衝執行時必須在 Assistant 回覆間注入 User 提示 (Nudge)。嚴禁移除此邏輯。

### 5. 靈魂保護機制 (Soul Protection)
*   核心靈魂檔案受嚴格保護。禁止透過自動化工具篡改 `data/soul/{id}/` 下的設定。

### 6. SOLID 原則與 Middleware 結構
*   系統採用 Middleware Pipeline 結構處理脈衝生命週期。Context (含已驗證身分) 應由上層注入並傳遞。

> 📋 歷史更新日誌請見 [docs/CHANGELOG.md](docs/CHANGELOG.md)。

### 7. 資料結構與主權 (Data Structure & Sovereignty)
*   **靈魂資料 (Soul)**：存儲於 `data/soul/{agent_id}`。受 `DATA_PROTECTION` 保護。
*   **用戶配置 (Custom)**：存儲於 `data/custom/{model_id}`。
*   **舊對話存儲庫 (Legacy)**：存儲於 `data/legacy/{agent_id}/legacy_history.db`。已實體 SQLite 化，統一透過 `vault_tools(vault="legacy")` 進行高效檢索。
*   **路徑主權隔離 (Path Sovereignty Isolation) ⭐**：系統實施 **「預設監獄」** 原則。
    - **強制物件依賴**：所有 Vault 操作必須透過 `ResourceManager` 並傳遞 `AgentIdentity` 物件取得路徑。
    - **全能 I/O 代理 (Full I/O Proxy)** ⭐：所有物理檔案操作（讀取、二進位寫入、追加、CSV 管理）必須透過 `ResourceManager` 執行。嚴禁調用 Python 內建 `open()` 或 `os` 模組進行直接操作。
    - **身分正規化與糾偏 (Identity Normalization)**：底層執行層（ToolExecutor）與內部系統（MemoryManager）具備最高身分判定與糾正權。系統會自動將 Agent 可能產生的變形名稱（如：小愛/愛）統一歸一化至正式 ID（Alice），確保實體目錄定位的強一致性，杜絕安全漏洞與資料夾散落。
    - **執行生命週期原子性 (Lifecycle Atomicity)** ⭐：身分驗證僅發生於「門禁點 (Gate 1)」。一旦脈衝任務啟動並成功獲取 `AgentIdentity` 物件，該任務的後續執行（多輪 ReAct 與內部工具調用）應錨定於此物件，不受原始 JWT Token 在任務中途過期之影響。這確保了長生命週期任務的穩定性。
    - **資源配額防禦 (Resource Guarding)** ⭐：所有自主演化工具的執行均受動態資源限制。系統強制實施「時間配額 (Execution Timeout)」、「空間配額 (Storage Quota)」與「記憶體配額」，防止 Agent 產生的錯誤代碼或惡意行為耗盡系統資源。**OS 級強制（ADR-0076 / DA28）**：演化沙箱子進程在 POSIX 下以 `preexec_fn` 套用核心級 `RLIMIT_FSIZE`（儲存，取代可繞的 in-process JailedWriter）、`RLIMIT_AS`（記憶體）、`RLIMIT_CPU`（CPU 秒）；Windows 無 `resource` 模組時優雅降級回逾時＋JailedWriter。
    - **物理鎖定**：系統強制將操作範圍鎖定在該 ID 所持有的子目錄。禁止越權存取。
    - **物理路徑禁令 (Physical Path Ban) ⭐**：見禁令 41。
        - **動態錨點**：系統根目錄必須透過 `config.py` 的 `DEFAULT_WORKSPACE_ROOT` 或 `config_utils.py` 的 `get_workspace_root()` 動態獲取。
        - **環境適應**：代碼必須同時相容 Windows 與 Docker 環境，路徑拼接統一使用 `pathlib.Path`。
    - **戰術性豁免 (Tactical Suppression) ⭐**：在開發核心基礎建設時，若需合法繞過 SSOT 靜態攔截（如 I/O 監獄），嚴禁使用 AST 規避手段（如 `getattr`）。必須使用標準的單行註解 `# noqa: [RULE_ID]` 進行精確豁免，以保持代碼的 Python 慣例與可讀性。
*   **資產主權 (Asset Sovereignty) ⭐**：所有 Agent 生成的資產必須透過 `/v1/outputs/{agent_id}/{filename}` 動態路由服務。禁止直接暴露物理路徑。
*   **記憶庫是正本（ADR-0256）**：`core_memory.db` 是記憶的正本；向量索引可以重建，索引失敗只標待補、不刪記憶。記憶庫升級在 Proxy 啟動時做（啟動時沒建成、或換了嵌入模型，第一次使用時會再試一次），先備份、可重跑，失敗就停在舊版；離線工具不升級。
*   **記憶會演變（ADR-0263）**：記憶被新的取代時不刪，舊的保留並連到新的。
*   **個人空間目錄（ADR-0255）**：個人空間的 `.touka/` 由系統維護，可以從實際檔案重建。檔案動詞不能寫入；用其他方式寫入的內容，讀取時以實際檔案為準。

### 8. 大一統工具鏈 (The Grand Unification of Tools) ⭐
*   **原生合約與執行 grant（ADR-0228）**：七個 typed public tools 使用領域 Pydantic SSOT 同時產生 schema／驗證；Memory合約留Memory。runtime metadata仍由EXECUTION_CONTEXT_KWARG_NAMES分類，模型不能提升為trusted context。執行器依實際native名稱與RoleGrants核對角色權，先於PaC/HITL；schema可見／YOLO不擴角色權。Guard #45核分類與有限consumer形狀，sandbox provider/backend測試驗傳值；新欄位/action不得未分類。research三欄位及handler/schema不改。
*   **整合模式單一部署 (Integration-Only，ADR-0116)**：記憶系統 (ToukaMemory) 以同行程 Python 函式庫整合於 ToukaProxy（`from memory_server...`）。**獨立 MCP 模式已退役**——原 `_FallbackResourceProxy` 降級代理會關閉身分隔離（`get_vault_path→None`）與路徑保護（`is_protected_path→False`），故一併移除；ToukaMemory 不再作為獨立 MCP 伺服器對外。依賴反轉接縫見禁令 6、14。
*   **領域大管家**：Memory／Mission 保留領域 `action="..."` 分發；Vault／Project 的公開介面已是 9 個裸動詞＋scope，舊工具只保留可執行 shim。〔ADR-0119 的 Tier 1–3 已落地：9 個檔案裸動詞＋scope 取代舊工具廣告，Memory/Mission保留領域action；不是提案中。完整性補強見 ADR-0228。〕
*   **身份感知**: 工具執行時會自動從 Context 獲取已驗證身分，並落實主權隔離。
*   **邏輯下沉與 Schema SSOT (Logic Sinking)** ⭐：記憶與檔案庫工具的參數定義 (Schema) 與實作邏輯必須下沉至 `ToukaMemory`。Proxy 僅作為權限透傳與身分錨定的「薄橋接器」，嚴禁在 Proxy 端重複定義或修改工具簽章。
*   **MCP 2026 保守式協商、資料面與發版閘（ADR-0200～0205）** ⭐：modern-only 的 `protocol_mode` 固定 `2026-07-28`，必須真送 `server/discover` 並完整驗證後 adopt；`-32601/-32602/-32020/-32021/-32022` 與 modern validation/capability/version error 一律 fail-loud，不重連 legacy。canonical complete result 必須保留任意 JSON 與欄位 presence（含顯式 `null`）。Tools／Resources／Prompts descriptor 必須先完整 canonical 化並走完 cursor，provider 投影只能在最後一步；MCP URI 永遠是不可信識別字，`file:`／相對 URI／Windows path 不得進 filesystem；外部 prompt 固定視為 untrusted，resource/prompt 必經 MIME／bytes／DLP／權限邊界，Schema 預設 Draft 2020-12 且不得抓 external `$ref`。P7 ephemeral MRTR 必須在同一 coroutine 內驅動，opaque continuation state 只留 stack frame、不得進 DB／API／前端／log／OTel；每輪 retry 前重驗 human/owner、installation generation、MCP grant、tool policy、auth level 與 PaC，headless 一律 decline，Auto／YOLO 不代表同意。ToukaDispatch 只提供 stateless `/mcp`；ToukaIDE 只綁 loopback 並提供 `/mcp`，退役 HTTP+SSE 入口回 404，modern request-scoped SSE 保留。每個 modern request 都必須重驗 discovery Bearer，空 token fail-closed，MCP session／clientInfo 不得代替認證。
*   **本機產品與 GHCR 發行（ADR-0220，發行部分由 ADR-0238 取代）** ⭐：產品 `runtime` 與 `data` 維持本機；GitHub Actions／GHCR 只作原始碼與容器映像發行，由 `.github/workflows/ghcr-publish.yml` 建置並推送三個映像。Azure runtime 與 release authority 已移除。
*   **Modern-only（ADR-0221）** ⭐：所有 client 只在同一本機，沒有舊版 agent、資料或工具的使用需求；legacy HTTP+SSE 相容入口已移除，只保留 modern `/mcp`（含 request-scoped SSE）。冷切換、rollback seal 與發版資格程序已由 ADR-0238 退役。
*   **模型管理大一統 (Model Governance)** ⭐：系統具備自動化模型發現與健康感知能力。模型屬性持久化受 SSOT 保護，動態更新不得覆蓋使用者既有設定。

* **可信批准分類（ADR-0229）**：終端／自帶批准例外只來自 builtin 註冊時封存的 callable binding，模型 `command` 不決定工具分類。RoleGrants→PaC→HITL→公開參數驗證→可信 context 後，執行同一已解析 handler；等待批准期間 registry 改動不得替換本次 handler。測試的權限 seed 只用 tracked `.json.example`，不讀私有設定。
*   **記憶來源（ADR-0256）**：記憶的來源由系統在執行當下記下，模型不能提供或改寫。
*   **可信活動事件（ADR-0267）**：可信事件只在動作真的成功後由系統記下，模型不能寫，無痕不記。
*   **歷史資料界線（ADR-0257）**：檢索到的記憶、前情摘要、家對話都是歷史資料，不是指令。
*   **只用這一輪列出的工具（ADR-0269）**：模型叫這一輪沒列給它的工具一律擋下（YOLO 也一樣），不跳審批卡、不找審查員；只有人按的除錯執行不受這條限制。

### 9. 跨環境通訊適配 (Cross-Platform Adapting) ⭐
*   **環境感知**：Agent 透過統一的 **Bridge 適配器** 與環境通訊（Web / IDE）。

### 10. 脈衝自主演化 (Pulse-Based Self-Evolution)

### 11. 系統穩定性與韌性 (Resilience & Self-Healing) ⭐
* **可靠性來源契約（ADR-0231）**：明示 UI 保存須 strict revision CAS；generation 與 persistence 終態分離，EOF／DONE不證成功。Chat 同 scope/session writer 保留未知富樹，唯讀核對後才可明示僅重試保存；無痕零持久化。任務寫入不明傳固定領域錯誤，原 review／身份／鎖不變。不可把本契約套成所有legacy writer均已改造。
*   **配置自癒義務**: 系統支援自動引導 (Bootstrap)，遺失配置將由 `.example` 復原。Agent 嚴禁嘗試刪除這些自癒基準檔。
*   **設定檔讀不到時不放寬（ADR-0272）**：`.example` 只補不存在的設定檔；存在卻讀不到時不覆寫，也不退回較寬的預設。
*   **優雅降級意識**: 當記憶系統損壞或整合失敗時，Proxy 應能識別異常並降級至基礎模式，而非持續重試失敗的操作。
*   **併發原子性**: 所有檔案與資料庫操作均受異步佇列與物理鎖定保護，Agent 應優先使用標準 `vault_tools` 以確保資料完整性。
*   **路徑執法 (Path Enforcement)**：禁止直接存取 `soul_service.base_dir` 等屬性，所有路徑存取必須透過 `resource_manager` 解析。
*   **失效可觀察性與當機復原 (Failure Observability) — ADR-0078** ⭐：失效不沉默——工具/網路/API 錯誤與系統降級皆在介面或 agent 回覆中明示（降級時側欄常駐 `DegradedBanner`）；檔案寫入採**原子置換**（`tempfile`+`fsync`+`os.replace`）防寫到一半損毀、壞檔有 corrupt 告警；背景多脈衝迴圈當機後可於 `/v1/stats` 的 `interrupted_runs` 看見「上次 N 個未完成」並一鍵重新觸發（`POST /v1/heartbeat/retrigger`，強制 Auto、僅擁有者）。

### 12. 群聊別名與身分解耦 (Group Alias & Identity Decoupling) ⭐
*   **身分解耦與靈魂投影 (Identity Decoupling)** ⭐：系統支援透過 `@別名` 呼叫靈魂。映射由 `group_whitelist.json` 的 `ToukaSoul` 區統籌；群聊成員只限靈魂（別名必須解析到 `agents_registry` 的靈魂），原始模型 ID 與 regex 不再展開，SNS 的 `@別名` 同樣只到靈魂。live 設定裡的舊 `Native`／`Custom` 區讀取時略過並記警告、不改檔（ADR-0245）。
*   **介面合約主權與自動化同步 (Contract as Code)** ⭐：全系統對外 API 必須使用 `src/models/` 下定義的 Pydantic 模型。
    - **強型別驗證**：嚴禁處理未驗證的 `dict`。
    - **端到端同步**：後端模型為 SSOT。透過 `openapi.json` 自動同步至前端 TypeScript 型別 (`api.ts`)。**實施 `test_api_schema_sync.py` (後端) 與 `guard_frontend.ts` (前端) 確保模型與物理合約 100% 同步。**
    - **合約唯一性保護**：嚴禁在前端手動重複定義已存在於 API 合約中的領域型別，必須透過 `src/lib/types.ts` 的 Alias 進行 SSOT 引用。
*   **SSOT 糾偏**：群聊成員 ID 必須是靈魂 ID（與 `agents_registry` 強一致），系統在儲存時自動修正舊格式 ID（如 `ToukaSoulAlice` → `Alice`）。

### 13. 自主預算大一統與優先級 (Unified Autonomy Budget) ⭐
*   **預算決策鏈 (Decision Chain)**：系統在執行 ReAct 循環時，其 Slot 輪數必須經由 `AutonomyService` 判定，遵循：**請求指定 (Explicit) > 全域配置 (autonomy.json) > 系統保底 (Default: 5)**。**實施 `smoke_autonomy_enforcement.py` 驗證預算耗盡時的強制攔截與 Nudge 注入。**
*   **跨場景一致性**：標準對話與主動心跳喚醒共享同一套判定邏輯，確保代理人的行為預期在各類觸發方式下完全一致。
*   **SSOT 降級 (Fallback)**：心跳排程若未指定輪數，必須自動「優雅降級」吸納使用者在安全設定中定義的全域預算。

### 13.5 工作分派中介身分與安全邊界 (AI Agent Dispatch & Safety Boundaries) ⭐
*   **DispatchAgent 中介設計**：註冊特殊中介代理人 `DispatchAgent` (Role: `base`)。其本身不執行排程心跳，僅作為內部已存 Agent 與外部 AI 工具協作的任務載體。
*   **跨身分代理**：系統在 `identity_map.json` 中提供 `"can_supervise_dispatch"` 能力配置。持有該特權能力的 AgentIdentity 獲准繞過物理路徑隔離限制，由 `MissionManager` 代理讀寫並操作 `DispatchAgent` 的個人任務看板。
*   **自主任務狀態 FSM 約束**：狀態更新強制執行有限狀態機 (FSM) 轉換矩陣（例如必須由 `TODO -> DOING -> DONE/FAILED`，禁止跳過 `DOING` 的非法轉換，`DONE` 狀態為終端不可再次變更）。
*   **FAILED 狀態特權重置**：見禁令 31。
*   **產出物欄位解耦**：見禁令 32。
*   **專用派發伺服器 (ToukaDispatch) ⭐**：系統提供 Python SDK v2 `MCPServer` 派發伺服器（Port 8083；ADR-0082 後為 proxy 容器內建的獨立進程，由 DispatchSupervisor spawn/監督，綁定預設 127.0.0.1）。依 ADR-0221 只提供 Streamable HTTP `/mcp`，舊 `/sse`＋`/sse/messages/` 回 404；`DISPATCH_TOKEN`／mission token handshake 換取 application session，之後每次 tool call 必須顯式帶 token 並重驗 transport/audience/method/generation。所有工具呼叫的身份固定為 `DispatchAgent`，嚴禁任何形式的自動能力注入。
*   **C1 安全修復 (去除自動特權注入)**：已從 `ToukaMemory/server.py` 移除 `FORCE_AGENT_ID` 環境變數機制與 `access_mission_board_tool` 工具。見禁令 34。
*   **H1 安全修復 (BLOCKED 必填阻礙細節)**：見禁令 35。
*   **H2 安全修復 (路徑穿越防禦強化)**：見禁令 36。
*   **Mission Token 委派隔離（ADR-0064 + ADR-0201）** ⭐：`dispatch_initialize_mission` 回傳一次性 `mission_token`（與 `mission_id`／generation 綁定，首次成功 handshake 即在鎖內原子消耗）。credential store **嚴禁**保存 bearer 明文或 caller 自述資料，只存 digest 與執法分面。外部 session 的 method allowlist **嚴禁**包含 `dispatch_initialize_mission`。其餘見禁令 40。
*   **自動召喚進程隔離（ADR-0067）** ⭐：`auto_invoke` 觸發的外部 AI subprocess **必須**經 `_build_child_env` 的 whitelist（預設拒絕）淨化子環境——只保留執行必需系統 env + 三個 `DISPATCH_*`，**嚴禁**洩漏任何密鑰。`command` **必須**全來自 `dispatch_hooks.json` 白名單 list 且 `shell=False`，**嚴禁**字串拼接。新增 executor 只改設定（CaC），`dispatch_hooks.json` 部署須限 Owner 寫。
*   **Skill Anthropic 標準（ADR-0066）** ⭐：Skill 為目錄式 `config/skills/<id>/SKILL.md`（標準 frontmatter + body），Touka 擴充欄位**必須**收於 `metadata:` 子欄位以維持標準相容；`mcp_servers` 由同目錄 `.mcp.json` 的 top-level keys 推導，**嚴禁**回退至 frontmatter 自訂欄位。

### 14. 策略即程式碼 (Policy as Code, PaC) ⭐
*   **模型化主權**：全系統的安全策略與權限配置模型（Permissions, Identity, Autonomy, Safety）均必須具備強型別 Pydantic 定義。
*   **決策去中心化 (Logic Injection)**：決策邏輯應注入模型層 (`evaluate` 方法)，Service 層僅負責資料加載與生命週期管理。
*   **上下文感知決策 (Context-Aware)**：系統執行決策時必須基於結構化的 `PolicyContext`，禁止使用散落在代碼中的硬編碼邏輯判定。
*   **元數據審計義務**：所有政策配置模型必須繼承自 `BasePolicyModel`，並強制包含版本號 (`version`) 與作者 (`author`) 欄位，以支援長期的策略演進追蹤與回滾。
*   **身分與權限深度耦合**：執行層（ToolExecutor）必須將已辨識的身分實體（AgentIdentity）封裝入 `PolicyContext` 進行每輪次的權限核驗。

### 15. 大一統路由治理 (Unified Router Governance) ⭐
*   **路由對等性 (Prefix Parity)**：系統核心路由統一支援 `/v1` 前綴與根路徑對等訪問。開發者必須將新路由器註冊於 `main.py` 的 `CORE_ROUTERS` 清單中。
*   **標準化裝飾器**：所有路由器必須使用 `APIRouter(prefix="/...", tags=["..."])` 定義，並在端點裝飾器中使用相對路徑，以確保掛載時的路徑精確性。
*   **無斜線韌性 (Trailing Slash Resilience)**：系統預設啟用 `redirect_slashes`，但代碼層級應優先使用無斜線結尾的定義，確保 RESTful 介面的一致性與 CORS 穩定性。

### 17. 測試基導大一統與虛擬診斷員 (Unified Testing & Virtual Debugger) ⭐
*   **測試主權唯一化 (Test Sovereignty)**：全系統測試統一收攏於根目錄 `tests/`。
*   **DebugAgent 職責大一統 (Identity Consolidation)**：全系統唯一的開發診斷與自動化測試身分定位為 `DebugAgent`。
*   **實體工具調度能力 (Real-time Execution)** ⭐：除錯介面必須具備「真實工具調用」能力。透過 `/v1/debug/execute` 實體執行指令並將結果注入聊天流，實現「開發、除錯、驗證」的閉環。
*   **動態角色提升 (Dynamic Role Handshake)** ⭐：系統具備「開發者友善」的握手機制。在受信任的本地網域下，自動將 `DebugAgent` 識別為 `Debugger` 角色。
*   **Full-lifespan smoke 反死鎖 (ADR-0103)** ⭐：standalone smoke 若用 `with TestClient(app)` 觸發完整 lifespan，**必須**在 import 任何 `src` 之前呼叫 `lib.sandbox_env.enable_smoke_sandbox(...)`。否則真實重量級啟動（背景 bots／動態模型發現）會在 **Windows ProactorEventLoop** 留下殘留任務/執行緒，使 teardown 死鎖、卡死整套（無 Linux CI 故易漏）。`run_all_smoke_tests.py` 已有每測逾時防呆作為安全網。
*   **自動 smoke 全隔離（ADR-0214）** ⭐：所有 `smoke_*.py` 都屬 `AUTOMATED_SANDBOXED`，runner 必須為每個 child 建立獨立 sandbox，腳本也必須在任何 `src`／`memory_server` import 前自行啟用並驗證 sandbox。自動 smoke 禁止載入真實 `.env`、provider／搜尋 API key 或下載模型；真實供應商連線只能放在不符合自動 discovery pattern 的人工 live probe。

### 18. 法規即代碼 (Legal Architecture, LaC) ⭐
*   **靜態守則執行 (Automated Mandate Enforcement)**：系統將抽象的架構守則轉化為前端 ESLint 與後端 Ruff 法律。ESLint/Ruff 在 Touka 中不僅是風格檢查，更具備「主權法官」地位。
> 🚧 **開發禁令清單（Guardrails — 唯一正本）**：以下為全系統強制鐵則，**每一條都源自一次真實事故**（例如一次 SSOT 重構曾炸掉系統大部分功能）。**嚴禁繞過或弱化任何一條。** 編號為穩定識別碼——程式碼與多份 ADR 以「禁令 N」引用，**新增規則一律往後追加、不得重編**。完整脈絡見各條所附 ADR。

*   **禁令 1**：除非使用者明確要求，否則**嚴禁**將內部的記憶通訊改為 MCP 模式。專案內部必須使用整合模式。
*   **禁令 2**：**嚴禁**修改 `soul_service.py` 以再次暴露物理路徑。所有路徑操作必須依賴 `resource_manager`。
*   **禁令 3**：在修改 `ToukaMemory` 的工具時，必須同步更新其 `tools/__init__.py` 的導出清單。
*   **禁令 4**：嚴禁繞過 `AuthService` 使用原生 `bcrypt` 或 `passlib`。必須維持 `SHA256 -> Bcrypt` 的預雜湊鏈以相容長密碼。
*   **禁令 5**：**嚴禁**在工具執行器或具體工具（如 `bash`）中忽略 `auth_level` 的傳遞。所有涉及安全性決策的工具調用必須明確獲取當前 Session 的授權等級。
*   **禁令 6**：**嚴禁**在 ToukaMemory 模組中直接導入 `src.services.resource_manager`。改用 `memory_server.config_utils.get_resource_proxy()` 工廠函數（回傳 ToukaProxy 的 `ResourceManager`；ADR-0116 後為整合模式單一部署、取不到即 fail-loud，不再有 MCP 獨立降級代理）。所有 I/O 操作應透過該代理執行。
*   **禁令 7**：**嚴禁**在業務代碼中直接調用 `print()`，必須使用 `logger = logging.getLogger(__name__)`。腳本/CLI 工具（`scripts/` 目錄）除外。（PRINT_BAN 規則）
*   **禁令 8**：**嚴禁**在 `src/core/logging_config.py` 以外的任何檔案中調用 `logging.basicConfig()`。（BASICCONFIG_BAN 規則）
*   **禁令 9**：**嚴禁**在前端對 `localStorage.*` 直接呼叫（`src/hooks/**`、`src/lib/**`、`src/app/**`），必須使用 `useLocalStorage<T>()` 或領域 Hook。唯一白名單：`useLocalStorage.ts`（定義本體）、`connections.ts`（Auth Token 非 React 函數）。
*   **禁令 10**：**嚴禁**在業務代碼中用 `raise ValueError("字串")` / `raise RuntimeError("字串")` 帶硬編碼字串，必須改用 `raise ToukaException(ErrorCode.XXX)`。ToukaMemory 等跨服務邊界可加 `# noqa: RAISE_STRING_BAN`。
*   **禁令 11**：**嚴禁**在 `auth_service.py` 以外的任何檔案直接 `import bcrypt` 或 `from passlib import`（BCRYPT_IMPORT_SSOT 規則）；**嚴禁**在授權白名單（`async_db.py`, `factual.py`, `legacy_store.py`）以外直接 `import sqlite3`（SQLITE3_IMPORT_BAN 規則）。
*   **禁令 12**：**嚴禁**在新增 `# noqa: RULE_ID` 後不同步更新 `NOQA_REGISTRY.md` 的統計表。`guard_noqa_registry.py`（Guard #19）會自動驗證，不一致時 CI 失敗。
*   **禁令 13**：**嚴禁**在前端生產代碼中直接呼叫 `console.log()`、`console.warn()`、`console.error()`，必須透過 `logSSOTError()` 或 ERRORS 常數封裝層。`guard_console.ts`（Guard #15）會自動攔截。（CONSOLE_BAN，ADR-0031）
*   **禁令 14**：**嚴禁** ToukaMemory 直接 import ToukaProxy 的 `src.services.*`、`src.utils.*` 等內部模組，必須改用 `get_resource_proxy()` 工廠函數／`IResourceProxy` 依賴反轉介面。`guard_module_boundaries.py`（Guard #21）會自動偵測。**此邊界保留以維持子系統解耦**（ADR-0116 退役獨立 MCP 模式後，正當性由「MCP 獨立性」改為「依賴反轉解耦」；`config_utils`/`legacy_vault`/`vector` 的 `ResourceManager`/時間工具注入點於 `.importlinter` 明列豁免）。（ADR-0032／ADR-0116）
*   **禁令 15（密鑰主權）**：**嚴禁**在 `config.py` 或任何位置寫入公開已知的硬編碼密鑰預設值（如 `"touka_*_secret_2026"`）。歷來公開過的值列在 `config.PUBLIC_SECRET_VALUES`（`ToukaProxy/src/config.py`）。`Pydantic Settings` 的 `TOUKA_AUTH_SECRET` / `DISPATCH_TOKEN` / `HEARTBEAT_TOKEN` 預設**必須**為空字串；持久密鑰於**首次需要時隨機生成並持久化**——`AuthSecretManager`（TOUKA_AUTH_SECRET）、`DispatchSupervisor`（DISPATCH_TOKEN）與 `config._resolve_heartbeat_token()`（HEARTBEAT_TOKEN，proxy 消費端於 config 載入尾聲；因 consumer 以 `from src.config import HEARTBEAT_TOKEN` 綁常數，故須在載入尾聲完成）皆經 `config.persist_generated_secret()` 寫 `.env`；heartbeat 客戶端另有 `StorageService`（config.json）為備援解析。皆受 **`TOUKA_BOOTSTRAP_ENABLED` 閘**保護（測試/沙盒 False→不寫、退回臨時），跨重啟穩定、免精靈。⚠️ docker：heartbeat 容器**勿**用 `environment: HEARTBEAT_TOKEN=${...}` 覆寫（解析期鎖成空、壓過 env_file）——改由 `env_file: .env` 於容器建立期讀 proxy 已生成值（Guard `test_heartbeat_token_not_overridden_in_compose` 執法）。（ADR-0033 第 1 道守衛）
*   **禁令 16（身份主權）**：沒登入一律拒絕，任何標頭都不能變成身分。**嚴禁**在認證失敗或沒帶憑證時讀取 `X-Agent-ID` Header（或任何客戶端控制標頭）作為 `agent_id` 來源，也**嚴禁**改用訪客或其他替代身分放行。聊天端點（`proxy.py` 的 `/messages`、`/chat/completions`，含 `/v1`）必須掛授權閘道 `require_authenticated`，沒有已驗證身分即 401、不得進 pipeline。首次安裝前的本機領卡（LDH）是唯一例外，範圍依禁令 60，不得放寬。跨子系統信任邊界（SNS Webhook）的事件物件必須有 `is_verified` 欄位。（ADR-0033 第 2 道守衛；ADR-0245）
*   **禁令 17（結構化邊界）**：路徑檢查**嚴禁**使用 `str.startswith()` —— 必須用 `Path.is_relative_to()`（避免 sibling-directory bypass）。動態生成 Python/Shell 程式碼**必須**用 `json.dumps()` 對任何外部輸入轉義。從磁碟讀取的設定檔**必須**透過 Pydantic `model_validate()` 並含 `@field_validator`。（ADR-0033 第 3 道守衛）
*   **禁令 18（跨切面安全守衛）**：新增的跨切面安全邏輯（CSRF、Rate Limiting、內容消毒、簽章驗證等）**必須**抽出為純函式至 `ToukaProxy/src/utils/` 或 `ToukaMemory/.../utils/`，可獨立單元測試；端點層只呼叫不重複實作。資源上限**必須**以模組級命名常數定義（如 `MAX_PENDING_APPROVALS`），禁止 magic number。（ADR-0033 第 4 道守衛）
*   **禁令 19（沙箱黑名單雙保險）**：演化沙箱的 `get_clean_env()` 變更**必須**同時更新黑名單模式與顯式移除清單兩處；新增的高危環境變數（Python 解譯器控制、動態連結 loader）必須**無條件移除**，不可僅依賴前綴匹配。（ADR-0033 第 5 道守衛）
*   **禁令 20（安全文件同步）**：每修復一個新的安全弱點，**必須**同時更新 `SECURITY.md` 的 Known Resolved Vulnerabilities 表、相關 `*_INTERFACE.md`（若涉及 API 行為變化）、`SSOT_MASTER_MAP § 3-K`（若新增 SSOT 元件）。每個 CVE 編號對應一個 `test_*_security.py` 或 `test_batch*.py` 模組（TDD-Red 先行）。
*   **禁令 21（並發鎖定主權）**：對 JSON 文件（`users.json` 等）進行 read-modify-write 操作**必須**透過 `_atomic_users_write(mutate_fn)` 或等效的鎖定原語，**嚴禁**直接呼叫 `_write_users()` 或其他裸寫入方法。`user_service.py` 的所有使用者修改方法**必須**透過 `AuthService._atomic_users_write()` 執行（N4/N11 TOCTOU 修復）。
*   **禁令 22（HTTP 重定向防護）**：所有使用 `httpx.AsyncClient` 或 `requests.Session` 的業務代碼**必須**明確設定 `follow_redirects=False`，除非有明確的白名單理由。預設允許重定向可能被攻擊者利用為 SSRF 重定向鏈（N8 修復）。
*   **禁令 23（敏感資訊消毒）**：`_connection_errors` 等存儲連線失敗原因的 dict，存入前**必須**先呼叫 `_sanitize_connection_error()` 消毒。嚴禁將含有 `Bearer`、`sk-*`、`key=`、`password=` 等模式的原始 exception 字串存入任何可經 API 存取的結構（N7/N9 修復）。
*   **禁令 24（SQL 結構安全）**：SQL 查詢的結構（包含 `IN` 子句的 placeholder 數量）**必須**在開發時固定為字面量，嚴禁透過 f-string 或字串拼接動態組裝。若查詢的 IN 子句數量確定（如永遠為 2 個），直接寫 `IN (?, ?)`（N12 修復）。
*   **禁令 25（端點認證守衛強制）**：所有新建或修改的 FastAPI route handler，若非在 `endpoint_auth_allowlist.py` 豁免清單中，**必須**在函式簽名中加入至少一個認證 Depends（`get_verified_identity`、`_require_*_role()` 或 `verify_heartbeat_token` 等）。**嚴禁**新增項目至 `GRANDFATHER` 清單（清單永久維持空）——違規時 Guard #24 在 `guard_all.py` 中自動攔截（ADR-0034）。
*   **禁令 26（Heartbeat Token 驗證 SSOT）**：`/heartbeat/awaken` 的 token 驗證邏輯**必須**且**唯一**位於 `verify_heartbeat_token()` Depends 函式中。嚴禁在 `awaken_agent()` body 或任何其他位置重複實作 JWT decode 或 `HEARTBEAT_TOKEN` 比較邏輯（SOLID-SRP，由 `test_awaken_agent_body_no_longer_contains_inline_validation()` 靜態確認）。
*   **禁令 27（來回計算 SSOT）**：全系統計算「對話則數」**必須**呼叫 `ToukaProxy/src/utils/history_manager.py` 的 `count_rounds_from_linear()`（後端）或 `ToukaChat/src/lib/export-utils.ts` 的 `countRounds()`（前端）。嚴禁在業務邏輯中自行實作 user 計數或節點計數作為「則數」替代品。（ADR-0036）
*   **禁令 28（滑動視窗觸發 SSOT）**：滑動視窗剪裁**只能在一個完整對話回合結束後**觸發，授權觸發點為**兩處且僅此兩處**：(a) `_process_heartbeat_loop` 的 `finally`（所有 pulse 跑完後）、(b) `dispatcher._pump_stream_to_buffer` 於 assistant turn 持久化完成後（ADR-0092）。**嚴禁**在 pulse／串流進行中、或任何第三觸發點執行 session 剪斷；兩觸發點**必須**經 `SlidingWindowService` 的 per-session 鎖（`_get_lock`）序列化，以確保 Agent context 在任務執行中不被打斷、且不致併發改壞 session 樹。（ADR-0035 amended by ADR-0092）
*   **禁令 29 (跨身分分派代理與路徑防禦)**：**嚴禁**破壞或繞過 `can_supervise_dispatch` 的跨身分路徑代理授權。所有涉及對 `DispatchAgent` 看板的讀寫存取，必須通過 `MissionManager` 及 `get_resource_proxy()` 以身分能力（Capability）方式進行校驗，嚴禁使用硬編碼的身分特權，或在未經驗證的情況下開放跨身分存取。（ADR-0044）
*   **禁令 30 (任務狀態流轉 FSM 約束)**：自主任務看板狀態更新**必須**強制執行有限狀態機 (FSM) 轉換矩陣，嚴禁任意跳過 `DOING` 步驟直接跳轉為 `DONE`；且 `DONE` 狀態為終端狀態，封鎖後續任何修改。（ADR-0045）
*   **禁令 31 (FAILED 重置特權限制)**：任務進入 `FAILED` 狀態後，**嚴禁**無特權的外部 AI 代理人自行重置回 `TODO`。必須由具有 `can_supervise_dispatch` 特權能力的 Touka Agent，或角色為 `Owner`/`Debugger`/`admin` 的特權身份才能重設。（ADR-0045）
*   **禁令 32 (產出物欄位解耦安全)**：產出物欄位**必須**徹底解耦為單一產物 `artifact_ref` (str) 與多產物列表 `artifact_refs` (list[str])，物理儲存互不干涉，且均需經過嚴格的絕對路徑及穿越防禦，以確保前後端型別安全與 UI 渲染穩定，防止覆蓋踩踏。（ADR-0045）
*   **禁令 33 (ToukaDispatch Token 非空)**：**嚴禁**在 `DISPATCH_TOKEN` 環境變數為空時允許任何操作通過。`_check_token()` 在 expected 為空時必須直接回傳 `False`，拒絕全部請求。嚴禁為了便利在開發環境設定空 token 後忘記在部署時填入。（ADR-0046）
*   **禁令 34 (禁止恢復 FORCE_AGENT_ID)**：**嚴禁**在任何服務中恢復 `FORCE_AGENT_ID` 環境變數機制或任何形式的「憑證缺失時自動注入能力」邏輯。所有身份解析**必須**來自 `identity_map.json`（SSOT），存取控制不得依賴外部環境變數強制覆蓋。（ADR-0046 C1 修復）
*   **禁令 35 (BLOCKED 狀態必填 blocker_detail)**：任務狀態轉換為 `BLOCKED` 時，`blocker_detail` 為**必填**非空字串。`MissionManager.update_task()` 必須驗證此欄位，缺失或空值時以 `ValueError` 拒絕轉換。（ADR-0046 H1 修復）
*   **禁令 36 (Task 賦值時觸發驗證)**：`Task` Pydantic 模型**必須**保持 `model_config = ConfigDict(validate_assignment=True)`。嚴禁移除此設定，否則直接屬性賦值將繞過 `@field_validator`，導致路徑穿越防禦失效。（ADR-0046 H2 修復）
*   **禁令 37 (FileLock 非同步主權 / FILELOCK_ASYNC_SSOT)**：服務層及路由層**嚴禁**直接 `from filelock import FileLock`，必須改用 `AsyncFileLock`（`thread_local=False` 為其預設值，無需手工指定）。根本原因：`FileLock` 預設 `thread_local=True`，`acquire()` 在 `run_in_executor` 的 thread pool 執行緒中執行，`release()` 從 event loop 主執行緒執行，兩者讀取**不同** thread-local context，`release()` 讀取到 `lock_file_fd=None` 後靜默返回，OS 層 byte-range lock 永遠未釋放，症狀為後續取得在 30s timeout 後才暴露。`lint_ssot.py` 的 `FileLockAsyncRule`（`FILELOCK_ASYNC_SSOT`）自動攔截違規；新增豁免必須同步更新 `NOQA_REGISTRY.md`。（ADR-0049）
*   **禁令 38 (環境感知 .env 互斥載入)**：嚴禁破壞或繞過 `resolve_env_file_path` 與 `resolve_heartbeat_env_file_path` 的環境感知互斥載入設計。整合執行時必須強制只讀取根目錄 `.env`，分開獨立執行時必須強制只讀取子專案目錄下的 `.env`，不得混淆加載或相互踩踏覆蓋。**讀寫 `.env` 一律經 `resolve_env_file_path()`（SSOT），嚴禁硬編碼相對 `.env` 路徑**（root-cause-A：`routers/config.py` 曾以裸 `".env"` 寫入，docker 下落在容器暫存層而非 compose 讀的宿主檔）。部署可設 **`TOUKA_ENV_FILE`**（最高優先，凌駕整合/獨立偵測）指向「持久化且被真正載入」的 .env——docker compose 已改設 `TOUKA_ENV_FILE=/app/host_root/.env`，並以穩定掛載 `./:/app/host_root`（恆為 compose 目錄、含 `env_file` 載入的同一份 `.env`）**與 `WORKSPACE_ROOT` 解耦**（舊 `/app/workspace/.env` 綁 `${WORKSPACE_ROOT}`，owner 設非預期值〔如 `D:/`〕時三 token 會寫進幽靈檔）；`.env` 須為目錄掛載內普通檔（`set_key` 的 temp+rename 覆寫需之），另有 `verify_env_persistence_integrity` 幽靈檔/生效探針守衛。（ADR-0052 amended by ADR-0151）
*   **禁令 39 (Gemini 動態版本篩選)**：在 `fetch_gemini_models` 中嚴禁使用硬編碼的名單篩選 Gemini 模型。必須使用正規表達式動態比對主版本號，僅放行 `>= 2.0` 的高版本模型，排除已停用的 `1.5` 系列。（ADR-0052）
*   **禁令 40 (ToukaDispatch Token 隔離)**：**嚴禁**將 `DISPATCH_TOKEN`（全域主金鑰）傳遞給任何外部 AI 工具或超出 Touka 進程邊界的任何通訊通道。外部 AI 工具只能收到由 `dispatch_initialize_mission` 生成的 `mission_token`（一次性，與 `mission_id`／generation 綁定，首次成功 handshake 即消耗，新 mission 建立時舊 credential/session 原子失效）。Token 必須透過**環境變數**傳遞（`DISPATCH_TOKEN=<mission_token>`），嚴禁放進 prompt 明文或命令列參數（避免 `ps` 可見）。`DISPATCH_URL` 必須是完整 modern `/mcp` endpoint；application session 不可跨 `/mcp`／`/sse` transport 使用。外部 session 不得呼叫 `dispatch_initialize_mission`，違反時系統立即拒絕。（ADR-0064 + ADR-0201）
*   **禁令 41（物理路徑禁令 / TID251）**：**嚴禁**在代碼中寫死任何絕對路徑字串。所有路徑必須透過 `ResourceManager` 解析。**由 `lint_ssot.py` 的 `PATH_LITERAL_SSOT` 規則與 ruff TID251（`ruff.toml` 的路徑 API 禁用，Guard #44）靜態攔截。**（ADR-0004）
*   **禁令 42（環境變數主權 / Env SSOT）**：業務邏輯**嚴禁**直接調用 `os.environ`、`os.chdir`、`sys.path.append` 等敏感 API；子程序一律經 `services/execution_broker.py`（`spawn_exec`／`run_sync`，ADR-0239），`os.system`／`subprocess.run`／`subprocess.Popen` 由 ruff TID251（Guard #44）攔截，`tests/lint/test_security_kernel_ratchet.py` 另禁止在 broker 以外直接啟動子程序；前端**嚴禁**直接調用 `process.env` 或 `import.meta.env`，所有配置必須統一經由 `src.config`（後端）或 `src/lib/config.ts`（前端）存取。特權白名單僅限 `config.py`、`main.py`、`bootstrap.py`、`terminal.py`、`conftest.py`。**由 `lint_ssot.py`（後端）與 `guard_env.ts`（前端）進行靜態稽核。**（ADR-0015）
*   **禁令 43（時間獲取禁令 / TIME_BAN）**：**嚴禁**調用 `datetime.now()`、`time.time()` 或 `datetime.today()`，必須統一使用 `time_utils`（後端）/ `getSystemEpoch()`（前端），內部一律以 UNIX Epoch 為基準。**由 `lint_ssot.py`（AST 級）與 `guard_time.ts` 強制攔截。**（ADR-0013）
*   **禁令 44（錯誤碼主權 / Error Code Sovereignty）**：嚴禁在 Exception 或 UI 提示中使用硬編碼字串——後端**嚴禁** `HTTPException(detail="...")`（補禁令 10 的 raise 字串禁令）；前端**嚴禁** `toast.error("...")` / `console.error("...")` 硬編碼內容。`errors.json` 為跨端唯一錯誤定義來源，自動生成雙端常數。**由 `lint_ssot.py`（後端）與 `guard_errors.ts`（前端）攔截。**（ADR-0014）
*   **禁令 45（HTTP 客戶端主權 / Fetch SSOT）**：前端所有 HTTP 請求必須透過 `src/lib/connections.ts` 的 `fetchTouka()` 發送，**嚴禁**直接調用原生 `fetch()`。**由 `guard_fetch.ts`（FETCH_SSOT）AST 掃描，整合至 `guard_all.py`。**（ADR-0024）
*   **禁令 46（死碼禁令 / Dead Code Guard）**：**嚴禁**未使用的函式、export 或檔案長期存在。**後端由 `vulture`、前端由 `knip` 偵測，整合至 `guard_all.py` 第 20 道關卡。**
*   **禁令 47（依賴與部署主權）**：必須驗證各 workspace 成員在單一 `uv.lock` 下的依賴（成員 `pyproject.toml` 漏列直接依賴時沙盒匯入即失敗）與 `Dockerfile` 的同步性——由 `guard_dependencies.py` 執行沙盒匯入測試與「Dockerfile 物理完整性檢查」，作為 Docker Build 前的**強制預檢**。Guard #16 在任何依賴快取工作前，必須先依 [ADR-0019](docs/architecture/adr/0019-secure-session-management-and-deployment-parity-guard.md) 掃描 repo-local `.pytest_cache`：只驗 metadata／開檔權限、不讀內容、不追蹤 symlink／junction／reparse、拒絕 hard link 與不確定狀態；只回報相對路徑，且不得進入任何 `data/`。顯式 pytest `--basetemp` 若等於 repo 或位於其下，或 Windows 路徑無法證明安全，必須在啟動時拒絕。嚴禁在 `Dockerfile` 手動白名單式 COPY 子模組，建議 `COPY . .` 搭配 `.dockerignore`。
*   **禁令 48（會話管理主權 / Session Sovereignty）**：統一全系統身份失效邏輯——`refresh_token` Cookie **必須**設為 `path="/"`；前端請求**必須**顯式帶入 `credentials: 'include'`。
*   **禁令 49（設計主權 / Design Token Sovereignty）**：透過 Guard #4（`ToukaChat/scripts/guard_design.ts`），強制所有視覺樣式**必須**出自 `tokens.ts` 或系統定義，嚴禁任意值（arbitrary value）。
*   **禁令 50（組件主權 / Component Sovereignty）**：強制實施組件使用白名單，**嚴禁**直接引用未經封裝的底層 UI 組件（如 Radix UI 原件）。
*   **禁令 51（合約完整性監督）**：透過 AST 級別的語法檢查，**禁止**開發者在前端手動 re-declare 領域關鍵模型（前端型別必須來自後端 Pydantic 生成的合約）。
*   **禁令 52（創建主權 / Creation Sovereignty）**：原生創建引擎（`ToukaProxy/src/services/creation/`）為 MCP / Skill 創建的**唯一編排入口**。**嚴禁**：①創建路由端點缺少 `_require_creation_role`（Owner/admin）把關；②creation 套件**以外**的模組繞過引擎直呼 `.update_skill()` / `.register_server()`（allowlist：定義處 `skill_service.py`/`mcp_hub_service.py` + 既有合法呼叫者 `routers/skills.py`）；③門面 `services/creation/api.py` 依賴 fastapi（須保持傳輸無關、route 與 agent 工具雙介面共用）。**CREATE 不得覆寫既有 id**（id 衝突於 VALIDATE 關卡擋下，以引擎層硬約束保護既有 agent 資料）。由 `scripts/guard_creation_sovereignty.py`（Guard #29）自動執法。（ADR-0069）
*   **禁令 53（依賴鎖檔主權 / Dependency Lockfile SSOT）**：全系統 Python 相依以 **uv workspace 單一 `uv.lock`** 為唯一鎖檔 SSOT——各成員（ToukaProxy/Memory/Heartbeat/Dispatch）的抽象相依宣告於各自 `pyproject.toml` 的 `[project].dependencies`（人改、`~=`/`==`，**禁 `>=`**）、開發/測試工具宣告於 root `[dependency-groups].dev`。**嚴禁手改 `uv.lock`**。加/升套件**必須**：改對應 `pyproject.toml` → 跑根目錄 `uv lock` → `uv sync --all-packages` → commit 重生的 `uv.lock`。**開發與部署共用同一次解析**：dev venv＝`uv sync --all-packages`；Docker＝`uv sync --locked --no-dev`（proxy 明列四個產品 package／heartbeat `--package toukaheartbeat` 精簡子集）。**佈建嚴禁裸 `uv sync`**（exact 同步至空虛擬 root＝剝空 venv；必帶 `--all-packages`）。`torch` 由 root `[tool.uv.sources]` 綁 `pytorch-cpu` index **納入鎖檔**（CPU 版、零 `nvidia-*`；GPU 需求部署時另行覆寫）。Python 版本宣告（`.python-version`／root＋成員 `pyproject` `requires-python`／各 Dockerfile 的實際 Python APK pin／相容 `FROM python:`／`ruff target-version`）**必須**全對齊。由 `scripts/guard_environment_parity.py`（Guard #30：pyproject↔uv.lock 離線逐項比對＋版本對齊＋禁 `>=`）自動執法；Docker build 的 `uv sync --locked` 為第二道 fail-loud 防線。`guard_environment_parity.py --venv` 提供唯讀 venv-parity 諮詢（advisory、exit 0、**不進 guard_all**）。（ADR-0071 amended by ADR-0117：requirements.in/.txt 與 `lock_requirements.py`／`sync_dev_venv.py` 已退役）
*   **禁令 54（Skill Hub 同步來源主權）— 🗑️ 已退役（2026-06-07）**：persona Skill Hub 子系統已移除；此編號只保留退役墓碑，不重新啟用舊 Hub 規則。技能探索與安裝由禁令 55／ADR-0074 承接；舊規則見 ADR-0072。
*   **禁令 55（Skill Catalog 安裝主權 / Skill Catalog Install SSOT）**：真技能（能力型 `.skill`，含可執行腳本）安裝的唯一策展來源為 `config/skill_catalog.json.example`（tracked 種子，bootstrap 自癒植入、`SkillCatalogService.get_catalog()` fallback SSOT）。每筆**釘 40-hex commit-SHA、列出確切 files**。**嚴禁**：①使用者自填 repo/URL 安裝（`POST /v1/skills/catalog/install` 只收 catalog 內已審 `catalog_id`，逐檔抓 `raw.githubusercontent.com/<repo>/<commit>/<file>`）；②runtime 引入 `api.github.com`（策展工具 `scripts/curate_skill_catalog.py` 屬 **dev-time**，嚴禁進 `src/`）；③收錄非開源許可（只收 `{Apache-2.0, MIT, BSD-3-Clause}`；避開 source-available 文件類；entry `files[]` 含 `LICENSE` 保留署名）。真技能含**可執行腳本**：前端須「⚠️ 含可執行腳本」警示＋二次確認＋Owner/admin；真正防線＝「只裝人工已審、commit-pinned 來源」。沿用既有真安裝防護（`follow_redirects=False`／單檔 20MiB・總量 50MiB／路徑穿越／`.bak` 原子上線）。（原 prompt-預設型 Hub＝禁令 54 已於 2026-06-07 退役；Catalog 為現行唯一技能探索來源）。（ADR-0074）
*   **禁令 56（前端側邊欄狀態提升與 SSOT）**：**嚴禁**在 `LeftSidebar` 內部自行維護折疊狀態的 `useLocalStorage` 狀態。側邊欄折疊狀態必須由 `page.tsx` 作為單一 facts 來源進行 `useLocalStorage` 持久化，並將 `isCollapsed` 狀態單向向下傳遞給 `LeftSidebar` 展示元件，消除雙重 State 不同步隱患。（ADR-0075）
*   **禁令 57（聊天側滑動視窗觸發安全）**：聊天側剪裁觸發（禁令 28 第二觸發點）**必須**非阻塞（`spawn_logged` fire-and-forget、不得阻斷 `stream_buffer_service.finish`）、**必須**排除無痕（incognito）與子對話（僅 `is_home` 家對話、對齊 heartbeat）、**必須**經 `SlidingWindowService` per-session 鎖序列化，且任何失敗只記 log、不得影響串流終態或回應。（ADR-0092）
*   **禁令 58（前端國際化主權 / i18n Translation Catalog SSOT）**：前端**使用者可見字串嚴禁硬編碼**，必須經 react-i18next 的 `t()` 渲染（元件用 `useTranslation()`；純函式由呼叫端注入 `t`）。翻譯目錄唯一 SSOT 為 `ToukaChat/src/locales/<lng>/translation.json`（`zh-TW` 來源語言／`en` 國際參考／其餘社群語言），採語意 key；新增語言經 `src/locales/index.ts` 註冊表（放檔＋一行）。錯誤訊息必須用 `errorMsg(code)`（橋接 `errors.json` 的 `i18n` key，見禁令 44 / ADR-0014）。**嚴禁**開 `nonExplicitSupportedLngs`（會把 `zh-TW` 收斂成不存在 bundle 的 `zh`，致 `t()` 全回原始 key）；測試固定 `zh-TW`（`src/test/setup.ts`）；`fallbackLng='en'`。由 **`guard_i18n.ts`（Guard #33）** 以 zh-TW 為基準驗證跨語言 key 同步（多餘/拼錯＝錯誤、`en` 不完整＝錯誤、社群語言缺 key＝警告），併入 `guard_all.py`。資料預設值／Server Component 字串／語言 endonym 刻意不抽。（ADR-0093）
*   **禁令 59（工具失敗可見性 / Tool Failure Legibility）**：原生工具執行的失敗一律**結構化且可見**，嚴禁無聲或籠統——(a) 回給模型的工具錯誤**必須**由 `services/tool_errors.py` 的 `build_tool_error()` 產生結構化 JSON（`error_class` 為首鍵，六類：`validation_error`／`permission_denied`／`resource_not_found`／`timeout_exceeded`／`upstream_error`／`internal_error`），**嚴禁**裸中文/emoji 字串或籠統 `not registered`（「查無工具」→ `resource_not_found`；「政策攔截」→ `permission_denied`，二者不得混同）；(b) Gate 1（`ToolLayer`）因政策/角色**過濾**工具時**嚴禁無聲**——必須記錄 `ctx['denied_tools']`／`ctx['tools_filtered']` 並注入 `<tool_availability>` 說明（防模型幻覺被過濾的工具；因 pipeline 中 `ContextLayer` 先於 `ToolLayer`，說明由 `ToolLayer` 自行注入）；(c) 幻覺偵測器（`guard_service.collect_known_tool_names`，SSOT——漂移修復批 C1 自 claude_handler 私有副本下沉；ADR-0156 抽取後唯一呼叫點＝`react_core`，三 handler 為薄殼）的已知工具集**必須涵蓋 MCP 工具**，且 `permission_denied` 不得被當成幻覺；(d) 政策未載入時 `evaluate_policy` 的 fallback **必須為 DENY**（fail-safe，嚴禁偽裝成 CONFIRM）。此為原生工具介面重設計的 **Tier 1**（提案 `docs/architecture/proposals/native-tool-interface-redesign.md`；統一裸動詞＋scope 的 Tier 3 已依 ADR-0119 落地；本輪完整性補強見 ADR-0228）；可靠度量測由 `tests/tool_eval/` 骨架把關。
*   **禁令 60（LDH 無卡限定 / LDH Containment）**：本機優雅降級（LDH）**僅限「完全未附 Authorization」的本機請求**；帶著**失效 token**（過期/簽章不符）的請求一律回 401 `AUTH_INVALID_TOKEN`（不分本地/外部），**嚴禁**「壞卡當無卡」降級發卡。前端**已登入**（記憶體有 access token）時**嚴禁**採納回應標頭 `X-New-Auth-Token`（僅未登入可採納，維持首裝精靈等 pre-login 領卡相容）；access token 續期唯一路徑為 `connections.ts` 的**送出前 exp 檢查 → single-flight `refreshAccessToken()`**（`/auth/refresh`）。（ADR-0131）
*   **禁令 61（測試資料與計量 API 主權）**：任何會或可能向外部供應商發送請求並消耗 token、credit、quota 或費用的 test／eval／診斷 probe，**嚴禁**納入 pytest、Vitest、`run_all_smoke_tests.py`、CI 或 guard。若 mock 無法判定而必須 live 診斷，代理人必須先停止，揭露精確命令、script、provider、model、case／retry 數、budget 或 token 上限、送出內容類型與本機寫入範圍，取得該次或明列 bounded batch 的人類同意後才可執行。方案核准、既有 API key、環境變數、YOLO、錯誤發生或先前同意都不構成本次核准；未知是否計量時按「會計量」處理，禁止未核准的 fallback／retry。（ADR-0214）
*   **禁令 62（MCP rollout qualification 與 release evidence 主權）— 🗑️ 已退役（2026-10-08，ADR-0238）**：發版資格驗證、release evidence、seal／receipt 與 owner-local 發行機制已整體移除；此編號只保留墓碑，不重新啟用。映像發行改由 `.github/workflows/ghcr-publish.yml` 直接建置推送。
*   **禁令 63（臨時查驗沙盒限定 / Ad-hoc Probe Sandbox）**：臨時查驗（`python -c`、臨時腳本、REPL 匯入等）**嚴禁**在測試沙盒外載入專案程式（`src`、`memory_server`、ToukaHeartbeat、ToukaDispatch；含透過 `scripts/`、`tests/` 模組間接匯入）——**載入時的初始化也算寫入**（例如 bootstrap 由 `.example` 自癒寫出設定、首次需要時生成密鑰並寫入 `.env`、建立 index／cache／lock／log）。需要看值或驗證行為時，**必須**改寫成沙盒內的測試（`conftest.py` 沙盒或 standalone smoke sandbox）。經受管介面、取得當次同意的人工 live probe 不算臨時查驗，另依 `DATA_PROTECTION`。（ADR-0243）
*   **禁令 64（Auto 與底線 / Auto Mode & Hard Floor）**：①底線（F1–F7，`utils/hard_floor.py`）**必須**寫成程式常數、在 `AuthPoliciesConfig.evaluate` 最前面判斷；**嚴禁**在 `auth_policies.json` 或任何設定檔加入能關掉或放寬底線的欄位。②審查員的核准與 YOLO **都不得**推翻底線：命中底線回 `HUMAN_ONLY`，只認人類審批卡的核准（可信 `_hitl_preapproved`），無人在場直接拒絕；審查員的核准（可信 `_approved_by="auto_reviewer"`）只滿足 `REVIEW`；模型自己在參數裡帶這些欄位無效。③審查員**只看**使用者的要求（最近 3 則人類訊息；心跳用排程時寫的提示；重新觸發用最後一則人類訊息；標成資料）與這個動作（完整參數與執行環境）；**嚴禁**送入工具輸出、代理人的推理、助理文字或轉述（含子代理的派工目標）、System nudge、SNS 外人訊息與前端內嵌的附件。④審查失敗**一律不放行**：沒設模型、逾時、拒答、截斷、超預算、連不上、解析失敗或任何不是 `allow` 的判決，都轉人（有人在場）或拒絕（無人在場）。⑤審查員只經 `complete_once`、只送到所選模型自己的廠商，**嚴禁**跨廠商備援或重試。（ADR-0246）
*   **禁令 65（插話 / Interjection）**：只有對話的擁有者或管理員能插話；插話**不能**核准任何動作、**不能**改模式；工具輸出等外部內容**永遠不能**走這條路。（ADR-0249）
*   **禁令 66（隔離執行 / Agent Command Isolation）**：①代理人撰寫或指定的程式（範圍同 §2；MCP 伺服器依本條 ⑩ 分類啟動）一律經 `services/isolation` 的 runner 啟動：spawn 前才取得、spawn 後 `plan.release()`，所有 spawn `close_fds=True`；exec 類呼叫只准出現在 `sandbox_kernel/launcher.py`。②啟動器只用標準函式庫、以 `python -I -S` 執行；容器內只從映像裡唯讀的 `TOUKA_SANDBOX_KERNEL_DIR` 載入，和原始碼雜湊不同就當 `none`。③永不授權：規則路徑先 resolve 再判斷、拒絕符號連結，專案根任何一段是符號連結或 junction 就整個拒絕；Touka 目錄（定義見 ADR）底下只有使用者自建的頂層資料夾能當專案、其餘全部受保護，名稱比對不分大小寫；工作區根不能當專案根；sandbox state 根受保護；專案根只來自 session／ctx，**嚴禁**讀模型給的參數。④隔離不可用時 fail-closed：拿不到 runner 就不執行；沒經人在標示過的審批卡上核准，**嚴禁**降級成較弱的方式執行。⑤沒接上 runner 的工具一律當 `none`（`ISOLATION_WIRED_ACTIONS`）；新增動作必須在同一個 PR 接上 runner，不能只改清單。⑥執行器（C2-2）：伺服器只准標準函式庫、`execution_broker` 與彼此（kernel 依路徑載入，不讀環境變數、不載入 `src.config`）；控制通道的權杖只從 ipc 檔讀，**嚴禁**放進 argv、環境變數或 log；accept **必須**先查 `SO_PEERCRED` 才算連線上限；START 驗證全部 fail-closed（讀寫只限這次的目標與執行器 state、env 只收精確名稱允許清單）；判 `executor` 必須 READY 的版本、kernel、伺服器程式與封鎖埠雜湊、roots、自測、ABI 全部相符。⑦失敗代碼只認啟動器與 relay 的狀態 fd，**嚴禁**再從指令輸出解析；啟動器、relay、自測與執行器一律用 `trusted_interpreter()`；隔離邊界（`sandbox_kernel/**`、`services/isolation/**`）**嚴禁**使用 `child_python_env`、`get_full_env`、`get_clean_env`。⑧封鎖埠只由 monitor 算（從設定、`connections.json`、路由快照、MCP registry 推導，只增不減）；讀不到或超過上限**必須**判 `none`，**嚴禁**截斷；讀 `connections.json` 只取 `url`，**嚴禁**記下金鑰或例外原文。⑨權限自癒只在白名單內補自己擁有的 inode 的擁有者位元（執行器自己的 W6 inode 另外拿掉群組與其他人的位元，只會變嚴），**嚴禁**修 Touka 自己的狀態或受保護路徑、**嚴禁**換掉原本的例外；ResourceManager 每個碰檔案的公開方法都要有 `permission_repair` 或豁免裝飾器。⑩MCP（C2-3，前提 `mcp>=2.2.0`）：`origin`／`sandbox_paths` 只存於 `mcp_server_isolation.json`，**嚴禁**寫進 `server_config.json`；新增或改程式時先寫隔離設定檔再寫伺服器設定，隔離紀錄讀不到時**嚴禁**覆寫。state 段名由 Touka 依伺服器名雜湊，自建與第三方分開父目錄。stdio MCP 伺服器一律由 `services/isolation/mcp_launch` 判定；包住的經 runner 與 `protocols/mcp/isolated_stdio` 啟動，判定准許的直接啟動才走 SDK。來源只由各寫入點依程式路徑寫入，**嚴禁**由模型參數、前端或匯入指定；設定頁只能把第三方或未標示改成代理人建立。非人類或匯入改 env 時撤銷第三方來源與資料夾授權，必須先遮罩回填再比較，只有遮罩回填不算改動；未提供 human 時按非人類處理。任一啟動參數或 path-like env 指到代理人可影響的位置一律當自建，包含資料夾與尚不存在的路徑；程式參數中的現存資料夾從嚴分類，解析不得改寫啟動 argv（既有哨兵除外）；來源不明或紀錄讀不到不得放寬。自建在執行器或本機 Landlock 啟動，只寫自己的 state、部署程式資料夾唯讀；層級 none **嚴禁**自動啟動，只有人確認這次去處與保護能力、版本及條目雜湊後才能啟動，同意只用一次。第三方與 scaffold 看本機 Landlock；可用就包住，引用路徑與額外允許資料夾先過永不授權，APPDATA 只對映到 personal_drive，引用路徑所在資料夾是家目錄或根目錄時不給；因暫時性原因等待過久須標示「暫停」與原因；還在檢查或暫時故障時**嚴禁**先未隔離啟動，穩定的不可用原因才可不經人類同意直接啟動並標示「未隔離」（④ 的唯一例外，僅限第三方與 scaffold）。包住時條目 env 只進 spec 的 `exec_env`、套完限制後交給目標，**嚴禁**進啟動器自己的環境，也不能覆蓋 profile 管的變數。部署前後的程式須核對雜湊一致。已部署能力**嚴禁**再演化；驗證須真的收到 MCP 工具的非錯誤回應，**嚴禁**比對輸出成功字串。創建試跑的 SMOKE／EVAL 共用一次 grant，scaffold 只看本機層級；HTTP 沒有降級同意，none 不試跑。有隔離時 YOLO 依層級執行，none 仍只有人能核准；未隔離卡沒完整顯示執行內容時**嚴禁**核准。由 `test_mcp_*_security.py`、`test_evolution_deployed_freeze_security.py`、`test_evolution_verify_protocol_security.py`、`test_unisolated_card_completeness_security.py`、`test_launcher_exec_env_security.py`、`test_creation_isolation_grant_security.py`、`tests/lint/test_security_kernel_ratchet.py`、`test_executor_server_imports.py`、`test_isolation_boundary_no_full_env.py`、`test_resource_manager_permission_repair.py`、`tests/unit/test_isolation_*_security.py`、Linux 隔離測試與 `.github/workflows/isolation.yml` 執法。（ADR-0272；ADR-0275）
*   **禁令 67（Hook / ADR-0292）**：①hook 只能擋下或要人確認：**嚴禁**採用 `allow`、`updatedInput` 一類放行或改寫的欄位，hook 也**不能**核准審批卡、推翻底線或改寫已核准的參數；PreToolUse 排在政策判定之後，hook 的「要人確認」不送審查員、無人在場直接拒絕，Auto 與 YOLO 都不能代替人。②`hooks.json` 只經 `PUT /v1/hooks` 改（人類 admin、revision 比對、CSRF Origin），**嚴禁**放進設定匯出／匯入；代理人讀寫屬底線 F2（http 標頭可能有權杖）。③command hook **嚴禁**經 shell：只以程式＋參數清單經 `execution_broker.spawn_exec` 啟動，事件內容只從標準輸入給，環境不含金鑰；http hook 不跟隨轉址、有逾時與大小上限，標頭照字面送出、不替換環境變數。④參數裡的路徑必須在設定資料夾的 `hooks/`，程式在別處時**嚴禁**指到代理人寫得到的地方（`services/hooks/paths.py`），存檔與每次執行前都檢查，讀不到狀態一律拒絕；一般使用者的專案根**嚴禁**放在 Touka 保護的位置（`isolation/profiles.project_root_reason`）。⑤回給模型的 hook 輸出一律當資料（`<hook_output>` 區塊或 `hook_output` 鍵，先中和區塊 tag）。⑥hook 自己失敗或逾時不擋動作、但要記在執行紀錄；無痕只記時間、事件與結果。⑦UserPromptSubmit 擋下的內容**嚴禁**換條路進到模型或對話：插話進佇列前也要檢查，SNS 外人訊息被擋下時不寫進對話；工具 hook 也要比對舊工具名稱對應的新名稱。由 `test_hook_paths_security.py`、`test_project_root_protected_security.py`、`test_hook_runners_security.py`、`test_hook_wiring_security.py`、`test_hooks_router_security.py`、`test_interjection_endpoint_security.py`、`test_sns_gateway.py` 執法。（ADR-0292）

> 🛡️ **資訊安全主題摘要（禁令 15–26 之背景）**：歷史審計批次與修補細節見 `SECURITY.md`；以下七個跨切面安全原則持續有效（ADR-0033 + ADR-0034）：① **密鑰主權**（無公開已知硬編碼預設密鑰，短壽命密鑰啟動生成、持久密鑰首次隨機生成並持久化）；② **身份主權**（沒登入一律拒絕、禁任何 Header 作身份來源、狀態修改端點必加角色守衛 N1/N2/N3/N10）；③ **結構化邊界**（`is_relative_to()`、Shell metachar CONFIRM、`json.dumps()` 轉義、Pydantic `model_validate()`、固定 SQL 字面量 N12）；④ **統一安全守衛層**（純函式抽出、並發鎖定原語 N4/N5/N11、`follow_redirects=False` N8）；⑤ **沙箱白名單優先**（黑名單+顯式移除雙保險、連線錯誤消毒 N7/N9）；⑥ **端點認證強制**（Guard #24、Grandfather List 清零，ADR-0034）；⑦ **服務 Token SOLID-SRP**（`verify_heartbeat_token()` Depends 唯一）。完整弱點清單與修復追蹤見根目錄 `SECURITY.md`；每項對應一個 `test_*_security.py`／`test_batch*.py`（TDD）。
