# ADR-0271：Claude 5.5 家族接手前要先修（依用途少想、送前最壞花費、價目分檔、審查員等 60 秒）

- 狀態：✅ 已接受（維護者 2026-10-16 核准 r55 計畫：審查員照模型預設思考強度、只加長等待；要做延遲實測工具但跑之前另經同意）
- 日期：2026-10-16
- 決策者：維護者
- 影響範圍：ToukaProxy（`services/models/model_rules.py`、`handlers/adapters/base.py`、`handlers/adapters/claude_adapter.py`、`adapters/openai_to_claude.py`、`adapters/schemas.py`、`services/completion.py`、新 `services/cost_guard.py`、`services/autonomy.py`、`services/providers/pricing.py`、`services/telemetry.py`、`handlers/react_core.py`、`routers/proxy.py`、`models/autonomy.py`、`config/autonomy.json.example`）；ToukaChat（`AutoReviewerSettings`、`useAutoReviewSettings`、兩語翻譯、`types/api.ts` 重產）；ToukaIDE（`InlineCompletionProvider`）；`openapi.json` 重產；人工實測工具（新 `tests/smoke/manual/thinking_latency_probe.py`、`memory_eval_probe.py`、`tests/live_api_policy.py`、`tests/smoke/manifest.py`）
- 相關：修訂 [ADR-0244](0244-model-layer-stage-1b.md)（`_shape_claude` 改合併 `output_config`、`complete_once` 多一道送前估算、畫面與稽核的整次 run 金額改每輪分檔）、修訂 [ADR-0247](0247-model-layer-stage-3a.md)（5.5 三列、default_on 下限在思考真的關掉時免除、原生入口補下限、P24 Haiku 5.5 分檔與 Sonnet 5.5 另列）、修訂 [ADR-0246](0246-model-layer-stage-2b-2c.md)（審查員等待預設 20→60 秒）、修訂 [ADR-0268](0268-memory-eval-thinking-models.md)（「5.5 思考關不掉」只對 Opus 5.5 成立；評估工具取貴檔）、修訂 [ADR-0254](0254-iso-c1-small-fixes-and-execution-gaps.md)（計分卡情境上限碰到 5.x 補到 8,192）；五份的狀態區都已加回指本 ADR 的註記。禁令 16、39、61、64 與 ADR-0214 照舊、一行沒放寬。

## 背景

- Claude 5.5 家族不帶參數也會思考。Touka 為了不讓思考吃光額度，送 5.x 時把 `max_tokens` 拉到至少 8,192（ADR-0247），但從沒告訴它「這是小工作」：審查員、摘要員、評分、IDE 行內補全都變慢變貴；審查員只等 20 秒，用 5.5 當審查員時多半等不到就轉人，Auto 等於失效。
- 官方（2026-10-16，網址見參考）：Opus 5.5 思考關不掉（`thinking.disabled` 在任何 effort 都 400），只能降 `output_config.effort`（預設 medium）；Sonnet 5.5 送 `disabled` 會 400，要關「先想再答」得送 `between_tools`，且 effort ≤ high；Haiku 5.5 可以 `disabled`，同樣要 effort ≤ high。effort 五級 low／medium／high／xhigh／max；官方沒給「少想能快多少」的數字。
- 附帶發現：Claude 路徑一直沒轉送 OpenAI 格式的 `stop`；原生 `/v1/messages` 送來的 `output_config` 被悄悄丟掉；原生入口的 `max_tokens` 不補下限（評分頁 512／1,024 碰到 5.x 會被思考吃光）；花費上限只看已花、不看這次可能花多少；價目表 Haiku 5.5 一律記貴檔（高估 5 倍）、Sonnet 5.5 快取命中記成 Sonnet 5 的 0.20（高估 2 倍）。

## 決策

### 1. 依用途決定思考強度（只對 Claude 5.5 三型號生效）

