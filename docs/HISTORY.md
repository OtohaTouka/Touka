# 📚 Touka 系統開發歷史 (Development History)

> 本文件保存各歷史階段的成果與背景；「已達成」只限各條所述當時範圍，不表示全部計畫或目前待辦已完成。唯一現況入口為 [HANDOVER.md](../HANDOVER.md)，有效規範以根 [AGENTS.md](../AGENTS.md) 為準；以下舊命令不授權直接執行。
> 2026-10-04 整理前的 AGENTS／HANDOVER 完整原文已隨 ADR-0238 刪除，只留在開源前的私人歷史。

最近來源成果見 [CHANGELOG 摘要](CHANGELOG.md)，完整更新紀錄按 [月份](changelog/README.md)歸檔。

## 📌 已達成目標清單 (Completed Objectives)

1. **[已達成] 多使用者身分系統 (Human Auth)**：實作 `users.json` SSOT、雙軌 Token (Access/Refresh) 與前端路由守衛 (`AuthGuard`)。
2. **[已達成] Bcrypt 72-Byte 限制根治**：引入 SHA256 預雜湊機制，徹底解決長密碼導致的 500 錯誤。
3. **[已達成] Heartbeat 安全加固**：實施 Service JWT 與擁有者 Refresh Token 效期聯動檢查。
4. **[已達成] 自主預算硬治理**：確保 Agent 決策鏈符合預算，防止無限循環。
5. **[已達成] API 合約自動驗證與 Parity Guard**：後端 Pydantic 模型與物理 `openapi.json` 100% 同步，且前端呼叫完全符合合約。
6. **[已達成] 時間、環境與錯誤碼主權硬執法 (Full-stack)**：透過 `lint_ssot.py`、`guard_time.ts` 與 `guard_errors.ts` 攔截所有違規的時間、環境變數與錯誤字串調用。
7. **[已達成] 全棧環境變數主權 (Env SSOT Guard)**：完成 Pydantic `BaseSettings` 遷移，達成 100% 配置中心化。
8. **[已達成] 錯誤碼主權 (Error Code Sovereignty)**：實作 `errors.json` 作為跨端唯一錯誤定義來源。
9. **[已達成] 啟動穩定化與自癒**：修復啟動異常並實施配置自動恢復。
10. **[已達成] 全能 I/O 代理化**：達成 100% 的 I/O 代理覆蓋。
11. **[已達成] 身分能力管理大一統**：將心跳權限成功整合至 Identity SSOT 體系。
12. **[已達成] 依賴主權與沙盒化驗證機制**：建立 `guard_dependencies.py` 在 Docker 打包前自動驗證 `requirements.txt` 完整性。
13. **[已達成] 安全會話管理 (Session Recovery)**：解決登出失效問題，統一 Cookie 路徑為 `/` 並實施前端憑證透傳。
14. **[已達成] Heartbeat 組件解耦 (SOLID)**：將單一 `awaken.py` 拆分為服務化架構，建立單元與整合測試集。
15. **[已達成] 部署等價性守衛 (Deployment Parity)**：在 `guard_dependencies.py` 中實作 Dockerfile 物理完整性檢查。
16. **[已達成] 群聊功能恢復與整合測試 (ToukaGroup Recovery)**：恢復群聊模型顯示並補全整合測試集。
17. **[已達成] 靈魂模型復原與 ID 統一化 (Soul Restoration)**：實施 ID 簡潔化（移除前綴），完成動態掃描與配置合併，並建立硬核保護機制。
18. **[已達成] ABAC 四階安全等級體系 (Auth Level Matrix)**：修正了 `Manual` 與 `SafeAuto` 的邏輯重疊，實施「禁止 -> 全手動 -> 半自動 -> 全自動」的精確決策。
19. **[已達成] 工具執行鏈路加固 (Auth Propagation)**：修復了 `ToolExecutor` 遺失 `auth_level` 的 Bug，確保 Terminal 工具能正確連動安全等級。
20. **[已達成] 安全等級越權攔截 (Auth Level Guard)**：在 `MonitorLayer` 實施權限核驗，非管理員嘗試開啟 FullYOLO 將被強制降級。
21. **[已達成] 管理員代理權限優化 (Identity Assumption)**：確保管理員在代理 Agent 時依然保留其管理 API 存取權限。
22. **[已達成] 身分物件化執法 (Identity Anchoring)**：全面重構 Proxy 路由層 DTO，強制實施從 `str` 到 `AgentIdentity` 的自動轉化。
23. **[已達成] 前端環境變數禁令 (Frontend Env Guard)**：實作 `guard_env.ts` 掃描器，達成 100% 前端配置中心化。
24. **[已達成] fetch() SSOT Guard（前端 HTTP 客戶端主權）**：實作 `guard_fetch.ts`（ADR-0024），強制前端所有 HTTP 請求走 `fetchTouka()`；修復 21 處違規，3 處合法 `# noqa: FETCH_SSOT`。整合至 `guard_all.py`。
25. **[已達成] 日誌主權靜態執法 (PRINT_BAN / BASICCONFIG_BAN)**：在 `lint_ssot.py` 新增 `PrintBanRule` + `BasicConfigBanRule`（ADR-0025）；`logging_config.py` 確立為唯一日誌初始化 SSOT 入口；`grok_imagine_server.py` 加 noqa。
26. **[已達成] Worktree 雙重掃描修復**：`lint_ssot.py` 的 `IGNORED_DIRS` 加入 `.claude`，消除 worktree 副本誤報，`guard_all.py` 全 15 項守衛全部通過。
27. **[已達成] localStorage SSOT Guard（前端存取主權）**：實作 `guard_localStorage.ts`（ADR-0026）；重構 `useConnectionSettings`、`useSnsBotSettings`、`connections/page.tsx` 使用 `useLocalStorage`；11 處合法 noqa。
28. **[已達成] RAISE_STRING_BAN（raise 硬編碼字串禁令）**：在 `lint_ssot.py` 新增 `RaiseHardcodedStringRule` + `check_Raise`/`visit_Raise` 鉤子（ADR-0027）；修復 `gemini_handler.py`、`dispatchers/utils.py` 改用 `ToukaException(ErrorCode.XXX)`；2 處合法 noqa。
29. **[已達成] 後端整合測試全面修復 (0 failed)**：修復 `proxy.py` 的 `mark_model_seen(str)` 錯誤（改 `AgentIdentity`）、`heartbeat.py:330` 的錯誤 `await`；修復 5 個 stale integration tests；後端 pytest 從 28 失敗降至 0。
30. **[已達成] 前端 UI 過時測試修復 (0 failed)**：修復 11 個 stale unit tests + 8 個 Radix UI 相容性 UI tests；前端 vitest 從 11 失敗降至 0（133 passed）；修復 HeartbeatScheduler 元件 `modelOverride` 未宣告 bug。
31. **[已達成] Gemini Context Cache Service**：實作 `GeminiCacheService`（`ToukaProxy/src/services/gemini_cache_service.py`），修正舊版 operator precedence bug（`gemini-2.x`/`gemini-3.x` 模型前綴誤排除）。正向白名單 + 排除關鍵字（thinking 模型、1.0-pro）設計。含 10 個單元測試（`tests/unit/test_gemini_cache_service.py`）。
32. **[已達成] BCRYPT_IMPORT_SSOT — 密碼安全靜態執法**：在 `lint_ssot.py` 新增 `BcryptImportRule`，白名單僅限 `auth_service.py`，封鎖 `bcrypt`/`passlib` 直接匯入（ADR-0028）。8 個 TDD 測試（`tests/unit/test_bcrypt_import_rule.py`）。同步修復 `lint_ssot.py` 7 個預存在 ruff 錯誤（E501 中文訊息拆行、SIM102 合併、D205 空行）。
33. **[已達成] NOQA Registry Sync Guard（第 16 道 guard）**：實作 `scripts/guard_noqa_registry.py`，使用 Python `tokenize` 正確排除字串字面量與說明型 comment，自動比對 `NOQA_REGISTRY.md` 統計表與代碼庫實際計數（ADR-0029）。初次執行修正 3 處真實差異：IDENTITY_SSOT 5→17（補錄 12 條 router 層條目）、LOCAL_IMPORT_BAN 10→8（移除測試字串誤計）、TID251 6→5。15 個 TDD 測試（`tests/unit/test_guard_noqa_registry.py`）。
34. **[已達成] SQLITE3_IMPORT_BAN — 資料庫存取靜態執法**：在 `lint_ssot.py` 新增 `Sqlite3ImportRule`，白名單：`async_db.py`、`factual.py`、`legacy_store.py`（ADR-0030）。`observability.py` L3 加 noqa 豁免。8 個 TDD 測試（`tests/unit/test_sqlite3_import_rule.py`）。
35. **[已達成] observability.py 架構重構**：`/memory/list` 端點移除直接 `sqlite3` 讀取，改透過 `get_manager().get_factual_store().query_all_facts()` 整合層存取。`FactualStore` 新增 `query_all_facts()` 跨代理診斷方法（`ToukaMemory/src/memory_server/factual.py`）。支援 ToukaProxy 獨立運作優雅降級（ImportError → 空列表）。SQLITE3_IMPORT_BAN noqa 計數 1→0（零違規）。
36. **[已達成] EnvSovereigntyRule 擴充 — 系統 API 禁令**：`lint_ssot.py` 的 `EnvSovereigntyRule.check_Call()` 補齊 AGENTS.md 第 18 節聲稱但未實作的禁令：`os.system()`、`os.chdir()`、`sys.path.append()`（限 `/src/` 業務層，`is_whitelisted` 自動豁免）。5 個 TDD 測試追加至 `tests/unit/test_lint_ssot.py`。
37. **[已達成] CONSOLE_BAN — 前端 Console 禁令（第 15 道 guard）**：實作 `guard_console.ts` + `console_guard_logic.ts`（ADR-0031）；白名單：`error_handler.ts`；9 個 TDD 測試（`tests/unit/console_guard_logic.test.ts`）。初次掃描 10 處現有違規加 noqa；NOQA_REGISTRY.md 新增 CONSOLE_BAN（10 條）。
38. **[已達成] 死碼偵測整合（第 20 道 guard）**：整合 `vulture>=2.11`（Python）與 `knip ^5`（TypeScript）；`guard_dead_code.py` + `guard:prune` script。初次執行清除：5 個未使用 TS 檔案（SettingsModal、SettingsSidebar、SnsBotsTab、useSessions、useStudio）、3 個 connections.ts 死亡 export、1 個 StudioShared.tsx ToolToggle、config.ts 重複 default export、heartbeat.py 未使用參數 `is_first_pulse`、config.py 未使用 `Optional` import。
39. **[已達成] SNSGatewayService 單元測試**：新建 `tests/unit/test_sns_gateway.py`，5 個測試案例（預設 Agent 路由、@alias 覆蓋、independent/home Session ID、SSE chunk 聚合）。修復 `sns_gateway.py` 的 model 格式：移除 legacy `toukasoul-{id}` 前綴，改用純 `{agent_id}` 格式與 ToukaChat 一致（MonitorLayer 向後相容）。
40. **[已達成] Module Boundary Guard — 模組邊界守衛（第 21 道 guard）**：使用 `import-linter>=2.1` 靜態執法 ToukaMemory → ToukaProxy 的逆向依賴禁令（ADR-0032）。發現並修復 `ToukaMemory/src/memory_server/tools/diag.py` 的禁令 6 違規：`src.services.resource_manager` 直接 import 改為 `get_resource_proxy()`、`src.utils.time_utils.get_current_epoch` 改為 `memory_server.config_utils.get_current_epoch`。`.importlinter` 設定白名單涵蓋合法 try-import 整合點。
41. **[已達成] InfoSec C1–M6 — 21 項安全弱點修復**（ADR-0033，2026-05-19）：完成系統性安全審計與修復，分 5 個批次（詳見 `SECURITY.md`）。
42. **[已達成] InfoSec 二次審計 N1~N12 — 12 項補修**（2026-05-20）：完成系統性二次審計（詳見 `SECURITY.md`）。
43. **[已達成] Guard #22/#23/#24 — 三道新安全守衛**（2026-05-20）：pip-audit + npm audit + Endpoint Auth Dependency Guard（ADR-0034）。
44. **[已達成] Guard #24 Grandfather List 技術債清零**（2026-05-20）：分三批次清空 52 個無認證守衛的歷史端點，清單維持 `[]`。
45. **[已達成] SOLID-SRP Heartbeat Token — verify_heartbeat_token Depends**（2026-05-20）。
46. **[已達成] Legacy Stats URL agent_id 修復**（2026-05-20）。
47. **[已達成] 滑動視窗對話紀錄 (Sliding Window History)**（2026-05-21，ADR-0035）。
48. **[已達成] 測試清理 — 合併重複測試**（2026-05-21）：590 → 588 tests，all GREEN。
49. **[已達成] 來回計算大一統 (Round Count SSOT)**（2026-05-21，ADR-0036）。
50. **[已達成] InfoSec 三次審計 P1~P29 — 29 項安全弱點修復**（2026-05-21，詳見 `SECURITY.md`）。
51. **[已達成] 開源準備 — 測試 fixture 身分匿名化、production code 清理**（2026-05-22）。
52. **[已達成] Storybook 完整移除 + Docker build 修復**（2026-05-22）。
53. **[已達成] Guard #25 — Native NPM Build Environment Guard**（2026-05-22）。

