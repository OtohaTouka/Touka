# ADR-0285：舊審查報告甲組：提示裡模型寫的文字不能偽造可信區塊

- 狀態：✅ 已實作（甲-1、甲-2、甲-3 都已實作）。維護者 2026-10-25 核准修正計畫（「照建議：1～5 都選甲」）。
- 日期：2026-10-25
- 決策者：維護者
- 修訂 ADR-0257（S1「內文先中和」：前情摘要與家對話也中和其他 prompt 區塊 tag）；修訂 ADR-0267（已知限制最後一條：看板那一行的任務 id 也改用 `safe_ref`）；修訂 ADR-0266（甲-2：決策 1「分支之間先出現者為準」與已知限制第 4 條：同名欄位的定義都保留，多種合成 `anyOf`）；修訂 ADR-0269（甲-3：決策 2「事後指派」的寫法、決策 3 與「執行器開關預設關」一節：改成預設開，結構測試 C2 縮小）。
- 相關：ADR-0092（prompt 區塊邊界）、ADR-0127～0129（Claude 提示快取、波動區塊）、禁令 18（內容消毒抽成純函式）、禁令 20。
- 影響範圍：ToukaProxy `adapters/tool_schema.py`（甲-2）；`tests/unit/test_provider_tool_schema_egress.py`（甲-2）；ToukaProxy `services/tool_executor.py`、`tests/unit/test_advertised_tool_list_security.py` 與 32 個建構執行器的測試檔（甲-3）；ToukaMemory `utils/historical_data.py`；ToukaProxy `services/continuity_service.py`、`services/soul_service.py`、`middleware/layers/context.py`；新 `tests/unit/test_mission_board_boundary_security.py`、`tests/unit/test_historical_data_boundary_security.py`；SECURITY、MEMORY_INTERFACE。

## 背景

10/15 的程式審查（Touka #89～#105）在 dev `426dceda` 上核對後，甲組（安全與信任）仍有效的幾項：

- **High**：`<continuity>` 標成「系統記下的事實」，但看板那一行直接印出 `Task.id`（`continuity_service.py:266`）。任務 id 是模型取的、沒有格式限制，可以帶換行、假的「最後一則人類訊息」或 `</continuity>`，之後每一輪（含心跳）都會當成系統事實。同檔「最近完成」那一行早已用 `safe_ref`，這一行漏掉了。
- **同一種偽造的另一個入口**（調查時發現）：`<mission_awareness>`（由 ToolLayer 注入 system）原樣放入模型寫的大目標、任務 id、描述、依賴與產出；換行可以多造一行任務，嵌入的 `</mission_awareness>` 可以提早關閉區塊。
- **歷史資料只中和自己的標籤**：前情摘要與家對話只中和 `historical_data`。摘要裡一個 `</conversation_summary>`，在 Claude 路徑上更嚴重：送出前 `reorder_system_stable_first` 用非貪婪配對把波動區塊搬到尾端，摘要提早結束，後面的文字留在穩定前綴、沒有任何界線，還會進跨回合快取。

- **送給 Claude 的外部工具，同名欄位只留第一個定義**（甲-2）：Anthropic 不收根層組合子，出場時把各分支的欄位併回根層（ADR-0266）；分支之間同名欄位定義不同時只取第一個。外部 MCP 工具把判別欄位只寫在分支裡時（例如每個分支的 `kind` 各是一個 `const`），Claude 只看得到第一種，選不到其他種；欄位型別在分支之間不同時也一樣。
- **「只用這一輪列出的工具」預設關**（甲-3）：執行器開關 `enforce_advertised` 預設關（ADR-0269 方案 (a)），靠結構測試 C2 確保正式程式只有兩個建立點、各自寫出字面值。T1 之後同一個開關還管「使用者各自授權的 MCP 要人在場」那道檢查（ADR-0281），新入口忘了開，兩道會一起失效。現在兩個正式入口都有正確設定，沒有可利用的漏洞；風險在以後新增入口時。

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

