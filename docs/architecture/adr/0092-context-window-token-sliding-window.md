# ADR-0092: Context Window Token 百分比滑動視窗（取代則數觸發）

**狀態 (Status):** ✅ 已接受（分階段落地**全數完成**：Phase A / B0 / B / C / D / E 均已實作並驗證——見下方 Action Items [x]；`dispatcher._maybe_trim_after_chat` 雙觸發、AGENTS.md 禁令 28/57、ContextMeter、四組 SSOT 元件皆活體）
**日期 (Date):** 2026-06-14
**決策者 (Deciders):** 專案擁有者 + 主程

## 上下文 (Context)

現行 sliding window（ADR-0035）以「對話則數」（`upper_limit_rounds=150 / keep_rounds=100`）為切斷規則。**則數是很差的容量代理指標**：一則可以是 10 token、也可以是含工具輸出/圖片的上萬 token，「150 則」在 Gemini（百萬窗）與 Claude（20 萬窗）的實際占用天差地別。擁有者要對標桌面版 Claude Code 的 auto-compact + context meter：

1. 以「真實 token ÷ 模型 context window」的百分比顯示與觸發剪裁。
2. 送走的舊對話用 LLM 壓成摘要保留連續性（摘要失敗則優雅降級為純無損剪裁）。
3. UI 仿 Claude Code `/context`，分類別揭露各區塊（soul/工具/記憶/Skill/對話/摘要）佔多少。
4. 百分比閾值可調（如同現行則數閾值可調）。

### 關鍵數學陷阱（設計地基）
「送走 50% 對話 = context 降 50%」在真實系統**不成立**。真實 context = `OVERHEAD`（system/soul prompt + 注入記憶 + Skill 提示詞 + 工具定義；不可剪）+ `dialogue`（唯一可剪）。若 OVERHEAD 固定吃掉 40%，砍光對話也降不到「對話的 50%」。因此 config 語義必須重新設計成數學自洽。

## 決策方案 (Decision)

以「真實 token 百分比」取代「則數」作為 sliding window 的量測與觸發基準，分階段落地。**則數計算（ADR-0036 / 禁令 27）保留供 session 列表/UI 顯示，僅退役其 sliding-window 用途。**

### Config 語義（取代 keep_rounds/upper_limit_rounds）
| 欄位 | 含義 |
|---|---|
| `trigger_pct` | `usage_pct ≥` 此值才觸發剪裁 |
| `target_floor_pct` | 無損剪掉最舊對話 pair 直到 `usage_pct ≤` 此地板 |
| `summary_budget_pct` | 摘要 token 上限佔窗百分比 |

不變式（validator 強制）：**`target_floor_pct + summary_budget_pct < trigger_pct`**（防剪完立即重觸發迴圈）。邊界：OVERHEAD 已超地板時，剪到只剩 `min_keep_pairs`（預設 2）＋WARN，不報錯。

### 量測（使用者拍板：真 token ÷ 模型 window）
- **token 估算混合法**：以上一輪 API 回傳的 authoritative `input_tokens` 為分子權威值，本地以字元啟發式估「下一則增量」；無 usage 時降級純本地估，**永不拋例外**。不引入 tiktoken（各家 tokenizer 不一，對 Claude/Gemini 反不準）。
- **分母**：模型 context window，以「當前 active 模型」現算（換模型自然跟著變）。未知模型保守 fallback **128K**。

### 新 SSOT 元件（Phase A 已落地）
- `ToukaProxy/src/utils/token_estimator.py`：`estimate_tokens` / `estimate_messages_tokens`（CJK 感知、保守、永不拋例外）。
- `ToukaProxy/src/services/models/context_window_registry.py`：`get_context_window(model)`（內建表為主、`config/model_context_windows.json` 的 `windows`/`_default` 覆蓋為輔、未知回 128K）。
- `config/model_context_windows.json`(＋`.example`)：window 覆蓋表（bootstrap 自癒）。

