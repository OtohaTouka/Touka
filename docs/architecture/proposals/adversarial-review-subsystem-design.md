# 設計文件：對抗式互審子系統——證據接地紅隊互審（Evidence-Grounded Adversarial Review）

> 狀態：**設計定案（2026-07-07）；尚未實作；6 個未決設計點已由維護者決定（§5）。**
> 本文件是母提案 [in-process-subagent-orchestration.md](in-process-subagent-orchestration.md) **Phase 3（需求 2「對抗式互審」）的實作藍本**——母提案中唯一「全新建」的塊。實作待維護者明確啟動；依母提案路線圖應落在 Phase 1（`dispatch_subagent` MVP）／Phase 2（結構化回報＋finalize）之後，惟 §7.1 的 eval 前置工程為 design-independent，可提前或平行進行。
>
> 約束：**由維護者 commit**；只用 **DebugAgent** 身分驗證、不動其他 agent 資料；**嚴禁自動修改 `data/`**；每步過 ruff＋lint_ssot＋guard_all＋pytest＋vitest；互審有效性另以離線 eval 驗收（§7），**未達預註冊門檻前，需求 2 不算完成**。
>
> 依據：2026-07-06 的專門設計流程——5 個對立設計哲學各出完整結構化方案 × 每案被 4 個對抗攻擊鏡頭實打 → 3 位取向各異的評審六維排名 → 收斂 synthesis。支撐結論的關鍵核碼發現已**獨立複核**（本文標 ✔ 者）。

---

## 1. 一句話定位

**verdict 的權威來源不是模型意見，而是 critic 無法以幻覺竄改的可執行證據。**

`EvidenceCollector`（純 Python，非 LLM）跑 per-task `verification_cmd` 取 exit code、核對 `artifact_refs` 是否真實存在、節錄 `test_log`，組成 `EvidenceBundle`；critic（LLM）只是「證據蒐集器＋比對器」，其意見永遠不能推翻證據硬檢查。審查 gate 做成 Mission Board **落盤層（`_save_mission_unlocked`）的狀態不變式**而非某個轉移方法上的檢查——沒過審的 task **物理上無法落盤為 DONE**。

這直接落實母提案兩條誠實邊界：邊界 1（有效性必實測，§7）、邊界 5（遞迴與預算硬上限，§4.6）。

---

## 2. 為何是「證據接地」：三個核碼支點（✔＝已獨立複核）

| # | 事實 | 位置 | 設計後果 |
|---|---|---|---|
| 1 | **同腦坍縮是預設實況** ✔ | `ToukaProxy/src/utils/brain_persistence.py:84-85`：任何含 `toukasoul` 的 brain 一律回 `claude-4.6-sonnet`；未知臨時 agent_id → `get_active_brain` 回 `None` → fallback 到 producer 的 request.model | 「同模型不同 persona」的 critic **物理上是同一顆權重**——persona 只是 system 字串＝改採樣、不改事實先驗（同腦同錯）。凡把「異 persona 同模型」當預設隔離的設計，中心主張在預設部署自動失效 |
| 2 | **`save_mission` 是繞過 gate 的後門** ✔ | `ToukaMemory/.../mission_manager.py:193-197` 直呼 `_save_mission_unlocked`，零 `VALID_TRANSITIONS` 檢查（FSM 檢查只存在於 `update_task`:226-233）；mission-DONE 聚合（:275）只讀 `t.status` | gate 若只裝在 `update_task` 的 DONE 分支，一次 `save_mission` 就繞過——必須下沉到唯一落盤閘口做**不變式** |
| 3 | **eval LIVE seam 未接** ✔ | `tests/tool_eval/harness.py:87` `_run_case_live` = `NotImplementedError`；唯一 LIVE 路徑 `live_select_only` 只選不執行、無 artifact、無 persona/soul 注入 | 「互審降幻覺」的 A/B 對照**目前跑不起來**；且既有指標（`hallucinated_tool_rate`＝工具名捏造、`selection_ok`＝選對工具名）量的軸與 artifact 內容幻覺無關。**需求 2 最大的隱藏工程是 eval 基建，不是互審器本身**（§7.1） |

這三個事實合起來淘汰了其他四案的中心假設：A1 最小閘／A2 紅隊隔離／A4 辯證往返的預設隔離臂都是「同模型異 persona」（被支點 1 打穿）；A3 多鏡頭陪審的 K 個鏡頭在單模型下共用同一世界先驗，正交性崩塌成「貴的單 critic」。**只有 A5 把可信度來源移出模型**——同權重下，證據錨仍不可竄改。

---