6. **分支之間同名欄位的定義都保留**：`_anthropic_input_schema` 依出現順序收集各分支同名欄位的定義、相同的只留一份；只有一種照用，多種合成 `{"anyOf": [...]}`（Anthropic 只拒收根層組合子、內層可以：ADR-0266 背景第 1 點，2026-10-15 實測）。各組合子依 `oneOf`、`anyOf`、`allOf` 的順序收集。同名以根層為準、`required` 只留根層原有的、根層一律 `object`，都不變。只影響送給 Anthropic 的那份；正本、執行前檢查、其他廠商不變。
7. **Touka 自己的工具不受影響**：mission_tools、memory_tools、edit_soul 的分支裡只有根層已經有的 `action`，根層優先，所以送給 Claude 的欄位就是正本根層的欄位，和以前相同（新測試直接比對）。以後新增帶根層組合子的原生工具，分支裡根層沒有的同名欄位定義必須一致，也有測試把關。
8. **不在這批**：Touka 自己工具的廣告 schema 依動作合併同名欄位時也是「第一個為準」（`ToukaMemory/.../tools/contracts.py` 的 `action_parameters`，HANDOVER §3），影響所有廠商、要另外處理。

## 決定（甲-3）

9. **開關預設開**：`ToolExecutor(..., *, enforce_advertised: bool = True)`，仍只能用名稱指定、存成私有屬性、對外唯讀。`run_react_loop` 的字面 `True` 與建好後的執行期檢查保留；`/debug/execute` 照舊明寫 `False`。新的入口忘了設定，也照樣核對清單與使用者授權 MCP 的在場條件。
10. **靠舊預設的測試明寫關**：32 個測試檔共 65 處建構補 `enforce_advertised=False`，語意不變（`smoke_tool_coverage.py` 在內）。
11. **結構測試 C2 改寫**：預設開以後，要守的是「誰能把它關掉、誰能換掉核對」。拿掉「正式程式只能兩個建立點、沒帶參數就紅」與 `tool_executor` 模組的匯入棘輪；加上 `inspect.signature` 釘住「只能用名稱指定、預設 True」。規則（掃全部正式 .py，執行器檔本身也掃）：
    - **`ToolExecutor` 只能被直接呼叫**（`ToolExecutor(...)`、`.ToolExecutor(...)`）：當成參數（`partial`、`setattr`、`builtins.setattr`、`type.__setattr__`、`object.__setattr__`）、基底類別（含 `*[ToolExecutor]`、條件運算式）、指派對象（改綁名稱或模組屬性）、取屬性（換掉 `__init__` 等類別屬性）、交給變數，全都算違規；直接呼叫不得有 `**` 展開（值可能來自設定或 JSON）；不得改名匯入；執行器以外不得另寫名為 `ToolExecutor` 的類別或函式。
    - 任何呼叫裡的 `enforce_advertised=` 都必須是字面布林值；字面 `False` 只准直接寫在 debug.py 模組層的 `debug_execute_tool` 裡（包在 lambda、類別本體或巢狀函式裡都不算）。
    - 整串等於 `"enforce_advertised"`、`"_enforce_advertised"`、兩個核對方法名（`_offer_denial`、`_user_scope_denial`）、`"__kwdefaults__"` 或 `"ToolExecutor"` 的字串一律禁止（擋 `**{...}`、`getattr`、`vars()[...]`、`globals()[...]`）。
    - 執行器檔的形狀：只能有一個模組層的 `class ToolExecutor`，不得巢狀或重複、不得加裝飾器；兩個核對方法只能直接定義在它身上；`__init__`、兩個核對方法與 `execute_to_queue` 不得加裝飾器；私有屬性只准在它直接定義的 `__init__` 寫入；不得指派核對方法。
    - 執行器以外：私有屬性與兩個核對方法不得以任何形式出現（名稱、屬性、keyword 名稱如 `vars(ex).update(_enforce_advertised=False)`、參數名、定義名、匯入名）。
    - 不得寫入或刪除 `.enforce_advertised`；不得碰 `__kwdefaults__`（擋執行時把預設值翻成 False）。
    C3（清單欄位的讀寫者）不動。
12. **事後指派的寫法更正**：ADR-0269 決策 2 說清單欄位「dict、frozenset 一律拒絕」，那是建立時。建好之後不論用 `ctx["advertised_tools"] = …`（`PipelineContext.__setitem__` 對宣告過的欄位就是 `setattr`，正式寫入點 ToolLayer 用的就是這個）還是屬性指派都不驗證（沒開 `validate_assignment`，開了會牽動 26 處正式程式與 56 處測試），由執行器的 `isinstance` 擋下、當成清單不可用。既有的 B11 dict、frozenset 案例本來就在測這條；這次另加一個屬性指派的案例當說明。

## 後果

