# ADR-0137: 對抗式互審子系統——證據接地紅隊互審（Phase 3 本體）

## 狀態

✅ **已接受（實作與驗證 2026-07-07；擁有者尚未 commit）**。[in-process 多代理協調提案](../proposals/in-process-subagent-orchestration.md) **Phase 3 本體**＝需求 2（對抗式互審），依[設計文件](../proposals/adversarial-review-subsystem-design.md) §8 八步 TDD 落地（前置 eval 軌為 [ADR-0136](0136-review-eval-rails.md)）。

## 日期

2026-07-07

## 背景

母提案唯一「全新建」塊。設計階段的核碼結論：ModelJudge 是協作打分非對抗、同腦坍縮是本 repo 預設實況（`brain_persistence.py:84`）、`save_mission` 是繞 gate 後門、既有指標與 artifact 內容幻覺不同軸。故互審**必須**把可信度來源移出模型（證據接地）、把 gate 做成落盤不變式、隔離用結構化 gate 而非模型自律。6 個未決設計點已由維護者決定（設計文件 §5）。

## 決策

1. **Reviewer 介面**（[`services/review/reviewer.py`](../../../ToukaProxy/src/services/review/reviewer.py)）：泛化 `Judge` Protocol（`Judge`/`NoopJudge` 原地保留供 creation EVAL）。`CritiqueReport`（verdict/required_fixes/grounded/confidence，frozen pydantic）＋`EvidenceBundle`/`CmdResult`（客觀證據）。`GroundedReviewer` 一次 completer 呼叫（DI，非子 ReAct loop）＋`parse_critique` **fail-closed**：無 json 塊／壞 JSON／verdict 缺失／APPROVE 附 blocking fixes（矛盾）／APPROVE 低 confidence／completer 例外——一律 REJECT（gate 語義，與 ModelJudge fail-safe 0.0 相反）。
2. **EvidenceCollector**（[`services/review/evidence.py`](../../../ToukaProxy/src/services/review/evidence.py)）：**純資料蒐證**（非 LLM）——跑 `verification_cmd` 取 exit code（沿用 terminal 核可模式 `asyncio.create_subprocess_exec`：shell=False、沙盒 cwd、硬 timeout）、核對 artifact 存在、節錄 test_log。**反橡皮圖章**：拒 no-op 前綴（`true`/`echo`/`exit 0`/`:`）、pytest 零收集（exit 0 但「collected 0 items」）判硬失敗、`has_passing_assertions`（N passed 且 N>0）。
3. **Task 九審查欄位＋雙層 gate**（[mission model](../../../ToukaMemory/src/memory_server/models/mission.py)／[manager](../../../ToukaMemory/src/memory_server/services/mission_manager.py)）：`review_status`/`critic_verdict`/`reviewer_id`/`review_attempts_total`/`evidence_digest`/`review_required`/`review_locked`/`verification_cmd_locked`/`review_risk`（與 TaskStatus 正交）。**第一層落盤不變式**：`_save_mission_unlocked` 對 `status==DONE` 且 `review_required` 的 task 斷言 `review_passing`（APPROVED 恆過；LOW_CONFIDENCE 僅 normal 風險過）——堵 `save_mission` 後門；mission-DONE 聚合同步改讀 review 欄位。**第二層隔離寫路徑** `record_review`：`can_review` capability＋`reviewer_id≠看板擁有者`（堵自簽）＋僅限 REVIEW_STATES。**TOCTOU**：`update_task` 帶內容參數（artifact/test_log）時，既有 APPROVED* 立即 stale 化（防先蓋 PASS 再改產出）。**cmd 上鎖**：init 帶 cmd 即 `verification_cmd_locked=True`，落盤時被剝除即 raise。
4. **預算四道硬界**（[`services/review/budget.py`](../../../ToukaProxy/src/services/review/budget.py)）：往返 `GLOBAL_REVIEW_CAP=3`（決策 1，`effective_review_cap` 只能收緊不能放寬）；`MAX_REVIEW_DEPTH=1`（兩層：主 depth0 可開子、子不可再開孫）；`ReviewBudgetLedger` mission 級 spawn 記帳＋**單一 mission lock 防 lost-update**（**刻意不用** rhythm_service——其 `initialize_session` 對同 request_id 無條件重設、結構性聚合不了）；`cost_ceiling_exceeded` 互審路徑**無視 `cap.enabled`**（成本閘不可關，與 `is_over_budget` fail-open 相反），無數字＝誠實不擋。
5. **review_loop 薄層**（[`services/review/review_loop.py`](../../../ToukaProxy/src/services/review/review_loop.py)）：驅動單輪 FSM——永鎖／深度／成本／**軟任務**（無可執行證據→human-ack，不靜默放行）／**隔離**（高風險＋同模型→不開審直接 BLOCKED，fail-closed 省成本；同模型＋normal→APPROVE 降 `APPROVED_LOW_CONFIDENCE` 顯式不背書）先擋；**證據優先**（意見 APPROVE 推翻不了硬檢查失敗→REVISING）；REJECT 走停滯偵測（連續兩輪成功解析同組 blocking fixes→早停）→attempts 達 cap→BLOCKED＋review_locked。結論每輪落板（跨重啟可續）；**臨時審查身分不落盤**（不呼 `record_working_brain`）；critic 無工具面＝能力面根絕遞迴。
6. **模型面入口** `mission_tools(action="request_review", task_id, producer_claims?, critic_model?)`：讀板→組 completer（ctx anthropic_client，v1 同 episode 限制）→跑一輪→回 review_status＋next_hint。廣告數學不變（mission_tools action enum 擴充）。

