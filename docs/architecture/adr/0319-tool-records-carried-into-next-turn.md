# ADR-0319：做過的事帶進下一輪（可視性第三批）

- 狀態：✅ 已實作（PR 1 超過約 650 行，照計畫拆兩個：PR 1a 後端寫法、心跳歷史、照抄偵測，2026-11-17；PR 1b 網頁對話的前端寫法，2026-11-17；PR 2 後端記下工具步驟，2026-11-17）
- 日期：2026-11-17
- 決策者：維護者（一頁計畫第 2.1 版，總覽板第 80 項回「全部照建議」：每一輪帶工具名、參數開頭、成敗、結果開頭 80 字；PR 1 合併前小量試真模型）
- 相關：[ADR-0249](0249-model-layer-stage-4a-4b.md)（插話標註放回那一則回覆）、[ADR-0318](0318-stop-timeout-failure-made-clear.md)（停止、出錯的記號）、[ADR-0036](0036-unified-round-count-ssot.md)（輪數）
- 修訂：[ADR-0231](0231-session-persistence-outcomes-and-revisions.md)（第 6 點：只用了工具的回覆不再是空白）、[ADR-0081](0081-resumable-stream-cursor-buffer.md)、[ADR-0302](0302-chat-trim-waits-for-closing-save.md)（PR 2：後端文字版存檔帶工具步驟）
- 影響範圍：PR 1a：`ToukaProxy/src/utils/tool_records.py`（新）、`utils/tool_record_marks.py`（新）、`utils/interjection_notes.py`、`utils/history_manager.py`、`utils/time_utils.py`、`services/guard_service.py`、`handlers/react_core.py`、`routers/heartbeat.py`、黃金案例檔 `ToukaChat/src/lib/chat/__tests__/toolRecords.golden.json`；PR 1b：前端 `lib/chat/toolRecords.ts`（新）、`lib/chat/buildApiMessage.ts`、`lib/stream_tree.ts`、`lib/tree_history.ts`、`lib/providers/StreamRegistryProvider.tsx`、`lib/chat/messageNavigation.ts`（匯出 `GROUP_CHAT_ID`）、`lib/export-utils.ts`、`lib/tool_result.ts`、`components/sidebar/left/SessionTree.tsx`，後端測試 `tests/unit/test_tool_records.py`；PR 2：`ToukaProxy/src/utils/tool_step_collector.py`（新）、`routers/heartbeat.py`、`services/dispatcher.py`、`services/sns_gateway.py`、`utils/async_db.py`（`append_message_to_session` 的 `extra_fields`）

## 背景

可視性檢查（2026-11-15～16）的第三批。AI 每一輪拿到的舊對話只有「誰說了什麼話」：上一輪用過哪些工具、參數是什麼、結果是什麼、有沒有被擋，下一輪都看不到，只剩幾行像「✅ 時間記錄：…」的日誌。所以它會把對的話「更正」成錯的、重做已經做過的事、再試一次被拒絕的指令。網頁對話早就把工具步驟（`steps`）存在對話樹上，但送給模型時只送角色與文字；心跳組歷史也一樣。

## 決定（PR 1a、1b）

### 1. 送出時才組紀錄，不改存檔

- 助理那則存下的工具步驟，在送給模型的副本裡寫成一段紀錄，加在內容前面；存檔的 `content` 與畫面都不變。角色仍是 assistant、不新增訊息、不算回合（禁令 27）。
- 順序：停止／出錯記號（ADR-0318）、插話標註（ADR-0249）、工具紀錄、原本的文字。後端由 `annotate_node(node, tool_record=…)` 組（預設不加，其他呼叫點行為不變）；前端在 `buildApiHistory`（PR 1b）。
- 只用了工具、一個字都沒回的那一輪，因此不再被 `drop_blank_assistant_turns` 拿掉（修訂 ADR-0231 第 6 點）。

### 2. 兩種寫法