- 使用者畫面沒有差別。模型與 context 快照畫面會看到 `&lt;` 與 `⏎`。
- 一般的任務 id、描述（含 `<b>` 這類非區塊標籤）照原樣顯示。
- 已知限制：
  - ToukaDispatch 建任務不經 Proxy 的工具合約（`ToukaDispatch/server.py:358-391`），id 照樣不限格式；顯示端已處理，所以不會進到可信區塊。
  - 看板載入失敗會被吞成「沒有看板」（`mission_manager.py:108-117`），這批不改。
  - 只有模型看得出來的變體沒有中和：`<` 後面夾零寬字元（U+200B、U+2060 等）、全形 `＜／＞`、`&#60;` 這類 HTML 實體，以及 NFKC 正規化後才變成 tag 的寫法。Touka 自己的解析（`parse_system_blocks`、`reorder_system_stable_first`、`to_cached_system`）只認精確的 `<tag>`，不受影響；剩下的是模型自己會不會誤讀。
  - 舊紀錄裡刻意用 JSON 跳脫寫出單獨的代理字元（U+D800～U+DFFF）時，解開後的文字無法編碼成 UTF-8，那次請求會失敗；經轉換的路徑本來就是這樣，這次原生 Anthropic 入口的中間幾則也一樣。
  - id 含換行、控制字元或區塊 tag 時，看板顯示的是跳脫後的樣子，模型照抄去呼叫 mission 工具會對不上；這類 id 本來就是異常輸入。
  - 甲-2：分支本身是 `$ref`，或分支裡再巢狀組合子時，欄位仍收不到（ADR-0266 起就這樣）。
  - 甲-2：`allOf` 分支的同名欄位本意是「都要符合」，這裡一樣合成 `anyOf`，給模型的提示比正本寬，送錯時靠執行前檢查擋下。
  - 甲-2：去重用 Python 的 `==`，`true` 和 `1`、`false` 和 `0` 會被當成同一個值、只留先出現的（JSON Schema 裡是不同的值；實務上罕見）。
  - 甲-2：只差在說明文字（`description`、`title`、`examples`）的定義也算不同，會合成 `anyOf`；Anthropic 收，兩種說明模型都看得到，所以不另外合併說明（grok 審查的建議，判斷不需要）。
  - 範圍外的觀察（審查時發現、不是這次造成）：`<harness_context>` 的固定文字裡就有 `<conversation_summary>` 字樣，`to_cached_system` 用 `find` 找最早的波動區塊時會在 harness 中間切開，`tool_availability`、`validation_protocol` 被留在波動段，影響的是快取命中、不是安全（ADR-0139 起就有），記在 HANDOVER §3。
- 甲-2：外部工具（MCP 工具，或請求自己帶來的工具）把欄位直接寫在分支的 `properties` 裡時，Claude 看得到每一種定義；只有一種定義的欄位形狀不變。Claude 仍看不到「哪個值要配哪些欄位」（條件必填只剩說明文字，ADR-0266 已知限制），送錯組合時靠執行前檢查擋下。
- 甲-3：使用者感覺不到差別。新寫的執行器建立點不寫開關也會核對清單；常見的關開關與換掉核對的寫法（自我測試列了 65 種）都會被 C2 抓到，要關只該在人按的除錯執行。
  - 已知限制（甲-3）：C2 是只讀原始碼的結構測試，有心要繞還是繞得過，例如：
    - 用算出來的字串（`"_enforce" + "_advertised"`、`"Tool" + "Executor"`）經 `setattr`／`getattr` 改私有屬性或拿到類別（ADR-0269 起就這樣）；
    - 先把類別交給變數（或在執行器裡用 classmethod 的 `cls`），再把鍵名是算出來的 dict 用 `**` 展開。
- 撤回：甲-1、甲-2、甲-3 各自 revert 即恢復原樣；本 ADR 檔保留，在狀態註記撤回（後續批次會引用它）。

## 驗證

