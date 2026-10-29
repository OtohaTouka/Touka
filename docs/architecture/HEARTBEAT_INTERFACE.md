# ToukaHeartbeat 介面文件 (Standardized Heartbeat Interface)

> **TL;DR** — `POST /heartbeat/awaken` 需 `X-Heartbeat-Token` 或 service JWT（`sub=heartbeat, type=service`）；token 驗證唯一入口 `verify_heartbeat_token()` Depends（SOLID-SRP）；滑動視窗在 PostHeartbeat finally 區塊觸發，僅家對話，子對話跳過（ADR-0035）。

## 對話歸屬與獨立頻道（ADR-0276）

- `/heartbeat/awaken`（含 `/v1`）與 `/v1/heartbeat/retrigger` 在讀歷史及寫入前核對執行靈魂與對話綁定；外靈魂、矛盾家、多家、退休 ID 或非法保留 ID 回 403 `SESSION_SCOPE_MISMATCH`（1221）。Debugger／admin 代打不豁免；retrigger 仍需人類存取權與既有能力閘。
- `session_mode="independent"` 且省略 `session_id`、給空字串或 `pulse_channel_default` 時，解析為每靈魂的 `touka.hb.v1.<完整 SHA-256>`，雜湊輸入為無多餘空白的 UTF-8 JSON `["heartbeat", 正式靈魂ID]`。自己的計算 ID 也可直接指定；明確 `session_id="home"` 或 home mode 仍選家，不把模型預設值誤當成明確指定。
- `touka.` 保留給受管頻道；明確填其他保留 ID，不論是否存在都拒絕。既有頻道綁到外靈魂時拒絕，不覆寫或另挑 ID。舊共享 `pulse_channel_default`／`sns_*` 內容保留、不搬、不續寫，也不作新頻道前情。
- 家候選及樹由同一嚴格快照讀取，只有零候選才可新建；壞家不可當 absence。DB／JSON 失敗為 `SessionPersistenceError`，保存未確認沿 `SESSION_SAVE_FAILED`，不回成功。初始提示保存成功才排背景工作；脈衝中間及最後保存以 CAS 重驗。
- 背景歸屬／保存失敗記固定代碼、結束為 error，不追加錯誤內容、不觸發剪裁；其他錯誤的追加也經儲存閘。回合後剪裁另在歸檔／摘要前核對 scope。
- 排程 bridge、Heartbeat models／client／保存格式不變，沒有新增存檔時的 session 預檢；外靈魂 ID 可留在排程設定，但執行到 Proxy `/awaken` 會拒絕。部署必須一起更新所有 writer，不能混跑舊、新版本。

## 模型溝通層第 2c 段：心跳模式與工具（ADR-0246）

- `security_level` 對應：`safe`→Auto、`full`→YOLO、其他（含大小寫不對）→Manual；`/awaken` 明確帶 `auth_level`（只收 `Manual`／`Auto`／`YOLO`，舊名稱 422）時優先。UI 重跑 `/retrigger` 固定 Auto。
- 心跳一律無人在場（`heartbeat_run`）：Auto 心跳的唯讀與 safe 類工具照跑，終端與高風險工具直接拒絕、不叫審查員；YOLO 心跳的底線 F1–F4、F7 指令仍拒絕；Manual 心跳只跑唯讀工具。`ask_user` 不廣告。
- KF-4：脈衝在 `dispatch_request` 前跑 ToolLayer，請求第一次帶工具；靈魂 `config.json` 的 `enabled_tools` 照樣收斂。ToolLayer 採排程設定時決定的模式，不採家對話 session row 上人類選的模式；脈衝不跑 MonitorLayer。
- 心跳看得到 MCP 工具：YOLO 下會在無人在場時執行（MCP 工具不經底線分類），Auto 下未列名 MCP 屬 dangerous、直接拒絕；workload OAuth 仍維持關閉。
- 原意（審查員的資料）：`/awaken` 用消毒後的 `init_prompt`，`/retrigger` 用 session 樹最後一則人類節點。

