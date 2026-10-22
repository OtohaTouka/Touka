# ADR-0247：模型溝通層第 3a 段（Claude 官方 SDK 1.x、每模型規則表）

- 狀態：✅ 已接受（維護者 2026-10-12 核准模型層總計畫）
- 日期：2026-10-12
- 決策者：維護者
- 影響範圍：`services/models/model_rules.py`（新）、刪 `handlers/adapters/claude_model_rules.py`、`adapters/openai_to_claude.py`、`claude_adapter`、`gemini_adapter`、`services/completion.py`、`main.py`、`config.py`（`ANTHROPIC_API_BASE_URL`、SDK 環境變數清除）、`dispatchers/factory.py`、`providers/pricing.py`（新型號價格）、`services/models/fetcher.py`、`routers/models.py`（Claude 健康檢查）、另外 6 個檔共 8 處 httpx client 補明寫 `follow_redirects=False`（httpx 預設就不跟隨，行為不變）、`scripts/run_tool_eval_episode.py`、`tests/smoke/manual/live_chat_probe.py`、`ToukaProxy/pyproject.toml`／`uv.lock`、`SECURITY.md`、對應測試與文件；golden 零差異
- 取代 [ADR-0241](0241-model-layer-stage-0.md) 決策 3（Claude 思考與強制工具暫行規則併入每模型規則表）
- 修訂 [ADR-0242](0242-model-layer-stage-1a.md) 的 KF 表：KF-5、KF-7b、KF-10 與新對話第 2 輪沒有斷點，改為已修
- 2026-10-13：Mythos Preview 的中途 system 已核對，規則表改 False（由 [ADR-0249](0249-model-layer-stage-4a-4b.md) 修訂）
- 2026-10-16 由 [ADR-0271](0271-claude-5-5-thinking-by-purpose.md) 修訂（r55）：5.5 拆成 Opus／Sonnet／Haiku 三列（`effort_levels`、`thinking_off_mode`），5.6 以後照舊；default_on 的 8,192 下限在思考真的關掉（Haiku／Sonnet 5.5）時免除；原生入口改照實際值補下限、只轉送 `output_config.effort`、`disabled` 依型號改寫；OpenAI 入口的 `request.thinking` 仍不理會（剩餘風險那條不變），`stop` 改以 `stop_sequences` 送出；P24 的 Haiku 5.5 改兩檔計價、Sonnet 5.5 另列（快取命中 0.10）
- 決策 4 的移除條件（3b-U1 後拿掉非串流上限）已由 [ADR-0248](0248-model-layer-stage-3b.md) 修正為保留常數、只在主迴圈不串流送出時才壓（附註 4；2026-10-13）
- 範圍外的 `AnthropicThinking` 定義兩次，已由 [ADR-0250](0250-touka-leftovers.md) C6 修正（2026-10-14：只留一個定義，OpenAPI 的兩個元件合成一個）；剩餘風險中 ToukaDispatch 的 httpx client 沒明寫 `follow_redirects=False`、不在禁令 22 守衛範圍，同由 ADR-0250 B3 修正

## 背景

Claude 走官方 SDK 0.83。主迴圈、工具評測腳本與人工 probe 的 client 沿用 SDK 預設 http client，遇到轉址會把 `x-api-key` 帶過去；SDK 1.x 改用 httpx2，預設照樣跟隨轉址。沒拿到 `base_url` 時，SDK 會改連環境變數 `ANTHROPIC_BASE_URL`，金鑰也一起送過去（3b-U0 發現本機 shell 就帶著這個變數）。各模型的特例散在三處：第 0 段的 `claude_model_rules`、轉換器寫死的別名表與固定思考預算、Gemini 以關鍵字改寫模型名。1a 另釘住 KF-5、KF-7b、KF-10 與新對話第 2 輪沒有斷點。

## 決策