---

## 🔧 實作詳解 (Implementation Deep-Dives) ⭐

> 以下三段子系統實作細節於 2026-05-31 文件整理時從 `HANDOVER.md` 移入，內容未改，供日後追溯。

### 📌 環境同步註記（歷史，僅適用特定本機環境）

早期實體執行環境位於 `<ToukaSanctuary>\ToukaMemory`，曾需手動同步整合點：將 `<repo>\ToukaMemory\src\memory_server\tools\__init__.py` 複製覆蓋至該路徑，並 `docker-compose up -d --build`；因密碼雜湊演算法變更，舊 `users.json` 需刪除以自動重生 admin。此為個人環境一次性操作，非通用部署需求，僅留存備查。

### 資源管理器 MCP 回退機制實作詳解 (Resource Manager MCP Fallback Implementation)

> **已退役的歷史機制**：ADR-0116 已移除獨立 MCP 模式與 `_FallbackResourceProxy`；現行為 Integration-Only，取不到真 `ResourceManager` 時 fail-loud。下文只保存早期設計，不得據此恢復回退。

#### 背景與問題

ToukaMemory 支援兩種運作模式：
1. **整合模式**：作為 ToukaProxy 的原生 Python 模組掛載，可存取 `src.services.resource_manager`。
2. **MCP 獨立模式**：作為標準 FastMCP 伺服器獨立運行，無法匯入 `src.*` 模組。