## 沒設主要模型（ADR-0250）

- Proxy `POST /v1/heartbeat/awaken`：排程沒帶 `model_override`、靈魂也沒有主要模型時回 409 `AGENT_BRAIN_NOT_SET`（1215），不啟動背景迴圈。以前這一步的檢查不會擋，背景迴圈到送出時才失敗。探針請求照舊在這之前就回；認證、能力與擁有者效期檢查不變。
- 背景迴圈每次脈衝前解析模型（和聊天同一個入口 `request_model_name`：自訂連線送原名、其他轉小寫）；拿不到可用的模型（喚醒後主要模型被清掉、經 `/retrigger` 進來、或名字只差大小寫無法判斷）就記 log、結束這次 run，不丟例外、不寫進對話、不送請求。
- 心跳端只要把 409 當成這次喚醒沒做；靈魂設好主要模型後下一次排程照常。

## 活動紀錄與時間感（記憶系統第二批段 4，ADR-0267）

- Proxy 的背景迴圈（`/awaken` 與 `/retrigger` 共用）在 in-flight 標記寫入後記一筆 `heartbeat_started`，在 `finally` 記 `heartbeat_finished`（`pulses_total`、`pulses_done`、`turns`，結束原因 `completed`／`interrupted`／`quota`／`budget`／`no_model`／`error`／`cancelled`）；只記靈魂，DebugAgent 的心跳不記。滑動視窗的 finally 觸發點與順序不變。
- 每個脈衝送出前，靈魂的請求最後一則 user 尾端附 `<continuity>`（心跳模式：人類多久沒來、上一次自主執行、最近完成、任務看板），只在送出的請求、不寫回對話樹；審查員的原意不含它。
- 原本的時間前綴保留：nudge 前綴格式逐字不變，時間和區塊的 Current time 是同一個值；第一個脈衝的初始提示前綴是 `/awaken` 寫入當下。
- ToukaHeartbeat 服務本身不變。

## 刪除、重置、記憶清除期間（舊審查報告丙-4，ADR-0287）

- 刪除靈魂、重置、記憶清除期間，這個靈魂被標記（`services/destructive_ops.py`）；真的要動手時叫停（重置在預檢通過後、刪除在查心跳時），叫停讓這個靈魂的代數加一，沒做成的操作不動代數。
- `/awaken` 在擁有者與能力檢查之後、找或建對話之前回 409 `AGENT_RESET_IN_PROGRESS`（1415）；寫初始提示之前再看一次代數，找或建對話期間開始的也擋。心跳端把 409 當成這次喚醒沒做，下一次排程照常。
- `/retrigger` 讀完對話之後檢查，同樣回 409、不派發，「上次未完成」照留。
- 兩個入口都把檢查時的代數交給背景迴圈；迴圈開跑時已標記、或之後被叫停過，就不開跑、什麼都不寫（不寫進行中標記、`heartbeat_started`）。登記成功之後才清上一次殘留的中斷旗標。
- 每個脈衝前，標記還在或開跑之後被叫停過就停（結束原因 `interrupted`）；重置先做完也照樣停。中斷旗標被前景訊息清掉時，這個脈衝會跑完，下一個脈衝前才停。這時脈衝的一般錯誤訊息不寫進對話。
- `finally` 遇到同樣情況就不寫 `heartbeat_finished`、不剪裁。進行中標記照舊在剪裁前清；背景活動與登記留到剪裁做完才拿掉（例外或被取消時也拿掉）。
- 刪除時這個靈魂還有心跳在跑：叫它停下（同一個對話的中斷旗標），回 409 `SOUL_HEARTBEAT_RUNNING`（1419），什麼都不清；那次心跳留下的活動紀錄要先重置，建議先暫停這個靈魂的心跳排程。重置時也叫它停下，但照常重置（勾清除時在預檢通過之後才叫停）。
- 心跳派出的子代理不在登記裡，刪除與重置看不到它（ADR-0287 已知限制）。

## 發布前來源契約：排程交易與憑證（ADR-0230）

