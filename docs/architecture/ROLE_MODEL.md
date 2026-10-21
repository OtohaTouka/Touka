# 角色權限模型（ROLE_MODEL）— SSOT

> Touka 身分與角色的**權威參照**。實作與此文件不符時以 `ToukaProxy/src/models/identity.py`
> 的 `AgentIdentity` 為準、並修正本文件。背景與演進見 [ADR-0141](adr/0141-role-plane-separation.md)
> （人類／Agent 權限面分離，2026-07-10 收官）。

## 1. 兩個互斥的角色權限面

角色分屬**兩個永不相交的值空間**，由 `AgentIdentity` 的兩個互斥欄位承載，`is_human` 標示主體類別：

| 面 | 欄位 | 值空間 | 常數 |
|----|------|--------|------|
| **人類** | `human_role` | `admin`／`normal` | `HUMAN_ROLES` |
| **Agent** | `agent_role` | `Owner`／`Base`／`Subagent`／`Debugger`／`System` | `AGENT_ROLES` |

- 人類使用者：`is_human=True`、`human_role` 有值、`agent_role=None`。
- Agent：`is_human=False`、`agent_role` 有值、`human_role=None`。
- **跨面純度不變式**（型別層保證，`_enforce_plane_purity` after-validator）：`human_role ∉ AGENT_ROLES`
  且 `agent_role ∉ HUMAN_ROLES`。違反即 `CrossPlaneRoleError`（422）。故 `agent_role == "admin"`
  **結構性不可能**——「人類角色被走私到 agent 身分上」在型別層被杜絕（這正是 ADR-0141 要根治的混淆）。

`HUMAN_ROLES` 與 `AGENT_ROLES` 兩集合互斥（`HUMAN_ROLES.isdisjoint(AGENT_ROLES)`）。

## 2. 角色語意

| 角色 | 面 | 語意 |
|------|----|------|
| `admin` | 人類 | 系統管理員：管理端點（`/v1/admin/*`、`/config/env`、`POST /identity`…）＋特權旁路。 |
| `normal` | 人類 | 一般使用者：無特權（過不了任何特權閘），只及自身擁有的 agent。 |
| `Owner` | Agent | 使用者主權 agent（靈魂）：對自身資源有主權。 |
| `Base` | Agent | 預設最小權限、沒有工具：DispatchAgent，以及所有非靈魂 ID（自訂模型、一般模型、群聊、任意字串）。 |
| `Subagent` | Agent | **in-process 子代理專用**：工具清單照抄已退役的 Guest（13 項，不能改靈魂、不能再派子代理）。**不可經 identity_map 指派**（見 §4）。 |
| `Debugger` | Agent | **DebugAgent 專用**（政策而非機制）：除錯/重置特權。 |
| `System` | Agent | 內部專用：背景掃描等全域旁路。**不可經 identity_map 指派**（見 §4）。 |

> 註（ADR-0245）：只有靈魂、DebugAgent、DispatchAgent 能有代理角色與能力——靈魂經類型規則為 Owner、DebugAgent 經覆寫為 Debugger、DispatchAgent 為 Base。其他 ID 的覆寫、類型規則與 `default_role` 一律略過，固定 Base、沒有能力（live 設定的舊值讀取時略過並記警告、不改檔）。X-Agent-ID 只能切進靈魂（見 §5）。Guest 已退役。

## 3. 讀角色的唯一正確方式

拆分收官後，`AgentIdentity` **沒有** 扁平 `.role` 屬性（P7 移除）。依用途二選一：

- **顯示面**（log／debug／`ctx["role"]`／序列化）→ `identity.display_role`
  （純 `@property` ＝ `human_role or agent_role or "Base"`；**非** `@computed_field`，不進
  `model_dump()`／openapi）。**不得**用 `display_role` 當權限閘。
- **權限面**（授權判定）→ **謂詞**（讀雙欄位、內建代打語意）：

