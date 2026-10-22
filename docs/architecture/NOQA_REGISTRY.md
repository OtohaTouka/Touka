# NOQA Registry（例外豁免登記簿）

## 說明

本文件追蹤全系統的 `# noqa` 指令，記錄每處豁免的原因、修復狀態與優先度。

- **架構背景**：參見 [ADR-0023](adr/0023-dynamic-ssot-suppression-with-noqa.md)
- **核心原則**：每個 `# noqa` 必須綁定特定規則 ID（如 `# noqa: ARG002`），禁止全行抑制（裸 `# noqa`）
- **適用範圍**：僅記錄「架構決策型」豁免（基礎建設層），一般 Ruff 格式類豁免（E501、I001 等）不在此列
- **統計數據**：以下方統計表為準，逐規則計數由 Guard #19（`scripts/guard_noqa_registry.py`）對代碼庫實掃執法。
- **2026-10-08 ADR-0238**：隨發版資格腳本、容器安全管線、MCP rollout evidence 與對應測試刪除的豁免，已同步自統計表與明細移除；舊批次帳本不再保留。

---

## 全系統統計 (Global Statistics)

ADR-0276 移除 SNS gateway 延遲匯入 main 的 `LOCAL_IMPORT_BAN`（26 → 25），以及家查詢改固定 SQL 後的 `S608`（其他合計 326 → 325）；未新增豁免。

