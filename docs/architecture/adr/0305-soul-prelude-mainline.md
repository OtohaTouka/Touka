# ADR-0305：靈魂前情與主線

- 狀態：🚧 部分實作（PR 1：前情、主線組法、查對話去重、設定）。換家交接在 PR 2a、2b，畫面在 PR 3，各自補進本 ADR。一頁計畫由維護者核准（路線圖 3d，2026-11-07），計畫經五輪審查；PR 1 的第 1 輪審查（DEV-B 與三位對抗式審查）與第 2 輪審查（DEV-B 與對抗式各一位）找到的問題已修正並寫進下面各條（標「審查」的部分）。
- 日期：2026-11-07
- 決策者：維護者
- 相關：ADR-0092（滑動視窗剪裁；本 ADR 修訂 Phase D 摘要寫到哪裡）、ADR-0264（摘要時間範圍、剪下的對話歸檔；本 ADR 修訂剪裁摘要的去處）、ADR-0277（跨來源查對話；本 ADR 修訂合併查詢的計數）、ADR-0257（歷史資料界線）、ADR-0260（家對話外送白名單）、ADR-0267（活動時間軸）、ADR-0276（對話歸屬）、ADR-0301（家標記不算版本）、ADR-0302（聊天側剪裁）、ADR-0244（`complete_once`）、ADR-0271（用途少想）、ADR-0214（禁令 61）。
- 影響範圍：`ToukaProxy/src/utils/soul_prelude_db.py`・`src/utils/async_db.py`・`src/services/soul_prelude_service.py`・`src/services/soul_prelude_merge.py`・`src/services/mainline_service.py`・`src/services/sliding_window_service.py`・`src/services/sliding_window_config.py`・`src/services/dialogue_summarizer.py`・`src/services/soul_service.py`・`src/middleware/layers/soul.py`・`src/services/dispatchers/group.py`・`src/routers/heartbeat.py`・`src/routers/ui_sessions.py`・`src/services/prompt_blocks.py`・`src/services/context_snapshot_service.py`・`src/services/ui_conversation_reader.py`・`src/models/context.py`・`src/utils/token_estimator.py`・`src/services/agent_reset/`・`src/routers/souls.py`・`src/services/gdpr_export_service.py`・`src/models/sliding_window.py`＋`ToukaMemory/src/memory_server/models/conversation_search.py`・`services/legacy_store.py`・`tools/legacy_vault.py`・`protocols.py`＋`ToukaChat/src/components/settings/tabs/SlidingWindowTab.tsx`・`components/settings/tabs/slidingWindow/SummarySettings.tsx`・`hooks/settings/useKeyedModels.ts`・`hooks/settings/useAutoReviewSettings.ts`・`hooks/settings/useSlidingWindowSettings.ts`・`components/chat/ContextMeter.tsx`・`lib/types.ts`＋`scripts/guard_harness_context.py`。

## 背景

以前一個靈魂的「前面發生過什麼」散在各處：家對話剪裁時，摘要整份替換進那段對話的 `context_summary`；子對話看得到家的原文，看不到家的摘要；心跳不帶摘要；換家時什麼都不做，舊家的摘要就此不見。Owner 2026-11-06 定下方向：一個靈魂只有一條主線，主線＝累積的前情＋現任家，子對話一律看現在的主線。

本 ADR 先做主線的第一部分：前情怎麼存、怎麼組、誰看得到。換家時舊家原文進舊對話庫、摘要併進前情、舊家唯讀、子對話寫回報，留給 PR 2a、2b。

## 決定

1. **前情資料表 `soul_prelude_segments`**（`ui_sessions.db`，和對話同一區）。
   - 每列一段：靈魂（正式 ID）、範圍版本、種類、來源對話、`dedupe_key`、涵蓋起訖、內容、建立時間、`merged_into`。
   - 種類：`home_trim`（剪裁）、`home_retired`（退任摘要）、`child_report`（子對話回報）、`handover`（交接）、`merged`（合併）、`legacy_summary`（升級前留下的舊摘要）。PR 1 只寫 `home_trim`、`legacy_summary`、`merged`。
   - 部分唯一索引（靈魂、種類、來源、`dedupe_key`），只管範圍有效的列（`WHERE scope_version = 1`），一律 `INSERT OR IGNORE`。失效的列不佔位置：失效之後重寫同一鍵、再失效一次也不衝突（審查：原本的索引含範圍版本，第二次失效會撞索引，整筆還原退回）。舊版的索引啟動時移除。`dedupe_key` 不用空值：剪裁＝來源對話與剪到的最後一則編號；舊摘要＝內容的雜湊（版本含每次啟動隨機的值，不能用）。
   - 合併只標 `merged_into`，不刪來源段落。
   - 同名重建靈魂或還原備份時，和對話一樣把範圍版本設成 0，舊前情不再出現（`invalidate_agent_sessions`、`invalidate_restored_sessions`）。搬過又被失效的舊摘要（同一來源、同一雜湊已有失效的列，`was_invalidated`）不再搬回來。對話本身的範圍版本不拿來判斷：ADR-0276 的舊列不回填、換綁降成 0、設家也不升回 1，0 不等於失效（審查第 2 輪：原本用「對話範圍版本不是 1」判斷，升級前的家與還原後的家舊摘要永遠搬不進來）。
   - 計數、清除、列出、寫入都用正式 ID（別名換成註冊名）。
