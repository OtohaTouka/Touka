# ADR-0323：群聊沒有工具、心跳先檢查再寫、家知道去過哪、工具結果有上限（可視性第五批）

- 狀態：🚧 實作中（PR 1 群聊，2026-11-19；PR 2 心跳，2026-11-19；PR 3 家與子代理、PR 4 工具資訊與網頁以外的路待做）
- 日期：2026-11-19
- 決策者：維護者（一頁計畫第 2.1 版，總覽板第 90 項回「甲：照第 2.1 版做」；工具結果上限 4 萬字、子代理結果檔可偽造移出這批，是計畫裡寫明的「我自己定的兩件」）
- 相關：[ADR-0322](0322-who-said-it-and-which-conversation.md)（第四批：群聊成員發言、對話種類）、[ADR-0269](0269-advertised-tool-list-enforcement.md)（這一輪的工具清單）、[ADR-0276](0276-heartbeat-session-scope.md)（SNS 外人不能驅動有工具的靈魂）、[ADR-0316](0316-keep-harness-blocks-and-state-approval-mode.md)（區塊分組）、[ADR-0267](0267-trusted-activity-timeline-and-continuity-v1.md)（`<continuity>`）
- 修訂：[ADR-0269](0269-advertised-tool-list-enforcement.md)（決定 7 群聊沿用的清單是空的，PR 1）、[ADR-0322](0322-who-said-it-and-which-conversation.md)（已知限制：群聊成員的區塊與 `<continuity>`、API 群聊成員失敗句，PR 1；決定 7 心跳不再切段，PR 2）、[ADR-0318](0318-stop-timeout-failure-made-clear.md)（決定 9 入口被擋的原因與檢查時機，PR 2）、[ADR-0267](0267-trusted-activity-timeline-and-continuity-v1.md)（不是靈魂的心跳也有精簡 `<continuity>`，PR 2）
- 影響範圍：PR 2：`ToukaProxy/src/routers/heartbeat.py`、`services/continuity_service.py`、`services/continuity_events.py`（`HeartbeatSkip`）、`services/dispatchers/factory.py`、`utils/user_intent.py`、`config/errors.json.example`、`ToukaChat/src/lib/error_codes.ts`、翻譯檔。PR 1：`ToukaProxy/src/utils/mcp_request_identity.py`（`is_group_model`）、`utils/local_bridge_gate.py`、`middleware/layers/soul.py`、`middleware/layers/tool.py`、`middleware/layers/skill.py`、`services/dispatcher.py`、`services/dispatchers/group.py`、`utils/group_member_view.py`、`services/continuity_lines.py`（新）、`services/continuity_service.py`

## 背景

可視性檢查（2026-11-15～16）的第五批「其他小問題」：合併清單第 2 版的前景-9、前景-11、群聊-3、家-4、家-5、心跳-6、心跳-8、子代理-8，11/15 順帶發現的心跳請求編號，以及前四批留下、和「AI 看到什麼」有關的後續。分四個 PR：群聊、心跳、家與子代理、工具資訊與網頁以外的路。

寫計畫時搜尋代理與継初都以為群聊成員能用沒勾的工具；審查員指出成員身分沒有權限角色，核對屬實：成員叫什麼工具都會被擋。所以這批群聊不動權限，只拿掉成員看到的工具說明。

## 決定（PR 1：群聊）

### 1. 群聊判斷共用一個函式

- `utils/mcp_request_identity.py` 加 `is_group_model(name)`：模型名是 `ToukaGroup`，不分大小寫（和分發器一樣）；收到 None 不算。
- 六處改用它：SoulLayer 跳過群聊、ToolLayer、SkillLayer、分發器的群聊路由、使用者各自授權的 MCP、本機檔案工具。以前前三種寫法有的分大小寫，`toukagroup` 這種寫法會讓 SoulLayer 把群聊當成一般請求。

### 2. 群聊回合不組伺服器端工具，也不注入工具相關的區塊