## 3. 設計來源：5 方案 × 4 攻擊 × 3 評審

- **五案**：A1 最小可行審查閘（單次 critic＋二元 verdict）／A2 紅隊隔離（隔離度為核心賭注）／A3 多鏡頭陪審團（多 critic 各持失敗模式鏡頭＋多數決）／A4 辯證往返狀態機（多輪 critique→revise→re-review）／A5 證據接地（verdict 錨在可執行證據）。
- **四個攻擊鏡頭**（每案實打）：同腦共謀／成本與遞迴爆炸／不收斂・閘死鎖・假通過／有效性不可證偽。
- **三位評審**（弱模型實效／工程落地／魯棒與成本）六維排名，跨評審平均：

| 方案 | 平均名次 | 實效 | 隔離 | 可測 | 成本有界 | 整合 | 可增量 |
|---|---|---|---|---|---|---|---|
| **A5 證據接地** | **1.67** | 3.3 | 3.0 | 3.0 | 2.0 | 3.7 | 3.3 |
| A1 最小閘 | 2.33 | 2.0 | 2.0 | 3.0 | 2.7 | **5.0** | **5.0** |
| A2 紅隊隔離 | 3.00 | 2.3 | 2.3 | 2.3 | 1.7 | 3.3 | 3.3 |
| A3 多鏡頭陪審 | 4.00 | 3.3 | 2.3 | 2.3 | 1.0 | 2.0 | 1.3 |
| A4 辯證往返 | 4.00 | 2.3 | 2.0 | 2.0 | 1.0 | 2.7 | 1.7 |

（1–5，5 最佳。2 票 A5 第一；第 3 票選 A2，但其首選理由——context 隔離是同權重下唯一非換皮機制——已被 A5 涵蓋並嫁接進最終設計。注意**沒有任何方案全維高分**：五案都被攻擊打出 fatal，收斂設計把各案的修補硬編進骨架，§6。）

**收斂＝A5 為骨幹＋A1 的最小整合紀律落地＋跨案修補**（§6.1）。

---

## 4. 核心設計規格

### 4.1 Reviewer 介面（新模組 `ToukaProxy/src/services/review/reviewer.py`）

泛化 `evaluation.py` 既有 `Judge` Protocol（`async def score(tasks, artifact_md) -> float`；`Judge`／`NoopJudge`／`_is_real_judge` 雙閘注入**原地不動**，繼續供 creation EVAL 使用），搬出 creation 命名空間：

```python
class Reviewer(Protocol):
    async def review(
        self, *,
        goal: str,
        boundary_rules: list[str],
        producer_claims: str,
        evidence: EvidenceBundle,
    ) -> CritiqueReport: ...
```

- **`EvidenceBundle`**：純資料類——各 `verification_cmd` 的 exit code＋stdout 摘要、每個 `artifact_ref` 的存在性布林、`test_log` 節錄。由 **`EvidenceCollector`（純 Python，非 LLM）**產生。
- **`CritiqueReport`**（frozen pydantic）：`verdict: Literal["APPROVE","REJECT","ABSTAIN"]`；`reasoning: str`；`required_fixes: list[RequiredFix]`（`RequiredFix = {issue, evidence_anchor: str|None, severity: Literal["blocking","advisory"]}`）；`confidence: float`；`grounded: bool`（所有 blocking fix 是否都有 evidence_anchor）；`reviewer_id: str`；`critic_model: str`。
- **`GroundedReviewer`**（LLM 實作）：一次 `messages.create`，schema-forcing 強制 JSON 輸出；解析失敗／verdict 缺失／低 confidence 一律 **fail-closed 映射 REJECT**——與 `ModelJudge` 的 fail-safe 回 0.0 方向相反，因為這是 gate 不是計分：審查器壞掉時寧可擋住不放行。
- **`NoopReviewer`** 哨兵：`review_required=False` 時注入＝不審直接放行，保留漸進開關、既有行為零回歸。
- verdict 走既有 `tool_errors.build_tool_success(**fields)` 掛欄位（`is_tool_success` 前綴可辨識、`ToolCallEvent` 人類面投影），**不另立封套**。

### 4.2 producer↔critic 往返 FSM

狀態活在 orchestrator，但**每步落回看板欄位**（§4.3）＝跨重啟可續：重啟後輪詢 `get_mission_board` 重掛，不依賴記憶體 `asyncio.Event`。