1. **SDK 政策**
   - anthropic `~=1.11.0`：2026-09-30 上架，是 7 天冷卻期內最新的 1.x（1.12 未滿 7 天）。不開 `exclude-newer-package` 例外，只跑 `uv lock --upgrade-package anthropic`：anthropic 0.83.0→1.11.0、移除 distro，httpx2 2.12.0 與 `mcp==2.0.0` 不變。之後升級照同一條：冷卻期內最新、不開例外，裝不起來就退一版。
   - 拿掉 2024 年的 prompt-caching beta 標頭；1.x 照樣送出 `cache_control`。
   - **建構點規則**（禁令 22 AST 守衛 `tests/unit/test_model_layer_redirects.py`，只能變嚴；範圍是整個 `ToukaProxy/src`＋`scripts`＋`tests/smoke/manual`）：每個 SDK 建構子（anthropic 匯出、名稱符合 `(Async)?Anthropic\w*` 的類別，含平台 client 與別名；dotted import、別名指派都追得到）都必須①當場注入不跟隨轉址的 client（`DefaultAsyncHttpxClient(follow_redirects=False)` 或同等的 httpx／httpx2 client）；②明寫 `base_url=`，值不得是 `None`，也不得讀環境變數。範圍內每個建構點都直接寫 `ANTHROPIC_API_BASE_URL`，這個名字只能從 `src.config` 匯入、檔內不得另外綁定；`config.py` 給它的值必須是字串字面量。`with_options`／`copy` 帶 `base_url`／`http_client` 時套同樣規則（`copy` 只限追得到 SDK 的接收者；`with_options` 不分接收者一律檢查，OpenAI client 的 `with_options` 怎麼分家族留給 3b）；任何呼叫、`**{...}` 或屬性指派的 `follow_redirects` 不是字面量 `False` 一律違規；不得事後指派 `.base_url`（SSOT 常數除外）；型別標註與 tuple 指派的別名也追得到。違規範例逐例比對是哪一條規則。
   - **SDK 讀的環境變數**（R-3a）：`config.py` 把 `.env` 灌進環境之後，pop 掉 `ANTHROPIC_SDK_ENV_OVERRIDES`（SDK 1.11 原始碼裡會改目的地、憑證、標頭或日誌的變數：`ANTHROPIC_BASE_URL`、`_AUTH_TOKEN`、`_CUSTOM_HEADERS`、`_LOG`、`_PROFILE`、`_CONFIG_DIR`、身分聯合 7 個、各平台 base URL 6 個）；不設空字串，因為 SDK 會把空字串當 base_url。`ANTHROPIC_API_KEY` 保留（Touka 的金鑰）。ToukaMemory 匯入時會以 `load_dotenv` 把 `.env` 再灌回環境，所以主迴圈、factory 換金鑰、`complete_once` 三個建構點（與計分卡腳本的副本）送出前再清一次；每個名字只記一次 info。平台 client、environment worker、webhook 才讀的變數（`FOUNDRY_*`、`GOOGLE_CLOUD_*`、`VERTEX_PROJECT_ID`、`AWS_API_KEY`、`WORK_*`、`SESSION_ID`、`WEBHOOK_SIGNING_KEY`）沒清：Touka 不建這些 client，`WEBHOOK_SIGNING_KEY` 也不影響送出的請求。`ANTHROPIC_LOG` 只在 `import anthropic` 時讀（`main.py` 先 import SDK），清除時一併把 `anthropic`／`httpx2` logger 的層級還原。
   - factory 換即時金鑰用 `with_options(api_key=…)`：SDK 的 copy 帶著原本的 http client 與 `base_url`，不會重讀環境變數。
   - `ANTHROPIC_API_BASE_URL` 刻意寫成 `config.py` 的固定常數，不做成 Settings 欄位，因為 Settings 欄位會被同名環境變數覆寫。模型發現與 Claude 健康檢查讀同一個常數。
