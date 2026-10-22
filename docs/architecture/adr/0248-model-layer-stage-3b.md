# ADR-0248：模型溝通層第 3b 段（上游串流、OpenAI Responses、OpenAI／Gemini 接法）

- 狀態：✅ 已接受（維護者 2026-10-12 核准 3b 計畫；計畫第 3 版經審查裁定 1～7；維護者 2026-10-12 另核准期望翻轉）
- 日期：2026-10-12（3b-U6 定稿 2026-10-13）
- 決策者：維護者
- 影響範圍：`handlers/react_core.py`（重試、finish_reason、拒答提示、沒跑完那一輪的記帳）、`handlers/adapters/`（`claude_adapter`、`openai_adapter`、`gemini_adapter`、新 `responses_adapter`、新 `stream_timeout`、新 `sse_stream`、新 `retry`、`usage`、`base`）、`handlers/openai_handler.py`、`adapters/openai_to_gemini.py`、`services/dispatchers/{route_family,factory,utils}.py`、`services/completion.py`、`services/models/model_rules.py`、`config.py`（`OPENAI_RESPONSES_BASE_URL`、`OPENAI_SDK_ENV_OVERRIDES`）、`core/logging_config.py`、`main.py`、`tests/smoke/manual/live_chat_probe.py`、`ToukaProxy/pyproject.toml`／`uv.lock`（openai `~=3.24.0`）、對應測試與文件；golden 只刻意改 6 份
- 修訂 [ADR-0247](0247-model-layer-stage-3a.md) 決策 4：非串流上限不在 3b-U1 後移除，改成主迴圈不串流送出時才壓（附註 4）
- 修訂 [ADR-0242](0242-model-layer-stage-1a.md) 的 KF 表：KF-7a、KF-8、KF-9 與兩條既知（finish_reason 一律 stop、Gemini 不送輸出上限）改為已修
- 修訂 [ADR-0156](0156-react-core-extraction.md) 的 `send_round` 說明：重試與重試狀態文字改由 react_core 產生，adapter 每次只送一次

## 背景

3a 之後主迴圈三家都還是非串流：長回覆要等整輪，Claude 被壓在 21,333 個輸出 token，逾時只有一個總時間，取消時已收到的用量也拿不到。OpenAI 新模型只走 Responses API。1a 釘住的 KF-7a、KF-8、KF-9 交給本段。3b-U0 在 repo 外的實驗目錄（未入版控，只 import 第三方 SDK）跑 Q0～Q8 與 C1，依 §6-1 準則選每一家的接法。

## 決策

1. **接法（裁定 1～4）**
   - Claude：anthropic 1.11 `messages.stream()`（C1：最終 Message 和 `create()` 完全相等）。
   - 相容廠商 Chat：自寫 SSE。Q3 不過：b2 `stream()` 累加器遇到工具 index 缺號會 IndexError；e3 沒金鑰的連線，空字串被拒、傳 None 改讀環境變數 `OPENAI_API_KEY`、給假值則多送 Authorization。
   - Gemini：自寫，依據只有 Q5。**Q5 部分不過（h）**：實驗腳本 `q5_genai_stream.py`（未入版控）在 part、candidate、頂層各塞一個 SDK 不認得的欄位，google-genai 2.28.0 串流解析後，part 層與頂層那兩個被靜默丟掉、沒有任何例外（實驗輸出的 `[NOTE] Q5 h`；candidate 層沒印出，不推論）。adapter 把 part 原樣回送給下一輪，走 SDK 就還原不成 REST 形狀，Google 日後新增的必回送欄位會在升版前被吃掉。Q2、Q6 照注入 client 就滿足，不拿來當理由。
   - OpenAI Responses：openai SDK（Q1、Q4、Q6～Q8 過），**只給「非自訂連線、provider＝官方 OpenAI、規則表 `uses_responses_api`」的路由**：`resolve_model_route` 回 `responses` 種類，factory（`handle_responses_request`）與 `complete_once` 共用，不只看模型名。自訂連線一律留在 Chat 自寫路徑。相容廠商要走 Responses，得先解決 Q3-e3。
   - `complete_once` 遇到 Responses 路由改走非串流 `responses.create`：不重試、不套閒置逾時，停止原因、用量（扣 `cached_tokens`）、`store=false` 沿用 adapter；契約另在 `test_complete_once_nonstream_contract.py`。