| 規則 ID | 數量 | 說明 |
|---------|------|------|
| `TIME_SSOT_VIOLATION` | 0 | 目前無豁免（原有的本機發版工具的 epoch／clock-port 呼叫已隨 ADR-0238 移除）。 |
| `B008` | 228 | FastAPI 依賴注入 (Depends) 在參數預設值中的調用（-5 刪掉沒掛載、走不到的排程 CRUD `routers/scheduler.py`；+4 ADR-0249 U4a-1 插話三端點與 `require_human_requester`；+1 ADR-0246 U2c-6 `routers/autonomy.py` `list_reviewer_models`；+1 ADR-0195 `routers/mcp.py` `list_server_tools`；+11 ADR-0203 P5 resources/prompts；+3 ADR-0205 HITL；+21 ADR-0206 P8-A OAuth；query callback runtime 改 route 內解析；+31 ADR-0207 P8-B workload admin/owner control plane；-1 移除 `POST /mcp/scan`）|
| `LOCAL_IMPORT_BAN` | 25 | 循環匯入與初始化時序的合法延遲匯入；SNS gateway 已改由啟動注入 context，不再列入（ADR-0276）。其餘位置見下方明細。 |
| `S603` | 33 | 子進程調用安全性：固定 argv、`shell=False` 的受控子進程咽喉（測試 harness、CVE audit 守衛、ToukaIDE Marketplace 候選建置、offline npm gate 等；+1 ADR-0251 測試沙盒收尾的子程序 pytest；+1 ADR-0256 記憶庫升級測試寫一半就當掉的子程序；+1 ADR-0278 ruff 棘輪收集；+1 ADR-0278 tests/README 列表核對的 git ls-files）。 |
| `SLF001` | 68 | 跨類別私有成員存取（-2 ADR-0253 `services/legacy_sync.py` 改用公開的 `MemoryManager.get_legacy_dir`；P2 -3：刪除 `_get_effective_permissions` 私有測試接點；P5 +1：`PermissionService.reset_singleton()` 重設不可變 `_permission_config_health` 快照；+1 ADR-0195 tests/unit/test_mcp_server_tools_endpoint.py 注入 `_active_sessions` 建立「已連線」態以做 HTTP 級端點測試；+4 ADR-0195 tests/unit/test_mcp_tool_governance.py 直接注入 ProtocolManager 的 `_active_sessions`／`_circuit_breakers` 以建立不含真實子進程的受控連線態；-1 ADR-0146 test_mission_stop_gate 隨 nudge 退役刪除；+4 ADR-0148 test_skill_registry_rbac 注入/還原 skill_service._metadata 測試 seed；+5 P3 認證縫 test_auth_seam_hardening_p3 顯式建立/毒化沙箱 users.json（_atomic_users_write 清空／注入毒化／改水位／改密碼快照）＋user_service.create_user 單閉包呼叫 _atomic_users_write；+1 G03 test_ldh_cardless_no_type_flag 清空沙箱 users.json 設「未認領」LDH 前提；+1 G05 test_soul_stepup_audit 清空沙箱 users.json 建 admin 驗步升 AUDIT；+4 G07 test_soul_model_registration_cache 存取 model_manager._models_cache／_save_config 與 souls._update_models_config 源碼驗快取權威；+1 G13 test_env_persistence_anchor 讀 config._ENV_PERSISTENCE_PROBE_KEYS 驗 compose environment parity；+3 G20/G22 test_soul_model_registration_cache 讀 model_manager._models_cache 驗冪等/裸條目；-1 ADR-0208 connections status 改用 public diagnostics getter；+1 演化 MCP 驗證沿用 `EvolutionPathGuard._build_rlimit_preexec`，保持直接執行與啟動器的配額同源） |
| `PTH121` | 1 | 演化技能的實體別名判斷使用 `os.path.samefile`，可對不分大小寫的掛載注入一致的測試接點。 |
| `ARG002` | 5 | 介面協議規定的未使用參數（+4 ADR-0137 NoopReviewer.review 實作 Reviewer Protocol 對稱簽名，哨兵無條件放行不使用參數） |
| `TID251` | 3 | 物理路徑禁令 (Path Ban)／受控 `subprocess` 匯入／隔離啟動器組目標環境：僅限明細表所列的基礎建設與測試咽喉（+1 ADR-0275 `sandbox_kernel/launcher.py` `_exec`）。 |
| `IO_PRISON_VIOLATION` | 13 | I/O 監獄違規（備援實作、引導邏輯或獨立容器存取）；明細見下表（+3 ADR-0272 執行器伺服器）。 |
| `IDENTITY_SSOT` | 39 | 身分物件化違規（獨立容器協約、路由層 URL 參數、回應模型欄位、鎖原語 key 或誤判；含 debug.py `_read_last_payload(agent_id)` 依 payload log 的 agent_id 字串比對；跨身分看板定址修復淨 +1：-1 mission_manager 移除硬編 role="Base" 的 `_PathIdentity`，+2 `resource_manager`/`protocols` 的 `get_vault_path_for_agent(agent_id: str)`——跨身分定址入口以 agent_id 字串為鍵，於函式內查真實註冊身分後才物件化；ADR-0142 soul CRUD 修復 +3：`routers/souls.py` 的 `delete_soul(soul_id)` 端點＋`_remove_from_all_owned_agents(soul_id)`／`_remove_identity_override(soul_id)` 清除輔助，soul_id 為攻擊者可控的 URL path 字串、於函式內過路徑監獄後才操作，同既有 `update_soul(soul_id)` 慣例；hitl-unification H2 +1：`services/approval_service.py` 的 `PendingApproval.agent_id`——待批快照的顯示／owned_agents 比對用字串，授權判定主體是解算端點的已驗證 `AgentIdentity`；ADR-0214 +1：execution context 的 legacy `agent_id` kwargs 是已驗證 identity 的字串投影，不是授權 principal） |
| `TRY003` | 23 | 例外訊息過長或直接寫在 raise 中 (基礎建設底層 + Pydantic 模型驗證器 + setup_service.py 輸入驗證 + embedding_service.py apply_switch 空值檢查；+1 ADR-0137 test_reviewer 測試替身模擬傳輸失敗) |
| `FETCH_SSOT` | 6 | 前端直接 fetch() 合法豁免（fetchTouka 核心定義、多供應商 SSE 串流、外部 URL 入參、認證續期直呼 refresh 避免遞迴） |
| `BASICCONFIG_BAN` | 2 | MCP 伺服器必須將日誌導向 stderr 以保護 stdout 為 JSON-RPC 通訊通道 |
| `LOCALSTORAGE_SSOT` | 12 | 前端直接 localStorage 合法豁免：Theme／外觀偏好 SSOT 初始化與寫入（純字串/CSS） |
| `RAISE_STRING_BAN` | 30 | 後端 raise硬編碼字串合法豁免（已全面重構為自定義異常）；+3 ADR-0135 mission_manager.finalize_mission（空 verdict／無 mission／未全 DONE 校驗，沿用同檔既有慣例）；+7 ADR-0137 mission_manager 互審 gate（_save_mission_unlocked 落盤不變式 review gate＋cmd 鎖 ×2、record_review can_review／review_status／無 mission／自簽／task 不存在 ×5） |
| `BCRYPT_IMPORT_SSOT` | 0 | 目前無豁免（auth_service.py 為白名單，不需 noqa） |
| `SQLITE3_IMPORT_BAN` | 0 | 目前無豁免（原 container-security runtime smoke 已隨 ADR-0238 移除）。 |
| `FILELOCK_ASYNC_SSOT` | 5 | 服務層必須使用 AsyncFileLock；mission_lock.py 為既有定義點（ADR-0049）；ADR-0206 keyring atomic writer 與 OAuth profile fence；ADR-0207 workload profile/delegation JSON generation CAS 各一個定義點 |
| `PATH_LITERAL_SSOT` | 5 | 硬編碼物理路徑合法豁免：`test_io_prison.py` 的惡意攻擊 payload 字面量（`C:\Windows\win.ini`／`Z:\` 跨磁碟逃逸樣本）——path-escape 安全測試本質需要真實注入路徑，非業務路徑。 |
| `CONSOLE_BAN` | 1 | 前端直接 console.log/warn/error 合法豁免：`error_handler.ts` L76 是 `logSSOTError()` 的 SSOT 封裝層 |
| `ERROR_SSOT_VIOLATION` | 1 | 明示 UI admission 的固定 typed precommit 衝突結果須 HTTP 409；不反射原輸入。 |
| `PAC_MANUAL_LEVEL_CHECK` | 5 | 安全測試合法豁免：`test_native_tool_payload_contracts.py`（1）、`test_session_persistence_ordering.py`（2）、`test_auth_mode_authority_security.py`（2）讀回被保護的 auth_level 驗證既有安全級或舊值對照，非生產中介層手刻策略判定（ADR-0246 U2c-4：E01 測試改用屬性斷言 -5，舊值讀取對照斷言 +2） |
| `ENV_SSOT_VIOLATION` | 38 | 測試與開發基礎建設中的環境變數存取限制豁免：setup_service.py os.environ 直讀（ADR-0053）、隔離啟動器組交給目標的環境（ADR-0275，+1）、ToukaDispatch `_build_child_env`（ADR-0067）與 `_bind_host`（ADR-0082）、offline npm gate CLI 邊界、ADR-0214 manual CLI／sandbox policy 測試、ADR-0218 Marketplace release CLI 子環境、characterization harness 的 CI 缺檔閘（model-p1a）等；明細見下表。 |
| `PLW0603` | 6 | Singleton 模組級變數 global 宣告（dispatch_callback_store.py、ask_user_store.py get/reset；+2 ADR-0137 review/budget.py ReviewBudgetLedger get/reset 單例） |
| `BLE001` | 17 | 靜默失敗的非同步回呼（ADR-0051：回呼失敗不應中斷主流程）；+2 ADR-0138 subagent_tools 耐久落盤 best-effort（_persist_durable 失敗不得沉掉已交付結果）＋fan-out 隔離（run_subagent_group 一子失敗不沉全組）；+4 軌B Batch 6 DebugAgent 重置 best-effort 容錯（agent_reset：guard.py 列舉失敗保守拒絕、primitives.py 記憶系統不可用降級、orchestrator.py 單 store 失敗收進報告不中斷 ×2）；+1 ADR-0113 guard.py `_is_real_persona_strict` 列舉失敗 fail-closed；+4 bug① embedding_service.py 防禦式降級（下載背景任務落狀態、記憶存在性偵測、目前模型查詢、向量單例重置——記憶系統未載入/異常皆不得逸出）；-1 ADR-0146 mission_stop_gate 隨 nudge 續圈退役刪除（原 board_allows_stop 看板查詢 fail-open 豁免）；+1 ADR-0137 GroundedReviewer completer 傳輸失敗 fail-closed REJECT（審查傳輸壞掉必須擋而非放行）；+3 ADR-0270 清除連備份的三層邊界（`memory_scrub/swap.py` 每一檔、`memory_scrub/orchestrator.py` 每一類別與重查、`run_after_reset` 整個清除的安全網：意外只記日誌、回報原因代碼，不中斷其他檔、端點照常回報告） |
| 其他 (`S102`, `S104`, `S105`, `S108`, `S602`, `S607`, `TC001/TC002/TC003`, `T201` 等) | 325 | 安全、工具或基礎建設類架構豁免（2026-10-14 實際清點，ADR-0251 重新清點：用 Guard #19 同一掃描法（tokenize 只算行內 noqa、同一組略過資料夾），統計表沒有獨立列、也不是格式類 `E402`／`E501` 的規則合計 325，其中括號列出的 11 個代號共 269（`T201` 144、`TC001` 61、`TC002` 23、`S105` 18、`S607` 8、`TC003` 5、`S104` 4、`S602` 3、`S102` 1、`S108` 1、`S314` 1）；ADR-0251 刪掉讀真 `.env` 的測試時少 1 個 `S105`（19 → 18、合計 327 → 326）；刪掉沒掛載的排程 CRUD `routers/scheduler.py` 時少 1 個 `TC001`（62 → 61、合計 326 → 325）；ADR-0272 的 CI junit 核對腳本 `scripts/ci/isolation_junit_check.py` 只在 CI、只讀同一個 job 剛由 pytest 產生的 junit（輸入可信；腳本不在映像裡），多 1 個 `S314`（合計 325 → 326）；舊值 39 不是掃描數，ADR-0250 已照實更正。這一列不受 Guard #19 檢查。B4 刪 Proxy 排程引擎時 `scheduler_service.py` 的 `S501` 隨 `verify=False` 一起移除；ADR-0082 ToukaDispatch；routers/stats.py runtime `Request`；routers/system.py runtime DI types；ADR-0207 workload models的 Pydantic runtime schema＋workload router 的 FastAPI runtime Request/identity/body models） |

---

## 狀態圖例

| 標記 | 說明 |
|------|------|
| 🔴 **不可修復** | 受協議契約或架構設計約束，豁免是必要的 |
| 🟡 **可修復（低優先）** | 技術債，不影響功能，可於未來迭代改善 |
| 🟢 **已修復** | 豁免已移除 |

---

## ToukaHeartbeat（`ToukaHeartbeat/`）

### `awaken.py`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L271 | `S104` | 🔴 不可修復 | 監聽所有介面 (0.0.0.0) 以支援 Docker 容器內外部通訊 |
| L84, L101 | `IDENTITY_SSOT` | 🔴 不可修復 | ToukaHeartbeat 為獨立 Docker 容器，不包含 ToukaProxy 依賴，無法 import `AgentIdentity` 物件。 |

### `storage_service.py`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L24, L33 | `IO_PRISON_VIOLATION` | 🔴 不可修復 | 獨立容器本地配置存取：Heartbeat 負責自身排程持久化，無法透過 Proxy 的 ResourceManager 操作實體檔案。 |

---

## ToukaMemory（`ToukaMemory/src/memory_server/`）

### `services/manager.py`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L73 | `IO_PRISON_VIOLATION` | 🔴 不可修復 | `MemoryManager.get_personal_drive_dir()` 備援路徑：在 MCP 模式下 `resource_proxy` 不可用時，必須直接呼叫 `path.mkdir()` 建立目錄 |
| L95 | `IO_PRISON_VIOLATION` | 🔴 不可修復 | `MemoryManager.get_legacy_dir()` 同上，MCP 模式備援 I/O |
| L149 | `IO_PRISON_VIOLATION` | 🔴 不可修復 | `MemoryManager._load_registry()` 備援讀取：在 `resource_proxy` 不可用時，需直接讀取 registry JSON；此為 ToukaMemory 的底層引導邏輯 |

### `services/legacy_sync.py`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L230 | `ARG002` | 🟡 可修復（低） | `export_session_to_json()` 是未來功能的 stub，`identity`、`session_id` 尚未使用；待實作完整邏輯後自然消除 |

### `services/mission_lock.py`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L15 | `FILELOCK_ASYNC_SSOT` | 🔴 不可修復 | ADR-0049：mission_lock.py 是 AsyncFileLock 的唯一定義點，本身即為封裝層，此豁免允許定義點 import |
| L38 | `IDENTITY_SSOT` | 🔴 不可修復 | ADR-0049：`MissionLock.__init__` 接收 `agent_id: str`（低階鎖原語，呼叫方 `MissionManager._get_lock` 已完成身分校驗，此處僅需 string key） |

### `services/mission_manager.py`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| (已刪除) | `LOCAL_IMPORT_BAN` | 🟢 已修復 | C2 修復：以模組頂層 `_PathIdentity` 替換 `_get_mission_path` 內的 FallbackIdentity，消除延遲匯入 |
| (已刪除) | `IDENTITY_SSOT` | 🟢 已修復 | 跨身分定址修復：刪除硬編 role="Base" 的 `_PathIdentity`，`_get_mission_path` 跨身分改由 `proxy.get_vault_path_for_agent(agent_id)` 依真實註冊身分決定論定址（ADR-0116 已退役獨立 MCP 模式，原「無 AgentIdentity 可用」前提消失） |
| L47 | `IDENTITY_SSOT` | 🔴 不可修復 | ADR-0049：`_get_lock(agent_id: str)` 內部輔助方法，`agent_id` 已由呼叫鏈的 `_resolve_target_agent_id` 驗證後傳入 |
| L50 | `IO_PRISON_VIOLATION` | 🔴 不可修復 | ADR-0049：FileLock 鎖定目錄的自癒引導，確保鎖定檔父目錄存在；等同 server.py 的引導邏輯豁免 |
| L138, L155, L232, L240, L244, L252 | `RAISE_STRING_BAN` | 🔴 不可修復 | 獨立 MCP 服務之任務看板依賴、狀態機流轉、特權重置校驗；L244 為 H1 修復新增（BLOCKED 轉換需 blocker_detail）。ADR-0049 移除 `import asyncio` + `_mission_locks` global 後行號 -3 偏移 |
| `finalize_mission` (×3) | `RAISE_STRING_BAN` | 🔴 不可修復 | ADR-0135 finalize 原語：空 verdict／無 mission／未全 DONE 三重校驗，沿用同檔既有 raise 慣例（TRY003 檔案級豁免） |

### `protocols.py`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| `get_vault_path_for_agent` | `IDENTITY_SSOT` | 🔴 不可修復 | `IResourceProxy` 跨身分定址協約：以 `agent_id: str` 為鍵（呼叫端僅持有 `target_agent_id` 字串），真實身分於實作端（`ResourceManager.get_vault_path_for_agent`）查詢後才物件化；協約簽名須與實作對稱 |

### `models/mission.py`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L64 | `IDENTITY_SSOT` | 🔴 不可修復 | 任務看板模型需支援 MCP 協定邊界之 JSON 序列化，且 Memory 與 Proxy 程式碼解耦，無法直接使用物件身分。 |

### `server.py`（ADR-0116：獨立 MCP 進入點 `memory_server/server.py` 已整檔刪除，原 L31/L175 IDENTITY_SSOT 豁免隨之消除）

### `tools/diag.py`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L95 | `IDENTITY_SSOT` | 🔴 不可修復 | **誤判排除**：`manage_duplicate_memories` 的 `target_id` 是要封存的事實 ID，不是代理人身分（原本寫「同上」，指的 `server.py` 列已隨 ADR-0116 刪除）。 |

---

## ToukaProxy（`ToukaProxy/src/`）

### 路由層 (Routers)

| 檔案 | 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|------|
| `routers/auth.py` | L27-L360 | `B008` | 🔴 不可修復 | FastAPI 依賴注入專用模式，將 `Depends()`／`File()` 置於參數預設值符合框架慣例（含 DA25 `/auth/change-password`、P3 自助個資端點 `PATCH /auth/me`、ADR-0106 `/auth/avatar` 上傳/刪除、ADR-0107 `/auth/clear-my-data` 自助清空、ADR-0110 `/auth/export-my-data` 自助 GDPR 匯出）|
| `routers/custom_models.py` | L41, L58, L75, L95 | `IDENTITY_SSOT` | 🔴 不可修復 | FastAPI 路由層端點簽章：`model_id: str` 由 URL path parameter 提取，類型轉換在函式體內完成 |
| `routers/observability.py` | L165 | `LOCAL_IMPORT_BAN` | 🟡 可修復（低） | `get_memory_list` 在函式內 `from memory_server.tools import get_manager`。原本的理由（ToukaProxy 不含 ToukaMemory 時靠 `except ImportError` 降級）已不成立：ADR-0116 後 Proxy 一律整合 ToukaMemory，ADR-0253 起本檔頂層也匯入 `memory_server.factual`，缺 ToukaMemory 時整個模組就載入失敗，走不到這個分支。改成頂層匯入即可拿掉這個 noqa 與 `except ImportError` 分支（`memory_server.tools` 對 `src` 只有型別檢查用的匯入；未實測，待改程式時驗證） |
| `routers/observability.py` | memory/list、logs（×2 別名＋×2 實體）、events 端點 | `B008` | 🔴 不可修復 | FastAPI 依賴注入：2026-07-07 權限稽查收緊——五端點三元組閘＋`/events` 需已驗證身分的 get_verified_identity 注入 |
| `routers/debug.py` | tools / context-snapshot(+export) / execute / scorecard run / scorecard stream / reset 端點 | `B008` | 🔴 不可修復 | 同上（軌B 批次1 新增 scorecard run/stream；Batch 6 新增 `/debug/reset` DebugAgent 重置端點的 get_verified_identity 注入）|
| `routers/terminal.py` | approve, reject endpoints | `B008` | 🔴 不可修復 | C8 安全修復：terminal approve/reject 加入 Depends(get_verified_identity) |
| `ToukaHeartbeat/awaken.py` | schedule endpoints | `B008` | 🔴 不可修復 | C9 安全修復：heartbeat schedule CRUD 加入 Depends(verify_schedule_token) |
| `routers/connections.py` | list, save, status endpoints | `B008` | 🔴 不可修復 | N1/N2 安全修復：connections CRUD 加入 Depends(_require_connections_role) |
| `routers/mcp.py` | hub/install、tools/resources/templates/read/prompts/get endpoints | `B008` | 🔴 不可修復 | N3 安全修復與 ADR-0203：MCP 管理／data-plane 端點使用 FastAPI Depends 注入角色閘與 lifespan-scoped ProtocolManager |
| `routers/mcp_oauth.py` | control/admin/callback endpoints（21） | `B008` | 🔴 不可修復 | ADR-0206：OAuth request identity、runtime、manager、human-admin 與 rotation 服務使用 FastAPI `Depends()` 注入；query callback 為確保 runtime unavailable 亦能清除 history，改在 route 內解析 runtime。callback 本身不信任瀏覽器身分，僅以一次性 state CAS 解算。 |
| `routers/heartbeat.py` | awaken endpoint | `B008` | 🔴 不可修復 | FastAPI 依賴注入模式，heartbeat awaken 端點的 AuthService 注入 |
| `routers/heartbeat.py` | retrigger endpoint | `B008` | 🔴 不可修復 | P5c bonus：UI 一鍵重新觸發端點的 get_verified_identity（人類擁有者）注入 |
| `routers/debug.py` | context-snapshot 路由 + `_read_last_payload` | `IDENTITY_SSOT` | 🔴 不可修復 | FastAPI 路由層：`agent_id: str` 為 URL query 參數；`_read_last_payload(agent_id)` 依 proxy_payload.log 內記錄的 agent_id 字串比對最近一筆 payload（ADR-0133 static vs 實際送出對照）——皆為字串鍵，AgentIdentity 轉換在函式體進行 |
| `routers/debug.py` | L195, L207 | `LOCAL_IMPORT_BAN` | 🔴 不可修復 | 延遲載入 `permission_service` 與 `app` 實例（破壞循環導入）；IDOR 修復後 `resource_manager` 已改模組頂層匯入、不再需要延遲 |
| `services/identity_router.py` | L65, L187 | `B008` | 🔴 不可修復 | FastAPI 依賴注入同上（含 InfoSec 修復後新增的第二個 get_verified_identity 函式；ADR-0131 LDH 收斂刪行後行號 -3） |
| `routers/models.py` | L97, L225, L248, L267, L288 | `IDENTITY_SSOT` | 🔴 不可修復 | FastAPI 路由層：`model_id: str` 為 URL path 參數，AgentIdentity 轉換在函式體進行 |
| `routers/models.py` | L75 | `IDENTITY_SSOT` | 🔴 不可修復 | `ModelHealthResponse.model_id` 為模型識別碼而非代理人身分，API 合約必須為純字串（2026-06-11 code review 修復 #5：原誤型別為 AgentIdentity 導致回應序列化成物件） |
| `routers/custom_models.py` | L38 | `IDENTITY_SSOT` | 🔴 不可修復 | `_get_model_lock(model_id: str)` 為低階鎖原語（per-model-id TOCTOU 串行化，2026-06-11 code review 次要修復），僅需 string key；身分校驗由呼叫端路由完成（同 `mission_lock.py` 前例） |
| `routers/souls.py` | L138, L164, L194 | `IDENTITY_SSOT` | 🔴 不可修復 | FastAPI 路由層：`soul_id: str` 為 URL path 參數，同上 |
| `services/approval_service.py` | `PendingApproval.agent_id` | `IDENTITY_SSOT` | 🔴 不可修復 | hitl-unification H2：待批快照欄位——顯示與 `owned_agents` 所有權比對用的 agent 識別字串；授權判定主體是解算端點（tools_approval.py）的已驗證 `AgentIdentity`，非此欄位 |
| `routers/tools_approval.py` | approve_tool_call, reject_tool_call, cancel_tool_interaction | `B008` | 🔴 不可修復 | hitl-unification H2／ADR-0205：統一審批／MRTR 解算端點的 get_verified_identity 依賴注入（Guard #24 合規；is_human 硬性閘在函式體） |
| `routers/stats.py` | get_legacy_sync_stats, trigger_legacy_sync | `IDENTITY_SSOT` | 🔴 不可修復 | FastAPI 路由層：`agent_id: str` 為 URL path 參數；`AgentIdentity.from_str()` 轉換在函式體內完成，is_owner() 執行授權驗證 |
| `routers/stats.py` | import 區 | `TC002` | 🔴 不可修復 | ADR-0082：`Request` 用於 `/stats` 端點簽名取 `app.state.dispatch_supervisor`；FastAPI DI 需執行期型別（同 heartbeat.py / dispatch_callback.py 慣例） |
| `routers/system.py` | get_sliding_window_config, update_sliding_window_config, run_restore (get_current_user + auth_service 密碼閘), backup_stats, delete_backup, download_backup, get_backup_schedule, update_backup_schedule, export_settings, import_settings (get_current_user + auth_service 密碼閘) | `B008` | 🔴 不可修復 | FastAPI 依賴注入專用模式，Depends() 置於參數預設值（軌1 Phase 1：還原密碼閘/刪除/統計；Phase 2：加密下載；Phase 3：排程讀寫端點；ADR-0112：設定快照匯出/匯入端點） |
| `models/sliding_window.py` | keep_must_be_positive, upper_must_exceed_keep | `TRY003` | 🟢 已修復 | 已重構為專屬例外 SlidingWindowValidationError |

### MCP OAuth 基礎設施（ADR-0206）

| 檔案 | 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|------|
| `protocols/mcp/oauth/keyring.py` | L21 | `FILELOCK_ASYNC_SSOT` | 🔴 不可修復 | keyring atomic secret writer 的單一跨 coroutine／process 鎖定邊界；不得退回同步 `FileLock` 阻塞事件迴圈。 |
| `protocols/mcp/oauth/profile.py` | L17 | `FILELOCK_ASYNC_SSOT` | 🔴 不可修復 | per-profile mutation／egress fence 的單一跨 coroutine／process 鎖定邊界，防止 profile 變更與 discovery/DCR/token request 競態。 |
| `protocols/mcp/oauth/workload/models.py` | L1 | `TC002` | 🔴 不可修復 | `SecretStr` 與 `OAuthAuthorizationSlotKey` 參與 Pydantic 執行期 schema／nested validation，移入 TYPE_CHECKING 會留下未完成模型。 |
| `protocols/mcp/oauth/workload/profile.py` | import 區 | `FILELOCK_ASYNC_SSOT` | 🔴 不可修復 | workload profile JSON generation CAS 的單一跨 coroutine/process mutation lock；同步 FileLock 會阻塞事件迴圈。 |
| `protocols/mcp/oauth/workload/delegation.py` | import 區 | `FILELOCK_ASYNC_SSOT` | 🔴 不可修復 | owner delegation JSON generation CAS 的單一跨 coroutine/process mutation lock。 |
| `routers/mcp_oauth_workload.py` | control endpoints（27） | `B008` | 🔴 不可修復 | ADR-0207：admin/owner identity、lifespan control、rotation services 使用 FastAPI `Depends()` 注入。 |
| `routers/mcp_oauth_workload.py` | import 區（4） | `TC001/TC002` | 🔴 不可修復 | `Request`、`AgentIdentity` 與兩個 request body model 必須留在 runtime globals，供 FastAPI 在 postponed annotations 下解算路由依賴與 body schema。 |
| `routers/mcp_oauth_workload.py` | owner delegation routes（3） | `IDENTITY_SSOT` | 🔴 不可修復 | `agent_id` 是未信任 URL path 識別字；路由同時注入已驗證 `AgentIdentity`，control service 以 request human principal＋active owner mapping 授權後才使用此鍵，不能把 path parameter 型別改成 identity object。 |

| `routers/dispatch_callback.py` | L17 | `TC002` | 🔴 不可修復 | ADR-0051：`Request` 用於 FastAPI dependency `verify_dispatch_callback` 的執行時期注入（`request.client.host` IP 驗證），無法移入 TYPE_CHECKING 區塊 |
| `routers/dispatch_callback.py` | L35, L70 | `B008` | 🔴 不可修復 | FastAPI 依賴注入：`Header(alias=...)` 與 `Depends(verify_dispatch_callback)` 在參數預設值中使用 |
| `routers/setup.py` | L42, L43, L52, L53 | `B008` | 🔴 不可修復 | ADR-0053：Setup 端點的 `require_admin` 與 `get_setup_service` 依賴注入，符合 FastAPI Depends 慣例 |
| `routers/skills.py` | `export_skill`, `install_skill`, `get_skill_catalog`, `install_catalog_skill` | `B008` | 🔴 不可修復 | ADR-0066/0074：技能 export/install + 真技能策展目錄端點的依賴注入 |
| `routers/creation.py` | `create_skill_endpoint`, `create_mcp_endpoint`, `scaffold_mcp_endpoint` | `B008` | 🔴 不可修復 | ADR-0069 Phase D：創建端點 Owner/admin 依賴注入（Depends 置於參數預設值） |
| `routers/ui_sessions.py` | resume_session_stream, list_active_streams | `B008` | 🔴 不可修復 | ADR-0081 可恢復串流端點的 `_require_authenticated` 依賴注入（Guard #24 合規） |
| `routers/ui_interjections.py` | add_interjection, list_interjections, retract_interjection | `B008` | 🔴 不可修復 | ADR-0249 插話端點的 `_require_human_requester` 依賴注入（Guard #24 合規） |
| `routers/feedback.py` | `submit_feedback`, `get_session_feedback`, `get_recent_feedback` | `B008` | 🔴 不可修復 | ADR-0084：回饋端點（提交/還原/檢視）的依賴注入（Depends 置於參數預設值；含 owner-gate） |
| `routers/chat_upload.py` | `upload_chat_file`（`File(...)`／`Form(...)`／`Depends(get_verified_identity)`） | `B008` | 🔴 不可修復 | 聊天上傳 Phase 2：multipart 上傳端點的 FastAPI 依賴注入（File/Form/Depends 置於參數預設值，框架慣例）|
| `routers/ask_user.py` | `submit_answer` | `B008` | 🔴 不可修復 | ADR-0085：ask_user 答案端點的依賴注入（Depends 置於參數預設值） |

### 服務層 (Services)

| 檔案 | 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|------|
| `services/dispatch_callback_store.py` | L109, L117 | `PLW0603` | 🔴 不可修復 | ADR-0051 Singleton 模式：`get_callback_store` 與 `_reset_callback_store` 需要 `global _store` 宣告維護模組級 singleton；等同既有 `session_manager.py` 等 Singleton 模式 |
| `services/ask_user_store.py` | `get_ask_user_store`, `_reset_ask_user_store` | `PLW0603` | 🔴 不可修復 | ADR-0085 Singleton 模式：同 dispatch_callback_store，`global _store` 維護模組級 singleton |
| `services/setup_service.py` | L85, L89, L98, L101, L118, L121, L124 | `ENV_SSOT_VIOLATION` | 🔴 不可修復 | ADR-0053：`SetupService` 必須讀取目前生效的環境變數（非 Pydantic Settings 快取），以回報系統的真實設定狀態；含 bug② `_is_workspace_root_default`（WORKSPACE_ROOT）＋D#15 `_get_configured_api_keys` 改 registry 衍生 provider 清單（值仍讀 os.environ、含 tavily）|
| `services/setup_service.py` | L37, L40, L150, L156, L170 | `TRY003` | 🔴 不可修復 | ADR-0053：欄位輸入驗證錯誤訊息需說明具體欄位名稱（如 "dispatch_token must not be empty"），採局部變數模式；L37/L40 為 bug② `_validate_host_dir`（宿主目錄非空／原生模式目錄存在驗證）helper |
| `config.py` | L107 | `LOCAL_IMPORT_BAN` | 🔴 不可修復 | Bootstrap 循環破壞 |
| `services/auth_service.py` | L54 | `LOCAL_IMPORT_BAN` | 🔴 不可修復 | `resource_manager` 初始化時序競態破壞 |
| `services/telemetry.py` | L12 | `TID251` | 🔴 不可修復 | 舊版背景任務的物理路徑定址 (SSOT 豁免) |
| `services/authorization_gateway.py` | 5 個共用授權函式＋`require_human_requester`（ADR-0249）的 `Depends(get_verified_identity)` | `B008` | 🔴 不可修復 | ADR-0239：Authorization Gateway；各 router 的 `_require_*` 改為指向這 5 個函式的別名，原本分散的 B008 收斂於此 |
| `services/execution_broker.py` | `run_sync` | `S603` | 🔴 不可修復 | ADR-0239：同步子程序的單一啟動入口，純轉傳 argv／kwargs；TID251 由 `ruff.toml` per-file-ignores 豁免 |
| `services/permission_service.py` | L76-L89 | `SLF001` | 🔴 不可修復 | 單例模式重設時存取內部屬性；P5 同步清除 `_permission_config_health`，避免測試單例沿用 degraded 快照 |
| `services/resource_manager.py` | L54, L389, `get_vault_path_for_agent` | `LOCAL_IMPORT_BAN` | 🔴 不可修復 | `async_db` 反向導入破壞；`get_vault_path_for_agent` 延遲載入 `identity_service`，破 identity_service→permission_service→resource_manager 循環（跨身分看板決定論定址） |
| `services/resource_manager.py` | `get_vault_path_for_agent` | `IDENTITY_SSOT` | 🔴 不可修復 | 跨身分定址入口以 `agent_id: str` 為鍵——呼叫端（如 `MissionManager._get_mission_path` 的 `target_agent_id`）僅持有字串；本方法即負責於函式內經 `identity_service.get_identity(agent_id)` 查真實註冊身分後才物件化定址 |
| `services/skill_service.py` | L30 | `SLF001` | 🔴 不可修復 | 單例重設 |
| `services/sns_gateway.py` | 已移除 | `LOCAL_IMPORT_BAN` | 🟢 已修復 | ADR-0276：啟動時注入服務 context，事件入口不再延遲匯入 main。 |
| `services/soul_service.py` | L59, L206, `inject_context_summary` | `LOCAL_IMPORT_BAN` | 🔴 不可修復 | `soul_service`/`resource_manager`/`async_db` 循環破壞（含 ADR-0092 context_summary 讀取） |
| `services/context_snapshot_service.py` | `get_context_snapshot` harness (×3) | `LOCAL_IMPORT_BAN` | 🔴 不可修復 | ADR-0133：快照 harness 於函式內延遲載入 `AnthropicRequest`／`PipelineContext`／`proxy_service.pipeline`——後者匯入整條中介層 pipeline，頂層匯入會與 services 層形成重循環 |
| `tools/subagent_tools.py` | `_assemble_subagent_request` | `LOCAL_IMPORT_BAN` | 🔴 不可修復 | Phase 1 in-process 子代理：組裝走真實 `proxy_service.pipeline`（ADR-0133 同款 harness），頂層匯入會經 middleware→tools 形成重循環 |
| `services/user_service.py` | L77, L108, L143, L175, L190, L199, L209 | `SLF001` | 🔴 不可修復 | 呼叫 `AuthService` 的私有 `_atomic_users_write` 進行 users.json 的安全原子落庫 (N11 fix；含 DA25 `change_own_password` 自助改密碼) |
| `services/agent_reset/guard.py` | L33 | `BLE001` | 🔴 不可修復 | 軌B Batch 6：`_has_real_persona` 列舉真實 agent 失敗時無法證明目標「非真實 agent」，保守視為有 persona→拒絕重置（fail-closed 安全閘） |
| `services/agent_reset/primitives.py` | L151 | `BLE001` | 🔴 不可修復 | 軌B Batch 6：同行程記憶模組（ToukaMemory）未載入或初始化失敗時整段不可用，`_memory_manager` 須廣捕優雅降級為「跳過記憶清除」，不得中斷其餘 store 清除 |
| `services/agent_reset/orchestrator.py` | L169, L180 | `BLE001` | 🔴 不可修復 | 軌B Batch 6：破壞性重置採 best-effort——單一 store 的 purge（L169）或殘留自檢（L180）失敗須收進報告並續清其餘 store，不可因單點失敗中斷整體清除 |
| `services/memory_scrub/swap.py` | `guard_item` | `BLE001` | 🔴 不可修復 | ADR-0270：清除連備份「一檔一個邊界」——任何一檔的意外只記日誌、記成這一檔的 `internal_error` 失敗項目，不中斷其他檔；已知的失敗（記憶庫、檔案、zip 錯誤）在前面各自轉成原因代碼 |
| `services/memory_scrub/orchestrator.py` | `_attempt` | `BLE001` | 🔴 不可修復 | ADR-0270：每一類別與重查的邊界——類別本身出錯時留下 `kind=category` 的失敗項目（`internal_error`），重查失敗記成未知；不讓一類的意外吞掉其他類別與稽核 |
| `services/memory_scrub/orchestrator.py` | `run_after_reset` | `BLE001` | 🔴 不可修復 | ADR-0270：清除開始後的安全網——重置已經成功，任何一般意外都改成 `ran=True、complete=False、reason=internal_error` 的報告（端點照常 200）並補寫結果稽核；`CancelledError` 照樣往外丟 |
| `services/isolation/executor_server.py` | `make_private_dir`、`_claim_ipc`（×2） | `IO_PRISON_VIOLATION` | 🔴 不可修復 | ADR-0272 §2.1：執行器伺服器跑在獨立的 `touka-executor` 容器，依設計不可載入 `src.config`／ResourceManager（匯入邊界由 `tests/lint/test_executor_server_imports.py` 釘住），同 Heartbeat「獨立容器本地檔案存取」先例。三處：建自己的 state 資料夾（`Path.mkdir`，0700）、ipc 的 flock 鎖檔（`os.open`）、啟動時刪殘留的 socket 與 `alive`（`Path.unlink`）。其餘檔案動作用 mkstemp＋`Path.replace`、umask 建 socket、`os.scandir`、`read_bytes`、`shutil.rmtree`，不需豁免 |
| `sandbox_kernel/launcher.py` | `_exec`（`target_env`） | `TID251`、`ENV_SSOT_VIOLATION` | 🔴 不可修復 | ADR-0275 §4：啟動器是信任根，只能用標準函式庫、以 `python -I -S` 依路徑執行（禁令 66②），不能經 `src.config`；`os.environ` 是 spawn 端已經過允許清單的那份環境。只在套完 Landlock、rlimit、關 fd 之後讀一次，和 spec 的 `exec_env`（已驗證、禁令 19 那一套已移除）合併成交給 `execvpe` 的環境，不當設定讀、也不寫回自己的 `os.environ`。同 ToukaDispatch `_build_child_env`「獨立進程無法依賴 `src.config`」先例 |

### 工具與工具箱 (Tools & Utils)

| 檔案 | 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|------|
| `tools/grok_imagine_server.py` | L15 | `BASICCONFIG_BAN` | 🔴 不可修復 | 獨立 MCP 伺服器必須將日誌導向 stderr 以保護 stdout 作為 JSON-RPC 通訊通道；無法共用 `logging_config.py` 的 setup_logging()（後者輸出至 stdout） |
| `tools/execution_context.py` | `ToolExecutionContextKwargs.agent_id` | `IDENTITY_SSOT` | 🔴 不可修復 | ADR-0214：builder 只接受已驗證 `AgentIdentity`；此欄位是投影給 legacy tool kwargs 的字串 metadata，不作授權 principal。 |
| `models/history.py` | L28 | `RAISE_STRING_BAN` | 🟢 已修復 | 已重構為自定義 HistoryValidationError |
| `protocols/manager.py` | L106 | `RAISE_STRING_BAN` | 🟢 已修復 | 已重構為自定義 ProtocolError |
| `tests/unit/test_h1_h2_h6_security.py` | L162 | `S105` | 🔴 不可修復 | 測試 mock 設定 refresh_token_jti 欄位為已知測試字串，非真實密碼 |
| `main.py` | L23 | `F401` | 🔴 不可修復 | 確保 `config` 優先載入以設定環境變數 |
| `main.py` | L88 | `TRY003`, `TRY301` | 🔴 不可修復 | 啟動階段的異常捕獲與提示 |
| `main.py` | L212 | `B008` | 🔴 不可修復 | FastAPI 依賴注入 |
| `tools/editor.py` | L38-L52 | `TRY003` | 🟢 已修復 | 已將權限異常訊息提取至局部變數 |
| `tools/editor.py` | L83-L89 | `ARG001` | 🔴 不可修復 | 統一工具介面協議規定的未使用參數 |
| `tools/editor.py` | L125 | `S104` | 🔴 不可修復 | 監聽全介面 (IDE 通訊) |
| `utils/async_db.py` | 已移除 | `S608` | 🟢 已修復 | ADR-0276：嚴格家快照採固定 SQL 字面量與兩個參數，不再需要豁免。 |
| `utils/custom_io.py` | L13 | `TID251` | 🔴 不可修復 | 跨模組物理路徑解析備援 |
| `services/process_registry.py` | L30 | `S603`, `S607` | 🔴 不可修復 | P5d：Windows `taskkill /F /T` 連坐殺 bash 孫進程；固定參數 list + shell=False，零注入面 |

---

## ToukaDispatch（`ToukaDispatch/`）

### `server.py`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L30, L43 | `ENV_SSOT_VIOLATION` | 🔴 不可修復 | ToukaDispatch 為獨立進程入口點（ADR-0082 後由 proxy spawn，仍非 proxy 模組），無法依賴 ToukaProxy 的 `src.config` 模組；必須直接讀取 `DISPATCH_PORT`、`DISPATCH_TOKEN` 環境變數（Vuln 3：`import secrets` 移除後行號位移，淨計數不變） |
| L33 | `ENV_SSOT_VIOLATION` | 🔴 不可修復 | `TOUKA_PROXY_URL`：ADR-0051 回呼通道需要 ToukaProxy URL；獨立進程無法從 ToukaProxy `src.config` 讀取，必須直接讀取環境變數（ADR-0082 後由 DispatchSupervisor 注入 localhost 值） |
| `_bind_host` | `ENV_SSOT_VIOLATION` | 🔴 不可修復 | ADR-0082：綁定位址 secure-by-default（預設 127.0.0.1、compose 顯式升開）；獨立進程入口點直讀 `DISPATCH_BIND_HOST`（原 L38 硬編 `0.0.0.0` 的 `S104` 豁免隨之移除） |
| `_build_child_env` | `ENV_SSOT_VIOLATION` | 🔴 不可修復 | ADR-0067：自動召喚需 whitelist 淨化 `dict(os.environ)` 後傳給外部 AI 子進程；獨立進程直讀環境變數 |
| `_invoke_external_ai` | `S603` | 🔴 不可修復 | ADR-0067：fire-and-forget 召喚外部 AI；command 全來自 dispatch_hooks.json 白名單 list + shell=False，零指令注入面 |
| L52 | `IDENTITY_SSOT` | 🔴 不可修復 | `_DispatchIdentity` 使用 `agent_id: str` 欄位——獨立容器協約，無法 import ToukaProxy `AgentIdentity` |
| L237, L263 | `BLE001` | 🔴 不可修復 | ADR-0051：`_notify_touka_proxy` 與回呼觸發段落需靜默失敗——回呼網路失敗不應中斷外部 AI 的主要任務回報 |

---

## 腳本與工具（`scripts/`）

| 檔案 | 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|------|
| `lint_ssot.py` | L11 | `TC003` | 🔴 不可修復 | `Iterator` 用於 `get_files()` 的執行時期型別標註，無 `from __future__ import annotations` 時移入 TYPE_CHECKING 會導致 `NameError` |
| `backfill_session_owner.py` | L36, L50 | `LOCAL_IMPORT_BAN` | 🔴 不可修復 | 一次性運維腳本的 script-only 延遲載入（`src.config`／`src.utils.async_db`）：頂層匯入會使 `--help` 都觸發完整 config 載入（bootstrap/load_dotenv）；亦利於測試以 importlib 沙盒載入 |
| `hermetic_gate/npm_offline_gate.py` | `_run` | `S603` | 🔴 不可修復 | P8-B RED 1 只接受 resolved absolute npm executable 與固定 lifecycle argv，`shell=False`；offline cache miss／非零 exit fail-loud。 |
| `hermetic_gate/npm_offline_gate.py` | CLI environment | `ENV_SSOT_VIOLATION` | 🔴 不可修復 | 獨立 hermetic CLI 必須先看 inherited keys，大小寫不敏感拒絕 npm/proxy overrides，再重建 credential-free allowlist child env；production 業務碼不直接讀 env。 |
| `record_tool_eval_baseline.py` | pre-import sandbox environment | `ENV_SSOT_VIOLATION` | 🔴 不可修復 | ADR-0214 manual CLI 在 provider-capable product import 前，必須把 config／bootstrap／dynamic model／dispatch／judge／search 寫入面錨定至受控設定。 |
| `ToukaIDE/scripts/build_marketplace_candidate.py` | `_offline_child_environment`, `_run_subprocess` | `ENV_SSOT_VIOLATION`, `S603` | 🔴 不可修復 | ADR-0218：release CLI複製child env後移除publish bearer並強制npm offline；單一離線子進程咽喉只接受解析後的Node、驗證過的package-local npm／repo-local vsce JavaScript entry與固定Git argv；`shell=False`，且不含login／publish。 |

---

### PR72 精確入口增量（2026-10-06）

| 路徑／接點 | 規則 | 原因與限制 |
| --- | --- | --- |
| `scripts/guard_eslint_ratchet.py` readonly verify | `S603` ×1 | resolved Node、固定helper／argv、shell=False；verify失敗不進lint。 |
| `scripts/lint_ratchet.py::collect_ruff` | `S603` ×1 | ADR-0278：以 `sys.executable -m ruff` 跑固定 argv（四個產品資料夾、`--no-cache`、JSON 輸出），`shell=False`、有逾時；ruff 異常結束就失敗，不當成 0 個違規。 |
| `tests/lint/test_tests_readme_listing.py::git_ls_files` | `S603` ×1 | ADR-0278：以 `shutil.which` 解析出的 git 跑固定 argv `ls-files -z -- <pathspec>`（`tests` 或全 repo），`shell=False`、`check=True`；找不到 git 就失敗、不跳過。 |
| `scripts/list_theme_violations.py`、`scripts/summarize_theme_violations.py` readonly verify | `S603` ×2 | 同一固定helper，每支各一筆；不執行呼叫者任意程式。 |
| 上述兩支theme工具的既有固定shell命令 | `S602` ×2 | 保留既有命令／診斷語義，新增verify先行；不接收自由shell輸入。 |
| `tests/unit/test_braces_backport.py::_node` | `S603` ×1 | 固定bound Node、normal test sandbox／closed env、20秒；最終第163行，A有限30 PASS／0 skip。 |

## 測試與驗證（`tests/`）

Q12（2026-09-09）新增 1 處 `PATH_LITERAL_SSOT`：本筆 aggregate 315→316、該規則 7→8；僅固定官方映像 Config 樣本，沒有變更業務路徑解析。


| 檔案 | 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|------|
| `unit/test_p8b_supply_chain_gate.py` | Dockerfile PATH oracle | `PATH_LITERAL_SSOT` | 🔴 不可修復 | 精確核對 Dockerfile PATH 契約的 fixture 字串，非執行期存取；不拆接或弱化 equality。 |
| `unit/test_hard_floor_roots_security.py` | L751, L752 | `PATH_LITERAL_SSOT` | 🔴 不可修復 | 核對 `ResourceManager` 在 Docker 回傳的容器工作區掛載點（compose 的 `/app/workspace`）；期望值必須是字面值，不能用被測的產品屬性算（ADR-0246 R-2c）。其他仿佈局測試改用中性前綴 `/ctr`，不需要豁免。 |
| `integration/test_api_schema_sync.py` | L58 | `TRY003` | 🔴 不可修復 | 測試失敗時的詳細錯誤提示 |
| `integration/test_docker_deployment.py` | L104, L121 | `S603` | 🔴 不可修復 | 獲取出錯容器的運行日誌以便 debug |
| `integration/test_session_sandbox_no_residue.py` | `_run_child_pytest` | `S603` | 🔴 不可修復 | ADR-0251：E2E 用子程序跑這個直譯器的 pytest，確認程序結束後系統暫存沒有留下 `touka_test_session_*`；argv 固定、`shell=False`、乾淨環境、有逾時。 |
| `unit/test_facts_schema_migration.py` | L792（`test_t20_crash_journal_is_rolled_back_on_reopen`） | `S603` | 🔴 不可修復 | ADR-0256：要模擬升級中途當掉、留下殘留 journal，只能讓真的子程序在交易寫到一半時 `os._exit(1)`。子程序只用標準庫（`os`、`sqlite3`、`sys`），以 `sys.executable -I` 啟動（隔離模式：不讀 `PYTHONPATH`、不載入專案程式）；argv 固定、`shell=False`、有逾時，只寫 pytest `tmp_path` 裡合成的 DB。 |
| `smoke/run_all_smoke_tests.py` | L48 | `S603` | 🔴 不可修復 | 冒煙測試自動化指令執行 |
| `unit/test_ldh_cardless_no_type_flag.py` | L44 | `SLF001` | 🔴 不可修復 | G03 回歸鎖需直接呼叫 `_atomic_users_write` 清空沙箱 users.json，建立「系統未認領」的 LDH 前提 |
| `unit/test_soul_stepup_audit.py` | L27 | `SLF001` | 🔴 不可修復 | G05 AUDIT 回歸鎖需直接呼叫 `_atomic_users_write` 清空沙箱 users.json，建立 admin 驗步升稽核軌跡 |
| `unit/test_soul_model_registration_cache.py` | L34, L40, L43, L54, ＋G20/G22 | `SLF001` | 🔴 不可修復 | G07 快取權威回歸鎖需直接讀 `model_manager._models_cache`／呼叫 `_save_config`、並 `inspect.getsource(souls._update_models_config)` 驗不繞快取直寫；G20/G22 冪等/裸條目驗證亦需讀 `_models_cache` |
| `unit/test_env_persistence_anchor.py` | L55 | `SLF001` | 🔴 不可修復 | G13 探針鍵純度守衛需讀 `config._ENV_PERSISTENCE_PROBE_KEYS` 與雙 compose 的 proxy environment 做交集為空斷言 |
| `unit/test_rhythm_logic.py` | L42 | `SLF001` | 🔴 不可修復 | 測試內部狀態決策邏輯 |
| `unit/test_user_service_concurrency.py` | L42, L99 | `SLF001` | 🔴 不可修復 | 並發測試需直接呼叫 `_atomic_users_write` 設置 token 狀態與模擬競態旋轉 |
| `unit/test_auth_service_data_resilience.py` | L71, L90, L107, L131, L153, L154, L179, L185 | `SLF001` | 🔴 不可修復 | 資料韌性測試需直接呼叫 `_read_users`、`_write_users`、`_atomic_users_write` 驗證內部行為 |
| `unit/test_heartbeat_token_ssot.py` | L87 | `SLF001` | 🔴 不可修復 | Token 優先鏈測試需存取 `StorageService._env_token` 驗證注入值 |
| `unit/test_mission_manager_bugfixes.py` | L246 | `SLF001` | 🔴 不可修復 | C2 修復驗證：直接呼叫 `_resolve_target_agent_id` 確認非 Supervisor 身分拋出 PermissionError |
| `unit/test_permission_service.py` | 多處 | `SLF001` | 🔴 不可修復 | PermissionService 突變補測：直接呼叫 `_check_pattern`／`_load_config`／`_load_identity_map`／`_load_policies` 驗證私有載入與安全邏輯邊界；P2 授權解析改走公開 `resolve_grants` |
| `unit/test_permission_service_sandbox_isolation.py` | L34-L83 | `ENV_SSOT_VIOLATION` | 🔴 不可修復 | 測試動態切換 TOUKA_CONFIG_DIR 環境變數與路徑重置。 |
| `unit/test_live_api_consent.py` | process environment oracle | `ENV_SSOT_VIOLATION` | 🔴 不可修復 | ADR-0214 回歸鎖須驗證 pytest 在任何產品 import 前停用計量功能、清空 provider credential 並錨定 sandbox env file。 |
| `unit/test_smoke_sandbox_governance.py` | process environment mutation oracle | `ENV_SSOT_VIOLATION` | 🔴 不可修復 | ADR-0214 回歸鎖須對 sandbox helper 的 process environment mutation 做完整斷言，並在 `finally` 還原原始 mapping。 |
| `proxy/characterization/harness.py` | `_is_ci` | `ENV_SSOT_VIOLATION` | 🔴 不可修復 | model-p1a U4：讀 `CI`（GitHub Actions 預設 `CI=true`），CI 上缺 golden 改為失敗、本機維持錄製＋skip；測試基建門檻，非產品設定。 |
| `integration/test_server_lifecycle.py` | L37 | `S603` | 🔴 不可修復 | 啟動與中斷 ToukaProxy 伺服器整合測試，需要呼叫 subprocess.Popen |

---

---

## ToukaChat（`ToukaChat/src/`）

### `src/lib/ThemeContext.tsx`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L26, L34 | `LOCALSTORAGE_SSOT` | 🔴 不可修復 | Theme SSOT 初始化讀取，純字串，SSR 相容需直接存取 |
| L73, L79 | `LOCALSTORAGE_SSOT` | 🔴 不可修復 | CSS 變數應用前必須先持久化，且需同步觸發 `applyTheme/applyFontSize` |

### `src/lib/SystemStatusContext.tsx`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L37 | `LOCALSTORAGE_SSOT` | 🟢 已修復 | 已改用 getLocalStorageItem 規格化讀取 |

### `src/hooks/settings/useSettingsTab.ts`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L18, L27 | `LOCALSTORAGE_SSOT` | 🟢 已修復 | 已改用 useLocalStorage 統一存取 |

### `src/app/page.tsx`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L76 | `LOCALSTORAGE_SSOT` | 🟢 已修復 | 已改用 getLocalStorageItem 規格化讀取 |

### `src/lib/connections.ts`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L63 | `FETCH_SSOT` | 🔴 不可修復 | `refreshAccessToken` 認證續期直呼 `/auth/refresh`；不可經 `fetchTouka`（否則觸發其送出前續期而遞迴），且為 single-flight SSOT |
| L232 | `FETCH_SSOT` | 🔴 不可修復 | `fetchTouka` 核心定義：此處即為授權的 HTTP 客戶端 SSOT，必須調用原始 `fetch` |
| L371 | `FETCH_SSOT` | 🔴 不可修復 | `fetchChatStream` 多供應商 SSE 串流（Ollama/OpenAI/xAI），URL 格式與 `fetchTouka(proxyUrl, path)` 簽名不相容，且 Auth 已手動嵌入 Headers |

### `src/hooks/useAgentSSE.ts`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L93 | `FETCH_SSOT` | 🟡 可修復（低） | SSE 串流 fetch 連線在 hook 內部定義，需自行傳入 streamUrl |
| L133 | `FETCH_SSOT` | 🟡 可修復（低） | Hook 接受呼叫方提供的完整 URL（`postUrl`），可能含 IDE 外部端點，無法拆分為 `fetchTouka(proxyUrl, path)` 格式；未來可重構 hook 介面為 `(proxyUrl, path)` 形式 |
| L155 | `FETCH_SSOT` | 🔴 不可修復 | Fire-and-forget LSP 健康探針（`.catch(() => {})`），無需 Auth，改用 fetchTouka 無實質安全收益 |

### `src/lib/error_handler.ts`

| 行號 | 規則 | 狀態 | 原因 |
|------|------|------|------|
| L76 | `CONSOLE_BAN` | 🔴 不可修復 | `logSSOTError()` 是全系統 Console 呼叫的 **SSOT 封裝層**，其內部必須呼叫 `console.error` 以完成日誌輸出職責。行級豁免精確限定於此行，不允許擴散至其他位置。 |


---

## 擴充指引

新增 `# noqa` 時，請同步在此文件補充一列，格式如下：

