# 🛡️ Touka 安全性與認證指南 (Security & Auth Guide)

本文件說明 Touka 生態系的安全架構，包含身分驗證、自動授權機制以及工具執行的權限控制。

---

## 1. 認證體系 (Authentication)

系統採用全域的 **JWT (JSON Web Token)** 認證機制。

### 1.1 使用者登入
- **路徑**: `/v1/auth/login`
- **機制**: 使用者密碼經過 `SHA256 -> Bcrypt` 雙重雜湊處理。
- **令牌**: 成功後回傳 `access_token` (短期) 與 `refresh_token` (長期，存於 HttpOnly Cookie)。
- **Cookie Secure 旗標 (H1) ⭐**：Cookie 的 `secure` 旗標由環境變數 `SECURE_COOKIES` 控制，生產環境（HTTPS）必須設為 `True`。
- **速率限制 (H2) ⭐**：採 IP 滑動視窗（10 次/15 分），超限回 HTTP 429。成功登入後計數歸零。
- **Token Rotation (H6) ⭐**：每次呼叫 `/auth/refresh` 同時簽發新 access + 新 refresh token，舊 token 立即失效。

### 1.2 本地動態握手 (LDH - Local Dynamic Handshake)
LDH 只服務「系統尚未被人類認領」的首裝窗口：
- **無卡限定**：只有完全未附 `Authorization` 的受信任本機／Docker 內網請求可領臨時卡。
  帶過期、簽章錯誤或已撤銷 token 一律回 401 `AUTH_INVALID_TOKEN`，不得「壞卡當無卡」。
- **認領後關閉**：任一使用者完成真密碼設定後，無卡本機請求也 fail-loud；開發工具應
  改用登入憑證或 service token。
- **首裝身分**：LDH 可讀 `X-Agent-ID` 作首裝 agent id，但不信任其類型旗標；任意 id
  落到低權預設角色，`DebugAgent` 只依 `identity_map.json` 取得 Debugger。
- **正常代打**：有效的人類 JWT 才能依 `owned_agents`／admin 規則用 `X-Agent-ID`
  代打；這是 Gate 1 授權，不是 LDH fallback。
- **前端縱深**：已登入或曾登入的瀏覽器不得採納 `X-New-Auth-Token`；access token
  續期走 `/auth/refresh` 的 single-flight 流程。

### 1.3 密鑰主權 (C1, C2, H5) ⭐
- **JWT 簽署密鑰**：`TOUKA_AUTH_SECRET` 環境變數；未設定時 `AuthSecretManager` 用 `secrets.token_hex(32)` 自動生成（重啟後 token 失效）。
- **初始管理員密碼 (DA21)**：首次啟動時用 `secrets.token_urlsafe(16)` 隨機生成並寫入**一次性檔** `config/INITIAL_ADMIN_PASSWORD.txt`（經 `resource_manager.write_file_text`，遵 IO_PRISON）；log **僅記錄檔案路徑、不含明文**（避免可輪替/聚合的日誌永久殘留有效憑證）。首次改密或下次啟動（admin 已非臨時密碼）時自動 `remove_file` 清除該檔。
- **Heartbeat Token**：`StorageService` 首次啟動時隨機生成並持久化於 `config.json`；重啟後復用。
- **系統不含任何「公開已知」的硬編碼預設值**。

### 1.4 帳號停權強制 (DA23) ⭐
- **SSOT**：`User.is_active` 屬性（`status == "active"`，純 property、不序列化）為「帳號是否可用」的唯一判定。
- **四閘門強制**：停權（`status="suspended"`）在**所有解析使用者的入口**即時生效——
    - `authenticate_user`（登入）：密碼驗證後檢查，停權→403 `AUTH_PERMISSION_DENIED`（不可枚舉）。
    - `verify_token`（中央 Bearer 閘）：停權→token 立即失效（回 `None`）。
    - `get_current_user`（舊閘）：停權→拒絕。
    - `refresh_access_token`：停權→不換發、且不誤觸 replay 撤銷。
- **效果**：停權後既有 access/refresh token 一併失效；解除停權後恢復。修復前 `status` 欄位形同虛設（無任何路徑讀取）。

