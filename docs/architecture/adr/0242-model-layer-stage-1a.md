# ADR-0242：模型溝通層第 1a 段（凍結現況的快照）

- 狀態：✅ 已接受（維護者 2026-10-11 核准：本段只釘不修；Gemini 顯式快取之後全停）
- 日期：2026-10-11
- 決策者：維護者
- 影響範圍：`tests/proxy/upstream/`（新）、`tests/proxy/characterization/`（9 份新 golden、CI 缺檔改失敗）、`tests/unit/` 三個釘子檔、文件；產品程式 0 行。延續 [ADR-0241](0241-model-layer-stage-0.md)
- 決策 2 中「Gemini 快取加 `tool_config` 會不會被拒」的實測，已因 [ADR-0243](0243-model-pre-run.md) 全停 Gemini 顯式快取而失效（2026-10-12）
- KF 表的 KF-5、KF-7b、KF-10 與新對話第 2 輪沒有斷點，已由 [ADR-0247](0247-model-layer-stage-3a.md) 修正（2026-10-12；釘子照 docstring 翻轉）
- KF 表的 KF-7a、KF-8、KF-9 與兩條既知（finish_reason 一律 stop、Gemini 不送輸出上限），已由 [ADR-0248](0248-model-layer-stage-3b.md) 修正（2026-10-13；釘子照 docstring 翻轉）

## 背景

3a（Claude）與 3b（OpenAI 相容／Gemini）會改寫模型層。動手前先把「每一輪送出去的內容」與已知故障凍結成測試，之後每段改了什麼都看得見，修正哪個故障也一定會碰到對應的測試。

## 決策

1. **上游契約的斷言放在交給傳輸層之前**：`tests/proxy/upstream/harness.py` 的 `RecordingAdapter` 包住真的 adapter、只換 `send_round`，在送出當下把請求深拷貝成快照（`state.kwargs`／`state.payload` 會跨輪原地修改），快照正規化以 adapter 類別為鍵；不 patch httpx。L 測試證明替換後下游 SSE 逐字相同。期望值一律寫死字面值。3a／3b 沿用這個位置，不論 3b 選哪個傳輸。
2. **本段不打任何 API**。Claude／Gemini 帶工具迴圈的 live 基線、Gemini 快取加 `tool_config` 會不會被拒，留到 3a／3b 改動前後同一天各測一次（禁令 61）。
3. **CI 缺 golden 改成失敗**：characterization `assert_golden` 在 `CI` 小寫後屬於 {1, true, yes} 且缺檔時 `pytest.fail`；本機照舊錄製並 skip。為此加 1 個 `ENV_SSOT_VIOLATION`（NOQA_REGISTRY 36→37）。
4. **本段只記錄故障、不修**：每個故障一個 `test_kfN_…` 釘子，docstring 寫「修正時改這個測試」。清單正本是下表。1b 要改走 `complete_once` 的三個旁路呼叫（摘要、互審 critic、創建評分）也先凍結（`tests/unit/test_side_caller_requests.py`）。

## KF 清單

