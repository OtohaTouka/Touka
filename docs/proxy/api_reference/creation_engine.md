# 創建引擎 (Creation Engine — ADR-0069)

> **TL;DR** — `creation_engine.create(kind, spec)` 是 MCP 與 Skill 創建的**單一編排入口**。一條固定流水線 **VALIDATE → SMOKE → COMMIT →（後置）EVAL**；每種產物以 `CreationKind` 策略插入（SOLID-OCP），重用既有 SSOT（`SkillService` / `MCPHubService`），**CREATE 不覆寫既有 id**。對外經 `/v1/creation/*`（Owner/admin）與 agent 對話工具開放，兩者共用薄門面 `services/creation/api.py`。

本頁為**開發者參考**：說明子系統結構、擴充方式與安全不變量，並由 mkdocstrings 自動渲染各類別的最新 API（與程式碼同步、不漂移）。對外端點的請求/回應契約見 [PROXY_INTERFACE.md §創建引擎 API](../../architecture/PROXY_INTERFACE.md)；架構決策見 [ADR-0069](../../architecture/adr/0069-touka-native-creation-engine.md)。

---

## 🏭 流水線總覽

```mermaid
flowchart LR
    spec["CreationSpec<br/>(SkillSpec / McpSpec)"] --> V{VALIDATE}
    V -- "report.ok=False" --> R1["CreationResult<br/>committed=False"]
    V -- ok --> S{SMOKE<br/>(run_smoke)}
    S -- "smoke.ok=False" --> R2["CreationResult<br/>committed=False"]
    S -- ok --> C["COMMIT<br/>(原子寫入＋上線)"]
    C --> RES["CreationResult<br/>committed=True"]
    C -. "後置、非阻斷" .-> E["EVAL<br/>(run_eval)<br/>寫回歸基線＋選用計分"]
    E -. evaluation .-> RES

    SC["McpScaffoldSpec"] -. "scaffold (PRE-engine)" .-> spec
```

- **VALIDATE**：純靜態驗證（id 唯一性、欄位合法性、命令安全）。失敗即回 `committed=False`，**不寫入、不丟例外**。
- **SMOKE**（gate，`run_smoke=True`）：註冊前沙箱試跑。MCP 以**淨環境**真啟動握手（`initialize` + `tools/list ≥ 1`）+ 逾時 + 保證拆除；skill 做 SKILL.md round-trip 乾跑（**不執行碼**）。失敗即不 commit。
- **COMMIT**：委派該 kind 的 SSOT 寫入並上線（skill → `SkillService`；mcp → `MCPHubService.register_server`）。
- **EVAL**（後置、**非阻斷**，`run_eval=True`）：commit 後生成唯讀評測基線 `config/evals/<kind>/<id>.eval.md`；`eval_scoring` 預設關閉（見下）。EVAL 失敗只記 warning，**不影響** `committed`。
- **SCAFFOLD**（PRE-engine 生產者，Phase C）：`McpScaffolder` 由高階 `McpScaffoldSpec` 決定性生成 FastMCP server stub，產出可直接餵 `create("mcp", spec)` 的 `McpSpec`。

---

## 🧩 核心概念

| 概念 | 角色 | 位置 |
|------|------|------|
| `CreationEngine` | 只負責**編排與關卡控制**（SRP）；不認識具體產物 | `services/creation/engine.py` |
| `CreationKind`（Protocol） | 每種產物的策略合約：`validate` / `smoke` / `commit` / EVAL 任務生成（DIP / OCP） | `services/creation/kinds.py` |
| `SkillCreationKind` / `McpCreationKind` | 兩個內建 adapter，分別委派 `SkillService` / `MCPHubService` | `skill_kind.py` / `mcp_kind.py` |
| 薄門面 `api` | route 與 agent 工具**共用**的編排層（DRY/SSOT、**不依賴 fastapi**、零 IO、無角色邏輯） | `services/creation/api.py` |
| `McpScaffolder` | 決定性 stub 生成（零 LLM、零付費）＋注入防護 | `services/creation/scaffold.py` |
| `EvalRunner` + `Judge` | EVAL 持久化（SRP）＋可注入評分接口（DIP，預設 `NoopJudge`） | `services/creation/evaluation.py` |
| `ModelJudge` | 把 `Judge` 接縫接上真實 LLM；**雙閘預設關** | `services/creation/model_judge.py` |
| `server_name()` | server 名正規化 SSOT（單段 slug、防穿越） | `services/creation/naming.py` |

---

## 🔌 對外介面（雙介面共用門面）

兩個入口都**只**呼叫 `services.creation.api` 的函式，不各自重複「request → Spec → 引擎」邏輯：

| 入口 | 位置 | 把關 |
|------|------|------|
| HTTP 路由 `POST /v1/creation/{skill,mcp,scaffold}` | `routers/creation.py` | `_require_creation_role`（Owner/admin）。無 token→401、錯角色→403、多餘欄位（`extra=forbid`）→422 |
| Agent 對話工具 `create_skill` / `create_mcp` / `scaffold_mcp` | `tools/builtin/creation.py` | `_gate(identity)`：非 Owner/admin 回 `❌ 權限不足`（facade 不被 await） |