過去 ToukaMemory 在 MCP 模式下會因本地導入缺失而崩潰。本機制實施「工廠函數 + 協議回退」模式，確保兩種模式透明切換。

#### 核心設計

**檔案：`ToukaMemory/src/memory_server/config_utils.py`** — `_FallbackResourceProxy` 類（實作 `IResourceProxy` 協議，使用純 Python 內建 API 進行 I/O；`get_vault_path()` 在 MCP 模式回傳 `None`，呼叫方皆有備援邏輯）+ 工廠函數 `get_resource_proxy()`（整合模式回真實 `ResourceManager`，MCP 模式回 `_FallbackResourceProxy`，模組級快取 + `_reset_resource_proxy_cache()` 供測試）。所有 ToukaMemory 模組統一改用頂層 `from memory_server.config_utils import get_resource_proxy`。

修改檔案：`config_utils.py`、`factual.py`、`vector.py`、`services/legacy_sync.py`、`services/mission_manager.py`、`services/manager.py`、`tools/personal_drive.py`。9 個 TDD 測試（`tests/unit/test_memory_resource_proxy.py`）。邊界決策：`get_vault_path()` MCP 回 `None`（呼叫方已備援）、快取跨測試污染由 fixture 重置、`move_file()` 額外實裝。

### AI Agent 工作分派看板系統實作詳解 (AI Agent Dispatch Center Implementation)

