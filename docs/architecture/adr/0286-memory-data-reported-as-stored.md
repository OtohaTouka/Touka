# ADR-0286：舊審查報告乙組：記憶資料照實

- 狀態：✅ 已實作（乙-1、乙-2 都已實作）。維護者 2026-10-25 核准修正計畫（「照建議：1～5 都選甲」，第 2 題選甲）。
- 日期：2026-10-27
- 決策者：維護者
- 修訂 ADR-0264（決策 5「訊息只新增、同 id 不覆寫」：內容不同的另存成版本；決策 6「一則都沒寫進去就不剪」的計數改成確認在庫的筆數；已知限制第 4 條「節點內容不是字串歸檔會失敗」已解決）。
- 修訂 ADR-0263（乙-2：決策 7「`find_duplicates` 不改」：沒掃成改回錯誤；已知限制第 2 條「`query_vectors` 自己吞錯」已解決；forget 照實回四種結果）。
- 相關：ADR-0256（記憶來源、事實庫是正本）、ADR-0267（可信活動事件）、ADR-0092（滑動視窗）、禁令 24、28、57、59。
- 影響範圍：ToukaMemory `services/legacy_store.py`、`services/legacy_sync.py`、`vector.py`、`factual.py`、`services/lifecycle.py`、`services/manager.py`；ToukaProxy `tools/builtin/memory.py`；`tests/unit/test_legacy_archive.py`、`tests/unit/test_sliding_window_service.py`、`tests/unit/test_memory_index_state.py`、`tests/unit/test_continuity_event_hooks.py`；MEMORY_INTERFACE、PROXY_INTERFACE。

## 背景

10/15 的程式審查（Touka #89～#105）乙組（記憶資料完整性）在 dev `426dceda` 上核對後仍有效的：

- **新內容被靜默丟掉**：`LegacyStore.archive` 對訊息用 `ON CONFLICT(id) DO NOTHING`，`archive_payload` 卻回「要寫的訊息數」。同 id 已存著不同內容時新內容沒寫進去，滑動視窗剪裁、匯出端點、手動同步照樣當成已歸檔；剪裁會把這段剪掉，新內容就此遺失。
- **報告建議的修法不能用**：改成「回這次實際插入的筆數」的話，剪裁存 session 那一步（ACID-B）結果不明之後重剪時，這批訊息多半已經在庫裡，插入數很小甚至是 0，剪裁會判成沒歸檔而不剪，要等切點往後移才剪得動；匯出端點重送同一段也會報錯。
- **內容不是字串就歸檔失敗**：剪下的節點內容是 `None` 或多模態 list 時，寫進 `content TEXT NOT NULL` 失敗，那個 session 就一直不剪（ADR-0264 已知限制第 4 條）。
- **設定頁統計整個失敗**：`file_status` 只接 `OSError`；legacy 目錄裡有不是 UTF-8 的 JSON 檔時，`UnicodeDecodeError` 讓統計端點回 500。
- **語意查詢壞掉被當成「沒有」**（乙-2）：`query_vectors` 遇到任何錯誤（嵌入模型載入失敗、向量庫讀不到、查詢失敗）都吞成空清單。查記憶不知道語意半失效、不會註明（ADR-0263 已知限制）；掃描重複回「沒有重複」；存記憶的語意查重悄悄跳過，回覆照常。
- **封存把資料庫錯誤說成查無**（乙-2）：`archive_fact` 吞掉所有例外回 False，forget 回裸字串「Fact ID not found」（禁令 59(a)）。同一筆 forget 第二次也回成功、再記一次封存事件、再同步一次索引。

## 決定（乙-1）

1. **歸檔照實**：`LegacyStore.archive` 在同一個交易裡先只新增，再用一句固定 SQL 讀回這批 id 存著的內容（`json_each(?)`，只綁一個參數，同 `factual.py` 的寫法，禁令 24），比對 session、角色與內容（`messages.id` 是全域主鍵）：
   - 相同：確認在庫，包括這次之前就存著的。
   - 不同：另存成這則訊息的版本列（第 2 題甲案）。版本列的 id 是「原 id＋`~v`＋session、角色、內容的 SHA-256 前 16 碼」，重跑不重複存，改過兩次就各存一版；metadata 記 `version_of: 原 id`，讀 metadata 才分得出是哪一則的版本。
   - 版本列寫入後也讀回確認；對不上（同一個版本 id 已存著別的內容）丟 `LegacyArchiveMismatchError`，整筆 rollback。
   - 回 `ArchiveOutcome(confirmed, versions)`：確認在庫的筆數（含版本列）與另存成版本的筆數。和「記憶被取代時舊的保留」（ADR-0263）同一個精神：已歸檔的內容不改，新的內容也不丟。