### 1.5 自助改密碼 (DA25) ⭐
- **端點**：`POST /v1/auth/change-password`（`get_current_user`，僅能改自己）。
- **流程**：驗當前密碼 → 設新密碼（`Field(min_length=8)`）→ 清 `is_temp_password` → 撤銷 refresh token（強制重新登入）→ 刪除 bootstrap 一次性密碼檔。
- **動機**：修復前臨時/一般密碼使用者**無自助改密通道**（`is_temp_password` 憑證可無限期沿用），且 `update_user_profile` 不處理密碼。

---

## 2. 身分與能力映射 (Identity & Capabilities)

身分管理的核心位於 `config/identity_map.json` (SSOT)。

### 2.1 AgentIdentity 物件
系統不再依賴字串 ID，而是將認證後的身分封裝為 `AgentIdentity` 物件。
- **角色 (Role)**: 關聯至 `permissions.json` 中的權限等級。
- **能力 (Capabilities)**: 具備特殊特權的標籤（如 `heartbeat_awakening` 允許外部喚醒）。

### 2.2 權限分級 (RBAC)
定義於 `permissions.json`：
- **三個 allow 軸**：`allowed_tools`、`allowed_mcp_servers`、`allowed_skills`。
  `["*"]` 表示包含未來項目的開放式授權，明確清單則逐名授予。
- **角色本地 deny**：`denied_tools`、`denied_mcp_tools` 只對該角色生效，拒絕優先；
  裸 `"*"` 表示該角色 deny-all。
- **全域拒絕地板**：`global_denied_tools`、`global_denied_mcp_tools` 對所有角色（含
  Owner）生效，只能收窄。
- **唯一解析器**：`PermissionService.resolve_grants(agent_role)` 將 wire 解析為不可變
  `RoleGrants`；工具廣告／執行、技能與 MCP 不得各自重刻規則。Owner 也沒有硬編旁路。
- **role**: 角色權限面分離（ADR-0141）——人類 `human_role` ∈ {`admin`, `normal`}、agent `agent_role` ∈ {`Owner`, `Base`, `Guest`, `Debugger`, `System`}（`permissions.json` 的角色鍵為 agent 角色）。有效角色讀 `display_role`、權限判定走謂詞。詳見 [ROLE_MODEL.md](../architecture/ROLE_MODEL.md)。

### 2.3 管理員代理權限優化 (Identity Assumption)
當管理員 (`admin`) 切換身分至某個 Agent 進行代理操作時，系統會保留其管理端點的存取權限（ADR-0141 §8-1 **Option A：僅管理權**——代打身分帶 `human_role="admin"`／`agent_role=None`，管理閘讀 `human_role`）。這意味著管理員可在對話視窗中同時保持對系統管理 API（如 `/v1/admin/users`）的完整操縱能力；但**不繼承**目標 Agent 的軀體工具權限（那屬 `agent_role` 面）。

### 2.4 權限設定故障語意

- 不存在、空白或 `{}` 的 `permissions.json` 是 bootstrap，使用「僅 Owner 開放、其餘
  全封閉」的受 Guard #32 管制 fallback。
- 啟動期既有非空壞設定直接拒絕啟動並保留原始解析錯誤。
- 熱重載壞設定保留 last-known-good、記 `AUDIT`，`/health`／`/system_health`
  維持 HTTP 200 但回 degraded；成功重載自動復原。

完整決策見 [ADR-0196](../architecture/adr/0196-permission-representation-convergence.md)。

---

## 3. 安全執行等級 (Authorization Levels)

工具先通過 §2.2 的 RBAC 能力閘，再根據 per-session／request 的 `auth_level` 進行
ABAC／HITL 即時決策。`auth_level` 不儲存在 `permissions.json`：

| 模式 | 說明 | 決策邏輯 |
| :--- | :--- | :--- |
| **Manual** | 人工確認 | 唯讀類工具直接放行，其餘工具與指令都要使用者確認 (`CONFIRM`)。 |
| **Auto** | 審查員把關 | 唯讀與 safe 類放行；終端與高風險工具先交審查員（`REVIEW`），轉人或失敗時問人，無人在場拒絕；F5、F6 與動態 shell 寫法只有人能核准。 |
| **YOLO** | 全自動執行 | 除核心黑名單 (`critical_blacklist`) 與底線外，其餘操作皆自動執行。 |

三種模式都受程式常數底線約束：刪改 Touka 資料、讀金鑰、改權限設定、改原始碼、系統破壞（F1–F4、F7）只有人能核准（`HUMAN_ONLY`），設定檔關不掉（禁令 64，ADR-0246）。舊值讀取時對照：`ReadOnly`、`SafeAuto` 當 `Manual`，`FullYOLO` 當 `YOLO`。無痕對話一律 `Manual`。

