# 角色權限面分離：遷移棘輪基線（P0）

> **這是 P0 產出的「未遷移閘」台帳，供 P1 的 Guard #41 棘輪版消費。**
> 規則（P1 起）：下列「已遷移」集合只准增、「待遷移」集合只准減；任何**新**檔案引入
> `AgentIdentity.role` 的角色閘（不走拆分後的 `human_role`／`agent_role` 謂詞）即 FAIL。
> 台帳由 ADR-0141 的權威盤點（2026-07-09 四維度並行）＋grep 覆核產生；行號見盤點與
> 特徵化矩陣（tests/unit/test_role_plane_characterization.py、…_path_characterization.py）。

## 範圍界定

- **在範圍內**＝含 `AgentIdentity.role` 角色閘的檔案（拆分要改寫者）。
- **明確排除**（純 `User` 模型 `role`，人類面本來就分離、不動）：
  `routers/agents.py`、`services/auth_service.py`、`services/setup_service.py`、
  `services/user_service.py`，以及 `routers/system.py`／`ui_sessions.py` 中讀
  `current_user`（`User` 模型）的分支。**注意**：`system.py`／`ui_sessions.py` 仍
  因其 `AgentIdentity` admin 閘（`_require_admin`／`_require_admin_role`）而在範圍內。

## 待遷移檔案集（依批次；P1 起只准減）

### P2 — routers 批次 1（inline 密集／高流量）
```
ToukaProxy/src/routers/debug.py
ToukaProxy/src/routers/observability.py
ToukaProxy/src/routers/souls.py
ToukaProxy/src/routers/ui_sessions.py
```

### P3 — routers 批次 2（frozenset 三元組 × 10 ＋ {Owner,admin} × 3 ＋ 單值 admin ＋ 工具面）
```
ToukaProxy/src/routers/editor.py
ToukaProxy/src/routers/connections.py
ToukaProxy/src/routers/autonomy.py
ToukaProxy/src/routers/custom_models.py
ToukaProxy/src/routers/scheduler.py
ToukaProxy/src/routers/models.py
ToukaProxy/src/routers/mcp.py
ToukaProxy/src/routers/identity.py
ToukaProxy/src/routers/skills.py
ToukaProxy/src/routers/terminal.py
ToukaProxy/src/routers/creation.py
ToukaProxy/src/routers/feedback.py
ToukaProxy/src/routers/bots.py
ToukaProxy/src/routers/group_settings.py
ToukaProxy/src/routers/admin.py
ToukaProxy/src/routers/config.py
ToukaProxy/src/routers/permissions.py
ToukaProxy/src/routers/system.py
ToukaProxy/src/routers/chat_upload.py
ToukaProxy/src/middleware/layers/monitor.py
ToukaProxy/src/tools/builtin/creation.py
```
（`chat_upload.py` 為 agent-only `role == "Guest"` 閘，P0 台帳漏列、P1 Guard #41 盤點時補回。）

### P4 — services（含三個乾淨樣板的機械改名）
```
ToukaProxy/src/services/resource_manager.py
ToukaProxy/src/services/permission_service.py
ToukaProxy/src/services/soul_service.py
ToukaProxy/src/services/agent_reset/primitives.py
ToukaMemory/src/memory_server/services/mission_manager.py
```

### P5 — 身分組裝點（最高風險、單獨隔離）
```
ToukaProxy/src/services/identity_service.py
ToukaProxy/src/services/identity_router.py
ToukaProxy/src/handlers/claude_handler.py
ToukaProxy/src/handlers/gemini_handler.py
ToukaProxy/src/handlers/openai_handler.py
```