2. **每模型規則表**（`services/models/model_rules.py`）
   - 欄位：`forbidden_params`、`forced_tool_choice`、`thinking`（budget／adaptive／default_on／不調整）、`required_params_with_tools`、`uses_responses_api`、`default_max_output`、`nonstreaming_max_tokens`、`cache_min_tokens`、`mid_conversation_system`。context window 不在表內，沿用 `context_window_registry`（含使用者覆蓋），避免兩個來源。
   - 家族比對：名稱含 `claude`／`gemini` 就歸該家族，判斷和 `route_family.native_family` 相同。Claude 依家族＋版本樣式由上而下取第一列，新舊兩種命名都認得，8 位日期不當版本；Fable、Mythos 比照 5.x。認不出版本的 Claude 走保守預設：不送 `budget_tokens`、不強制工具、快取門檻未知。OpenAI 家族的 GPT-6 兩列只填欄位，3b 依「走 Responses」與「帶工具時必送參數」兩欄接上。
   - **永遠不拿來過濾模型發現**（禁令 39）：AST 匯入圖測試只准 `openai_to_claude`、`claude_adapter` 匯入本表；模型發現與管理模組（`services/models/*`、`routers/models`、`model_manager`）不得匯入本表或這兩個模組，也不得引用本表的符號；從讀本表的模組匯入它帶出的符號（含改名、多跳）也算讀本表。
   - 使用者：`claude_adapter`（思考參數正規化、可否強制 `tool_choice`）與 `openai_to_claude`。轉換器的模型名只轉小寫、去掉 Touka 的 `-thinking` 尾碼，寫死的別名表（如 `claude-4-5`→`claude-sonnet-4-5-20250929`）刪除。`-thinking` 時，只收預算的型號送 `enabled`＋schema 預設預算，其他送 `adaptive`。`max_tokens` 取請求值，沒給就用規則表預設：4.5 以後 16,000，4.1 以前與認不出的 8,192；只收預算的思考至少是預算＋1,024；default_on 思考的型號（5.x、Fable、Mythos、Haiku 5.5）至少 8,192（R-3a：思考關不掉，例如 IDE 行內補全送 128 會被思考吃光）。
   - `complete_once` 的 `_SHAPERS`（ADR-0244 剩餘風險）：Claude 的輸出上限已收回轉換器（照請求值）；Claude 清使用者訊息斷點與 `output_config` 仍在 `_SHAPERS`；Gemini 的 `maxOutputTokens` 留著，因為主迴圈不送上限，規則表沒有可以取代的地方。
3. **Gemini 模型名不再改寫**（KF-7b，決定 A13）：只去掉 `models/` 前綴並轉小寫，不再以關鍵字改成別的模型。轉小寫不改變是哪個型號，屬正規化，不是 P21 禁止的改寫（`Gemini-2.5-Pro`→`gemini-2.5-pro`）。空名、只有家族名（舊簡稱，D16）或含 URL 路徑字元時不送出，回 `MODEL_NOT_FOUND` 與看得懂的訊息。
4. **非串流上限與 3b 移除條件**：`CLAUDE_NONSTREAMING_MAX_TOKENS = 21_333`。SDK 的非串流檢查在 client 用預設逾時時，以 3,600 秒 × max_tokens ÷ 128,000 估時，超過 600 秒就丟例外（1.11 與 0.83 相同）；Opus 4／4.1 另為 8,192（`CLAUDE_OPUS_4_NONSTREAMING_MAX_TOKENS`，SDK 的 `MODEL_NONSTREAMING_TOKENS`；1.11 表內 Opus 4.1 只剩 Bedrock／Vertex 版本，本表對第一方 4.1 偏保守）。轉換器送出前壓低並記 info，不報錯。**移除條件**：3b-U1 的 Claude 上游串流（`messages.stream()`）合入後，拿掉壓低、兩個常數與 `nonstreaming_max_tokens` 欄位，`test_model_rules.py` 直接核對 SDK 界線的測試一起改。（3b 改了這條：常數保留，見 ADR-0248 決策 10。）
5. **Claude 快取正確性**：
   - KF-5：UI 入口把 system 以字串交給 adapter，和原生字串入口一樣做穩定前綴重排與兩個斷點切分（ADR-0129／0130）。
   - KF-10：多則 system 依序以換行合併（與 Gemini 轉換器相同），不再只留最後一則。
   - 第 2 輪斷點：新對話第 1 個工具輪後也標最新的 tool_result；標之前清掉所有先前 user 訊息（含 tool_result 內層）的訊息斷點，Touka 自己加的斷點每輪最多 4 個。
   - 1a 的四個釘子照 docstring 翻轉，ADR-0242 KF 表同步。