2. **剪裁寫前情，不再改 `context_summary`**。
   - `summary_budget_pct > 0` 時寫摘要，帶前情顯示順序的最後一段給摘要員避免重複（截到 2,000 token）；否則寫一行來源註記（剪了幾則、原文在舊對話庫）。摘要長度不超過 `prelude_max_tokens` 的一半（和合併保留的額度一致，審查第 2 輪）。
   - 送給摘要員的每則訊息壓成一行（換行寫成 `⏎`），外人不能在訊息裡假造另一則；附給摘要員的前情最後一段也壓成一行（審查第 2 輪）。指示說明以 SNS 外部訊息開頭的是外部人士的要求，不記成擁有者的決定或待辦（審查）。
   - 靈魂正在重置或刪除時不剪：一開始就記下重置的代數，歸檔進舊對話庫之前再看一次（重置中不寫舊對話庫），寫段落時在交易裡再看一次，期間開始的退回整筆（樹也不剪）；這種退回記 info，不記成 error（審查第 2 輪）。
   - 段落和剪過的樹在同一個交易寫入（`conditional_save_session(after_write=...)`）：存回被擋就沒有段落；段落寫不進去，樹也退回。
   - 畫面不顯示 `context_summary`，存檔時照舊原樣送回；後端不再改它，所以 ADR-0302 的接法不受影響。
   - 剪掉的對話太長時分段摘要（單一則超過一段的上限時截到估算不超過上限）：每次請求連同前情最後一段與指示不超過 8 萬 token（`SUMMARY_REQUEST_TOKEN_CAP`，離 10 萬的價格門檻留兩成），每段 6 萬，全部最多 50 萬、最多 3 段同時跑；超過全部上限時從最新往前取，沒取到的寫一行註記。任一段失敗就整段不寫摘要（退回來源註記）。
3. **舊摘要搬家**：某段對話成為家時（設家），換家時的舊家，以及現任家第一次讀前情時，把它的 `context_summary` 搬成 `legacy_summary` 段落。內容雜湊當 `dedupe_key`，重複搬只會寫一次。畫面能寫這欄，所以搬家時超過 2,000 token 就截短，註明後面截掉了、被摘要的原文在舊對話庫；同一來源只顯示最新一段舊摘要。寫入在 `BEGIN IMMEDIATE` 交易裡再看一次靈魂有沒有開始重置（審查第 2 輪）。上下文預覽不搬（預覽整個流程不寫資料，`PipelineContext.preview`）。
   - 對話自己的 `<conversation_summary>`（家與子對話一樣）只在這一輪前情原文照登同一段時不插（`summaries_shown_for`）；截短過、被折掉、被合併時照插，模型總看得到一份（審查第 2 輪：原本家一律不插，舊摘要搬不進前情或被折掉時模型就完全看不到）。只在 `inject_context_summary` 讀一次對話，和升級前一樣。
4. **組前情**（`prelude_view` 決定這一輪顯示哪幾段、折掉幾段，`render_prelude` 組成區塊）。
   - 依寫入順序排（舊摘要排最前面）；訊息時間可能沒有或比寫入早，只供標頭顯示，不拿來排序。同一來源的退任摘要與舊摘要只顯示最新一段。來源是現任家的退任摘要與子對話回報不顯示；剪裁段落與舊摘要不管來源都顯示（原文已經不在家裡）。
   - 依 `prelude_max_tokens`（預設 4,000）與最多 50 段取最新的，最新一段一定顯示，單段就超過上限時截短那一段（只影響送出的樣子）；更早的折成一行「更早還有 N 段，原文在舊對話庫」。
   - 包在 `<soul_prelude>` 區塊裡，內文經 `blank_control_chars` 與 `wrap_historical_data`（和舊摘要同一套歷史資料界線，ADR-0257）。區塊加進 `ALL_BLOCK_TAGS`，不列入 `VOLATILE_SYSTEM_TAGS`，跟著穩定前綴快取。
   - `<harness_context>` 告訴模型「長對話較早的部分剪進 `<soul_prelude>`，是濃縮不是遺失」；Guard #40 改綁剪裁寫段落與 SoulLayer 注入主線兩處。