## 已遷移檔案集（只准增）
```
# P2（routers 批次 1，改讀 AgentIdentity 謂詞 is_privileged/is_admin_authority/
#     is_owner_or_admin/is_admin_or_debugger；Guard #41 _BASELINE 28→24）
ToukaProxy/src/routers/debug.py
ToukaProxy/src/routers/observability.py
ToukaProxy/src/routers/souls.py
ToukaProxy/src/routers/ui_sessions.py

# P3（routers 批次 2＋monitor＋tools/creation，全改讀謂詞；Guard #41 _BASELINE 24→3）
ToukaProxy/src/routers/editor.py
ToukaProxy/src/routers/connections.py
ToukaProxy/src/routers/autonomy.py
ToukaProxy/src/routers/custom_models.py
ToukaProxy/src/routers/scheduler.py
ToukaProxy/src/routers/models.py
ToukaProxy/src/routers/mcp.py
ToukaProxy/src/routers/identity.py
ToukaProxy/src/routers/skills.py
ToukaProxy/src/routers/terminal.py
ToukaProxy/src/routers/creation.py
ToukaProxy/src/routers/feedback.py
ToukaProxy/src/routers/bots.py
ToukaProxy/src/routers/group_settings.py
ToukaProxy/src/routers/admin.py
ToukaProxy/src/routers/config.py
ToukaProxy/src/routers/permissions.py
ToukaProxy/src/routers/system.py
ToukaProxy/src/routers/chat_upload.py
ToukaProxy/src/middleware/layers/monitor.py
ToukaProxy/src/tools/builtin/creation.py

# P4（services；比較閘機械化＋消費檔改鍵；Guard #41 _BASELINE 3→1）
ToukaProxy/src/services/resource_manager.py         # 8 閘 is_human 分支機械改名＋:500 謂詞
ToukaMemory/src/memory_server/services/mission_manager.py  # 2 三元組閘 → is_privileged()
ToukaProxy/src/services/permission_service.py       # 消費檔：role_key 改讀 agent_role
ToukaProxy/src/services/soul_service.py             # 消費檔：is_toukasoul 改讀 agent_role
# agent_reset/primitives.py：盤點確認本就乾淨（無 .role 讀取閘），非遷移目標

# P5（身分組裝點；三閘讀改 human_role＋admin 代打走私改雙欄位配對；Guard #41 _BASELINE 1→0）
ToukaProxy/src/services/identity_router.py          # :96/:105/:119 閘讀→human_role＋:105 代打殲滅走私
```

## P5 附記：admin 代打走私殲滅（Option A）＋組裝建構延至 P7

**identity_router.py:105 走私殲滅**（§8-1）：舊碼把 `"admin"` 字串貼到 `is_human=False`
身分（`role="admin"`）＝跨面走私。改雙分支：admin 駕駛員 → `human_role="admin"`／
`agent_role=None`（**Option A：僅管理權**，維護者決定；不繼承目標軀體工具/is_owner）；
非 admin 擁有者 → `role=get_role_key(target)`（value-route 至 agent_role＝成為軀體）。
**行為逐欄位保持**（舊 role="admin" 亦 value-route 至 human_role="admin"）。§8-1 字面的
「agent_role=目標軀體」（Option B，admin 繼承軀體特權）**未採**，留日後明示。
代打雙向測試見 `tests/integration/test_identity_router.py`（admin 過管理閘 ✓／owner ✗）。

**延至 P7 的組裝殘項**（非 Guard 面、行為保持）：`identity_service`（human/agent/
get_identity）、三 handlers、PaC/LDH 建構點仍用 `role=`（validator 路由，非 `.role` 讀取
比較），隨 P7 拆 `role=` 建構子相容一起改填顯式欄位；`.role` 顯示/消費殘讀
（`resource_manager:233`／`tool_executor:219`／`proxy.py:22`）隨 P7 拆 `.role` 相容屬性清理。
P5 刻意不動組裝建構欄位改填——避免 `get_role_key` 回 human 角色的邊角在認證層放大。

## P4 附記：消費檔改鍵不變式＋跨套件身分替身

**permission_service／soul_service 是「消費檔」**（讀角色查政策/判定，非 allow/deny 閘），
改讀 `agent_role`（提案 §9 終態）：
- **不變式**：permission_service 遷移的行為保持繫於 **permissions.json／
  `DEFAULT_PERMISSIONS_CONFIG` 不含有實質 `allowed_tools` 的 `admin`／`normal` 角色鍵**
  ——如此 `.role`（"admin"/"normal"）與 `agent_role`（None）都塌成同一空「Base fallback」。
  現況成立（角色鍵僅 Owner/Guest/Base/Debugger）；且 `is_authorized` 目前為死碼。