2. **用量（裁定 5、6）**
   - 自訂連線維持非串流（路由層 `ModelRoute.custom`→context `custom_connection`，不看模型名），回應裡就有用量，1b 記帳照舊。
   - 只有規則表的已知廠商（`COMPAT_STREAM_PROVIDERS`＝OpenAI、DeepSeek、Grok，看 route 的 provider）才串流，並送 `stream_options.include_usage`；串流完成仍沒有用量時估算並記警告，不靜默不入帳。
   - 沒跑完的一輪（取消、首事件／閒置／整輪逾時、沒有完整訊號就斷線、200 之後出錯、重試放棄的那一次）：廠商已回報的照記，其餘估算（輸入用請求內容、輸出用已收到的 delta；`usage.estimate_partial_usage`、`ProviderAdapter.partial_usage`）。估計標記只放在日誌與 `token_usage` 事件（`estimated: true`），用量紀錄檔仍是四欄（P20）。串流還沒開（HTTP 200 之前失敗）不記也不估；`complete_once` 永遠不估，錯誤與逾時時帳本是空的。
3. **送出規則 D1～D7**
   - D1：adapter 預設非串流、只送 1 次，只有主迴圈 handler 明確打開串流。`complete_once` 三家照舊：Claude `messages.create`；相容廠商與 Gemini `httpx.AsyncClient.post(url, headers=, json=, timeout=)`，網址沒有 `alt=sse`；`test_complete_once_contract.py` 沒有改。
   - D2：串流旗標只在送出當下合併，不寫進 payload／kwargs，錄製台快照不變。
   - D3：兩段逾時加整輪上限（`handlers/adapters/stream_timeout.py`）：第一個位元組前 120 秒（`FIRST_EVENT_TIMEOUT_S`），之後位元組之間閒置 60 秒（`IDLE_TIMEOUT_S`，**推測值**，長思考期間的 ping 間隔沒實測，列入 L3），整輪 600 秒（`ROUND_TIMEOUT_S`）。任何位元組（ping、`:` 註解行、空行）都重設閒置計時；Claude 與 Responses 的 SDK 會吃掉 ping，所以在 httpx2 傳輸層用 response event hook（`liveness_event_hooks()`）記錄最後收到位元組的時間。會先送空殼事件的三家（Responses 的 `response.created` 等生命週期事件、相容 Chat 只有 role 的 chunk、Claude 的 `message_start`）收到第一個有內容的事件之前，期限不早於首事件的 120 秒（ping 照樣往後延）；Gemini 每個 chunk 都有內容，照第一個位元組（R-3b ② M2，推測性的防禦）。逾時區塊不跨過 yield；取消時等這一輪收尾、關上游、`aclose` round_gen，不回半輪。
   - D4：openai SDK 建構顯式傳 `api_key`、`base_url`（來自路由 context，不得是 None）與不跟隨轉址的 `http_client`；缺金鑰或 base URL 時不建 client（SDK 會改讀 `OPENAI_API_KEY`／`OPENAI_BASE_URL`）。主迴圈 Anthropic client 與 Responses client 都是 `max_retries=0`。
   - D5：Responses 用 `responses.create(stream=True)` 的原始事件（`stream()` 輔助遇到 `response.incomplete` 會丟例外）；一律 `store=false`、`include=["reasoning.encrypted_content"]`；`incomplete` 的 `max_output_tokens`→截斷、`content_filter`→安全過濾。
   - D6：規則表每家族一個 `stream` 開關（Claude、相容 Chat、Gemini、Responses 都開）；要退回非串流是改規則表＋重建映像，不是執行期開關；每家族保留一組 `stream=False` 的主迴圈測試。
   - D7：`include_usage` 只對已知廠商送；自訂連線不串流、也不送。