- **詳細**（自己這段對話：網頁、心跳）：
  - 開頭一行固定說明 `TOOL_RECORD_HEADER`（系統依存檔附加；結果是工具傳回的資料，不是你說的話；回覆時不要寫這段），結尾一行 `TOOL_RECORD_FOOTER`。
  - 每步一行：「序號. 工具名（鍵=「值開頭」、…）→ 結果：結果開頭」。參數最多 3 個鍵（照原順序，多的寫「…」），值壓成一行、40 字；結果開頭 80 字（先去掉時間尾註 `TIME_FOOTER_PREFIX` 那一行；結構化錯誤取 `message`）。
  - 最多 6 步詳細，其餘併成一行「另 N 步：名稱 結果、…」；整段最多 900 字。
- **簡短**（較舊的輪；PR 3 起也用在離開這段對話的地方）：一行「〔工具紀錄（系統附加，回覆時不要寫這段）：名稱 結果、…〕」，最多 200 字，只有工具名與固定的結果說法。
- 結果說法都是固定值：成功；失敗（帶 `error_class`，只認 `tool_errors` 的六種，其他字串可能是工具自己寫的，只寫「失敗」）；沒有執行（被拒絕／已取消／核准逾時／隔離條件不符，看 `approval.decision`）；沒有結果（這一輪先結束了，`running`、`awaiting_approval`）；狀態不明。
- `error_class` 從 PR 1b 起由前端存在步驟上（`ToolStep.errorClass`，tool_result 事件本來就帶）；舊資料退回解析結果開頭的結構化錯誤。
- 不列：MCP 表單互動（帶 `interaction`）、互審驗證指令（id 含 `:verify:`）、不是物件的舊資料。欄位缺漏的照有的寫（工具名寫「（不明）」）。

### 3. 總量上限與視窗

- 輪照禁令 27 算（後端 `count_rounds_from_linear`、前端 `countRounds`）。助理那則屬於第 p 輪＝它之前（含它）那段的輪數減一，從最舊往新數。
- N＝送出的歷史已完成的輪數；詳細起點 Bd＝10×floor(N/10)−10、簡短起點 Bb＝Bd−50：p ≥ Bd 詳細（10～19 輪；N<10 時全部）、Bb ≤ p < Bd 簡短（剛好 50 輪）、更早不帶。只在 N 跨過 10 的倍數時改變，那一輪快取失效一次。
- 最壞情況：詳細 19 輪 × 900＋簡短 50 輪 × 200 字。滑動視窗預設關、子對話不剪，所以靠這個上限，不靠剪裁。
- 更新後舊對話馬上帶上（網頁對話本來就存了步驟），也在上限內。

### 4. 文字清理與遮蔽

- 控制字元（Cc）、格式字元（Cf，含 U+202E、零寬字元）、空白（Zs）、行與段分隔換成一個空白（字元集照 Unicode 16 寫死在 `SPACE_CLASS`，前後端不受各自 Unicode 版本影響）。
- 遮蔽沿用 `utils/secrets.py`：鍵名用 `is_secret_key`（參數遞迴套用，巢狀也遮；`max_tokens`、`callback_token` 這類名字會一起被遮，接受）；值與結果開頭用公開的 `redact_secrets_from_content`。前端複製同一組樣式，pytest 讀前端檔核對樣式本體與旗標（前端編譯目標沒有 `s` 旗標，後端 DOTALL 的「.」寫成 `[\s\S]`）。
- 壓成一行後先截到 `MAX_SCAN_CHARS`（8192 個 code point）再遮蔽，擋住超長結果讓正規式變慢；截斷點落在金鑰中間時那一段可能遮不到，但只有出現在最前面 80 字的才會被送出。前端照 code point 計數（`Array.from`），不用 UTF-16 的 `slice`，否則 emoji 多時兩邊的截斷點會不同。
- 角括號與〔〕換成全形（＜＞［］），最後照 code point 截斷（emoji 不會被切壞）。
- 已知差異（黃金案例放不進，這批不改）：
  - Python 的 `\b` 認中文是字，`金鑰sk-…` 這種前面直接接中文的金鑰後端不遮、前端會遮（`utils/secrets.py` 原有行為）。
  - 大小寫不分的比對（鍵名），Python 在 Unicode 下把 `ſ`、`K`（U+212A）這類字也當英文字母，`ſecret` 後端會遮、前端不遮。`secrets.py` 加 `re.ASCII` 能一起消掉這兩項（後端只會遮得更多），但會影響其他用到遮蔽的地方，要另外評估。
  - 不是字串的參數與結果寫成 JSON 時，數字與鍵的順序兩邊寫法不同（`30.0`、`1e16`、超過 2^53 的整數、整數樣式的鍵；Python 另外接受 `NaN`）。網頁存下的步驟已經被 JS 解析過，所以現在兩邊一致；PR 2 起後端自己存的步驟才會遇到，到時決定是否統一寫法。