| ID | 內容 | 釘住的測試 | 處理階段 |
|---|---|---|---|
| KF-1 | Gemini 迴圈重建快取丟掉較早內容：上一個成功的快取（初次或迴圈中）若涵蓋過任何對話內容，下一次迴圈重建就會把那些內容丟掉 | 未釘；守門：`test_gemini_no_explicit_cache.py` | 已由 ADR-0243 全停消除 |
| KF-2 | Gemini 帶 `cachedContent` 時仍設 `tool_config`（矯正輪、收尾輪、矯正後工具輪） | 未釘；守門：`test_gemini_no_explicit_cache.py` | 已由 ADR-0243 全停消除 |
| KF-2d | 初次快取沒建成、迴圈中建成後，`cachedContent` 與 `system_instruction`／`tools` 同送 | 未釘；守門：`test_gemini_no_explicit_cache.py` | 已由 ADR-0243 全停消除 |
| D-3 | Claude 開思考時送 temperature 0.7 會 400 | 已由第 0 段解除；守門：`test_upstream_claude.py::test_d3_thinking_requests_send_no_temperature` | 不處理 |
| KF-4 | 心跳請求沒有任何工具 schema | `test_heartbeat_tool_schema_pinned.py`：原 `test_kf4_heartbeat_request_has_no_tools` 已改為新期望並改名（`test_kf4_heartbeat_request_carries_tools`） | 已修（ADR-0246） |
| KF-5 | UI 入口的 Claude system 被包成單一 list 區塊，穩定前綴切分不作用，摘要一變整塊就不同（原生字串入口正常） | `test_prefix_stability.py`：原 `test_kf5_claude_ui_entry_system_is_one_block` 已改為新期望並改名 `test_kf5_claude_ui_entry_keeps_stable_prefix_across_turns` | 已修（ADR-0247） |
| KF-6 | Claude 健康檢查沒帶 `anthropic-version`；附帶：OpenAI 健康檢查送 `max_tokens` | `test_model_health_headers_pinned.py`：原 `test_kf6_*`（2）已改為新期望並改名 | 已由 ADR-0243 修正 |
| KF-7a | Gemini 轉換器把外部 `role=tool` 訊息的 `functionResponse.name` 填成 call id | `test_upstream_gemini.py`：原 `test_kf7a_external_tool_history_uses_call_id_as_function_name` 已改為新期望並改名 `test_kf7a_external_tool_history_uses_tool_name` | 已修（ADR-0248） |
| KF-7b | Gemini 模型 ID 改寫（如 `gemini-2.5-flash-lite`→`gemini-2.5-flash`、`gemini-3.1-flash`→`gemini-3.1-flash-lite`） | `test_upstream_gemini.py::test_kf7b_model_id_rewrite_table`：原 18 筆已改為新期望（20 筆，模型名原樣送出） | 已修（ADR-0247） |
| KF-8 | OpenAI 相容端外送 Touka 內部東西：a 欄位（`incognito`／`deep_research`／`mcp_dispatcher` 一律送，`session_id` 等有值就送）、b MCP 工具的 `server`／`tool`／`category`、c 工具全被過濾時送 `tools: []` | `test_upstream_openai.py`：`test_kf8a_*`（4）改名 `…_not_sent`、原 `test_kf8_tools_are_sent_verbatim`（2）改名 `test_kf8_tools_are_cleaned` | 已修（ADR-0248） |
| KF-9 | Anthropic 形狀入口接非 Claude 模型：自訂連線、Gemini、一般相容三個入口都先經 `convert_to_openai`，只搬 content。OpenAI 相容端：`tool_use`／`tool_result` 區塊原樣外送；list system 先被 `json.dumps`，又被 `normalize_message_content` 解回 Anthropic text 區塊，連 `cache_control` 一起外送；只有降級時（尾巴接上警告）才以 JSON 字串原樣外送，中文被跳脫。Gemini 端：`tool_use`／`tool_result` 被略過、工具紀錄消失（tool_result 那則只剩空白 part），`system_instruction` 整個消失 | `test_upstream_openai.py`：`test_kf9_*`（5）改名 `…_sends_tool_calls_to_openai_compatible_vendor`（2）、`…_hands_over_the_tool_history`（3）；`test_upstream_gemini.py`：原 `test_kf9_anthropic_entry_loses_tool_blocks_on_gemini` 改名 `…_keeps_tool_blocks_on_gemini` | 已修（ADR-0248） |
| KF-10 | Claude UI 入口帶多則 system 時只留最後一則 | `test_upstream_claude.py`：原 `test_kf10_claude_keeps_only_last_system_message` 已改為新期望並改名 `test_kf10_claude_merges_system_messages_in_order` | 已修（ADR-0247） |
| 既知 | 截斷與拒答時 `finish_reason` 一律是 `stop`；拒答時使用者看到空回覆 | golden `{claude,openai,gemini}__{truncated,refusal}`（6 份，3b-U5a 刻意重錄） | 已修（ADR-0248） |
| 既知 | Gemini 不送 `max_tokens`／`stop`（沒有 `generationConfig`） | `test_upstream_gemini.py::test_converter_merges_strips_schema_and_drops_sampling`（3b-U3 起 `generationConfig` 只送 `maxOutputTokens`／`stopSequences`） | 已修（ADR-0248） |

非 KF 的新現況：Claude 新對話（只有一則 user）第 1 個工具輪後，第 2 輪沒有任何訊息斷點（`append_tool_results` 的訊息數＞2 條件），只影響費用；交 3a，已修（ADR-0247）：釘子 `test_first_tool_round_of_new_conversation_leaves_no_message_breakpoint` 已改為新期望並改名 `test_first_tool_round_of_new_conversation_marks_tool_result`。

## 限制

