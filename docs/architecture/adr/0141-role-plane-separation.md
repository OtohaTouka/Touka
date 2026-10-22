# ADR-0141: 人類／Agent 權限面分離 — 接受決策與 P0 遷移安全網

## 狀態

✅ **已接受並實作完成（決策 2026-07-09；P0–P7 全數落地、2026-07-10 收官；擁有者逐階段 commit）**。
本 ADR 記錄擁有者對 [role-plane-separation 提案](../proposals/role-plane-separation.md)
的核准與六個拍板點定案，並涵蓋絞殺者**全階段**：P0（特徵化矩陣＋棘輪基線）→ P1（模型雙欄位＋
相容計算屬性）→ P2–P5（閘/組裝點逐批遷移，Guard #41 棘輪 28→0）→ P6（四刻意行為修正）→
**P7（拆除 `.role` 相容層＋`for_role`/`display_role` SSOT＋Guard #41 升 AST 永久禁令＋文件收官）**。
各階段成果於下逐節補記。**終態**：`AgentIdentity` 無扁平 `.role`，人類角色（admin/normal）只存
`human_role`、agent 角色（Owner/Base/Guest/Debugger/System）只存 `agent_role`，型別層
（`_enforce_plane_purity`）保證跨面不可能。

> 🔧 **代理角色集合與 X-Agent-ID 範圍（[ADR-0245](0245-model-layer-stage-2a.md)，2026-10-12）**：`AGENT_ROLES` 現為 Owner／Base／Subagent／Debugger／System；Guest 退役，Subagent 只給 in-process 子代理，與 System 同樣不可經 identity_map 指派。只有靈魂、DebugAgent、DispatchAgent 能有代理角色；X-Agent-ID 只能切進靈魂，切向其他 ID 一律 403，管理員切進靈魂仍照 §8-1 選項 A。

## 日期

2026-07-09

## 背景

`AgentIdentity.role`（`identity.py:27`）是單一扁平字串，同時裝人類角色（admin／
normal）與 agent 角色（Owner／Base／Guest／Debugger），`is_human`（:28）是唯一分辨
訊號但約 35 處閘無視它。`VALID_ROLES`（:68）是矛盾聯集（含 admin、漏 normal）。
此混淆已釀真實漏洞（`PROXY_INTERFACE.md:96-97` soul IDOR；2026-07-07 context-snapshot
漏 admin 同類）。詳見提案 §2-§3 與 2026-07-09 四維度並行盤點。

## 決策

採**選項 C：完整欄位拆分**（`human_role`＋`agent_role` 互斥欄位），以絞殺者模式
分階段（P0–P7），終態由型別層保證「人類角色不可能出現在 agent 欄位」。棄選項 A
（只修文件，程式仍混）與 B（謂詞層，可被繞過只能靠守衛盯）。

**維護者決定的六個點（2026-07-09 核准）：**

1. **代打建模＝雙欄位配對**：代打身分帶 `agent_role=目標軀體角色` ＋
   `human_role=駕駛員人類角色`，取代 `identity_router.py:105` 把 "admin" 字串貼到
   `is_human=False` 身分上的走私。admin 閘改讀 `human_role=="admin"`（不論 is_human）＝
   admin 代打保權；閘不再需要知道「這是代打」。
2. **非 admin 駕駛員的 human_role＝保持 "normal"**（語意誠實、行為中立——normal 過不了
   任何特權閘）。
3. **`System` 正式納入 `AGENT_ROLES`**（內部專用、identity_map 不可指派，validator 另限）。
4. **四條刻意行為修正**（P6 逐條紅→綠、各自獨立 commit、以特徵化矩陣顯式 diff 呈現）：
   a. `VALID_ROLES` 拆成 `HUMAN_ROLES`／`AGENT_ROLES`；identity_map 寫入只驗 AGENT_ROLES。
   b. `is_owner:280` Debugger 旁路補 `not is_human` 防護（對齊姊妹函式）。
   c. `resource_manager.py:500` `{admin,Debugger}` 拆顯式雙面判定。
   d. `agent_role` 結構性不可能為 "admin"（validator）——原本理論上可過 admin 閘的
      非人類 admin 身分從此不可建構。