| 謂詞 | 允許集合 | 定義 |
|------|----------|------|
| `is_admin_authority()` | 人類 `admin`（含 admin 代打） | `human_role == "admin"` |
| `is_privileged()` | `{admin, Owner, Debugger}` | `is_admin_authority() or agent_role in ("Owner","Debugger")` |
| `is_owner_or_admin()` | `{admin, Owner}`（排除 Debugger） | `is_admin_authority() or agent_role == "Owner"` |
| `is_admin_or_debugger()` | `{admin, Debugger}`（排除 Owner） | `is_admin_authority() or agent_role == "Debugger"` |

資源主權（跨 agent 存取、vault/工作區路徑）另由 `resource_manager` 的 `is_owner`／`get_vault_root`
把關——這些**保留 `is_human` 守衛**，故 admin 代打（`human_role="admin"`、`is_human=False`）
拿得到管理閘、但**拿不到**資源監獄旁路（見 §5）。

## 4. 建構身分

- **靜態、角色已知** → 直接指定欄位：`AgentIdentity(agent_id=..., agent_role="Subagent")` 或
  `AgentIdentity(agent_id=..., human_role="admin", is_human=True)`。
- **動態、角色值不確定**（如 `role=get_role_key(target)`）→ `AgentIdentity.for_role(agent_id, role, *, is_human=False, **kw)`：
  **依值路由**——`role ∈ HUMAN_ROLES` → `human_role`；`∈ AGENT_ROLES` → `agent_role`；未知值依
  `is_human` 落面；`None` → 兩欄位皆空（`display_role` fallback "Base"）。
- **admin 代打不變式**：`for_role(id, "admin", is_human=False)` 仍把 `"admin"` 落 `human_role`
  （依值路由、非依 `is_human`），確保 admin 恆走人類面、絕不誤入 agent 面而繞過 `is_admin_authority`。
- **禁止** `AgentIdentity(role=...)`（扁平相容建構已於 P7 移除；Guard #41 於 src 端 AST 禁止）。

**identity_map 白名單**：`IdentityMapUpdateRequest`（`POST /v1/identity`）只接受
`ASSIGNABLE_AGENT_ROLES = AGENT_ROLES − {Subagent, System}` ＝ `{Owner, Base, Debugger}`。
指派人類角色（admin/normal）或內部 `Subagent`／`System` → 422（堵「經 map 把 agent 提升成 admin」的後門）；
覆寫寫給靈魂、DebugAgent、DispatchAgent 以外的 ID 也回 422，body 帶 `rejected_agent_ids`（ADR-0245）。

## 5. Admin 身分代打（Identity Assumption）— Option A

管理員代打某 agent 時（ADR-0141 §8-1，擁有者拍板 **Option A：僅管理權**）：

- 代打身分 ＝ `human_role="admin"`、`agent_role=None`、`is_human=False`。
- **保管理權**（管理閘讀 `human_role`）；`display_role == "admin"`（單欄位、不模糊）。
- **不繼承**目標 agent 的軀體工具權限，也**不取得** `is_owner`／vault／工作區旁路
  （那些要求 `is_human`）——代打是「管理」而非「變成那個 agent」。

非 admin 擁有者代打 ＝ 成為目標軀體（`agent_role=目標角色`、`human_role=None`），無管理權。

X-Agent-ID 只能切進靈魂（ADR-0245）：擁有者或管理員切向自訂模型、群聊、DebugAgent、DispatchAgent 或任意 ID，一律 403 `AUTH_PERMISSION_DENIED`。

## 5.1 Agent 工具／技能權限表示法（ADR-0196）

Agent 權限由 `PermissionService.resolve_grants(agent_role)` 唯一解析成不可變
`RoleGrants`：原生工具與技能使用精確名稱的 `NameGrant`，MCP 使用複用
`mcp_tool_policy` 比對規則的 `McpGrant`。

- allow 陣列的 `"*"` → `open_ended=True`，代表含未來項目的開放式授權。
- deny 陣列的 `"*"` → `deny_all=True`；拒絕永遠優先。
- 繼承只累加 allow；父角色 deny 不傳播，子角色自己的 deny 照常生效。
- 跨角色禁令只由 `PermissionsConfig.global_denied_tools`／
  `global_denied_mcp_tools` 表達，並約束所有角色（含 Owner）。
