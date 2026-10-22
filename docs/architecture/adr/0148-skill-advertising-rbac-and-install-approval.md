# ADR-0148: 技能廣告 RBAC 過濾（廣告面＝授權面）＋ request_skill_install 審批雙修

## 狀態

✅ 已接受（07-11 認證事故餘波清單 #4 剩餘項；維護者決定先修 B 再評估後執行）

## 日期

2026-07-12

## 背景

Sumi/grok-4.5 診斷的「看得到用不了」第三例，以及 skill 安裝「批准了卻失敗」，
源碼追蹤確認為**三個獨立**缺陷（前兩者關於 activate、後者關於 install）：

1. **A1 — 技能廣告不查 `allowed_skills`（廣告面 ≠ 授權面）**：
   - 廣告面：`sync_skill_dispatcher` 產生的 `<skill_registry>` 列**全 catalog**（靜態
     全域 SKILL.md），`SkillLayer` 對每個 agent 無差別注入；`tool.py:126-127` 只要
     dispatcher 在 active_skills（恆成立）就**無條件補回** `activate_skill` 工具
     （繞過同檔 118-121 的 `allowed_tools` 過濾）。二者都不查 `allowed_skills`。
   - 授權面：`activate_skill`（system.py:343）執行時才查 `can_use_skill`
     （`role.allowed_skills`）。permissions.json 中 Owner=`["*"]`、Base/Guest=`[]`。
   - 結果：Base/Guest 看得到完整清單＋拿得到 `activate_skill`，activate → dangerous
     → CONFIRM → 審批卡 → 人類批准 → **才**撞 `can_use_skill` RBAC 被拒＝審批被白白
     浪費。（ADR-0147 後 Sumi=Owner 已無此痛；本案針對刻意受限的 Guest/Base。）

2. **install Bug B — env payload 形狀錯配（真失敗）**：`useEvolution.confirmInstall`
   送 `{ env_vars }`，但 `/terminal/approve` 以 `TerminalApprovalRequest` 解析、只有
   `payload` 欄位 → pydantic 丟棄 → `env_vars={}` → MCP server 無金鑰啟動 → 安裝回
   Success 但 runtime 失敗（「安裝了、設了、就是不能用」）。

3. **install Bug A — 雙重審批**：`request_skill_install` 是 broker 上線後唯一
   「Gate-2 已 CONFIRM，本體又再喊一次 `terminal.request_approval`」的工具（不看
   `_hitl_preapproved`）。Manual/SafeAuto 下上下各一張互動卡（broker ToolApprovalCard
   ＋EvolutionModal），兩個行程內 registry、兩個端點；使用者批了一張、沒處理另一張
   → 另一通道空等 180s → 自動拒。

## 決策

1. **A1 治本（廣告面依 `can_use_skill` 過濾）**：`SkillLayer` 算出此身分「用得到」的
   技能集合（`usable_skill_ids`）——**唯有非空才注入 dispatcher**（連帶 `tool.py` 才
   廣告 `activate_skill`），且 `<skill_registry>` **只列 usable 技能**。無可用技能的
   角色（Base/Guest，`allowed_skills=[]`）自此**看不到** registry、拿不到 `activate_skill`。
   廣告面自此與授權面同判準。（產品意圖：受限角色不該被廣告用不了的技能；若要開放
   Base/Guest 某些技能，改 `permissions.json` 的 `allowed_skills` 即可，二者仍對齊。）
2. **install Bug B（前端 payload 形狀）**：`confirmInstall` 改送
   `{ payload: { env_vars } }` 對齊 `TerminalApprovalRequest`。
3. **install Bug A（雙重審批）**：`request_skill_install` 的 EvolutionModal 是**更豐富
   的單一審批**（權限＋env 輸入）。標記為 `tool_executor._SELF_APPROVING_TOOLS`——
   CONFIRM 時**跳過 broker**（不雙重審批）；由工具本體自 gate：互動（`_interactive=True`）
   走自帶 EvolutionModal，headless（`_interactive=False`）**立即 fail-closed**（不呼叫
   `request_approval` 空等 180s，H1 headless 語意保持）。

