# ADR-0089: 唯讀工具全權限免確認（Read-Only Tool Auto-Allow）

**狀態 (Status):** ✅ 已接受（[ADR-0143](0143-hitl-unification.md) 汰換具名機制）
**日期 (Date):** 2026-06-13

> 🗑️ **具名機制已被 [ADR-0143](0143-hitl-unification.md)（HITL 統一，2026-07-11）汰換**：`AuthPoliciesConfig.read_only_tools: list[str]` 欄位、`config/auth_policies.json` 的 `read_only_tools` 鍵、以及 Guard #32 的 `read_only_tools ⊆ tool_registry` 驗證，皆已改為 `tool_risk: ToolRiskConfig`（`read_only`/`safe`/`dangerous` 三類分類表；Guard #32 改驗 tool_risk 分類雙向對齊）。**「read-only ⇒ 全等級（含 ReadOnly）ALLOW、免 HITL」的核心語意由 `tool_risk` 的 `read_only` 類承接不變**（`safety.py` `evaluate()` `if risk == "read_only": return ALLOW`）。
**決策者 (Deciders):** 維護者

> 🔧 **「全等級（含 ReadOnly）ALLOW」已被 [ADR-0246](0246-model-layer-stage-2b-2c.md)（Manual／Auto／YOLO 三模式）修訂**：ReadOnly 已退役，read_only 類改為三種模式都 ALLOW、免 HITL，語意不變。

## 上下文 (Context)

網路搜尋（`research` 工具）在現行安全策略下體驗有摩擦：`AuthPoliciesConfig.evaluate()`
對非終端工具以「工具名」為 `resource`（`get_execution_strategy`：`args.get("command", tool_name)`），
而 `safe_whitelist` 清一色是 shell 指令樣式（`^ls`、`^cat`…），工具名永遠配不上 → 故
**ReadOnly=DENY、Manual=CONFIRM、SafeAuto=CONFIRM、只有 FullYOLO=ALLOW**。等於唯讀的網路
搜尋在預設等級下每次都要人工確認、ReadOnly 下還直接被擋。

維護者要求：**唯讀（side-effect-free）動作（如網路搜尋）在任何 auth_level 一律自動執行、免確認。**
此為 Web Search 改進 Phase A（最小、自足、立即改善體驗）。

## 決策方案 (Decision)

新增「唯讀工具放行」概念，以 Policy-as-Code 模型化（不散落硬編碼）：

- **模型**：`models/safety.py` 的 `AuthPoliciesConfig` 加 `read_only_tools: list[str]`。
- **判定**：`evaluate()` 在**黑名單檢查之後、等級矩陣之前**插入——`if resource in self.read_only_tools: return ALLOW`。一律 ALLOW、不分等級、免 HITL。
- **設定 SSOT**：`config/auth_policies.json`（＋`.example`）加 `"read_only_tools": ["research"]`；in-code fallback `DEFAULT_AUTH_POLICIES` 同步加（降級模式不失效）。
- **範圍（v1）**：只收 `research`（純唯讀、你的主訴求）。混合型工具（`vault_tools`/`memory_tools` 的讀 action）因 `resource` 為工具名、無法區分讀/寫，需 action 級策略，列後續（A.2）。
- **守衛**：擴充 Guard #32（`guard_permissions_registry.py`，ADR-0088）——額外驗證 `auth_policies.json` 的 `read_only_tools` 每個名稱 ∈ `tool_registry` 真名，防唯讀清單打錯字而靜默失效。

### 兩道閘門一致放行
- **Gate 1（schema 注入）**：原本 ReadOnly 下 `evaluate` 回 DENY → research schema 被跳過、模型看不到；現 ALLOW → research 在所有等級皆注入、模型看得到。
- **Gate 2（執行）**：所有等級 ALLOW → 不跳確認、ReadOnly 不擋。

### 安全論證
- 黑名單仍**最先**檢查（defense-in-depth）——即使被標唯讀，命中黑名單仍 DENY。
- Gate 1 的 RBAC（`get_allowed_tools`/`allowed_tools`）仍決定該角色「是否有」research 工具；本變更只放寬「有此工具者的 auth_level 確認」，不擴張「誰有此工具」。
- ReadOnly 放行唯讀其實**更符合語義**（ReadOnly＝禁寫入與執行；唯讀搜尋兩者皆非）。

## 後果與影響 (Consequences)

- 網路搜尋在所有等級自動執行、免確認；體驗顯著改善。
- **取捨（記錄）**：免確認＝可無限自動搜尋——① API 成本/額度（建議搭 Phase C 的 fallback 鏈＋未來搜尋次數上限）；② 隱私外送（查詢送往 Tavily/Google——建議搭 Phase C 的自架 SearXNG 使查詢不外洩）。
- **遷移**：bootstrap 只還原「缺檔」不自動遷移既有檔；既有部署需手動在 `auth_policies.json` 補 `read_only_tools`（live 檔已更新；`DEFAULT_AUTH_POLICIES` fallback 涵蓋降級）。
- 無 schema/端點變動（免 openapi/api.ts 重生）；guard 仍 32 道（擴充 #32，非新增）。

## Action Items
1. [x] `safety.py`：`read_only_tools` 欄位 ＋ `evaluate()` 黑名單後放行。
2. [x] `auth_policies.json`＋`.example`＋`DEFAULT_AUTH_POLICIES` 加 `read_only_tools: ["research"]`。
3. [x] 擴充 Guard #32 驗證 `read_only_tools` ⊆ `tool_registry`。
4. [x] TDD：`test_security_policies.py`（research 四等級 ALLOW、非唯讀工具仍 CONFIRM/DENY）＋ `test_guard_permissions_registry.py`（read_only_tools 打錯字→違規）。
5. [x] ADR-0089 ＋ `adr/README`；`README_HANDOVER`／`CHANGELOG`／`tests/README` 同步；guard 32/32＋全量測試綠。