5. **欄位命名＝`human_role`／`agent_role`**。
6. **Guard #41「Role Plane Guard」**：遷移期＝棘輪（未遷移檔案集只縮不長，基線見
   `docs/architecture/role-plane-migration-baseline.md`）；終態＝AST 禁 `.role` 屬性存取
   ＋禁混合角色字面量集合出現在 routers/services。

## P0 成果（本次落地，純測試、零生產碼改動）

**特徵化矩陣＝遷移安全網**：把「拆分前」每個角色閘對完整 (role × is_human) 身分空間
的 allow/deny 拍照凍結，作為 P2–P5 所有遷移批的驗收基準（**一個 cell 都不能變**）。

- `tests/unit/test_role_plane_characterization.py`：22 個可匯入 `_require_*` 閘 helper
  ＋工具面 `_gate`（tools/builtin/creation.py，回拒絕字串）× 身分形態 ＋ None ＋ 混淆
  熱點顯式斷言＝**209 條**。三家族允許集合：三元組 `{Owner,Debugger,admin}`、
  `{Owner,admin}`（刻意排除 Debugger）、admin-only。（`_gate` 由 P0 落地後的獨立
  對抗式驗證揪出——原 22 集合遺漏此工具面閘，已補入，涵蓋邊界宣稱同步收緊。）
- `tests/unit/test_role_plane_path_characterization.py`：`resource_manager` 乾淨樣板
  （`is_owner` 旁路 DB 無關三列＋`get_vault_root` 存取 8 形態）＝**12 條**。
- **北極星對比已凍住**：混淆產物 `assumed_admin`（role="admin"、is_human=False，
  即 admin 代打產物）**通過所有扁平閘**，但被 `is_owner`／`get_vault_root`（正確按
  is_human 分支者）**拒絕**——拆分的目標即是讓全系統閘都具備此「不被 admin 字串走私」
  的性質。
- 兩矩陣皆**第一次執行即全綠**（221/221），證明對現行行為的推導精準無誤。

**棘輪基線**：`docs/architecture/role-plane-migration-baseline.md` 列出 35 個待遷移
檔案（P2:4／P3:21／P4:5／P5:5），排除純 `User.role` 檔（agents/auth_service/
setup_service/user_service）。

## P1 成果（模型雙欄位＋相容計算屬性＋兩集合＋Guard #41）

- **`AgentIdentity` 加互斥雙欄位** `human_role`／`agent_role`（`models/identity.py`），
  舊 `role` 改為**唯讀計算屬性**（人類回 human_role、否則回 agent_role），before-validator
  把舊式 `role=` 依 `is_human` 映射進正確欄位（`setdefault`→顯式雙欄位優先、dump round-trip
  安全）。`HUMAN_ROLES`／`AGENT_ROLES` 兩集合常數落地（VALID_ROLES 拆分用法留 P6-a）。
- **零呼叫點變動**（絞殺者核心）：全部既有 `AgentIdentity(role=...)` 建構與 `.role` 讀取
  結果不變——**P0 的 221 條矩陣＋新增 10 條雙欄位測試全綠、零 cell 變動**，後端 pytest
  2898→3128（工具面 _gate +9 進 P0＋雙欄位 10）。de-risk 確認：零 `identity.role =` 賦值
  （唯讀屬性安全）、frozen=True、無 AgentIdentity.model_dump 消費者讀 role。
- **OpenAPI／前端**：AgentIdentity schema 加雙欄位 → `update_openapi.py` 重生＋前端
  `openapi-typescript` 重生 api.ts（additive，vitest 974 不受影響）。
- **Guard #41「Role Plane Ratchet」**（`scripts/guard_role_plane.py`，guard_all 41 道）：
  追蹤 28 個比較閘面檔（`_BASELINE`），只准縮不准長；新檔引入扁平 `.role` 閘即 FAIL；
  identity.py 永久豁免。P2–P6 每批遷移後同步縮減 `_BASELINE`。

