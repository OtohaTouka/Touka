# ADR-0147: 靈魂建立即定角色——類型硬綁定（tag_rules）＋identity_map 自癒＋id 撞名防禦

## 狀態

✅ 已接受（07-11 認證事故餘波清單 #4-B；維護者決定三點）

> 🔧 **類型規則與覆寫範圍（[ADR-0245](0245-model-layer-stage-2a.md)，2026-10-12）**：`is_custom→Guest` 規則已移除（Guest 退役），自訂模型落到 Base；`agent_overrides`、`tag_rules` 與 `default_role` 只對靈魂、DebugAgent、DispatchAgent 生效，其他 ID 一律 Base、沒有能力。`is_toukasoul→Owner`、`identity_map.json.example` 自癒與 LDH 不帶類型旗標的做法不變。

## 日期

2026-07-12

## 背景

維護者回報「重建 Sumi 後掉了 Owner 角色（變 Base）」。清空重啟後源碼追蹤揭露：

1. **靈魂→角色的 SSOT ＝ `config/identity_map.json`** 的 `tag_rules`／`agent_overrides`
   （`permission_service.get_role_key` 解析：`agent_overrides[id]` 優先＞`tag_rules`
   按 `is_toukasoul`/`is_custom` 旗標＞`default_role`）。
2. **該檔 gitignore（`config/*.json`）、從未 tracked、無 `.example`、不在
   `ESSENTIAL_CONFIGS`** → fresh checkout／清空重啟後**根本不存在** →
   `_load_identity_map` 退 `DEFAULT_IDENTITY_MAP(default_role="Base")` → **所有靈魂
   ＋DebugAgent 全塌 Base**（Sumi 掉 Owner；除錯/計分卡失權）。它只在 admin 手動
   POST `/identity` 時才被建立。
3. **`tag_rules["is_toukasoul"]` 機制在程式裡已存在但休眠**（`get_role_key` line
   292）——因為 `verify_token`／`get_identity`／`identity_router` 等**每一個**
   角色解析站點都呼叫 `get_role_key(agent_id)` **不帶類型旗標**，tag_rules 永不觸發。
4. `create_soul` 只寫 soul.md/config.json/models/registry，**零角色綁定**；重建靈魂
   自然回到 Base。
5. 附帶：`verify_token` 人類分支短路——若 `user.id == soul.agent_id`（清空前手動
   建立的 `a706a710` 二義性 id），帶該 id 的 token 永遠解析成人類、拿不到靈魂的
   agent 角色（清空後已無此撞名，但結構性風險仍在）。

## 決策（維護者決定三點）

1. **類型硬綁定（tag_rules，Decision 2）**：`is_toukasoul→Owner`、`is_custom→Guest`、
   native/API→`default_role(Base)`。靈魂由 admin-only、步升閘的 `create_soul` 建立，
   本就受信＝Owner；`agent_overrides` 提供逐靈魂降級的逃生口。**選 tag_rules 而非
   建立時寫 override**：tag_rules 對所有靈魂**均勻、追溯、免遷移**生效（含重建/既有）。
2. **建立時禁止同名（Decision 1a）**：`create_soul` 拒 `agent_id` 撞任何現存
   `user.id`（fail-loud，比事後解析靠優先序乾淨）。
3. **技能廣告 A1 先修 B 再評估**（Decision 3）：本 ADR 不動技能廣告；先讓靈魂拿回
   正確角色（Sumi→Owner→`allowed_skills=["*"]`），再評估 Base/Guest 的廣告落差是否
   仍需獨立修。

## 實作

- **種子＋自癒**：新 `identity_map.json.example`（tag_rules `is_toukasoul→Owner`／
  `is_custom→Guest`、`agent_overrides DebugAgent→Debugger`、`default_role Base`）
  ＋加進 `bootstrap.ESSENTIAL_CONFIGS`（缺檔時 `ensure_configs` 從 `.example` 還原）。
  Guard #34（`test_config_examples`）納入 `IdentityMapConfig` parity。
- **接線（讓 tag_rules 真的觸發）**：`agent_service.resolve_role_and_caps(identity_id)`
  ——經 `identify_agent`（soul 目錄→is_toukasoul、custom 目錄→is_custom）取旗標，
  交給 `get_role_key`／`get_capabilities_by_id`。**中心化在 identity gateway**，避免把
  旗標穿線到 7+ 站點。套用於 `verify_token`、`get_identity`、`identity_router` 的
  **擁有者代打路徑**（帶卡、走 owned_agents）。import 方向
  `identity_service/identity_router → agent_service` 無環。