- 現況（先用沙盒測試釘住）：成員身分是 `AgentIdentity(agent_id=...)`，沒有權限角色，`resolve_grants(None)` 是空授權。內建與記憶工具在執行器的角色檢查就被擋（`native_tool_rbac`），MCP 被這一輪的清單（`not_advertised`）或 MCP 角色檢查（`mcp_rbac_denied`）擋；改了之後清單是空的，一律被清單擋。但外層 ToolLayer 組的工具與區塊會被複製給每位成員：成員看到用不到的工具說明，SNS 群聊還看到 bot 那個靈魂的看板（外層 ctx 帶著 bot 的靈魂）。
- 改成：ToolLayer 遇到群聊回合，內建、記憶、MCP、調度工具都不組，清單照樣經原本唯一的寫入點寫成空的（結構測試 C3 只准 `tool.py` 一處寫入）；寫完就結束，不注入 `<tool_availability>`、`<mcp_unavailable>`、`<validation_protocol>`、`<harness_context>`、`<approval_context>`、`<mission_awareness>`。照舊做的兩件：寫入模式（`auth_level`，hook 的 payload 要用）、把用戶端帶的工具正規化成 OpenAI 形狀（`/v1/messages` 的 API 群聊交給 OpenAI、Gemini 成員時要用）。
- SkillLayer：群聊回合不注入任何技能（技能清單、標頭選的技能、對話裡啟用的技能、深入研究都不加）。人與 SNS 本來就沒有技能授權；代理身分的呼叫者（例如用靈魂 token 開的 API 群聊）以前會把技能說明複製給成員，說明教它用 `activate_skill`，成員卻沒有這個工具。
- 群聊說明加一句「There are no server-side tools in a group chat; reply with text only.」。用戶端帶的工具定義照舊轉送（只正規化形狀），成員叫了一樣被執行器擋下（不在這一輪的清單）；這是既有行為，不是這批改的。
- 權限完全不變。網頁群聊成員能不能用自己勾的工具是放寬權限，要另外設計成員身分、核准卡與停止，列進路線圖候選；SNS 群聊不論怎樣都不給工具（ADR-0276）。

### 3. 成員知道現在幾點、這是群聊

- 每位成員的群聊說明之後接一段精簡 `<continuity>`：開頭說明、現在時間、「This conversation: a group chat; its content is not reported to your home」（無痕群聊寫無痕那一句）。不讀上一輪、不讀家、不寫事件：群聊沒有成員自己的對話，其他行對成員都不成立。
- 開頭說明、時間行、對話種類搬到新的 `services/continuity_lines.py`：`continuity_service` 會讀家與交接，經摘要繞回分發器，群聊分發器直接匯入它會循環匯入。兩邊都引用同一份。

### 4. API 群聊成員失敗時，下一輪認得出是出錯

- 一般群聊（不是 SNS）成員失敗時，分發器在這位成員段落最後接單一括號的記號「〔@A 這一輪出錯：{已遮罩的說明}〕」，說明裡的〔〕先換成［］，換行與空白收成一個空格（說明是廠商的錯誤原文，夾著分隔線加成員標籤時，切段會多出一位假成員）。SNS 群聊照舊送給外人固定文字。網頁不會走到（`ui_authorized` 的路先停下，整輪標失敗）。
- 自己送歷史的 API 用戶端下一輪送回來時，`member_messages` 切段只認段落**最後**、別名和這一段標籤相同的記號：別人的段落換成和同一輪一樣的「（@A 這一輪出錯，沒有說完）」，失敗說明不算那位成員的話；自己的段落只拿掉記號（和同一輪裡自己的失敗一樣）。段落中間或別名不符的不算，最多讓自己那段被標成出錯，碰不到別人。
- 防偽在產生的時候：成員串流時，`LabelGuard` 把長得像記號開頭的寫法換掉一個字（〔→［），可能長成記號的尾巴先留著；真記號在守門之後才接上。這個記號**不**加進 `neutralize_peer_markers`：那個函式在切段前就跑，加進去真記號會在解析前被換掉。
- 新記號和既有的「（@A 這一輪出錯，沒有說完）」分開：前者是畫面與用戶端看得到的，後者只給成員看。

### 5. 成員的請求帶對話編號

- 分發器呼叫 `get_stream_generator` 時傳 `session_id`。以前沒傳，OpenAI、Gemini 等非 Claude 成員的 ctx 對話編號被設成 None，帶對話編號的 API 群聊按停止時停不了它們。
- 也因此和 Claude 成員一樣：網頁群聊的非 Claude 成員收得到插話；Responses API 的 `prompt_cache_key` 從「代理加空字串」變成「代理加對話編號」。