## P2 成果（routers 批次 1：debug/observability/souls/ui_sessions）

- **4 檔閘改讀雙欄位謂詞**：`AgentIdentity` 加 `is_privileged()`（三元組）／
  `is_admin_authority()`（admin-only）／`is_owner_or_admin()`（{Owner,admin}）／
  `is_admin_or_debugger()`（{admin,Debugger}）四謂詞（含 P6-d 前的 admin 代打過渡條款
  `agent_role == "admin"`）；4 檔全部 `identity.role in {...}` 改呼叫謂詞、移除
  `_PRIVILEGED_ROLES` 常數。Guard #41 `_BASELINE` 28→24。
- **依值路由修正**（P1 模型的行為保持前提）：完整套件揪出 fixture 的**不一致組合**
  （`AgentIdentity(role="Owner", is_human=True)`＝人類帶 agent 角色，扁平模型容忍），
  故 before-validator 改**依角色值**路由（非依 is_human）、計算 `.role` 改
  `human_role or agent_role or "Base"`。教訓：P0 矩陣只含一致形態，已加回歸鎖。
- **行為零變動**：221 特徵化矩陣＋34 謂詞測試＋既有角色閘測試全綠（**302 條**）；
  後端 pytest 3129→3163；openapi 無變（謂詞是方法非欄位）；前端零改動。

## P3 成果（routers 批次 2：21 檔全改讀謂詞）

- **21 檔閘全遷謂詞**（棘輪 `_BASELINE` 24→3）：三大家族機械對應——三元組
  `{Owner,Debugger,admin}`→`is_privileged()`（editor／connections／autonomy／
  custom_models／scheduler／models／mcp／identity／skills／terminal）；`{Owner,admin}`
  →`is_owner_or_admin()`（bots／creation／feedback／skills 第二閘／group_settings inline／
  mcp:445 inline／tools/builtin/creation `_gate`）；admin-only→`is_admin_authority()`
  （admin／config／permissions／system／identity `_require_admin_only`）。全部移除
  `_*_ROLES` frozenset 常數（Guard #41 regex 不再命中）。
- **兩個「非家族」inline 閘**（不對應任何謂詞，獨立處理＋等價鎖）：①`monitor.py` auth_level
  自動化模式閘 `role == "admin"`→`is_admin_authority()`（保留 owner 代打 owned_agents 分支
  不動）；②`chat_upload.py` Guest 拒絕閘 `role == "Guest"`→`agent_role == "Guest"`
  （人類 agent_role=None 恆放行、只擋 agent Guest）。`tools/builtin/creation._gate`
  另補 `identity is None` 顯式前擋（原靠 `getattr(...,"role")` 容錯 None）。
- **行為零變動**：221 特徵化矩陣**零 cell 變動**（helper 內部改呼謂詞、允許集合不變）；
  新增 `test_role_plane_p3_novel_gates.py` 18 條**等價鎖**（對全 8 形態證明新表達式
  ≡ 拆分前扁平 `.role` 檢查——絞殺者行為保持性質）；既有端點安全測試
  （`test_upload_rejects_guest`／`test_batch1_security` MCP install／`test_batch2_security`
  approve／`test_terminal_auth`）實跑遷移後源碼；後端 pytest 3163→3182；ruff 零新增
  （monitor 由 1→0）；openapi 無變；前端零改動；guard_all 41/41。
- **剩餘閘面**：`resource_manager`（P4 乾淨樣板）／`mission_manager`（P4，ToukaMemory）
  ／`identity_router`（P5，身分組裝點）＝Guard #41 `_BASELINE` 最後 3 檔。

## P4 成果（services：閘機械化＋消費檔改鍵；Guard #41 3→1）

