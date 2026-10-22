# P7 收官執行計畫：拆除 `.role` 相容層（ADR-0141 完整拆除 / Option A）

> **狀態：✅ 已核准並執行完成（2026-07-10，P7-1~P7-6 逐批 commit）。** 詳見
> [ADR-0141](../adr/0141-role-plane-separation.md) P7 成果節與 [ROLE_MODEL.md](../ROLE_MODEL.md)。
> 本計畫是 role-plane-separation 提案最後一階段 P7 的**分批執行藍圖**，依 2026-07-10
> 探索盤點（498 個站點）產出。維護者已選 **Option A（完整拆除 `.role` 讀取屬性
> ＋ `role=` 建構子相容）**。

## 0. 前提與最重要的安全事實

- **`production_gate = 0`**：已無任何授權/比較閘讀 `AgentIdentity.role`（全走
  `human_role`/`agent_role` 謂詞，P2–P5 完成）。**故移除 `.role` 只可能崩
  （AttributeError/ValidationError），不可能無聲繞過權限**——這是「機械但廣」的遷移，
  不是邏輯變更。
- **拆除是原子的**：`.role` 計算屬性與 `role=` 建構子分支，在**所有** ~426 個站點遷完前
  不能刪；半遷會留下硬性 import/執行期斷裂。故拆除放在最後一批、且前面每批都**在相容層
  仍在的情況下**保持全綠可交付。
- 純度 validator（P6-d）與 `str→agent_id` 建構分支**保留**（P7 只拆 `role=` 分支與
  `.role` 屬性）。

## 1. 範圍（去重後 458 distinct 站點；需編輯 ≈426）

| 類別 | 數量 | 批次 |
|---|---:|---|
| production_gate | **0**（安全前提） | — |
| 生產 `.role` 讀取（5 display＋3 consumer） | 8 | P7-2 |
| 生產 `role=` 建構（含 custom_models 6 IDOR＋auth 組裝） | 30 | P7-2 |
| 生產無 kwarg 建構（靠 "Base" 預設，**不會斷**除非有下游 `.role` 讀取） | 20 | 不需改 |
| 測試 `.role` 讀取 | 25 | P7-3 |
| conftest `_create_identity` 工廠（覆蓋 ~29 間接 mock_identity 呼叫） | 1 | P7-3 |
| 相容特徵化測試（手審安全面） | 少量 | P7-3 |
| 測試 `role=` 建構（純機械 value-routing） | **≈363／156 檔** | P7-4 |
| 相容層定義（`_DEFAULT_ROLE`＋`role` 屬性＋`role=` 分支＋Guard #41 基線） | 4 | P7-5 |
| 文件/契約（openapi/api.ts/SECURITY/ADR/baseline/proposal） | 10 | P7-6 |

## 2. 兩個 enabler（P7-1，先落地、零行為變更、可獨立交付）

1. **`AgentIdentity.display_role` 唯讀屬性** ＝ `human_role or agent_role or "Base"`（與垂死的
   計算屬性**逐位元相同**）。供 display/序列化讀取改用。
2. **`AgentIdentity.for_role(agent_id, role, is_human=False, **kw)` classmethod** ＝**精確複製**
   現行 before-validator 的依值路由（`role ∈ HUMAN_ROLES → human_role`；`∈ AGENT_ROLES →
   agent_role`；未知值依 `is_human` 提示）。用於 de-risk 動態 `role=get_role_key(...)` 站點
   （值不確定時交給它路由＝與拆分前逐位元相同）。

這兩個都是**加法**（新屬性＋新 classmethod），相容層仍在，零行為變更。

## 3. 分批（每批一 commit 邊界；P7-1~4 相容層仍在＝全綠可交付）

### P7-1 · Enablers
新增 `display_role`＋`for_role`。驗收：模型測試＋全套綠。

### P7-2 · 生產遷移（8 讀取＋30 建構，手審）
- **8 讀取**：debug.py:46/142/188、resource_manager.py:233、tool_executor.py:219（display）
  ＋proxy.py:22（`ctx["role"]`）、audit_service.py:79、tool.py:305 → `display_role`。
  - ⚠ `tool.py:305` 用 `getattr(identity,"role","")`，拆後會**無聲退化為 ""**——改**顯式
    None-guard**（`identity.display_role` 或明確判空），非 getattr。
  - `proxy.py:22` `ctx["role"]=identity.role` → `identity.display_role`（逐位元相同；下游
    test_security_hardening 期望 "Owner"/"Guest" 不變）。
- **30 建構**：靜態人類（identity_service:94 `role=user.role`）→ `human_role=`；靜態/動態 agent
  （handlers、identity_router PaC/LDH、identity_service:104/140、custom_models 6 IDOR）→
  `agent_role=` 或**動態用 `for_role(...)`**。
  - **最高風險**：auth 組裝（identity_service/router/handlers）＋custom_models（IDOR 面）——
    最後做、逐行審。
- 驗收：全套綠、特徵化矩陣**零 cell 變動**（皆行為保持）。

### P7-3 · 測試關鍵面遷移（25 讀取＋conftest 工廠＋相容特徵化測試，手審）
- 25 測試 `.role` 讀取 → `display_role`／顯式欄位。
- **conftest.py:119 `_create_identity`**（工廠 SSOT）先改，一次覆蓋 ~29 間接 mock_identity 站點。
- 相容特徵化測試（test_identity_dual_role_fields／test_identity_role_predicates／
  test_role_plane_*／test_role_plane_p6_*）——安全載重，逐行手改（含 `role="admin"` 代打不變式）。
