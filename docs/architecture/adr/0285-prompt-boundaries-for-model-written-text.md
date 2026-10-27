# ADR-0285：舊審查報告甲組：提示裡模型寫的文字不能偽造可信區塊

- 狀態：🚧 部分實作（甲-1、甲-2 已實作；同組的甲-3 列出的工具預設開，PR 完成時補進本 ADR）。維護者 2026-10-25 核准修正計畫（「照建議：1～5 都選甲」）。
- 日期：2026-10-25
- 決策者：維護者
- 修訂 ADR-0257（S1「內文先中和」：前情摘要與家對話也中和其他 prompt 區塊 tag）；修訂 ADR-0267（已知限制最後一條：看板那一行的任務 id 也改用 `safe_ref`）；修訂 ADR-0266（甲-2：決策 1「分支之間先出現者為準」與已知限制第 4 條：同名欄位的定義都保留，多種合成 `anyOf`）。
- 相關：ADR-0092（prompt 區塊邊界）、ADR-0127～0129（Claude 提示快取、波動區塊）、禁令 18（內容消毒抽成純函式）、禁令 20。
- 影響範圍：ToukaProxy `adapters/tool_schema.py`（甲-2）；`tests/unit/test_provider_tool_schema_egress.py`（甲-2）；ToukaMemory `utils/historical_data.py`；ToukaProxy `services/continuity_service.py`、`services/soul_service.py`、`middleware/layers/context.py`；新 `tests/unit/test_mission_board_boundary_security.py`、`tests/unit/test_historical_data_boundary_security.py`；SECURITY、MEMORY_INTERFACE。

## 背景

10/15 的程式審查（Touka #89～#105）在 dev `426dceda` 上核對後，甲組（安全與信任）仍有效的幾項：

- **High**：`<continuity>` 標成「系統記下的事實」，但看板那一行直接印出 `Task.id`（`continuity_service.py:266`）。任務 id 是模型取的、沒有格式限制，可以帶換行、假的「最後一則人類訊息」或 `</continuity>`，之後每一輪（含心跳）都會當成系統事實。同檔「最近完成」那一行早已用 `safe_ref`，這一行漏掉了。
- **同一種偽造的另一個入口**（調查時發現）：`<mission_awareness>`（由 ToolLayer 注入 system）原樣放入模型寫的大目標、任務 id、描述、依賴與產出；換行可以多造一行任務，嵌入的 `</mission_awareness>` 可以提早關閉區塊。
- **歷史資料只中和自己的標籤**：前情摘要與家對話只中和 `historical_data`。摘要裡一個 `</conversation_summary>`，在 Claude 路徑上更嚴重：送出前 `reorder_system_stable_first` 用非貪婪配對把波動區塊搬到尾端，摘要提早結束，後面的文字留在穩定前綴、沒有任何界線，還會進跨回合快取。

- **送給 Claude 的外部工具，同名欄位只留第一個定義**（甲-2）：Anthropic 不收根層組合子，出場時把各分支的欄位併回根層（ADR-0266）；分支之間同名欄位定義不同時只取第一個。外部 MCP 工具把判別欄位只寫在分支裡時（例如每個分支的 `kind` 各是一個 `const`），Claude 只看得到第一種，選不到其他種；欄位型別在分支之間不同時也一樣。

## 決定（甲-1）