此節為已實作來源候選，B2／B3 階段接受；最終 B6 見 ADR-0230；發布／live 狀態當時記在 HANDOVER，現在只留在開源前的私人歷史。

- HB `GET/POST /v1/schedules`、`DELETE /v1/schedules/{id}` 沿 Bearer secret。GET 保留原欄位 presence，ETag 是 schedules-only canonical revision，不包含 token；mutation 在同一交易 lock 內檢查可選 If-Match。
- 真人管理走 Proxy `/v1/heartbeat/schedules`：fresh human JWT、一般 user 限 owned agents、admin 全域；更新驗舊／新 agent，隱藏 ID 不洩漏。bridge 必帶 If-Match，409 無副作用。
- `models.py` 是純模型 SSOT，`sync_heartbeat_models.py` 生成 Proxy 投影；舊合法 `9:00`／`09:00` 與未指定欄位保持。重複 schedule ID、等價 daily 時間、無效工作及非有限 JSON 拒絕。
- StorageService 僅確定缺檔時初始化；讀取／解析失敗及 dangling link 保留原件、不生成 token。保存採原子 replace 與 durability 回報；ScheduleService 保存實際 trigger／next_run_time，套用失敗補償一次，不能證明恢復時 degraded。
- runtime 讀取或解析失敗的 persisted state 為 UNKNOWN；pause 失敗的 engine state 為 UNKNOWN。應用 callback 閘阻擋後續 outbound，不宣稱已撤銷既有 in-flight 工作。
- 同一 TokenProvider 供管理、trigger、ProxyClient；共享設定優先，持久 fallback／產品生成維持本機管理用途。共享設定未就緒時 trigger 不建立工作、client 不建 HTTP；shared_ready 只表示本機可提供憑證，不代表 Proxy 健康。
- `TOUKA_ENV_FILE` 明示後優先且不回退其他 env。兩服務需要重新載入同一持久設定；不支援隱含 hot rotation。Proxy plain secret 與合法 service JWT 的既有語意保持。
- client 與 bridge 單送、無 redirect／retry、整體 10 秒；錯誤不回傳原始 exception／輸入。mutation timeout 明示結果未知。HTTP 回應與提交／持久／引擎狀態須分開解讀。
- 出貨錯誤 SSOT 是 `ToukaProxy/config/errors.json.example`，同步 HB／Chat；舊 Proxy registry 缺項只做 in-memory fallback，不修改既存檔。

所有自動化使用 owned env/config/data 與 mock transport，包含 fresh-process restart；詳見 [ADR-0230](adr/0230-heartbeat-transaction-and-credential-contracts.md)。

本文件定義了 ToukaHeartbeat（自主脈衝調度器）的對外 API 端點、排程格式以及單一事實來源 (SSOT)。這套系統負責在背景自動「喚醒」模型代理人，驅動其進行自主思考與任務執行。

---

## 🔗 與記憶系統的獨立性 (Independence from ToukaMemory)

**重要澄清**：ToukaHeartbeat 與 ToukaMemory 是**完全獨立**的兩個系統：

| 維度 | Heartbeat | ToukaMemory |
|-----|-----------|------------|
| **責任** | 排程與預算決策 | 記憶儲存與工具執行 |
| **通訊方式** | HTTP Webhook → Proxy | 整合模式：Python 同行程直接掛載（唯一模式，ADR-0116） |
| **運作模式** | 無 MCP 模式（始終獨立） | 整合模式單一部署（ADR-0116 已退役獨立 MCP 模式） |

**通訊流程**：
1. Heartbeat 定時檢查排程
2. 發送 HTTP Webhook 至 `ToukaProxy:/v1/heartbeat/awaken`
3. Proxy 呼叫 `AutonomyService` 檢查預算
4. 若預算充足，Proxy 將任務註入同行程掛載的記憶系統（整合模式，唯一模式）
5. 記憶系統工具內部透過 `get_resource_proxy()` 取得注入的真實 `ResourceManager`（取不到即 fail-loud，ADR-0116）