| 轉移 | 條件 |
|---|---|
| `PRODUCED → EVIDENCE_COLLECTING` | 無條件——蒐證是一切審查的地基 |
| `EVIDENCE_COLLECTING → REVIEWING` | 帶 `EvidenceBundle` 進審（蒐證成敗都進：失敗本身就是證據） |
| `REVIEWING → APPROVED` | verdict=APPROVE **∧** bundle 全部 cmd exit==0 **∧** 全部 artifact_ref 存在 **∧**（`review_risk=high` ⇒ `critic_model≠producer_model`） |
| `REVIEWING → APPROVED_LOW_CONFIDENCE` | 單模型降級路徑（決策 3）：`review_risk=normal` ∧ verdict=APPROVE ∧ 證據硬檢查全過 ∧ `critic_model==producer_model`。**顯式落盤、不與 APPROVED 混用**，供 eval 單獨量測此路的 false-pass 率 |
| `REVIEWING → REVISING` | verdict=REJECT ∧ 存在 blocking fix ∧ `grounded=true` ∧ `review_attempts_total < CAP` |
| `REVISING → EVIDENCE_COLLECTING` | producer 修正後**必須重新蒐證**（不採信「我改好了」）；`review_attempts_total += 1` |
| 任一狀態 `→ BLOCKED_FOR_HUMAN` | `review_attempts_total ≥ CAP`；或停滯偵測觸發；或軟任務（無可執行證據）；或 `review_risk=high` 而異模型不可用（fail-closed）；或 `grounded=false` 的純主觀 REJECT 累計達上限 |
| `BLOCKED_FOR_HUMAN → APPROVED` | **僅限真人 ack**（決策 4）：走既有 `request_approval` HITL 流程，`reviewer_id` 落 `human:{user}` |

**收斂三重保險**：(a) 到達 APPROVED*；(b) `review_attempts_total` 跨生命週期累計硬上限——落看板、`BLOCKED→DOING` 重進**不歸零**（必要性：`VALID_TRANSITIONS[BLOCKED]={TODO,DOING}` 是回旋門 ✔ `mission_manager.py:229`，單輪計數擋不住外圈無限重試）；(c) 停滯偵測〔嫁接 A4〕——連續兩輪「成功解析」的 `required_fixes` 正規化集合相同即早停，**刻意排除 JSON 解析故障輪**，避免格式故障誤判為語意收斂失敗。

**對 A5 原始設計的兩個硬反轉**（synthesis 拍死，防「靜默放行」）：
1. `grounded=false` 的純主觀 REJECT **不再**靜默降 advisory 放行——原規則本意防 critic 幻覺，卻把「這個 cmd 是假的」這類唯一能戳穿共謀的質疑系統性消音。改為計入 `review_attempts_total`、達上限導向 `BLOCKED_FOR_HUMAN`。
2. 軟任務**不走**靜默 ABSTAIN 放行——弱模型在無可驗證的軟任務恰最會幻覺，且 producer 只要宣稱無產出物即獲免審捷徑。改為一律 `BLOCKED_FOR_HUMAN`＋真人 ack。

### 4.3 Mission Task 新增欄位（`ToukaMemory/.../models/mission.py`）

| # | 欄位 | 型別／預設 | 語義 |
|---|---|---|---|
| 1 | `review_status` | `str \| None = None` | 審查子 FSM 態（§4.2 各態）；`None`＝未進審查流。與 `TaskStatus` **正交**（不動既有 5 態 StrEnum），gate 讀它 |
| 2 | `critic_verdict` | `str \| None` | 最後一次 `CritiqueReport` 的 verdict＋reasoning 摘要（人類／主 agent 可讀溯源） |
| 3 | `reviewer_id` | `str \| None` | `__reviewer_{task_id}__@{critic_model}`（決策 5/6）；真人 ack 時 `human:{user}`（決策 4） |
| 4 | `review_attempts_total` | `int = 0` | **跨生命週期累計**往返數（`BLOCKED→DOING` 重進不歸零），達 CAP 觸發 `BLOCKED_FOR_HUMAN` |
| 5 | `evidence_digest` | `str \| None` | `EvidenceBundle` 摘要（各 cmd exit code、artifact 存在性布林、test_log 節錄）＝可審計的證據快照，非模型意見 |
| 6 | `review_required` | `bool = False` | per-task 開關；**預設關＝零回歸漸進上線**；True 時 gate fail-closed |
| 7 | `review_locked` | `bool = False` | 達預算上限後永鎖：不再開新 review，重進一律走 human-ack（封住外圈無限重試） |
| 8 | `verification_cmd_locked` | `bool = False` | cmd immutable-after-init——producer 不可覆寫自己的考卷 |
| 9 | `review_risk` | `Literal["normal","high"] = "normal"` | 決策 2 新增：自動（boundary_rules 關鍵字底線）＋顯式（mission init 標記）**聯集**；**單調只升不降**；init 後 immutable |