#### 背景與問題
為了與外部 AI 工具 (如 Claude Code、Aider 等) 通過 MCP 溝通與指派任務，系統引入了 `DispatchAgent` 作為協作看板中介。已存 Agent 寫入 `DispatchAgent` 看板會因物理路徑監獄受阻；外部 AI 用 Standalone MCP 時面臨憑證缺失與跨身分授權問題。

#### 核心設計
1. **跨身分代理校驗**：`MissionManager._get_mission_path()` 中，呼叫方具 `"can_supervise_dispatch"` 能力時暫時重塑身分上下文為 `DispatchAgent`，安全繞過路徑監獄。
2. **Task 模型 Schema 擴充與產出物解耦**：`Task` 擴充 `verification_cmd`/`boundary_rules`/`test_log`/`blocker_detail`，產出物解耦為 `artifact_ref: str | None` 與 `artifact_refs: list[str]`，均經路徑安全與穿越校驗。
3. **FSM 狀態流轉矩陣**：強制 `TODO → DOING → DONE/FAILED`，`DONE` 為終端。
4. **FAILED 特權重置**：唯特權身分（`can_supervise_dispatch` 或 `Owner`/`Debugger`/`admin`）可重設為 `TODO`。
5. **專用派發 MCP 伺服器 (ToukaDispatch, Port 8083)**（C1 安全修復）：獨立 FastMCP 取代原自動特權注入；`DISPATCH_TOKEN` Handshake 換 session token（ADR-0047 + ADR-0050）；身份固定 `DispatchAgent`；`FORCE_AGENT_ID` 與 `access_mission_board_tool` 永久移除。
6. **Mission Token 安全隔離（ADR-0064）**：`mission_token` 委派，`DISPATCH_TOKEN` 不離開 Touka 進程；雙路徑 handshake；外部 session 禁呼叫 `dispatch_initialize_mission`；board 完整性檢查。
7. **任務分派協調員 Skill（ADR-0065 → 0066/0067）**：`touka-dispatch-coordinator` 內建分派流程，自動召喚外部 AI；外部 AI 指南 `docs/external/touka_dispatch_guide.md`。
8. **Skill 系統 Anthropic 標準化（ADR-0066）**：遷移至目錄式 `SKILL.md` + `.mcp.json`、Progressive Disclosure、Pull 模式 auto-trigger、`.skill` 打包/安裝（SSRF + zip-bomb 防護）、metadata 子欄位。
9. **Dispatch 自動召喚 CaC（ADR-0067）**：`auto_invoke` 依 `dispatch_hooks.json` 白名單 subprocess 召喚外部 AI（`shell=False`、env-only mission_token、whitelist 子環境）。

（完整修改檔案清單與驗證流程見對應 ADR-0044~0067。當時驗證：`guard_all.py` 全綠、TDD 全綠、冒煙全綠。）

### 環境感知 .env 互斥載入與 Gemini 篩選重構實作詳解

#### 背景與問題
1. **環境變數污染**：各子系統獨立/整合運作時 `.env` 載入路徑模糊，可能重複載入或覆蓋。
2. **Gemini 停用模型殘留**：需動態過濾僅放行 `>= 2.0`，不用硬編碼清單。

#### 核心設計與實作
- **環境感知互斥載入**：`ToukaProxy/src/config.py` 與 `ToukaHeartbeat/config.py` 的 `resolve_env_file_path` / `resolve_heartbeat_env_file_path` 動態偵測上下文——整合執行錨定根目錄 `.env`，獨立執行只讀子專案 `.env`（ADR-0052，對應禁令 38）。
- **Gemini 動態篩選**：`fetcher.py` 用正則 `^gemini-([2-9]|\d{2,})\.` 比對主版本號，僅放行 `>= 2.0`（ADR-0052，對應禁令 39）。

---

## 📝 歷史演進日誌 (Evolution Logs: Categorized History)

### 🛡️ 安全、認證與主權 (Security, Auth & Sovereignty)
- **2026-05-17**: **RAISE_STRING_BAN — raise 硬編碼字串禁令 ⭐**
    - **新規則**：在 `lint_ssot.py` 新增 `RaiseHardcodedStringRule`，同時新增 `check_Raise()` 鉤子到 `BaseRule` 與 `visit_Raise()` 到 `SSOTGuard`。
    - **禁止目標**：`raise ValueError("...")` / `raise RuntimeError("...")` / `raise TypeError("...")` 帶字串字面量或 f-string。
    - **修復**：`gemini_handler.py`、`dispatchers/utils.py` 改為 `raise ToukaException(ErrorCode.MODEL_NOT_FOUND)`。
    - **ADR**：0027