2. **`archive_payload` 仍回 int**，意思改成確認在庫的筆數；剪裁「一則都沒歸檔到就不剪」與匯出端點「沒有可歸檔的訊息回錯誤」的判斷不變。手動同步的結果多一個 `conflicts`（另存成版本的筆數）。
3. **內容一處正規化**：`_message_row` 把內容換成文字：字串原樣、`None` 是空字串、其他存成 JSON（`ensure_ascii=False`、`default=str`）。沒有 id 的訊息，決定性 id 仍用原始內容算，舊紀錄的 id 不變。
4. **`file_status`** 讀不到或不是 UTF-8 的檔算待處理（`except (OSError, ValueError)`）。雜湊方式不改（解碼時 CRLF 會正規化，改成雜湊原始位元組會讓既有紀錄對不上）；不加快取。
5. **不變的**：不改既有列、不需要遷移、不新增剪裁觸發點（禁令 28、57 不受影響）；`upsert_messages` 照舊覆寫；三個呼叫點與「兩個 tools 目錄不得引用」不變。

## 決定（乙-2）

6. **語意查詢失敗丟錯**：`VectorStore.query_vectors` 整段（含 `collection_exists`）失敗時丟 `SemanticSearchUnavailableError`（`raise … from exc`，原因只進日誌）；這個身分還沒有向量庫仍回 `[]`。三個呼叫端：
   - 查記憶：既有的 `except` 照常附「語意搜尋暫時無法使用」（ADR-0263 決策 5 不變）。
   - 存記憶與一步取代（`lifecycle.check_new_memory`）：事實庫是正本（ADR-0256），照存。取不到向量庫或查詢丟這個錯時 `CheckedContent.semantic_checked=False`，回覆在 `(ID: …)` 之後附 `SEMANTIC_DEDUP_SKIPPED_NOTE`。原本吞掉一切的外層 `except Exception` 拿掉：其他錯誤（例如回查事實庫失敗）往上丟，閘道回 `internal_error`、不寫入。
   - 掃描重複（`find_duplicates`）：取不到向量庫也轉成同一個錯；Proxy 閘道對這個錯回 `build_tool_error(upstream_error, …)`（禁令 59），不再說「沒有重複」。沒有任何有效記憶時不開向量庫，照舊回沒有重複。
7. **forget 照實回報**：封存的 UPDATE 不能直接加 `AND archived = 0`（ADR-0263 用 forget 把已被取代的舊版本從歷史拿掉，那種列本來就是封存的）。`archive_fact` 改成同一個交易分三步，回 `ArchiveResult`：
   - 封存仍有效的（`archived = 0`）→ `archived`。
   - 只在目前版：清掉已被取代舊版本的 `superseded_by`（同時更新 `updated_at`，和原本一樣）→ `detached`。
   - 兩步都沒改到才查這筆在不在 → `already_archived` 或 `not_found`。
   - 只有前兩種記 `memory_archived` 事件、同步向量封存；`already_archived` 回資訊性的成功（forget 冪等），不改 `updated_at`；`not_found` 回原句。
   - 資料庫失敗不再吞成 False：丟 `FactWriteError`（SQLite 原文只進日誌），閘道回 `internal_error`。
8. **不變的**：公開合約、權限與風險分類、Guard #45 基準都不動；`resolve_duplicate`、`find_duplicates`、`query_vectors` 的參數不變，`archive_fact` 只換回傳型別（呼叫端只有 `resolve_duplicate` 與測試）。

## 後果

