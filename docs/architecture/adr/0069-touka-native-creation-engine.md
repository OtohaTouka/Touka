# ADR-0069: Touka 原生創建引擎（Creation Engine）— MCP 與 Skill 共用的創建流水線

> C2-3 現況補註：創建的 SMOKE／EVAL 已接 runner、共用一次 grant；來源由呼叫程式決定，HTTP 在 none 不試跑，YOLO 依創建工具的全域／本機層級執行。 詳見 ADR-0275。

## 狀態

✅ 已接受（Phase 1 脊椎 + Phase A SMOKE + Phase B EVAL + Phase C SCAFFOLD + Phase D 路由/工具/守衛/ModelJudge 已實作——ADR 完整落地）

## 日期

2026-05-31

## 背景

Touka 已有成熟的 MCP「安裝 + 派發 + 生命週期」機制（`MCPHubService.install` → `server_config.json` → `registry.load_config()` + `protocol_manager.initialize_all()` → `sync_system_dispatcher`），以及 Skill 的創建/更新流程（`SkillService.update_skill`）。系統下一步要新增「在 Touka 內製作 skill」的能力，並以「製作 MCP」流程為參考範本。

## 問題

1. **創建路徑分裂**：Skill 與 MCP 各有一套寫入/上線邏輯、無共用抽象；新增「skill 創建功能」等於再造一套輪子。
2. **MCP 無真正的「創建」**：現況只能從 hub 安裝既有 server；從零創建仍須手刻 FastMCP + 手動註冊，無生成、無註冊前試跑、無評測。
3. **缺兩道品質關卡**：產物上線前無「沙箱試跑（smoke）」確認真能啟動；上線後無「可驗證評測（eval）」確認 LLM 真會用。

## 決策

建立 **Touka 原生創建引擎（CreationEngine）**：一條固定創建流水線，MCP 與 Skill 作為兩種「產物類型（CreationKind）」插入同一引擎，共用驗證/寫入/上線邏輯，並補上 SMOKE 與 EVAL 兩道關卡。

### 完整流水線（願景，7 階段）

SPEC（意圖→規格）→ VALIDATE（靜態驗證）→ 🆕SMOKE（沙箱試跑）→ REGISTER（原子寫入）→ ACTIVATE（熱重載+同步）→ 🆕EVAL（可驗證評測）→（PACKAGE 選填打包）。任一關卡失敗即停止、回滾、回「可行動錯誤訊息」。

### Kind Adapter 抽象（SOLID-OCP / Strategy）

引擎只負責編排；每種產物提供一個 `CreationKind` 策略物件實作該類型行為。新增第三種產物（未來如 workflow / agent）只需新增一個 adapter，引擎一行不動。`SkillCreationKind` 委派 `SkillService`、`McpCreationKind` 委派 `MCPHubService`，皆以結構化型別（structural typing）符合協定、不需繼承。

### 重用既有 SSOT（不另立事實來源）

- Skill 寫入委派 `SkillService`（`config/skills/` SSOT）。
- MCP 寫入委派 `MCPHubService`（`server_config.json` SSOT；本 ADR 新增 `register_server` / `get_server_names` 兩個加法方法）。
- 沙箱試跑重用 `EvolutionPathGuard.run_sandbox_command` / `get_clean_env`（ADR-0033 演化沙箱）。
- 命令安全統一至 `src/utils/mcp_security.py`（`is_command_safe`），與 router 的 `MCPServerItem` 共用同一份 shell 注入防護（消除原本內嵌於 router 的重複正則）。

### 兩道新關卡

