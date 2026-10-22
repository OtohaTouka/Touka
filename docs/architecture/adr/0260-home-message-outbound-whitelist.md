# ADR-0260：家對話外送只送角色與內容，送出邊界逐則白名單

- 狀態：✅ 已接受（記憶系統第二批 h1，安全修補；維護者 2026-10-15 核准第二批計畫，同日決定外送識別資料屬安全修補、單獨小 PR 先修）
- 日期：2026-10-15
- 決策者：維護者
- 影響範圍：ToukaProxy（`services/soul_service.py`、`handlers/adapters/base.py`、`handlers/adapters/openai_adapter.py`、`handlers/adapters/claude_adapter.py`）；SECURITY.md 一列
- 相關：ADR-0242（KF-8 原始紀錄）、ADR-0248（KF-8 修正）、ADR-0257（歷史資料界線）、ADR-0249（插話標註）、ADR-0152（思考分頻）。這些 ADR 的決策都不變。

## 背景

- 家對話是 UI 對話樹原樣存的節點。前端的 `MessageNode` 帶 `id`、`parentId`、`childrenIds`、`createdAt`、`model`、`attachment`、`steps`（工具呼叫的參數與輸出）、`progress`、`thinking`、`interjections`、`interruptions`；後端寫入的節點另帶 `timestamp`、`name`（心跳寫 `System` 或靈魂 ID）。後端節點模型 `extra="allow"`，保存時存原 JSON，所以實際帶哪些鍵依當時存檔而定。
- 自 2026-04-17 初始版本起，`SoulService.inject_home_messages` 對 Anthropic 形狀只取 role／content，其他形狀把節點整個接在訊息最前面，排在 system 人格之前。
- ADR-0248 的 KF-8 修正在 OpenAI 相容 Chat adapter 出口濾掉 Touka 內部欄位與 MCP 工具鍵，但只看請求最外層。`model_dump` 會把 `list[OpenAIMessage]` 裡程式插入的 dict 原樣序列化，每則訊息的鍵沒有人濾。這是 KF-8 只濾最外層留下的漏網：OpenAI 相容廠商收到節點 ID、工具輸出、別家模型的思考、附件，而且家對話排在人格之前。
- ADR-0257 把家對話包進歷史資料界線，處理的是「舊內容會不會被當成指令」，沒有處理「外送哪些欄位」與「排在哪裡」。家對話排在人格之前時，歷史資料出現在 system 之前，界線想表達的「人格與規則優先」也被削弱。
- 受影響的路徑：請求是 OpenAI 形狀（`/v1/chat/completions` 入口；群聊與 SNS 在內部也是這個形狀）、送往走 OpenAI 相容 Chat Completions 的連線（Provider Registry 的 OpenAI 相容廠商、規則表沒標 Responses 的官方 OpenAI 模型、啟用中的自訂連線）。

## 決策

1. **第一道（來源）**：`inject_home_messages` 對所有請求形狀都只取 `role`／`content`；插話標註在 `get_home_messages` 已併進 content，照舊。OpenAI 形狀插在開頭連續的 system 訊息之後（`_after_leading_system`），沒有 system 就在最前。Anthropic 形狀的行為不變（system 在獨立欄位）。
2. **第二道（送出邊界）**：每則訊息的鍵白名單，寫成模組常數，共用 `adapters/base.only_keys`（回新 dict、不改輸入、不是 dict 的原樣返回；禁令 18）：
   - OpenAI 相容 Chat：`_MESSAGE_KEYS` 是 `role`、`content`、`tool_calls`、`tool_call_id`，也就是 `OpenAIMessage` 的欄位去掉 `name`，在 `prepare_request` 的 `model_dump` 之後套用。測試釘住「白名單＝schema 欄位減 `name`」，schema 加欄位時要一起決定是否外送。
   - Claude 原生入口：`_MESSAGE_KEYS` 等於 `AnthropicMessage` 的欄位（`role`、`content`）。原生入口 `model_dump` 後直送，原本只靠第一道。
   - Gemini、Responses、Claude 的 OpenAI 入口：轉換器本來就逐則重建訊息、只讀規格欄位，不動。
3. **`name` 也整個濾掉，沒有路徑需要**：Touka 自己的呼叫端組請求時都只用 role／content，不送 `name`（ToukaChat `lib/chat/buildApiMessage.ts`、`lib/connections.ts` 的 `ChatMessage`；ToukaIDE `ChatViewProvider.ts`、`inlineChat.ts`、`InlineCompletionProvider.ts`；SNS `sns_gateway.py`；子代理 `tools/subagent_tools.py`；群聊 `dispatchers/group.py`；心跳 `routers/heartbeat.py`）。心跳與 SNS 的 `name` 只寫進對話樹；ToukaDispatch 不組模型請求。唯一把 `name` 送出去的路，就是整個家對話節點。
4. 白名單只套在整備請求那一步；迴圈中途寫回的內容照原形送（raw 透傳不變式）：模型回覆照原形（例如 DeepSeek 的 `reasoning_content` 已在解析時拿掉），OpenAI 相容 adapter 自己寫回的 tool 訊息（`append_tool_results`、`build_denied_turn`）照原本帶工具名 `name`，本 ADR 不改。