- Gemini 503 重試與 OpenAI 非 200 處理在 `send_round` 裡，錄製台碰不到，由下游 golden（`gemini__retry_503`、`*__upstream_error`）涵蓋；OpenAI 401 提示另以直接呼叫 `_post_once`（假 client）驗。
- 互審只凍結到 completer 層（model、max_tokens、訊息形狀、system 與 user 原樣轉交），審查提示的文字沒有寫成字面值。
- mock 證明不了廠商會不會拒收：KF-2、KF-8、KF-9 的「可能 400」仍是推論。（KF-2 的「可能 400」已因 ADR-0243 全停而失效。）
- 下方盤點是文字比對，註解提到 `send_round` 等也會算進去。

## 附錄：3b 數字

U0＝`630185f3`；最終＝`9731f1f2` 加 U7 文件（U7 不改測試的比對結果）。測試數用 `pytest --collect-only -q`。

| 項目 | U0 | 最終 |
|---|---|---|
| (a) 直接 patch httpx 的測試檔 | 35 | 36（多 `test_model_health_headers_pinned.py`，屬 ④） |
| ① OpenAI 相容主線（檔／用到 httpx 的測試） | 8／23＋1 smoke | 8／26＋1 smoke（golden 多 3 個情境） |
| ② Gemini 主線 | 10／30 | 10／33（golden 多 3 個情境） |
| ③ Gemini 快取服務 | 2／11 | 2／11 |
| ④ 模型周邊／⑤ 無關 | 8／12 | 9／12 |
| S1 假 Anthropic SDK client（檔／整檔測試數） | 27／245 | 29／259 |
| S4 adapter 接縫 | 7／52 | 11／90 |
| S5 handler／dispatcher 樁（含 S4） | 25／145 | 31／227 |
| S1–S5 聯集 | 42／376 | 49／463 |
| 回應建構函式（regex 起算） | 14 個、7 檔 | 14 個、7 檔（另：錄製台共用 6 個，不符該 regex） |

- 驗證：1a 新增的主線測試對 (a) 貢獻 0 檔；唯一新增的 (a) 檔是 U6 健康檢查（④，路由直接用 httpx）。
- ①：`test_golden_openai`、`test_reasoning_channel`、`smoke_autonomy_enforcement`，以及混合檔 `test_handler_integration`、`test_handlers_sovereignty`、`test_pure_gen_ms_semantics`、`test_failure_visibility`、`test_no_sampling_on_wire`。
- ②：`test_golden_gemini`、`test_gemini_recache_preserves_inputs`、`test_gemini_incognito_no_cache`、`test_drift_c5_small_items`、`test_live_chat_probe_gemini`（實測程式的測試，但 patch 的是 `GeminiAdapter.send_round` 底下的 httpx），以及上列 5 個混合檔。③：`test_gemini_cache_service`、`test_gemini_key_header_security`（另含 ④）。
- 與規劃初步值不同：`test_progress_emit` 唯一的 httpx patch 在研究搜尋，歸 ④，所以 ①②③ 是 15 檔不是 16。

**(c) OpenAI 相容的特例**（特例｜位置｜廠商｜覆蓋的測試｜數量）