5. **合併**（`soul_prelude_merge`）：前情摘要開著、可見段落超過上限時，最新的留到上限的一半，其餘可合併的合成一段 `merged`，一次摘要呼叫，輸入不超過 8 萬 token。
   - 能合併的：剪裁摘要、子對話回報、交接、舊摘要、之前的合併段；退任摘要與只有一行的來源註記不合併（註記留給折疊）。能合併的少於兩段就不合併。
   - 輸出上限＝前情上限扣掉全部不合併的可見段落（保留的、夾在中間的註記與退任摘要）與 50 token 餘量，最多 2,000；不到 200 就不合併。指示寫明長度；拿到結果後再用本機估算截到上限內（截在換行處），模型的 token 比估算少（英文、本機模型）時也放得進前情（審查：原本固定 2,000，上限小時合併段永遠放不進去、每次剪裁又付費合併；第 2 輪補上扣掉夾在中間的段落與估算截短）。
   - 送去合併的每段壓成一行，指示說明每段一行（審查第 2 輪）。
   - 合併段的寫入時間取被合併段落中最新的，但不晚於「被合併段落之後第一段非舊摘要」的寫入時間減 1（舊摘要可能比剪裁晚寫入，審查第 2 輪），排在它取代的位置；涵蓋時間只取有時間的來源。
   - 同一來源被取代、沒顯示的舊版舊摘要一起標成已合併，合併後不會重新出現（審查第 2 輪）。
   - 在禁令 28 的兩個觸發點剪完之後在背景做（`spawn_logged`，剪裁不等它），在剪裁的對話鎖外面，拿靈魂鎖。
   - 重置或刪除中不做，摘要期間開始的也丟掉結果（`destructive_ops`）。只標它讀到的那幾筆；任何失敗都留著原本的段落，同一個靈魂冷卻 6 小時再試（記在記憶體，重啟就清掉）。
6. **主線只有一個組法**（`inject_mainline`），SoulLayer、群聊、心跳都呼叫它：

   | 誰送出 | 前情 | 家原文 |
   |---|---|---|
   | 網頁版家本身（送完整紀錄） | 帶 | 不帶 |
   | 家，但 IDE、SNS 家模式（只送最新一句） | 帶 | 帶 |
   | 子對話（網頁、IDE；帶對話編號、不是家） | 帶 | 帶，每則內容前標時間（寫在內容裡，符合 ADR-0260） |
   | 沒有對話編號的請求 | 帶 | 帶，不標時間 |
   | 群聊 | 帶 | 照舊帶 |
   | 心跳（在家或子對話） | 帶 | 不帶 |
   | 無痕 | 不帶 | 不帶 |

   每輪只讀一次家，前情與家原文用同一份。讀家碰到儲存錯誤時前情照樣組，家原文交回原本的讀法，和以前一樣讓這一輪失敗（ADR-0078 失效不沉默）；心跳以前不讀家，照樣帶前情、不帶家原文（審查第 2 輪：原本當作沒有家照樣送出，IDE、SNS 只送最新一句時模型一點歷史都沒有）。兩個家等範圍矛盾當作沒有家。前情讀不到時照樣送出這一輪（記 warning 與原因）。
7. **查對話去重**：合併查詢（`source="all"`）時，聊天對話裡已有的同一則不再從舊對話庫算一次。
   - UI 讀取器回傳符合文字與角色的全部訊息鍵（`對話編號\x1f訊息編號`），不套日期條件（聊天對話裡沒有時間的訊息，在舊對話庫那份的時間是歸檔當下）。
   - 舊對話庫的列表、總數、未知時間數都用一個 JSON 參數排除（`json_each`，結構固定，禁令 24）。版本列（`~v`）內容不同，保留。只查舊對話庫時照實全列。
8. **設定**（`sliding_window.json`）：
   - `prelude_summary_enabled`（預設開）：關掉時前情超過上限也不合併（PR 2b 起，退任摘要與子對話回報也只留來源註記）。剪裁要不要摘要照舊只看 `summary_budget_pct`。
   - `prelude_max_tokens`（500～20,000，預設 4,000）。
   - `summary_model` 預設改成 `claude-haiku-5-5`。既有設定檔不動，要換到設定頁選。摘要員用途是 `summarizer`，照 ADR-0271 少想（Haiku 5.5：effort low、思考關閉）。
   - 設定頁加摘要模型選單（只列已設定金鑰的模型，和 Auto 審查員共用 `useKeyedModels`；目前的模型不在清單裡時照樣列出並提醒，提醒寫明不需要金鑰的本機模型可以忽略）與前情摘要開關，兩個都不受滑動視窗開關影響。上下文用量面板的前情區塊顯示「前情」。
