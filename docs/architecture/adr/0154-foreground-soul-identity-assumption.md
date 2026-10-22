# ADR-0154: 前景對話身分代打——擁有靈魂的人類以「靈魂身分」執行（修身分裂腦）

## 狀態

✅ 已接受（維護者決定採 Option A：前景對話以靈魂身分執行；源於 Sumi 對話「看不到工具」＋幽靈 `custom/{human_uuid}/drive` 診斷）

> 修訂註記：ADR-0245 修補（2026-10-12）把「非擁有者、非 admin 的人類指定靈魂」從「不代打（維持現況）」改為 MonitorLayer 直接 403 `AUTH_PERMISSION_DENIED`；擁有者與 admin 的前景代打不變。

## 日期

2026-07-13

## 背景

前景（UI 對話）跟自己擁有的靈魂（如 Sumi）聊天時，`MonitorLayer` 解析出 target 靈魂後
**只把 `ctx["agent_id"]` 字串改成靈魂 id，`ctx["identity"]` 物件仍是人類請求者**
（`agent_id`＝人類 user.id、`is_human=True`、`agent_role=None`）。下游全部消費這個人類身分物件
＝**身分裂腦（字串 vs 物件）**，三個症狀一源：

1. **工具看不到**（Sumi 對話實錄的症狀）：`ToolLayer` 以 `permission_service.get_allowed_tools(identity)`
   組裝工具集，而該函式讀 `identity.agent_role`——人類為 `None` → 回受限集，靈魂的
   Owner=`["*"]` 全開從未生效。
2. **記憶/檔案分家**：`resource_manager.get_vault_path("personal", identity)` 依
   `soul_service.is_toukasoul(identity)` 分流——人類（`agent_role=None`）→ 落到
   `custom/{human_uuid}/drive`，與**心跳**自主跑（`identity_service.get_identity(soul)`→Owner）的
   `personal_drive/{soul}` 分家 → 前景 vs 心跳記憶不連續（實機出現幽靈
   `data/custom/2a72059c/drive/missions`）。
3. **payload 歸屬錯**：`proxy_service` 的 payload logger 記 `identity.agent_id`＝人類 UUID（非靈魂）。

系統本已有兩處把「執行身分」正確設為靈魂——**心跳**（`identity_service.get_identity(agent_id)`）與
**X-Agent-ID 代打**（`identity_router` :98-125，人類對其擁有的 agent 假身）。唯獨**前景聊天兩者都沒走**
（不送 X-Agent-ID、靈魂只在 `request.model` 名裡）。

**根因＝MonitorLayer 只改字串未改物件。** 這是 ADR-0147 警告的 user.id↔agent_id 撞名於 runtime 現形的
姊妹問題（此處非撞名，而是「請求者身分」未切換為「執行身分」）。

## 決策

維護者於設計岔路決定採 **Option A：前景對話以靈魂身分執行**（明示接受「在該對話中以 Owner 而非 admin
行事」）。target 解析為「人類擁有（或人類 admin）的靈魂」時，把 `ctx["identity"]` 換成該靈魂身分
（= 心跳 `get_identity` 同源的 Owner 身分），使工具/記憶/vault/payload 全部一致以靈魂身分執行。
**人類請求者的角色僅作 Gate-1 授權**（擁有該靈魂或人類 admin），不參與執行身分。

## 實作

`middleware/layers/monitor.py` 新增 `MonitorLayer._maybe_assume_soul_identity(ctx, target_agent_id, is_toukasoul)`，
於 `ctx["agent_id"] = target_agent_id` 之後呼叫。代打閘（全部成立才換）：

- `is_toukasoul`（僅靈魂；custom/Guest 模型不代打）；
- `caller = ctx["identity"]` 為**人類**（`is_human`；心跳/既有代打的 caller 已是靈魂身分
  `is_human=False`→不重複處理；Guest/未驗證 `None`→不代打）；
- `caller.agent_id != target_agent_id`；
- `target_agent_id in caller.owned_agents` **或** `caller.is_admin_authority()`（**擁有權/admin 閘**，防越權代打他人靈魂）。

