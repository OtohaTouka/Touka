# 提案：Touka 內部多代理工作流協調（In-Process Sub-Agent Orchestration，「Ultracode 強化版」）

> 狀態：**🏁 四階段全數落地／收官（2026-07-07）。** Phase 1 [ADR-0134](../adr/0134-in-process-subagent-dispatch.md)／Phase 2 [ADR-0135](../adr/0135-structured-report-finalize-and-board-bound-stop.md)／Phase 3 前置 [ADR-0136](../adr/0136-review-eval-rails.md)＋本體 [ADR-0137](../adr/0137-adversarial-review-subsystem.md)／Phase 4 [ADR-0138](../adr/0138-parallel-fanout-and-resumable-delivery.md)。guard 39/39、後端 pytest 2869、前端 vitest 971。這是「ReAct loop ＋ ToukaDispatch 的加強版，但任務**不外派、整個在 Touka 內部（in-process）完成**」——對標 Claude Code 的 workflow／ultracode，並強化為「主 agent 動態協調子代理＋對抗式互審＋持久可續」。
>
> **收官後仍待維護者手動處理或明確啟動（非核心可行性，見各 ADR 誠實邊界）：** ①**互審有效性五臂 A/B 實測**（設計文件 §7.2/7.3，注錯案例集由維護者 commit；**未達預註冊門檻前不宣稱降幻覺**）；②異模型 critic 的 OpenAI 相容傳輸（現 v1 走 anthropic_client）；③**自動背景續跑驅動**（ADR-0078 #8，明確劃出 Phase 4 範圍外＝另一提案）；④live E2E／episode 實跑（`scripts/run_tool_eval_episode.py`）。
>
> 約束：**由維護者 commit**；只用 **DebugAgent** 身分驗證、不動其他 agent 資料；**嚴禁自動修改 `data/`**；每步過 ruff＋lint_ssot＋guard_all(39)＋pytest＋vitest。
>
> 依據：**2026-07-06 的平行程式碼調查**涵蓋七子系統（ReAct 迴圈／ToukaDispatch／多代理原語／Mission Board／複審機制／持久化與可續跑／自主預算）＋收斂 synthesis。本提案的「現況盤點」為該調查的實地程式碼結論。

---

## 1. 北極星（要解決什麼）

讓對話中的**主 agent** 能把一個複雜任務**拆成多步、動態派多個 subagent 執行、彼此互審降幻覺、逐步驗收並持久記錄**，全程在 Touka 內部完成、不外派給外部 AI。兩個使用目標：

- **(a) 複雜任務防偏移**：任務需拆成很多小步驟、又不能偏移原始目標時，用結構化的「拆分→執行→互審→驗收→續下一步→最終總檢查」把注意力鎖在軌道上。
- **(b) 弱模型也不失敗**：能力較弱的開源模型，靠這套**外部結構化 gate**（而非靠模型自律）在一般任務中不偏移、不幻覺、不半途而廢。

一句話定位：**不是重寫一套，而是「地基九成都在，補三條黏合線＋蓋一間互審房」。**

---

## 2. 目標功能規格（原始需求）

| # | 需求 |
|---|---|
| 1 | 主 agent 能**動態協調、派出多個 subagent**，並**記錄任務拆分的步驟與每步的目標**。 |
| 2 | subagent 之間需**對抗式複審或類似互審**，以減少幻覺、提高產出正確率。 |
| 3 | subagent 的回報需**持久化紀錄**，避免網路斷連或 token 額度用完，才能**接續工作**。 |
| 4 | 主 agent 看完報告，**覺得該步完成就記錄並繼續下一步**；覺得未完成／有錯／需再確認，**可自己接手或再度開 workflow**。 |
| 5 | 每步都走同樣流程，**全部完成後主 agent 最終檢查、確認達成原始任務**。 |

---

## 3. 可行性判定

**判定＝`mostly-existing-plus-glue`（大部分現成 ＋ 三條關鍵黏合線 ＋ 一塊全新互審）。不需要大重構。**