- Owner 權限完全來自設定資料，不得以 `role_key == "Owner"` 硬編旁路。

設定檔仍使用既有 `list[str]` 角色欄位；`Grant` 是載入後的內部表示，不是另一套 wire 模型。
安全設定頁的 `RbacMatrix` 直接對映此 wire：開放式 toggle 開啟時寫 `["*"]`，關閉時
寫 `[]` 並開放明確清單；三個 allow 軸共用同一互動元件，避免 UI 表示法分岔。

## 5.2 權限設定載入與故障語意

`permissions.json` 的執行期模型與 fallback 都由 `PermissionsConfig` 驗證，但載入行為
依生命週期分流：

- 檔案不存在、空白或 `{}` 是合法 bootstrap，使用
  `src/utils/permission_defaults.py` 的最小 fallback：只保留 Owner 修復通道，其餘角色
  全封閉。
- 啟動時既有非空設定解析失敗，重拋原始例外並拒絕啟動，不以 fallback 掩蓋。
- 熱重載解析失敗，保留 last-known-good、記 `AUDIT`，健康端點標示 degraded；
  下一次成功重載才原子替換並自動復原。
- Guard #32 同時掃生產、`.example` 與 fallback；`PermissionService.resolve_grants()`
  是執行期決策 SSOT。

RBAC 與 ABAC 不混合：`permissions.json` 不儲存 `auth_level`。Manual／Auto／YOLO
（ADR-0246；舊值 ReadOnly、SafeAuto 讀成 Manual，FullYOLO 讀成 YOLO）是 per-session 的
HITL 輸入（請求參數不是來源），由 `auth_policies.json` 與程式常數底線
（`utils/hard_floor.py`）處理。誰能把對話存成 Auto／YOLO 看人類請求者：
`is_admin_authority()` 或 `owned_agents` 含該靈魂，不看代理角色。

## 6. 防復活守衛

**Guard #41**（`scripts/guard_role_plane.py`，`guard_all` 第 41 道）＝ AST 永久禁令：src
（ToukaProxy/ToukaMemory）出現 identity-ish 的 `.role` 屬性存取，或 `AgentIdentity(role=...)`
建構 → FAIL。豁免 `identity.py`（SSOT 模型本體）、不掃 tests（測試可特徵化拆除後行為）。

## 7. 反模式（不要這樣做）

- ❌ `identity.role`（已移除、AttributeError；Guard #41 擋）→ ✅ `display_role`（顯示）或謂詞（權限）。
- ❌ `if identity.display_role == "admin"` 當授權閘 → ✅ `if identity.is_admin_authority()`。
- ❌ `AgentIdentity(role="Owner")` → ✅ `AgentIdentity(agent_id=..., agent_role="Owner")` 或 `for_role(...)`。
- ❌ 比對 `identity.role in {"Owner","admin",...}` 混合集合 → ✅ 對應謂詞（§3 表）。

## 8. 相關

- [ADR-0141](adr/0141-role-plane-separation.md)：接受決策、六拍板點、P0–P7 全階段成果。
- [ADR-0196](adr/0196-permission-representation-convergence.md)：Grant 唯一解析器、
  deny self-only／全域地板，以及權限設定 fail-fast／last-known-good 生命週期。
- [ADR-0195](adr/0195-mcp-per-tool-governance-two-axis.md)：MCP per-installation 與
  per-role 兩軸；角色 deny／Owner／載入語意由 ADR-0196 修訂。
- [遷移棘輪基線](proposals/role-plane-migration-baseline.md)：P0 台帳＋Guard #41 演進 28→0→AST。
- [security_auth_guide](../guides/security_auth_guide.md) §2.2/2.3；[SECURITY.md](../../SECURITY.md) DA2/DA12。
- 模型：`ToukaProxy/src/models/identity.py`（`AgentIdentity`、兩集合常數、謂詞、`for_role`、純度 validator）。