- **LDH 刻意不帶旗標（對抗式審查修正）**：無卡首裝視窗（`get_optional_identity`
  第 3 步）的 `X-Agent-ID` 是攻擊者可控——若帶旗標，`X-Agent-ID=<既存靈魂 id>`
  會解析成 Owner 並鑄一張綁定該具名靈魂的 Owner 卡（身分冒用面擴大）。故 LDH 維持
  **plain `get_role_key`**：DebugAgent 經 `agent_overrides→Debugger`（開發卡照舊），
  任意靈魂 id 落回 default Base（無害、＝改版前行為）。認領後 LDH 全關（ADR-0144）。
- **能力隨角色一併恢復**：`.example` 的 `is_toukasoul` tag_rule 授
  `["heartbeat_awakening", "can_supervise_dispatch"]`——**修好角色須一併恢復能力**，
  否則靈魂拿回 Owner 卻仍無法被心跳自主喚醒（`can_awaken` 檢 soul 自身能力，
  heartbeat.py:478）。與清空前 admin 手設映射同源。
- **撞名防禦**：`create_soul` 建立前檢查 `agent_id ∉ {user.id}`，撞則 422。

## 後果

- **正面**：靈魂建立/重建即得 Owner（無需手動設定、無需遷移既有靈魂）；`identity_map`
  進自癒＝fresh checkout／清空重啟不再全塌 Base（DebugAgent 也拿回 Debugger）；
  id 撞名結構性杜絕。
- **相容/低風險**：以**空 DEFAULT map 為基線的既有測試零行為變動**——接線在空
  tag_rules 下是惰性 no-op（`get_role_key` 需 `tag_rules` 含 `is_toukasoul` 才觸發），
  故全套 3300+ 測試不受接線影響；僅 seed 了 map 的新測試看到 Owner。
- **代價/限制**：agent token 驗證（`verify_token`／`get_identity`）多一次
  `identify_agent`（soul-dir stat＋config 讀）——**每 HTTP 請求一次、非每 ReAct 輪**
  （身分在 Gate-1 解析一次、迴圈沿用），且其後緊接多秒 LLM 呼叫，成本被 LLM 延遲
  淹沒；人類 token 於人類分支短路、零 I/O（對抗式審查評 medium，實際 per-request
  影響低，接受不加快取以免引入 staleness）。「靈魂＝Owner」是強預設（全工具/MCP/
  技能）——刻意，靈魂受 admin-only＋步升閘的 create_soul 保護；例外走 `agent_overrides`
  降級。
- **對抗式審查（驗證後 2 項確認）**：①LDH 具名靈魂 Owner 卡（已修，見上
  LDH 不帶旗標）；②per-request I/O（已評估接受＋文件化）。誠實邊界：`IdentityRule`/
  `AgentOverride.role` 在 config 載入層未經 `ASSIGNABLE_AGENT_ROLES` 驗證（僅
  API 邊界 `IdentityMapUpdateRequest` 驗）——手改 live identity_map 塞跨面角色會載入
  成功、延到 `for_role` 才觸 `CrossPlaneRoleError`（**既有、非本案引入**；本案 `.example`
  用合法角色）。
- **既有部署**：下次重啟由 `ensure_configs` 自動建立 `identity_map.json`；admin 過去
  手動設的映射若已存在則 `ensure_configs` 不覆蓋（保護現有）。

## 未納入（先修 B 再評估）

- 技能廣告 RBAC 落差（A1「看得到用不了」）：靜態 dispatcher 廣告全 catalog 但
  `activate_skill` 按 allowed_skills 擋。Sumi 拿回 Owner 後其技能痛點多半消失；
  Base/Guest 的廣告過濾另評估。
- `request_skill_install` 的兩個確認 bug（env_vars payload 形狀錯配、雙重審批卡）——
  獨立於角色，另批。

## 相關

- [ADR-0141](0141-role-plane-separation.md)（human/agent 角色面分離——本案的 agent_role 語意）
- [ADR-0142](0142-soul-crud-repair-and-admin-stepup.md)（靈魂 CRUD＋admin 步升——本案延續其「模型類型→角色」另案）
- [ADR-0144](0144-auth-seam-hardening.md)（verify_token 人類分支——本案 id 撞名防禦的同一短路）
- 診斷：07-11 事故餘波清單 #4-B（HANDOVER）；平行調查＋合成