9. **生命週期**：前情也算記憶。
   - 靈魂資料重置會清掉（`agent_reset` 的 `ALL_STORES` 加 `StorePurger("prelude")`，在對話之後；DebugAgent 的診斷重置也走這裡）。
   - 刪除靈魂前還有前情就擋（409 `SOUL_MEMORY_NOT_RESET`，帶 `prelude_segments`）；讀不到就 503（`SOUL_MEMORY_CHECK_FAILED`），不能確認就不刪。
   - 個人資料匯出多 `agents/<id>/soul_prelude.json`（全部的列，含已合併、已失效的，帶範圍版本欄位，和對話匯出一致；審查第 2 輪：原本只帶有效列，還原後擁有者自己的前情全不在匯出裡）；讀不到列進 skipped（`soul_prelude_unavailable`）。

## 取捨與影響

- 每次送給主模型多帶前情，最多約 4,000 token，在可快取的位置。前情沒變時算快取價。
- 剪裁摘要開著時，每次剪裁一次摘要呼叫；前情超過上限時多一次合併呼叫。都用摘要模型。
- 子對話、心跳、群聊都看得到同一份前情，包含家裡剪掉的內容摘要。這本來就是主線的目的。
- 升級後現任家第一次讀前情時，舊的 `context_summary` 搬成一段（太長時截短）。
- 前情摘要關閉、剪裁也不摘要時，前情只是一串來源註記；超過 50 段只顯示最新的。
- 合併查詢時，排除清單可能有幾萬筆、幾 MB 的 JSON 參數，可以接受。
- 多段同時摘要時，事前的花費估算各自判斷，加總可能稍微超過上限。
- SNS 頻道與 SNS 群聊也帶前情：家原文以前就會送進去，PR 1 新多的是剪掉部分的摘要。維護者 2026-11-07 決定 PR 1 照樣帶。PR 2b 加子對話回報後，私人子對話的摘要也會進去，要不要讓回報進 SNS 在 PR 2b 前再問。

### 已知限制

- 查對話去重只比「對話＋訊息編號」：歸檔後被編輯過的訊息，合併查詢會藏起舊對話庫那份。少見；PR 2b 整棵歸檔前再看。
- 長得像區塊標籤的寫法（零寬字元、全形括號）沒有中和，沿用 ADR-0257 的做法；記在 SECURITY 的殘留風險。
- 分段摘要每段的輸出小，任一段被截斷整份就失敗、退回來源註記；多段摘要期間拿著剪裁的對話鎖較久。之後看實際頻率。
- 前情標頭用對話編號，查對話工具目前不顯示編號；PR 3 畫面與工具一起看。
- 既有資料出現兩個家時，兩個家的舊摘要都不搬，各自只在那段對話裡插（家原文那邊本來就記 warning）。
- 合併失敗的冷卻記在記憶體，重置或同名重建後還留著，最多晚 6 小時才合併。
- 還原時剛好有背景合併提交：隨後全部列失效，不留可見錯誤。

## 驗證

- pytest：`test_soul_prelude_db.py`（資料表、部分唯一索引與失效兩次、合併只標讀到的、失效、找得到失效過的列、寫入鎖內再檢查、匯出全部列與範圍版本、正式 ID）、`test_soul_prelude_service.py`（顯示規則、寫入順序、上限與折疊、單段截短、界線、舊摘要搬家與截短、範圍版本 0 的家照搬、搬過又失效不回來、合併的輸出上限與估算截短、合併段的順序、舊版一起標已合併、合併各段一行、冷卻、不碰註記、重置中不合併）、`test_prelude_trim.py`（剪裁同一交易寫段落、被擋不寫、段落失敗樹退回、摘要帶顯示順序最後一段、摘要最多上限的一半、背景合併、重置中不剪、讀完才重置不歸檔、退回不記 error）、`test_mainline_service.py`（主線表每一列、預覽不寫、舊家摘要不重複、家的舊摘要搬不進或被折掉時照插、截短的在自己的對話給全文、只讀一次家、讀家儲存錯誤這一輪失敗、心跳照帶前情、前情讀不到照送、設家時搬舊摘要）、`test_mainline_callers.py`（心跳與群聊接對主線）、`test_dialogue_summarizer.py`（分段、上限、任一段失敗、一則一行、外人訊息、前情最後一段截短且一行、合併指示說明一行、長度提示）、`test_conversations_merge.py`（合併查詢去重、去重後換頁、只查舊對話庫照列）、`test_reset_primitives.py`、`test_soul_delete_memory_guard.py`、`test_gdpr_export.py`（生命週期）、`test_harness_context.py`（Guard #40 新綁定）。
- 少想參數沿用既有測試：`test_complete_once_thinking.py`（`summarizer`→`minimal`）、`test_model_rules.py`（Haiku 5.5 `minimal`→思考關閉、effort low）。
- 自動化測試一律用假的摘要員（禁令 61）。
