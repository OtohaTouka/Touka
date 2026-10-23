# ADR-0274：靈魂依日期查舊對話（memory_tools 的 conversations，先接舊對話庫）；無痕讀寫不到舊對話庫

- 狀態：✅ 已接受（hist-bak b；維護者 2026-10-16 核准計畫第一件：兩個來源都查、分 b、c 兩個 PR，可見範圍只看自己的，無痕一律讀不到並修漏洞；權限設定檔加一行同日同意）
- 日期：2026-10-16
- 後續修訂：[ADR-0277](0277-chat-conversation-search.md) 接上聊天對話；conversations 預設查 ui 與 legacy，明示 ui／all 已可用，來源與分頁以該 ADR 為準。
- 決策者：維護者
- 影響範圍：ToukaMemory（`tools/contracts.py` 新 `MemoryConversationsInput`；`tools/legacy_vault.py` 改寫為 `search_conversations`；`services/legacy_store.query_messages`；`tools/__init__.py`）；ToukaProxy（`tools/builtin/memory.py`、`tools/file_verbs.py`、`tools/vault_tools.py`、`middleware/layers/tool.py`、`utils/auth_mode.py`；`config/auth_policies.json(.example)` 各一行）；`scripts/native_tool_contract_baseline.json`
- 相關：ADR-0257（歷史資料界線）、ADR-0258（舊對話時間；本 ADR 接上它留下的未接入口）、ADR-0228（公開合約與 Guard #45）、ADR-0269（只用這一輪列出的工具）、ADR-0086（無痕）、ADR-0119（檔案動詞）

## 背景
1. 靈魂沒有依日期查舊對話的入口：只能 `memory_tools(action="random")` 隨機抽 3 則。`legacy_vault.access_legacy_vault` 會依日期查，但沒接到任何分發表，而且每次查都寫 stats、走備援建目錄、讀取失敗當成沒有資料、沒包歷史資料界線。
2. 無痕對話不列 `memory_tools`，但檔案動詞 `read/list/find/grep`（以及寫入家族）的 `scope` 在無痕仍有 `legacy`，唯讀類在 Manual 直接放行，所以無痕對話讀得到 `legacy/<agent>/` 的原始檔。舊的 `vault_tools` shim 在九個動詞都列出時，依 ADR-0269 的後繼規則也算列出，同樣走得到。

## 決策
1. **新動作 `memory_tools(action="conversations")`，唯讀**。參數：
   - `query`（只比對內容，空＝不篩）；
   - `date_start／date_end`：YYYY-MM-DD，台北整天，含頭含尾。格式錯或開始晚於結束一律 validation_error，不會變成不篩；
   - `role`：user|assistant；
   - `page_size`：1～20，預設 10。刻意不叫 `limit`：memory_tools 的廣告 schema 依動作合併同名欄位，只留第一個動作的限制。換名字，query 的 `limit`（預設 5）與其他既有欄位的形狀都不變；
   - `offset ≥ 0`；
   - `source`：legacy|ui|all，可省略。
   - 欄位名不撞 `EXECUTION_CONTEXT_KWARG_NAMES` 與閘道剝除的名稱。
2. **`source` 預留給 hist-bak c**：省略＝目前查得到的全部來源，結果開頭寫出實際查了哪些。這個 PR 只有舊對話庫；明確給 `ui`／`all` 回 validation_error（`param="source"`）。c 接上聊天介面的讀取器時，合約、schema 與權限那一行都不用改。
3. **只讀**：
   - 不建目錄、不寫 stats、沒有 filename 回退；
   - 資料庫以唯讀模式開啟，不存在就回「沒有符合的舊對話」；
   - 讀取失敗往上拋，閘道回 internal_error，不當成沒有資料；
   - SQL 是固定字面量（禁令 24），LIKE 萬用字元照字面比對。
4. **輸出**：
   - 界線外寫已查來源、日期範圍（Asia/Taipei）、總則數、排序、時間說明（沒有時間的舊訊息記成匯入那天）與下一頁提示；
   - 訊息包在 `<historical_data source="legacy_conversation">` 裡，每則寫台北時間、誰說的、對話標題（截 80 字）、內容開頭 300 字；
   - 有日期時舊到新，沒有日期時新到舊，同時間依訊息 ID 排。
5. **只看自己的**：舊對話庫的路徑由身分決定（`legacy/<agent_id>`），DebugAgent 也只看得到自己的，測試釘住。
6. **權限**：`auth_policies.json.example` 與被追蹤的 `auth_policies.json` 的 `memory_tools.actions` 各加 `"conversations": "read_only"`，三種模式都直接放行、心跳可用。沒有這一行就算 dangerous。
7. **無痕讀寫不到舊對話庫（加嚴）**：
   - ToolLayer 在無痕時改列一份拿掉 `legacy` 的檔案動詞 schema（共用的 schema 物件不動）；
   - 硬叫時，九個動詞的入口（`_scope_error`）與 Proxy `vault_tools` 橋（動詞與舊 shim 共用）都回 `permission_denied`（`denied_scope="legacy"`、`reason="incognito"`），記憶引擎不會被呼叫；
   - 讀取與寫入家族都擋：寫入會把無痕內容存下，edit 也能拿來反推舊內容；
   - 判斷用 `utils/auth_mode.incognito_hides_vault`（純函式，禁令 18）。

## 後果
- 靈魂可以回答「我們 10/3 聊了什麼」，但照預設設定舊對話庫多半是空的（滑動視窗預設關）；聊天介面的對話等 hist-bak c。
- Guard #45 基準更新：legacy_vault 的形狀、`search_conversations` 成為 typed consumer、vault_tools 橋讀 `_ctx`。
- `test_legacy_dates.py` 的舊入口測試改呼叫新處理函式；有日期時改成舊到新。

## 已知限制
- 舊對話內容若是多模態 JSON 字串，預覽會顯示 JSON 片段（截斷擋得住大小）。
- 關鍵字只比對內容，不比對標題。
- 廣告 schema 依動作合併同名欄位、只留第一個動作的型別限制，這是共用機制的潛在問題，列候選，這裡不修。
- 終端指令在無痕一律 Manual、每次都要人核准，不在本次範圍。
