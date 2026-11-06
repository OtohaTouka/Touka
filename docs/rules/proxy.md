# Proxy規則

範圍：`ToukaProxy/**`：API、工具、模型層、對話流程。

從根 `AGENTS.md` 搬來的全文，沒有改寫。跑 `python scripts/applicable_rules.py --print-rules` 看這次改動適用哪幾條，不必讀完整份。改規則見 [README](README.md)。

## 禁令

*   **禁令 2**：**嚴禁**修改 `soul_service.py` 以再次暴露物理路徑。所有路徑操作必須依賴 `resource_manager`。
*   **禁令 5**：**嚴禁**在工具執行器或具體工具（如 `bash`）中忽略 `auth_level` 的傳遞。所有涉及安全性決策的工具調用必須明確獲取當前 Session 的授權等級。
*   **禁令 8**：**嚴禁**在 `src/core/logging_config.py` 以外的任何檔案中調用 `logging.basicConfig()`。（BASICCONFIG_BAN 規則）
*   **禁令 11**：**嚴禁**在 `auth_service.py` 以外的任何檔案直接 `import bcrypt` 或 `from passlib import`（BCRYPT_IMPORT_SSOT 規則）；**嚴禁**在授權白名單（`async_db.py`, `factual.py`, `legacy_store.py`）以外直接 `import sqlite3`（SQLITE3_IMPORT_BAN 規則）。
*   **禁令 19（沙箱黑名單雙保險）**：演化沙箱的 `get_clean_env()` 變更**必須**同時更新黑名單模式與顯式移除清單兩處；新增的高危環境變數（Python 解譯器控制、動態連結 loader）必須**無條件移除**，不可僅依賴前綴匹配。（ADR-0033 第 5 道守衛）
*   **禁令 26（Heartbeat Token 驗證 SSOT）**：`/heartbeat/awaken` 的 token 驗證邏輯**必須**且**唯一**位於 `verify_heartbeat_token()` Depends 函式中。嚴禁在 `awaken_agent()` body 或任何其他位置重複實作 JWT decode 或 `HEARTBEAT_TOKEN` 比較邏輯（SOLID-SRP，由 `test_awaken_agent_body_no_longer_contains_inline_validation()` 靜態確認）。
*   **禁令 28（滑動視窗觸發 SSOT）**：滑動視窗剪裁**只能在一個完整對話回合結束後**觸發，授權觸發點為**兩處且僅此兩處**：(a) `_process_heartbeat_loop` 的 `finally`（所有 pulse 跑完後）、(b) `dispatcher._pump_stream_to_buffer` 於 assistant turn 持久化完成後（ADR-0092）。**嚴禁**在 pulse／串流進行中、或任何第三觸發點執行 session 剪斷；兩觸發點**必須**經 `SlidingWindowService` 的 per-session 鎖（`_get_lock`）序列化，以確保 Agent context 在任務執行中不被打斷、且不致併發改壞 session 樹。（ADR-0035 amended by ADR-0092）
*   **禁令 37 (FileLock 非同步主權 / FILELOCK_ASYNC_SSOT)**：服務層及路由層**嚴禁**直接 `from filelock import FileLock`，必須改用 `AsyncFileLock`（`thread_local=False` 為其預設值，無需手工指定）。根本原因：`FileLock` 預設 `thread_local=True`，`acquire()` 在 `run_in_executor` 的 thread pool 執行緒中執行，`release()` 從 event loop 主執行緒執行，兩者讀取**不同** thread-local context，`release()` 讀取到 `lock_file_fd=None` 後靜默返回，OS 層 byte-range lock 永遠未釋放，症狀為後續取得在 30s timeout 後才暴露。`lint_ssot.py` 的 `FileLockAsyncRule`（`FILELOCK_ASYNC_SSOT`）自動攔截違規；新增豁免必須同步更新 `NOQA_REGISTRY.md`。（ADR-0049）
*   **禁令 39 (Gemini 動態版本篩選)**：在 `fetch_gemini_models` 中嚴禁使用硬編碼的名單篩選 Gemini 模型。必須使用正規表達式動態比對主版本號，僅放行 `>= 2.0` 的高版本模型，排除已停用的 `1.5` 系列。（ADR-0052）
*   **禁令 48（會話管理主權 / Session Sovereignty）**：統一全系統身份失效邏輯——`refresh_token` Cookie **必須**設為 `path="/"`；前端請求**必須**顯式帶入 `credentials: 'include'`。
*   **禁令 52（創建主權 / Creation Sovereignty）**：原生創建引擎（`ToukaProxy/src/services/creation/`）為 MCP / Skill 創建的**唯一編排入口**。**嚴禁**：①創建路由端點缺少 `_require_creation_role`（Owner/admin）把關；②creation 套件**以外**的模組繞過引擎直呼 `.update_skill()` / `.register_server()`（allowlist：定義處 `skill_service.py`/`mcp_hub_service.py` + 既有合法呼叫者 `routers/skills.py`）；③門面 `services/creation/api.py` 依賴 fastapi（須保持傳輸無關、route 與 agent 工具雙介面共用）。**CREATE 不得覆寫既有 id**（id 衝突於 VALIDATE 關卡擋下，以引擎層硬約束保護既有 agent 資料）。由 `scripts/guard_creation_sovereignty.py`（Guard #29）自動執法。（ADR-0069）
*   **禁令 55（Skill Catalog 安裝主權 / Skill Catalog Install SSOT）**：真技能（能力型 `.skill`，含可執行腳本）安裝的唯一策展來源為 `config/skill_catalog.json.example`（tracked 種子，bootstrap 自癒植入、`SkillCatalogService.get_catalog()` fallback SSOT）。每筆**釘 40-hex commit-SHA、列出確切 files**。**嚴禁**：①使用者自填 repo/URL 安裝（`POST /v1/skills/catalog/install` 只收 catalog 內已審 `catalog_id`，逐檔抓 `raw.githubusercontent.com/<repo>/<commit>/<file>`）；②runtime 引入 `api.github.com`（策展工具 `scripts/curate_skill_catalog.py` 屬 **dev-time**，嚴禁進 `src/`）；③收錄非開源許可（只收 `{Apache-2.0, MIT, BSD-3-Clause}`；避開 source-available 文件類；entry `files[]` 含 `LICENSE` 保留署名）。真技能含**可執行腳本**：前端須「⚠️ 含可執行腳本」警示＋二次確認＋Owner/admin；真正防線＝「只裝人工已審、commit-pinned 來源」。沿用既有真安裝防護（`follow_redirects=False`／單檔 20MiB・總量 50MiB／路徑穿越／`.bak` 原子上線）。（原 prompt-預設型 Hub＝禁令 54 已於 2026-06-07 退役；Catalog 為現行唯一技能探索來源）。（ADR-0074）
*   **禁令 57（聊天側滑動視窗觸發安全）**：聊天側剪裁觸發（禁令 28 第二觸發點）**必須**非阻塞（`spawn_logged` fire-and-forget、不得阻斷 `stream_buffer_service.finish`）、**必須**排除無痕（incognito）與子對話（僅 `is_home` 家對話、對齊 heartbeat）、**必須**經 `SlidingWindowService` per-session 鎖序列化，且任何失敗只記 log、不得影響串流終態或回應。（ADR-0092）
*   **禁令 59（工具失敗可見性 / Tool Failure Legibility）**：原生工具執行的失敗一律**結構化且可見**，嚴禁無聲或籠統——(a) 回給模型的工具錯誤**必須**由 `services/tool_errors.py` 的 `build_tool_error()` 產生結構化 JSON（`error_class` 為首鍵，六類：`validation_error`／`permission_denied`／`resource_not_found`／`timeout_exceeded`／`upstream_error`／`internal_error`），**嚴禁**裸中文/emoji 字串或籠統 `not registered`（「查無工具」→ `resource_not_found`；「政策攔截」→ `permission_denied`，二者不得混同）；(b) Gate 1（`ToolLayer`）因政策/角色**過濾**工具時**嚴禁無聲**——必須記錄 `ctx['denied_tools']`／`ctx['tools_filtered']` 並注入 `<tool_availability>` 說明（防模型幻覺被過濾的工具；因 pipeline 中 `ContextLayer` 先於 `ToolLayer`，說明由 `ToolLayer` 自行注入）；(c) 幻覺偵測器（`guard_service.collect_known_tool_names`，SSOT——漂移修復批 C1 自 claude_handler 私有副本下沉；ADR-0156 抽取後唯一呼叫點＝`react_core`，三 handler 為薄殼）的已知工具集**必須涵蓋 MCP 工具**，且 `permission_denied` 不得被當成幻覺；(d) 政策未載入時 `evaluate_policy` 的 fallback **必須為 DENY**（fail-safe，嚴禁偽裝成 CONFIRM）。此為原生工具介面重設計的 **Tier 1**（提案 `docs/architecture/proposals/native-tool-interface-redesign.md`；統一裸動詞＋scope 的 Tier 3 已依 ADR-0119 落地；本輪完整性補強見 ADR-0228）；可靠度量測由 `tests/tool_eval/` 骨架把關。
*   **禁令 60（LDH 無卡限定 / LDH Containment）**：本機優雅降級（LDH）**僅限「完全未附 Authorization」的本機請求**；帶著**失效 token**（過期/簽章不符）的請求一律回 401 `AUTH_INVALID_TOKEN`（不分本地/外部），**嚴禁**「壞卡當無卡」降級發卡。前端**已登入**（記憶體有 access token）時**嚴禁**採納回應標頭 `X-New-Auth-Token`（僅未登入可採納，維持首裝精靈等 pre-login 領卡相容）；access token 續期唯一路徑為 `connections.ts` 的**送出前 exp 檢查 → single-flight `refreshAccessToken()`**（`/auth/refresh`）。（ADR-0131）
*   **禁令 64（Auto 與底線 / Auto Mode & Hard Floor）**：①底線（F1–F7，`utils/hard_floor.py`）**必須**寫成程式常數、在 `AuthPoliciesConfig.evaluate` 最前面判斷；**嚴禁**在 `auth_policies.json` 或任何設定檔加入能關掉或放寬底線的欄位。②審查員的核准與 YOLO **都不得**推翻底線：命中底線回 `HUMAN_ONLY`，只認人類審批卡的核准（可信 `_hitl_preapproved`），無人在場直接拒絕；審查員的核准（可信 `_approved_by="auto_reviewer"`）只滿足 `REVIEW`；模型自己在參數裡帶這些欄位無效。③審查員**只看**使用者的要求（最近 3 則人類訊息；心跳用排程時寫的提示；重新觸發用最後一則人類訊息；標成資料）與這個動作（完整參數與執行環境）；**嚴禁**送入工具輸出、代理人的推理、助理文字或轉述（含子代理的派工目標）、System nudge、SNS 外人訊息與前端內嵌的附件。④審查失敗**一律不放行**：沒設模型、逾時、拒答、截斷、超預算、連不上、解析失敗或任何不是 `allow` 的判決，都轉人（有人在場）或拒絕（無人在場）。⑤審查員只經 `complete_once`、只送到所選模型自己的廠商，**嚴禁**跨廠商備援或重試。（ADR-0246）
*   **禁令 65（插話 / Interjection）**：只有對話的擁有者或管理員能插話；插話**不能**核准任何動作、**不能**改模式；工具輸出等外部內容**永遠不能**走這條路。（ADR-0249）