**Proxy 的責任**：作為橋接層，不直接實作工具邏輯。

## 🔐 Headless MCP workload OAuth（ADR-0207）

Heartbeat 是 P8-B 唯一允許的 workload execution kind；Heartbeat 本身不保存、不取得 OAuth
client secret 或 user bearer，所有授權、exchange、audit 與 egress permit 均在單一
ToukaProxy process 內完成。

- **Direct**：installation 明示 `oauth_client_credentials`＋`workload_authority_kind=direct`，
  Proxy 以獨立 workload profile/credential 交換短命 access token。
- **Owner delegation**：owner 對自己擁有的 agent 建 exact installation generation＋raw tool＋
  scopes＋expiry policy；Proxy 每次呼叫重驗 exact P8-A active grant，不複製 token。
- **一律拒絕互動**：headless 不開 browser、不等待 popup、不進 MRTR/HITL，也不因 Auto／
  YOLO 推導同意。缺 credential/delegation、過期、撤權、generation/tool/scope 不符皆 fail-loud。
- **撤權次序**：停止新 permit、取消 in-flight task、等待 send task exit acknowledge，再完成
  credential/delegation/source-grant mutation；permit TTL 不可當作完成證據。
- **部署前置**：P8-B 只支援 single Proxy process＋local filesystem exclusive lock；
  Heartbeat 排程器仍可獨立，但不得水平擴展 Proxy workload runtime。

---

## 💓 核心排程 API (Schedules)

Heartbeat 作為一個獨立的調度引擎，提供對自主任務生命週期的 CRUD 操作。

### 🔐 排程 API 認證 (C9 fix) ⭐
所有 `/v1/schedules` 端點（GET / POST / DELETE）**必須**帶有效 Bearer Token：

```
Authorization: Bearer <heartbeat_token>
```

TokenProvider 優先採用共享設定，再由 StorageService 讀持久 token，確定缺檔才初始化／生成並持久化；讀取或解析錯誤不生成替代值。fallback 可供本機管理，outbound 仍要求 shared setting。Token 不符回 HTTP 401。

### 1. 排程清單
- **端點**: `GET /v1/schedules`
- **輸出**: `HeartbeatSchedule[]`
- **認證**：要求 Bearer Token（C9）

### 2. 建立或更新排程
- **端點**: `POST /v1/schedules`
- **認證**：要求 Bearer Token（C9）
- **說明**: 由於 Heartbeat 運行於隔離容器，其 DTO 保持 `agent_id: str` 型別（標註 `# noqa: IDENTITY_SSOT`）。
- **輸入 (HeartbeatSchedule)**:
  ```json
  {
    "id": "optional-id",
    "agent_id": "Charlie",
    "active": true,
    "mode": "interval | daily | once",
    "interval_minutes": 60,
    "daily_times": ["08:00", "22:00"],
    "once_datetime": 171XXXXX, // UNIX Epoch MS
    "pulse_count": 1,
    "pulse_slots": null, // Optional. 若為 null 則自動跟隨 ToukaProxy 的全域「安全設定」
    "init_prompt": "如有需要請處理...",
    "session_mode": "home | independent"
  }
  ```

### 3. 指令手動觸發 (Manual Trigger)
- **端點**: `POST /trigger`
- **說明**: 驗證管理 token 與同次共享 readiness，通過後建立單次喚醒工作；fallback 未就緒不建立 task／HTTP。
- **輸入 (TriggerPayload)**:
  ```json
  {
    "token": "heartbeat_secret",
    "agent_id": "AgentID",
    "pulse_count": 1,
    "pulse_slots": null, // Optional. 指定本次手動脈衝的 ReAct 輪數
    "session_mode": "home"
  }
  ```

---

## 🚥 系統狀態與時間 (Observability)

### 1. 服務狀態
- **端點**: `GET /status`
- **輸出**:
  ```json
  {
    "status": "running",
    "active_jobs": 5,
    "time_taipei": "2026-04-01 22:40:00",
    "time_utc": "2026-04-01T14:40:00Z"
  }
  ```