1. **`<continuity>` 看板那一行用 `safe_ref`**：和「最近完成」那一行一致，只放行 `[A-Za-z0-9._:-]{1,32}`，其他換成 `h:` 加雜湊。這個區塊只是摘要，細節導向看板。
2. **`<mission_awareness>` 保留原文、只做跳脫**：這個區塊是模型操作看板時看的，mission 工具用 id 完全比對找任務與依賴（`mission_manager.py:318, :363, :399`），所以不能換成 `safe_ref`（超過 32 字或含中文的 id 會之後更新不到）。大目標、任務 id、描述、依賴與產出：
   - `str.splitlines()` 會切開的字元（`\r\n`、`\n`、`\r`、`\x0b`、`\x0c`、`\x1c`～`\x1e`、U+0085、U+2028、U+2029）都換成可見的 `⏎`，其他控制字元換成空白，不能多造一行；
   - 然後才把 prompt 區塊 tag（`ALL_BLOCK_TAGS`）的開頭 `<` 換成 `&lt;`，不能關閉或另開區塊。順序不能反：先中和、後刪控制字元的話，`<`＋NUL＋`/tag>` 會重組成真的 tag。
   - 單行化與控制字元處理是新的純函式 `src/utils/model_text.py`（`single_line`、`blank_control_chars`，禁令 18）。
3. **新純函式 `neutralize_tags(text, tags)`**（`memory_server/utils/historical_data.py`，禁令 18）：規則同既有的界線 tag 中和（不分大小寫、`<` 與 `/` 前後可有空白、帶屬性與自閉合都算），另要求 tag 名稱後面接非名稱字元，`persona` 不會動到 `<personality>`。Memory 不能匯入 Proxy，所以 tag 清單由呼叫端傳入。
   - 空白用 possessive 量詞（`\s*+`），新舊兩個 pattern 都改：原本兩段 `\s*` 互相回溯，`<` 後面一長串空白要花平方時間（2 萬個空白約 9 秒），在主迴圈上會讓整個 Proxy 卡住。tag 名稱都是字母開頭，語意不變。
4. **前情摘要與家對話也中和其他區塊 tag**：`wrap_historical_data` 多一個 `extra_tags` 參數；`soul_service` 的前情摘要與家對話傳入 `ALL_BLOCK_TAGS`（界線 tag 照舊另外中和），並先把控制字元換成空白（換行保留）。
   - 家對話的內容先還原成模型會看到的形狀（`normalize_message_content`）再中和；原本是 JSON 字串的，中和後存回 JSON 字串，送出形狀不變（原生 Anthropic 入口不轉換 `image_url`）。舊做法在原始 JSON 字串上中和，JSON 跳脫（`\u003c`、`\/`）當下還不是 `<`、`/`，轉接層送出前解開後就變成真的 tag，連 `historical_data` 界線本身也能被關掉；這個漏洞從 ADR-0257 就有。
   - 多模態 part 的 `text`、`thinking`、巢狀 `content`（工具結果等，字串或清單）與清單裡的純字串都中和。
5. **不做**：
   - 任務 id 的格式限制（只在建立時檢查）：描述本來就是自由文字，增益很小；在 `Task.id` 加 validator 會因 `mission_manager.py:108-117` 吞錯而讓舊看板靜默消失。
   - Memory 端的查記憶、隨機回想與舊對話查詢（`manager.py`、`recall.py`、`legacy_vault.py`）只中和界線 tag：工具結果本來就在信任較低的位置，這批不改。

## 決定（甲-2）

6. **分支之間同名欄位的定義都保留**：`_anthropic_input_schema` 依出現順序收集各分支同名欄位的定義、相同的只留一份；只有一種照用，多種合成 `{"anyOf": [...]}`（內層可以用 `anyOf`，Anthropic 收）。同名以根層為準、`required` 只留根層原有的、根層一律 `object`，都不變。只影響送給 Anthropic 的那份；正本、執行前檢查、其他廠商不變。
7. **Touka 自己的工具不受影響**：mission_tools、memory_tools、edit_soul 分支裡根層沒有的同名欄位，定義都一樣（新測試釘住），送給 Claude 的形狀和以前相同。
8. **不在這批**：Touka 自己工具的廣告 schema 依動作合併同名欄位時也是「第一個為準」（`ToukaMemory/.../tools/contracts.py` 的 `action_parameters`，HANDOVER §3），影響所有廠商、要另外處理。

## 後果