- **2026-05-16**: **localStorage SSOT Guard — 前端存取主權 ⭐**
    - **新 Guard**：實作 `guard_localStorage.ts`（LOCALSTORAGE_SSOT），掃描 `src/hooks/**`、`src/lib/**`、`src/app/**`、`src/contexts/**`。
    - **重構**：`useConnectionSettings`、`useSnsBotSettings`、`connections/page.tsx` 改用 `useLocalStorage<T>` hook。
    - **ADR**：0026
- **2026-05-16**: **日誌主權靜態執法 + Worktree 修復 ⭐**
    - **PRINT_BAN / BASICCONFIG_BAN**：新增兩條 `lint_ssot.py` 規則，`logging_config.py` 確立為唯一日誌 SSOT 入口。（ADR-0025）
    - **Worktree 修復**：`lint_ssot.py` 的 `IGNORED_DIRS` 加入 `.claude`，消除 worktree 副本雙重掃描；`guard_all.py` 全 15 守衛全部 PASS。
- **2026-05-16**: **fetch() SSOT Guard — 前端 HTTP 客戶端主權 ⭐**
    - 實作 `guard_fetch.ts`，強制所有 HTTP 請求走 `fetchTouka()`；修復 21 處違規（`listSessions`、`saveSession`、`authService` 等）。（ADR-0024）
- **2026-05-16**: **後端整合測試全面修復 (0 failed) ⭐**
    - 修復 `proxy.py:26,64` 的 `mark_model_seen(str)` → `mark_model_seen(AgentIdentity(...))`；修復 `heartbeat.py:330` 的錯誤 `await`。
    - 修復 5 個 stale integration tests（`is_owner(str)` → `is_owner(AgentIdentity(...))` 等）。後端 pytest 從 28 失敗降至 **0 失敗**（225 passed）。
- **2026-05-16**: **前端 UI 測試修復 (0 failed) ⭐**
    - 修復 11 個 stale unit/integration tests；修復 8 個 Radix UI 相容性 UI tests（mock Tabs/Select/Slider）。
    - 修復 `HeartbeatScheduler.tsx` 元件 bug（`modeOverride` 未宣告）。前端 vitest 從 11 失敗降至 **0 失敗**（133 passed）。
- **2026-05-14**: **身分物件化執法與前端環境變數禁令 (Identity Anchoring & Frontend Env Guard) ⭐**
    - **身分物件化 (Identity Anchoring)**：全面重構 `ToukaProxy` 路由層，強制將 `str` ID 轉換為 `AgentIdentity` 物件，達成 100% 物理路徑隔離安全性。
    - **前端環境變數禁令 (Frontend Env Guard)**：實作基於 `ts-morph` 的 `guard_env.ts` 掃描器，攔截前端代碼中所有對 `process.env` 的直接調用，強制統一經由 `src/lib/config.ts` 存取。
    - **戰術性豁免 (Tactical Suppression)**：針對 `ToukaHeartbeat` 與 `ToukaMemory` 的架構特性實施 `# noqa` 豁免並記錄於 `NOQA_REGISTRY.md`。
- **2026-05-11**: **動態 SSOT 豁免機制與代碼風格大一統 (Dynamic SSOT Suppression)** ⭐
    - **Linting 重構**: 升級 `lint_ssot.py` 支援基於正則表達式的 `# noqa: [RULE_ID]` 行級解析，允許在核心基礎建設中進行「戰術性豁免」。
    - **AST Hack 廢除**: 修復了 `ToukaMemory/manager.py` 中為了規避 IOPrisonRule 而寫的 `getattr(path, "mkdir")` 旁門左道，還原為符合 Python 慣例的 `path.mkdir()` 搭配 `# noqa` 標籤。
    - **PEP-8 與型別標註**: 完成 `lint_ssot.py` 的全文件 Type Hints (`-> None`, `-> list`) 與代碼風格 (E501, SIM102) 修正，成為完美無警告的法官程式。
- **2026-05-10**: **ABAC 安全等級體系與執行鏈路加固 (Auth Level Matrix & Security Enforcement)** ⭐
    - **四階矩陣實作**: 在 `safety.py` 中重構決策矩陣。`Manual` 現在會強制要求所有指令（包含 `ls`）的手動確認，達成 100% 交互主權。
    - **執行鏈路參數透傳**: 修改 `ToolExecutor` 與 `terminal.py`，確保 `auth_level` 屬性能從請求上下文一路滲透至底層 Bash 工具，解決了 FullYOLO 失效的 Bug。
    - **安全等級門禁**: 在 `MonitorLayer` 增加核驗邏輯，攔截非 Admin/Owner 對自動化等級 (`SafeAuto`/`FullYOLO`) 的非法設定，並自動降級。
    - **TDD 驗證**: 新增 `tests/unit/test_auth_level_matrix.py` 與 `tests/integration/test_terminal_auth_propagation.py` 並通過 100% 綠燈。