- 用途表在 `services/completion.py`（`_THINKING_PROFILES`、`thinking_profile_for`）：`judge`（創建評分）、`summarizer`（前情摘要）、`inline_completion`（IDE 行內補全）＝`minimal`。**審查員（`auto_reviewer`）與互審（`critic`）刻意不列**＝照模型預設：維護者決定審查員不降思考強度（審查判斷不打折）；互審退回會擋任務完成，是品質關卡。
- 共用詞彙在 `handlers/adapters/base.py`：`ThinkingProfile = Literal["minimal"]`、`THINKING_PROFILE_KEY = "thinking_profile"`。寫入者只有 `routers/proxy.py`（主迴圈）與 `completion._adapter_context`（旁路，每次新建 dict，主對話 context 的任何鍵都不會流進審查員等旁路呼叫）；讀者只有 `ClaudeAdapter.prepare_request`（靜態測試釘住）。`completion.py` 不讀規則表（禁令 39 的讀者名單不變）。
- 規則表（`model_rules.py`）：`ModelRules` 新欄位 `effort_levels`（低到高；空＝不送 effort）與 `thinking_off_mode`（`disabled`＝Haiku 5.5、`between_tools`＝Sonnet 5.5、`unsupported`＝Opus 5.5、None＝Touka 不調整）；`_CLAUDE_ROWS` 的 5.5 拆成 opus／sonnet／haiku 三列。**5.6 以後（含 6.x）一列 `((5, 6), None, _CLAUDE_5)` 照舊**：不送 effort、不改寫 disabled、8,192 下限照舊，等查過官方再加列（新型號不收這些參數時請求會整個被拒）。5.5 但認不出系列、4.x、5.0、Fable、Mythos 也照舊。
- `claude_thinking_plan(model, *, thinking, effort, minimal)`：優先序是客戶端明確值＞用途 minimal＞廠商預設。effort 只送給列了它的型號（舊型號與未知等級丟掉並 warning）；minimal 補最低 effort（low）；能關思考（Haiku／Sonnet 5.5）且 effort ≤ high、客戶端沒指定思考時就關（送 `disabled`／`between_tools`）；客戶端明確要思考（`-thinking` 尾碼或原生 thinking）時尊重客戶端，只補 effort。Opus 5.5 只降 effort。
- `claude_send_max_tokens(..., *, thinking_off=False)`：default_on 的 8,192 下限只在思考真的關掉（Haiku／Sonnet 5.5）時免除，Opus 5.5 永遠有下限。OpenAI 入口只在思考關掉時以請求原值重算（轉換器已拉到 8,192）。

### 2. 用途標頭只能讓 Claude 5.5 少想

- IDE 行內補全送 `X-Touka-Purpose: inline-completion`（另加 10 秒逾時與 VS Code 取消，見決策 9）。`routers/proxy.py` 的 `_client_thinking_profile` 只認白名單 `inline-completion`（ASCII、不分大小寫、只去頭尾空格與 Tab），換成 context 專用鍵；其他值（`judge`、`minimal`、變形、超長、非 ASCII、NBSP）一律忽略、不報錯。兩個聊天端點（`/messages`、`/chat/completions`，含 `/v1`）都適用，原生入口帶了也一樣只讓它少想。
- 比照禁令 16：標頭不是身分，不影響路由、工具、權限、模式、花費上限；聊天端點的 `require_authenticated` 一行沒改，沒登入只帶標頭照樣 401、不進 pipeline。測試以真 pipeline 比對「有無標頭只差 `headers` 與 `thinking_profile`」，並以真 ToolExecutor／AutoReviewer／complete_once 驗證主對話帶標頭時審查員那次呼叫的 context 沒有這個鍵、送出內容逐 byte 相同。

### 3. 原生 `/v1/messages`

- `AnthropicRequest.output_config`（`AnthropicOutputConfig`，`extra="allow"`，`effort: str | None`，≤16 字）只轉送 effort，且只給規則表列了它的型號；其他鍵（`format`、`task_budget`…）不轉送，只記鍵名 warning。effort 用字串不用列舉：等級清單只在規則表一處，未知等級丟掉並 warning、不 422。
- `thinking.disabled` 依型號改寫：Opus 5.5 不送並 warning（照樣想、照樣計費）；Sonnet 5.5 effort ≤ high 改 `between_tools`，xhigh／max 不送並 warning；Haiku 5.5 effort ≤ high 照送，xhigh／max 不送並 warning（官方：會 400）。舊型號照原樣。
- 輸出上限：一律以實際 `max_tokens` 套 `claude_send_max_tokens`（不看 `model_fields_set`：計分卡刻意不標它，ADR-0254），所以 5.x、Fable、Mythos 的 4,096／1,024／512 補到 8,192，沒給時 schema 預設 8,192 不變；**`enabled`＋budget 的請求也補到 budget＋1,024**（以前小於 budget 會被廠商 400）。思考真的關掉時照請求值。4.x 不帶思考照舊。
- warning 只記型號、等級名與鍵名，不記訊息內容、標頭原值或整包 `output_config`。

### 4. OpenAI 入口的 `stop` 轉送