### 4.4 Gate：落盤不變式＋隔離寫路徑

**第一層——落盤不變式（堵後門）。** 接點在 `_save_mission_unlocked`（唯一落盤閘口 ✔ `mission_manager.py:102`），而非只裝在 `update_task` 的 DONE 分支——因為 `save_mission`（✔ :193-197）零檢查直達落盤，是繞過任何轉移層 gate 的後門。不變式：

- 對每個 `status==DONE` 且 `review_required` 的 task，斷言 `review_status ∈ {APPROVED, APPROVED_LOW_CONFIDENCE(僅 normal)}` 或帶 human-ack 標記，否則 `raise ValueError`（沿用既有 `# noqa: RAISE_STRING_BAN` 慣例）。
- mission 總體 DONE 聚合（✔ :275 現只讀 `t.status`）同步改為 `all(t.status==DONE and (not t.review_required or t.review_status in PASSING))`。
- **不改 `VALID_TRANSITIONS` 拓撲**（DOING→DONE 仍合法）：gate 是合法轉移之上再疊的業務前置條件，與既有 dependency-gate（:250-254）同層風格，正交、低回歸。

**第二層——隔離寫路徑（堵自簽）。** review 欄位**不得**經 producer 走的 `update_task` 寫入（✔ 其簽名本就不含 review 欄位，天然利於隔離）。新增 `MissionManager.record_review(identity, task_id, *, verdict, reviewer_id, evidence_digest, ...)`：

1. 強制 identity 帶 `can_review` capability（仿 ✔ :237-241 `is_privileged` 範式）；
2. 校驗 `reviewer_id ≠` task 執行者（reviewer≠producer）；
3. 校驗 review 時戳 ≥ producer 最後產出時戳——防「先蓋 PASS 再改產出」的 TOCTOU（時戳來源用既有 `task.updated_at` 或新增 `last_output_epoch`，建置第 4 步拍定）。

一次堵三洞：`save_mission` 後門、自簽假通過、TOCTOU。

### 4.5 隔離三層

| 層 | 機制 | 為何有效 |
|---|---|---|
| 1 **證據接地**（主軸，必做） | verdict 權威在 `EvidenceCollector` 的客觀 anchor（exit code／存在性／test_log） | 同權重下仍不可被幻覺竄改——對支點 1 的**結構性**免疫，不隨 critic 能力弱化而崩塌 |
| 2 **context 隔離**〔嫁接 A2〕 | critic 只餵 goal＋boundary_rules＋producer_claims＋evidence_bundle，**不餵 producer 的 CoT／工具軌跡** | 斷開複讀鏈——persona 換皮之外唯一還能降低錯誤相關性的機制 |
| 3 **模型隔離**（高風險 task 的 gate 硬前提，**非 opt-in**） | `review_risk=high` ⇒ 強制 `critic_model ≠ producer_model`（config 固定，決策 6）；異模型不可用時 **fail-closed**（`BLOCKED_FOR_HUMAN`），絕不同腦硬審後 APPROVE | 支點 1 證明同腦是預設實況——唯一能破共有事實先驗的硬防禦不可預設關閉 |

**臨時身分不落盤**（回應母提案 §7.2 風險 3）：critic 用 `__reviewer_{task_id}__` 保留前綴身分（決策 5）；蒐證／審查路徑一律**不呼叫** `record_working_brain`／`soul_service`（污染源 ✔ `group.py:343` 全域寫入）；brain 選擇以參數傳入、審完即棄；配 guard／紅隊測試斷言 `__reviewer_` 前綴**永不出現於 `data/` 或 personal_drive 檔名**。

### 4.6 預算四道硬上限（邊界 5）

全部落 Mission Board（SSOT、跨重啟持久、**對不落盤臨時身分也生效**）：