- **SMOKE（✅ Phase A 已實作）**：註冊前試跑候選產物——MCP 以**淨環境**（`EvolutionPathGuard.get_clean_env()`，候選 server 看不到 Touka 密鑰）啟動 + MCP 握手（initialize + tools/list ≥ 1 工具）+ `asyncio.wait_for` 逾時 + `async with` 保證拆除；skill 做 SKILL.md round-trip 乾跑（**不執行碼**）。失敗即不 commit。最小握手啟動器見 `creation/smoke.py`。
- **EVAL（✅ Phase B 已實作）**：commit 之後的**後置、非阻斷**階段——仿 Anthropic mcp-builder，為新產物生成 N 題可驗證**唯讀**任務（MCP 每工具一題、上限 20，僅 `tools/list` 枚舉、**不執行**工具；skill 由 `required_tools`+description 決定性生成），序列化為 `config/evals/<kind>/<id>.eval.md` 回歸基線（經 `resource_manager` 寫檔、無時間戳故決定性）。LLM-as-judge 為注入式 `Judge` 介面（DIP），**預設關閉**（`eval_scoring=False`、僅 `NoopJudge` 哨兵、零付費呼叫），真正模型評分留待 Phase D。EVAL 失敗只記 warning、**不影響已 commit 的產物**。

### 安全與主權

- 創建只寫 `config/skills/<新id>/` 與 `server_config.json` 新條目；**CREATE 不得覆寫既有產物**（id 衝突於 VALIDATE 關卡擋下），以此在引擎層強制「不更動既有 agent 資料」——既有 skill 與 MCP 條目皆受保護，`data/soul`、`data/custom`、`data/personal_drive`、`data/legacy` 完全不碰。
- 例外：系統 dispatcher（DispatchAgent）由既有 `sync_*_dispatcher` 重生，屬允許範圍。
- ENV 主權：產物只用環境變數佔位、禁內嵌密鑰；命令過 shell 注入防護；創建端點 Owner/admin 限定（路由 Phase）。

### 實作進度

- **Phase 1（脊椎，commit `dee9ddb`）**：VALIDATE + COMMIT、skill 與 mcp 兩 kind、純加層零行為變更。
- **Phase A（SMOKE）**：`create()` 改 async，於 VALIDATE 後插入 SMOKE gate（MCP 真啟動握手 / skill 乾跑）；淨環境 + 逾時 + 保證拆除；`run_smoke=False` 可跳過（離線/純設定情境）。
- **Phase B（EVAL）**：commit 後插入**後置、非阻斷**的 EVAL（`run_eval=True`、`eval_scoring=False` 預設不計分）；生成每產物的唯讀評測任務並寫 `config/evals/<kind>/<id>.eval.md` 回歸基線。新增 `EvalRunner`（SRP：持久化＋評分接口）、`eval_formatter`（序列化/解析）、`smoke.list_mcp_tools`（唯讀枚舉）、`Judge`/`NoopJudge`（DIP 接口）；`CreationResult` 增 `evaluation` 欄位。
- **Phase C（SCAFFOLD）**：新增 `McpScaffolder`（`creation/scaffold.py`）——**stub-only 決定性生成**（零 LLM、零付費）：由高階 `McpScaffoldSpec`（name + description + tools[name/description/params]）生成可執行 FastMCP server stub（每 tool 一個 `@mcp.tool()` 佔位、回傳 placeholder），寫至 `config/mcp_servers/<normalized-name>/server.py`（經 `resource_manager`），並回傳可直接餵既有 `creation_engine.create("mcp", spec)` 的 `McpSpec(command=sys.executable, args=[server.py 絕對路徑])`；SMOKE 會真啟動該 stub 並驗證宣告的工具。**注入防護**：識別字（server/tool/param 名）須 `isidentifier()` 且非關鍵字、server 名正規化後須為單段 slug（擋路徑穿越）；自由文字（描述）僅以 `repr()` 轉義字面值嵌入；型別限白名單（str/int/float/bool，未知降級 str）；最終 `ast.parse()` 兜底，無法解析即拒寫。**不覆寫**：`server.py` 已存在或名稱已註冊即拒。`_server_name` 上移為共用 `creation/naming.py`（SSOT）。引擎與 kinds **完全不變**——scaffold 是 PRE-engine 生產者。**已知侷限**：`command=sys.executable` 為絕對路徑，config 跨機搬移需重生。
- **Phase D（路由 + 工具 + 守衛 + ModelJudge）**：對外開放整套創建能力。新增 `/v1/creation` 路由（`routers/creation.py`，`POST /skill|/mcp|/scaffold`，**Owner/admin 限定**）、agent 對話工具（`tools/builtin/creation.py`：`create_skill`/`create_mcp`/`scaffold_mcp`）、route 與工具共用的薄門面 `services/creation/api.py`（DRY/SSOT）、**Creation Sovereignty Guard #29**（`scripts/guard_creation_sovereignty.py`：路由須 `_require_creation_role` 把關、creation 套件外不得繞引擎直呼 `update_skill`/`register_server`、門面不依賴 fastapi）、以及 `ModelJudge`（`model_judge.py`，把 Phase B 的 `Judge` 接縫接上真實 LLM；**雙閘預設關**：`TOUKA_EVAL_JUDGE_ENABLED` ＋ `eval_scoring=True` 才花 token，fail-safe 0.0 不阻斷 commit）。引擎/kinds/scaffold/eval 行為穩定（唯一加法：`EvalRunner.set_judge`）；驗證/SMOKE 失敗回 200＋errors（不報 500），無 token→401、角色不符→403。