4. **完整訊號與彙整（附註 7、14、26）**
   - 完整訊號：相容 Chat 是 `[DONE]`；Gemini 是任一 candidate 有 `finishReason`，或 `promptFeedback.blockReason` 有值（只有 blockReason 時停止原因是 safety）；Claude 是 `message_stop`；Responses 是 `response.completed`／`response.incomplete`。沒有完整訊號就斷線、200 之後的 error 物件或事件（Anthropic overloaded、`response.failed`、資料帶 `error`）、壞 JSON 或形狀不對，一律 RoundError，下游沒有半截文字。
   - 共用 SSE 解析器 `sse_stream.sse_data`：WHATWG 行規則（CRLF／LF／CR，跨 chunk 的 CR 先留著）、`:` 註解與 event／id／retry 略過、去 BOM、增量 UTF-8 解碼；最後一個事件沒有結尾空行也照送（完整與否另看完整訊號）。`ChatStreamAggregator` 把 chunk 折回非串流形狀，鍵只來自 chunk，工具呼叫依 index 合併（缺號、交錯、同 index 換 id 都處理），不接受 chunk 裡的 `message` 鍵。
   - Responses：`response.completed` 沒帶 output 時，依 output_index 從 `output_item.done` 補回。
   - 零差異證明：每個 golden 情境跑三種切法（1 個 chunk、語意多 chunk、固定種子的位元組切割，切點含 UTF-8 字元中間），輸出都和同一份 golden 完全相同。
5. **Gemini 自寫串流的彙整規則**（3b-U3 動工前定）
   - 請求與非串流完全相同，只把網址換成 `:streamGenerateContent?alt=sse`；金鑰照舊走 `x-goog-api-key` 標頭。每個 SSE 事件是一份完整的 GenerateContentResponse，彙整回非串流的形狀，鍵只來自 chunk。
   - 相鄰同類 text part 只在 chunk 交界合併：新 chunk 的第一個 part 接到已累積的最後一個 part，條件是兩者都只有 `text`／`thought`（新的那個可以另帶 `thoughtSignature`）、`thought` 旗標相同、前一個還沒帶簽章。同一個 chunk 裡的多個 part 照原樣保留。
   - `thoughtSignature` 跟著它所在的 part。新 chunk 的第一個 part 符合上一條時（包含只帶簽章的空 text part），簽章併進前一個 text part；不符合時，那個 part 連同簽章原樣成為獨立的 part。彙整後每個簽章只出現一次；哪些 part 回送沿用既有規則（`thought` part 不回送，ADR-0157 K3），回送的簽章只回送一次。
   - functionCall 等其他 part、SDK 不認得的欄位原樣保留並回送。candidate 的其他欄位取最後一個非 null 值；`usageMetadata` 每個 chunk 都帶，取最後一個；其他頂層欄位取第一次出現的值。
   - 完整訊號見第 4 點；沒有完整訊號就斷線＝RoundError，不回半輪。
6. **重試（附註 3、29、30；U5a）**
   - 重試在 react_core，不進 adapter 的 `send_round`：adapter 只在 RoundError 標 `retry_note`／`retry_after`（`handlers/adapters/retry.py`）。可重試：狀態 408／409／429 與 5xx；串流還沒收到 200 就斷的連線錯誤（串流的標頭在生成前就回，200 前斷線的計費風險低）；非串流路徑（自訂連線，與規則表關掉串流時的主迴圈）只重試連不上（`ConnectError`／`ConnectTimeout`／`PoolTimeout`，SDK 例外沿 `__cause__` 判斷）——讀逾時、讀到一半斷線表示請求已送出、廠商可能還在生成，重送最多付 3 倍，所以不重試（R-3b ② N1；代價是連線池重用到已被伺服器關掉的舊連線而 reset 時，非串流路徑也直接顯示錯誤、不重送——寧可偶爾多一次手動重試，也不冒多付錢的險）；200 之後才收到、型別或代碼等同 429／5xx 的錯誤本文（Anthropic `overloaded_error`／`api_error`、Gemini `UNAVAILABLE`、OpenAI `server_error` 等）；還沒有輸出時的首事件與閒置逾時。不重試：整輪上限的逾時、已經收到輸出之後的閒置逾時（R-3b ② L3）、400 等其他狀態、一般例外、串流開了之後的斷線、半截串流、壞資料（形狀錯或 JSON 壞掉一律轉 RoundError，用量照第 2 點記帳，R-3b ② L4）。有輸出之後兩種情況刻意分開處理：閒置逾時只是猜測（可能只是慢），重送可能讓已計費的輸出再付一次，所以不重試；收到忙碌錯誤是廠商明說的暫時性錯誤，最多送 3 次、被放棄那次的部分用量已照第 2 點記帳，所以照樣重試。
   - `react_core.MAX_SEND_ATTEMPTS = 3`（四家共用；人工 live probe 設 1＝retry=0）；退避 2 秒×次數，`retry-after-ms`／`retry-after`（秒數或 HTTP 日期）優先，Gemini 另讀錯誤本文的 RetryInfo，上限 30 秒（`MAX_RETRY_AFTER_S`）；等待期間照樣送心跳（ADR-0157 K1），等完先看使用者有沒有按停，按了就不送下一次（R-3b ② L1、L2）。每次重試先送一則 touka_status「<狀態>, retrying... (attempt N)」再等一次，Gemini 第一次仍是「Gemini busy (503), retrying... (attempt 1)」；失敗那次的 RoundError 不送下游；被放棄那次照第 2 點記帳；等待中被取消照常往外傳，不吞 `CancelledError`。
   - Gemini 主迴圈只送一次、adapter 內部不等待；`complete_once` 照舊只送一次（Gemini `max_attempts=1`、503 先發狀態、連線錯誤後先等再放棄）。