| # | 上限 | 機制 | 為何不能沿用既有件 |
|---|---|---|---|
| 1 | 往返 **CAP=3**（決策 1） | `review_attempts_total` 跨生命週期累計，達 CAP → `review_locked=True` → `BLOCKED_FOR_HUMAN`；重進讀 `review_locked` 直接走 human-ack | `BLOCKED→{TODO,DOING}` 是回旋門（✔ :229），單輪記憶體計數擋不住外圈重試 |
| 2 | 子 loop 開啟數 | 新建 **mission-scoped `ReviewBudgetLedger`** 單例（以 mission_id 為鍵、mission 級 lock 防 lost-update）；開任何 producer/critic 子 loop 前 `can_spawn()`；子 loop 用衍生 request_id（`{parent}:rev{n}`）不覆寫父 slots | `rhythm_service` 是扁平 `dict[request_id,int]`：`initialize_session`（:85-92）對同 request_id 無條件重設＝子 loop 進入即回血；`decrement_slots` 只在無 tool_use 的 nudge 觸發＝有工具的子 loop 燒 0 slot——**結構性聚合不了**。ledger 也不掛 context（✔ `group.py:200` `model_copy` 深拷貝分叉），以 mission_id 查同一實例 |
| 3 | 成本 | critic token 記帳**歸戶父 identity**；互審路徑強制 cost cap enabled（不可關） | `is_over_budget` 預設 `cap.enabled=False`（fail-open）且讀 per-identity `token_usage.json`＝不落盤 critic 恆回 0 而免疫。「隔離不落盤 brain」與「成本必計費」是兩件事，拆開處理 |
| 4 | 遞迴深度＝**兩層**（決策 1） | 行為承諾：主 agent 可開子 loop；producer/critic **不可再開孫**——`PipelineContext` 加 `review_depth` 硬攔（超限回 `permission_denied` 結構化封套）＋ critic 子 loop 以**精簡工具集**啟動（移除一切能開子 loop 的工具）＝能力面根絕，雙保險 | 深度概念目前全庫不存在，必須新造 |

CAP 與深度的天花板為程式內 SSOT 常數；config（如 `review.max_attempts`）沿用 evolution `_clamp_quota` 慣例**只能收緊不能放寬**；eval 後若數據支持放寬＝修天花板＝一次人審 commit。

---

## 5. 已決定的 6 個設計決策（2026-07-07，維護者核可）

| # | 決策 | 決定 | 核心理由 |
|---|---|---|---|
| 1 | CAP／深度 | **CAP=3；兩層**（主可派子、子不可派孫；critic 另從工具集面根絕）；SSOT 天花板＋config 只能收緊 | 首產＋2 修正涵蓋多數「可修」錯誤；多輪對弱模型邊際遞減（A4 否決理由）；`review_attempts_total` 落盤＝eval 後有真實收斂分布可重調（改 config 零架構成本） |
| 2 | 高風險判定 | **自動＋顯式聯集**；旗標單調只升不降；init 後 immutable | 代價不對稱：過標＝多一次異模型呼叫；漏標＝gate 在最需要處是安慰劑。沿用 `verification_cmd_locked`／`_clamp_quota` 慣例 |
| 3 | 單模型 degraded 預設 | **分層**：高風險 fail-closed→human-ack；一般 task `APPROVED_LOW_CONFIDENCE` **顯式**放行（獨立落盤值）；config 可收緊、不可放鬆高風險 | 單模型是本 repo 預設實況（支點 1）；全 fail-closed 會逼使用者關掉 gate＝真實世界更不安全。A5 紅利：降級只降意見層、**證據層權威完整保留**。顯式落盤≠被否決的「靜默放行」 |
| 4 | human-ack | **需真人**（借 evolution `request_approval` HITL 先例）；ack 落盤 `reviewer_id=human:{user}`；eval 把 human-ack task 從自主 gate 指標排除 | 主 agent 自 ack＝把權威還給模型自律（違反邊界 2）＋重演剛堵掉的自簽洞；DONE 是不可逆終端態，保守方向＝人；`review_required` per-task opt-in 是打擾洩壓閥——沒有獨立權威的 gate 不是 gate |
| 5 | reviewer_id | **每 task 衍生** `__reviewer_{task_id}__`＋保留前綴；配「前綴永不落盤」guard 測試 | 並行零互踩（Phase 4-proof）；洩漏會尖叫（personal_drive 冒出 `__reviewer_task-3__` 檔＝一眼可見的 bug 旗標）；成本一個 f-string；前綴讓「臨時審查身分」一行 `startswith` 可判 |
| 6 | critic model 池 | **config 固定** `review.critic_model`；未設＝走決策 3 降級路徑；**不做動態挑選** | eval 可重現性是 Phase 3 的存在意義（critic 換來換去＝基準不可比、預註冊門檻失效）；「名字≠」不保證「權重≠」（支點 1 反向適用：不同 gateway 可能同一顆開源權重）；選法官是人的治理決策。日後可演化為人工排序 fallback 清單 |

共同脊椎：**權威單向**——預算只能收緊、風險只能升級、放行不能自赦——與既有 `_clamp_quota`／fail-closed 慣例同構，不引入新哲學。

---

## 6. 嫁接與否決（誠實記錄）

### 6.1 嫁接進最終設計（7）