- 使用者畫面沒有差別。模型與 context 快照畫面會看到 `&lt;` 與 `⏎`。
- 一般的任務 id、描述（含 `<b>` 這類非區塊標籤）照原樣顯示。
- 已知限制：
  - ToukaDispatch 建任務不經 Proxy 的工具合約（`ToukaDispatch/server.py:358-391`），id 照樣不限格式；顯示端已處理，所以不會進到可信區塊。
  - 看板載入失敗會被吞成「沒有看板」（`mission_manager.py:108-117`），這批不改。
  - 只有模型看得出來的變體沒有中和：`<` 後面夾零寬字元（U+200B、U+2060 等）、全形 `＜／＞`、`&#60;` 這類 HTML 實體，以及 NFKC 正規化後才變成 tag 的寫法。Touka 自己的解析（`parse_system_blocks`、`reorder_system_stable_first`、`to_cached_system`）只認精確的 `<tag>`，不受影響；剩下的是模型自己會不會誤讀。
  - 舊紀錄裡刻意用 JSON 跳脫寫出單獨的代理字元（U+D800～U+DFFF）時，解開後的文字無法編碼成 UTF-8，那次請求會失敗；經轉換的路徑本來就是這樣，這次原生 Anthropic 入口的中間幾則也一樣。
  - id 含換行、控制字元或區塊 tag 時，看板顯示的是跳脫後的樣子，模型照抄去呼叫 mission 工具會對不上；這類 id 本來就是異常輸入。
  - 範圍外的觀察（審查時發現、不是這次造成）：`<harness_context>` 的固定文字裡就有 `<conversation_summary>` 字樣，`to_cached_system` 用 `find` 找最早的波動區塊時會在 harness 中間切開，`tool_availability`、`validation_protocol` 被留在波動段，影響的是快取命中、不是安全（ADR-0139 起就有），記在 HANDOVER §3。
- 甲-2：外部 MCP 工具在 Claude 上看得到每個分支的欄位定義；一般只有一種定義的欄位形狀不變。Claude 仍看不到「哪個值要配哪些欄位」（條件必填只剩說明文字，ADR-0266 已知限制），送錯組合時靠執行前檢查擋下。
- 撤回：甲-1、甲-2 各自 revert 即恢復原樣；本 ADR 檔保留，在狀態註記撤回（後續批次會引用它）。

## 驗證

- 先寫的測試在照原樣回傳的空殼上 8 個紅（看板偽造、`<mission_awareness>` 關閉與多造行、摘要與家對話關閉或另開區塊、摘要經重排後注入文字留在穩定前綴），實作後全綠；一般 id 與文字原樣的案例修前修後都綠。
- 程式審查有兩位：內部子代理，以及 xAI `grok-4.7`（經 pi，只能讀檔；維護者同意送出那一次，送 12 個公開 repo 的檔，花費 US$0.72）。兩位都抓到家對話 JSON 跳脫的繞過；內部審查另外抓到長空白的平方時間。修正後新增的測試在修正前的版本上 10 個紅（JSON 跳脫 2、巢狀內容 1、控制字元 4、分隔字元 3），長空白的案例在修正前的版本超過 300 秒；每個區塊 tag 各一個案例與看板控制字元順序的案例是防回歸用，修正前後都綠。
- 突變檢查 27 個全部變紅（含 possessive 量詞改回一般量詞時逾時）。
- 甲-2 先寫的測試修正前 3 個紅：欄位只在分支裡的 MCP 工具（同名欄位型別不同）、判別欄位只在分支裡（各分支的 `const` 都要選得到、相同定義只留一份、依出現順序）、合成的 `anyOf` 是新建的不改到輸入；「Touka 自己工具的分支沒有衝突」修正前後都綠。變異檢查 6 個全部變紅（又只留第一個、相同的不去重、只有一種也包 `anyOf`、順序反過來、分支蓋過根層、只看 `oneOf`）。
