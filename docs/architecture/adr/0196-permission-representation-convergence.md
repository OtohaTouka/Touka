# ADR-0196：權限表示法收斂與安全設定生命週期

- **狀態**：Accepted
- **日期**：2026-07-27
- **相關**：[ADR-0141](0141-role-plane-separation.md)（人類／Agent 角色平面）、
  [ADR-0195](0195-mcp-per-tool-governance-two-axis.md)（MCP 供給面與身分面兩軸）、
  [ADR-0143](0143-hitl-unification.md)（ABAC／HITL 與 RBAC 分工）
- **提案**：[權限表示法收斂](../proposals/permission-representation-convergence.md)

## 脈絡

角色設定把 `"*"` 塞在 `list[str]`：allow 面把它解讀為「含未來項目的開放式授權」，
deny 面卻曾把它當一般元素。更危險的是，工具廣告、工具執行、技能與 MCP 各自解析
同一份設定；Owner 另有硬編旁路，造成兩個工具閘門能對同一角色給出相反答案。

P0 另揭露測試沙盒長期未播種生產 `permissions.json`，多數測試實際使用 Python
fallback。該 fallback 含已退役工具名、對低權角色開任意 MCP，既未受 Guard #32
掃描，也沒有一致的安全方向。舊 `_load_config` 更會在任何解析錯誤時靜默換成 fallback，
使損壞設定可能抹除使用者明示的 deny，形成 fail-unpredictable。

## 決策

### 1. Wire 維持 `list[str]`，執行期升成不可變 Grant

採用相容選項 α：`permissions.json` 的角色欄位維持既有 `list[str]`；載入後由
`NameGrant`／`McpGrant`／`RoleGrants` 表達有效權限。

- allow 的 `"*"` 只在 `Grant.from_lists()` 解析為 `open_ended=True`。
- deny 的 `"*"` 解析為 `deny_all=True`；拒絕永遠優先。
- `NameGrant` 精確比對原生工具與技能；`McpGrant` 複用
  `mcp_tool_policy` 的 MCP reference 比對 SSOT。
- Grant 是執行期值物件，不是第二套 wire／Pydantic 模型。

### 2. `resolve_grants` 是唯一角色權限解析器

`PermissionService.resolve_grants(agent_role)` 唯一負責角色繼承、角色本地 deny 與
全域拒絕地板。工具廣告與執行、技能、MCP 掛載與單一工具判定都委派其輸出的
`RoleGrants`，不得自行重刻 `"*"` 或角色特判。

Owner 的開放式能力完全來自 `permissions.json`，不再有
`role_key == "Owner"` 旁路。這使設定能實際縮窄 Owner，並讓兩個工具閘門共享同一個
`allows()` 答案。

### 3. 繼承只累加 allow；deny self-only；全域地板約束所有角色

角色能力鏈只累加父角色 allow。角色自己的 `denied_tools`／`denied_mcp_tools`
只對該角色生效，不沿繼承鏈傳到更高權限角色。

跨角色禁令必須明示於頂層：

- `global_denied_tools`
- `global_denied_mcp_tools`

兩個地板只能收窄，對所有角色（含 Owner）生效。角色本地 deny 的裸 `"*"` 因此可安全
表示該角色 deny-all，不再因繼承聯集誤殺整條角色鏈。

### 4. 前端開放式授權必須可雙向切換

`RbacMatrix` 的原生工具、MCP 與技能共用同一個 open-ended 互動：

- 開啟 toggle → wire 寫 `["*"]`
- 關閉 toggle → wire 寫 `[]`，再允許編輯明確清單

不得讓 `"*"` 使清單永久 disabled 而無解除路徑；設定更新採不可變方式，避免不同角色
或不同權限軸互相污染。

### 5. Fallback 是最小修復通道，且納入 Guard #32

fallback 原始資料的 SSOT 是零應用相依的
`ToukaProxy/src/utils/permission_defaults.py`。只有 Owner 的原生工具、MCP 與技能為
開放式；Base、Guest、Debugger 與未知角色全封閉。這是 bootstrap 修復通道，不是
一般執行模式。

Guard #32 同時掃描生產設定、`.example` 與 Python fallback；驗證原生工具名稱及
MCP reference 形狀，也涵蓋兩個全域地板。MCP 工具由外部伺服器動態提供，CI 只能驗
reference 形狀，不能保證工具名稱存在；此限制必須明示。

### 6. `permissions.json` 採啟動 fail-fast、熱重載 last-known-good

載入生命週期依情境分流：