7. **finish_reason 與拒答提示（U5a）**：終端 chunk 的 finish_reason 照實回報：`max_tokens`→`length`，`refusal`／`safety`→`content_filter`，其他（含串流正常結束但缺欄位、未知值）維持 `stop`；Claude 原生 Anthropic 出口不動。拒答多一則「⚠️ 模型拒絕回答。」，模型自己給了拒答文字時寫成「⚠️ 模型拒絕回答：<文字>」（OpenAI `message.refusal`、Responses 的 refusal part）；安全過濾多一則「⚠️ 回應被廠商的安全過濾擋下。」。`stop_reason` 讀不到時照 `stop` 收尾並記日誌。刻意改 6 份 golden：`{claude,openai,gemini}__{truncated,refusal}`。
8. **KF 修正與規則表接線**
   - KF-7a（U3）：外部客戶端帶 role=tool 歷史時，Gemini 的 `functionResponse.name` 填對應 functionCall 的工具名，不再填 call id。
   - KF-8（U2，adapter 出口）：相容 Chat 不送 Touka 內部欄位（`incognito`、`deep_research`、`mcp_dispatcher`、`session_id`、`project_id`、`auth_level`、`ide_context`）、MCP 工具的 `server`／`tool`／`category`，工具全被過濾時不送 `tools`；`thinking`、`stop`、`max_tokens` 照送。
   - KF-9（U5b）：Anthropic 形狀入口（IDE 的 `/v1/messages`）接非 Claude 模型時，`convert_to_openai` 把 `tool_use`／`tool_result` 轉成 `tool_calls`／role=tool 訊息（call id＝tool_use id；`is_error` 的結果在內容前加「Tool error: 」），list system 合成字串（不送 `cache_control`）；Gemini 與 Responses 再由既有轉換器轉成 functionCall／functionResponse、function_call／function_call_output。
   - Gemini `generationConfig` 只送請求的 `maxOutputTokens`／`stopSequences`，從不送取樣參數；`complete_once` 不再自己補 Gemini 上限。
   - 規則表新讀者：相容 Chat（`stream`、`required_params_with_tools`＝gpt-6-sol／luna 帶工具送 `reasoning_effort='none'`、`forbidden_params`、`default_max_output`、`forced_tool_choice`）、Gemini（`stream`）、Responses（`stream`、`forced_tool_choice`）、`route_family`（`uses_responses_api`）。
