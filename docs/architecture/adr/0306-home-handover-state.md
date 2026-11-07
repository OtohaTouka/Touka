# ADR-0306：換家的狀態與舊家唯讀

- 狀態：🚧 部分實作（PR 2a：交接紀錄、設家狀態機、舊家唯讀與刪除、交接前的主線）。交接工作本身（舊家歸檔、退任摘要、子對話回報、換家標記）在 PR 2b，畫面在 PR 3，各自補進本 ADR。一頁計畫由維護者核准（路線圖 3d，2026-11-07），計畫經五輪審查。
- 日期：2026-11-08
- 決策者：維護者
- 相關：ADR-0305（靈魂前情與主線；本 ADR 接著做換家）、ADR-0301（家標記不算版本；本 ADR 修訂決定 2 的「心跳跑到一半換家」）、ADR-0092（滑動視窗剪裁）、ADR-0257（歷史資料界線）、ADR-0260（家對話外送白名單）、ADR-0264（剪下的對話歸檔）、ADR-0267（活動時間軸的生命週期前例）、ADR-0276（對話歸屬與範圍版本）、ADR-0277（跨來源查對話）、ADR-0302（聊天側剪裁）、ADR-0078（失效不沉默）、ADR-0231（對話保存的版本）。
- 影響範圍：`ToukaProxy/src/utils/home_transitions.py`・`src/utils/async_db.py`・`src/services/proxy_service.py`・`src/services/mainline_service.py`・`src/services/soul_service.py`・`src/routers/heartbeat.py`・`src/routers/ui_sessions.py`・`src/routers/ui_interjections.py`・`src/services/agent_reset/`・`src/services/gdpr_export_service.py`・`src/services/context_snapshot_service.py`＋`ToukaProxy/config/errors.json.example`＋`ToukaChat/src/lib/error_codes.ts`・`src/locales/`。

## 背景

以前換家只清家標記（`async_db.set_home`），舊家變成普通子對話：可以繼續聊、可以刪、可以換靈魂，它的內容就此離開主線。Owner 2026-11-06 定下的方向是「換家＝搬家」：舊家原文進舊對話庫、摘要併進前情、舊家唯讀；新家第一次被使用才真正交接，之前改回去當沒換過。

PR 2a 先做換家的狀態：記下誰換到誰、舊家從此唯讀、交接完成前不能刪。交接工作本身留給 PR 2b；2a 合併後到 2b 合併前，舊家原文照樣帶在主線裡，靈魂接得上。

## 決定

1. **兩張新表**（`ui_sessions.db`，和對話同一區，`async_db.init_db_async` 呼叫 `home_transitions.create_tables` 建立）。
   - `soul_home_transitions`：每次換家、取消家記一筆：編號、靈魂（正式 ID）、範圍版本、舊家、新家（可空）、新家已開始用、狀態（待交接／交接中／完成／取消／失敗）、做到第幾步、失敗次數、退任摘要待補、「舊家已由第 n 筆交接」、時間（epoch 毫秒）。
   - `soul_child_report_marks`：子對話的回報寫到哪（對話、靈魂、範圍版本、目前節點）。2a 只建表；升級那次（表這次才建）把既有子對話的位置設在當下內容，不補寫歷史，之後新建的子對話沒有位置（全部算新內容）。2b 寫回報時用。
   - 失效同前情：同名重建與還原備份把範圍版本改成 0（`invalidate_agent_sessions`、`invalidate_restored_sessions`）。失效的紀錄不處理、不算舊家、不擋刪除，舊靈魂或不可信的還原對話不會被摘要進前情（ADR-0276、0277）。
   - 所有 SQL 是固定字面量（禁令 24）。
2. **設家、取消家照狀態對照表改紀錄**，和家標記在同一個交易（`home_transitions.apply_home_change`；設家不拿靈魂鎖）：

   | 現在 | 動作 | 結果 |
   |---|---|---|
   | 家 A，沒有紀錄 | 設 B | A 退任；新紀錄 A→B（還沒開始用） |
   | A→B，B 還沒開始用 | 設 A | 取消 A→B；A 恢復成家（當沒換過） |
   | A→B，B 還沒開始用 | 設 C | 紀錄改成 A→C；B 回到普通子對話 |
   | A→B，B 已開始用（待交接或交接中） | 設 A | A 恢復成家；A→B 照樣要做，另加 B→A |
   | A→B，B 已開始用 | 設 C | A→B 照樣要做，另加 B→C |
   | 家 A | 取消家 | 紀錄 A→（無） |
   | A→B，B 還沒開始用 | 取消家 | 紀錄改成 A→（無） |
   | 不是家的那段 | 取消家 | 什麼都不做（以前對任何一段按取消家都會清掉家） |
   | A→（無），還沒開始交接 | 設 D（含心跳建家） | 紀錄改成 A→D（還沒開始用）；設回 A 則取消 |
   | A→（無），交接中 | 設 D | 進行中的不動；另加 A→D，標「舊家已由第 n 筆交接」（2b 只寫換家標記） |
   | A→（無），交接中 | 設 A | A 立刻恢復成家，不加紀錄 |
   | 已完成交接的舊家 A | 設 A | A 回任；原本的家另加一筆 |
   | 既有資料有兩個家 | 設 X | 其他家各加一筆，依序排隊 |

