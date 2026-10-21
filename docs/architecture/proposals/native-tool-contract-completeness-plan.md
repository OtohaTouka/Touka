# 原生工具公開介面完整性修復與防回歸計畫

> 狀態：**P1–P6_SOURCE_COMPLETE / DEV-B_ACCEPTED**；2026-09-15。
> Owner 已批准 DEV-A 並委託主代理代審後續審核點；SUPERVISOR_DELEGATED／SUPERVISOR_ACCEPTANCE，非嚴格獨立資格。
> 基線：`dev`，`1093e8d8e9a355c32b363c9dfa9b335dca474cb2`。核准版 322 行原 bytes 留在 task Temp 的 `OWNER_APPROVED_PLAN.md`。
> P1–P4 來源與 sandbox focused 已完成；P5 初回歸與五組 fixture 修訂後799PASS、standalone24/24，最終full R3為9604P／11skip／5warnings，44候選／2422inputs無漂移；兩個HB條件skip另以synthetic原模組11P補驗，fresh非audit43/43與smoke24/24，最終DEV-B已由主代理SUPERVISOR_ACCEPTANCE；P1–P6來源計畫完成，見Root DEV-B 驗收原件。CI 僅來源接線，hosted NOT_RUN、required checks NOT_VERIFIED。
> `research` 三欄位與 handler/schema 修改排除；Git、發布、部署、live 均不執行。原始 RED／測試 harness 錯誤與後補 GREEN 分開保存。

### DEV-A R2 必要修訂（SUPERVISOR_ACCEPTANCE）

Root 唯讀查證三 provider adapter → `handlers/react_core.py` → `ToolExecutor.execute_to_queue` 路徑；
原生工具僅受 PaC 風險分級、缺少 RoleGrants 執行閘，schema 廣告不構成授權。
已依實際 builtin／Memory 分支補 native name grant（builtin 優先），在 PaC／HITL 前拒絕，
沿既有 `resolve_grants(identity.agent_role).tools.allows(name)`；不改角色/private config、MCP/plugin 路徑。
新增精確檔案 `SECURITY.md`、`tests/unit/test_native_tool_execution_grants.py`；既有 executor、ADR、交班與測試文件同步。
Guest／Base／無角色＋PaC ALLOW 的有效 RED 先保留；Owner wildcard／deny／legacy alias／重複名／MCP 舊閘與四級負例驗收。
`research` 的既有角色 grant 執法亦受此通用修訂保護，三欄位與 handler/schema 不改、不外連。
Root 已讀 12F 有效 RED 與 caller graph 並代審接受；原件 `ROOT_DEV_A_R2_ACCEPTANCE.json`。
R2當時的歷史進度是進入實作與GREEN；目前完整來源驗收已DEV-B_ACCEPTED，原12F與各次GREEN仍各自保存。

### DEV-A R3 顯式環境來源（SUPERVISOR_ACCEPTANCE）

full R2原Q48 fresh-child audit在Memory package bootstrap拒絕host.env探測，尚未到新contracts；確認為既有env來源不一致。
新增精確 `ToukaMemory/src/memory_server/config_utils.py` 與 `tests/unit/test_memory_env_selection.py`；沿Proxy既有非空TOUKA_ENV_FILE優先，明示後不fallback，缺省保留原搜尋。
先4F/3P後focused105P；原Q48 factory/audit兩例未改。不搬package/Task、不global disable、不檢查真.env；舊父程序I/O狀態NOT_PROVEN。
Root原件 `ROOT_DEV_A_R3_ACCEPTANCE.json`；新增兩檔後實際候選44。新測前凍結與full驗收獨立於原R2失敗。

### P5 已代審的精確驗證修訂

新增候選 `docs/architecture/NOQA_REGISTRY.md`：factual target_id／安全級測試 oracle 各一個精確 noqa，不做全域豁免。
五個既有測試修訂：`tests/integration/test_tool_loop_budget.py`、`tests/unit/test_identity_sovereignty.py` 只給假工具精確 grant；
`tests/unit/test_evolution_deploy_approval.py` 只補 typed evolution 必填合法 args；
`tests/unit/test_guard_all_frontend_runtime.py` 同步45總／43非audit且保留未完整狀態斷言；
`tests/unit/test_deployment_permissions.py` 以本機Git blob對齊真正consumer的raw bytes，保留固定digest並拒絕CRLF差異。
不修改產品使錯誤fixture通過、不讀真.env、不改EOL／Git設定；原full FAIL保存。新的task-owned pytest Temp避開既有目錄權限，不改ACL。

### 已核對的實作細節修正

- `artifact_ref` 與 `artifact_refs` 是獨立 legacy／public 欄位，不互合；top-level／初始化 nested singular 保真並驗型別／領域路徑，schema 只公開 plural。
- `await_dispatch_result` 不消費 `target_agent_id`，其 action model 不繼承該欄。
- quota 保留原值到既有 verifier **只 clamp 一次**；bool/無效值回上限，0.5 依既有 int 行為為 0；不更動建立 venv/install/deploy 配額。
- Guard #45 採 AST-only 來源查核，不 import Memory 父 package；其既有 29 modules／149 函式形狀 baseline 含私有 helper，合法重構也需顯式更新，不能推導任意動態 Python。

## 1. 目標與現況

模型應看得到 Touka 原生工具已支援、且允許模型指定的完整參數；依揭露介面呼叫時，參數應到達正確實作並產生預期效果。
新增工具、action、選填或巢狀欄位時，漏宣告、漏傳遞與轉換遺失應由本機檢查與 PR 檢查攔截。
不把系統注入的身分／session、內部服務選項或退役別名當成應公開的工具能力。