1. 〔A5→骨幹〕`EvidenceCollector` 純 Python 證據錨——五案中唯一結構性免疫同腦共謀的機制。
2. 〔A2〕context 隔離：critic 只看產物與 goal、不看 producer CoT。
3. 〔A1〕最小整合紀律：不新增 `TaskStatus` 列舉值、`review_status` 子欄位正交、gate 沿用 dependency-gate 同層風格、`build_tool_success(**fields)` 掛 verdict 不另立封套。
4. 〔A1〕eval 反作弊臂：「同模型同 persona critic」當故意違反隔離的對照臂（§7.2 Arm-D），把邊界 1 的「同腦同錯」從論證變成可證偽實測。
5. 〔A4〕停滯偵測（只比對成功解析輪）。
6. 〔A3〕verifiability veto 概念保留作日後「多失敗模式鏡頭」擴充的分層依據（有證據強 gate、無證據誠實標缺口）。
7. 〔跨案共識〕gate 做成落盤不變式＋`record_review` 隔離寫路徑（can_review capability／reviewer≠assignee／時戳校驗），一次堵後門＋自簽＋TOCTOU。

### 6.2 明確否決（7）

1. **A4 多輪辯證為核心**：多輪是對弱模型最無效的軸（self-consistency 只降變異不擴覆蓋、同腦同盲區），卻換 O(r²) 成本；其 `ReviewBudget` 掛 context 會被 `group.py:200` `model_copy` 深拷貝分叉（✔），聚合宣稱在既有碼上結構性不成立。
2. **A3 多鏡頭陪審為 Phase 3 首個增量**：成本 K×往返；單模型下四鏡頭共用同一先驗＝貴的單 critic；聚合規則自身成新信任單點；veto 誤報會把好產物確定性卡死。
3. **異模型隔離設 opt-in**：支點 1 證明同腦是預設實況——唯一硬防禦若預設關閉，中心降幻覺主張在預設部署自動失效。改為高風險 task 的 gate 硬前提。
4. **A5 原始「`grounded=false`→advisory 靜默放行」**：把唯一能戳穿假 cmd 的質疑消音。改為計入預算、達上限走 human。
5. **A5 原始「ABSTAIN 軟任務直接放行」**：弱模型在軟任務最會幻覺卻最無 gate，且宣稱無產出＝免審捷徑。改為 human-ack。
6. **`verification_cmd` 由 producer 自寫自證作為 APPROVE 唯一權威**：`update_task`／init 對 cmd 零校驗，`true`／`echo ok`／pytest 收集不到測試皆 exit 0＝橡皮圖章。改為 immutable-after-init＋拒 no-op 前綴＋要求 test_log 斷言數 >0（critic 獨立出題雙源列為緩解、非根治）。
7. **借用 `hallucinated_tool_rate`／`selection_ok` 當降幻覺證偽指標**：兩者只測工具名捏造與工具選擇，與互審要降的 **artifact 內容／宣稱型幻覺不同軸**——門檻劃在無關曲線恆無訊號。必須新造指標（§7.1-B）。

---

## 7. eval 驗收：邊界 1 的可證偽方案

### 7.1 前置工程（design-independent；**需求 2 最大的隱藏工程**，可先行） ✅ **已完成（2026-07-07，[ADR-0136](../adr/0136-review-eval-rails.md)；A/B/C 三支柱落地，pytest 2800、guard 39/39。注錯案例集與五臂實跑屬 §7.2，待 Phase 3 本體）**

- **A. 接通 LIVE seam**：`harness._run_case_live`（✔ 現為 `NotImplementedError`）→ 真跑 producer 子 loop 產 artifact → `EvidenceCollector` 客觀跑 `verification_cmd` → critic 讀取 → 往返。並保留 persona/soul 注入（現 `live_select_only` 無 persona/soul、工具永不執行——否則隔離對照 C-same vs C-diff 無效）。
- **B. 新指標**：`EvalCase` 新增 `ground_truth`／`expected_artifact_check`／`injected_failure ∈ {none, goal_drift, fabrication, unverified_claim}`；`metrics.py` 新增 episode 級 **`gate_recall`**（有 injected_failure 者被擋比例）／**`gate_precision`**（擋下者真的錯的比例）／**`false_block_rate`**（誤擋正確產物）。明確**廢用** `hallucinated_tool_rate`／`selection_ok` 當降幻覺主指標（§6.2-7）。
- **C. 非 LLM oracle**：injected_failure 用機械可判定形式（AST/import 檢查捏造函式名、規則比對違反 boundary 的宣稱），catch 判定不靠另一個 LLM judge——避免循環信任。