3. **「是不是舊家」由紀錄推導**：某筆有效、沒取消、不是「只記新家」的紀錄的舊家，而且現在不是家。設回家立刻不算舊家，不必改旗標。舊家交接完成前（待交接、交接中、失敗，或退任摘要待補）不能刪；完成後可以刪，但照舊唯讀。
4. **新家「開始用」在交易裡記**（之後改回去就不能當沒換過）：網頁在准入交易（`admit_ui_stream`）裡記；IDE 舊路徑、SNS 家模式、外部相容路徑沒有准入交易，在共同入口快速檢查通過時記（之後被 hook 擋下也算開始用）；心跳在 `/awaken` 找到或建好家之後記。無痕不記。
5. **舊家不能開新的一輪**：
   - 共同入口 `proxy_service._run_request` 在 `pipeline.execute` 之前先擋，hook、組主線、記錄這一輪都不會先跑；網頁、IDE、外部相容路徑、SNS 都經過這裡。網頁在准入交易裡再驗一次，作為最後判定。
   - 插話、心跳指定這段（`/awaken` 帶對話編號、`/retrigger`）、換靈魂也擋。
   - 交接開始前，一般存檔不擋（分不出收尾保存和編輯），改名、收尾保存、「僅重試保存」照舊；交接開始後（第二段），存檔閘（`_save_session`）擋所有內容寫入。
   - 錯誤碼兩個：`SESSION_OLD_HOME_READ_ONLY`（1223，「舊家，請先設為家」）、`SESSION_OLD_HOME_NOT_DELETABLE`（1224，「舊家交接完成前不能刪」），都是 409。刪除在路由與刪除閘都擋。
   - 讀不到紀錄時讓這一輪失敗、不放寬（ADR-0078）。
6. **心跳建家走同一套紀錄**：新列寫入的同一個交易裡（`new_home_after_write`，經 `conditional_save_session(after_write=...)`）確認沒有別的家、照上表改紀錄；讀家之後有人設了家就整筆退回，不做出兩個家。
7. **交接前的主線**（`mainline_service`）：還沒交接完的舊家原文（待交接、交接中、失敗；已經又是現任家的不放）依紀錄順序排在家原文前面，各自包一層 `source="old_home_pending_handover"` 的歷史資料界線、每則標時間。帶家原文的地方都帶，網頁版家本身與心跳在家時也帶（新家第一輪就接得上）；心跳在子對話、無痕不帶。讀不到時和讀家一樣：這一輪失敗，心跳照樣送出。
8. **對話清單**（`GET /ui/sessions`）每段多一個 `is_old_home`。
9. **生命週期**（照 ADR-0267、0305 前例）：重置清除與殘留自檢（`StorePurger("home_records")`，在 `PROXY_STORES`、和對話與前情同一區）、刪靈魂前檢查（`assert_prelude_cleared` 一起查，409 帶 `home_records`）、GDPR 匯出（`agents/<id>/home_handover.json`，只有對話編號與狀態）。
10. **PR 1 審查留下的**：`soul_service.inject_home_messages` 的家訊息改成必填，拿掉自己讀家的分支與 `async_db.get_home_messages`；安全測試改走真正的注入路徑（`inject_mainline`）。

## 取捨與影響

- 換家之後舊家不能聊、不能刪、不能換靈魂；要繼續那段就把它設回家。新家還沒開始用時設回去等於沒換過。
- 2a 合併到 2b 合併之間交接不會開始：舊家原文一直帶在主線裡（請求變大），舊家也一直不能刪。這段期間不發版（計畫寫明）。
- 每一輪多一次讀紀錄（有索引）；存檔多一次查詢。
- 取消家只對現任家有作用，修好以前按任何一段都會清掉家。

## 已知限制

- **切分存入舊對話庫不在後端擋**：那個端點不帶對話編號，後端分不出是哪一段；接著的存檔在交接開始前本來就不擋，切出去的部分已經在舊對話庫，不會遺失。PR 3 在舊家不顯示這個操作。
- **收尾等待**：剪裁觸發點遇到不是家的對話會立刻丟掉收尾等待（`dispatcher._closing_save_arrived`）。交接開始前要等舊家的收尾保存，這要等交接工作，留給 PR 2b。
- **IDE 舊路徑**沒有准入交易：快速檢查通過到設家之間的小空檔，當作「換家前就開始的回覆」放行。
- 失敗紀錄在 `/v1/stats` 的顯示、24 小時自動再試、退任摘要的補寫都在 PR 2b。
- `is_old_home` 只在對話清單有，單一對話的快照沒有（`SessionDocument` 是嚴格模型，加欄位會動到版本計算的輸入）；PR 3 用清單。
- 一個靈魂只能有一個家的資料庫唯一限制不做（既有資料可能已有兩個家），由設家與心跳建家在交易裡處理。

## 驗證

- `tests/unit/test_home_transitions.py`：狀態對照表每一列、開始用、舊家與刪除的推導、失效（同名重建、還原）、清單欄位、還沒交接完的舊家順序、清除與計數、升級時回報位置的初始值。
- `tests/unit/test_old_home_gates.py`：共同入口擋在 hook 與組主線之前、IDE 記開始用而網頁等准入、准入交易擋舊家並記開始用、插話、換靈魂、交接前一般存檔照舊、交接開始後存檔閘擋寫入、刪除閘與刪除路由、心跳建家照紀錄且不做出第二個家。
- `tests/unit/test_heartbeat_session_scope_security.py`：`/awaken` 與 `/retrigger` 擋舊家、心跳在家記開始用、沒有家時建家照紀錄。
- `tests/unit/test_mainline_service.py`：舊家原文在主線對照表每一列帶不帶、排在家原文前面、交接完成後不帶、回任後只當家送一次、讀不到時這一輪失敗（心跳照送）。
- `tests/unit/test_session_scope_storage_security.py`：家與舊家都不能換靈魂、沒用過的新家改回去可以換；讀家安全測試改走主線。
- 生命週期：`test_reset_primitives.py`、`test_soul_delete_memory_guard.py`、`test_gdpr_export.py`。