---

## 🔄 數據流與 SSOT (Single Source of Truth)

Heartbeat 系統必須嚴格遵守時間 SSOT 與配置統一原則。

| 功能模組 | SSOT 來源 | 存放位置 | 說明 |
| :--- | :--- | :--- | :--- |
| **排程配置** | `config.json` | `/app/config/config.json`（ADR-0150：改掛設定『目錄』/app/config；非 Docker 回退 `/app/config.json`） | 儲存所有 Agent 的喚醒頻率與參數。由 `StorageService` 管理。 |
| **通訊令牌** | TokenProvider | 共享設定優先，其次受管持久 token | fallback僅本機管理；outbound需要共享設定，不能推定Proxy健康。 |
| **執行日誌** | `heartbeat.log` | `/app/heartbeat.log` | 記錄每次觸發的成功與失敗。 |
| **時間基準** | **UNIX Epoch** | 系統層級 | 內部儲存與計算一律使用 Epoch；介面顯示轉為台北時間。 |
| **依賴配置** | `requirements.txt` | `/app/requirements.txt` | 定義執行所需套件。必須包含 `apscheduler` 與 `pydantic-settings`。 |
| **環境配置** | `config.py` | `/app/config.py` | 使用 Pydantic BaseSettings 的強型別配置定義。支援環境感知互斥載入（ADR-0052）：明示TOUKA_ENV_FILE時優先且不回退；未明示才沿原整合／獨立路徑。 |

## 🚦 權限與安全 (Security & Auth)

### 1. 喚醒授權 (Capability Check) ⭐
Proxy 在接收到 `awaken` 請求時，會檢查 `config/identity_map.json`。
*   **規則**：Agent 的 `capabilities` 必須包含 `"heartbeat_awakening"`。
*   **角色綁定**：心跳觸發時，系統會自動載入該 Agent 所屬 RBAC 角色的工具權限。只有靈魂、DebugAgent、DispatchAgent 能有角色與 `heartbeat_awakening`；其他 ID（自訂模型、群聊等）一律 Base、沒有能力，喚醒回 `HEARTBEAT_UNAUTHORIZED_AGENT`（ADR-0245）。
*   **心跳白名單（ToukaChat）**：只列上述三種 ID，依 `/identity/agents` 的有效能力顯示（靈魂經類型規則得到的也算）；授予或撤銷時以有效角色與能力為底，經 `POST /identity` 寫覆寫，不再把靈魂降級成 Base（ADR-0245）。
*   **SSOT**：`identity_map.json` 是唯一的身分權限源碼，原有的 `heartbeat_whitelist.json` 已正式廢除。

### 2. 擁有者 Token 效期檢查 (P26 fix) ⭐
*   Proxy 在執行 `/heartbeat/awaken` 時，比對擁有者的 `refresh_token_exp` 是否仍有效。
*   **Grace Period**：比對條件為 `now_sec >= (owner.refresh_token_exp + 30)`（加入 30 秒容錯），避免時鐘偏差（clock skew）在臨界點誤拒合法請求。
*   超過 grace period 回傳 `HEARTBEAT_ORPHAN_AGENT`（HTTP 403）。

### 3. 診斷日誌等級 (P29 fix)
*   Heartbeat 流程中的「驗證通過」等正常狀態訊息必須使用 `logger.debug()`，不得使用 `logger.info()`。
*   `logger.info()` 保留給使用者可見的重要事件（喚醒啟動、任務完成、錯誤警告）。

### 2. 服務令牌驗證 (Service Token Validation — SOLID-SRP) ⭐

`POST /v1/heartbeat/awaken` 採用 **雙重守衛**：

#### 守衛 A：服務層驗證（`verify_heartbeat_token` Depends）

```python
def verify_heartbeat_token(
    request: Request,           # X-Heartbeat-Token header
    req: HeartbeatAwakenRequest,  # req.token body fallback
) -> str:
```