- 需求 **1/3/4/5 的地基已成熟、可直接重用**：ReAct 主迴圈、Mission Board（FSM＋依賴 DAG＋原子落盤）、預算/配額閘、`asyncio` 等待語義、`group.py` 已示範的 in-process 多身分執行。
- 真正要**全新做的只有需求 2（對抗式互審）**——全庫只有 `ModelJudge`（單次打 0–1 分）這個種子，沒有 critic 角色、往返狀態機、審查落盤欄位、接回 FSM 的 gate。
- 既有的**外派 ToukaDispatch 路徑可原地保留**；in-process 版是**新增一條分支**而非改寫既有（降低回歸風險）。
- 不選 `significant-new-build`（4/5 需求骨幹都在）；不選 `major-refactor`（既有元件幾乎不用拆改，多為擴充）。

---

## 4. 現況盤點：可直接當骨幹重用（幾乎不用改）

| 元件 | 位置 | 角色 |
|---|---|---|
| **`handle_claude_request` while-True ReAct 迴圈** | `ToukaProxy/src/handlers/claude_handler.py:191-361` | 主 agent 引擎：模型↔工具反覆、併發工具派發、自主續命、三路終止、防幻覺打臉（363-396）。**加一個 agent-as-tool 即可在此派子代理。** |
| **`get_stream_generator`** | `ToukaProxy/src/services/dispatchers/factory.py:23` | 以 `(model, request, identity, request_id, context)` 產生任一 agent 的完整 SSE 串流＝「開一個子 ReAct loop」的核心入口，可被工具內部呼叫。 |
| **`MissionManager` ＋ mission 資料模型** | `ToukaMemory/.../services/mission_manager.py`、`models/mission.py` | 任務拆分/狀態/持久化 SSOT：FSM（`VALID_TRANSITIONS`）＋依賴 DAG＋環偵測＋`MissionLock` 雙鎖＋原子寫入＋per-task `artifact_refs`/`test_log`/`verification_cmd`/`boundary_rules`。與 in/out-of-process 無關。 |
| **`mission_tools` ＋ `render_mission_block`** | `ToukaProxy/src/tools/mission_tools.py`、`middleware/layers/context.py:46` | 看板 CRUD（initialize/update_status/get_board/await_dispatch_result）＋每輪把看板重注入 system（`ToolLayer._inject_mission_awareness`，對抗多輪注意力流失）。 |
| **`DispatchCallbackStore`** | `ToukaProxy/src/services/dispatch_callback_store.py` | `register`/`deliver_result`/`wait_for_result`＝「主迴圈掛起等子任務結果」的語義。in-process 版只需加一條**直呼 deliver 路徑**繞過 HTTP。 |
| **`dispatch_group_stream`** | `ToukaProxy/src/services/dispatchers/group.py:26-343` | **唯一 in-process 多身分執行示範**：建多個 `AgentIdentity`＋各自 brain/soul＋呼 `get_stream_generator`＋輸出累進。**fan-out 協調器的最佳改造起點。** |
| **持久化三層** | `async_db.py`（SQLite 對話樹）、`resource_manager.py:515`（原子寫入）、`autonomy.py`（配額跨日重置＋崩潰 in-flight 標記＋retrigger） | 需求 3「斷線/額度續接」的成熟基建（ADR-0078）。 |
| **`RhythmService`** | `ToukaProxy/src/services/rhythm_service.py` | slots 預算、`should_stop_react`、`interrupt`/`is_interrupted`、`nudge_prompt`＝每步回合預算與停止閘。 |
| **輕量身分** | `models/identity.py`、`brain_persistence`、`soul_service` | `AgentIdentity(agent_id)` 零註冊即可生一個臨時 subagent，成本極低。 |

---

## 5. 逐需求映射：現有支援 / 缺口 / 工作量級

