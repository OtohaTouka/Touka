# ADR-0061: 技能 MCP 伺服器收集、工具可見性與 HubSkill 型別完整性

> **現況**：HubSkill 型別與 Skill Hub 部分已退役（ADR-0077）。

## 狀態

✅ 已接受（子系統退役 by [ADR-0077](0077-retire-skill-hub-consolidate-into-catalog.md)：Decision(C) 對齊的 HubSkill 型別與整個 persona Skill Hub 子系統已退役（types.ts 無 HubSkill、skill_hub_service.py 移除、TDD 測試檔亦刪）；skill_required_mcp/工具可見性其餘決策仍在）

## 日期

2026-05-30

> 📝 **回溯補寫（2026-05-31）**：本決策與實作已於 Skill 系統標準化期間完成並有對應 TDD 測試（`tests/unit/test_skill_layer_mcp.py`、`test_tool_layer_skill_mcp.py`、`test_hub_skill_type_safety.py`）。當時編號 0061 已配給此決策但未撰寫書面 ADR，此文件補齊紀錄。

## 背景

技能除了 `required_tools` 外，也可宣告所需的 MCP 伺服器（`mcp_servers`）。啟用技能時，這些 MCP server 必須被連線、其工具必須對 Agent 可見。此外前端 `HubSkill` 型別與後端 `SkillHubModel` 存在欄位漂移，需一併對齊。

## 問題

1. `SkillLayer` 已收集授權技能的工具（ADR-0060），但未收集其 `mcp_servers` → 技能宣告的 MCP server 不會被連線。
2. Dispatcher 模式下，工具可見性只看 `active_session_mcps`；技能所需 server 不在其中 → 技能的 MCP 工具被隱藏。
3. `HubSkill`（前端型別）與後端漂移：`rating` 前端 `string` / 後端 `float`；缺 `mcp_servers` 欄位導致 `as any` 強制轉型；`get_enabled_skills()` 為無呼叫者的死碼。

## 決策

### (A) SkillLayer 收集 MCP（`middleware/layers/skill.py`）

對每個**已授權**技能（沿用 ADR-0060 的 `authorized_skill_ids`），將其 `mcp_servers` 收集進 `ctx["skill_required_mcp"]`，以 `dict.fromkeys` 去重；未授權技能的 server 被 RBAC 隔離；技能無 server 時不設定該 ctx key。

### (B) ToolLayer 連線與可見性（`middleware/layers/tool.py`）

讀 `ctx["skill_required_mcp"]`，對每個「存在於 registry 且通過 `is_mcp_server_allowed`」的 server 呼叫 `ensure_connected()`；未授權或不在 registry 的 server 靜默跳過。工具可見性條件改為 `active_session_mcps OR skill_required_mcp`，使 dispatcher 模式下技能 server 的工具也可見。

### (C) HubSkill 型別對齊（`ToukaChat/src/lib/types.ts`、`hooks/settings/useSkillSettings.ts`、`ToukaProxy/src/services/skill_service.py`）

`HubSkill.rating?: number`（對齊後端 `float`）；新增 `HubSkill.mcp_servers?: string[]` 並移除 `installHubSkill` 的 `as any` 轉型；刪除死碼 `get_enabled_skills()`（滿足 Dead Code Guard，ADR 守衛 #18）。

## 設計決策

### 為何工具可見性是「聯集」而非「取代」

`active_session_mcps`（使用者顯式連線的 server）與 `skill_required_mcp`（技能隱式需要的 server）是兩種正交來源。取聯集確保「使用者連的」與「技能帶的」都可見，互不覆蓋。RBAC（`is_mcp_server_allowed`）仍是兩者共同的硬閘門。

## 後果

### 正面

- 技能可自帶 MCP 連線需求，啟用即連線、工具即可見（含 dispatcher 模式）。
- 前後端 `HubSkill` 契約一致，消除 `as any` 與一處死碼。

### 負面

- 工具可見性條件變複雜（兩個來源的聯集），需測試覆蓋 dispatcher × skill_required_mcp 組合。

## 相關 ADR

- [ADR-0060](0060-skill-layer-rbac-tool-isolation.md) — 共用的 `authorized_skill_ids` RBAC 過濾
- [ADR-0059](0059-skill-header-injection-protocol.md) — 技能來源標頭
- [ADR-0063](0063-skill-hub-external-sync.md) — `HubSkill` 型別所屬的 Skill Hub
- [ADR-0066](0066-skill-system-anthropic-standard-migration.md) — 標準遷移後 `mcp_servers` 改由 `.mcp.json` keys 推導