## 後果

- **需求 2 骨架完成**：主 agent 可 `dispatch_subagent`（Phase 1）→ 拿結構化回報（Phase 2）→ `request_review`（本 ADR）→ 依 verdict 收案／退修／轉人；gate 物理擋在落盤層。三 ADR（0134/0135/0137）疊成完整 producer→report→review→gate 鏈。
- **邊界（誠實，未做即說）**：①**互審有效性尚未實測**——ADR-0136 的量測軌就位、但注錯案例集＋五臂 A/B（設計 §7.2/7.3）需 LLM 實跑（擁有者手動、案例人類 commit）；**未達預註冊門檻前不得宣稱「降幻覺」**；②critic 傳輸 v1 走 anthropic_client（Claude 原生）——異模型隔離的完整實現（OpenAI 相容 critic 傳輸）為後續項，現階段異模型不可用時 fail-closed 轉人；③往返的「producer 修正重跑」由主 agent 決策驅動（工具回 REVISING→主 agent 改產出後再 request_review），非全自動循環；④跨身分看板路徑既有歧異（`_PathIdentity` 角色猜測）已另立背景任務追蹤，本 ADR 的 record_review 沿用既有權限牆不擴大攻擊面。
- **驗收**：ruff（改動檔）＋lint_ssot＋**guard_all 39/39**＋**後端 pytest 全綠**（新增 test_reviewer 14／test_review_gate 15／test_evidence_collector 8／test_review_budget 8／test_review_loop 10＝55 測，含後門繞過／自簽／TOCTOU／同腦降級／證據優先／停滯／cap 紅隊）＋前端 vitest 971（前端零改動）。NOQA registry 同步。

## 補記（2026-10-09）：verification_cmd 執行邊界

[ADR-0239](0239-authorization-gateway-and-execution-broker.md) 的唯讀盤點發現決策 2 的「沿用 terminal 核可模式」只沿用了 `shell=False`，
沒有沿用 terminal 的其他邊界：子程序繼承完整 Proxy 環境（`TOUKA_AUTH_SECRET`、
`DISPATCH_TOKEN`、provider API key）、沒有行程群組、取消時不殺子程序，而且
`request_review` 屬 `safe` 類，SafeAuto／FullYOLO 下不經 HITL、也沒有逐指令政策。
`verification_cmd` 由 agent 在 initialize 時撰寫，SafeAuto 下沒有人看過它。

決策 2 改為「與 `execute_command` 同一套指令政策與執行邊界」（SECURITY.md SP1）：

1. **指令政策**：`collect_evidence` 必須帶 `command_gate`。正式路徑由
   `policy_command_gate` 依 `permission_service.classify_command`（終端政策＋C5
   串接字元升級，與 terminal 共用 SSOT）判定：ALLOW 執行；CONFIRM 只有在本次
   `request_review` 已經 Gate 2 人工核准（`_hitl_preapproved`）時執行；DENY 與未知
   決策一律不執行。被擋的指令記為 `CmdResult.rejected_reason`，硬檢查失敗，不會被
   靜默略過。`run_review_round` 未帶 `auth_level` 時預設 Manual 且未核准（fail-closed）。