6. **`complete_once` 在 1.11**：1b 的契約測試 35 個原封不動通過。另用真的 SDK 加 httpx2 MockTransport 確認 `output_config.format` 照送，`end_turn`／`refusal`／`model_context_window_exceeded` 的正規化與四欄用量不變。`completion.py` 只加 `base_url`，不需要其他配合 1.x 的調整。1.11 的 `Message` 比 0.83 多 `diagnostics`、`stop_details`、`usage.output_tokens_details`，廠商沒回時為 null；原生出口照樣輸出、四欄用量不變，相容（真 SDK 物件測試）。
7. **新型號價格**（P24，R-3a 補）：`pricing.py` 加 Opus 4.8／5／5.5、Sonnet 5／5.5、Haiku 5.5、Fable／Mythos 5 與 5.1，來源 claude-api skill 的 models.md、model-migration.md（2026-10-06）；快取讀寫廠商有寫就照寫，否則讀 0.1×、寫 1.25× input（1b 規則）。Opus 4.6／4.7 取自 skill 主文 Current Models 表（$5／$25）。Haiku 5.5 分兩檔計價，取 >100K 的較高檔（$0.50／$2.50）。查不到價格的型號套同家族已知最高價（`claude-fable`／`claude-mythos` 家族鍵 $10／$50），寧高勿低。

## 代價與剩餘風險

- Claude 目的地寫死在常數。要走自架 gateway 或代理得改程式；以前可經 `ANTHROPIC_BASE_URL` 改，現在刻意不行。
- 清除前 `ANTHROPIC_CUSTOM_HEADERS` 能換掉 `x-api-key`、加任意標頭（建構與 `with_options` 副本都會讀）；現在 config 載入時與三個建構點都會清掉。殘留：Touka 子行程（終端機工具等）也不再繼承這批變數；工具評測腳本與 live probe 只靠 config 載入時那次；`main.py` 先 import SDK，`ANTHROPIC_LOG` 若已觸發 SDK 的 `basicConfig`，根 logger 的 handler 留著（層級已還原，請求內容不會寫進日誌，但 Touka 自己的 `setup_logging` 可能不生效）。`api_key` 為 `None` 時 SDK 仍會讀 `ANTHROPIC_API_KEY`（主迴圈傳空字串、`complete_once` 先擋空金鑰，都不會走到這裡）。
- 3b 之前，請求的 `max_tokens` 超過非串流上限會被壓低，長輸出可能被截斷。
- default_on 思考型號的 8,192 下限未實測（列入下一段實測）：會讓小額度請求（如 IDE 行內補全 128）多付輸出費。原生 Anthropic 入口不經轉換器，`max_tokens` 照請求值（schema 預設 8,192），和 OpenAI 形狀入口的規則表預設（16,000）是兩個來源，本段不統一。
- 價格待補：Opus 4.5 在參考資料裡查不到，照舊落到 `claude-opus-4`（$15／$75，偏高）；Mythos Preview 落到家族鍵 $10／$50。點號寫法（如 `claude-opus-4.8`）只比對到舊鍵。Mythos Preview 的快取門檻已改 2,048，其他欄位仍照 5.x（廠商文件說它收 `budget_tokens`）；中途 system 清單沒列它，4a 之後已核對，規則表改 False（見 ADR-0249）。
- `ToukaDispatch/server.py` 約 276 行的 `httpx.AsyncClient()` 沒明寫 `follow_redirects=False`（httpx 預設不跟隨，行為不受影響），不在禁令 22 守衛範圍（2026-10-14 已由 ADR-0250 B3 修正：明寫旗標，守衛擴到 Dispatch、Heartbeat、Memory）。守衛仍抓不到 `functools.partial` 與子類別；禁令 39 的遞移只追 `from … import`，不追「匯入整個模組再取屬性」跨檔的情況（發現路徑本身的屬性引用仍會抓）。
- 1.11 送出已棄用型號（如 `claude-sonnet-4-5`）時會發 `DeprecationWarning`，只出現在 log 與測試警告。
- 範圍外發現（記下、未修）：
  - `context_window_registry` 內建表過時：4.6 以後、5.x 與 Haiku 5.5 應為 1M。
  - `adapters/schemas.py` 的 `AnthropicThinking` 定義了兩次。（2026-10-14 已由 ADR-0250 C6 修正）
  - Claude 的 OpenAI 形狀入口不理會 `request.thinking`，只看 `-thinking` 尾碼。
  - 呼叫端第 1 輪就送超過 4 個斷點時，Claude 仍會回 400；Touka 只保證自己加的不超過。