- **2026-05-10**: **多使用者管理系統與個人檔案實作 (Multi-User Management & Profile)** ⭐
    - **Admin 管理面板**: 在 `ToukaChat` 設定中新增「使用者管理」分頁，支援 Admin 進行使用者 CRUD 與 `owned_agents` (代理人配給) 操作。
    - **管理員代理權限優化**: 修正了 `IdentityRouter` 邏輯，管理員在 Assume 身分後依然具備 `admin` 角色，解決了管理 API 無法呼叫的問題。
    - **個人檔案自主維護**: 在「一般設定」中實作個人檔案編輯區塊，支援修改顯示名稱、頭像 URL 與個人簡介。
    - **全鏈路權限隔離**: 透過 `AuthContext` 攜帶完整 User Profile，並在 `IdentityRouter` 攔截點強制校驗使用者對代理人的擁有權。
    - **API 合約與設計執法**: 通過 `guard_all.py` 全項檢查，修復了 5 處 API Parity 違規與 6 處 Design Token (Arbitrary Value) 違規。
- **2026-05-10**: **文檔大稽核與中性身分化 (Documentation Audit & Anonymization)** ⭐
    - **中性代稱實施**: 完成全域文檔隱私化，將 `Soul-A`, `Soul-B`, `Soul-C` 作為公開文檔的標準代稱，隔離代理人私人特徵。
    - **文檔主權 (Doc SSOT)**: 實施「文檔即架構」原則。清理了 `MEMORY_SSOT_MANDATE.md` 等冗餘文件，整合至 `SSOT_MASTER_MAP.md`。
    - **前端正名**: 統一將 `Touka AI Studio` 稱呼拆分為 `ToukaChat` (管理) 與 `ToukaIDE` (開發)。
    - **新增指南**: 建立了 `security_auth_guide.md` (安全架構) 與 `soul_management_guide.md` (靈魂維護)。
- **2026-05-10**: **靈魂模型復原與 ID 統一化 (Soul Restoration & ID Unification)** ⭐
    - **ID 簡潔化**: 全面捨棄 `ToukaSoul` 前綴，統一使用簡潔 ID（如 `Alice`, `Bob`, `Charlie`），符合 SSOT 精神。
    - **配置動態合併**: 修復了 `AgentService` 掃描目錄與 `models.json` 配置不匹配的 Bug，靈魂模型現在能正確繼承 UI 設定。
    - **Pruning 硬保護**: 實作了針對 L1 靈魂層級的物理鎖定，防止自動清理機制因 ID 變更而誤刪設定。
- **2026-05-10**: **群聊功能恢復與整合測試補全 (ToukaGroup Recovery)** ⭐
    - **模型恢復**: 修復了 `ToukaGroup` 在 `models.json` 中缺失導致 UI 不可見的問題。將其標記為 `owned_by: proxy` 與 `enabled: true` 以防止被清理機制剔除。
    - **測試補全**: 新增 `tests/integration/test_group_chat.py`，驗證了群聊模型的 API 可見性、別名解析與分發流。
- **2026-05-10**: **安全會話管理、Heartbeat 組件解耦與部署等價性守衛 ⭐**
    - **Session Sovereignty**: 解決了登出無效問題。統一 `refresh_token` Cookie 於 `path="/"`，並強制前端 `fetchTouka` 攜帶 `credentials: 'include'` 以確保跨來源 Cookie 處理。
    - **Heartbeat SOLID 重構**: 將 `awaken.py` 拆分為 `StorageService` (IO) 與 `ProxyClient` (Network)，實現高度可測試性。
    - **部署等價性守衛 (Parity Guard)**: 在 `guard_dependencies.py` 中新增靜態分析邏輯，驗證 `Dockerfile` 之 `COPY` 指令是否完整涵蓋了子專案中的所有本地 Python 模組。
- **2026-05-09**: **多使用者身分認證與 Bcrypt 72-Byte 限制修復 ⭐**
    - **Human Auth SSOT**: 建立 `users.json` 作為人類使用者與 Agent 擁有權的權威來源。
    - **Bcrypt Pre-hashing**: 實作 `SHA256 -> Bcrypt` 流程，解決 bcrypt 長度限制並移除 `passlib` 依賴。
    - **Frontend Auth UI**: 實作 `/login`, `/register` 頁面與全域 `AuthContext` 攔截器。
    - **Heartbeat 聯動**: 喚醒 Agent 時強制校驗擁有者之 Refresh Token 活性 (30d)。
- **2026-05-06**: **依賴主權與沙盒化驗證機制 (Dependency Sovereignty) ⭐**
    - **依賴守衛 (Dependency Guard)**: 實作 `scripts/guard_dependencies.py`。透過建立隔離的 `venv` 沙盒模擬生產環境，僅安裝 `requirements.txt` 宣告的套件並執行匯入測試。
    - **雜湊緩存機制**: 實施 `requirements.txt` 雜湊比對，確保僅在依賴變動時重新安裝，優化開發流程。
    - **匯入完整性執法**: 新增 `tests/unit/test_imports_integrity.py`，驗證全系統進入點與型別定義。修復了 `ToukaHeartbeat/awaken.py` 的相對匯入錯誤與 `ToukaProxy` 的模組缺失。