## 小節

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

### 4. 角色交替原則 (Role Sequence)
*   為滿足 Claude API 要求，多脈衝執行時必須在 Assistant 回覆間注入 User 提示 (Nudge)。嚴禁移除此邏輯。

### 6. SOLID 原則與 Middleware 結構
*   系統採用 Middleware Pipeline 結構處理脈衝生命週期。Context (含已驗證身分) 應由上層注入並傳遞。

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

### 11. 系統穩定性與韌性 (Resilience & Self-Healing) ⭐
* **可靠性來源契約（ADR-0231）**：明示 UI 保存須 strict revision CAS；generation 與 persistence 終態分離，EOF／DONE不證成功。Chat 同 scope/session writer 保留未知富樹，唯讀核對後才可明示僅重試保存；無痕零持久化。任務寫入不明傳固定領域錯誤，原 review／身份／鎖不變。不可把本契約套成所有legacy writer均已改造。
*   **設定檔讀不到時不放寬（ADR-0272）**：`.example` 只補不存在的設定檔；存在卻讀不到時不覆寫，也不退回較寬的預設。
*   **優雅降級意識**: 當記憶系統損壞或整合失敗時，Proxy 應能識別異常並降級至基礎模式，而非持續重試失敗的操作。

### 12. 群聊別名與身分解耦 (Group Alias & Identity Decoupling) ⭐
*   **身分解耦與靈魂投影 (Identity Decoupling)** ⭐：系統支援透過 `@別名` 呼叫靈魂。映射由 `group_whitelist.json` 的 `ToukaSoul` 區統籌；群聊成員只限靈魂（別名必須解析到 `agents_registry` 的靈魂），原始模型 ID 與 regex 不再展開，SNS 的 `@別名` 同樣只到靈魂。live 設定裡的舊 `Native`／`Custom` 區讀取時略過並記警告、不改檔（ADR-0245）。
*   **SSOT 糾偏**：群聊成員 ID 必須是靈魂 ID（與 `agents_registry` 強一致），系統在儲存時自動修正舊格式 ID（如 `ToukaSoulAlice` → `Alice`）。