### 分階段
- **Phase A（✅ 完成）**：token 估算器 + 模型 context_window 規格表（純後端基建，零行為改變、零接線）。
- **Phase B0**：統一 prompt 區塊邊界為一致 XML delimiter（soul 內文不動、只加外層 tag），讓分解能用單一 parser 切段；對標 Anthropic best practice。獨立回歸驗證人格/行為不變。
- **Phase B**：ContextMeter 改顯真 token%＋Claude Code 風格分類別分解條（純展示，仍不剪）。
- **Phase C**：剪裁引擎改用 token 地板（**仍只在 heartbeat 觸發，不碰禁令 28**）；含換模型冷卻（本輪 model ≠ 上輪則跳過、下輪才剪）。
- **Phase D（需禁令改寫）**：心跳＋聊天雙觸發（聊天側插在 `dispatcher._pump_stream_to_buffer` 完整 turn 落盤後，契合禁令 28 精神）＋摘要服務。摘要存於 session 級獨立欄位 `context_summary`（**不進對話樹**、固定前置注入、替換式、**摘要不寫 legacy**），避免被當對話再剪/再摘要。per-session 鎖序列化兩觸發點。
- **Phase E**：退役則數的 sliding-window 用途、文件收尾（則數顯示保留）。

### 禁令影響
- **禁令 28（Phase D 改寫，已獲擁有者書面同意）**：授權觸發點由「唯一 heartbeat finally」擴為兩處——(a) heartbeat finally、(b) dispatcher assistant turn 落盤後；仍禁串流中途/第三觸發點，兩點經 per-session 鎖序列化。對應新增**禁令 57**（聊天剪裁須非阻塞/排除無痕子對話/經鎖/不影響 finish）。
- **禁令 27（保留不改寫）**：則數計算續供顯示用途。
- **ADR-0035** 將於 Phase C/D 標記為 Amended by ADR-0092；**ADR-0036** 保留（則數顯示）。

## 後果與影響 (Consequences)

- **模型感知**：百分比隨模型實際容量調整，比則數準確得多——這是換基準的核心動機。
- **補漏**：Phase D 後長聊天（無心跳）也受剪裁保護，補上現行只在 heartbeat 觸發的空窗。
- **連續性**：摘要讓 agent 不致突然「斷片」；獨立欄位設計消滅「摘要的摘要」衰減。
- **取捨**：摘要引入一次 LLM 呼叫（成本/延遲/失敗），以「失敗降級純無損剪裁」緩解；「替換式」跨觸發會讓更早要點退出即時 context，但原文無損留 legacy 可檢索。
- **量測近似**：分解條各塊本地估算加總≠ authoritative 總數，需按比例校正（Claude Code 自身亦有此對不齊）。
- **靈魂保護**：不修改 `data/soul` 檔；Phase B0 僅在 soul 外層加 delimiter tag、內文逐字不變，須回歸驗證行為不變。
- **編號協調**：ADR-0091 已由並行的「搜尋備援鏈」佔用，本決策取 ADR-0092。