- **resource_manager 三乾淨樣板＋:500（8 處）機械改名**：`get_vault_root`／`is_owner`／
  `list_all_agent_stats` 沿既有 is_human 分支——人類 admin 讀 `human_role`（**保留
  is_human 守衛**）、agent 讀 `agent_role`；Debugger 旁路讀 `agent_role`（無 is_human，
  與拆分前一致）。**北極星性質不變**：`assumed_admin`（human_role="admin"、is_human=False）
  仍被 get_vault_root／is_owner **拒絕**（is_human 守衛擋下）——路徑特徵化矩陣**零 cell
  變動**。`:500` `role not in {"admin","Debugger"}` → `not is_admin_or_debugger()`
  （行為等價；P6-c 再做刻意雙面拆分）。唯一保留＝`:233` log 顯示讀（不觸發守衛、P7 掃）。
- **mission_manager 兩三元組閘**（ToukaMemory）→ `identity.is_privileged()`
  （`{Owner,Debugger,admin}`＋既有 capability OR 分支不變）。
- **permission_service 四消費點**（`is_authorized`／`get_allowed_tools`／
  `is_mcp_server_allowed`／`can_use_skill`）`role_key = identity.role` → `agent_role`
  （提案 §9 終態＝以 agent 軀體角色查工具表、人類特權走 router）。**行為保持已驗證**
  （獨立子代理追呼叫圖）：`is_authorized` 目前是死碼（零生產呼叫）；其餘三者雖收人類/
  代打身分，但 **permissions.json 不含 admin／normal 角色鍵** → `.role`（"admin"/"normal"）
  與 `agent_role`（None）都塌成同一空「Base fallback」＝結果 byte 級相同。`_get_effective_permissions`
  簽名放寬為 `str | None`。**不變式**（文件已記）：安全性繫於 permissions.json／
  `DEFAULT_PERMISSIONS_CONFIG` 永不定義有實質 `allowed_tools` 的 admin／normal 角色。
- **soul_service.is_toukasoul** `identity.role` → `agent_role or "Base"`（靈魂＝Owner 軀體
  判定；人類 agent_role=None→非 Owner，與拆分前 human_role 恆非 "Owner" 一致）。
- **agent_reset/primitives.py**：盤點確認**本就乾淨**（無 `.role` 讀取閘，唯一角色引用是
  `:38` 建構 `role="Debugger"`＝寫入非讀取），非遷移目標、移出 P4 清單。
- **跨套件身分替身教訓**：mission_manager／soul_service 以**鴨子型別**消費 identity，
  正式執行是 `AgentIdentity`，但另有兩個模仿舊扁平介面的最小身分提供者需同步暴露分面
  介面——`_DispatchIdentity`（ToukaDispatch/server.py 生產類，補 `human_role`／`agent_role`／
  `is_privileged()`）與 `_FakeIdentity`（mission_manager 測試替身，同補）。完整套件是揪出
  這類替身缺口的最終防線（矩陣只覆蓋 `AgentIdentity` 真身）。
- **驗收**：路徑＋閘特徵化矩陣**零 cell 變動**；後端 pytest 3182（含 ToukaMemory／
  ToukaDispatch 跨套件）；ruff 零新增；guard_all 41/41；Guard #41 `_BASELINE` 3→1
  （僅剩 `identity_router`，P5）。

## P5 成果（身分組裝點：閘讀＋admin 代打走私殲滅；Guard #41 1→0＝棘輪關閉）

- **identity_router 三閘讀 → `human_role`**（`:96`／`:105` 三元運算子條件／`:119`）——
  皆判定「原始 token 駕駛員是否 admin」（is_human 身分，`role` 即 `human_role`），
  `identity.role == "admin"` → `identity.human_role == "admin"`、`!= "admin"` 同理
  （`role == "admin"` iff `human_role == "admin"`，因 admin 恆 value-route 至 human_role）。
- **admin 代打走私殲滅（§8-1，維護者選 Option A：僅管理權）**：`:105` 原本把 `"admin"`
  字串貼到 `is_human=False` 身分上（`role="admin"`）＝跨面走私。改為**雙分支顯式建構**：
  admin 駕駛員 → `AgentIdentity(human_role="admin", agent_role=None, is_human=False)`
  （保管理權、閘讀 human_role；**不繼承目標軀體角色**）；非 admin 擁有者 → 沿用
  `role=get_role_key(target)`（value-route 至 agent_role＝成為目標軀體）。**行為完全保持**：
  舊 `role="admin"` 亦 value-route 至 `human_role="admin"`／`agent_role=None`，故 admin 代打
  身分與拆分前**逐欄位相同**；`.role` 仍單欄位（不因雙填而模糊）。§8-1 字面的「agent_role=
  目標軀體」（admin 繼承軀體工具/is_owner）＝**選項 B，未採**，留作日後明示（權限邊界擴張）。
