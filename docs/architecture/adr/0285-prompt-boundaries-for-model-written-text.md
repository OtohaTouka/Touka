# ADR-0285：舊審查報告甲組：提示裡模型寫的文字不能偽造可信區塊

- 狀態：🚧 部分實作（甲-1 已實作；同組的甲-2 外部工具參數格式、甲-3 列出的工具預設開，各自的 PR 完成時補進本 ADR）。維護者 2026-10-25 核准修正計畫（「照建議：1～5 都選甲」）。
- 日期：2026-10-25
- 決策者：維護者
- 修訂 ADR-0257（S1「內文先中和」：前情摘要與家對話也中和其他 prompt 區塊 tag）；修訂 ADR-0267（已知限制最後一條：看板那一行的任務 id 也改用 `safe_ref`）。
- 相關：ADR-0092（prompt 區塊邊界）、ADR-0127～0129（Claude 提示快取、波動區塊）、禁令 18（內容消毒抽成純函式）、禁令 20。
- 影響範圍：ToukaMemory `utils/historical_data.py`；ToukaProxy `services/continuity_service.py`、`services/soul_service.py`、`middleware/layers/context.py`；新 `tests/unit/test_mission_board_boundary_security.py`、`tests/unit/test_historical_data_boundary_security.py`；SECURITY、MEMORY_INTERFACE。

## 背景

10/15 的程式審查（Touka #89～#105）在 dev `426dceda` 上核對後，甲組（安全與信任）仍有效的幾項：

- **High**：`<continuity>` 標成「系統記下的事實」，但看板那一行直接印出 `Task.id`（`continuity_service.py:266`）。任務 id 是模型取的、沒有格式限制，可以帶換行、假的「最後一則人類訊息」或 `</continuity>`，之後每一輪（含心跳）都會當成系統事實。同檔「最近完成」那一行早已用 `safe_ref`，這一行漏掉了。
- **同一種偽造的另一個入口**（調查時發現）：`<mission_awareness>`（由 ToolLayer 注入 system）原樣放入模型寫的大目標、任務 id、描述、依賴與產出；換行可以多造一行任務，嵌入的 `</mission_awareness>` 可以提早關閉區塊。
- **歷史資料只中和自己的標籤**：前情摘要與家對話只中和 `historical_data`。摘要裡一個 `</conversation_summary>`，在 Claude 路徑上更嚴重：送出前 `reorder_system_stable_first` 用非貪婪配對把波動區塊搬到尾端，摘要提早結束，後面的文字留在穩定前綴、沒有任何界線，還會進跨回合快取。

## 決定（甲-1）

1. **`<continuity>` 看板那一行用 `safe_ref`**：和「最近完成」那一行一致，只放行 `[A-Za-z0-9._:-]{1,32}`，其他換成 `h:` 加雜湊。這個區塊只是摘要，細節導向看板。
2. **`<mission_awareness>` 保留原文、只做跳脫**：這個區塊是模型操作看板時看的，mission 工具用 id 完全比對找任務與依賴（`mission_manager.py:318, :363, :399`），所以不能換成 `safe_ref`（超過 32 字或含中文的 id 會之後更新不到）。大目標、任務 id、描述、依賴與產出：
   - 換行（`\r\n`、`\n`、`\r`、`\x0b`、`\x0c`、U+0085、U+2028、U+2029）換成可見的 `⏎`，其他控制字元換成空白，不能多造一行；
   - prompt 區塊 tag（`ALL_BLOCK_TAGS`）的開頭 `<` 換成 `&lt;`，不能關閉或另開區塊。
3. **新純函式 `neutralize_tags(text, tags)`**（`memory_server/utils/historical_data.py`，禁令 18）：規則同既有的界線 tag 中和（不分大小寫、`<` 與 `/` 前後可有空白、帶屬性與自閉合都算），另要求 tag 名稱後面接非名稱字元，`persona` 不會動到 `<personality>`。`neutralize_historical_tags` 的行為不變。Memory 不能匯入 Proxy，所以 tag 清單由呼叫端傳入。
4. **前情摘要與家對話也中和其他區塊 tag**：`wrap_historical_data` 多一個 `extra_tags` 參數；`soul_service` 的前情摘要與家對話傳入 `ALL_BLOCK_TAGS`（界線 tag 照舊另外中和）。
5. **不做**：
   - 任務 id 的格式限制（只在建立時檢查）：描述本來就是自由文字，增益很小；在 `Task.id` 加 validator 會因 `mission_manager.py:108-117` 吞錯而讓舊看板靜默消失。
   - Memory 端的查記憶、隨機回想與舊對話查詢（`manager.py`、`recall.py`、`legacy_vault.py`）只中和界線 tag：工具結果本來就在信任較低的位置，這批不改。

## 後果

- 使用者畫面沒有差別。模型與 context 快照畫面會看到 `&lt;` 與 `⏎`。
- 一般的任務 id、描述（含 `<b>` 這類非區塊標籤）照原樣顯示。
- 已知限制：
  - ToukaDispatch 建任務不經 Proxy 的工具合約（`ToukaDispatch/server.py:358-391`），id 照樣不限格式；顯示端已處理，所以不會進到可信區塊。
  - 看板載入失敗會被吞成「沒有看板」（`mission_manager.py:108-117`），這批不改。
- 撤回：revert 甲-1 的 commit 即恢復原樣；本 ADR 檔保留，在狀態註記撤回（後續批次會引用它）。

## 驗證

- 先寫的測試在照原樣回傳的空殼上 8 個紅（看板偽造、`<mission_awareness>` 關閉與多造行、摘要與家對話關閉或另開區塊、摘要經重排後注入文字留在穩定前綴），實作後全綠；一般 id 與文字原樣的案例修前修後都綠。