**所有** token 驗證邏輯封裝於此純函式（SSOT）：
1. 提取：`X-Heartbeat-Token` header 優先，其次 `req.token` body
2. 策略 A：若 `HEARTBEAT_TOKEN` 已設定且匹配 → 直接接受
3. 策略 B：解碼 JWT，需 `{"sub": "heartbeat", "type": "service"}`；不符則拋 `HEARTBEAT_INVALID_TOKEN`
4. 任何異常均包裝為 `ToukaException(ErrorCode.HEARTBEAT_INVALID_TOKEN)`

`awaken_agent` body 中**不包含**任何 token 驗證邏輯（由 `test_awaken_agent_body_no_longer_contains_inline_validation()` TDD 靜態確認）。

#### 守衛 B：人類授權驗證（body 邏輯）

- 驗證 Agent 所屬人類擁有者（`users.json` SSOT）
- 驗證 Refresh Token 效期（超過 30 天未登入則拒絕喚醒）
- 驗證 Agent 是否具有 `heartbeat_awakening` capability

### 3. 安全策略映射 (Security Level) — DA24 加固 ⭐
Heartbeat 呼叫時會根據請求參數與角色定義決定整次 run 的模式。映射由**允許清單** `_SECURITY_LEVEL_TO_AUTH` ＋ `_resolve_auth_level()` 統籌（**SSOT**，ADR-0246）：
*   **`safe` 模式** ⮕ 脈衝期間是 **`Auto`**（無人在場：唯讀與 safe 類照跑，終端與高風險工具直接拒絕、不叫審查員）。
*   **`full` 模式** ⮕ 脈衝期間是 **`YOLO`**（僅限已指派為 `owner` 角色的 Agent；底線 F1–F4、F7 仍拒絕）。
*   **未知值（typo/亂碼）⮕ fail-safe 回 `Manual`**（最嚴，並記 warning）。**DA24 修正**：原邏輯 `"SafeAuto" if level=="safe" else "FullYOLO"` 會把**任何非 `"safe"` 值都升級為 FullYOLO**（全自動、無 HITL）；後來僅明確的 `"full"` 才給全自動，看不懂的值先退回 SafeAuto，2c 起退回 Manual。

---

## 🔁 UI 一鍵重新觸發 (Retrigger) — ADR-0078 ⭐

當機/被誤關時，進行中的背景多脈衝迴圈會在 `autonomy_log.json` 留下 in-flight 標記（`AutonomyService.mark_heartbeat_in_flight`，`_process_heartbeat_loop` 起點落盤、`finally` 清除）。ToukaProxy 啟動時 `scan_and_clear_interrupted()` 掃描各 agent 殘留標記，折進 `GET /v1/stats` 的 `interrupted_runs`，前端顯示「⚠️ 上次有 N 個未完成的背景任務」並提供「重新觸發」按鈕。`interrupted_runs` 與 `background_activity` 只列呼叫者能存取的靈魂（規則同 `can_access_session`），一般使用者看不到別人靈魂的紀錄（2026-10-28）。

- **端點**: `POST /v1/heartbeat/retrigger`
- **認證**：以**人類擁有者**身分驗證（`get_verified_identity` + `resource_manager.is_owner`），**非** service token——供登入使用者從 UI 直接重跑自己擁有的 agent。
- **守衛**：擁有權守衛（`is_owner`）＋ 能力守衛（`can_awaken`，須具 `heartbeat_awakening`）＋ 會話須存在（404）。重跑一律以 Auto 執行，所以再過 session 寫入同一個模式授權：只有 admin 或人類擁有者（Debugger 旁路與代理身分都 403）；會話的 `agent_id` 必須是這個靈魂、呼叫者要能存取它，否則 403（R-2c，ADR-0246）。
- **安全**：**強制 `Auto`**（UI 重新觸發永不升級 `YOLO`；無人在場不跑終端、不叫審查員）；`pulse_count` 夾在 `1..5`；仍受每日配額約束。
- **刪除、重置、清除期間**：回 409 `AGENT_RESET_IN_PROGRESS`、不派發，「上次未完成」照留（ADR-0287）。
- **語意**：不新建會話、不重寫 init prompt——對既有（中斷）會話派發延續脈衝，沿用既有歷史（`_execute_single_pulse` 於上一則為 assistant 時自動補 user nudge 續跑）。成功後自 `interrupted_runs` 移除（`clear_interrupted_run`），免下次輪詢復現。
- **輸入**:
  ```json
  { "agent_id": "Charlie", "session_id": "home_Charlie_xxxx", "pulse_count": 1 }
  ```