> **錯誤語意**：VALIDATE / SMOKE 失敗 → **200 + `errors[]`**（`committed=false`，產物未上線）；只有非預期例外才 `SYSTEM_ERROR`（500）。

---

## 🔒 安全與不變量

1. **權限門檻**：所有創建路由都經 `_require_creation_role`（`_CREATION_ROLES = {"Owner", "admin"}`）；agent 工具同樣先 gate。由 **Creation Sovereignty Guard #29**（`scripts/guard_creation_sovereignty.py`）靜態執法。
2. **CREATE 不覆寫**：id 衝突於 VALIDATE 關卡擋下——以引擎層**確定性硬約束**落實「不更動既有 agent 資料」；只寫新產物，`data/soul|custom|personal_drive|legacy|vault` 完全不碰。
3. **禁繞引擎**：creation 套件**以外**的模組不得直呼 `.update_skill()` / `.register_server()`（Guard #29 規則 2；allowlist：定義處 + `routers/skills.py`）。
4. **門面純度**：`services/creation/api.py` 不得 `import fastapi`（Guard #29 規則 3），確保 route 與 agent 工具雙介面共用。
5. **Scaffold 注入防護**：識別字 `isidentifier()` + 非關鍵字；自由文字 `repr()` 轉義；型別白名單（str/int/float/bool）；`ast.parse()` 兜底；server 名單段 slug（擋 `../` 穿越）；不覆寫既有 `server.py`。
6. **ModelJudge 雙閘預設關（零成本）**：需 `TOUKA_EVAL_JUDGE_ENABLED` 環境旗標 ＋ 呼叫時 `eval_scoring=True` 兩閘全開才花 token；artifact 截 4000 字；任何例外/解析失敗 fail-safe 回 `0.0`，不阻斷 commit。

### 啟用 LLM 評分（預設停用）

```bash
# 1) 環境旗標（main.py 啟動時據此注入 ModelJudge；缺則保持 NoopJudge）
TOUKA_EVAL_JUDGE_ENABLED=true
TOUKA_EVAL_JUDGE_MODEL=<評分模型 ID>   # 必填：沒設時不啟用評分並記錯誤（ADR-0241）

# 2) 呼叫時開第二閘
await creation_api.create_skill(spec, eval_scoring=True)
```

兩者缺一即不計分、零付費呼叫。

---

## 🧱 擴充新產物類型（OCP 指南）

新增第三種產物（例如 `workflow` / `agent`）**不需改引擎一行**，只要：

1. 在 `services/creation/` 新增一個 adapter，以**結構化型別**（structural typing）符合 `CreationKind` Protocol（實作 `validate` / `smoke` / `commit` 與 EVAL 任務生成）。
2. 委派該產物既有的 SSOT 寫入服務（**勿**在引擎內重造寫入邏輯）。
3. 在 `engine.py` 的 `creation_engine` 單例字典註冊：`{"workflow": WorkflowCreationKind(), ...}`。
4. 若要對外開放：在 `routers/creation.py` 加端點（**務必**帶 `_require_creation_role`，否則 Guard #29 失敗）、在 `services/creation/api.py` 加對應委派函式、（選）在 `tools/builtin/creation.py` 加 agent 工具。
5. 動到請求/回應模型時重跑 `python scripts/update_openapi.py`（否則 Schema 同步守衛 #17 失敗）。

> **鐵則**：新 adapter 的寫入路徑也必須遵守「CREATE 不覆寫既有」；新對外端點必須 Owner/admin 把關。

---

## 📖 程式碼參考 (Auto-generated)

### 引擎 (CreationEngine)

::: services.creation.engine.CreationEngine
    options:
      show_root_heading: true
      show_source: true

### 共用門面 (api facade)

::: services.creation.api
    options:
      show_root_heading: true
      show_source: true

### 產物策略合約 (CreationKind Protocol)

::: services.creation.kinds.CreationKind
    options:
      show_root_heading: true
      show_source: true

### 資料模型 (Specs & Results)

::: services.creation.models
    options:
      show_root_heading: true
      show_source: true
      members:
        - CreationSpec
        - SkillSpec
        - McpSpec
        - McpScaffoldSpec
        - ToolSpec
        - ParamSpec
        - ValidationReport
        - SmokeReport
        - CommitResult
        - EvalTask
        - EvalReport
        - CreationResult
        - ScaffoldResult

### Scaffold 生成器 (McpScaffolder)

::: services.creation.scaffold.McpScaffolder
    options:
      show_root_heading: true
      show_source: true

### 評測執行器 (EvalRunner & Judge)

::: services.creation.evaluation.EvalRunner
    options:
      show_root_heading: true
      show_source: true

::: services.creation.evaluation.Judge
    options:
      show_root_heading: true
      show_source: true

### LLM 評分 (ModelJudge — 預設關)

::: services.creation.model_judge.ModelJudge
    options:
      show_root_heading: true
      show_source: true

### 名稱正規化 (server_name SSOT)

::: services.creation.naming.server_name
    options:
      show_root_heading: true
      show_source: true