### 13. 自主預算大一統與優先級 (Unified Autonomy Budget) ⭐
*   **預算決策鏈 (Decision Chain)**：系統在執行 ReAct 循環時，其 Slot 輪數必須經由 `AutonomyService` 判定，遵循：**請求指定 (Explicit) > 全域配置 (autonomy.json) > 系統保底 (Default: 5)**。**實施 `smoke_autonomy_enforcement.py` 驗證預算耗盡時的強制攔截與 Nudge 注入。**
*   **跨場景一致性**：標準對話與主動心跳喚醒共享同一套判定邏輯，確保代理人的行為預期在各類觸發方式下完全一致。
*   **SSOT 降級 (Fallback)**：心跳排程若未指定輪數，必須自動「優雅降級」吸納使用者在安全設定中定義的全域預算。

### 14. 策略即程式碼 (Policy as Code, PaC) ⭐
*   **模型化主權**：全系統的安全策略與權限配置模型（Permissions, Identity, Autonomy, Safety）均必須具備強型別 Pydantic 定義。
*   **決策去中心化 (Logic Injection)**：決策邏輯應注入模型層 (`evaluate` 方法)，Service 層僅負責資料加載與生命週期管理。
*   **上下文感知決策 (Context-Aware)**：系統執行決策時必須基於結構化的 `PolicyContext`，禁止使用散落在代碼中的硬編碼邏輯判定。
*   **元數據審計義務**：所有政策配置模型必須繼承自 `BasePolicyModel`，並強制包含版本號 (`version`) 與作者 (`author`) 欄位，以支援長期的策略演進追蹤與回滾。
*   **身分與權限深度耦合**：執行層（ToolExecutor）必須將已辨識的身分實體（AgentIdentity）封裝入 `PolicyContext` 進行每輪次的權限核驗。