```
| L行號 | `規則代碼` | 🔴/🟡 狀態 | 一句話說明豁免原因 |
```

> 若同一規則在同一檔案多次出現，可合併為一列並標註行號範圍（如 `L53, L69`）。

### ADR-0228 原生工具契約（2026-09-15；精確 +2）

| 檔案／欄位 | 規則 | 理由 |
|---|---|---|
| `ToukaMemory/src/memory_server/tools/contracts.py`／`MemoryDuplicatesInput.target_id` | `IDENTITY_SSOT` | factual 記錄識別字，並非授權 principal；身分仍由已驗證 context 注入。 |
| `tests/unit/test_native_tool_payload_contracts.py`／context 注入斷言 | `PAC_MANUAL_LEVEL_CHECK` | 只核對正式執行入口傳給 backend 的既有安全級，不在產品重刻策略。 |


## 可靠性 R2 精確增量（2026-09-21，來源候選）

| 檔案／接點 | 規則 | 理由 |
|---|---|---|
| `ToukaProxy/src/routers/ui_sessions.py`／stream state Depends | `B008` +1 | FastAPI 執行期依賴注入，沿原認證門檻。 |
| 同檔／SessionSnapshotResponse import | `TC001` +1 | FastAPI 在 runtime 解算回傳型別，不可移至 TYPE_CHECKING。 |
| `ToukaProxy/src/routers/proxy.py`／UI admission | `ERROR_SSOT_VIOLATION` +1 | 固定 `FailedResult` envelope 的 precommit revision 衝突以 409 拒絕；沒有原輸入或 cause。 |
| `tests/unit/test_session_persistence_ordering.py`／auth_level 讀回 | `PAC_MANUAL_LEVEL_CHECK` +2 | owned SQLite 回歸斷言 None 不變更既有安全級，非產品授權判定。 |
| 同檔／三處故障注入 SQL | `S608` +3 | column 僅來自有限 pytest 參數，值仍參數化，資料庫由 owned fixture 建立。 |

Guard19 的標準規則集合不統計 TC001／S608；此處登錄理由，不虛增架構規則總數。歷史 211 起點不變。


## ADR-0246 U2c-4 精確增減（2026-10-12）

| 檔案／接點 | 規則 | 理由 |
|---|---|---|
| `ToukaProxy/src/routers/ui_sessions.py`、`routers/debug.py`、`routers/heartbeat.py`／`AuthMode` import | `TC001` +3 | 三模式 Literal 用在 Pydantic request model 欄位；模組有 postponed annotations，移入 TYPE_CHECKING 會留下未完成的 schema。 |
| `tests/unit/test_monitor_authlevel_gate.py` | `PAC_MANUAL_LEVEL_CHECK` -5 | 模式授權移到 session 寫入；本檔改斷言 MonitorLayer 不產生模式，用 `ctx.auth_level` 屬性讀取，不再需要豁免。 |
| `tests/unit/test_auth_mode_authority_security.py`／快照與清單讀回 | `PAC_MANUAL_LEVEL_CHECK` +2 | 沙箱 DB 寫入舊值後，斷言公開快照與清單回傳對照後的模式名稱；非產品授權判定。 |