9. **Responses 細節與 SDK 守衛（D4、D5，附註 8、19、24、37）**
   - base URL 是 `config.OPENAI_RESPONSES_BASE_URL`（`https://api.openai.com/v1`，字串常數，比照 `ANTHROPIC_API_BASE_URL`），不經會被同名環境變數覆寫的 `OPENAI_BASE_URL` 設定。
   - `OPENAI_SDK_ENV_OVERRIDES`（`OPENAI_ORG_ID`、`OPENAI_PROJECT_ID`、`OPENAI_ADMIN_KEY`、`OPENAI_CUSTOM_HEADERS`、`OPENAI_WEBHOOK_SECRET`、`OPENAI_LOG`）在 config 載入時與每次建構 openai client 前移除；`OPENAI_API_KEY`／`OPENAI_BASE_URL` 是 Touka 自己的設定名，保留（建構點顯式傳值，SDK 不讀）。openai 與 httpx2 的 logger 釘在 WARNING（`pin_sdk_loggers`）。
   - 禁令 22 的 AST 守衛擴到 openai 建構子（AsyncOpenAI／OpenAI／Azure 版與別名）：`api_key=`、`base_url=`、`http_client=` 都要在、不得是 None 字面量、`api_key`／`base_url` 不得讀環境變數；openai 的 `DefaultAsyncHttpxClient` 算 client，要明寫不跟隨轉址。
   - 請求：system 合成 `instructions`；工具攤平、沒寫 strict 就送 false；`max_tokens`→`max_output_tokens`；不送取樣參數與 stop。下一輪把 output items（含加密 reasoning、function_call）照原順序原樣回送，工具結果用 call_id 對應。`prompt_cache_key`＝`touka-`＋sha256(agent＼0session) 的前 32 碼，不送靈魂名或 session id 原文；這只是去識別、不是不可逆（沒有 session 時等於 sha256(靈魂名)，用名字字典就能還原），R-3b ① 判定不改 HMAC：OpenAI 本來就收到含人格的 instructions。
   - GPT 登入只留接縫（附註 24，本段不做登入）：context 可給 `token_provider`（每次請求取新權杖）、`extra_headers` 與另一個 `base_url`。
   - 傳輸層契約：真 SDK 加 httpx2.MockTransport，送出內容和 `tests/fixtures/responses_u4a_wire.json` 逐字比對。
10. **Claude 非串流上限**（附註 4，維護者 2026-10-12 核准）：21,333／8,192 兩個常數與 `nonstreaming_max_tokens` 欄位保留，只在主迴圈不串流送出時於送出當下壓（`claude_nonstream_cap`）；主迴圈串流與 `complete_once`（帶 timeout）不壓，轉換器也不再壓。

## 代價、已知限制與待 R-3b 判斷

- 只用 mock 與本機 loopback 驗證：真 API 的事件形狀、ping 間隔、廠商收不收 `include_usage`、Responses 回送鍵集合（含 `status` 這類只出現在輸出的欄位）都沒實測，列在 [live-check-backlog](../../proxy/live-check-backlog.md)。閒置 60 秒是推測值（L3）。
- Gemini 的廠商範例 fixture 是依文件形狀重建的（CRLF、最後一個 chunk 是空字串 text），不是原文，標為推測，由 L1 核對。
- 記帳邊界：HTTP 200 之前就失敗的不記也不估；已完成的一輪不再估，所以「最後結果已產生、核心還沒讀到就被取消」的極小窗口會漏記一輪。RoundError 路徑在估算時多送一筆 `token_usage`（`estimated: true`），取消路徑只寫日誌；兩者都不寫稽核 CSV。
- Ollama、LiteLLM 不在已知廠商清單，維持非串流。Responses 的終端 chunk 不帶 usage（格式和 Chat 不同，目前找不到下游讀者）；思考頻道只送 reasoning 摘要。
- `max_retries=0` 之後 SDK 不再自己重試：Claude 與 Responses 的連線錯誤、408／409、200 之後的忙碌錯誤改由主迴圈接手（R-3b ② M1）；Gemini 的連線錯誤也只在還沒收到 200 時重試（以前不分階段送 3 次）。只有 ping、沒有輸出的串流在整輪上限前才閒置逾時，仍會重試。重試狀態文字中英混合（例如「上游串流逾時：閒置太久, retrying... (attempt 1)」，R-3b ② L6 決定只記錄）；人工 probe 也把 ResponsesAdapter 限在 1 次請求，probe 的 Claude client 和主迴圈一樣掛位元組計時（L7）。
- KF-9：工具結果裡的圖片等非文字區塊略過、只記警告；不在工具區塊裡的 Anthropic image 區塊照舊原樣外送（不在 KF-9 範圍）。
- `subagent_tools.py` 的註解仍寫「Claude 路由不寫 ctx model」，U4b-2 後已不正確（產品碼註解，本段只改文件）；ADR-0245 剩餘風險那句已照實改寫。
- 交 R-3b 判斷：
  - ①（R-3b 已審，3b-R1 修正）：`custom_connection` 缺值改成當自訂連線、不串流（fail-closed）；`prompt_cache_key` 不改 HMAC（見決策 9）；不支援的內容區塊只記型別；禁令 22 守衛的 base_url 也禁 `OPENAI_BASE_URL`、openai client 的副本一律禁止。
  - ②（R-3b 已審，3b-R1 修正 M1、M2 與 L1～L5、L7）：3b-U5a 以 WIP commit 加 revert 再疊兩個 commit 的歷史（沒有改寫歷史）。
  - ③（R-3b 已審）：`responses_adapter` 讀規則表在核准範圍內；3b-R1 把 Gemini 的 generationConfig 斷言與 probe 的 503 狀態清單改成寫死值，移除 Gemini 沒有呼叫者的「送 `_MAX_RETRIES` 次」路徑（禁令 46）。
  - KF-9 的 `is_error` 前綴做法；Responses 終端 chunk 沒有 usage、思考頻道只送摘要。