- 先寫的測試在照原樣回傳的空殼上 8 個紅（看板偽造、`<mission_awareness>` 關閉與多造行、摘要與家對話關閉或另開區塊、摘要經重排後注入文字留在穩定前綴），實作後全綠；一般 id 與文字原樣的案例修前修後都綠。
- 程式審查有兩位：內部子代理，以及 xAI `grok-4.7`（經 pi，只能讀檔；維護者同意送出那一次，送 12 個公開 repo 的檔，花費 US$0.72）。兩位都抓到家對話 JSON 跳脫的繞過；內部審查另外抓到長空白的平方時間。修正後新增的測試在修正前的版本上 10 個紅（JSON 跳脫 2、巢狀內容 1、控制字元 4、分隔字元 3），長空白的案例在修正前的版本超過 300 秒；每個區塊 tag 各一個案例與看板控制字元順序的案例是防回歸用，修正前後都綠。
- 突變檢查 27 個全部變紅（含 possessive 量詞改回一般量詞時逾時）。
- 甲-2 先寫的測試修正前 3 個紅：欄位只在分支裡的 MCP 工具（同名欄位型別不同）、判別欄位只在分支裡（各分支的 `const` 都要選得到、相同定義只留一份、依出現順序）、合成的 `anyOf` 是新建的不改到輸入；「Touka 自己工具的分支沒有衝突」修正前後都綠。變異檢查 6 個全部變紅（又只留第一個、相同的不去重、只有一種也包 `anyOf`、順序反過來、分支蓋過根層、只看 `oneOf`）。
- 甲-2 內部程式審查第 1 輪（通過，附意見）：「相同的只留一份」只對第一個定義有測到、跨組合子與怪輸入沒有測試、原生工具那個測試其實沒在比任何欄位；文件有幾處說得過頭。補了第 4 個分支（重複的是後面的定義）、跨組合子的順序、6 種怪輸入（含不能迭代的分支清單）不拋例外也不改輸入、原生工具送給 Claude 的欄位直接等於正本根層；審查者兩輪建議的 6 個突變加 1 個對照組都變紅。第 2 輪通過。
- 甲-2 另送 xAI `grok-4.7` 審查（經 pi，只能讀檔；維護者同意送出那一次，送 8 個公開 repo 的檔加審查說明，花費 US$0.34），通過，附 1 個建議與 3 個 LOW：怪輸入的測試只驗不拋例外，改成每種都比對完整輸出（新的 3 個突變都變紅）；ADR-0266 正文改成以現行規則為主句；只差說明文字也合成 `anyOf` 記為已知限制、不改；自我參照的 dict 比較時會拋 `RecursionError`，工具定義來自 JSON、不會自我參照，不改。
- 甲-3 先寫的測試修正前 2 個紅：開關的預設值與「只能用名稱指定」（`inspect.signature`）、不寫開關的執行器要擋清單外工具。縮小後的 C2 掃描器自我測試 14 種關掉開關的寫法都抓到、6 個反例（docstring、兩個正式入口、其他地方不寫開關或明寫 `True`、讀屬性）都放行；正式程式掃描乾淨。B11 的事後屬性指派案例修正前後都綠（釘住既有行為）。65 處補 `False` 後，改到的 32 個測試檔共 1209 個測試全過，`smoke_tool_coverage.py` 通過。
- 甲-3 變異檢查 11 個全部變紅：預設改回關、debug 不寫關，以及掃描器的 9 條規則各拿掉或放寬一次（不擋開關名字串、私有名稱、私有屬性、屬性寫入、非字面值、哪裡都能寫 False、debug.py 任何函式都能寫 False、連 True 也擋、`services/` 底下都略過）。
- 甲-3 內部程式審查第 1 輪（需修改；沒有 BLOCKING、HIGH）：
  - MEDIUM：縮小後的 C2 整個略過執行器檔，在 `ToolExecutor` 裡加一個關掉開關的工廠方法抓不到；`_enforce_advertised` 當成 keyword 名稱（`vars(ex).update(...)`、`ex.__dict__.update(...)`）也抓不到（舊 C2 抓得到）。
  - LOW：拿掉「`ToolExecutor` 只能出現在三個檔」之後，子類別或 monkeypatch 能換掉兩個核對方法；建立呼叫的 `**` 展開與 `__kwdefaults__`；B11 新案例和既有的 dict 案例是同一條路（文件說法更正）；ADR-0285 說得太滿、已知限制沒列全；ADR-0281 還拿已拿掉的匯入棘輪當理由。
  - 處理：執行器檔也掃（私有屬性只准在 `__init__` 寫）；私有名稱與兩個核對方法在執行器以外不得以任何形式出現；建立呼叫不得 `**`、不得改名匯入；不得碰 `__kwdefaults__`；文件照實改寫，已知限制列出算出來的字串與經變數轉手。審查者建議的 6 個繞法（執行器裡兩種工廠、debug 用 `vars().update`、新檔子類別覆寫核對方法、新檔 `**` 展開、新檔改 `__kwdefaults__`）真的寫進正式程式後都被 C2 抓到。
  - 掃描器的 19 條規則各拿掉或放寬一次（含預設改回關、debug 不寫關、執行器檔不掃），全部變紅；其中「不擋開關名字串」第一次存活（原本的案例改被 `**` 規則先抓到），補了「類別交給變數再 `**` 展開字面 dict」的案例後也變紅。