- 舊對話庫裡同一則訊息可能有兩個以上的版本。模型用的舊對話搜尋（`query_messages`、`legacy_vault`）不讀 metadata、也不印 id，兩版在結果裡就是兩則一樣格式的訊息，看不出哪則是版本、哪則比較新；設定頁的訊息數（`get_stats`）也把版本列算進去。
- 手動同步的回應多 `conflicts`；前端不顯示，畫面不變。
- 模型看到的記憶回覆（乙-2）：forget 已經封存的多一種「已經封存，沒有改動」；forget 已被取代的舊版本改說「從版本歷史拿掉」；存記憶在語意查重沒做成時多一行；掃描重複沒掃成、forget 遇到資料庫錯誤時回結構化錯誤。成功回覆的開頭與 `(ID: …)` 格式不變（smoke、記憶承諾閘照舊）。
- 向量庫壞掉期間存記憶照存，意思相近的重複可能存進來（回覆會說）；向量庫恢復後可用 `manage_dups` 的 scan 整理。
- 已知限制：
  - 記憶來源核對用原 id 呼叫 `get_message`，拿到的是第一版內容：記憶的來源訊息若被存成版本列（同 id 早已在庫、而且是別的 session 或舊內容），來源畫面會顯示「已修改」或「已不在」，但原文其實在庫裡。要同 id 早已在庫才會發生。候選：查不到相符時，再找同 session、`version_of` 是原 id 的版本列比對內容指紋。
  - 比對只看 session、角色與內容，不看 metadata、時間與 parent：只有這些欄位不同時算同一則，新欄位不存（同步檔記成已處理）。
  - 內容轉文字（`_content_text`）沒有排序 key，記憶來源的內容指紋（`content_fingerprint`）有；目前存進 session 的節點內容只會是字串或 None（`TreeHistory` 驗證），沒有影響。以後若允許多模態 list，剪下後記憶來源會一律顯示「已修改」，要先統一這兩處。
  - 版本 id 已被別的內容占用時，那次歸檔失敗、剪裁不剪（機率極低；安全的失敗方向）。
  - 滑動視窗剪裁會遺失附件與工具呼叫紀錄（只在開了滑動視窗剪裁時；這次調查新發現，三份 ADR 都沒提）。乙-1 修好後，原本卡住的對話會開始剪，影響範圍變大。維護者選甲案：這次不改，記成候選另做（要改舊對話庫的結構），記在 HANDOVER §3。
  - 較新版本的記憶庫（換回舊版執行）不做取代連結的讀寫：forget 一筆已被取代的舊版本回「已經封存，沒有改動」（原本回已封存，但同樣沒有從歷史拿掉）。
  - 掃描重複只要有一筆的語意查詢失敗，整次回錯誤、不給部分結果。
- 撤回：乙-1、乙-2 各自 revert 即恢復原樣；乙-1 已寫入的版本列留在舊對話庫裡（metadata 有 `version_of`，可辨識），乙-2 不改任何資料。本 ADR 檔保留，在狀態註記撤回。

## 驗證

- 先寫的測試在修正前 14 個紅：內容不同另存成版本、同 id 不同 session 或角色、同一批再歸檔算確認在庫、版本讀回不符整筆 rollback、`None`／list／dict 內容、決定性 id 用原始內容、同步回 `conflicts`、統計遇到不是 UTF-8 的檔、`archive_payload` 把版本算進確認數、剪裁遇到已存著不同內容的訊息照常剪並另存版本。修正後全綠。
- 程式審查第 1 輪找到數字 id 的問題：匯入檔的 session 或訊息 id 是數字時，SQLite 存成文字、讀回比對時型別對不上，數字 session id 每次同步都丟 mismatch，數字訊息 id 每次都多存一份假版本。修法是 `_message_row` 先把 id 與 session id 換成文字（既有資料本來就是文字，不用遷移）；「數字 id 比對成文字」的測試修正前紅。另補同一批裡同 id 出現兩次、同內容在三個 session 各存一份的測試，以及守門測試多攔 `_archive(`。
- 「存 session 結果不明後重剪，第二輪照常剪成」在修正前後都綠：釘住不能改成「回這次插入的筆數」。
- 變異檢查 12 個全部變紅（內容正規化兩處、比對欄位兩處、不另存、不讀回、版本 id 不看內容、不記 `version_of`、確認數、`archive_payload` 回傳、同步計數、統計例外）；其中「版本 id 不看內容」靠之後補的「改過兩次各存一版」測試抓到。審查後再加 3 個（訊息 id、session id 不換成文字，版本雜湊不看 session）也都變紅。
- 乙-2 先寫的測試在修正前 14 個紅：查詢失敗與讀不到向量庫時丟錯、查記憶附語意失效、存記憶與一步取代附查重沒做到（包括取不到向量庫）、重複掃描兩種沒掃成都回 `upstream_error`、沒有記憶時不開向量庫、封存四種結果、拿掉歷史、忘第二次不重做也不重記事件、資料庫錯誤（SQLite trigger 擋下封存）回 `internal_error`。「這個身分還沒有向量庫時是空的」修正前後都綠。修正後全綠；`archive_fact` 換回傳型別後，兩個只斷言回傳為真的舊測試改成比對 `ArchiveResult.ARCHIVED`。
- 乙-2 變異檢查 19 個全部變紅：查詢又吞錯、讀不到向量庫不包進來、兩處當成查重做了、說明一律空白、存記憶與取代各自不附說明、沒有記憶也開向量庫、掃描取不到向量庫不轉錯、閘道說成內部錯誤、封存不看原本有沒有封存、不從歷史拿掉、一律當成存在／不存在、資料庫錯誤說成查無、拿掉歷史不更新時間、本來就封存照記事件、拿掉歷史當成沒改動或回一般封存句。