| 特例 | 位置 | 廠商 | 覆蓋的測試 | 數量 |
|---|---|---|---|---|
| `reasoning_content` 移除且不回送 | `openai_adapter.py:143` | DeepSeek、Grok | `test_adapter_contract::test_parse_reasoning_field_popped_from_history`、`test_reasoning_channel` 的 openai 3 個、`test_upstream_openai::test_tool_round_writes_back_tool_messages_without_reasoning`、golden `openai__reasoning_split` | 6 |
| `<think>` 前綴剝離 | `utils/reasoning.py` | 自架、DeepSeek 類 | `test_split_reasoning`（9）、`test_adapter_contract::test_parse_embedded_think_stripped_and_written_back`、`test_reasoning_channel::test_openai_embedded_think_stripped` | 11 |
| safety 參數只留給 Grok | `openai_adapter.py:72-75` | Grok | `test_adapter_contract::test_prepare_filters_safety_params_for_non_grok` | 1 |
| 誠實矯正 `"required"`→工具輪後 `"auto"` | `openai_adapter.py:259` | 全部 | `test_adapter_contract::test_openai_honesty_hook_*`（2）、`test_upstream_openai::test_honesty_correction_requires_then_releases_tool_choice` | 3 |
| OpenAI 缺金鑰 401 提示 | `openai_adapter.py:119-124` | OpenAI | `test_upstream_openai::test_post_once_401_hint_only_for_openai_without_key`（1a 前 0） | 3 |
| tool 訊息 content 保持字串 | `openai_adapter.py:58-64` | 全部 | `test_adapter_contract::test_prepare_keeps_tool_role_content_as_string`、`test_upstream_openai` 工具輪 | 2 |
| 多模態 JSON 還原 | `content_normalize.py` | 全部 | `test_content_normalize`（9） | 9 |
| 內部欄位外送（KF-8a） | `openai_adapter.py:58`＋`schemas.py:84-92` | 全部 | `test_upstream_openai::test_kf8a_*`（1a 前 0） | 4 |
| MCP 非標準鍵、空工具陣列（KF-8b／c） | `protocols/manager.py:2006-2012`、`layers/tool.py:91-94` | 全部 | `test_upstream_openai::test_kf8_tools_are_sent_verbatim`（1a 前 0） | 2 |
| Anthropic 形狀入口（KF-9） | `factory.py`→`dispatchers/utils.py:29-47` | 自訂連線、相容廠商 | `test_upstream_openai::test_kf9_*`（1a 前 0） | 5 |
| `max_tokens` 原樣送、沒有 `max_completion_tokens` | `openai_adapter.py:58` | 全部 | `test_upstream_openai::test_kf8a_set_internal_fields_are_sent`（1a 前 0） | 1 |
| Ollama 固定金鑰 | `registry.py` `literal_key` | Ollama | `test_provider_registry::test_get_provider_info_ollama_literal_key` | 1 |
| chat 端點正規化 | `registry.py` `chat_completions_url` | 全部 | `test_provider_registry::test_normalize_chat_url_*`（5）、grok／deepseek 完整端點（2） | 7 |
| 金鑰標頭與端點 URL | `openai_adapter.py:92-99` | 全部（含自訂連線） | `test_upstream_openai::test_headers_and_url`（3）；路由本身見 `test_model_route_isolation` | 3 |
| 取樣參數不送 | `adapters/sampling.py` | 全部 | `test_no_sampling_on_wire` 的 openai 6 組 | 6 |

**(d) 依 3b 候選換算**（只列檔數與測試數，行數是 3b 的事）：OpenAI 相容與 Gemini 都改用 SDK＝(a)①②③全部要搬，15 檔、70 個測試＋1 支 smoke；折衷（只有 Gemini 改用 SDK）＝(a)②③，12 檔、44 個測試，另新寫 Responses 測試；兩者都自寫＝0，但串流改造時三家的上游 mock 都要重做。S1 與三個候選無關（Claude 走 3a）。

**重跑命令**（在 3.12 venv 的工作資料夾，只讀文字）：
```
rg -l --glob '*.py' -e '["'"'"']httpx\.AsyncClient' -e '\.httpx\.AsyncClient' -e 'setattr\(httpx' -e 'setattr\([A-Za-z_]+\.httpx' -e 'patch\.object\([A-Za-z_]*httpx' tests
rg -l --glob '*.py' -e 'anthropic_client' -e 'messages\.create' -e 'AsyncAnthropic' tests   # S1
rg -l --glob '*.py' -e 'send_round' -e 'RecordingAdapter' tests                              # S4
rg -l --glob '*.py' -e 'patch\([^)]*src\.handlers' -e 'setattr\([^)]*src\.handlers' -e 'handle_(claude|openai|gemini)_request' -e 'run_react_loop' -e 'send_round' tests   # S5
rg -n 'def _(tool|text)_resp|def _resp\b|def _block\b' tests
python -m pytest --collect-only -q <檔案>
```

## 驗證

- 新測試 109 個（上游 83、characterization 18、unit 8）全綠；每個新檔與每個 KF 釘子都做過「改壞產品程式→變紅→還原」與「套用修正→釘子變紅」，紀錄在各單位回報。
- `git diff --stat 630185f3..<候選> -- ToukaProxy ToukaMemory ToukaHeartbeat ToukaDispatch ToukaChat ToukaIDE config` 為空。
- golden 31→40 份：只多 9 個 `A`、沒有 `M`／`D`；characterization 0 skip；全量 skip 數與 U0 相同（17）。
- 全量 pytest 與守衛結果記在當時 HANDOVER 的第 2 節，公開 repo 第一個 commit `4479c47f` 的 HANDOVER.md 還查得到；只剩已知 12 個環境問題（compose 外掛 6、Node 26 companion 6）。

## 回復

不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。revert 本段新增的測試、golden、characterization harness 的 CI 判斷（連同 NOQA_REGISTRY 的 1 個 `ENV_SSOT_VIOLATION`）與文件；本 ADR 改「⛔ 已撤回」，CHANGELOG 加一列。產品行為不受影響。
