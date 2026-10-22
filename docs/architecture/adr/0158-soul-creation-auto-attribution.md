# ADR-0158: 靈魂建立即自動歸屬建立者（owned_agents 建立/刪除/回滾三面對稱）

## 狀態

✅ 已接受（四功能區唯讀調查的 P1 項，維護者 2026-07-15 核准實作；調查另三區＝側欄部署漂移 P0/認證縫 P3/輸入列 P4 各自另案）

## 日期

2026-07-15

## 背景

維護者回報：新建靈魂處於「無歸屬＋（顯示）基礎權限」狀態，須到安全設定手動歸屬到使用者後才能正常運作。唯讀調查拆出三個成分：①`create_soul` 寫四個 SSOT 面（soul 目錄/models.json/agents_registry/brain）但**漏寫 users.json 的 `owned_agents`**——而 `delete_soul`（ADR-0142）反而會逐使用者清 owned_agents＝建立/刪除不對稱，證明是漏寫非設計；②硬斷點＝心跳喚醒守衛 B 對孤兒靈魂直接 `HEARTBEAT_ORPHAN_AGENT` 403（無 admin 旁路），前景代打（ADR-0154）/SafeAuto/審批解算等授權面同樣過不了；③「基礎權限」對靈魂是 **UI 顯示 bug**——ADR-0147 tag_rules（is_toukasoul→Owner）後端確實生效，但 IdentityMapping 表格不懂 tag_rules、無 override 一律顯示 Base，且在該 UI 點擊會寫出**蓋掉 tag_rules 的降級 override**（live 實跡：Sumi 的 override 缺 `can_supervise_dispatch`）。對照組：自訂模型建立路徑**早已**自動歸屬（custom_models IDOR 修復時做的）。

## 決策與實作

- **`create_soul` 成功路徑補自動歸屬**：SSOT 註冊後 `UserService().add_owned_agent(identity.agent_id, soul_identity)`——與 custom_models 建立路徑逐字同型（既有原子原語 `_atomic_users_write`）。歸屬目標＝建立請求的人類 admin JWT sub（create_soul 本就限登入人類 admin；人類的 `identity.agent_id` 即 `user.id`）。
- **D6 回滾對稱**：失敗回滾清單加 `remove_owned_agent`（冪等、`contextlib.suppress` 同款 best-effort）——失敗不得留下指向不存在靈魂的歸屬。
- **刻意不寫 identity_map**：角色面由 ADR-0147 tag_rules 涵蓋（is_toukasoul→Owner）；建立時寫 override 反而製造「override 蓋 tag_rules」的降級風險（Sumi 實跡同型）。
- **測試**（先紅後綠）：`test_create_soul_attributes_creator_owned_agents`（建立→歸屬落盤）＋`test_delete_soul_revokes_attribution_symmetry`（round-trip 對稱錨）＋`test_create_failure_rollback_revokes_attribution`（回滾涵蓋歸屬）＋`test_awaken_orphan_denied_until_attributed`（心跳因果鏈釘測：孤兒拒 `HEARTBEAT_ORPHAN_AGENT`→歸屬後過守衛 B 改死在能力閘）。

## 後果

- 新建靈魂立即可被心跳喚醒（前提：建立者帳號 refresh token 未過期——守衛 B 既有語意）、前景代打/SafeAuto/審批解算即刻可用；「建立完要去安全設定手動歸屬」的步驟消失。
- 測試用假身分（無對應 user）建立靈魂時 `add_owned_agent` 為 no-op（找無 user 即跳過）＝既有測試零變動；生產面 JWT sub 恆為真實 user。
- **後續已落地（owner 續辦，同 ADR）＝IdentityMapping UI 誤導修正**：後端 `agent_service.get_all_agents` 為每個 agent 附 tag_rules-aware 的 `effective_role`/`effective_capabilities`/`has_override`（SSOT get_role_key/get_capabilities_by_id 帶型別旗標，override＞tag_rules＞default 求值；response_model 為 untyped list、openapi 零變）；前端 IdentityMapping 表格對無 override 的靈魂**顯示有效角色（Owner）而非 default_role（Base）**、標「繼承自類型規則」／「已覆寫」、移除鈕僅有 override 才現；關鍵修正＝角色下拉與能力 toggle **以有效值為基底建立 override**（過去 fallback `{role: default_role, capabilities: []}`＝點一下即把 Owner 降 Base、清光能力＝Sumi 掉 can_supervise_dispatch 的路徑），能力清單＝有效能力 ∪ heartbeat_awakening（不再單一硬編碼遮蔽其他能力）。前端 vitest 新增 IdentityMapping.test.tsx 6 測（含「toggle 一能力其餘必保留」的降級回歸錨）。
- **仍未納入（另行列出）**：live identity_map 裡 Sumi 的降級 override 清理（data/ 檔案，需人工處理）；自訂模型 Guest 預設是否放寬（設計決策）；`delete_user` 不清其 owned_agents 指向的靈魂（既有邊角）。
- **驗收**：靈魂家族測試 50 passed（含歸屬 4 條）＋後端 effective 斷言／pytest 全套 3535 passed／前端 security vitest 15 passed（IdentityMapping 6＋SecurityTab 6＋AutonomyBudget 3）／guard_all 41/41（含 #4 design token、#33 i18n 對稱）／lint_ssot clean／ruff 零新增／tsc 零新增（44≤基線 45）。

## 相關

- [ADR-0142](0142-soul-crud-repair-and-admin-stepup.md)（delete_soul 清理面＝本案對稱的另一半）
- [ADR-0147](0147-soul-role-type-binding.md)（tag_rules 類型硬綁定＝角色面毋須 override 的依據）
- [ADR-0154](0154-foreground-soul-identity-assumption.md)（前景代打的 owned_agents 授權閘＝本案受益面之一）