## 決定（PR 2：心跳）

### 6. 跑得了才寫初始提示

- 以前 `/awaken` 先把初始提示寫進對話，才檢查有沒有大腦；模型名稱有問題、每日配額或花費上限用完時要到背景迴圈才發現。家裡疊出沒人回的提示，你下一次說話時和你的話併成一則，AI 可能以為你也要它做那件事（心跳-6）。
- 改成寫初始提示之前先檢查，探針照舊先寫再回、不檢查：
  - 模型：和脈衝同一套解析（`_resolve_pulse_model`，`model_override` 優先），丟出真正的錯誤碼。沒設主要模型 409 `AGENT_BRAIN_NOT_SET`、略過原因 `no_brain`；名字只差大小寫等解析不出來時回 `request_model_name` 丟出的碼（例如 `MODEL_NAME_AMBIGUOUS`）、略過原因 `no_model`。
  - 每日自主配額與花費上限：背景迴圈原本那組檢查抽成 `_autonomy_limit`，入口與每個脈衝前共用。不夠時回新錯誤碼 429 `AUTONOMY_LIMIT_REACHED`（1121，兩種共用），原因（`quota`、`budget`）只寫在略過事件裡。
- 略過事件照 ADR-0287：寫入期間開始重置、清除或刪除的不記。檢查排在「寫初始提示之前再看一次重置」之後。
- 錯誤碼照 SSOT 流程：`errors.json.example` 加一筆，`sync_errors.py` 重產 `ToukaChat/src/lib/error_codes.ts`，兩語翻譯各一句。ToukaHeartbeat 那份自動產生的檔只收 `HEARTBEAT_` 開頭的碼，這次不變（計畫寫會跟著更新，核對後不需要）；心跳端收到錯誤只記 log、不重試。
- 背景迴圈每個脈衝前照舊再檢查（多脈衝之間仍可能用完）；`_pulse_model_name` 改成包一層 `_resolve_pulse_model`，log 字面不變。

### 7. 初始提示寫明是排程送的

- 初始提示在開頭那行（`【目前系統時間：…】`，審查員與前端靠它）之後多一行「〔這是排程心跳的預設提示，由系統送出，不是人當下說的話〕」（`user_intent.HEARTBEAT_INIT_NOTE`）。舊的提示不回頭改。

### 8. 心跳的 `<continuity>`：寫明沒帶技能與專案；不是靈魂的也拿精簡版

- 靈魂的心跳多一行「Heartbeat runs do not load chat skills or the conversation's project context; only your persona, prelude and the listed tools.」。
- 不是靈魂的代理人（例如 DebugAgent）跑心跳時，以前完全沒有 `<continuity>`（ADR-0267 只給靈魂），不知道沒人在場、第幾個脈衝。改成精簡版：現在時間、沒人在場、第幾個脈衝與被誰叫醒、「…; only the listed tools.」；不讀活動紀錄、看板與家（那些是靈魂的）。
- 這批只寫明沒帶；真的帶技能與專案會改行為、多花 token，不在這批。

### 9. 每個脈衝有自己的請求編號

- 11/15 順帶發現：心跳的 ctx 沒有請求編號，走 Claude 的那條路也沒補，工具輪計數（`rhythm_service`，照請求編號）全部共用同一個空鍵。兩個靈魂的心跳同時跑時，後開的會把前一個剩下的輪數重設成滿的，兩邊扣同一個計數。
- 改成脈衝的 ctx 帶 `request_id`；`dispatchers/factory.py` 的 Claude 分支只多帶 `request_id` 這一個鍵（另外兩家原本就由 `run_info` 帶；不照抄 `run_info`，否則沒傳 `session_id` 的呼叫端會被設成 None）。

### 10. 拿掉切段

- 模型寫出「--- … 自主行動」時，心跳會切成兩則、中間插一句「【系統】自主行動階段完成，請繼續。」（舊程式留下的）。拿掉，整個脈衝存一則；工具步驟與出錯旗標都在那一則上。舊對話裡已經存下的照舊（帶 `SYSTEM_SPEAKER`）。

## 取捨與影響