| 情境 | 行為 |
|---|---|
| 檔案不存在、空白或 `{}` | 合法 bootstrap；使用最小 fallback，不標 degraded |
| 啟動時既有非空設定無法解析 | 重拋原始 JSON／Pydantic 例外，拒絕啟動 |
| 熱重載設定無法解析 | 保留 last-known-good，不套 fallback；記 `AUDIT` warning |
| 後續熱重載成功 | 原子替換設定並清除 degraded |

熱重載失敗期間 `/health` 與 `/system_health` 保持 HTTP 200 liveness，但回
`status="degraded"`、`permission_config_degraded=true` 與穩定 reason
`permissions_config_reload_failed`。啟動壞檔則在服務健康端點可用前即拒絕啟動。

### 7. RBAC 與 ABAC 維持分離

`permissions.json` 只定義角色的工具／技能／MCP 授權；`auth_level` 不屬於角色設定。
ReadOnly／Manual／SafeAuto／FullYOLO 是 per-session／request 的 ABAC／HITL 輸入，
由 `auth_policies.json` 與安全決策矩陣處理。RBAC 先回答「能不能使用該能力」，ABAC
再回答「本次如何執行或是否需人類確認」。

## 修訂 ADR-0195

本 ADR 修訂 [ADR-0195](0195-mcp-per-tool-governance-two-axis.md) 的三項後續語意：

1. per-role deny 不再沿角色繼承鏈聯集，而是 self-only；跨角色拒絕改用全域地板。
2. Owner 的身分面權限不再硬編短路；軸 A per-installation 仍獨立於 PermissionService，
   且照舊約束所有身分。
3. 損壞權限設定不再靜默退回較寬 fallback；改採啟動 fail-fast、熱重載
   last-known-good。

ADR-0195 的核心「軸 A per-installation × 軸 B per-role，任一拒絕即拒絕」仍有效，
MCP reference 比對 SSOT 與不支援前綴 glob 的決策也不變。

## 後果

**正面**

- 同一份角色設定只有一個語意解析點，廣告面與執行面無法再靠各自慣例維持一致。
- Owner 可被設定與全域安全地板實際約束，不再有程式碼旁路。
- deny 繼承方向與能力鏈一致；真正跨角色的禁令具名、可審計。
- 壞設定不會無聲放寬權限；熱重載錯誤可觀測且不犧牲服務 liveness。
- fallback 與生產設定進同一個 Guard #32 監管面。

**代價／邊界**

- 啟動期壞設定會阻止服務啟動；這是刻意的 fail-loud，操作者需依原始欄位錯誤修檔。
- wire 仍保留 `"*"` 哨兵；安全性依賴唯一解析器與測試／守衛，未追求檔案外觀純化。
- `/health` 的 200 只代表程序仍可服務，不代表最新權限檔已生效；監控必須讀
  `status` 與 `permission_config_degraded`。
- Guard #32 無法靜態驗證動態 MCP 工具是否存在，拼錯但形狀合法的名稱仍需部署／整合
  驗證。
- 本 ADR 只收斂 `permissions.json`；`identity_map.json` 與 `auth_policies.json` 的
  載入生命週期不在本次範圍。

## 否決方案

- **全面禁用 `"*"`**：無法消除多解析器分歧，且 MCP 未連線工具、自建技能與未來工具
  無法事前完整列舉。
- **Wire 改成 Grant 物件（選項 β）**：需破壞性遷移 JSON、OpenAPI 與前端，卻不增加
  執行期安全保證；留待獨立提案。
- **deny 沿繼承鏈聯集**：高權限子角色無法解除低權父角色限制，且 Base 的 deny-all
  可誤殺整條角色鏈。
- **所有載入錯誤都套 fallback**：會抹除 last-known-good 與使用者明示 deny。
- **所有載入錯誤都停機**：熱重載暫時錯字不應中斷仍持有安全 last-known-good 的程序。

## 驗證

- P0 矩陣凍結生產與 fallback 的 Gate 1、Gate 2、技能與 MCP 答案。
- P1 Grant 純值測試、P2 全入口接線與指定 4-cell diff、P3 UI 往返、P4 Guard #32
  fallback mutation、P5 啟動／熱重載／復原測試均已落地。
- P6 終態：ADR Integrity Guard 確認 196 份 ADR（0001..0196）連續、索引齊全、
  修訂雙向一致；權限／ADR 目標組 163 passed；完整 pytest 4024 passed／5 skipped。
- `guard_all` 43/44，唯一紅燈為既知 Guard #23 上游 `npm audit` advisory；
  vitest 沿用 P5 已驗證的 1125 passed（220 files，P6 未改前端）。