2. **執行邊界**：經 ADR-0239 的 `execution_broker.spawn_exec(..., new_process_group=True)`
   啟動；子程序只拿 `EvolutionPathGuard.get_clean_env()`，逾時或取消時
   `terminate_process_tree` 連坐殺孫進程。
3. **cwd**：personal vault 解析不到時 `request_review` 直接回錯誤，不再退回 Proxy 行程的
   cwd。

取捨：Manual 互動會話核准 `request_review` 時，審批卡只顯示 `task_id`／`producer_claims`，
不顯示 `verification_cmd`；此時放行的依據是該指令在 initialize 時（Manual 下同樣需要人工
核准、且指令就在參數內）已被看過並上鎖。若之後要更嚴格，可在審批卡直接列出指令。

## 補記（2026-10-12）：verification_cmd 逐條人類審批（SECURITY.md B2、A7）

上一則補記的取捨（Manual 下核准 `request_review` 即放行卡上看不到的指令，依據是 initialize
時已看過）不成立：initialize 卡的 `tasks` 參數被截到 200 字、前端再單行省略，人其實看不到
後面任務的指令。模型溝通層第 2b 段（[ADR-0246](0246-model-layer-stage-2b-2c.md)）改為：

1. **逐條審批**：`policy_command_gate` 改為 async，不再接受 `_hitl_preapproved`，改收可選的
   `approver`（`Callable[[str], Awaitable[str]]`，回審批結果）。CONFIRM 時有人在場（`_interactive`）就為
   **那一條指令**跳一張卡，步驟 id 用 `<request_review 的 id>:verify:<n>`（n 依問人順序從 1 起），
   核准才執行；拒絕、逾時、佇列滿都不執行。無人在場或在子代理裡（子 loop 只收文字、卡送不到人，
   子 ctx 帶 `subagent_run`）時 approver 為 None，維持原本 fail-closed（不跳卡、不執行）。
   核准後審查若中途拋例外，已開的步驟以 `internal_error` 收尾，例外照常往上。
   指令先解析得出才過政策閘，不為跑不起來的指令跳卡。
2. **共用審批流程**：登記 → 推卡 → 等決定 → 推解算抽成 `services/approval_flow.py`；
   `ToolExecutor._await_human_approval` 保留同名方法並委派給它，互審用其中的
   `CommandApprover`（evidence 只依賴 `approver(cmd) -> 審批結果`）。`_hitl_preapproved` 仍只給
   terminal 跳過自帶審批。
3. **完整指令**：審批卡的 `command` 參數不再截到 200 字，上限 8 KB（`APPROVAL_COMMAND_PREVIEW_LIMIT`），
   超過時附明確截斷標示；ToukaChat 審批卡改為換行顯示。

使用者看得到的改變：Manual 下互審要跑指令時有兩張卡（先 `request_review`，再那條指令）；
SafeAuto 互動對話多一張指令卡（以前一定失敗）。無人在場時的收尾（不呼叫 critic、不扣次數）
不在本補記範圍。

## 相關

- [設計文件：對抗式互審](../proposals/adversarial-review-subsystem-design.md)（§4 規格＝本 ADR 實作；§5 六決策；§7 eval）
- [ADR-0134](0134-in-process-subagent-dispatch.md)（producer 子 loop）／[ADR-0135](0135-structured-report-finalize-and-board-bound-stop.md)（回報＋finalize＋停止 gate）／[ADR-0136](0136-review-eval-rails.md)（eval 量測軌）
- [ADR-0239](0239-authorization-gateway-and-execution-broker.md)（ExecutionBroker；2026-10-09 補記的子程序執行邊界）
- [ADR-0045](0045-mission-board-fsm-and-artifacts-decoupling.md)（mission FSM，gate 疊加不改拓撲）；[ADR-0069](0069-touka-native-creation-engine.md)（Judge Protocol 來源）；[ADR-0125](0125-long-running-command-monitor.md)（`create_subprocess_exec` 核可模式）