## 設計決策

### 為何「一引擎兩產物」而非各做各的

Touka 的價值在 install/派發/安全 生命週期；外部生成器不懂這套，產物進 Touka 仍須重接線、重上鎖。共用引擎讓「把 MCP 創建做通」自動等於「skill 創建做通」，兩者收斂為同一條可演進的流水線。

### 為何 commit 暫時綁定 register+activate

沿用現有「service 層寫入、router 層熱重載」職責分工：脊椎的 commit = 寫入 + dispatcher 同步（皆 import-safe）；`protocol_manager` 即時熱重載沿用 router 觸發，留待路由 Phase，避免 service→`src.main` 循環匯入與 `LOCAL_IMPORT_BAN` 例外。

### 為何 CREATE 不覆寫既有 id

以「創建不覆寫」在引擎層把「不更動既有 agent 資料」變成確定性、可單元測試的硬約束，比靠流程紀律可靠（符合「自動守衛優於人盯流程」）。

## 後果

### 正面

- 一套抽象服務兩種產物；新增產物類型成本最低（OCP）。
- 重用既有 SSOT 與安全閘門，零重複事實來源；附帶把 MCP 命令安全收斂為 `mcp_security` 單一事實來源。
- 為後續 SMOKE / EVAL 兩道品質關卡預留乾淨掛點，使創建有「先試跑、後評測」的保證。
- skill 創建功能水到渠成（共用引擎）。

### 負面

- 多一層編排抽象（薄，但需維護）。
- 完整能力跨多個 Phase 才到位（本輪僅脊椎，尚不可由 agent 直接呼叫）。
- protocol_manager 即時熱重載延後到路由 Phase。

## 相關 ADR

- [ADR-0044](0044-agent-dispatch-system.md) — Agent 工作分派中心（DispatchAgent 來源）
- [ADR-0046](0046-toukadispatch-dedicated-mcp-server.md) — ToukaDispatch 專用 MCP server（FastMCP 創建範例）
- [ADR-0061](0061-skill-mcp-server-collection.md) — 技能 MCP 伺服器收集（skill 宣告 mcp_servers）
- [ADR-0063](0063-skill-hub-external-sync.md) — SkillHub 外部同步（安全/驗證模式先例）
- [ADR-0066](0066-skill-system-anthropic-standard-migration.md) — Skill 遷移 Anthropic 標準（SKILL.md 目錄 + metadata）
- [ADR-0033](0033-infosec-hardening-c1-m6.md) — InfoSec 五道守衛（沙箱 get_clean_env、命令安全）