## Action Items
1. [x] **Phase A**：`token_estimator.py`（CJK 感知估算）＋ `context_window_registry.py`（內建表＋JSON 覆蓋＋128K fallback）＋ `config/model_context_windows.json`(＋`.example`)。
2. [x] **Phase A** TDD：`test_token_estimator.py`(10)＋`test_context_window_registry.py`(10)；lint_ssot 綠、vulture 綠、guard 32/32、全量測試綠、`tests/README` 同步。
3. [x] **Phase B0**：統一 prompt XML 邊界——新 `src/services/prompt_blocks.py`（tag 名稱 SSOT＋`wrap_block`）；soul 外層包 `<persona>`（內文逐字不變）；`context.py` 的 project→`<project_context>`、IDE→`<ide_context>`（取代 `[IDE Context:]`）、mission tag 改用常數；`group.py` IDE 同步。6 回歸測試（`test_prompt_block_delimiters.py`）；guard 32/32、pytest 1671、smoke 17/17、lint 0。
4. [x] **Phase B**：三家 handler 的 `token_usage_event` 加 `usage_pct`（claude 另附 `context_breakdown`，gemini/openai 僅百分比）；新 `context_breakdown.py`（XML 單一 parser 切段＋authoritative 校正）；前端 `AgentProgress` 加 `usage_pct`/`context_breakdown` 型別、ContextMeter 改混合顯示（有 token 用量→真 token%＋分解條；無→則數 fallback 保留 HAX G12）。`token_usage_event` 為 SSE 原始事件非 openapi 合約 → 免重生 api.ts（Guard #2 確認）。後端 10＋前端 2 TDD；guard 32/32、pytest 1681、vitest 543、smoke 17/17。
5. [x] **Phase C**：`SlidingWindowConfig` 加 `trigger_pct`/`target_floor_pct`/`summary_budget_pct`/`min_keep_pairs`＋`pct_invariant` validator（`floor+summary<trigger`），舊 round 欄位 deprecated 保留；`should_trim(usage_pct)`、新 `compute_split_by_tokens`（剪到地板、保底 min_keep_pairs、suffix-sum O(n)）、`_current_total_tokens`（authoritative token_usage 優先、否則本地估）、換模型冷卻（in-memory per-session）；`check_and_trim` 改 token 基準，**ACID 兩步與觸發點（heartbeat-only）原封不動、禁令 28 未破**。`sliding_window.json`(＋example) 加欄位；openapi.json＋api.ts 重生。7 新 TDD（共 25）；guard 32/32、pytest 1688、smoke 17/17。
6. **Phase D**（分兩段）：
   - [x] **D-summary（不碰禁令 28）**：session `context_summary` 欄位（`async_db` schema＋migration＋upsert 參數）；`dialogue_summarizer.py`（haiku 一次呼叫、fail-safe 回 None）；`prompt_blocks` 加 `conversation_summary` tag；`soul_service.inject_context_summary`（`<conversation_summary>` system 區塊、無痕由 SoulLayer gate）；`check_and_trim` 三步 ACID（A 寫 legacy→S 摘要 best-effort→B upsert＋`context_summary` 替換式）；`SlidingWindowConfig.summary_model`。NOQA registry 14→15、openapi/api.ts 重生。12 新 TDD；guard 32/32、pytest 1700、vitest 543、smoke 17/17。
   - [x] **D-trigger（動禁令 28）**：聊天側觸發（`dispatcher._maybe_trim_after_chat` 於 `_pump_stream_to_buffer` 持久化後 `spawn_logged` 非阻塞、僅家對話、無痕由既有閘門擋）＋`SlidingWindowService` per-session 鎖（`check_and_trim`→鎖包裝＋`_check_and_trim_inner`，序列化兩觸發點）；**改寫禁令 28**（授權兩觸發點、須經鎖）＋新增**禁令 57**（`AGENTS.md`，禁令計數 1–57）。4 新 TDD（鎖 1＋chat-trigger 3）；guard 32/32、pytest 1704、smoke 17/17。
   - [x] **D-ui**：`useSlidingWindowSettings` interface＋`SlidingWindowTab` 改三個 pct slider（trigger/floor/summary，client validate `floor+summary<trigger`），舊 round 欄位轉 optional 相容；TS 型別/設計token/vitest 543 全綠。
7. [x] **Phase E（收尾）**：因使用者拍板「則數顯示保留」，退役範圍縮減為文件收尾——ADR-0035 索引標「ADR-0092 修訂」；`SSOT_MASTER_MAP` 更新滑動視窗列為 token% 規則＋新增三列（Context Token 量測 / Prompt 區塊邊界 / 對話摘要欄位）；`CHANGELOG`／`README_HANDOVER` 收尾。**保留為 deprecated**：`SlidingWindowService.compute_split`/`count_rounds`（生產零引用、僅測試；vulture 不報、無害）、`keep_rounds`/`upper_limit_rounds` config 欄位（移除會破壞既有 config/openapi 相容）。則數計算 SSOT `count_rounds_from_linear`/`countRounds` 保留供顯示（禁令 27 不改）。guard 32/32、pytest 1704。