## 驗證

- 各單位新增測試與突變（每條都確認變紅再還原）：U1 35、U2 40、U3 55、U4a 44、U4b-1 39、U4b-2 14、U5a 48、U5b 16，合計 291。新測試檔見 tests/README（`test_sse_stream`、`test_claude_stream_sdk`、`test_claude_stream_timeouts`、`test_partial_usage`、`test_openai_stream`、`test_compat_stream_usage`、`test_gemini_stream`、`test_openai_sdk_env_security`、`test_responses_request`、`test_responses_parse`、`test_responses_stream`、`test_complete_once_nonstream_contract`、`test_main_loop_retry`、`test_kf9_anthropic_entry`、`upstream/test_upstream_responses`）。
- golden：U1～U4、U5b 對分叉點零差異（`CI=1`）；U5a 剛好 6 份 M，經獨立審查代理核對（只改終端 finish_reason 與多一則提示，chunk 數、telemetry、`token_usage` 不變；刪掉重錄逐位元組相同）；`gemini__retry_503` 與三份 `*__upstream_error` 原樣保留。
- 錄製台 KF 翻轉：KF-8 6 個、KF-7a 1 個、KF-9 6 個，都照 docstring 改名並寫明原名。另一條期望翻轉補記：3b-U3 把 `gemini_adapter` 加進可讀規則表的模組清單（`test_model_rules.py` 的 `_RULES_IMPORTERS`，D6 的 Gemini `stream` 開關），當時的 commit 訊息漏列（R-3b ③）。
- 3b-R1（R-3b 三位的意見）：R1a 修 ② 的 M1、M2 與 L1～L5、L7，突變 43 個；R1b 修 ① L1～L6 與 ③ L1～L3、移除 Gemini 死碼、更新 `subagent_tools` 過時註解，突變 9 個；golden 零差異。
- 全量 pytest（3b-U5b，固定順序、fnm Node 24）：8177 passed／17 skipped／6 failed；6 failed 是已知環境問題（`test_local_compose_bindings`）。各單位 lint_ssot、#19、#28、#44、#45 通過。本段沒有打任何 API，也沒有啟動 Touka。

## 回復

不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。依相依倒序逐單位 revert：U5b → U5a-2（連同 6 份 golden）→ U5a-1 → U4b-2 → U4b-1 → U4a-2 → U4a-1 → 依賴 commit（pyproject 與 uv.lock 一起）→ U3 → U2 → U1。只想退回非串流：改規則表的 `stream` 開關並重建映像（D1 保留了非串流路徑）；只想停用 Responses：`resolve_model_route` 不回 `responses`，就回到 Chat。撤回時本 ADR 改「⛔ 已撤回」（或註明撤回哪一項），ADR-0242／0247／0156 的反向註記一併還原，CHANGELOG 加一列。