換身分：`role, caps = agent_service.resolve_role_and_caps(target)` →
`AgentIdentity.for_role(target, role, capabilities=caps)`（鏡像 `identity_service.get_identity` 的
`resolve_role_and_caps` SSOT ＋ `for_role` 依值路由；以已匯入的 `agent_service` 直建，避免對 service 層的
額外模組相依＝無循環匯入、符禁令 33 頂層匯入）。同步 `ctx["role"]=display_role`、`ctx["capabilities"]`。

**不動 dispatcher／session 持久化**：`owner_user_id` 為 INSERT-only（`async_db._upsert_session_unlocked`
的 `owner_user_id` 不進 `ON CONFLICT DO UPDATE SET`），且 `_persist_assistant_turn` 僅於 session 已存在時
UPDATE（`get_session_by_id` 守衛），故 session 的人類歸屬由**前端以人類 token 的 INSERT** 決定、re-bind
（`is_human=False`）**永不改動既有 owner**。

## 後果

- **正面（三症一併修）**：靈魂在前景對話看得到自己的工具（Owner=`["*"]`）；記憶/檔案統一落 `personal_drive/{soul}`
  （與心跳連續）；payload 正確歸屬靈魂。前景 ≡ 心跳的執行身分。
- **admin 語意（刻意，與 ADR-0141 §8-1 差異）**：admin 驅動靈魂時**變成 Owner（`agent_role=Owner`、失去
  admin 權）**，屬**去權（de-escalation，fail-closed）**。這與 **X-Agent-ID 代打**（ADR-0141 §8-1 Option A：
  admin 保 `human_role=admin`、`agent_role=None`）**刻意不同**——因兩者意圖不同：X-Agent-ID 代打是**管理/除錯**
  （保管理權），前景對話是**與靈魂交談**（靈魂以自身行事）。且對 admin 而言**必須**變 Owner 才能讓 vault
  統一到 `personal_drive/{soul}`（保 admin 權則 `agent_role=None`→`is_toukasoul` False→vault 仍分家至
  `custom/`）＝Option A 是達成「記憶統一」目標的**必要條件**。聊天工具皆 agent 工具（記憶/vault/改 soul），
  `admin` 僅管 `/v1/admin/*` 管理 API，故對話中無實質能力損失。
- **安全（對抗式審查 SAFE）**：擁有權閘靠**簽章 JWT 派生**的 `caller.owned_agents`/`human_role`（`verify_token`
  由 DB user 記錄組裝、header 不可膨脹）；`target_agent_id` 雖 header/model 可控，但不擁有即早退不代打
  （非 admin 且非擁有者＝IDOR 關閉）。auth_level 授權在 re-bind **之前**以人類身分判定（FullYOLO/SafeAuto
  不受換身分回溯影響、decision table 只讀 auth_level+tool risk）。HITL 審批為獨立人類認證請求
  （`tools_approval` 硬要 `is_human`），re-bind 不影響。與既有 X-Agent-ID 代打**產生相同 Owner 身分**＝不授予
  該路徑未有的能力。
- **相容**：非擁有者非 admin 的人類、Guest、心跳、custom 模型一律**不代打**（維持現況）。既有測試零回歸
  （pytest 3440、guard_all 41/41）。
- **待驗（人工）**：行為效益（Sumi 前景看得到工具、記憶與心跳連續）需在實際環境對話確認
  （需真模型呼叫、API 費）；本 ADR 僅以單元＋全套＋守衛＋對抗審查在程式層驗證。

## 相關

- [ADR-0141](0141-role-plane-separation.md)（角色權限面分離；X-Agent-ID 代打 §8-1 Option A＝本案前景代打的姊妹
  路徑，admin 語意刻意分歧見上）
- [ADR-0147](0147-soul-role-type-binding.md)（靈魂→角色類型綁定；`resolve_role_and_caps` SSOT＝本案換身分
  的角色來源；其警告的 user.id↔agent_id 撞名為同片土壤）
- 診斷：Sumi 對話側邊欄消失案的 IDENTITY-LEAK 支線（payload `agent_id=2a72059c`＋幽靈 `custom/2a72059c/drive`
  ＝本案症狀的實機證據）