- soul_service 判「Owner 軀體＝靈魂」，人類 agent_role=None 恆非 Owner，行為保持。

**跨套件身分替身**：mission_manager／soul_service 以鴨子型別消費 identity；除正式
`AgentIdentity` 外，兩個最小身分提供者需同步暴露分面介面（`human_role`／`agent_role`／
`is_privileged()`）——`_DispatchIdentity`（ToukaDispatch/server.py，生產）與 `_FakeIdentity`
（test_mission_manager_bugfixes.py，測試替身）。完整套件是揪出此類缺口的最終防線。

## P3 附記：兩個「非家族」inline 閘（等價鎖）

P3 的 21 檔中，多數閘屬三大已特徵化家族（矩陣直接鎖 helper），但兩個 inline 閘不對應
任何謂詞家族，另以 `tests/unit/test_role_plane_p3_novel_gates.py` 的**等價鎖**證明
「改讀雙欄位 ≡ 拆分前扁平 `.role` 檢查」（對全 8 形態逐一斷言）：
- `middleware/layers/monitor.py`：auth_level 自動化模式閘 `role == "admin"`
  → `is_admin_authority()`（owner 代打 owned_agents 分支不動）。
- `routers/chat_upload.py`：Guest 拒絕閘 `role == "Guest"` → `agent_role == "Guest"`
  （人類 agent_role=None 恆放行、只擋 agent Guest）。
實際源碼路徑另由既有端點測試涵蓋（`test_upload_rejects_guest`／整合套件每請求皆過
MonitorLayer）。`tools/builtin/creation._gate` 另補 `identity is None` 顯式前擋。

## P2 附記：依值路由修正（P1 模型的行為保持前提）

P2 遷移時完整套件揪出 8 個 fixture 用**不一致組合**（如 `AgentIdentity(role="Owner",
is_human=True)`＝人類帶 agent 角色，扁平模型原本容忍——`.role`＝"Owner"、過三元組閘）。
故把 P1 的 before-validator 由「**依 is_human** 路由」改為「**依角色值**路由」
（`role ∈ HUMAN_ROLES`→human_role、`∈ AGENT_ROLES`→agent_role、未知值退回依 is_human），
計算 `.role` 改 `human_role or agent_role or "Base"`。一致組合行為不變、不一致組合亦保持。
教訓：P0 特徵化矩陣只含一致形態、未含 fixture 不一致組合——已於 test_identity_role_predicates
加回歸鎖，P3-P5 遷移前完整套件仍是最終防線。

## 乾淨樣板（P4 機械改名，非重寫——邏輯已正確）

`resource_manager.py` 的 `get_vault_root`（:222-226）、`is_owner`（:278-343）、
`list_all_agent_stats`（:354-358）已正確按 `is_human` 分支，是全提案的目標行為
（特徵化矩陣 test_role_plane_path_characterization.py 已凍住其「拒絕 `assumed_admin`
混淆產物」的性質）。P4 只把 `identity.role` 改讀 `agent_role`／`human_role`，邏輯不動。

## Guard #41 棘輪追蹤範圍（P1 落地）

`scripts/guard_role_plane.py`（Guard #41）追蹤的是**比較閘面**——含 `.role` 比較/成員
或 `_*ROLES` frozenset 定義的 src 檔（P1 基線＝**28 檔**，`_BASELINE` 常數），
**只准縮不准長**；新檔引入扁平 `.role` 閘即 FAIL。這 28 檔是本台帳的**子集**：台帳另含
role-**消費**檔（permission_service／soul_service／identity_service／handlers／
agent_reset——讀寫 role 但非比較閘，regex 不偵測），它們隨各自批次遷移但不被 Guard #41
盯。`models/identity.py`（SSOT 模型、定義兩集合＋計算屬性）為**永久豁免**。

## 統計

- 待遷移**比較閘**檔：P2:4 ＋ P3:21 ＋ P4:2〔resource_manager／mission_manager〕＋ P5:1
  〔identity_router〕＝**全數已遷**。P4 消費檔（permission_service／soul_service）改鍵、
  primitives 本就乾淨；P5 的組裝**建構**殘項（identity_service／handlers／PaC/LDH 的
  `role=`）＋`.role` 顯示/消費殘讀（resource_manager:233／tool_executor:219／proxy.py:22）
  **延至 P7** 隨 `.role` 相容屬性＋`role=` 建構子拆除一起清（非 Guard 面、行為保持）。