- **Guard #41 `_BASELINE` 1→0＝棘輪完全關閉**：自此任何**新**的扁平 `identity.role`／
  `caller.role`／`verified.role` 比較閘或 `_*ROLES` frozenset 即 drift → FAIL（禁令生效）。
- **代打雙向測試**（提案 §10 P5 驗收）：`test_admin_assumption_is_pilot_pair_option_a`
  （admin 代打 human_role="admin"／agent_role=None／`is_admin_authority()` True＝管理閘 ✓）
  ＋`test_owner_assumption_becomes_body_no_admin_authority`（非 admin 擁有者 agent_role=目標／
  human_role=None／`is_admin_authority()` False＝管理閘 ✗）。
- **延至 P7 的組裝殘項**（**非** Guard 面、行為保持、隨 `role=` 建構子拆除一起做）：
  `identity_service`（human/agent/get_identity 三建構）、三 handlers、PaC/LDH 建構點仍用
  `role=`（validator 路由，非 `.role` 讀取比較故不觸發守衛）；`.role` 顯示/消費殘讀
  （`resource_manager:233`／`tool_executor:219`／`proxy.py:22 ctx["role"]`）亦隨 P7 `.role`
  相容屬性拆除清理。P5 刻意**不動組裝建構的欄位改填**——避免 `get_role_key` 回 human 角色
  的邊角在認證層放大風險（維護者選行為保持）。
- **驗收**：閘＋路徑矩陣**零 cell 變動**；後端 pytest 3182→**3184**（+2 代打雙向）；
  ruff 零新增；guard_all 41/41；openapi 無變；Guard #41 `_BASELINE` **1→0**。

## P6 成果（四刻意行為修正——**這批刻意改變行為**，各自顯式 diff）

與 P2–P5 不同，P6 修的是拆分前的漏洞/不一致行為（矩陣顯式 diff、非零變動）。擁有者
核准「四條全做」。測試：fix a 於 `test_identity_admin_security.py`，b/c/d 於
`test_role_plane_p6_intentional_fixes.py`。

- **a. identity_map 寫入只驗可指派 AGENT 角色**：`VALID_ROLES`（含 admin 的矛盾聯集）→
  `ASSIGNABLE_AGENT_ROLES = AGENT_ROLES − {System}`＝{Owner,Base,Guest,Debugger}。
  **行為變更**：`POST /v1/identity` 給 agent 指派 `role="admin"`（人類角色）或 `"System"`
  （內部專用）→ **200→422**（堵「經 map 把 agent 提升成 admin」的後門）。磁碟既有條目仍
  經未驗證 `IdentityMapConfig` 載入（零資料遷移）。
- **b. is_owner 的 Debugger 旁路補 `not is_human`**：對齊姊妹函式（get_vault_root／
  list_all_agent_stats 的特權 agent 判定皆要求 `not is_human`）。**行為變更**：不一致組合
  「is_human=True＋agent_role=Debugger」不再經旁路取得主權（落到 human-pilot 分支）。
  路徑矩陣三凍結列（human_admin／agent_debugger／assumed_admin）**不變**。
- **c. `_resolve_project_path`（:503）工作區旁路拆顯式雙面**：`is_admin_or_debugger()` →
  `(is_human ∧ human_role=="admin") ∨ (¬is_human ∧ agent_role=="Debugger")`。**行為變更**：
  **admin 代打**（is_human=False、human_role="admin"）失去 :503 工作區 jail 旁路——與其在
  get_vault_root／is_owner 已無旁路一致（P4 後那兩處要求 is_human；:503 是唯一漏網），
  且符 P5 Option A（代打＝管理閘權、非資源監獄旁路）。人類 admin／agent Debugger 仍旁路。
