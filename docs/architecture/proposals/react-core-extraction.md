# 提案:三 Handler 抽取重構——單一 ReAct 核心＋Provider Protocol(絞殺者)

## 狀態

🏁 **已收官(2026-07-14)**=[ADR-0156](../adr/0156-react-core-extraction.md)。E0–E4 全數落地(人類逐階段 commit):E0 特徵化 golden 30 檔對舊碼錄製、E1 react_core＋OpenAIAdapter、E2 GeminiAdapter、E3 ClaudeAdapter、E4 守衛錨＋矩陣收斂＋文件收官。**全程 golden 零位元組變動**;終態 pytest 3521/guard_all 41/41。計畫偏差(刻意,記於 ADR):三舊 handler 收 6 行薄殼而非整檔刪除(入口名凍結=factory 零改動);Protocol 較 §終態架構表實戰進化(send_round async generator/async append_tool_results/extract_usage(state)/telemetry_model/emits_context_breakdown/reasoning_parts,詳 ADR)。前置步驟 0(漂移修復批)=[ADR-0155](../adr/0155-handler-drift-fix-batch.md)。

## 日期

2026-07-13(解剖審計同日;審計數據:openai 410/claude 507/gemini 498=1,415 行,約 60%(842 行)為同一 ReAct 狀態機三份拷貝、12+ 處逐位元相同、~33% 為不可約 provider wire)

## 背景與動機

三 handler 是「一台狀態機養三份」:ADR-0146 保險絲、雙中斷閘、工具 fan-out、防幻覺、遙測收尾全部三份拷貝——ADR-0155 修掉的六個漂移 bug 就是這個結構的病理切片(每個修復都只落在其中一份)。**抽取後**:迴圈邏輯單一實作(修一次=三家生效)、新增供應商=寫一個小 adapter 而非複製第四個 handler、ADR-0137 懸置的「異模型 critic OpenAI 相容傳輸」與未來 any-llm 類外部庫(若有朝一日要)都獲得乾淨接縫。

## 目標與非目標

- **目標**:行為保持(behavior-preserving)的結構重構——特徵化測試凍結現行為,重構前後 SSE 輸出逐位元一致(時間戳/隨機 id 正規化除外)。
- **非目標(明列,嚴禁順手做)**:①Gemini reasoning(thought parts)補齊——已知遺留(ADR-0152 同類),屬新行為,收官後另案(新架構下=改一個 `parse_round`,成本大降);②keepalive/Honesty Guard 推廣到三家(拍板 K1/K2 預設「保持現狀」);③任何 wire/schema 變更(openapi 零變動);④外部統一庫引入(已於評估中否決)。

## 終態架構

### 單一核心 `src/handlers/react_core.py`

`run_react_loop(adapter, request, ctx) -> AsyncGenerator[str]`(async generator=既有 dispatcher 合約不變),**獨占**所有曾三份拷貝的邏輯:

- 身分 SSOT 回填＋`allow_react`＋ToolExecutor 建構;
- ADR-0146 工具輪保險絲全鏈(init/耗盡跳閘/denied round/WRAP_UP_NOTE/`wrapping_up` 防禦 break);
- 雙中斷閘(呼叫前=不發遙測即返、回合後=break 續發遙測——**兩者語意刻意不同,特徵化測試釘死**);
- `react_slots` 進度事件;
- asyncio.Queue 工具 fan-out＋log/事件分流＋`all_thinking_and_tool_logs`(含 `has_tool_call` 由 log 事件置真的現行語意——先保持,審計已標記其脆弱、收官後另議);
- **唯一一份** `yield_delta`/`yield_reasoning_delta`(下游 SSE 方言=凍結 wire:ADR-0081 `stream_buffer_service` 逐 chunk 緩衝重播＋Guard #38 封套＋ADR-0152 `delta.reasoning_content`);
- 防幻覺/記憶/誠實 guard 呼叫(ADR-0155 後已是 guard_service SSOT,核心只是唯一呼叫點);
- C4 計時語意(`upstream_gen_s` 累加)＋遙測收尾(accumulate/log_usage_audit/token_usage_event/touka_status/finish chunk/`[DONE]`);
- 工具呼叫 id 統一(adapter 供 id、缺者核心合成=C5 gcall 語意收編)。

### Provider Protocol(`typing.Protocol`,符 AGENTS DIP 鐵則)

每供應商一個 adapter(`src/handlers/adapters/{openai,claude,gemini}_adapter.py`,拍板 K4),約 10 個方法:

| 方法 | 職責 | 供應商差異落點 |
|---|---|---|
| `prepare_request(request, ctx) -> state` | 參數映射/degraded 警告拼接/快取設置 | openai:top_k pop・Grok safety;claude:`apply_tool_cache_breakpoint`+`reorder_system_stable_first`+`to_cached_system`(ADR-0127~0130);gemini:cachedContent＋**保存 system/tools**(ADR-0155 C3) |
| `send_round(state) -> raw` | 傳輸與錯誤映射 | openai:httpx POST+401 提示;claude:SDK+5s keepalive(K1);gemini:503 指數退避 |
| `parse_round(raw) -> Turn` | `Turn{reasoning, text, tool_calls[{id,name,args}], raw_assistant}` | ADR-0152 分頻各方言:openai=`reasoning_content` pop+`split_reasoning`;claude=thinking blocks;gemini=現況無(K3 遺留) |
| `append_assistant_turn(state, raw_assistant)` | 歷史寫回(**raw 透傳,核心不重建**) | claude:thinking/tool_use blocks 原樣+`cache_control` ephemeral 輪替;gemini:parts 原樣+re-cache(讀保存值) |
| `append_tool_results(state, results)` | 工具結果回填 | role:"tool" vs tool_result blocks vs functionResponse parts |
| `build_denied_turn(state, denied, note)` | 耗盡輪佔位+WRAP_UP_NOTE | 三家 wire 形狀各異(openai 獨立 user msg/claude 同 turn text block/gemini denied_parts) |
| `set_tool_choice(state, mode)` | auto/none/any | `"none"` vs `{"type":"none"}` vs `function_calling_config.mode`(含 gemini 條件式 AUTO 重置語意保持) |
| `extract_usage(raw) -> dict` | usage 欄位正規化 | prompt/completion vs input/output vs usageMetadata |
| `finalize(state, raw) -> dict` | 終端包裝 | claude 雙 egress(`is_native_anthropic`)留在 adapter 內部,不外洩為核心概念 |
| `provider_label()`/`context_breakdown_args()` | 遙測掛鉤 | claude 獨有 context 分解(ADR-0092 Phase B)=能力掛鉤非核心分支 |
| `mid_loop_hook(turn, state)`(選用) | 迴圈中介入 | gemini Honesty Guard 專用(K2=維持 gemini-only) |

### SOLID/SSOT 對齊

SRP:核心=迴圈政策、adapter=wire 翻譯;OCP:新供應商=新 adapter 零核心改動;DIP:`typing.Protocol`(AGENTS 鐵則);SSOT:迴圈邏輯/中斷字串/WRAP_UP 流程/計時/SSE 方言全部單份;Import SSOT:全頂層匯入(禁令 33)。

## TDD 策略(兩層測試網)

1. **特徵化 golden-SSE 套件(E0,重構前錄製)**:參數化三供應商×情境矩陣——純文字/單工具輪/多工具並行/預算耗盡收尾/呼叫前中斷(無遙測)/回合後中斷(有遙測)/幻覺・記憶・誠實警告/degraded/遙測尾(token_usage・touka_status・finish・[DONE])/reasoning 分頻(openai+claude)/gemini 503 retry/gemini re-cache/claude keepalive/claude 原生 egress。以**假時鐘＋固定 request_id** 正規化非決定性(K5),對現行 handler 錄 golden chunk 序列,重構全程斷言**逐位元一致**。基建沿用 ADR-0155 的鷹架(fake httpx/SDK/cache、mock 執行器先 log 後 sentinel、`record_tool_usage` 真呼叫)。
2. **Protocol 契約測試(紅測先行)**:一份參數化契約套件對所有 adapter 跑同一組義務(如「denied turn 後 set_tool_choice(none) 必達」「raw_assistant 透傳不重建」「extract_usage 正規化鍵齊」),先對 FakeAdapter 紅→核心綠,再逐 adapter 掛上——契約套件本身=Protocol 語意的 SSOT。
3. 既有 tests/proxy(integration/hard_interrupt/progress/tool_events/failure_visibility/sovereignty)全程綠=第三層安全網;`tests/tool_eval` 為 live 行為後盾(owner 手動)。

## 階段(絞殺者;每階段收在 guard_all 41/41＋pytest 全綠,人類逐階段 commit)

```
E0  特徵化 golden-SSE 套件＋正規化基建(零生產改動)             ~1–1.5 session
E1  react_core＋OpenAIAdapter;factory:124 改接核心             ~1–1.5 session
    (首發理由:wire 最簡、無快取機構、Grok/DeepSeek/OpenAI 全走它=實測面最大)
E2  GeminiAdapter;factory:88 改接                               ~1.5–2 session
    (Protocol 壓力測試:503 retry/快取生命週期/Honesty Guard 收 mid_loop_hook/
     條件式 tool_config AUTO 重置語意保持)
E3  ClaudeAdapter;factory:50 改接                               ~1.5–2 session
    (雙 ingress/egress、cache 斷點輪替、keepalive 依 K1)
E4  刪三個舊 handler＋守衛錨(靜態測試:ADR-0146/中斷字串等迴圈邏輯**只存在於
    react_core**;handlers/ 目錄僅剩 adapters)＋既有 mock 站點 codemod(tests/proxy
    的 patch 目標隨 adapter 邊界遷移,比照 P7-3/4 機械化)＋文件收官            ~1 session
```

合計 **6–8 sessions**(漂移修復已前置完成,較審計原估 7–9 略減)。**任一階段可停**:停在 E1 後=openai 家族已單源,claude/gemini 維持舊 handler 照常運作(factory 分流=天然共存)。

## 拍板點(owner 決策)

| # | 問題 | 建議 |
|---|---|---|
| K1 | claude 的 5s SSE keepalive:維持 claude-only(行為保持)或升核心三家共用 | **維持 claude-only**(重構期零行為變化;升三家=收官後另一個一行小案) |
| K2 | gemini Honesty Guard(mid_loop_hook):維持 gemini-only 或推廣三家 | **維持 gemini-only**(行為保持;推廣=另議) |
| K3 | Gemini reasoning(thought parts)缺口:E2 順手補或收官後另案 | **另案**(保持重構純度;新架構下=改一個 parse_round) |
| K4 | 檔案佈局:新 `src/handlers/adapters/` 套件(舊 handler E4 刪除)或就地改寫 | **新套件**(絞殺者共存期清晰、E4 原子刪除) |
| K5 | golden 正規化:假時鐘＋固定 request_id(monkeypatch) | 照案 |

## 風險與緩解(審計 synthesis 全單,擇要)

1. **凍結的下游 SSE 契約**:ADR-0081 重播逐 chunk 位元組比對、Guard #38 封套、前端 ThinkingFold 解析——任何 chunk 順序/形狀變動=靜默破壞。緩解=E0 golden 逐位元斷言先行,動任何生產碼之前先有網。
2. **raw_assistant 必須不透明透傳**:claude thinking/tool_use id 配對、gemini parts——核心若從正規化 Turn 重建歷史=掉 thinking block/斷 id 配對。緩解=契約測試明文義務+特徵化多輪情境。
3. **async generator 語意**:yield 即背壓、stop=dispatcher 取消 producer 的 CancelledError 傳播、「呼叫前中斷不發遙測 vs 回合後中斷發遙測」的刻意不對稱。緩解=中斷×時點的特徵化情境+取消傳播測試。
4. **測試夾具翻新量**:tests/proxy 直接 patch handler 內部(httpx.post/anthropic_client/ToolExecutor)——adapter 邊界後 patch 目標遷移。緩解=E4 集中 codemod(P7-3/4 前例)、特徵化套件從 E0 起就以「可遷移的 patch 面」設計(patch adapter 方法而非 httpx,舊 handler 錄 golden 時例外)。
5. **gemini 特例語意**(is_force_tool ANY/AUTO/NONE 舞步、tool_config 條件式重置、mid-loop continue 不執行工具):過度一般化=改變強制工具語意。緩解=這些全留在 GeminiAdapter/mid_loop_hook,核心不長 gemini 形狀的分支。

## 驗收(每階段＋終態)

- 特徵化 golden 套件全綠**且 golden 檔零變動**(E0 錄製後凍結;任何 diff=行為變化=停手回報);
- guard_all 41/41、後端 pytest 全綠(基線 3469+新增)、lint_ssot clean、ruff 基線持平;
- openapi/api.ts 零變動、vitest 不動;
- E4 後:三舊 handler 刪除、迴圈字串唯一存在於 react_core(守衛錨測試)、`git grep "wrapping_up"` 等僅命中核心。

## 文件義務(E4)

ADR 取下一號＋README 索引;`PROXY_INTERFACE.md` handler 架構節(若有描述舊三 handler 的段落隨改);HANDOVER 現況;tests/README 隨 owner 對帳慣例;NOQA registry(若有新增,禁令 12);AGENTS §8 大一統工具鏈不受影響(工具面零變動)。

## 與外部庫的關係(已於評估否決引入,存檔語意)

抽取後 `send_round`/`extract_usage` 即外部統一庫(any-llm 類)的天然接縫——**若**未來要接冷門供應商,寫一個包它的 adapter 即可,核心與既有三家零受累。這是本案附帶買到的選擇權,非本案目標。