- `adapters/openai_to_claude.claude_stop_sequences`：字串轉一項；丟掉非字串、空字串與純空白（官方 OpenAI 相容頁：只有非空白的停止字串有效）；保序去重；沒剩任何一項就不送。ClaudeAdapter 在 OpenAI 入口送 `stop_sequences`。官方 Messages API 參考頁沒寫數量或長度上限，所以不設上限。原生入口的 `stop_sequences` 仍不轉送（schema 沒這欄，見限制）。

### 5. `complete_once` 送前最壞估算（加嚴）

- 新 `services/cost_guard.py`：`estimate_input_tokens`（實際要送的內容 JSON 每字元 1 token＋廠商餘量 1,024）、`worst_case_input_price`（一般輸入與寫入快取取較貴）、`worst_case_usd`、`worst_case_call_usd(model, payload, max_output_tokens)`（依估計提示長度選檔）。估法和人工實測的 `tests/memory_eval/live_bounds.py` 相同，產品不 import tests，parity 測試釘住。
- `AutonomyService.would_exceed(identity, worst_usd)`：上限沒開或沒設上限＝False；已花＋最壞 > 上限＝True（剛好等於不算；`is_over_budget` 的 `>=` 照舊，改用共用的 `_cap_limit`、行為不變）。沒有新設定欄位，主上限仍是 opt-in、預設關。
- 接點在 prepare＋shaper 之後、`_send_once` 之前：`worst_case_call_usd(adapter.telemetry_model, 實送 kwargs／payload, 實送輸出上限)`；超過就回既有 `over_budget`（沒送、不記帳）。實送輸出上限讀送出欄位（Claude／Responses 的 kwargs、相容 Chat payload、Gemini `generationConfig.maxOutputTokens`），讀不到用請求值，取較大。既有的失敗處理照舊：審查員轉人／無人拒絕、摘要員退回無損剪裁、互審合成退回、評分 0 分。禁令 64 的執法點（單一路由、`max_retries=0`、收到重試通知即中止、不跨廠商）一行沒動。

### 6. 價目表（官方 https://platform.claude.com/docs/en/about-claude/pricing ，2026-10-16）

- `Rate.long_prompt=(門檻, 長檔費率)`、`Rate.for_prompt`；`Rate.cost_usd` 每個請求依「未快取輸入＋快取讀＋快取寫」選檔（官方每請求獨立計價），`Rate.flat_cost_usd` 不選檔。`HAIKU_5_5_LONG_PROMPT_TOKENS = 100_000`。
- Haiku 5.5：≤10 萬 $0.10／$0.50（快取讀 0.01、寫 0.125）；>10 萬 $0.50／$2.50（0.05、0.625）。新列 `claude-sonnet-5-5`：快取命中 0.10（Sonnet 5 仍 0.20）。只建 5 分鐘快取寫入價（Touka 只用 5 分鐘 ephemeral）。
- 整次 run 金額：`_RunUsage` 另記落在長檔那幾輪的加總，`telemetry.run_cost_usd(totals, long_totals, *, model)` 短檔與長檔各算一次；畫面 `token_usage` 事件與稽核 CSV（`log_usage_audit(..., cost_usd=...)`）用同一個值，和每輪入帳的 `token_usage.json` 一致。沒分檔的型號算式等於改前，特徵化 golden 沒重錄、全部照過。

### 7. 審查員等待 60 秒

- `AutoReviewConfig.timeout_s` 預設 20→60（`gt=0, le=120` 不變），範例檔同步。**逾時不是放行條件**：審查員要明確判 allow 才放行，逾時＝轉人（有人在場）或拒絕（無人在場），所以加長不放寬規則；但用 5.5 當審查員時，原本逾時轉人的動作改由審查員判斷，實際自動放行會比現在多（恢復 Auto 原本的設計）。審查員卡住時要等約 65 秒（60＋寬限 5 秒）才跳審批卡。
- live `autonomy.json` 若是從舊範例長出來、已寫 `"timeout_s": 20`，改預設不會生效，也不做啟動遷移：**在設定頁「審查員等待秒數」看到 20 時，改成 60 存檔**。設定頁新欄位見 CHAT_INTERFACE。
- `services/auto_reviewer.py` 一行沒改（另一批正在改那幾段）。

### 8. 人工實測工具（只寫、未跑）