- **d. 跨面純度 validator（型別層保證）**：新 `CrossPlaneRoleError`＋`_enforce_plane_purity`
  after-validator——`human_role ∉ AGENT_ROLES`、`agent_role ∉ HUMAN_ROLES`（跨面即
  ValidationError）；只擋跨面污染、不強制窮舉（未知/自訂 agent 角色仍可）。效果：
  `agent_role == "admin"` 結構性不可能，故 `is_admin_authority` 的過渡條款
  `or agent_role == "admin"` 成死碼、**單點移除**（收斂為純 `human_role == "admin"`）。
  **原「消滅 assumed_admin」框架因 P5 Option A 過時**：assumed_admin（`role="admin"`／
  is_human=False → 依值路由成 human_role="admin"）**正是合法的 admin 代打形態**、保留可建構、
  仍過 admin 閘（保權）；純度 validator 不影響它（admin ∉ AGENT_ROLES）。矩陣中性
  （無形態用顯式 agent_role="admin"）；特徵化/謂詞測試 docstring 同步修正此重新詮釋。
- **驗收**：矩陣＋謂詞＋路徑測試全綠（fix d 收斂謂詞不改允許集）；後端 pytest 3184→**3200**
  （+16：a×2＋b/c/d×14）；ruff 零新增；guard_all 41/41；openapi 無變。
- **P7 待收**：SECURITY.md:266／security_auth_guide 等文件仍引用舊 `VALID_ROLES`（已改名）
  ——隨 P7 文件 SSOT 一併更新（見 P7-6，已完成）。

## P7 成果（收官：拆 `.role` 相容層＋`for_role`/`display_role` SSOT＋Guard #41 AST 永久禁令＋文件）

絞殺者最終步——移除 P1 的相容層，全系統改讀分面（`display_role`／謂詞）＋依值路由建構
（`for_role`）。逐批 commit（P7-1~P7-6），每批全綠可交付。

- **P7-1 加 enabler（加法、零行為變更）**：`display_role`（純 `@property`＝
  `human_role or agent_role or "Base"`，**非** `@computed_field`——不進 `model_dump()`／
  openapi，純顯示面）取代垂死的 `role` 計算屬性讀法；`for_role(agent_id, role, *, is_human)`
  classmethod＝**依值路由**建構子，取代動態 `role=`（`role=get_role_key(...)` 值不確定的站點），
  與 before-validator 的 `role=` 分支逐位元相同（admin 代打不變式：`role="admin"` 即使
  `is_human=False` 也必落 `human_role`）。特徵化測試證 `for_role ≡ legacy`。
- **P7-2 生產遷移**：identity_router／identity_service／三 handlers／custom_models（6 IDOR 站）
  的 `role=` 建構 → `for_role`／`agent_role=`；`.role` 顯示/消費殘讀（`resource_manager:233`／
  `tool_executor:219`／`proxy.py:22 ctx["role"]`／audit／debug）→ `display_role`；
  `_DispatchIdentity`＋`_FakeIdentity` 補 `display_role`。
- **P7-3 測試關鍵面遷移**：conftest 身分工廠 → `for_role`；~22 個測試 `.role` 讀 → `display_role`；
  meta-test 更新。
- **P7-4 測試建構大批遷移（AST codemod、確定性）**：`AgentIdentity(role=…)` →
  `AgentIdentity.for_role(…)` 共 **352 站／153 檔**（別名 `ProxyIdentity` 依 per-file import
  解析；`role="base"` 等大小寫引數原樣保留＝for_role 依值路由相同）；2 相容特徵化檔
  （`test_identity_dual_role_fields`／`test_role_plane_p7_enablers`）排除留 P7-5 改寫；1 個
  `**spread`（無 role 的 dict）安全留。值路由保持故 pytest **3222 不變**（行為保持）、零淨增 ruff。