| 需求 | 現有支援 | 缺口 | 量級 |
|---|---|---|---|
| **1** 動態派多 subagent＋記步驟目標 | ReAct 迴圈＋工具併發；`mission_tools` 記錄 goal/步驟/依賴/boundary_rules，原子落盤 | **無 in-process「agent-as-tool」原語**（全樹 grep 無工具開子 loop）；無「同時派多個」協調器；Task 是扁平 DAG 非巢狀樹；無「協調決策軌跡/派工日誌」 | **黏合** |
| **2** 對抗式互審 | 幾乎為零。種子＝`ModelJudge`（單次打分，證明可插拔 Judge Protocol 已被接受）；`tests/tool_eval/metrics.py` 的 pass^k／幻覺率可當驗收；`build_validation_protocol` 可當 reviewer prompt 藍本 | 無 critic 角色、無 producer↔reviewer 往返狀態機、無紅隊 persona 隔離、審查結論無落盤欄位、不接回 FSM gate；`ModelJudge` 只回 float 且綁死 creation 命名空間 | **全新** |
| **3** 回報持久化、斷線/額度續接 | **最強一項**：看板經雙鎖＋原子寫入耐久落盤（**先落盤後通知**）；SQLite 對話樹；配額跨日重置＋崩潰標記＋一鍵 retrigger | 「即時交付通道」`DispatchCallbackStore` 是純記憶體 `asyncio.Event`，**重啟即失→正在等的 await 掛不回**（結果雖已落盤）；ADR-0078「背景迴圈自動續跑」仍 TODO（現為人工 retrigger） | **黏合** |
| **4** 看報告決定續/接手/重開 | ReAct 迴圈天然支援；看板 FSM（DONE 終端不可改、FAILED→TODO 需特權）＋每輪看板重注入；retrigger＝再開 workflow | 子代理回報只有**純文字摘要**→主 agent 難程式化裁決「真的完成沒」；需結構化回報 schema | **黏合** |
| **5** 每步同流程＋最終總檢查 | while-loop＋slot 續命＝每步同流程引擎；依賴 gate 強制順序＋「全 DONE→mission DONE」自動判定；防幻覺打臉可併入 | **無 mission 級最終驗收原語**（all-DONE 只是機械式 `all(status==DONE)`）；停止條件靠**子字串比對關鍵字**＋預算→弱模型易「亂喊完成」提前停（正是目標 b 要防的偏移） | **黏合** |

---

## 6. 誠實邊界（寫死避免範圍蔓延）

1. **對抗式互審的有效性必須「實測」證明，不能假設**：`ModelJudge` 是**協作式打分**非對抗式；proposer 與 critic 若**同模型同 persona＝同腦同錯**，互審只會複讀、不會降幻覺。→ 互審**必須**做 persona/模型隔離，且用 `tests/tool_eval/metrics.py` 的 **pass^k / 幻覺率實測驗收**；未證實有效前，需求 2 不算完成。
2. **權威放在結構化 gate，不是靠模型自律**：弱模型「不偏移」靠的是——`boundary_rules` 硬注入子代理 system prompt、**停止條件綁看板實際完成度＋互審通過**（而非子字串「說完成了」）、finalize 對照原始 goal 的獨立驗收。不做「prompt 說教式」的偏移防治。
3. **in-process only；既有外派路徑原地不動**：**不改寫** ToukaDispatch（out-of-process 外派外部 AI，ADR-0051/0082），in-process 是**新增分支**。降低回歸風險。
4. **一律以既有 Mission Board 當唯一狀態源（SSOT）**：不另立狀態源；所有步驟/進度/產出/審查結論都落回看板。
5. **遞迴與預算必須有硬上限**：subagent 能派 subagent（都走同一 `get_stream_generator`）→ **必須有深度上限＋全局 slot/成本預算聚合**，否則弱模型無限展開。現行 slots 是 per-request 記憶體狀態，跨子 loop 預算聚合要明確設計。
6. **不做**：不改寫既有 ToukaDispatch／不移除外派能力；不把互審塞進單一 prompt 假裝完成；不在未證實有效前宣稱「降幻覺」；不追求一步到位的多 subagent 並行（增量分階段）。

---

## 7. 必須新建 / 必須重構