- 甲-3 內部程式審查第 2 輪（只看差異；需修改，沒有 BLOCKING、HIGH）：
  - MEDIUM：私有屬性的寫入只看最內層函式是不是 `__init__`，執行器檔又不套用定義名規則：在執行器檔裡另寫一個子類別，在它的 `__init__` 寫 `False` 或覆寫核對方法，再從別的檔匯入使用，C2 抓不到。
  - LOW：`partial(ToolExecutor, **…)` 與子類別轉手的 `**`；用字串寫的 `__kwdefaults__`（`getattr(..., "__kwdefaults__")`）。
  - 處理：正式程式（含執行器檔）不得繼承 `ToolExecutor`；私有屬性只准在 `ToolExecutor.__init__` 寫；執行器檔裡也不得指派核對方法；把 `ToolExecutor` 交給別的呼叫時也不得帶 `**`；`"__kwdefaults__"` 加進禁止的字串；收回「字面寫法只有除錯執行過得了」的說法。審查者建議的 5 個繞法真的寫進正式程式後都被抓到，新的 5 條規則各拿掉一次都變紅。
- 甲-3 內部程式審查第 3 輪（只看差異；通過，附意見）：第 2 輪的三則都已處理，「34 種」也核對過。附的意見都已照建議改：
  - LOW：從別的檔替換 `ToolExecutor` 的類別屬性（例如用 `partialmethod` 換掉 `__init__`）抓不到。改成執行器以外替換、刪除類別屬性與 `setattr`／`delattr` 都算違規。
  - 建議：在別的方法裡寫巢狀的 `__init__` 能寫私有屬性。改成只認 `class ToolExecutor` 直接定義的 `__init__`。
  - 建議：已知限制裡「子類別覆寫 property」那一則在禁止繼承後已經沒有作用，拿掉。
  - 審查者建議的 2 個繞法真的寫進正式程式後都被抓到，新的 3 條規則各拿掉一次都變紅。
- 甲-3 另送 xAI `grok-4.7` 審查（經 pi，只能讀檔；維護者同意送出那一次，送 7 個公開 repo 的檔加審查說明，內部審查三輪通過後的版本 `d57629d4`，花費 US$0.52），需修改，5 個 MEDIUM、2 個 LOW，都是字面寫得出來的繞法：
  - `builtins.setattr`／`type.__setattr__`／`object.__setattr__(ToolExecutor, "execute_to_queue", f)` 換掉執行方法；執行器檔裡 `ToolExecutor.execute_to_queue = …`。
  - 基底寫成 `*[ToolExecutor]` 或條件運算式的子類別，覆寫核對方法。
  - 執行器檔裡工廠函式內另一個同名的 `class ToolExecutor` 寫私有屬性。
  - 在核對方法上加裝飾器把它變成空的。
  - 改綁名稱 `ToolExecutor = _Open`、`te.ToolExecutor = _Open`、`globals()["ToolExecutor"]`。
  - LOW：`debug_execute_tool` 裡的 lambda 或類別本體帶字面 `False`；ADR-0269 決策 3 的簽名還寫 `= False`。
  - 處理：不再逐種補洞，改成「`ToolExecutor` 只能被直接呼叫」一條規則涵蓋所有轉手、改綁與換掉類別屬性的寫法（舊 C2 也有類似規則）；執行器檔的形狀定死（唯一的模組層類別、核對方法只定義在它身上、關鍵方法不加裝飾器）；`False` 只准直接寫在模組層 `debug_execute_tool`；`"ToolExecutor"` 加進禁止字串；ADR-0269 簽名改正。正式程式掃描乾淨（正式程式只有 react_core 與 debug 匯入並直接呼叫 `ToolExecutor`）。
  - grok 的 7 個繞法真的寫進正式程式後，只跑正式程式掃描那一個測試就都被抓到；改寫後的 26 條規則各拿掉或放寬一次全部變紅（其中 2 條第一次存活，因為既有案例先被新規則抓到，各補一個只有那條規則抓得到的案例後也變紅）。自我測試 65 種要抓到、7 個反例。
