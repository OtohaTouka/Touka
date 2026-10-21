# ADR-0228：原生工具公開輸入合約與執行 grant

- 日期：2026-09-15
- 狀態：DEV-A Owner approved；R2／R3 SUPERVISOR_ACCEPTANCE；P1–P6來源計畫完成；**DEV-B_ACCEPTED／SUPERVISOR_ACCEPTANCE**，見Root DEV-B 驗收原件
- 影響：ToukaMemory／ToukaProxy native tools、Guard #45、PR source checks
- 前置：[ADR-0119](0119-unified-tool-interface-redesign.md)、[完整計畫](../proposals/native-tool-contract-completeness-plan.md)

## 問題

既有實作的 8 組／12 個非 alias 欄位與 manage_dups action 未完整廣告；部分條件描述不符實作。
手寫 schema、kwargs 讀取及 provider 轉換分離，新增選填或 nested 欄位可能無聲消失。
另在三 provider 共用的 ToolExecutor 發現原生執行只評估 PaC，未驗 RoleGrants；模型猜名稱可繞過廣告面。

## 決策

1. Memory 公開輸入 SSOT 留在 `memory_server/tools/contracts.py`；Proxy 其他六工具在 `src/tools/contracts.py`。
   使用既有 Pydantic，不引入框架或 lock 依賴。七工具共用模型生成 schema 及 public 入口驗證。
   typed 範圍：memory_tools、mission_tools、evolution_implement_and_verify、create_skill、ide_open_diff、edit_soul、dispatch_subagent_group。
2. Memory integration `**kwargs` 橋接維持；domain fields／legacy inputs／internal fields／trusted runtime 分類分離。
   runtime 欄位 SSOT 仍是 `EXECUTION_CONTEXT_KWARG_NAMES`。模型 reserved 值在 project fallback 前移除，trusted context 蓋過模型。
   不套用外部 MCP、GDPR export_facts 或任意 internal 入口。research 三欄位與 handler/schema 不改。
3. 公開輸入拒絕未知欄、錯型別、非法 canonical 日期、必要 action 缺參；這是明示行為變更。
   ValidationError 只回既有 build_tool_error 的安全 field/reason，不含原 input、secret、未知 key 或 traceback。
   Mission 單複數 artifact 欄位各自保真，公開 plural 與 legacy singular 都沿原 Task 路徑 validator。
4. memory emotions 是自由 `list[str]`，不加情緒 enum。date_start/end 沿日期界線；is_recent 最近七天，
   與 start 同給取較晚值；end 含當日末。「最近」query 只切檢索策略，不等於七天。
   query 顯示 exact `id`／semantic `fact_id` 的 canonical factual ID，forget使用同ID與既有identity predicate。
5. manage_dups 外 action 不動，內操作用 `operation=scan|resolve`，resolve 必填 target_id。
   保守以外 action 全體 dangerous 分類，沿既有 ReadOnly／Manual／SafeAuto／FullYOLO，無新 ABAC。
6. quota 原樣傳到既有 verifier **只 clamp 一次**：只能縮小 SSOT 上限，bool／無效值 fallback；
   0.5 沿既有 int 轉換為 0。不涵蓋 venv/install/deploy。author 只metadata，空值沿agentID。
   ide diff session由系統注入；soul 0-based/min50/protected delete語意不改；group最多8，同時4。
7. R2 原生執行閘依實際 builtin 優先、其次 Memory 分支，使用 verified identity 的 RoleGrants native name grant；
   在 PaC/HITL 前拒絕無權限名稱。別名各自核對自身 grant，沒有 role/grant 不放行，schema 可見不等於授權。
   此通用安全變更包含 research 既有角色授權執法；不改 research 參數／handler、MCP/plugin授權模型或private角色設定。

8. R3 修正既有Memory bootstrap忽略顯式env來源：非空TOUKA_ENV_FILE只載入該Path，缺席也不fallback；未指定/空字串保留既有搜尋。
   原Q48 audit/source-origin/缺Proxy負例保持；4F/3P→105P focused。舊full父程序dotenv I/O NOT_PROVEN，不能假稱沒有host探測；新驗證使用owned明示來源。

## 守衛與證據分工

Guard #45 `scripts/guard_native_tool_contracts.py` 純 AST讀來源，不 import Memory（父 package可能 bootstrap）。
`native_tool_contract_baseline.json` 僅記既有工具分類、handler/import binding及有限consumer形狀，**不複製公開 schema**。
目前有限 29 modules／149 函式（含私有helper）可能因合法重構紅；須審查更新該 metadata，不能盲目重新拍照消紅。
只接受單一 literal registry；module-level update／subscript與無法分類動態形狀拒絕。
有限手動action selectors與Memory dispatch/action/risk對齊；不聲稱可推導任意動態Python。

runtime pytest 在 conftest sandbox 讀真 registry、重複名稱優先handler、ToolLayer 最終 schema、OpenAI/Anthropic/Gemini converter。
local refs先展開再投影；核nested properties、items型別與required，不要求Gemini不支援的combinators。
每個 public field path 必有非預設值 case 到實際adapter/backend觀察；新增optional/nested但無案例會FAIL。
query→forget與Mission保存回讀使用真sandbox儲存；危險工具的smoke維持WIRED_ONLY。
full-source變異fixture覆蓋newtool／kwargs／action／nested／dynamic／binding／legacy回流；provider掉欄與backend丟值亦必紅。

PR source workflow 最小 contents:read、immutable actions pin、固定uv0.12.1＋Python3.12、locked workspace CPUindex；
沒有 secrets、pull_request_target、token寫權或新套件。CI來源驗證不等於 hosted 執行；required checks是否啟用 NOT_VERIFIED。
依賴audit仍由既有全guard流程；此次可引用本輪相同inputs的root原件 REUSED_NOT_FRESH，不宣稱新來源已遠端發行。

## 風險、相容與回復

風險 HIGH：新揭露記憶寫入／任務證據與身份授權執行面。既有使用者無需legacy產品相容，不代表可無聲破壞既有internal橋接。
來源回復以本候選精確path patch／正常revert進行；沒有live切換、沒有data回復或清理步驟。
本機自動測試只sandbox/mock；此ADR不授權Git、release、image、live或真data操作。
完成需來源精確manifest、完整pytest／standalone smoke／Ruff／SSOT／全部45guard覆蓋與docs接受。
本候選full R3為9604P／11skip／5warnings；兩個HB條件skip另synthetic原模組11P補驗。44候選／2422inputs無漂移；fresh非audit43/43、smoke24/24。
文件結果closure以doc-only差異＋MkDocs補驗，既有測試結果對最終文件快照標REUSED_NOT_FRESH；不稱全測後又改產品。
不保證模型會理解每個描述，也不以歷史 P11／IDE PASS 套用本候選。