- PR 1：成員的請求少掉幾段工具說明、多一句群聊說明與一段 `<continuity>`（都在請求尾端）；上線後每位成員第一輪的提示快取重寫一次。權限不變。
- PR 1：記號文字照樣存在 API 用戶端與後端另存的那份裡；讀那份的其他地方（例如摘要）看到的是寫明出錯的記號。
- PR 2：心跳跑不了時不再把提示寫進家對話，排程那端收到 409 或 429，只記 log、不重試。多一個錯誤碼：網頁畫面沒改，只多兩句翻譯。靈魂的心跳每個脈衝多一行說明；不是靈魂的多一段精簡 `<continuity>`。

### 已知限制

- PR 1：成員在自己段落最後寫出跟真記號一模一樣的文字（守門換掉一個字以前就已經存在用戶端的舊對話）時，下一輪會把自己那段標成出錯；碰不到別人。
- PR 1：舊對話裡這版之前的失敗句（沒有記號）照舊算成那位成員的話。
- PR 1 審查發現的既有問題，排進 PR 4：誠實度檢查（成員沒查資料就講記憶裡的事時插一輪矯正）不看這一輪有沒有工具，矯正輪會強制要求用工具（`tool_choice`），沒有工具時廠商回 400，那位成員這一輪失敗。網頁群聊以前就是零工具；這個 PR 之後 SNS 與代理身分開的 API 群聊也一律零工具，範圍跟著變大。觸發詞很窄，發生機率低。

## 驗證（PR 2）

- `tests/unit/test_heartbeat_run_checks.py`（新）：沒設大腦、名字模糊（主要模型或排程指定）、配額、花費都在寫提示之前擋下、回對的碼、記對的原因、不寫提示也不啟動迴圈；跑得了時提示多那一行；探針照舊先寫；靈魂心跳的 `<continuity>` 有沒帶技能那行、一般聊天沒有；不是靈魂的拿精簡版（脈衝實際送出的請求也有）；兩個脈衝同時跑各有自己的請求編號；Claude 分支帶請求編號、不動其他鍵；分隔線不再切段。
- 改了舊測試：`test_reply_failure_marks.py`、`test_tool_step_collector.py`（不再切段）、`test_heartbeat_session_scope_security.py`（只剩一次寫入）、`test_system_speaker_contract.py`（nudge 只剩一種）、`test_continuity_not_persisted.py`（不是靈魂的心跳拿精簡版、照樣不存）。

## 驗證（PR 1）

- `tests/unit/test_group_round_tools.py`（新）：
  - `is_group_model` 的大小寫與 None；兩個人在場條件都擋各種大小寫的群聊。
  - 先釘現況：成員叫內建工具被角色檢查擋、叫 MCP 被空清單擋，工具都沒有被呼叫。
  - ToolLayer：同一組設定不是群聊時工具與區塊都在（對照組）；群聊回合清單是空的、用戶端工具留著並轉成 OpenAI 形狀、模式照寫、沒有任何工具相關區塊與 bot 的看板。
  - SkillLayer：代理身分有技能授權、標頭選了技能、對話啟用了技能、開了深入研究時，群聊回合一個技能都不注入（不是群聊的對照組都在）。
  - 群聊說明有「沒有伺服器端工具」；精簡 `<continuity>` 只有時間與對話種類（無痕寫無痕）；每位成員都拿到，接在群聊說明之後。
  - 成員拿到對話編號；factory 對非 Claude 成員保留它。
  - 出錯記號：說明裡的括號換掉、夾著分隔線加成員標籤的說明收成一行，切段不多出成員；失敗時只送一次、下一位只看到出錯、看不到說明；沒寫字就失敗時只送記號；SNS 照舊送固定文字；串流裡偽造的記號不論怎麼切片都被換掉、單獨的〔不會一直被留著；別名超過 64 字馬上放出、剛好 64 字先留著，接著不是記號就放出；跨一輪後真記號被認出（自己的段落只拿掉記號），段落中間、別名不符或已經被換成［開頭的都不算。
- `tests/unit/test_user_scoped_mcp_security.py`：群聊回合連 MCP 清單都不拿（取代原本「群聊不帶身分」那一格）。
- 其他群聊測試的假模型改成收得下 `session_id`；`test_model_name_case_lookup.py` 的群聊測試把讀家換成替身（只在亂數順序下才會出錯）。