### 3.1 模式變更守衛 (Authority Guard)
把對話設成 `Auto` 或 `YOLO` 是一項特權操作，在對話存檔時判定（看發起請求的人類身分）：
- 僅限 **管理員 (Admin)** 或 **該靈魂的擁有者** 可以存成 `Auto` 或 `YOLO`。
- 其他人的寫入會存成 `Manual` 以策安全。請求參數裡的 `auth_level` 不是模式來源。

---

## 4. 資源監獄與路徑隔離

### 4.1 TID251 (Physical Path Ban)
嚴禁 Agent 存取其權限範圍外的物理路徑。
- **Resource Manager**: 根據 `AgentIdentity` 物件解析出的 `Personal Drive` 為唯一合法寫入空間。
- **隔離執法**: 任何嘗試越權的 I/O 操作都會被攔截並引發 `PathGuardError`。

### 4.2 路徑邊界校驗 (C4 fix) ⭐
- 所有跨層路徑檢查**禁用** `str.startswith()`（會被 sibling directory 繞過，如 `/workspace/Toukaproxy` 通過但實際在 `/workspace/Touka` 之外）。
- **必須**使用 `Path.is_relative_to()` 進行結構化校驗。

### 4.3 演化沙箱環境變數 (H4 fix) ⭐
`EvolutionPathGuard.get_clean_env()` 採「擴展黑名單 + 顯式移除」雙保險：
- **黑名單模式**：`SECRET`、`KEY`、`TOKEN`、`AUTH`、`CREDENTIAL`、`PASSWORD`、`PYTHON`、`LD_`、`DYLD_`、`NODE_`、`GEM_`、`JAVA_`、`PERL`
- **顯式移除**：`PYTHONHOME`、`PYTHONSTARTUP`、`PYTHONINSPECT`、`LD_PRELOAD`、`LD_LIBRARY_PATH`、`DYLD_INSERT_LIBRARIES`、`DYLD_LIBRARY_PATH`
- **強制覆寫**：`PYTHONPATH = "."`

### 4.4 演化沙箱 OS 級資源限制 (DA28 / ADR-0076) ⭐
演化沙箱原本只有掛鐘逾時 ＋ **可繞的** in-process `builtins.open` monkeypatch（JailedWriter）控管儲存，且 `max_memory_mb` 宣告卻**零強制**。`EvolutionPathGuard.run_sandbox_command` 現額外掛上 POSIX `preexec_fn`（`_build_rlimit_preexec`），在子進程 fork 後、exec 前套用核心級限制：
- **`RLIMIT_FSIZE`**：儲存配額（取代可繞的 JailedWriter——模型自寫碼以 `os.write`/子進程都繞不過 OS 限制）。
- **`RLIMIT_AS`**：記憶體上限（wire 起原本 inert 的 `max_memory_mb`）。
- **`RLIMIT_CPU`**：CPU 秒數（補強掛鐘逾時）。
- **跨平台降級**：Windows 無 `resource` 模組 → 優雅降級回「逾時 ＋ JailedWriter」（記 info）；`NPROC` 因共用 uid 會誤殺，暫不設。JailedWriter 保留為縱深防禦。
- 詳見 [ADR-0076](../architecture/adr/0076-evolution-sandbox-os-resource-limits.md)。

---

## 5. 跨切面安全守衛 (Cross-cutting Guards)