- **2026-05-06**: **環境變數與錯誤碼主權大一統 (Env & Error SSOT Guard) ⭐**
    - **Env SSOT Guard**: 全面遷移至 Pydantic `BaseSettings`。實施 `lint_ssot.py` 強制攔截 `os.environ` 違規，達成 100% 配置中心化。重構了 `ToukaProxy/src/config.py` 並為 `ToukaHeartbeat` 建立獨立配置，修復了 42 處硬編碼配置問題。
    - **Error Code Sovereignty**: 實作 `errors.json` 作為 SSOT 定義檔。新增 `ToukaException` 與 `ErrorCode` 體系，取代零散的 `HTTPException`。
    - **自動化同步與執法**: 新增 `scripts/sync_errors.py` 自動生成前端 `error_codes.ts`。部署 `guard_errors.ts` (前端) 偵測並攔截 86 處硬編碼錯誤字串。
- **2026-05-05**: **API 合約校驗、時間主權硬執法與身分能力大一統 ⭐**
    - **API Parity Guard**: 實作 `guard_frontend.ts` 強制校驗前端 API 呼叫是否符合 `openapi.json` 定義（包含 `/v1` 前綴標準）。解決了 10 處路徑不一致問題。
    - **時間主權 (Full-stack)**: 實施前端 `guard_time.ts` 靜態檢查，確保全系統（含 UI）統一使用 `getSystemEpoch()`。
    - **能力管理重構**: 廢除 `/v1/heartbeat/whitelist`，將心跳權限歸併至 `identity_map.json` 的 `heartbeat_awakening` 能力標籤中。更新 `HeartbeatWhitelist` 元件達成 SSOT。
- **2026-05-05**: **自主預算治理、API 合約同步與全域守則執法 ⭐**
    - **自主預算硬限額**: 實施 `RhythmService` 優先級判定與 `ReAct` 循環強制攔截。新增 `smoke_autonomy_enforcement.py` 驗證 Slot 消耗邏輯。
    - **合約一致性監控**: 新增 `test_api_schema_sync.py` 單元測試，確保模型變更時必須同步更新 `openapi.json`。提供 `scripts/update_openapi.py` 作為開發者工具。
    - **全域守則靜態稽核**: 擴充 `lint_ssot.py`，偵測並攔截業務邏輯中直接調用 `datetime.now()`、`time.time()` 或 `os.getenv()` 的行為，強制落實時間與環境主權。
- **2026-05-04**: **全系統 I/O 代理化與串流等價性達成 ⭐**
    - **I/O 監獄最終執法**: 透過擴充 `ResourceManager` 介面（新增 `write_file_binary`, `append_file_text` 等），達成 100% 的檔案操作代理化。成功移除 `proxy_service.py` 與 `stats.py` 中最後的違規 `open()` 調用。
    - **整合測試集穩定化**: 修復了 `test_handler_integration.py` 的 Pydantic 參數衝突、`test_react_integration.py` 的串流旗標缺失，以及 `test_token_resilience.py` 的 LDH 邏輯斷言。
    - **架構 SSOT 達標**: `lint_ssot.py` 全域掃描通過，無任何物理路徑違規。
- **2026-04-27**: **模型發現鏈實作與 UX 交互主權加固 ⭐**
    - **動態模型管理**: 補全 `ModelManager` 抓取邏輯，支援 Google (Gemini), OpenAI, xAI (Grok) 與 `connections.json` (OpenAI 相容) 的自動掃描。
    - **主權合併演算法**: 實作 `_merge_models` 確保動態更新時保護使用者既有的 `enabled` / `visibility` 設定，僅更新 `last_seen`。
    - **UX 交互規範**: 解決了邊欄對話刪除選單自動關閉的 UI Bug。規範了雙重確認按鈕必須使用 `onSelect` 配合 `preventDefault()` 的交互準則。
- **2026-04-26**: **Docker 環境韌性三合一加固與 LDH 實作 ⭐**
    - **優雅降級 (Local Dynamic Handshake)**: 針對信任網域 (Loopback/Private) 實作 Token 自動重新簽發。解決了 Docker 容器重啟後因 Secret 變更導致 UI 需頻繁重新整理的問題。
    - **模型初始化修復**: 補齊了 `ModelManager` 的非同步 stub 方法（`fetch_latest_*`），消除了啟動時因併發呼叫空方法導致的掛起風險。
    - **對話序列化修復**: 修正了 `ui_sessions.db` 持久化 `TreeHistory` 對象時的 `TypeError`。強制在寫入 SQLite 前執行 `model_dump()`。
- **2026-04-24**: **身分主權滲透修復、全域 Lint 達標與測試集重構 ⭐**
    - **身分物件化修復**: 修正了 `CustomModelLayer` 與 `AgentService` 中嘗試將 `AgentIdentity` 當作字串處理的 `AttributeError`。
    - **路由層 SSOT 加固**: 重構了 `custom_models.py` 路由器，確保所有對 `CustomModelService` 的調用皆通過正式身分物件。
    - **測試沙盒一致性**: 修復了 `conftest.py` 在 `issue_token` 時的參數型別錯誤，確保測試環境的認證鏈與生產環境 100% 同步。
    - **Lint 法律執行**: 執行 `ruff check --fix` 並通過 `lint_ssot.py` 檢查，所有改動檔案均符合最高靜態分析標準。