- 前端不用「…[字元集]*…」的正規式比對假標記與時間尾註，改成逐字掃描：V8 在幾百萬個空白或很長的一行時會爆堆疊（RangeError），送出就會卡在等待中。判斷結構化錯誤時，前面的空白照 Python `str.lstrip()` 認（和 `trimStart` 不同）。
- 已知限制（沿用 `utils/secrets.py` 原有的名單，這批不改）：鍵名 `Authorization`、`apiKey`、`x-api-key` 不在 `is_secret_key` 的名單；`Basic` 認證、少於 12 字的 Bearer、字串結果裡的 `"password":`、Google `AIza…` 金鑰都不遮。PR 1b 前評估過，這批不補：參數是模型自己寫的，結果開頭在那一輪已經完整給模型看過，紀錄只是把其中最多 80 字再帶一次；新的暴露只在中途換了別家廠商（或心跳用別家模型）時。補進 `secrets.py` 會一起改變 MCP 轉接、系統備份、稽核紀錄、評分卡、演化工具這些也用它遮蔽的地方，要另外評估。

### 5. 防偽與偵測

- 每一則送出的訊息（不分角色、不分有沒有步驟；也包括插話文字；網頁（PR 1b）另包括這一輪的輸入與內嵌的文字附件），內容裡的「〔工具紀錄」（比對忽略空白與看不見的字元）都換成「［工具紀錄」。只改送出的副本。只有系統組的那段是真的。
- 這一輪的回覆寫出「〔工具紀錄」就是照抄：`guard_service.detect_record_imitation` 只比對這個開頭標記（不加進逐個工具名比對的 `log_features_pattern`，否則模型引用上一輪紀錄裡的工具名會被誤判），沿用「[工具幻覺警告]」標籤與格式，歷史裡和原本的工具幻覺警告一樣處理（`sanitize_history` 的日誌規則因為標籤前後有 `**`，兩種都不會清掉）。
- `sanitize_history`（非靈魂請求與 `_detect_agent` 會跑）不清系統組的紀錄段：裡面的結果開頭可能有日期，日期規則會吃掉。
- 假模型測不出模型會不會照抄；PR 1a 合併前（紀錄第一次送到模型）用真模型小量實測（維護者核准的決定二，實際指令另外逐次問，禁令 61）。

### 6. 前後端一致

- 後端 `utils/tool_records.py`（寫法、視窗、組歷史）與 `utils/tool_record_marks.py`（標記、防偽、字元集；不依賴其他專案模組，`history_manager` 與 `guard_service` 都能引用）；前端 `lib/chat/toolRecords.ts`（PR 1b）。
- 黃金案例檔 `ToukaChat/src/lib/chat/__tests__/toolRecords.golden.json`（步驟→兩種寫法、整段歷史、視窗 N＝9、10、19、20、69、70、假標記；看不見的字元寫成跳脫序列）。PR 1a 起 pytest 讀；PR 1b 起 vitest 也讀，兩邊輸出一字不差。
- 群聊對話自己的歷史不加紀錄（前端看 `GROUP_CHAT_ID`，和後端 `utils/mcp_request_identity.py` 的 `GROUP_MODEL` 同值，比對和後端分派一樣不分大小寫），假標記照換；無痕對話照加（前端組）（PR 1b）。