- **P7-5 原子拆除**：`identity.py` 刪 `role` `@computed_field`＋`_DEFAULT_ROLE`＋
  `validate_from_str` 的 `role=` 映射分支＋`computed_field` import（**保留** `str→agent_id`
  轉型與 `_enforce_plane_purity` 純度 validator）；`display_role` fallback 內聯 `"Base"`；
  除 `_DispatchIdentity.role`。改寫 2 相容特徵化檔＝**回歸鎖**（`.role` 存取→AttributeError／
  `model_dump` 無 `role` 鍵／舊式 `role=` 不再被路由＝fail-safe Base——相容層不可復活）。
  **Guard #41 regex 棘輪 → AST 永久禁令**：靜態禁 identity-ish `.role` 屬性存取 ＋
  `AgentIdentity(role=…)` 建構（掃 src、exempt identity.py、不掃 tests）。**升級即抓 2 個
  P7-2 漏網生產站**：`tools/subagent_tools.py:167`（Guest）＋`services/agent_reset/primitives.py:38`
  （Debugger）——後者若靜默退成 role-less 身分則 `is_privileged()`→False＝**會斷 reset 授權**，
  真潛在 bug 被守衛擋下（驗證 fail-loud 邊界價值）；兩站已改 `for_role`。openapi 唯一變＝
  AgentIdentity `description`（class docstring）重生，`role` 屬性本就不在 wire（驗證上下文
  schema、computed 不入）＝api.ts 無變。驗收：零淨增 ruff（全庫對照，唯一差＝既有 TRY003 行號
  位移）、guard_all 41/41、pytest **3222→3223**（+1 測試改寫）。
- **P7-6 文件 SSOT 收官**：新 [`ROLE_MODEL.md`](../ROLE_MODEL.md)（角色模型權威 SSOT）；
  SECURITY.md（DA2/DA12）＋`security_auth_guide.md`（role 值 `admin,user`→`admin,normal` 事實
  修正＋分面說明）＋conftest/README 註解 `VALID_ROLES`→`ASSIGNABLE_AGENT_ROLES`；提案／棘輪
  基線／本 ADR 台帳結案。

**終態不變式**：`AgentIdentity` 無 `.role`；人類角色只存 `human_role`、agent 角色只存
`agent_role`（`_enforce_plane_purity` 型別層保證跨面不可能）；有效角色讀 `display_role`（顯示）
或謂詞（權限，`is_admin_authority`/`is_privileged`/…）；動態建構走 `for_role`（依值路由，
admin 恆落 human 面）。Guard #41 AST 永久防復活。

## 後果

- **每個中間態可交付**：P1 加雙欄位＋`.role` 相容計算屬性（零呼叫點變動全綠）→ 逐批
  遷移（棘輪只縮不長）→ P7 拆舊＋守衛永久化。停擺＝未完成、非壞掉。
- **零 token／資料遷移**：JWT 不帶 role（現場解析）；users.json／identity_map／
  permissions.json 值空間不變。前端已是兩平面、幾乎不動。
- **三個真風險有前置護欄**（提案 §12）：①組裝點 P5 單獨隔離＋smoke 全跑；②代打
  設計本 ADR 已拍板＋P0 矩陣含 assumed_admin 全形態；③行為偷渡靠矩陣鐵律
  （遷移批零變動、修正條目顯式 diff）。
- **範圍外（誠實邊界）**：LDH 發卡姿態（同網段宣稱 DebugAgent 即得 Debugger）為獨立
  安全議題，本提案不解、僅記載；不擴充 normal 人類權限。
- **驗收**：ruff（新測試）＋guard_all **40/40**＋後端 pytest 全綠（新增 221 條特徵化）＋
  前端 vitest 不受影響（純後端測試）。

## 相關

- [提案：role-plane-separation](../proposals/role-plane-separation.md)（§8 六拍板點、
  §9 矩陣、§10 P0–P7）；遷移棘輪基線
- [ADR-0104]（Debugger 排除姿態：reset／bots，本提案保留）；[ADR-0132]（session owner
  human/agent 通道分離先例）；[ADR-0134~0138]（多階段提案執行慣例）
- 乾淨樣板：`resource_manager.py` get_vault_root／is_owner／list_all_agent_stats