- 只用 mock 與本機 loopback 驗證；廠商實際回應留給下一段實測（禁令 61）。

## 驗證

- 規則表（3a-U1）：`test_model_rules.py` 103 個，取代 `test_claude_model_rules.py`；上游契約 Claude／Gemini 改期望，KF-7b 釘子翻轉為 20 筆原樣送出；21 個突變都紅。
- SDK 1.x 與不跟隨轉址（3a-U2）：`test_anthropic_sdk_transport_security.py` 3 個（307 不跟隨、金鑰不送到轉址目標、`cache_control` 照送）、`test_network_guard.py` 加 httpx2 傳輸被封鎖；19 個突變都紅。
- 快取正確性（3a-U3）：KF-5、KF-10、第 2 輪三個釘子翻轉，新增多輪多 system 與呼叫端斷點的上限測試；9 個突變都紅。
- 目的地固定與 `complete_once`（3a-U4）：`test_anthropic_base_url_pinned.py` 6 個（對照組證明環境變數真的生效；主迴圈、factory 換金鑰後、`complete_once` 仍送到 Touka 設定的目的地）；禁令 22 守衛加 `base_url` 規則與違規範例（39→56 個）；15 個突變都紅。
- 對抗式複審修正（R-3a）：新增 `test_anthropic_sdk_env_security.py` 7 個（環境變數清除、對照組、主迴圈／換金鑰副本／`complete_once` 只送 Touka 的金鑰與標頭、`ANTHROPIC_LOG` 不寫請求內容）；禁令 22 守衛 56→111 個；`test_model_rules.py` 103→142 個（default_on 下限、Mythos Preview、禁令 39 匯入圖含兩跳、家族判斷與 `native_family` 一致）；`test_pricing.py` 加 19 個、`test_budget_cap.py` 加 3 個（新價格、家族最高價與成本上限閘）；環境變數 8 個；真 SDK `Message` 2 個；Gemini 轉小寫 1 個；53 個突變都紅。全量 pytest 另跑一次帶不可信 `ANTHROPIC_BASE_URL` 的固定順序，結果與不帶時相同。
- golden 零差異（`tests/proxy/characterization/goldens` 對本段起點沒有任何變動）。全量 pytest 與守衛結果見 HANDOVER。本段沒有打任何 API，也沒有啟動 Touka。

## 回復

不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。依相依倒序逐單位 revert：3a-U4（`base_url` 與守衛規則）可單獨撤回；3a-U3（快取正確性）要連同 ADR-0242 KF-5／KF-10 列與釘子一起撤；3a-U2（SDK 1.11）連同 `uv.lock` 撤，撤之前先撤 3a-U4；3a-U1（規則表）最後撤，要還原 `claude_model_rules.py` 與 ADR-0241 決策 3 的反向註記。本 ADR 改「⛔ 已撤回」（或註明撤回哪一項），CHANGELOG 加一列。