### 7. 心跳

- 心跳組歷史改用 `annotate_history(linear, level=RECORD_DETAILED)`。心跳在家跑時看得到網頁回合存下的步驟。心跳自己的工具步驟要到 PR 2 才會存下來。
- PR 1a 只處理心跳自己這段對話的歷史：換家時帶進來的舊家對話文字與摘要器還沒換假標記，PR 3（離開這段對話的地方）一起處理；SNS 在 PR 4。
- 後端只換字串內容：內容是多段（list）的訊息不換；前端把多模態訊息存成 JSON 字串時，假標記中間夾的換行、tab 這類字元會被跳脫成兩個字元，也比對不到。列為已知限制。

## 決定（PR 2）

### 8. 後端記下工具步驟

- 心跳、網頁文字版存檔（分頁關掉時由後端存的那一份）、SNS 寫回時，`utils/tool_step_collector.py` 讀同一條串流的工具事件（`tool_start`、核准請求與結果、`tool_result`、`tool_stream`、例外），照前端 `applyEventToTree` 的規則配對，組成和前端 `ToolStep` 同形狀的步驟（含 `errorClass`），存在助理節點的 `steps`；下一輪照第 1～7 點寫成紀錄。
- 和前端不同：核准只存結果（`approval.decision`），沒有 execId 等可以再按的欄位；這一輪結束時還在等核准的只存「等核准」狀態、不存核准資料，畫面不會出現可按的核准卡。MCP 表單互動不是工具呼叫，不存。
- 存檔上限：結果 2,000 字、工具名與參數鍵名 200 字、每個參數值 500 字、整組參數 4,000 字（不是字串的值或整組參數序列化後超過就改存截斷的 JSON 字串；截成字串前先把秘密鍵名的值遮掉，因為之後的紀錄讀不回鍵名）、每則 30 步；截斷的結尾加「…（存檔時截斷）」。沒存的步驟數記在節點的 `stepsDropped`，只供診斷（紀錄與畫面都不讀）。工具串流的輸出每接一段都截回上限。
- 帶 NaN、Infinity 的事件整個略過：模型的參數可能被 Python 解析成這種值、照樣送進串流，但存檔不接受，存了會讓整則回覆存不進去（心跳整次中止、網頁停在保存不確定、SNS 不回覆）；前端 `JSON.parse` 失敗時也是整個略過。孤立的代理字元（lone surrogate）和文字本身一樣會讓存檔失敗，是原有的限制，這批不處理。
- 同一個 call_id 已經有步驟時又收到核准請求：前端另外新增一步，後端沿用原步驟、改回等核准（現在的事件順序不會發生）。
- 心跳：一個脈衝的步驟掛在那個脈衝最後寫入的助理節點（`TURN_SPLIT_PATTERN` 切段時也一樣）；只用了工具、一個字都沒寫的脈衝也寫一則內容空白的回覆，步驟才留得下來（段數照算）。心跳的對話在畫面上因此看得到工具卡。
- 網頁文字版存檔：存步驟；「沒有文字」改成「沒有文字也沒有步驟」才略過（`no_text`）。畫面收到終態後的富版存檔照舊整個覆蓋，所以這份只在分頁關掉、畫面沒存到時留下來（修訂 ADR-0081、ADR-0302）。
- SNS：回覆連同步驟存進對話（`append_message_to_session` 的 `extra_fields`）；只用了工具、沒有文字的也存，但不往外送。
- 不在範圍：沒有畫面保存契約的舊可續傳串流（`_pump_stream_to_buffer`）照舊只存文字。
- 後端收集的步驟由 Python 解析參數：同一則在網頁（JS 讀回存檔）與心跳寫出的數字寫法可能不同（第 4 點已知差異），只差在文字、不差在內容，這批不統一。

