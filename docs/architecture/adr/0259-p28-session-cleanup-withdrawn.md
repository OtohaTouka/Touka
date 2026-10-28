# ADR-0259：P28 撤回：對話不再自動刪除

- 狀態：✅ 已接受（維護者 2026-10-15 核准記憶系統第二批計畫，P28 決定正式拿掉；與 ADR-0258 同一個 PR）
- 日期：2026-10-15
- 決策者：維護者
- 影響範圍：ToukaProxy（`utils/async_db.py` 刪 `cleanup_expired_sessions()`、`main.py` lifespan 刪啟動呼叫、`config.py` 刪 `SESSION_TTL_DAYS`）；文件 SECURITY.md（P28 列標撤回、Structural 表 `SESSION_TTL` 列刪除）、SSOT_MASTER_MAP、LEGAL_ARCH
- 相關：ADR-0092（滑動視窗只處理家對話）、ADR-0110（GDPR 自助匯出）、ADR-0253（解決其已知限制「P28 單位錯，由維護者決定」），決策都不變；撤回 2026-05-21 安全修補 P28（commit `c8fdd646`）

## 背景

- 2026-05-21 那批安全修補（P1～P29）的 P28 為「Session 無 TTL」加了 `user_sessions.last_activity_at` 欄位、`cleanup_expired_sessions()` 與 `SESSION_TTL_DAYS = 90`，每次 Proxy 啟動刪除 90 天沒寫入的非家對話。
- 截止值用秒算，`last_activity_at` 存毫秒，所以上線後從沒刪過任何列。文件（SECURITY.md、SSOT_MASTER_MAP、LEGAL_ARCH）卻寫成「會刪」。
- 只修單位的後果：每次啟動把 90 天沒寫入的非家對話（包括與靈魂的非家對話、專案對話、群聊、自訂模型對話）整列刪除——標題、整棵對話樹、前情摘要一起消失、不轉存。非家對話不會被滑動視窗剪進舊對話庫，原文就永久消失；在這些對話裡存過的記憶留著，來源核對會變成「原文已不在」。「最後活動」只算寫入，打開看不算。
- 選項：修好並先轉存再刪；正式拿掉；只修單位、不轉存；維持現狀（程式說會刪、實際不會）。

## 決策

1. 正式拿掉：刪除 `cleanup_expired_sessions()`、lifespan 啟動呼叫與 `SESSION_TTL_DAYS`（Settings 欄位與模組常數）。對話不再自動刪除。
2. 保留 `user_sessions.last_activity_at` 欄位、遷移與寫入：它是對話存檔合約的一部分（`models/session_persistence.py`、前端 `session_persistence.ts` 讀回核對），只記最後一次寫入時間，不再驅動任何刪除。
3. 刪除對話改由人類手動刪除或靈魂重置；要把資料帶走用 GDPR 匯出（ADR-0110）。
4. `.env` 裡若還有 `SESSION_TTL_DAYS`，Settings 是 `extra="ignore"`，啟動不受影響、值不再有作用。
5. 測試釘住新行為（`tests/unit/test_sec_batch_epsilon.py` 的 P28 段）：Settings 與 config 模組沒有 TTL 設定；async_db 沒有清除函式；`main.py` 不呼叫；async_db 與 main 的 SQL 字面量裡沒有「依 `last_activity_at` 刪 `user_sessions`」；啟動的資料庫初始化跑完後，很舊的非家對話仍在。同檔 P24／P26／P29 測試不動。

## 後果

- 硬碟用量隨對話數成長（對話是文字，成長小）；要清理請人類手動刪除或重置。
- 文件與程式一致：不再宣稱會自動刪除。
- 日後若要回到「定期清理」，必須另開決策並先轉存，不能只修單位、不轉存；`test_no_automatic_session_cleanup` 會擋下沒有經過決策的清除 SQL。
- 這個決定只是不再自動刪除，既有對話不受影響。

## 補記：P28 段的測試只看行為（2026-10-28，舊審查報告丙-6）

- 決策 5 列的測試裡有三項是在原始碼裡找字：async_db 原始碼裡有 `last_activity_at`、`main.py` 原始碼裡沒有 `cleanup_expired_sessions`、async_db 與 main 的 SQL 字面量裡沒有「依 `last_activity_at` 刪 `user_sessions`」。這三項拿掉。
- 留下的：Settings 與 config 模組沒有 TTL 設定；async_db 沒有 `cleanup_expired_sessions`；啟動的資料庫初始化跑完後，很舊的非家對話仍在。最後這條也確認 `last_activity_at` 欄位存在（沒有欄位就寫不進測試資料）；啟動時刪對話，不管 SQL 寫成什麼樣子都會紅。
- 因此上面後果段「`test_no_automatic_session_cleanup` 會擋下沒有經過決策的清除 SQL」改成：啟動時的清除由 `test_startup_db_init_keeps_old_conversations` 擋下；寫在啟動以外的地方（例如排程）的清除，測試測不到，要靠審查。原本的字面量掃描也只認得特定寫法，換個寫法就測不到。
- 同檔的 P26（擁有者 refresh token 30 秒容差）、P29（「checks passed」記在 DEBUG）改成行為測試，移到 `tests/unit/test_heartbeat_awaken_hardening_security.py`。