- **2026-04-24**: **記憶系統合約大一統、參數對齊與測試集全面修復 ⭐**
    - **Schema SSOT 落地**: 在 `ToukaMemory` 建立專屬 `schemas.py` 並將其作為全系統唯一事實來源。移除了 Proxy 端的影子定義。
    - **參數命名糾偏**: 修正了 `write_content` -> `content` 等命名不一致，修復了 ReAct 循環中的參數丟失問題。
    - **測試集全綠**: 修復了 `test_vault_sovereignty.py` 與 `test_memory_knowledge.py`，驗證了身分錨定與物理隔離的可靠性。
    - **資源管理擴展**: 為 `ResourceManager` 補齊 `get_project_path` 介面，滿足專案審計需求。
- **2026-04-24**: **啟動穩定性、整合模式與路徑 SSOT 最終執法 ⭐**
    - **整合模式導出**: 在 `ToukaMemory` 實作 `memory_tool_registry` 整合入口，解決了啟動時找不到屬性的 `AttributeError`。
    - **路徑主權強行執法**: 徹底封鎖 `soul_service.base_dir`，修正 `main.py` 啟動日誌的違規訪問，達成全系統 Path SSOT。
    - **啟動穩定化**: 為 `load_memory_system` 加入防衛性 `getattr` 檢查，確保系統能優雅降級。
- **2026-04-23**: **時間主權加固、資源監獄隔離與引導自癒機制 (The Final Sovereignty Shield) ⭐**
    - **時間主權絕對化**: 部署 Ruff `TID251` 法律，封鎖全系統所有原生時間獲取 API (如 `datetime.now`)，強制統一使用 `time_utils` 並確立 UNIX Epoch 作為唯一時間基準。
    - **引導自癒與環境白名單**: 實作 `bootstrap.py` 自動復原機制。建立環境白名單，嚴禁在業務邏輯中調用 `os.environ` 或 `subprocess`，將系統權力鎖定在受控的引導者檔案中。
    - **資源監獄 (ResourceManager)**: 強化路徑主權隔離，強制所有檔案操作必須透過 `ResourceManager` 並攜帶 `AgentIdentity` 物件，消滅了寫死路徑的可能性。
    - **身份物件化認證**: 徹底完成從「ID 字符串」到「驗證後身份物件」的架構遷移，大幅提升了權限判定與審計的安全性。
- **2026-04-21**: **法規即代碼 (Legal Architecture) 與 主題主權重構 ⭐**
    - **法規自動化監督**: 在 `ToukaChat` 實作 ESLint「主權法官」機制，透過 `tailwindcss/no-arbitrary-value` 強制執行 Design tokens，並禁止直接引用 Radix UI。
    - **合約主權深層同步**: 將 `McpHubServer` 等前端手寫型別全數遷移至後端 Pydantic 定義，實作跨語言 100% 同步機制。
    - **主題設定 WYSIWYG**: 重構設定視窗為雙欄佈局，整合 `ThemeShowcase` 即時預覽，實現「所見即所得」的主題配置。
- **2026-04-19**: **測試架構大一統 (Unified Testing) 與 DebugAgent 身份鎖定 ⭐**
    - **測試基建歸併**: 成功將所有分散於子專案（Proxy, Memory）的測試遷移至根目錄 `tests/`，實現「一處調研，全域綠色」。
    - **虛擬 Agent 職責整合**: 移除 `HeartbeatTester`, `heartbeat_probe` 等冗餘身份，將所有診斷與心跳測試職責整合至 `DebugAgent`。
- **2026-04-18**: **大一統路由治理 (Grand Unification) 與 結構化除錯表單實作 ⭐**
    - **路由對等化 (Prefix Parity)**: 重構 `main.py` 與 `CORE_ROUTERS` 註冊機制，確保全系统核心 API 同時支援 `/v1` 與根路徑前綴。
    - **無斜線韌性 (Trailing Slash Resilience)**: 標準化所有路由器為無斜線定義，並透過 `redirect_slashes` 提供兼容性。
- **2026-04-17**: **檔案操作引擎下沉 (File Ops Sinking) 與 任務看板實作 (Mission Board) ⭐**
    - **邏輯下沉 (Logic Sinking)**: 為了徹底貫徹 SSOT，將 `vault_tools` 的強大實作（`str_replace`, `grep` 等）從 Proxy 移入 `ToukaMemory`。Proxy 轉型為「薄橋接器 (Bridge)」。
    - **自主任務看板 (TMB)**: 實作 `MissionManager` 與 `mission_tools`。
- **2026-04-15**: **自主行為預算大一統與硬編碼邏輯收斂 (Autonomy Budget Unification) ⭐**
    - **建立預算決定優先級 (Slots Priority Chain)**: 實施了「請求指定 (Explicit) > 全域配置 (autonomy.json) > 系統保底 (Default: 5)」的判定鏈。
- **2026-04-12**: **身分憑證化、主權隔離與全系統穩定化 (Identity & Vault Isolation)**
    - **身分憑證化 (Gate 1)**: 全系統導入 JWT 認證。
    - **物件化身分主權 (AgentIdentity)**: 實作不可變身分模型。
    - **路徑主權強制隔離**: 重構 `ResourceManager`，確保物理鎖定。

---
*-- Touka System Autonomy Records (Last Updated: 2026-06-25)*