- Guard #41 比較閘面基線：P1＝28 → P2＝24 → P3＝3 → P4＝1 → **P5＝0＝棘輪完全關閉**
  （任何新扁平角色閘即 FAIL）；豁免 identity.py（SSOT 模型）。**P7-5 已升 AST 永久禁令**
  （靜態禁 identity-ish `.role` 存取＋`AgentIdentity(role=)` 建構；升級即抓 2 個 P7-2 漏網生產站）。
- 特徵化矩陣：221 條（閘矩陣 209〔含工具面 _gate〕＋ 路徑/主權 12），P0 全綠＝凍結契約。
- P1（模型雙欄位＋計算屬性）：矩陣 221＋雙欄位 10 條全綠、零 cell 變動。
- P2（4 檔＋4 謂詞）：矩陣 221＋謂詞 34＋角色閘測試全綠（302）；依值路由修正。
- P3（21 檔全改謂詞）：矩陣 221**零 cell 變動**＋新增 18 條等價鎖（非家族 inline 閘）；
  後端 pytest 3182、ruff 零新增、openapi 無變、guard_all 41/41。
- P4（4 檔遷移＋1 檔確認乾淨）：路徑＋閘矩陣**零 cell 變動**（北極星 assumed_admin 拒絕
  性質保住）；消費檔改鍵不變式；補兩身分替身分面介面（_DispatchIdentity／_FakeIdentity）；
  後端 pytest 3182、ruff 零新增、guard_all 41/41。
- P5（identity_router：3 閘讀＋admin 代打走私殲滅 Option A）：閘＋路徑矩陣**零 cell 變動**；
  代打雙向測試（admin 過管理閘 ✓／owner ✗）；後端 pytest 3184（+2）、ruff 零新增、
  openapi 無變、guard_all 41/41、Guard #41 `_BASELINE` 1→0。
- **P6（四刻意行為修正，維護者核准四條全做）**：a. identity_map 寫入只驗 `ASSIGNABLE_AGENT_ROLES`
  （拒 admin/System，200→422）；b. is_owner Debugger 旁路補 `not is_human`；c. `_resolve_project_path`:503
  拆顯式雙面（admin 代打失 :503 工作區旁路、與 get_vault_root/is_owner 一致）；d. 跨面純度
  validator（`CrossPlaneRoleError`：agent_role ∉ HUMAN_ROLES、human_role ∉ AGENT_ROLES）＋移除
  `is_admin_authority` 死碼過渡條款。**這批刻意改變行為**（顯式 diff：test_identity_admin_security
  ＋test_role_plane_p6_intentional_fixes）；矩陣/謂詞中性、pytest 3184→3200、guard_all 41/41、openapi 無變。
- 驗收鐵律：P2–P5 每批遷移後矩陣**零 cell 變動**（已達成）；P6 刻意修正以顯式 diff 呈現（已達成）。
- **P7（收官，2026-07-10 完成）**：P7-1 enabler（`display_role`/`for_role`）→ P7-2 生產遷移
  （identity_service/handlers/PaC/LDH 的 `role=`＋`.role` 顯示殘讀〔resource_manager:233/
  tool_executor:219/proxy.py:22〕全清）→ P7-3/P7-4 測試遷移（AST codemod **352 站/153 檔** → `for_role`）
  → P7-5 原子拆除（刪 `.role` 計算屬性＋`role=` 相容映射＋`_DispatchIdentity.role`；Guard #41 升
  **AST 永久禁令**，升級即抓 `tools/subagent_tools.py`〔Guest〕＋`services/agent_reset/primitives.py`
  〔Debugger〕2 個 P7-2 漏網生產站）→ P7-6 文件 SSOT（新 `ROLE_MODEL.md`＋SECURITY.md/
  security_auth_guide.md 修）。**全部組裝殘項清空、棘輪台帳結案**；終態 pytest 3223、guard_all 41/41。
  詳見 [ADR-0141](../adr/0141-role-plane-separation.md) P7 成果節。