前兩輪唯讀調查以本基線的 registry、schema、ToolLayer、executor 與領域實作交叉比對：

- builtin registry 40 名、Memory registry 5 名，重複 3 名，合計 42 個可執行名稱。
- builtin schema 30 名、Memory schema 3 名，mission 重複，合計 32 個可能揭露的原生工具名稱。
- 此為來源清單統計；實際請求仍受權限、PaC、desired_tools、headless 與 incognito 過濾，未讀取現行使用者 payload。
- ToolLayer 同名定義優先序為 request tools → builtin → Memory；executor 的同名 native handler 以 builtin 優先。
- 本次採「8 組、12 個非 alias 欄位＋1 個 action」及另外 6 組條件／描述問題作修復範圍。
- 上述數量只描述基線，不寫成守衛硬編碼的預期工具數，也不作為執行通過證據。

證據入口：[builtin registry](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/tools/builtin/__init__.py)、
[Memory registry 與 action 分派](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaMemory/src/memory_server/tools/__init__.py)、
[Memory schema](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaMemory/src/memory_server/tools/schemas.py#L51)、
[ToolLayer](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/middleware/layers/tool.py)、
[ToolExecutor](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/services/tool_executor.py)。

### 1.1 與既有重設計的關係

[ADR-0119](../adr/0119-unified-tool-interface-redesign.md) 已接受 Tier 1–3：9 個檔案裸動詞已落地，舊 god-tools 保留可執行 shim、停止廣告。
本案是既有介面的完整性補強，不重啟 Tier 3，不改成另一套工具命名或 scope 設計。
ADR 的 Tier 2 明確決定 identity／project_id 僅描述透明化、不進模型 required；本案沿用。
[舊提案](native-tool-interface-redesign.md) 的 Tier 2 第 2 點仍寫成 required，與其已收斂 ADR 不一致；
根 `AGENTS.md` §8／禁令 59 亦仍稱 Tier 3 在提案階段。實作文件同步時更正這些現況敘述並保留歷史脈絡。
[MEMORY_INTERFACE](../MEMORY_INTERFACE.md) 的 `**kwargs` 是 runtime 橋接合約，建立 typed public inputs 不代表一次改掉全部橋接簽章。

### 1.2 明確不做

- `research.search_depth`／`max_results`／`include_answer` 的揭露、預設值與 Tavily 路由行為均不改。
- 不新增計量 provider 呼叫、真實模型 eval、真 `data/`／`soul/` 讀寫、掛載或初始化。
- 不公開 GDPR `export_facts` 內部入口，不把 `importance_min` 等既有 alias 算作新增能力。
- 不擴大 soul／vault 寫入權、跨身分任務權限、sandbox 配額或 session 操控權。
- 不重寫全部 42 個 handler，不改 MCP 協定遷移、P11、liblzma、IDE 發行、部署或依賴版本。
- 不新增 HTTP 端點、前端功能、資料庫 schema 或 migration；若證據顯示必須新增，先回到範圍審查。

## 2. 修復明細與可核對的 RED

以下 source 連結固定至基線 commit；以函式／欄位名稱定位，不依賴未來分支狀態。文件互連使用相對連結。
下列「預期RED」保留DEV-A核准時的歷史TDD設計；目前已執行並完成來源驗收，實際RED／GREEN以原件為準。匯入錯誤、環境失敗或測試程式錯誤不得冒充有效RED。

### 2.1 八組主要缺漏

| ID／範圍 | 基線證據與問題 | 預期行為／RED 理由 | 測試與修改落點 |
|---|---|---|---|
| M1：`memory_tools.emotions`，save／query 共用 1 欄 | [manager.save_memory／query_memory](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaMemory/src/memory_server/services/manager.py#L213) 已接受；[schema](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaMemory/src/memory_server/tools/schemas.py) 缺欄位，與 tags 是不同用途 | `list[str]` 自由感情標籤，不新增封閉 enum；schema 說明查詢用途，非預設值經公開入口到 factual／vector。RED：模型 schema 無欄位 | Memory public contracts、schemas、gateway；新增 memory contract tests，mock／sandbox 驗證保存與篩選 |
| M2：query 的 `date_start`／`date_end`／`is_recent`，3 欄 | [manager 的日期正規化與 query](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaMemory/src/memory_server/services/manager.py#L264) 已支援；目前「最近」字樣只影響 query 策略，不等同 7 日限制 | `is_recent=True` 取最近 7 日；與 date_start 同給取較晚起點，date_end 含當日末；時間走既有 SSOT。RED：schema 不含三欄 | 同 M1；固定時鐘、起訖邊界、指定日期＋recent 組合、無結果與非預設值傳遞 |
| M3：forget 的 `fact_id_to_archive`，1 欄 | [resolve_duplicate](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaMemory/src/memory_server/services/manager.py#L367) 必需 ID；schema 只列 forget action，query render 未顯示可用 ID | forget 條件必填；query 的 factual 命中用 `id`、vector 命中用 `fact_id`，輸出同一 canonical factual ID，保持目前文字回傳型態。RED：無公開參數且查詢結果不能穩定取得 ID | Memory manager／gateway／contracts；sandbox query→forget→確認該 fact archived；另一 identity 的 ID 拒絕且不洩漏 |
| M4：`manage_dups`，1 action | [MEMORY_ACTIONS](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaMemory/src/memory_server/tools/__init__.py#L89) 已分派；[diag](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaMemory/src/memory_server/tools/diag.py#L116) 的內層 `action=scan/resolve` 與外層衝突 | 公開 `action=manage_dups`，另用 `operation=scan/resolve`，預設 scan；resolve 條件必填 `target_id`。gateway 明確映射至內層 action。RED：enum 缺漏且 resolve 無正確外層路徑 | Memory contracts／gateway／diag；正式入口 scan、resolve、缺 target、未知 operation、跨 identity 拒絕 |
| M5：mission initialize 的 `tasks[].review_required`／`review_risk`，2 欄 | [MissionManager.initialize_mission](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaMemory/src/memory_server/services/mission_manager.py#L185) 消費；[Proxy schema](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/tools/mission_tools.py#L410) 缺巢狀欄位 | 原 false／normal 預設與領域有效值不變；非預設值建立出的任務可回讀。RED：tasks item schema 缺欄位 | mission typed inputs／schema／橋接；既有 manager sandbox、任務審核政策與非法值測試 |
| M6：mission initialize 的 `tasks[].test_log`／`artifact_refs`，2 欄 | [初始化證據欄位](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaMemory/src/memory_server/services/mission_manager.py#L185)；目前僅 top-level update_status 揭露 | 初始任務可攜帶既有格式的證據；沿領域路徑驗證與 FSM，不把初始化證據等同 DONE／審核通過。RED：巢狀 schema 缺欄位 | 同 M5；非空 test_log／多 refs、路徑穿越拒絕、內容回讀、既有獨立單複數欄位保真 |
| M7：`evolution_implement_and_verify.timeout_override`／`max_storage_mb`，2 欄 | [evolution wrapper／verify_mcp_locally](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/tools/builtin/evolution.py#L400) 已傳遞與消費；[builtin schema](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/tools/builtin/schemas.py#L366) 缺欄位 | 只縮小 MCP 驗證 sandbox 配額，不控制整體安裝／實作耗時，也不套到一般 Skill。沿既有 clamp：正數且小於系統上限才生效，其他值維持上限 | Proxy public contracts／schemas；mock sandbox 驗有效值、等於／大於上限、零／負數與既有型別邊界，不能放大配額 |
| M8：`create_skill.author`，1 欄 | [creation wrapper](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/tools/builtin/creation.py#L47) 接受，落到 [SkillSpec](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/services/creation/models.py)；schema 缺 | 只設定作者 metadata；未填／空值沿既有 agent_id fallback，不改身份／Owner 閘。RED：schema 無 author | Proxy public contracts／schemas；mock creation API 驗作者與預設值，禁止因產生 schema 暴露 `is_system`／MCP `sdk_major` |

M4 的 `operation`／`target_id` 是打通既有 duplicate 功能所需的公開子參數，不回填成「原稽核 12 欄」的一部分。
M3 不使用 vector 自身 ID、顯示序號或內容 hash 代替 factual ID；無 canonical ID 的結果不得被偽裝為可 forget 的項目。
必要的 ID 格式補充是回傳文字的加法；不新增資料表、全域匯出或跨 agent 掃描。
M4 須接既有 action risk policy；現有 `ToolRiskEntry` 以 action 粒度分類，`manage_dups` 採 `dangerous` 保守寫入分類，scan 亦受同級審批。
forget／resolve 不得因新 schema 而略過寫入審批；不為區分 scan／resolve 新造 ABAC。Guest、ReadOnly、Manual、SafeAuto、FullYOLO 各有授權負例。
來源 SSOT 是 `ToukaProxy/config/auth_policies.json.example` 的 `tool_risk.tools.memory_tools.actions`，在其中補 `manage_dups: dangerous`。
`ToukaProxy/src/models/safety.py` 的 `ToolRiskConfig.class_for()` 現已把未知 action 歸 dangerous；現場私有設定未讀取、不修改，既有 fallback 必須保留。
新 contract guard 比對來源 action 與 tracked policy example；既有 Guard 32 如需共用純解析 helper／補測，限 `scripts/guard_permissions_registry.py` 與其既有 unit tests，不重造政策表。
M7 配額以執行時既有 SSOT 上限為準；程式 fallback 30 秒／50 MiB 不代表已查明現場設定，亦不涵蓋 venv 建立／install／deploy。

### 2.2 六組條件與描述修正

| ID | 基線證據 | 決定與預期 RED | 測試／修改落點 |
|---|---|---|---|
| C1：save 的 content | [Memory schema](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaMemory/src/memory_server/tools/schemas.py#L51) root required 僅 action | save 明確條件必填 content；沿既有內容清理與長度限制，不把其他 action 也設 content 必填。RED：漏 content 的公開呼叫未由 contract 給出可修正錯誤 | M1 contracts／gateway；各 action 正反例，現有清理與 8000 字規則回歸 |
| C2：完成通知條件 | [mission_tools](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/tools/mission_tools.py#L106) 僅 target_agent_id 為 DispatchAgent 時採用 notify_on_complete | 描述明示 target 條件，其他 target 不發通知；保持既有權限與預設。RED：schema 描述未交代，只有正向測試不足 | mission schema、[既有 notify tests](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/tests/unit/test_mission_tools_notify.py) 加非 DispatchAgent 與未授權分支 |
| C3：ide_open_diff 的 session_id | [editor schema](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/tools/editor.py#L293) 要求模型填；[executor](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/services/tool_executor.py#L588) 最後覆寫 | 公開 properties／required 不要求 session_id；可信 runtime 照常注入。舊模型送入的此欄於邊界剝除，不准跨 session 控制。RED：公開 schema 仍 required | editor schema／contract、executor；無 session 參數可正常呼叫、偽造值不能覆蓋 context |
| C4：vault_tools 舊 schema 回流 | [Memory schema list](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaMemory/src/memory_server/tools/schemas.py#L117) 回流；[Proxy bridge](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/tools/vault_tools.py#L53) soul 僅讀 | 從完整廣告來源移除 legacy shim；保留必要執行相容，不擴 soul 寫權。同步校正殘留 legacy 形狀說明。RED：帶真 Memory schemas 的 ToolLayer 最終清單出現 vault_tools | Memory schema exports、ToolLayer 契約檢查、[schema hygiene test](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/tests/unit/test_tool_schema_hygiene.py) |
| C5：edit_soul 約束 | [system 實作](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/tools/builtin/system.py#L155)：insert_line 為 0-based；soul.md 最少 50 字；soul.md／config.json 不可 delete | schema 如實寫現行語意；本案不默改成其他檔案動詞的 1-based。RED：描述未完整揭露重要條件 | edit_soul schema／contract；sandbox 0／中間位置、49／50 字、protected delete 拒絕；不操作真 soul |
| C6：dispatch_subagent_group 上限 | [subagent_tools](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/tools/subagent_tools.py#L58) 單批 8、並行 4；描述只說有上限 | 公開明示單批最多 8、最多並行 4；由現行常數生成相關限制，不另存兩份數字。RED：schema／描述無精確限制 | group schema／contract、[既有 group tests](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/tests/unit/test_subagent_group.py)；8／9 個任務與受控 async fake 併發上限 |

## 3. 合約設計與所有權

### 3.1 單一公開輸入來源

沿用既有 Pydantic 2.12.5 與 jsonschema 依賴，不新增套件或重生 lock。
建立純資料 public-input contracts：Memory 領域置於 `ToukaMemory/src/memory_server/tools/contracts.py`，
Proxy 工具置於 `ToukaProxy/src/tools/contracts.py`；必要的純 schema helper 放既有 schema 邊界，不建立新 plugin framework。
Memory 不 import Proxy 內部；Proxy 引用 Memory 公開合約而非重寫 memory 欄位。

本次完整 typed 遷移限 7 個受影響公開工具：memory_tools、mission_tools、evolution_implement_and_verify、
create_skill、ide_open_diff、edit_soul、dispatch_subagent_group。mission 的 tasks item 使用具名子模型。
每個模型包含公開欄位、型別、預設、描述與條件；同一來源供 canonical schema 及公開參數驗證使用。
不把完整 SkillSpec 或 Task 儲存模型直接當公開輸入：`is_system`、runtime identity、session 等不會因此曝光。

其餘現行工具先引用既有 schema，登錄 public／internal／legacy／alias 分類與既有 handler，避免全量重寫。
新原生工具與新 public action 必須提供 typed contract；未遷移工具增加公開欄位時，須一併遷移該入口。
既有公開欄位不建立第二份人工名單；對動態 kwargs 無法判定的舊路徑只設精確的 callable／field／原因例外，禁止整工具永久豁免。
本案 `research` 只保留上述三個既有 per-call 內部欄位的排除分類與行為，不能因此豁免未來新增欄位。

### 3.2 呼叫與身分邊界

1. 維持既有 Gate 1、Gate 2、PaC、auth_level、角色與工具政策，不把 schema 合法視為授權。
2. 從原始模型 args 分離可信 runtime context；保留現有 alias 正規化與明確的 legacy session 剝除規則。
3. 僅對選定 typed 原生入口驗證公開參數；不得用全域 strict model 改變 research、MCP、plugin 或內部 GDPR 呼叫。
4. 驗證成功才建立 public kwargs，再合併可信 context；identity／auth_level 等只能來自既有執行上下文。
5. 無效輸入以既有 `build_tool_error(error_class="validation_error", ...)` 回覆可修正欄位／原因，不含原始敏感輸入、完整例外或 stack trace。
6. Memory 保留整合 gateway 的 `**kwargs` 接縫，使用型別合約清理公開欄位後委派；不為此次修補改成獨立 MCP 模式。

公開 typed validation 對一般欄位錯型別、非法日期或未知 kwargs 的拒絕，屬本次明示的行為改變，不稱為純 schema 加法。
無效日期採結構化 validation_error；合法日期與既有 alias、runtime 注入、internal 入口均有回歸，避免寬泛清理影響其他呼叫者。
M7 兩個 quota override 為明列例外：保留原值到既有 verifier 只 clamp 一次；無效值仍回系統上限，不讓通用 strict 型別驗證改成直接拒絕。
此逐欄策略由 contract metadata／normalizer 記錄並測試，不使用全域寬鬆 `extra=allow` 放過未知欄位。

沿用 [EXECUTION_CONTEXT_KWARG_NAMES](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/tools/execution_context.py) 作 runtime metadata SSOT；
`identity` 是另有明確保護的領域身份，不因未列入該集合而允許模型提供。
canonical 公開參數、內部可接受 alias、trusted context 三者分開驗證；不把所有 kwargs 差異都宣告漏洞。

### 3.3 action 與 provider schema

Memory action 的公開名稱與對應 contract 由同一 registry 記錄，導出公開 enum；`export_facts` 仍分類 internal。
`manage_dups.operation` 於 gateway 明確映射到既有診斷函式的 `action`，不讓第二個 action 被 Python 呼叫語法吞掉。
條件必填採 action-specific input 模型；公開 canonical JSON Schema 的同名欄位來自模型，而非重抄字典。
動態業務條件仍保留 runtime 驗證，不能宣稱 Pydantic 任意自訂 validator 都會自動變成 JSON Schema。

沿用 [ADR-0100](../adr/0100-tool-schema-canonical-openai.md) 的 canonical／最後一步 provider 投影。
處理 Pydantic `$defs`／`$ref`、nullable 與 union 時，僅展開本機封閉 definitions，不抓外部 `$ref`；遞迴／未知投影明確報錯。
[Gemini converter](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/ToukaProxy/src/adapters/openai_to_gemini.py) 目前剝除 oneOf／anyOf／allOf／const；
不能把全部欄位藏在將被剝除的 union 分支中。投影保留公開 property paths、型別、enum、必要描述與 top-level required，
條件必填另保留對模型可讀的 action 說明並由 runtime 強制；不要求 provider schema bytes 完全相同。
OpenAI／Anthropic／Gemini 全部以真實工具 schema 驗 semantic parity，不只用 toy schema 證明 converter 能運作。

## 4. 新增一項原生工具合約守衛

預計新增 `scripts/guard_native_tool_contracts.py`，掛入 `guard_all.py`；**本計畫不預配 Guard／ADR 編號**。
檢查核心為純函式，可被 pytest 直接調用；CLI 只負責有限 source inventory、輸出與 exit code。
使用既有字面 registry／AST 接點，兼顧 builtin 與 Memory，不能為讀 schema 而初始化產品 manager／config。
已核對 `memory_server/__init__.py` 會呼叫 `bootstrap_env()`；因此 static guard **不 import memory_server 或其 tools／contracts**，固定採 AST 檢查來源形狀／引用／分類。
保留上述 domain-owned 檔案落點；模型 schema 產生及實際 public-contract parity 只在既有 `conftest.py` 已完成隔離後的 pytest 內 import 執行。
純 contract 模組本身不得加 I/O，亦不宣稱這會讓其父 package 自動無副作用；不為此次守衛重構全專案 bootstrap。

守衛檢查：

1. 每個 executable native name 均有 public contract 或具體 internal／legacy／alias 分類；新增未分類名稱直接失敗。
2. 公開 action 與實際分派對齊；內部 action 必須明確列出理由，不要求所有 backend actions 向模型公開。
3. 遷移入口的 schema 由同一 input contract 導出；必要欄位、選填欄位與 nested paths 無遺失。
4. 檢查 wrapper／handler 的字面 kwargs access 與已宣告欄位；未知動態 consumer 必須具體分類，不回報「已自動證明完整」。
5. reserved runtime 欄位不得誤列 public required；對 C3 的舊輸入相容規則另驗不具權限效果。
6. 完整組裝後的 public tool set 不含 legacy shim，去重優先序有測試，不能僅看 builtin list。
7. 失敗輸出定位 tool／action／field path／source 與原因，不印真資料或完整模型參數。

現有 [Guard 32](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/scripts/guard_permissions_registry.py) 繼續維護名稱／風險分類，
[Guard 38](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/scripts/guard_tool_events.py) 繼續驗事件 envelope；本案不挪用其名稱冒稱 input completeness 已受保護。
[smoke_tool_coverage](https://github.com/OtohaTouka/Touka/blob/1093e8d8e9a355c32b363c9dfa9b335dca474cb2/tests/smoke/smoke_tool_coverage.py) 的 memory schema 豁免改為正確跨 registry 檢查，
legacy shim 仍可明確分類；危險工具的 `WIRED_ONLY` 不改成真執行或真寫入。

### 4.1 守衛的反例測試

新增 `tests/unit/test_guard_native_tool_contracts.py`，使用小型 source／schema fixtures，不修改真業務檔案作 mutation：

- 新 executable tool 無 contract／分類 → FAIL；補完整 contract → PASS。
- 新 public action 無對應 handler／schema，或把 internal action 意外公開 → FAIL。
- 新 optional kwargs 欄位未宣告、刪掉 emotions、遺失 tasks[].review_required → FAIL。
- 新動態 consumer 無精確分類、擴大 research 例外到整工具 → FAIL。
- provider 投影移除欄位或錯改 enum／required → FAIL。
- Memory 重新注入 vault_tools、公開 session_id required 或 identity → FAIL。
- 合法 alias、已列 internal action、provider 已知條件投影差異 → PASS，避免假陽性逼人加廣泛豁免。

AST 只覆蓋限定可分析模式；任意動態 Python 與描述的自然語言品質，不能靠此守衛保證。
同源產生 schema 也不能證明下游真的使用每個值，故下節行為測試不可省略。

## 5. Gherkin 與 TDD 對照

以下為已明確需求的可執行場景，不另外要求 Owner 重複決定原已選範圍。

| 場景 | Given／When／Then | 預定測試／有效 RED |
|---|---|---|
| G1 完整記憶操作 | Given 有記憶工具權限；When 保存含感情的記憶並用日期／感情查詢；Then 符合條件的結果及可操作 ID 可見 | `test_memory_contract_emotions_and_date_filters`；schema 缺欄位／傳遞值遺失 |
| G2 正確封存 | Given 查到自身一筆記憶；When 用結果 ID forget；Then 僅該 factual／vector 記憶被封存；別人的 ID 不可操作 | `test_query_to_forget_uses_canonical_fact_id`；query 無 ID 或 vector ID 對錯儲存實體 |
| G3 重複處理 | Given 有可處理重複項目；When scan 後以 operation=resolve 與 target_id 呼叫；Then 正確處理或提示缺少參數 | `test_manage_dups_public_gateway_operations`；outer／inner action 衝突 |
| G4 任務完整初始化 | Given 有合法目標看板；When 建立含審核要求及證據的任務；Then 回讀相同值且原 FSM／審核權限仍成立 | `test_mission_initialize_preserves_review_and_evidence`；nested 欄位缺失 |
| G5 正確通知 | Given 分別為 DispatchAgent／其他目標；When 設 notify_on_complete；Then 僅合法 DispatchAgent 流程發通知 | 擴充既有 notify tests；描述斷言及負向 target 缺漏 |
| G6 可縮小配額 | Given 系統已有驗證配額；When 傳較小 override 或超大值；Then 驗證 sandbox 可收緊、不能超過系統配額 | 擴充 evolution tests；公開 schema 缺漏／clamp 被繞過 |
| G7 作者是 metadata | Given Owner 合法創建；When 提供 author 或省略；Then metadata 正確且權限身份不變 | 擴充 creation tests；schema 缺漏／作者未傳入 |
| G8 清楚與可信的介面 | Given 模型取得工具清單；When 開 diff、edit_soul 或 group dispatch；Then session 自動錨定，0-based／50 字／8 與 4 上限可見且受執行層維持 | editor／soul／group contract tests；公開 session 必填或描述漏條件 |
| G9 不回流與不漏轉換 | Given builtin＋真 Memory schemas 與各授權分支；When ToolLayer 組裝後投影給各 provider；Then 合法欄位完整且無 legacy vault | `test_native_tool_payload_contracts`；現有 Owner fixture 沒帶 Memory schemas |
| G10 新工具防漏 | Given 開發者新增工具／action／optional／nested 欄位；When 執行守衛與 PR 檢查；Then 未完整宣告或映射的變更失敗並指出欄位 | Guard negative fixtures；原守衛只看名稱或 schema 存在 |

測試分三層：最終 ToolLayer＋provider 真 schema、公開入口到 mock／sandbox 的非預設值效果、守衛反例。
優先擴充已有 tests，不為每個欄位新增獨立測試檔；預定新增：
`test_memory_tool_contracts.py`、`test_native_tool_payload_contracts.py`、`test_guard_native_tool_contracts.py`。
identity／policy／manager／provider client 必須使用既有 fixtures；Memory store 效果測試採 sandbox，其他以確定性的 fake／mock。
禁止只呼叫 inner diag.resolve 或 manager.forget 就宣稱公開工具鏈已通過。
每次建立 RED 後保存原 stdout／stderr／exit code、基線與測試輸入；GREEN 新增結果，不覆寫 RED。

本次 7 個 typed 工具的傳遞測試另建立可計算的 field-path coverage：從合約列出 action-specific 公開欄位與 nested paths，
每個非預設值 case 宣告應驗 paths，再對實際 mock 呼叫／sandbox 回讀值斷言；只有案例名稱或自行回報 covered 不算通過。
省略預設、條件不適用與 quota fallback 需明確 case 分類；新增 optional／nested 欄位沒有有效案例即 FAIL。
加入「schema 已有新欄位，但測試 fixture 的下游橋接丟掉該值」反例，確認實際傳遞斷言會失敗；此仍屬上述傳遞／守衛測試，不新增第四套框架。

## 6. 實作順序與檢查

| 階段 | 工作與順序 | 階段出口 |
|---|---|---|
| P0：DEV-A | 審閱本計畫、範圍、風險與當次實作授權；重新核對 base／其他候選變更，不沿用 P11／IDE 核准 | 明確 DEV-A 結論；未通過只修計畫 |
| P1：基線與 RED | 建立精確 source／test inventory；補 M1–M8、C1–C6 的有效 RED；先帶 Memory schemas 修正漏測 fixture | 原始 RED 與環境異常分開；未修產品前看到正確失敗 |
| P2：領域修復 | Memory contracts／ID／duplicate gateway → mission typed nested fields → Proxy 其他欄位與條件；每組 RED→GREEN→小重構 | 所有範圍內公開入口與資料效果測試 GREEN |
| P3：合約與最終揭露 | 接受公開驗證、trusted context 分離、provider 投影；保留既有 aliases／權限／過濾 | 三 provider 真 schema 與 runtime 邊界測試 GREEN |
| P4：防回歸 | 先寫 guard negative fixtures，再實作單項 guard；跨 registry smoke、guard_all、pytest 與最小 PR workflow | 所有故意缺漏確實 FAIL、合法新增 PASS；危險工具仍 WIRED_ONLY |
| P5：整體驗收／文件 | focused、全 pytest、standalone smoke、lint／完整 guards 含 audit；同步文件、建立最終 exact candidate | DEV-B 材料完整，無未處理 ERROR／未知重大風險 |
| P6：DEV-B／交付 | 依當次模式審核原件、計畫偏差與候選；Git／發布依另有有效授權辦理 | source acceptance 與 commit／release／live 狀態分開回報 |

### 6.1 本機驗證方式（原計畫命令；本輪進度以上方與交班為準）

使用既有 `.venv` Python 3.12，先 Activate 或把 `.venv/Scripts` 置於 PATH 首位。
pytest 原生執行 `python -B -m pytest`；禁止 stdin／runpy 或全域 subprocess.Popen 包裝。
不另建整套環境；必要依賴同步須 `uv sync --locked --all-packages`，使用原 CPU Torch lock。

1. 逐組 focused pytest（新 contract tests＋受影響既有測試）；原始 RED／GREEN 各自留存。
2. `python -B -m pytest tests/`，使用原 `conftest.py` sandbox，不跳過其初始化保護。
3. 既有 `run_all_smoke_tests.py` 入口完整 standalone smoke，保留每項結果與隔離證據。
4. 對候選 Python 執行既有 Ruff check／format check，確認 typing 匯入與 undefined name；執行 SSOT lint。
5. `python -B scripts/guard_all.py` 完整範圍含新 guard／audit；若依現有入口分批跑 `--non-audit` 與 audit，逐批記錄而不稱單次全綠。
6. audit evidence 使用 repo 外新目錄；完整 Python runtime／build-test、Torch CPU／editable 與 Node 各 scope 覆蓋保留。
7. MkDocs build 至 task Temp、ADR／文件連結／tests README 計數與 exact diff 檢查。

未改 Chat／IDE 程式時，不額外重跑其整套 package 流程；完整 guards 中適用檢查照實保留。
P11／IDE 的歷史 PASS 不能轉套本候選；重用需逐項證明輸入、內容與環境適用且標記 `REUSED_NOT_FRESH`。
audit metadata 需 fresh 的範圍依當次完整驗收處理，離線／網路阻礙不能改稱零漏洞或全部完成。
本節命令保留核准時的執行設計；逐組RED／GREEN、P5完整來源驗證及P6 DEV-B均已完成。每批以task Temp原件為準；P1驗證selector誤記依correction標UNKNOWN，不從測試結果反推原命令。

### 6.2 最小 PR 檢查

預計新增 `.github/workflows/native-tool-contracts.yml`，只做 source contracts／focused pytest；不承擔發版 qualification。
採 `pull_request`，`permissions: contents: read`、checkout 不保留 credentials、不用 secrets／`pull_request_target`／自架 live runner。
使用 Python 3.12、既有 uv.lock 與 `uv sync --locked --all-packages`，actions 於實作時核對官方來源並固定版本／commit。
`tests/conftest.py` 會載入 workspace 服務依賴，不能只裝 pytest／Pydantic 或移除 conftest 來縮短 job。
保留 provider 清空／bootstrap=false／sandbox 配置；不得繼承部署憑證、真設定或資料 cache。
單 job 初始 timeout 20 分鐘、不加自動重試；第一次耗時量測後只能依證據調整，不盲目延長。
PR 檢查範圍需涵蓋 contract／工具／executor／schema converter／guard／相關 tests／workflow 變更，避免過窄 path filter 漏跑。
最簡單預設對全部 PR 執行此精簡 job，名稱穩定，成功／失敗可回讀。
遠端 branch protection／required checks 為 **NOT_VERIFIED**；workflow 綠燈不代表合併已被強制保護。
正式 required-check 設定屬後續 GitHub 管理動作，本輪不更改或宣稱已啟用。

### 6.3 網路與停止條件

核准時文件工作未下載／外傳；實作驗證沿授權由 Root 完成本輪兩 Python 與四 Node scopes audit。最終按13個inputs及audit腳本閉包明標 REUSED_NOT_FRESH；Node有10個scope findings／4個moderate套件，沒有HIGH/CRITICAL，不宣稱零漏洞。
後續必要官方依賴／advisory／Actions 作業在有效授權內，先記錄目的地、傳輸內容、命令、timeout 與重試上限；只送套件 metadata／核准來源。
相同錯誤三次，或同方向兩輪無進展即停止同方向、保存原件並分析；不以換批次／代理清零。
無法安全隔離、發現未授權資料路徑、context 權限漂移、provider 欄位遺失或測試不可判定，均阻擋該階段出口。
外部阻礙先完成獨立工作，剩餘標 `BLOCKED` 與實際缺口；不把平台限制包裝成 Owner 尚未同意原已授權事項。

## 7. 預定檔案範圍與文件同步

以下是實作允許評估的路徑集合，**不是目前已修改或已驗收的 exact candidate**。
每列先列目錄前綴，其後相對檔名皆接在該前綴下；不代表允許任意目錄檔案。列為既有的 tests 已核對實際存在。
P1 在建立 RED 前列明每檔必要性與新增／修改；新增落點需先對照現有可重用 helper，避免重複模組。
P5 結束後重建 tracked／staged／unstaged／untracked／deleted／renamed 精確清單，含內容與 patch SHA-256，再交 DEV-B。
只收本計畫實際需要的檔案，其他人的修改保留；scope／bytes 改變即重建證據，不以資料夾 glob 當核准 manifest。

| 類別 | 預定路徑／責任 |
|---|---|
| Memory | 前綴 `ToukaMemory/src/memory_server/`：`tools/contracts.py`（新）、`tools/schemas.py`、`tools/__init__.py`、`tools/diag.py`、`services/manager.py`；mission 純輸入型別若在 Memory 共用，仍留此領域 |
| Proxy 工具 | 前綴 `ToukaProxy/src/tools/`：`contracts.py`（新）、`builtin/schemas.py`、`builtin/memory.py`、`builtin/creation.py`、`builtin/evolution.py`、`builtin/system.py`、`mission_tools.py`、`editor.py`、`subagent_tools.py`、`vault_tools.py`；僅必要契約接線／描述，不重寫引擎 |
| 共用邊界 | 前綴 `ToukaProxy/src/`：`services/tool_executor.py`、`tools/execution_context.py`、`middleware/layers/tool.py`、`adapters/openai_to_gemini.py`；其他 provider adapter 僅測試證實需修才列 exact path |
| Action risk 接線 | `ToukaProxy/config/auth_policies.json.example` 增加 M4 explicit dangerous；`ToukaProxy/src/models/safety.py`／`ToukaProxy/src/services/permission_service.py` 原則不改，沿既有解析／fallback；Guard 32 必要 helper 接線限 `scripts/guard_permissions_registry.py`／`tests/unit/test_guard_permissions_registry.py` |
| 守衛／CI | `scripts/guard_native_tool_contracts.py`（新）、`scripts/guard_all.py`、`.github/workflows/native-tool-contracts.yml`（新）；Guard 32 原則重用、不隨意擴為另一套參數清單 |
| 新測試 | 前綴 `tests/unit/`：`test_memory_tool_contracts.py`、`test_native_tool_payload_contracts.py`、`test_guard_native_tool_contracts.py` |
| 既有測試 | 前綴 `tests/unit/`：`test_tool_schema_hygiene.py`、`test_smoke_production_repairs.py`、`test_mission_tools_notify.py`、`test_creation_tools.py`、`test_evolution_tool_ssot.py`、`test_evolution_security.py`、`test_subagent_group.py`；editor／soul／provider 既有檔以 P1 精確確認 |
| Smoke | `tests/smoke/smoke_tool_coverage.py`；runner 僅在確有接線必要時修改，保留既有 sandbox 與危險工具策略 |
| 正本文件 | 前綴 `docs/architecture/`：`MEMORY_INTERFACE.md`、`PROXY_INTERFACE.md`、`SSOT_MASTER_MAP.md`；公開參數、context 邊界、guard 接點 |
| 治理／索引 | `AGENTS.md` 適用工具／守衛說明、`tests/README.md`、`docs/architecture/adr/README.md` 與同時建立的新 ADR、`docs/CHANGELOG.md`、本計畫、`HANDOVER.md` |
| 舊提案 | `docs/architecture/proposals/native-tool-interface-redesign.md` 僅更正與 ADR-0119 不一致的現況，保留歷史驗證數據 |

新 ADR 記錄 public contract 所有權、runtime 分離、provider 投影、例外分類與守衛義務；實作時先核對連續編號，再同時建檔／索引，現在不引用不存在編號。
AGENTS DATA_PROTECTION、權限規則與禁令原意不變；只校正 Tier 3 落地狀態並補本次實際 guard／合約接點。
未修改 FastAPI 端點不重生 openapi.json；未修已證實安全弱點不在 SECURITY.md 虛列「漏洞已解決」。
如驗收發現安全弱點或新的 noqa，依現有協議補精確安全紀錄／NOQA_REGISTRY，並納入重新審核的 scope。

## 8. SOLID、相容性與安全

- **SRP**：input contracts 描述公開參數；gateway 負責驗證與映射；領域服務維持實際行為；guard 只檢查，不做產品初始化。
- **OCP**：新增工具透過既有 registry＋contract metadata 接線；不靠改 guard 硬編碼總數放行。
- **LSP**：既有合法 input／alias／預設不意外改義；新增條件驗證以 action 為單位，不讓 save 規則影響 query。
- **ISP**：public inputs、runtime context、儲存模型分離；不讓模型接收只有內部服務需要的欄位。
- **DIP**：Memory 仍透過 IResourceProxy／既有注入接縫；測試依賴 fake／mock，無跨子系統反向 import。
- 不為符合名詞而新增基底類別、插件系統、通用 DSL 或第三份 registry；依真實重用需要抽取。

風險定為 **HIGH**：變更公開工具參數、Memory 封存識別碼、可信 context 分離及 CI 執行入口。
原始調查僅界定介面完整性；DEV-A R2 後另已確認並修補 native 執行分支缺少角色 grant 的授權缺口。來源風險須以 R2 負例及全回歸驗收；發布風險尚未驗收，不可用 schema 全綠取代身分、資料與執行層證據。
安全清單已規劃處理：身分注入維持 Gate／trusted identity；記憶篩選沿參數化 SQL與既有 limit；
factual ID 僅在同 identity 授權結果中揭露；validation error 不回顯原內容／秘密；CI 無 secrets 且只跑 sandbox。
密碼演算法、前端 token 儲存、新 HTTP 認證端點與 DB schema 皆不適用本案，不為此新增機制。

相容性預設保留工具名稱與原 action，新增參數可選（forget／save 本來就需要的條件除外）。
新 operation 不改內部直接 diag(action=...) 呼叫；internal export 與 alias 經明確路徑保留。
不把描述修正變成偷偷改行號、配額預設、通知目標或寫入政策；若要改產品語意，回到 DEV-A 修正範圍。

## 9. 回復與完成定義

核准計畫原bytes已保存；實作前HANDOVER整檔原bytes未封存（NOT_RETAINED），不重建冒稱原件；實作來源回復只針對最終精確候選差異，不覆蓋他人修改或管理 runtime 資料。Git／live回復不在本輪執行範圍。
未來 source 修復以核准 exact manifest 做普通 revert／反向補丁；不 force push、不重寫歷史、不刪除發布產物。
source rollback 不會撤銷已由產品完成的記憶封存或創建；本案測試只在 sandbox，因此不需要管理真資料。
live 映像／發布／切換未納入目前任務，若後續另有授權，仍需 fresh qualification、image digest、rollback seal 與受管升級驗收。
現在不操作目前 MODERN_ACTIVE 部署，不借用 P11／IDE 歷史結果聲稱本候選已上線。

實作完成必須同時具備：

- M1–M8、C1–C6 均有有效 RED→GREEN；research 三欄、handler 與 schema 保持未變；DEV-A R2 的通用 native 執行 grant 閘亦適用其既有角色權限，這項授權行為為明示修訂。
- query→forget／duplicate、mission review／evidence、quota／author 等有正式入口傳遞與必要 sandbox 效果證據。
- builtin＋Memory 最終工具清單正確，三 provider 公開欄位不遺失，可信身份與全部原權限邊界保持。
- 新工具／action／optional／nested 漏宣告的反例確實失敗；guard 已接入本機與 PR workflow，不把 branch protection 未核對寫成已強制。
- 必要完整 pytest、standalone smoke、lint、guard／audit 與文件檢查通過；原 FAIL、skip、warning、NOT_RUN、重用限制清楚保留。
- 精確 source candidate 與文件同步完成，DEV-B 依當次模式接受；Git／release／image digest／live 若未授權或未執行分別標示。

靜態守衛與 deterministic tests 能防已定義的合約漂移，不能保證所有模型都理解自然語言描述，也不能證明任意動態程式沒有未知缺漏。
核准後本輪完成定義為上述來源候選修復與必要完整驗收完成，最終由Root SUPERVISOR_ACCEPTANCE代審。不得將來源完成冒稱Git提交、CI遠端執行、release或live已完成。