### 7.2 五臂對照（同批 case、同 producer 模型、trials k≥5）

| 臂 | 配置 | 隔離出的變因 |
|---|---|---|
| A | 無互審 | baseline |
| B | 純意見互審（critic 只讀 claims、無 evidence_bundle） | 「審查動作」本身（＝退化 ModelJudge） |
| **C** | **本設計（證據接地互審）** | 完整設計 |
| D | 反作弊臂：同模型同 persona critic | 故意違反隔離 |
| E | 安慰劑臂：producer 自我複審往返 | 「多想一輪」效應 |

### 7.3 預註冊門檻（寫死於 case 檔；**任一未達＝需求 2 不算完成**）

1. Arm-C 的 `gate_recall` 對 injected_failure 子集顯著 > 0，且 `false_block_rate` 有界。
2. Arm-C 對 fabrication／unverified_claim 子集的 catch_rate 顯著 > Arm-B——證明「證據接地」帶來純意見達不到的淨增量。（實驗陷阱：exit≠0 **不可**直接餵給 critic 當提示——須讓 producer 不知情下產出、由 `EvidenceCollector` 客觀跑出，否則循環論證。）
3. Arm-D ≈ Arm-A 而 Arm-C > Arm-A——實證「同腦同錯」：隔離是有效性來源，非審查動作本身。
4. **成本硬門檻並列**：每 task 子 loop 總開啟數、總 token（歸戶父 identity）、`review_attempts_total` 設回歸上限——若降幻覺是靠往返暴增買來，判不合格。

未達門檻的退場：退回單 critic，或明標「此模型不適用互審」。基線存 `tests/tool_eval/baselines` 防漂移。由維護者 commit cases、只用 DebugAgent 跑、不動 `data/`。

---

## 8. Phase 3 建置順序（TDD 八步，紅→綠）

1. 〔紅〕`services/review/reviewer.py`：`EvidenceBundle`／`CritiqueReport`／`RequiredFix` pydantic＋`Reviewer` Protocol＋`NoopReviewer`；失敗測試斷言 schema 與 fail-closed（malformed JSON→REJECT、APPROVE 但 blocking fixes 非空→非 APPROVED、低 confidence→非 APPROVED）。
2. 〔綠〕泛化 `evaluation.Judge`→`Reviewer`（`Judge`／`NoopJudge` 原地保留供 creation EVAL）；實作 `GroundedReviewer`（一次 `messages.create`＋schema-forcing JSON）。
3. 〔紅〕`models/mission.py` Task 新增 §4.3 九欄位＋validator；測試斷言 `review_required=True` 的 DONE 在 review_status 不合格時**落盤即 raise**。
4. 〔綠〕`_save_mission_unlocked` 加 DONE 不變式＋mission-DONE 聚合改讀 review 欄位＋新增 `record_review`（can_review capability／reviewer≠assignee／時戳校驗）；加 **`save_mission` 後門繞過紅隊測試**＋**`__reviewer_` 前綴不落盤 guard**（決策 5）。
5. 〔紅→綠〕`EvidenceCollector` 純 Python 蒐證：核對 artifact_ref 存在、跑 verification_cmd 取 exit、**拒 no-op 前綴**、要求 test_log 斷言數 >0；測試含 `echo ok`／`true`／pytest 收集不到——三種假 cmd 皆判硬失敗。
6. 〔紅→綠〕`verification_cmd` immutable-after-init＋`ReviewBudgetLedger`（mission 級 lock 防 lost-update）＋`review_depth` 硬攔＋critic token 歸戶父 identity＋cost cap 強制 enabled。
7. 〔紅→綠〕`review_loop.py` orchestrator 薄層：驅動 §4.2 FSM＋開 critic 子 loop（衍生 request_id、不落盤臨時身分、context 隔離不餵 CoT、高風險異模型硬前提、精簡工具集）；停滯偵測只比對成功解析輪。
8. 〔驗收〕§7.1 前置工程（LIVE seam＋新指標＋非 LLM oracle）＋五臂 A/B＋預註冊門檻判定；全套 ruff＋lint_ssot＋guard_all＋pytest＋vitest。

> 排程註：§7.1 前置工程 design-independent——可在母提案 Phase 1/2 期間先行，第 8 步才消費它。步驟 1–7 依賴母提案 Phase 1 的子 loop 原語（`dispatch_subagent`）先存在。

---

## 9. 殘餘風險（收斂後仍在，不可粉飾）