## 行為改變

- 外部 OpenAI 相容呼叫者在訊息裡帶的 `name` 不再轉送給廠商（OpenAI 規格允許這個欄位，原本會原樣送出）。Touka 自己的呼叫端都不送 `name`（決策 3），不受影響。

## 各家出口核對（修正前）

| 出口 | 每則訊息怎麼來 | 受影響 |
|---|---|---|
| OpenAI 相容 Chat | `openai_adapter.prepare_request` 的 `model_dump` 原樣序列化；KF-8 只濾最外層 | 是 |
| OpenAI Responses | `responses_adapter._input_items` 逐則重建，只讀 role／content／tool_calls／tool_call_id；system 併成 instructions | 否 |
| Gemini | `openai_to_gemini.convert_openai_to_gemini` 逐則重建成 `{"role","parts"}`；system 抽成 system_instruction | 否 |
| Claude（OpenAI 入口） | `openai_to_claude.convert_openai_to_anthropic` 重建成 `AnthropicMessage(role, content)`；system 抽出 | 否 |
| Claude（原生入口） | `inject_home_messages` 的 Anthropic 分支本來就只取 role／content | 否 |
| Anthropic 形狀入口送 OpenAI 相容廠商 | `dispatchers/utils.convert_to_openai` 只取 role／content | 否 |
| 心跳 | `routers/heartbeat.py` 只用 role／content 建訊息 | 否 |

新測試在修正前的程式上跑，只有 OpenAI 相容 Chat 的案例紅，其餘出口的案例都綠。

## 後果

- OpenAI 相容廠商收到的家對話和 Claude、Gemini、Responses 一樣：只有角色與內容，人格在最前。
- 家對話裡心跳與 SNS 寫的 `name`（`System` 或靈魂 ID）不再外送；心跳自己的請求本來就不送。
- 已知：`model_dump` 遇到 list 裡的 dict 會發 Pydantic serializer 警告（原本就有，Anthropic 形狀也一樣），不影響外送內容。
- 已知：自 2026-04-17 起已經送出去的內容收不回來，廠商端怎麼留存依各家政策。
- 不變：openapi、NOQA_REGISTRY、Guard #45 基準、characterization golden。

## 決策落點表

| 決策 | 檔案 | 位置 | 測試 |
|---|---|---|---|
| 1 第一道 | `ToukaProxy/src/services/soul_service.py` | `inject_home_messages`、`_after_leading_system` | `test_home_message_outbound_security.py` |
| 2 共用白名單 | `ToukaProxy/src/handlers/adapters/base.py` | `only_keys` | 同上 |
| 2、3 OpenAI 相容 Chat（含濾掉 `name`） | `ToukaProxy/src/handlers/adapters/openai_adapter.py` | `_MESSAGE_KEYS`、`prepare_request` | 同上 |
| 2 Claude 原生入口 | `ToukaProxy/src/handlers/adapters/claude_adapter.py` | `_MESSAGE_KEYS`、`prepare_request` | 同上 |

## 驗證

本機 Windows、3.12 venv、fnm Node 24.21.0。session 讀取用 mock、靈魂資料用 stub，不讀 `data/`、不打付費 API，也沒有啟動 Touka。

- 新測試 `tests/unit/test_home_message_outbound_security.py`：28 個（10 個測試函式含參數化），先紅後綠。濾掉 `name` 的測試另外核對過：只換回舊白名單（含 `name`）時 3 個紅；再換回基準的家對話注入，家對話 `name` 的 OpenAI 相容 Chat 案例也紅。其他出口的 `name` 案例在基準上本來就綠，因為它們本來就不送 `name`。
- 突變 3 個（只拿掉第一道、只拿掉 OpenAI 白名單、只拿掉 Claude 白名單），各自轉紅。只拿掉第一道時，外送鍵的測試仍綠，證明第二道自己擋得住。
- 全量（merge dev `65eeccf8` 後的 `5b27421b`）：9378 passed／7 skipped／6 failed（6 個是本機 Docker Desktop 的 `test_local_compose_bindings`，以 CI 為準）；`guard_adr_integrity.py` 0001..0260 連續。

## 回復

- 第一道和第二道可以分開撤：撤第二道時，第一道仍只送角色與內容（但外部呼叫者帶的 `name` 會恢復轉送）；撤第一道時，第二道仍擋得住 UI 樹的鍵（含 `name`），但人格排在家對話之前只靠第一道。
- 不寫任何資料，撤回後沒有要清的東西；SECURITY.md 那一列要同步改。