### 7.1 必須新建（5 項）
| 項目 | 為何 | 規模 |
|---|---|---|
| **`dispatch_subagent` 工具（agent-as-tool 原語）** | 最大缺口。在 loop 內 in-process 呼 `get_stream_generator` 開子 loop、抽乾 SSE 串流、收斂成**結構化結果**回主 agent。骨架（迴圈/工具層/路由入口/callback 語義）全在，缺這條黏合線。 | 中 ~200–400 行 |
| **in-process orchestrator 薄層** | 需求 1「同時派多個各跑各的再匯總」無協調器；管多子 loop 生命週期/預算分配/結果匯聚/與看板 task 綁定。大量借用 `group.py` 多身分建立與 `event_queue` 併發。 | 中 ~300–500 行 |
| **對抗式互審子系統** | 需求 2 幾乎全新：泛化 `ModelJudge`→通用 reviewer（回 critique 文字＋verdict，搬出 creation 命名空間）；producer↔critic 往返狀態機（挑錯→修正→再審→收斂上限）；紅隊 persona/模型隔離；Task 新增 `review_status/critic_verdict/reviewer_id` 欄位；審查結論接回 FSM gate（沒過→該步不 DONE）。 | **大 ~600–1000 行＋資料模型變更＋測試** |
| **結構化子代理回報 schema** | 需求 4 裁決依據：取代純文字摘要，含 `artifact_refs`、驗證執行結果、`confidence`、self-report claims，讓主 agent 可程式化判定。 | 小 ~100 行 |
| **mission 級最終驗收原語（finalize/verify）** | 需求 5 閉環：all-DONE 後主 agent 對照原始 goal 最終確認並記錄結論；併入防幻覺打臉；可選自動執行 per-task `verification_cmd`。 | 小–中 ~150–250 行 |

### 7.2 必須重構/調整（4 項，皆非大改）
1. **`DispatchCallbackStore` deliver 路徑**：加 in-process 直呼（繞過 `server.py._notify_touka_proxy` HTTP）；深層需把「不可續的阻塞 await」改成**以看板 status 為 SSOT 的輪詢/重掛**，解需求 3 破口。
2. **看板歸屬與權限模型**：外部路徑硬編 `target_agent_id=='DispatchAgent'`＋跨身分寫需 `can_supervise_dispatch`；in-process 多身分子代理各自 `agent_id` 會撞 `PermissionError`→改為「主 agent 擁看板、子只回報某 task」或引入父子 mission 關聯。
3. **臨時 subagent 的非落盤身分/腦路徑**：`record_working_brain`/soul 是全域檔案寫入；大量並行共用 `agent_id` 會互踩、污染真實 agent 記錄→需一條「不落盤臨時身分/腦」。
4. **停止條件：從「slot 預算＋stop_keywords 子字串」改綁「看板完成度＋互審通過」**：這是讓弱模型「不偏移」的關鍵（既有邏輯調整，非新建）。

---

## 8. 架構草圖

```
使用者訊息 → 主 agent ReAct loop〔既有 claude_handler〕
  └ 首輪 mission_tools(initialize) 拆步驟＋每步 goal/boundary_rules〔既有, 原子落盤〕
  └ 逐步：主 agent 呼 dispatch_subagent(task_id, goal, ctx)〔新〕
        → orchestrator 組臨時身分＋brain → get_stream_generator 開子 loop〔既有〕
        → ToolExecutor 併發執行子代理工具〔既有〕→ 子代理產出
        → 【互審點】orchestrator 開 reviewer subagent（獨立 persona/模型）
              讀 producer 產出＋原始 task goal → 結構化 critique(verdict/理由)〔新〕
              └ verdict=需修 → producer↔critic 往返〔新, 有收斂上限〕
              └ 通過 → 結構化回報 deliver_result(in-process 直呼)〔既有 store＋新路徑〕喚醒主 agent
  └ 主 agent 讀「結構化報告」裁決〔需求4〕：
        滿意 → mission_tools(update_status DONE)〔既有 FSM〕→ 下一步
        不滿意 → 自己接手／重派〔既有 loop 能力〕
  └ 全 task DONE → finalize 原語：對照原始 goal 最終驗收＋防幻覺打臉〔新＋既有 363-396〕→ 確認達成

持久化點：
  〔既有耐久〕current_mission.json（狀態/artifact_refs/test_log/blocker_detail，原子寫入）
            ＋SQLite 對話樹＋autonomy_log（配額/in-flight 崩潰標記）
  〔需新增落盤〕審查結果欄位（review_status/critic_verdict/reviewer_id）
            ＋協調決策軌跡（派工日誌，放 metadata 或 append-only 事件流）
            ＋可重掛的等待狀態（取代純 in-memory Event）

複審點：producer 產出後、update_status DONE 之前（reviewer 為 gate；沒過該步不 DONE）；
       finalize 為第二道總複審點（對照原始 goal）。

續接點：任一步斷線/額度用盡 → 結果已落盤 → 重啟後 orchestrator 讀最後 DONE task → 從下一 task 續派
       （需補 ADR-0078 #8 自動續跑，現為人工 retrigger）。
```