1. **證據覆蓋率天花板**：強保證只在 task 具可執行證據時成立；純創作／設計文件／開放推理類軟任務退回 `BLOCKED_FOR_HUMAN` 或弱保證——而弱模型恰在軟任務最會幻覺。**非均勻保證**，必須靠五臂 eval 誠實量出覆蓋比例，不可高估整體效果。
2. **`verification_cmd` 語意不完整殘留**：即使 immutable＋拒 no-op，「多傳被忽略的 kwarg」這類在很多框架 exit 0 不報錯的錯誤仍可能假通過（真實觀測錨在「沒 crash」而非「行為正確」）；critic 獨立出題雙源能緩解、不能根治。
3. **單模型部署下的弱保證**：若走 `APPROVED_LOW_CONFIDENCE` 放行路徑（決策 3），同腦共謀通過的產出仍可能放行——只是被顯式標記、不被背書。
4. **critic 自身仍會幻覺**：`GroundedReviewer` 是一次 LLM 呼叫，可能回假 required_fixes 或對罕見但正確的 artifact false-positive REJECT，把好產物卡向 human——增加人的負擔；fail-closed 只防「解析失敗」，不防「解析成功但語意錯」。
5. **前置 eval 工程量被低估的風險**：接 LIVE seam＋新指標＋非 LLM oracle＋保留 persona/soul 是需求 2 **自己必須新建的最大工程**，絕非白嫖既有基建；這塊沒做，整個可證偽性懸空、邊界 1 永遠無法觸發。
6. **`ReviewBudgetLedger` 並發正確性**：mission-scoped 單例被 producer loop 與 critic loop（不同 agent_id）並發 read-modify-write 時，需以單一 mission 級 lock 守護，避免重演 per-agent 鎖的 lost-update。

---

## 10. 附錄

### 相關既有元件（本設計的錨點）

- 複審種子：`ToukaProxy/src/services/creation/model_judge.py`（`ModelJudge`）、`evaluation.py`（`Judge` Protocol／`NoopJudge`——原地保留）
- 看板／FSM：`ToukaMemory/.../services/mission_manager.py`（`_save_mission_unlocked`:102 ✔／`save_mission`:193 ✔／`VALID_TRANSITIONS`:226-233 ✔／`is_privileged`:237-241 ✔／DONE 聚合:275 ✔）、`models/mission.py`
- 子 loop 入口：`ToukaProxy/src/services/dispatchers/factory.py`（`get_stream_generator`）、`group.py`（多身分示範；`model_copy`:200 ✔／`record_working_brain`:343 ✔）
- 身分／brain：`ToukaProxy/src/utils/brain_persistence.py`（toukasoul 坍縮 :84-85 ✔）
- 封套：`ToukaProxy/src/services/tool_errors.py`（`build_tool_success(**fields)`）、`utils/tool_events.py`（`ToolCallEvent`）
- 預算：`ToukaProxy/src/services/rhythm_service.py`（`initialize_session`:85）、`autonomy_service`（cost cap）
- HITL／clamp 先例：`ToukaProxy/src/tools/builtin/evolution.py`（`request_approval`／`_clamp_quota`）
- eval：`tests/tool_eval/`（`harness._run_case_live`:87 ✔＝未接、`metrics.py`、`live_select_only.py`）

### 相關 ADR

[ADR-0045](../adr/0045-mission-board-fsm-and-artifacts-decoupling.md)（mission FSM＋artifacts）、[ADR-0049](../adr/0049-cross-platform-mission-filelock.md)（雙鎖）、[ADR-0051](../adr/0051-dispatch-callback-channel.md)（回呼通道）、[ADR-0078](../adr/0078-resilience-and-failure-observability.md)（韌性）、[ADR-0082](../adr/0082-dispatch-in-proxy-consolidation.md)（dispatch 併入 proxy）、[ADR-0121](../adr/0121-evolution-pipeline-merge.md)（演化管線＝FSM＋HITL 先例）、[ADR-0123](../adr/0123-verification-convention.md)（驗證慣例＝reviewer prompt 藍本）、[ADR-0124](../adr/0124-structured-success-and-applied-state-echo.md)（結構化成功封套）、[ADR-0125](../adr/0125-long-running-command-monitor.md)（背景執行＝EvidenceCollector 跑 cmd 可借鏡）

### 來源與方法

2026-07-06 的設計流程：5 份對立哲學的設計 → 每份以 4 個攻擊鏡頭攻擊（共 20 次）→ 3 位評審排名 → 收斂成最終設計。設計與攻擊皆以真實程式碼為地基（多處實地核碼）；本文件標 ✔ 者為對關鍵行號的獨立複核。收斂全文（含各案完整設計、逐案攻擊結果、三評審完整排名）未入版控；本文件即其耐久記錄。