- test_sec_batch_gamma.py:63（meta-test 斷言 source 含 `'identity.role'`）→ 更新為遷移後字串。
- 驗收：全套綠。

### P7-4 · 測試建構大批遷移（≈363 站／156 檔，機械）
- `AgentIdentity(role="X",...)` → `human_role=`/`agent_role=` 依值路由。
- **機制（待決策，見 §4-4）**：優先**確定性 codemod 腳本**（AST/regex，可重現、可審 bulk diff）；
  備案並行遷移工作流（worktree、每代理一批檔）。
- **鐵律**：`role="admin"/"normal"` **必依值路由到 human_role**（即使 is_human=False＝北極星
  代打不變式）；`Owner/Base/Guest/Debugger/System` → agent_role；非典範值（`base`/`Member`…）
  **原樣保留**（見 §4-3）。
- 驗收：全套綠。

### P7-5 · 原子拆除＋Guard #41 升 AST（fail-closed，最後一批）
- **先重掃**：`AgentIdentity(` ＋ `\.role\b` 全 src＋tests，確認**零殘留**（探索警告：多命中
  檔被折成代表列＋custom_models 曾漏抓——刪前必重掃 ToukaProxy/src、ToukaMemory/（含 scripts/）、
  ToukaDispatch/）。
- 刪 `identity.py`：`_DEFAULT_ROLE`、`role` @computed_field、validate_from_str 的 `role=` 分支
  （保留 str→agent_id ＋ 純度 validator）。
- **Guard #41 升級**（guard_role_plane.py）：regex 空基線棘輪 → **AST 永久禁**
  `<AgentIdentity>.role` 屬性存取；移除 identity.py `_EXEMPT`（屬性已不存在）。
- 驗收：全套綠（無人再讀 `.role`）、guard_all、Guard #41 AST 對植入的違規能抓紅。

### P7-6 · 文件 SSOT＋契約重生＋提案收官
- 重生 openapi.json（AgentIdentity schema 掉 `role` 欄位——**動工前先確認它是否真在
  response schema**，探索指出可能只在 prose）＋api.ts。
- 新 `ROLE_MODEL.md`（雙平面模型 SSOT）。
- 修 SECURITY.md:266（`VALID_ROLES`→`ASSIGNABLE_AGENT_ROLES`）＋:292、security_auth_guide.md:60。
- 收官 ADR-0141／migration-baseline／proposal 的 P7 台帳。
- 驗收：guard_all（Guard #17 API sync、Guard #28 ADR）。

## 4. 需你核准的決策點（探索 §5）

1. **role-less 身分的 "Base" 語意**：拆後無角色身分＝`human_role=None ∧ agent_role=None`。
   **建議**：`display_role` 對 None 回 `"Base"`（display 逐位元不變）；
   test_identity_dual_role_fields:47/56 的 `.role=="Base"` 改斷言 `display_role=="Base"`。
   （替代：改成斷言雙 None＝顯式承認「無角色」，但會動到顯示輸出。）
2. **動態建構走 `for_role()`（建議）** 而非硬寫 `agent_role=`：若 `get_role_key` 理論上可能回
   人類值，硬寫 agent_role 會誤路由、繞過 is_admin_authority；`for_role` 依值路由則永遠安全。
   （若你確認 `get_role_key` 只回 agent 面，也可硬寫；建議用 for_role 免除此顧慮。）
3. **非典範/小寫值原樣保留（建議）**：`ui_sessions.py:785 role="base"`（小寫、疑似 typo）、
   測試中的 `base/owner/user/Member/Reader/Manual` → 逐位元保留（零行為差）；**不趁機正規化**
   （正規化＝行為變更，另案）。
4. **P7-4 機制**：確定性 codemod（建議，可重現）vs 並行遷移工作流。
5. **openapi 契約**：拆 `role` 計算欄位是否改動對外 response schema／前端型別（P7-6 動工前確認；
   若有前端消費 `.role`，需一併遷）。

## 5. 風險與緩解

| 風險 | 緩解 |
|---|---|
| 半遷留下斷裂 | 拆除（P7-5）放最後、且 P7-5 前重掃零殘留才刪 |
| 遺漏站點（探索折疊代表列） | P7-5 重掃全 src＋tests＋scripts；Guard #41 AST 事後永久兜底 |
| 代打不變式被 codemod 破壞 | P7-4 依**值**路由（admin→human_role 覆蓋 is_human）；相容特徵化測試（P7-3）先手改把關 |
| auth 組裝/IDOR 面出錯 | P7-2 最後做、逐行手審；矩陣零 cell 變動為驗收 |
| 動態 get_role_key 誤路由 | 一律走 `for_role()` |

## 6. 驗收（每批）

ruff（零新增）＋lint_ssot＋guard_all（41 道；P7-5 後 Guard #41＝AST）＋後端 pytest（每批全綠；
特徵化/謂詞矩陣 P7-2/3 **零 cell 變動**）＋vitest（P7-6 若動 api.ts）＋openapi sync（P7-6）。
每批一乾淨 commit 邊界；P7-1~4 在相容層仍在下全綠可交付；P7-5 是 fail-closed 翻轉；P7-6 收文件。