## 實作

- `skill_service`：抽 `_render_dispatcher_body(skill_list_xml)`（靜態 sync 與
  per-request 共用 SSOT）＋新 `render_skill_registry(skill_ids)`（只列給定 id、系統
  技能/未知 id 不列、無項回空字串）＋抽 `append_system_prompt(request, text)`。
- `SkillLayer`：`usable_skill_ids = [can_use_skill 過濾]`；`has_dispatcher = bool(...)`；
  dispatcher/registry 條件注入；業務技能 body 照舊經 `apply_skills_to_request`。
- `tool_executor`：`_SELF_APPROVING_TOOLS`＋CONFIRM 分支 `name not in _SELF_APPROVING_TOOLS`
  才走 broker；call_kwargs 加 `_interactive`。
- `request_skill_install`：`if not _interactive: return fail-closed`（在呼 request_approval 前）。
- `useEvolution.confirmInstall`：body `{ payload: { env_vars } }`。

## 後果

- **正面**：受限角色不再被廣告用不了的技能，審批不再被浪費；skill 安裝 env 正確
  寫入（真能用）；install 單一審批卡（EvolutionModal），無雙卡混淆/逾時。
- **相容/低風險**：Owner（souls）`allowed_skills=["*"]` → usable=全部 → 見到與過去
  相同的完整 registry（行為不變）；只有 Base/Guest 從「見全 catalog」變「見不到」。
  快取（ADR-0127/0129 技能提示前綴）：Owner 內容不變、Base/Guest 少一段——非退化。
- **代價/限制**：dispatcher registry 從「啟動時 sync 的靜態全域檔」改為 SkillLayer
  per-request 依角色渲染；靜態 SKILL.md 仍存在（供 UI/catalog），只是不再是注入來源。
  install headless 從「broker fail-closed」改為「工具本體 fail-closed」（同結果、
  不同訊息，且不再有雙卡）。

## 對抗式審查（1 medium + 3 low + 1 PASS，2 誤報駁回）

- **已修（low）**：裸注入的 skill_registry 未歸 `<capabilities>` → context_breakdown 記
  system_other。改以 `dispatcher_body` 併入 `apply_skills_to_request` 的同一
  `<capabilities><skill name="system-skill-dispatcher">` 區塊——**結構與舊制一致**
  （只是 registry 已 RBAC 過濾），歸因正確。
- **另案（medium，既有、非本案引入）→ 已於 [ADR-0149](0149-evolution-deploy-approval-wiring.md)
  修復**：`evolution_implement_and_verify(submit_for_deploy)` → `request_mcp_creation` 用
  `request_approval(skill_creation_approval_request)` 自帶審批卡，與 request_skill_install
  同構的雙卡反模式，且該事件型別**不在 ToukaChat 白名單** → 前端丟棄 → 180s 逾時
  auto-reject＝互動部署靜默失敗。**未納入本案**（out of ADR-0148 scope＝request_skill_install；
  完整修需前端創建審批 UI）；已 spawn 跟進任務，ADR-0149 比照本案範式雙修收官。
- **誠實邊界（low，接受）**：①`authorized`（get_skill 讀磁碟）與 `usable`（list_skills
  讀 `_metadata`）異源——多 worker／install-race 視窗可致「已注入卻未列於 registry」的
  短暫顯示不一致（非安全、授權正確）；②`usable_skill_ids` 每請求全掃技能＋逐技能取
  `_config_reload_lock`——成本較舊制放大但為 in-memory、低 QPS、被 LLM 延遲淹沒。

## 相關

- [ADR-0143](0143-hitl-unification.md)（HITL 統一 broker——本案 install 收斂延續其
  「通道收斂待另案」；request_skill_install 自帶審批不遷 broker，改跳過 broker）
- [ADR-0147](0147-soul-role-type-binding.md)（先修 B：souls→Owner 後 A1 只剩 Base/Guest）
- [ADR-0066](0066-skill-system-anthropic-standard-migration.md)（技能 Pull 模式 dispatcher）
- 診斷：平行調查（skill-advertising / skill-install-approval facets）