---

## 9. 階段規劃（增量四階段，先窄後寬，每階段可獨立驗收）

- **Phase 1（MVP 邊界）＝單一 in-process subagent 直派，無互審** ✅ **已完成（2026-07-07，[ADR-0134](../adr/0134-in-process-subagent-dispatch.md)；guard 39/39、pytest 2745、vitest 971；live E2E 待維護者手動 smoke）**
  只做 `dispatch_subagent` 工具＋in-process `deliver_result` 直呼。主 agent 能在 loop 內派**一個**子代理跑單一 task、拿回（純文字或最小結構化）結果、自行 `update_status`。重用 `get_stream_generator`＋`group.py` 串流收斂＋mission board＋callback store。先不碰多派/互審/權限重設（主 agent 用自己身分擁看板、子代理暫走非落盤 brain）。**這步就驗證「in-process 取代外派」的核心假設**，風險最低、最快端到端跑通。

- **Phase 2＝結構化回報 ＋ finalize 閉環 ＋ 停止條件改綁看板完成度** ✅ **已完成（2026-07-07，[ADR-0135](../adr/0135-structured-report-finalize-and-board-bound-stop.md)；pytest 2775、guard 39/39）**
  加結構化回報 schema（需求 4 程式化裁決）＋mission 級 finalize 驗收（需求 5）＋把 `should_stop_react` 從子字串改綁 board 完成度（目標 b 不偏移）。**此階段讓弱模型路徑穩定。**（誠實註記：降幻覺／防偏移的**實測**仍待 Phase 3 的 eval 前置工程——見設計文件 §7。）

- **Phase 3＝對抗式互審**（最大新建塊）✅ **前置 eval 軌已完成（ADR-0136）＋本體已完成（2026-07-07，[ADR-0137](../adr/0137-adversarial-review-subsystem.md)；pytest 2854、guard 39/39）**
  泛化 `ModelJudge`→reviewer、加 review 往返狀態機、Task 新增審查欄位、審查 gate 接回 FSM（需求 2）。**放最後**因為前兩階段已能獨立產生價值，且互審 prompt/收斂需靠**真實子代理產出**來調校；用 pass^k／幻覺率**實測**互審是否真降幻覺。（誠實註記：骨架與所有結構化 gate 已落地並單元驗證；**「真的降幻覺」仍待 ADR-0136 量測軌上跑注錯案例集＋五臂 A/B——未達預註冊門檻前不宣稱降幻覺**。詳見設計文件 §7.2/§7.3。）

- **Phase 4＝多 subagent 並行 orchestrator ＋ 續跑韌性 ＋ 權限/臨時身分收尾** ✅ **已完成（2026-07-07，[ADR-0138](../adr/0138-parallel-fanout-and-resumable-delivery.md)；pytest 2869、guard 39/39）**
  orchestrator 管多子 loop 生命週期/預算聚合（需求 1 完整 fan-out）＋看板歸屬權限重設＋臨時非落盤身分＋斷線自動續跑（補 ADR-0078 #8）。**放最後**因為並行與續跑是規模化與可靠性收尾，非核心可行性。（實際落地：`run_subagent_group` 並發封頂 fan-out＋一子失敗不沉全組；耐久落盤 `dispatch_results/<token>.json`＋await 耐久優先＝結果**跨重啟可重讀**；臨時非落盤身分於 Phase 1 已根治、本階段加並行隔離測試釘死。**自動背景續跑驅動（ADR-0078 #8）明確劃出範圍外**——耐久 substrate 已備，續跑 driver 屬另一提案。）

> 全程原則：**保留既有外派路徑不動**（in-process 是新增分支非替換）；**一律以既有 mission board 當 SSOT**，避免另立狀態源。

---

## 10. 風險與明確「不做」

