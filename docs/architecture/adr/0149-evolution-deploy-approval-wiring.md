# ADR-0149: 互動演化部署審批雙修——skill_creation_approval_request 跳過 broker＋接進 ToukaChat

## 狀態

✅ 已接受（ADR-0148 對抗式審查揪出的既有缺口，medium；非 ADR-0148 引入故未納入該批，另立本案修復）

〔[ADR-0254](0254-iso-c1-small-fixes-and-execution-gaps.md) 修訂：`evolution_implement_and_verify` 不再列入自帶審批（建 venv、pip、匯入代理人寫的 `server.py` 先過一般審批卡，無人在場由 executor 拒絕），推翻本 ADR「語意界線（headless）」那段「沙盒建置與驗證在 headless 可執行、非安全退化」的取捨；`request_mcp_creation` 的部署卡與 headless fail-closed 不變。〕

## 日期

2026-07-12

## 背景

ADR-0148 的對抗式審查標記一個**既有**缺陷（非 0148 引入）：互動聊天會話
（Manual/SafeAuto）中，`evolution_implement_and_verify(submit_for_deploy=True)` 沙盒
驗證通過後，部署會**雙重審批＋靜默失敗**——

1. **雙重審批（卡一＋卡二）**：`evolution_implement_and_verify` 分類 dangerous
   （`auth_policies.json`）→ `tool_executor` 先彈統一 broker 卡
   （`tool_approval_request`，卡一），人類核准；執行中委派
   `request_mcp_creation`（雙軌 shim），其**無條件**呼
   `request_approval(type="skill_creation_approval_request")`（卡二，帶 `source_code`），
   與 `request_skill_install` 同構的自帶審批卡，但**未納入
   `_SELF_APPROVING_TOOLS`**、也不看 `_interactive`。

2. **靜默失敗（卡二被前端丟棄）**：`skill_creation_approval_request` 型別**不在
   ToukaChat 事件白名單**（`connections.ts` 的 `dispatchSseLine`
   `customEventTypes` 只列 `skill_install_approval_request` 等）→ 前端無 handler →
   事件被丟 → 無人解算 → `request_approval` 180s 逾時 auto-reject → 回「Rejected:
   User denied the creation」。

即：使用者批了卡一，卻等 180s 收到「你拒絕了」，**互動部署事實上不可用**。修法與
ADR-0148 的 `request_skill_install` 同構（自帶審批跳過 broker＋前端白名單＋
headless fail-closed），故兩步缺一不可。

## 決策

比照 ADR-0148 的 `request_skill_install` 範式，兩步同修：

1. **消除雙重審批（後端）**：`evolution_implement_and_verify` 與 `request_mcp_creation`
   雙雙納入 `tool_executor._SELF_APPROVING_TOOLS`——CONFIRM 時**跳過 broker**（不再
   彈卡一）；部署審核收斂為 `request_mcp_creation` 自帶的**單一** `EvolutionModal`
   審批卡（呈現 `source_code` 供審）。`request_mcp_creation` 為雙軌 shim、可被模型
   獨立調用，故一併列入並於本體 self-gate。

2. **headless fail-closed（後端）**：`request_mcp_creation` 於本體最前置加
   `_interactive` 閘——headless（heartbeat／派工，無人可審熱部署）**立即
   fail-closed**，不呼 `request_approval` 空等 180s（H1 headless 語意保持；接手
   broker 原本 `command not in args` 的 fail-closed 職責）。

3. **接進 ToukaChat（前端）**：`skill_creation_approval_request` 納入
   `dispatchSseLine` 白名單，並與 `skill_install_approval_request` **同走
   `onSkillInstallRequest` → `EvolutionModal`**（該 modal 本已支援 `source_code`＋
   `is_creation` 分支，只是事件過去從未被路由到它）。

## 實作

- `tool_executor._SELF_APPROVING_TOOLS` ＋= `evolution_implement_and_verify`,
  `request_mcp_creation`。
- `evolution.request_mcp_creation`：本體最前置 `if not _interactive: return`
  fail-closed（在讀檔／`request_approval` 之前）。
- `connections.ts`：`customEventTypes` ＋= `"skill_creation_approval_request"`。
- `StreamRegistryProvider.onEvent`：`skill_install_approval_request ||
  skill_creation_approval_request` → `onSkillInstallRequest`。
- `EvolutionModal`：「Step 2 指令預覽／安全掃描」以 `installRequest.args` 條件渲染
  ——部署事件無 `args`（launch command 於核准後才組出），審核標的是 `source_code`；
  同時避免 `args.join` 崩潰。
- `types.SkillInstallRequest`：`args`／`permissions`／`env_requirements` 改選填
  （部署事件不帶）。
- `test_functional_bugs_security`（DA19）：`request_mcp_creation` 現於 headless
  fail-closed，DA19 驗的「`request_approval` 收 `identity` 非 `agent_id`」只在互動
  路徑觸發 → 該測試補 `_interactive=True`（不變式與新 gate 正交）。

## 後果

- **正面**：互動部署單一審批卡（`EvolutionModal`，呈現 `source_code`），批准即熱
  部署、不再逾時靜默失敗；headless 部署明確 fail-closed（不空等 180s）。
- **相容/低風險**：安裝流程（`skill_install_approval_request`）行為不變（同一路由、
  同一 modal、`args` 照舊渲染 Step 2）。`SkillInstallRequest` 三欄改選填僅為型別誠實，
  執行期 `EvolutionModal` 早有 `&&` 守衛。
- **語意界線（headless）**：`evolution_implement_and_verify` 納入
  `_SELF_APPROVING_TOOLS` 後，broker 不再擋其 headless 執行；**部署**（唯一動到 live
  系統的側效）仍由 `request_mcp_creation` 的 `_interactive` 閘 fail-closed，安全不變式
  （無人不熱部署）保持。沙盒 write+verify（隔離、不動 live 系統）在 headless 可執行
  ——此為 `_SELF_APPROVING_TOOLS` 名單機制的既定結果，非安全退化。

## 對抗式審查（未變之處的釘死）

- `request_mcp_creation` 自帶審批卡型別＝`skill_creation_approval_request` 且帶
  `source_code`／`emit`（前端據此渲染、且同步推進當前聊天 SSE）——單元測試對釘。
- 兩工具在互動 CONFIRM 下**不**再彈 `tool_approval_request`（broker 未預核准，
  `pending_count==0`）；headless 下跳過 executor fail-closed、`_interactive=False`
  下放工具本體——executor 層測試對釘。

## 相關

- [ADR-0148](0148-skill-advertising-rbac-and-install-approval.md)（本案範式＝
  `request_skill_install` 自帶審批跳過 broker＋前端白名單＋headless fail-closed；其
  對抗式審查標記本缺口為「另案」並 spawn 本跟進案）
- [ADR-0143](0143-hitl-unification.md)（HITL 統一 broker——`_SELF_APPROVING_TOOLS`
  即其 CONFIRM 分支的例外通道）
- [ADR-0119](0119-unified-tool-interface-redesign.md) 系（演化管線 4→2：
  `evolution_define` → `evolution_implement_and_verify`，舊四步保留為 shim）