- 新 `tests/smoke/manual/thinking_latency_probe.py`（operation `manual_thinking_latency_probe`）：直接呼叫產品 `complete_once`，minimal＝用途 summarizer、default＝用途 critic，啟動時核對用途表；只收三個 5.5 型號且路由是 Anthropic；每組重複 1..5、請求 1..30（型號×強度×次數不得超過）、請求裡的 `max_tokens` 1..1,024、`--budget` ≤3。每個請求外連前依序核對實送上限（≤核准的每組實送值與 8,192）→ 已花＋最壞（該型號價，Haiku 5.5 取貴檔）≤ 上限 → 核銷請求數；任何一關不過都不送，任何一個請求失敗整批停、非零結束。報告不印回應原文；先印報告再刪沙盒（共用記憶評估工具的釋放流程）。**本 ADR 合併時沒有執行過**；要跑時照禁令 61 另附精確命令與金額取得維護者同意（`docs/proxy/live-check-backlog.md` 的 T1）。
- 記憶評估工具 `memory_eval_probe.py` 改用 `_dearest_rate`：有分檔就取貴檔，維持原估價（Haiku 5.5 仍 0.625／2.5）；結清改依實際提示長度選檔。**工具改過，之後再跑記憶評估要照新版重新取得同意**。

## 會改變的行為

- 摘要員、評分、IDE 補全用 Haiku／Sonnet 5.5 時：送 `disabled`／`between_tools`＋effort low，輸出上限回到請求值（摘要員寫超過預算會截斷→退回無損剪裁；以前 8,192 讓它寫超過）；Opus 5.5 只送 effort low、仍 8,192。摘要員預設模型是 Haiku 4.5 且預設不啟用，預設設定下沒有影響。審查員、互審送出內容不變。
- 所有用 OpenAI 格式送 Claude 的請求，`stop` 開始生效；IDE 補全的 "```" 照舊保留（模型以程式碼圍欄開頭時補全會變空，和 OpenAI 相容路徑一致）。
- 原生入口：客戶端的 effort 開始照送（5.5）；`disabled` 依型號改寫或不送；5.x 的小輸出上限補到 8,192（ToukaChat／IDE 選 anthropic 協定的 4,096、計分卡 512／1,024）；`enabled`＋budget 補到 budget＋1,024。
- 花費上限有開時，`complete_once` 送前就可能回 `over_budget`（例：上限 $1、已花 $0.95，Opus 5.5 審查員最壞約 $0.17 → 不送、轉人）。
- Haiku 5.5 的入帳、畫面「本回合花費」與稽核 CSV 金額變準（一般請求不再高估 5 倍）；Sonnet 5.5 快取命中金額減半。
- 審查員新安裝預設等 60 秒；設定頁可看可改。

## 鬆緊

- 依用途少想（評分、摘要員、IDE 補全）：不變（只影響速度與花費；摘要品質可能略降【推測】）。互審：不動。審查員：照舊強度（維護者決定），等待 60 秒＝規則不變、實際自動放行回到原設計。
- 用途標頭：不變（只能降思考，標頭不是身分）。stop 轉送、原生入口 effort／disabled 改寫／下限：不變（行為改變已列）。
- 送前最壞估算：加嚴。價目表：不變（金額變準）。

## 替代方案

- 審查員也少想：審查判斷可能變粗、要再加一次對抗式審查；維護者決定審查員照舊、只加長等待。
- 互審也少想：等於放寬品質關卡；不採。
- 依模型名或請求內容猜用途：猜錯時會讓不該少想的請求少想；改成明確白名單標頭與內部用途表。
- adapter 直接讀原始標頭：標頭變成多一個讀者、測試難釘；改成 proxy.py 換成專用鍵、adapter 只讀鍵。
- 原生入口只對 `model_fields_set` 補下限：計分卡刻意不標，會被跳過；改成照實際值。
- 5.6 以後沿用 5.5 列（版本下限語意）：新型號不收這些參數時整個請求被拒；改成先照舊。
- 送前估算放主迴圈第一輪：範圍外，列候選。
- 啟動時把 live `timeout_s` 20 遷移成 60：會寫使用者的設定檔；不遷移（審查時決定），使用者自己在設定頁改。

## 後果與限制

