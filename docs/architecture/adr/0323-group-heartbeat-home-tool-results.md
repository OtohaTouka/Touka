# ADR-0323：群聊沒有工具、心跳先檢查再寫、家知道去過哪、工具結果有上限（可視性第五批）

- 狀態：🚧 實作中（PR 1 群聊，2026-11-19；PR 2 心跳、PR 3 家與子代理、PR 4 工具資訊與網頁以外的路待做）
- 日期：2026-11-19
- 決策者：維護者（一頁計畫第 2.1 版，總覽板第 90 項回「甲：照第 2.1 版做」；工具結果上限 4 萬字、子代理結果檔可偽造移出這批，是計畫裡寫明的「我自己定的兩件」）
- 相關：[ADR-0322](0322-who-said-it-and-which-conversation.md)（第四批：群聊成員發言、對話種類）、[ADR-0269](0269-advertised-tool-list-enforcement.md)（這一輪的工具清單）、[ADR-0276](0276-heartbeat-session-scope.md)（SNS 外人不能驅動有工具的靈魂）、[ADR-0316](0316-keep-harness-blocks-and-state-approval-mode.md)（區塊分組）、[ADR-0267](0267-trusted-activity-timeline-and-continuity-v1.md)（`<continuity>`）
- 修訂：[ADR-0269](0269-advertised-tool-list-enforcement.md)（決定 7 群聊沿用的清單是空的，PR 1）、[ADR-0322](0322-who-said-it-and-which-conversation.md)（已知限制：群聊成員的區塊與 `<continuity>`、API 群聊成員失敗句，PR 1）
- 影響範圍：PR 1：`ToukaProxy/src/utils/mcp_request_identity.py`（`is_group_model`）、`utils/local_bridge_gate.py`、`middleware/layers/soul.py`、`middleware/layers/tool.py`、`middleware/layers/skill.py`、`services/dispatcher.py`、`services/dispatchers/group.py`、`utils/group_member_view.py`、`services/continuity_lines.py`（新）、`services/continuity_service.py`

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

## 取捨與影響

- PR 1：成員的請求少掉幾段工具說明、多一句群聊說明與一段 `<continuity>`（都在請求尾端）；上線後每位成員第一輪的提示快取重寫一次。權限不變。
- PR 1：記號文字照樣存在 API 用戶端與後端另存的那份裡；讀那份的其他地方（例如摘要）看到的是寫明出錯的記號。

### 已知限制

- PR 1：成員在自己段落最後寫出跟真記號一模一樣的文字（守門換掉一個字以前就已經存在用戶端的舊對話）時，下一輪會把自己那段標成出錯；碰不到別人。
- PR 1：舊對話裡這版之前的失敗句（沒有記號）照舊算成那位成員的話。
- PR 1 審查發現的既有問題，排進 PR 4：誠實度檢查（成員沒查資料就講記憶裡的事時插一輪矯正）不看這一輪有沒有工具，矯正輪會強制要求用工具（`tool_choice`），沒有工具時廠商回 400，那位成員這一輪失敗。網頁群聊以前就是零工具；這個 PR 之後 SNS 與代理身分開的 API 群聊也一律零工具，範圍跟著變大。觸發詞很窄，發生機率低。

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
