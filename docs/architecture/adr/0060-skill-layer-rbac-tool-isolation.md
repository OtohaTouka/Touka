# ADR-0060: SkillLayer RBAC 工具授權隔離

> **現況**：dispatcher 常駐、免 RBAC 改成條件注入（ADR-0148 反轉）。

## 狀態

✅ 已接受

> 🗑️ **決策「system-skill-dispatcher 常駐注入（免 RBAC）」已被 [ADR-0148](0148-skill-advertising-rbac-and-install-approval.md)（技能廣告 RBAC 過濾）反轉**：現行 `middleware/layers/skill.py` 以 `usable_skill_ids`（逐一過 `permission_service.can_use_skill`）條件注入 dispatcher，`has_dispatcher = bool(usable_skill_ids)`；Base/Guest 若 `allowed_skills=[]` 則**無 dispatcher、無 skill_registry**（廣告面＝授權面）。本 ADR 的核心工具隔離決策（只 iterate `authorized_skill_ids`、未授權技能之工具完全隔離）仍有效。

## 日期

2026-05-30

> 📝 **回溯補寫（2026-05-31）**：本決策與實作已於 Skill 系統標準化期間完成並有對應 TDD 測試（`tests/unit/test_skill_layer_rbac.py`）。當時編號 0060 已配給此決策但未撰寫書面 ADR，此文件補齊紀錄。

## 背景

`SkillLayer`（middleware）會依 `x-touka-skills` 標頭（ADR-0059）注入技能的 system_prompt 與 `required_tools`。該標頭內容完全由前端（使用者）提供，屬於**不可信輸入**。

## 問題

`SkillLayer` 的工具收集步驟原本直接迭代 `skill_ids`（未過濾的原始清單），未先經 RBAC（`permission_service.can_use_skill`）檢查。攻擊者可在 `x-touka-skills` 標頭塞入「未授權技能」的 ID，使其 `required_tools` 被加入工具白名單 → **RBAC 授權繞過**（拿到本不該擁有的工具）。

## 決策

工具收集只迭代 `authorized_skill_ids`（經 `can_use_skill` 過濾後的清單），而非原始 `skill_ids`：

- **修復前（Bug）**：`for sid in skill_ids:` → 未授權技能的 `required_tools` 仍被收集。
- **修復後（Fix）**：`for sid in authorized_skill_ids:` → 僅已授權技能的工具進入 `ctx["skill_required_tools"]`。

未授權技能即使出現在標頭中，其工具也被完全隔離。`system-skill-dispatcher` 為常駐注入（免 RBAC，僅列出可用技能清單供 Agent 自選，見 ADR-0066 Phase 3 Pull 模式）。

## 設計決策

### 逐技能而非逐請求的 RBAC

一個請求可同時帶多個技能，其中部分授權、部分未授權；只有授權的那些才貢獻工具。判定下沉至 `permission_service.can_use_skill(identity, skill_id)`（Policy as Code，ADR-0021 ABAC 體系），`SkillLayer` 不自行硬編碼判斷。

### 與 MCP 收集共用同一過濾路徑

ADR-0061 的 `mcp_servers` 收集沿用同一份 `authorized_skill_ids`，確保工具與 MCP 兩條路徑的授權邊界一致，不會出現「工具被擋但 MCP 漏出」的不對稱。

## 後果

### 正面

- 關閉了 header-injection 造成的工具越權面。
- 與 ADR-0061 的 MCP 收集共用一致的授權路徑。

### 負面

- 每個標頭中的技能都需一次 `can_use_skill` 呼叫（技能數通常個位數，成本可忽略）。

## 相關 ADR

- [ADR-0059](0059-skill-header-injection-protocol.md) — `x-touka-skills` 標頭（不可信輸入來源）
- [ADR-0061](0061-skill-mcp-server-collection.md) — 同一 RBAC 過濾套用於 MCP 伺服器收集
- [ADR-0021](0021-abac-security-levels-and-hitl-matrix.md) — ABAC 安全等級與權限體系
- [ADR-0066](0066-skill-system-anthropic-standard-migration.md) — Skill 系統標準遷移
- [ADR-0148](0148-skill-advertising-rbac-and-install-approval.md) — 技能廣告 RBAC 過濾（**反轉本 ADR「dispatcher 常駐免 RBAC」：改依 `can_use_skill` 條件注入，空清單無 dispatcher**）