- 子代理（`_derive_context` 的 `model_copy`）與群聊（`nested_ctx`）會繼承主對話的少想；只降思考、不碰安全。IDE 補全設一般模型時本來就不跑工具，只有把補全模型設成靈魂才可能生出子代理。
- IDE 中止（逾時或取消）後，Proxy 端的非串流請求不會跟著停，仍會花錢【推測：Starlette 不取消非串流 endpoint】。
- 並行的 `complete_once` 不互相預留（不是 SpendLedger 語意），同時送出時合計可能超過上限一點；主迴圈第一輪送前不檢查。
- `config/model_pricing.json` 覆寫仍是單一費率；覆寫 Haiku 5.5 就失去分檔。
- 原生入口的 `stop_sequences` 仍不轉送（schema 沒這欄）；原生客戶端送 `between_tools` 給 Opus／Haiku 5.5 照原樣轉出（廠商會 400）。
- 少想對延遲與輸出 token 的實際效果、60 秒是否足夠，都沒有實測（官方沒給數字；實測工具已寫、待維護者同意）。

## 和設計不同的地方

- 標頭只認 ASCII、只去頭尾空格與 Tab；`thinking_profile` 一律由 proxy.py 覆寫（含 None）。
- 原生 `enabled`＋budget 也補到 budget＋1,024（設計寫法的自然結果，列入行為改變）。
- 5.6 以後照舊（計畫審查時要求的修改）。
- 實測工具共用記憶評估工具的上限解析、取貴檔與沙盒釋放；行數約 +485（工具 285、離線測試 171、登記與說明 29），比計畫的 +150 多，因為禁令 61 的保護逐項做了。

## 驗證

本機 Windows、3.12 venv、fnm Node 24.21.0。全部測試在 conftest 沙盒或 mock；沒有打任何付費 API、沒有執行實測工具、沒有啟動 Touka。

- 新測試：`test_claude_thinking_profile.py` 57、`test_complete_once_thinking.py` 44、`test_thinking_purpose_header_security.py` 46、`test_cost_guard.py` 30、`test_run_usage_tiers.py` 11、`test_thinking_latency_probe.py` 20；擴充 `test_model_rules.py` 151→215、`test_internal_callers_max_tokens.py` 9→12、`test_pricing.py` 37→45、`memory_eval/test_probe_bounds.py` 47→48；`proxy/upstream/test_upstream_claude.py` 的 stop 斷言改成「以 `stop_sequences` 送出」（預期的行為改變）；`test_complete_once_contract.py`、`test_anthropic_base_url_pinned.py`、`test_anthropic_sdk_env_security.py`、`test_side_caller_billing.py` 的假預算物件只補 `would_exceed`（回 False），斷言不改；`test_live_api_consent.py` 的 operation 集合多一筆。ToukaChat `AutoReviewerSettings.test.tsx` 10→18、`useAutoReviewSettings.test.ts` 3→15；ToukaIDE `inlineCompletion.test.ts` 5→12。先紅後綠。
- 突變：思考路徑 M1～M13（含 13a／b／c）、M6b、M18、MR1（旁路一律少想＝模擬繼承主 context）；送前估算接點 MS1～MS6；5.6 列 MF3；花費 M14a～e、M15a～c、M16a～d、M17a～d；前端 M19a～f、M20a～g；實測工具 M21a～i；最後全部有測試變紅（M6b 加跑規則表測試、M20e 補測試後才變紅）。
- 全量（程式 HEAD `4a433502`＋文件）：pytest 10639 passed／7 skipped／6 failed（6 個是本機 Docker Desktop 的 `test_local_compose_bindings`，以 CI 為準；7 個跳過與 ADR-0251 的基準相同）；ToukaChat vitest 277 個檔、1738 個測試全過，前端守衛（prune 以外）通過；ToukaIDE 95 個測試全過、typecheck 通過。`guard_adr_integrity.py`（配號後 0001..0271 連續、修訂雙向一致）、`guard_noqa_registry.py`、`lint_ssot.py`、`guard_native_tool_contracts.py`（基準不動）、`guard_module_boundaries.py`、`guard_endpoint_auth.py`、文件契約測試、ruff（改到的產品程式沒有新增問題）通過。

## 回復

- 不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）；程式可分段撤：用途表／規則表新欄位、原生入口與 stop、送前估算、價目分檔、審查員預設各自獨立。撤回 `timeout_s` 預設不會改使用者已存的設定。Guard #45 基準、禁令、AGENTS.md、SECURITY.md 都沒動。

## 參考

- https://platform.claude.com/docs/en/build-with-claude/thinking
- https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting
- https://platform.claude.com/docs/en/build-with-claude/effort
- https://platform.claude.com/docs/en/about-claude/models/opus-5-5/migration-guide 、…/sonnet-5-5/migration-guide 、…/haiku-5-5/migration-guide
- https://platform.claude.com/docs/en/about-claude/pricing
- https://platform.claude.com/docs/en/api/messages
- https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk
- 以上皆 2026-10-16 查。