**關鍵風險**
- **互審有效性未證**：協作打分 ≠ 對抗降幻覺；proposer/critic 同腦同 persona＝同腦同錯。→ 強制 persona/模型隔離＋pass^k/幻覺率實測；否則需求 2 做了也無效。
- **遞迴資源爆炸**：subagent 派 subagent 無深度上限＋全局預算聚合→弱模型無限展開。
- **串流收斂脆弱**：`dispatch_subagent` 要 drain 子代理 SSE 並抽「最終結果」，`group.py` 解析已複雜；子代理自己多輪 ReAct 時更難。
- **記憶體等待不可續**（需求 3 破口）：`DispatchCallbackStore` 純 `asyncio.Event` 重啟即失→需改輪詢 get_board 重掛，否則長跑卡死。
- **權限撞牆 ＋ 全域 brain/soul 落盤污染**：見 §7.2 重構 2、3。
- **停止條件靠子字串比對**：弱模型易誤判提前停/複讀（目標 b 要防的偏移）；未綁 board 完成度前不穩。
- **mission 是扁平 DAG 非巢狀樹＋per-agent 單一看板**：多主 mission 掛多 sub-mission 需擴充資料模型。
- **協調決策軌跡缺失**：現只落盤最終狀態快照，不記「為何派這個 subagent/派了什麼/回報全文」→ 複雜任務除錯與精確續接困難。

**明確不做**（見 §6 誠實邊界）：不改寫既有外派 ToukaDispatch；不移除外派能力；不靠 prompt 說教防偏移；不在未實測前宣稱降幻覺；不一步到位做多 subagent 並行。

---

## 11. 決策與待明示項

- **可行性已定調**＝`mostly-existing-plus-glue`，不需大重構；**建議照 §9 四階段增量落地**，Phase 1 MVP 先驗證核心假設。
- **已決定（2026-07-07）**：「對抗式互審子系統」（§7.1 最難塊）的專門設計 **已完成並定案**——見 [adversarial-review-subsystem-design.md](adversarial-review-subsystem-design.md)（A5 證據接地為骨幹、6 個未決設計點已由維護者決定、即 Phase 3 實作藍本）；維護者並已核可**啟動 Phase 1 MVP**（2026-07-07 開工）。
- **未決設計點**（進實作前需決定）：(1) 看板歸屬模型＝「主擁看板、子回報 task」vs「父子 mission」；(2) 臨時 subagent 身分＝共用主 identity vs 非落盤臨時 identity；(3) 互審隔離＝不同 persona 同模型 vs 不同模型；(4) 續接策略＝輪詢 get_board vs 落盤等待狀態。
- **每階段驗收沿用既有全套**：ruff＋lint_ssot＋guard_all＋pytest＋vitest；互審另加 pass^k/幻覺率離線 eval。

---

## 附錄：相關既有元件與 ADR

- 執行：`claude_handler`（ReAct loop）、`tool_executor`、`dispatchers/factory.py`（`get_stream_generator`）、`dispatchers/group.py`（in-process 多身分示範）
- 任務看板：`ToukaMemory` `MissionManager`／`models/mission.py`、`mission_tools`、`render_mission_block`；[ADR-0045](../adr/0045-mission-board-fsm-and-artifacts-decoupling.md)（FSM＋artifacts）、[ADR-0049](../adr/0049-cross-platform-mission-filelock.md)（雙鎖）
- 委派/回呼：`dispatch_callback_store`、`dispatch_supervisor`、`routers/dispatch_callback`；[ADR-0051](../adr/0051-dispatch-callback-channel.md)（雙向回呼）、[ADR-0082](../adr/0082-dispatch-in-proxy-consolidation.md)（併入 proxy）
- 持久化/韌性：`async_db`（SQLite）、`resource_manager`（原子寫入）、`stream_buffer_service`（[ADR-0081](../adr/0081-resumable-stream-cursor-buffer.md)）、`autonomy`（崩潰復原）；[ADR-0078](../adr/0078-resilience-and-failure-observability.md)（韌性；#8 自動續跑仍 TODO）
- 自主/預算：`rhythm_service`、`autonomy_service`；[ADR-0001](../adr/0001-pulse-driven-architecture.md)（脈衝驅動）、[ADR-0012](../adr/0012-autonomy-governance-and-static-mandates.md)（自主預算大一統）
- 複審種子：`services/creation/model_judge.py`（`ModelJudge`／Judge Protocol）、`services/creation/evaluation.py`；`prompt_blocks.build_validation_protocol`（[ADR-0123](../adr/0123-verification-convention.md)，self-critique 藍本）；`tests/tool_eval/metrics.py`（pass^k／幻覺率驗收）
- 調查來源：本提案「現況盤點」出自 2026-07-06 的平行程式碼調查＋收斂 synthesis。