| 守衛模組 | 對應弱點 | 用途 |
|---------|---------|------|
| [`utils/csrf_guard.py`](../../ToukaProxy/src/utils/csrf_guard.py) `verify_origin()` | M1 | 終端機審核端點的 CSRF Origin 標頭白名單驗證 |
| [`utils/rate_limiter.py`](../../ToukaProxy/src/utils/rate_limiter.py) `RateLimiter` | H2 | `/auth/login` IP 滑動視窗速率限制 |
| `tools/terminal.py` `_classify_command_strategy()` | C5 | Shell metachar 偵測；ALLOW 遇串接字元升級為 CONFIRM（三種模式下只在 YOLO 碰得到） |
| `tools/builtin/evolution.py` `_build_jail_code()` | C6 | 演化沙箱動態程式碼用 `json.dumps()` 轉義 |
| `services/sns_gateway.py` `_warn_if_unverified()` + `_guard_content_length()` | C7, M3 | SNS event 來源驗證 + 內容長度上限 |
| `routers/mcp.py` `MCPServerItem.command` validator | M4 | MCP config 反序列化拒絕 shell 注入字元 |
| `ToukaMemory/utils/content_guard.py` `sanitize_memory_content()` | H3 | Memory Prompt Injection 偵測 + 長度截斷 |
| `ToukaChat/lib/connections.ts` `sanitizeHeaderValue()` | M5 | 前端 HTTP header 注入消毒（剝除 `\r \n \x00`）|
| `scripts/guard_endpoint_auth.py` Guard #24 | N1~N3, N10 + 52 endpoints | AST 掃描確保每個非公開端點有認證 Depends；Grandfather List 清零 |
| `routers/heartbeat.py` `verify_heartbeat_token()` | SOLID-SRP | `/heartbeat/awaken` 的 JWT 驗證邏輯獨立為 Depends 純函式，可單元測試 |

詳細修復歷史見 [`SECURITY.md`](../../SECURITY.md)、[ADR-0033](../architecture/adr/0033-infosec-hardening-c1-m6.md) 與 [ADR-0034](../architecture/adr/0034-endpoint-auth-dependency-guard.md)。

---

## 6. 端點認證依賴守衛 (Endpoint Auth Dependency Guard #24)

### 6.1 設計原則
系統中所有非公開端點（非 health check、非 SSE stream）**必須**帶有至少一個 FastAPI Depends 認證守衛。

角色檢查一律使用 `ToukaProxy/src/services/authorization_gateway.py` 的共用函式（ADR-0239），router 只定義指向它們的別名（例如 `_require_mcp_role = require_privileged`）。別名名稱需保留 `_require_` 前綴，admin 類需含 `require_admin`，Guard #24 才能辨識；`tests/lint/test_security_kernel_ratchet.py` 禁止在 router 內自行實作角色檢查。

| 共用函式 | 判斷 | 拒絕時錯誤碼 | 代表別名 |
|---------|------|-------------|---------|
| `get_verified_identity()` | 有效 token（無效回 401） | `AUTH_INVALID_TOKEN` | — |
| `require_authenticated` | 已登入 | `AUTH_PERMISSION_DENIED` | `_require_authenticated` |
| `require_privileged` | Owner／Debugger／admin | `AUTH_PERMISSION_DENIED` | `_require_mcp_role`、`_require_editor_role`、`_require_models_mgmt`、`_require_connections_role` |
| `require_owner_or_admin` | Owner／admin | `AUTH_PERMISSION_DENIED` | `_require_creation_role`、`_require_bots_role` |
| `require_admin_authority` | admin | `AUTH_ADMIN_REQUIRED` | `require_admin`、`_require_admin_role`、`_require_config_role` |
| `require_admin_human` | 人類 admin（不含代打） | `AUTH_ADMIN_REQUIRED` | MCP OAuth 管理端點 |
| `verify_heartbeat_token()` | service JWT 或 HEARTBEAT_TOKEN | — | `/heartbeat/awaken` |

掛在 `Depends` 的閘會在 body 驗證前執行（錯誤 body 也回 403）；在函式內呼叫的閘則在 body 驗證後執行（錯誤 body 回 422）。兩種形式不可任意互換。

### 6.2 verify_heartbeat_token (SOLID-SRP)
`/heartbeat/awaken` 端點採用雙重驗證（守衛 A + 守衛 B）：
- **守衛 A** (`verify_heartbeat_token` Depends)：驗證 service token（header 優先於 body；支援 plain secret 與 service JWT）
- **守衛 B**（endpoint body）：驗證人類擁有者授權與 Refresh Token 效期

`verify_heartbeat_token()` 作為純函式可直接單元測試，無需 FastAPI TestClient。

### 6.3 公開端點白名單
以下端點合法豁免（`endpoint_auth_allowlist.py`）：
- `GET /`、`GET /health`（health check）
- `GET /v1/heartbeat/test`（connectivity probe）
- `GET /terminal/stream`（SSE read-only broadcast）
- 等約 20 個公開服務探測端點

---
*Verified Security v3.0 (2026-05-20) — C1~N12 33 項弱點 + Guard #24 端點認證守衛 + SOLID-SRP Heartbeat*