## 沒選的做法

- **用原生 `tool_calls`／`tool` 訊息重建**：各家轉換器格式不同，Gemini 要簽章，呼叫與結果要成對，完整結果太貴；中途換廠商時也容易壞。改成在助理文字前面加一段系統整理的紀錄。
- **持久對話改由後端依存檔組、前端只管無痕**：無痕仍要前端一份，兩份寫法省不掉；改成前後端各一份、用黃金案例鎖住。
- **完整結果**（維護者決定一的丙）：讀一個檔就可能幾千 token。

## 後果

- 每一輪多送一些字（有上限）；連續聊天時有快取，心跳、隔太久或其他廠商不一定有。
- 工具結果的開頭放在「AI 說過的話」那一側；已標明是系統整理、結果是工具資料，並清理、遮蔽、截斷。中途換了別家模型時，新的廠商會看到這些摘要（以前只看到文字）。
- 心跳在家裡會看到網頁回合的結果開頭（80 字、已清理與遮蔽），這是心跳-1 要的效果，也是新的提示注入面：模型最壞會把結果開頭當成自己說過的話，所以只有 80 字、只在自己這段對話。
- PR 2 起，無人值守的心跳自己的工具結果、SNS 外人觸發的工具結果也會存下、寫進之後的紀錄（家模式的 SNS 存進家，之後擁有者的網頁回合與在家跑的心跳都看得到）。外人的原話本來就在 user 那一側，但結果開頭現在出現在助理那一側、標明是系統整理的工具資料，可信度比較高；同樣只有 80 字、已清理與遮蔽、只在詳細視窗內。
- 不改核准判斷、不多給工具、沒有新端點。

## 驗證（PR 1）

- PR 1a `tests/unit/test_tool_records.py`：黃金案例、固定值的來源（錯誤種類、時間尾註、核准結果）、超長結果仍然很快（清理先截到 `MAX_SCAN_CHARS`，時間尾註的比對不會照長度平方變慢）、超長歷史的總量上限、`annotate_node` 的順序、心跳送出的歷史帶紀錄、`sanitize_history` 不吃紀錄、照抄的偵測與警告（經 claude 特徵化測試的主迴圈）。
- PR 1b `ToukaChat/src/lib/chat/__tests__/toolRecords.test.ts`：黃金案例、超長結果仍然很快、群聊不加、附件的假標記、`errorClass` 存進步驟（之後的結果沒帶就不留舊的）、九百萬個空白夾在假標記中間、九百萬字的時間尾註那一行（兩邊同一組輸入）；`StreamRegistryProvider.toolRecords.test.tsx`：送出的訊息一般對話與無痕帶紀錄、群聊（不分大小寫）不帶；`test_tool_records.py` 加前後端常數、字元集、金鑰樣式（含 `g` 旗標）與 `GROUP_CHAT_ID` 核對。PR 1a 合併前的小量真模型實測，結果在跑完後補記。

## 驗證（PR 2）

- `tests/unit/test_tool_step_collector.py`：收集器的配對（和前端同規則）、核准只存結果並寫成「沒有執行」、等核准沒有核准資料、既有步驟的核准請求、串流與例外、MCP 表單不存、帶 NaN 的事件略過、之後的結果沒帶種類就不留舊的、存檔上限（含工具名、鍵名與整組參數）與丟掉的步驟數；心跳脈衝存步驟（切段掛在最後一則、只用工具也寫一則、參數帶 NaN 照存、沒用工具不加欄位）；網頁文字版存檔存步驟、只用工具也存、參數帶 NaN 照存、什麼都沒有照舊略過；SNS 存步驟、只用工具不往外送；`append_message_to_session` 存額外欄位、不蓋掉節點本身的欄位（`TreeMessageManager.add_new_fields`）。
- `ToolStepTracker.test.tsx`：畫面顯示後端存的步驟（核准只有結果時只顯示結果、等核准時沒有核准卡）。