- **輸出**: HTTP 202 `{ "status": "accepted", "session_id": "...", "message": "..." }`

---

## 📡 脈衝通訊協議 (Proxy Awakening)

當排程觸發時，Heartbeat 會向 `ToukaProxy` 發送 `POST /v1/heartbeat/awaken` 請求。Payload 包含：
- **`init_prompt`**: 自動注入當前時間標記（如 `【目前系統時間：2026-04-01 22:40:00 (週三)】`）。**DA24 加固**：呼叫端可控的 `init_prompt` 在注入 agent 特權 LLM 脈絡前，先經 H3 `sanitize_memory_content`（8000 字截斷＋注入偵測）消毒；傳給背景脈衝 loop 的也是消毒版。
- **`agent_id`**: 指定要喚醒的代理人。
- **`pulse_count`**: 該次喚醒允許執行的最大連鎖反應次數。
- **`session_id`**: 指定對話發生的頻道（通常為 `home`）。

**多脈衝之間的 nudge**：兩種 nudge（脈衝補給、自主行動階段完成）以 user 角色、`name=user_intent.SYSTEM_SPEAKER` 寫進對話樹；審查員據此判斷不是人話，前端的訊息導覽列據此不畫線（雙端常數，`test_system_speaker_contract.py` 核對）。初始提示不帶這個 name。

## 📦 部署與執行規範 (Deployment) ⭐
為了確保容器環境的穩定性，ToukaHeartbeat 遵循以下規則：
1. **絕對匯入**：嚴禁使用相對匯入（如 `from .config import ...`）。
2. **組件解耦 (SOLID)**：核心邏輯必須拆分為 `StorageService` (IO)、`ProxyClient` (Network) 與 `awaken.py` (Scheduler/Router)。
3. **部署等價性 (Parity Check)**：在每次 Docker Build 之前，必須執行 `scripts/guard_dependencies.py` 通過「Dockerfile 物理完整性檢查」，確保所有本地模組均已包含在 `COPY` 範疇中。
4. **鏡像打包**：Dockerfile 應優先使用 `COPY . .` 並搭配 `.dockerignore` 以防止遺漏重構後的新模組。

---

## 🔄 PostHeartbeat Hook：滑動視窗（Sliding Window）⭐

`_process_heartbeat_loop` 的 `finally` 區塊在家對話且結束原因不是 error 時觸發滑動視窗檢查；歸屬或持久化失敗不剪裁（ADR-0276）：

```python
finally:
    if is_home_mode and end != "error":
        await sliding_window_service.check_and_trim(identity, final_session_id)
```

**行為規則**：
- **家對話**：`check_and_trim()` 先驗執行靈魂與對話歸屬，再計算來回數；超過 `upper_limit_rounds` 時先存 Legacy，再以 CAS 更新 Session。
- **子對話**：完全跳過，`is_home_mode=False` 時不呼叫 hook。
- **失敗隔離**：hook 拋出例外時，`logger.exception` 記錄後略過，不影響 heartbeat 整體結果。

**設定 SSOT**：`ToukaProxy/config/sliding_window.json`（由 `GET/POST /v1/system/sliding_window` 管理）。

----
*Updated to Sliding Window PostHeartbeat Hook v1.4.0 (2026-05-21)*
*+ ADR-0078：UI 一鍵重新觸發（`/v1/heartbeat/retrigger`）＋ in-flight 標記/當機復原可見化 (2026-06-09)*
