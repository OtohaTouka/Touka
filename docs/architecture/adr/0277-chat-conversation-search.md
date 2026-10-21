# ADR-0277：聊天對話搜尋與跨來源分頁

- 狀態：✅ 已接受
- 日期：2026-10-17
- 影響範圍：ToukaMemory 的公開 conversations 合約、來源分發、LegacyStore、內部 row/page 與資源協定；ToukaProxy 的 ResourceManager、SessionService、session_scope、ui_conversation_reader、記憶閘道及啟動接線；Guard #45 基準與沙盒測試。
- 修訂／相關：修訂 [ADR-0274](0274-memory-conversation-search.md) 的 conversations 來源契約，預設查兩庫、聊天對話可搜尋；沿用 [ADR-0276](0276-heartbeat-session-scope.md) 的建立標記與歸屬閘。時間與歷史資料界線沿 ADR-0258、ADR-0257。

## 背景

舊對話搜尋只接 legacy，無法查仍在聊天儲存裡的內容。聊天列的建立標記與精確靈魂歸屬已可用，搜尋須沿同一規則判定，不能用角色、擁有者或標題猜歸屬。

## 可見範圍

- UI reader 只接受可信 `AgentIdentity` 中的非人類靈魂執行身分，或精確 `DebugAgent`；人類（含 admin、同名人類）、一般模型、群聊、DispatchAgent、子代理與未知 actor 均拒絕。前景交談用換過的靈魂身分，心跳用執行身分；明示 legacy 保留原有身分契約。
- 身分與列綁定兩側經 `_canonical_session_agent`／`agent_service.identify_agent` 解析，正式 ID 必須相同；候選只取正式 ID 與既有 `ToukaSoul` 別名，DebugAgent 不接受別名。高角色、同一人擁有多個靈魂、模型名或 context 字串均不擴權；同靈魂不因人類建立者不同而排除。
- `session_scope_version` 必須精確為整數 1；退休的 `pulse_channel_default` 與所有 `sns_` ID 永遠不可見。新 `touka.hb.v1.*`／`touka.sns.v1.*` 通過相同條件後可查，來源仍標 `ui`。
- 同一讀取交易先查目標全部家候選中繼資料，不能先套 marker、退休 ID 或綁定過濾。多家、退休家、綁定／家欄矛盾整次拒絕；唯一合法 marker=0 家只是不輸出，普通對話仍可查。逐列也驗殘留 `home_for_agent`，外靈魂壞 JSON 不解析。
- 只投影已提交樹的 `currentId → parentId` 祖先鏈，合法 null 為零筆；只取 user／assistant 的字串 content，不讀旁支、buffer、摘要、備份或已刪列。先驗 TreeHistory，再驗目前鏈的循環、斷鏈及節點 ID；損壞不當空結果。
- 無痕沿 ToolLayer 不廣告、ToolExecutor 拒絕未廣告工具，閘道另於取得任何來源前檢查可信 `_ctx.incognito`；全部 source 都拒絕。無痕不保存，搜尋不虛設資料表的 incognito 欄位。

UI 的 assistant 顯示「助理」，附註「按對話歸屬整理，不代表逐則作者已驗證」；legacy 保留原顯示。marker 只證明受管生命週期，不能證明每則都是該靈魂所寫：一般模型回覆、人類貼入或引用舊內容都可能存在。

建立或重新註冊靈魂須在寫 config 前、後各以單一交易，將既有 `agent_id` 或 `home_for_agent` 屬於正式 ID／`ToukaSoul` 別名的全部列降 marker 為 0，封住兩步之間的人類存檔競態；不刪列、不改其他欄位、不提升 marker。`create_soul` 與 `update_soul` 補建已接線，失敗以固定 `SESSION_SAVE_FAILED` 阻止靈魂可用；第二次失敗回滾新靈魂，更新轉換則復原原 config 或移除新 config，保留既有目錄內容。一般更新既有靈魂不降標記。新增／轉換的 ID 不得以 `ToukaSoul` 開頭（不分大小寫），在降標記前回固定 `VALIDATION_ERROR`；失效服務與搜尋共用正式化候選。模型／別名 registry 本身不建立靈魂，設定快照匯入也不匯入靈魂資料。

系統快照是覆蓋式還原，會保留備份後才建立的靈魂，不能假設靈魂與對話一起回到備份當時。Proxy 的 `_execute_restore` 在還原子程序啟動前，經 ResourceManager 原子寫入資料根的 `.session-restore-pending`（名稱與位置由 `async_db.get_restore_pending_path()` 統一解析）；寫入失敗不啟動子程序。標記存在時，UI 搜尋在讀任何列之前回固定 `internal_error`，all 亦整次拒絕。子程序返回或失敗後、報完成或重啟前，必經 SessionService 將現存對話庫的**所有列** marker 降為 0，成功後才刪除持久標記；備份沒含對話庫也照做。降標記或刪除持久標記失敗則還原狀態為 `failed`、固定 `SYSTEM_BACKUP_FAILED`，不重啟。缺庫不建庫，缺表／marker 欄視為無列可降，留給正常啟動 migration（預設 0）。因此從備份還原的 UI 對話永遠不能被搜尋；只有還原後建立的新對話能依其他歸屬條件被搜尋，續寫舊對話不會提升 marker。明示 legacy 搜尋不受此標記影響。

Proxy 啟動時在正常 session-store migration 後、reader 綁定與 bots 啟動前檢查持久標記；有標記就補降全部列，再刪除標記。失敗只記固定 `SESSION_RESTORE_RECOVERY_FAILED`（不帶例外內容），保留標記、不阻擋啟動，搜尋持續拒絕；下次啟動仍會再試。

重置的 `purge_agent_sessions`／`count_agent_sessions` 與搜尋共用正式 ID／別名候選（DebugAgent 只認精確 ID），只比對 `agent_id`；僅由家欄引用目標、實際綁定其他靈魂的列保留，矛盾家仍整次拒絕搜尋。

## 來源、時間與分頁

- `source="ui"`／`"legacy"` 各查單一來源；`"all"`、省略或 null 查兩庫。公開欄位不變，`page_size` 1～20、預設 10，`offset` 非負、預設 0；不接受 `both`、target 或 bypass。
- query 只比完整內容，`%`、`_`、反斜線照字面，大小寫沿 SQLite LIKE 的 ASCII 語意；標題不參與搜尋。日期是 YYYY-MM-DD 台北整天、含頭含尾；時間以 `to_epoch_ms` 正規化，UI 不拿 session updatedAt 補時間。
- 有日期時毫秒與訊息 ID 都升冪，沒有日期時都降冪；同鍵先 legacy 再 ui，最後 session ID 升冪。未知時間永遠最後；有日期則排除，另報可見且符合 role／query 的「無法按日期判定」數量。legacy 的異常時間也適用，包含單查 legacy。
- 合併前每來源各取前 `offset + page_size` 筆及總數，再排序切頁；不依文字或 ID 去重。總數是訊息數，各來源 count/page 共用自己的讀取交易，不承諾跨庫或跨頁共同快照。
- 回傳仍是文字：界線外列來源、日期、總數、排序及下一頁 offset；每筆標來源、台北時間、角色、標題前 80 字與內容前 300 字。歷史界線固定為 `legacy_conversation`／`chat_conversation`／`conversation_search`，資料內的界線標籤先中和。UI 提醒時間可能來自裝置時鐘，legacy 保留匯入日期說明。

## 讀取接點與錯誤

Memory 只經 `get_resource_proxy()`／`IResourceProxy.query_ui_conversations`，不取得 UI DB 路徑。正常啟動在初始化後綁定 `resource_manager.bind_ui_conversation_reader(session_service)`；查詢只委派，未綁定不自行初始化或延遲匯入 main。

`SessionService.query_ui_conversations` 管連線、交易、家候選與綁定；`services/ui_conversation_reader.py` 只做樹驗證、文字投影、篩選與分頁。內部傳原 identity、query、role、毫秒日期邊界、oldest_first、limit、offset，以 `ConversationRow`／`ConversationPage` 回傳。

搜尋在連線前以 ResourceManager 判存在性，缺庫回成功空頁；`get_db_path()` 只解析路徑，既有連線的目錄準備留在 `get_async_db()`。UI 沿普通連線，搜尋資料 SQL 只有 SELECT，交易允許 BEGIN／COMMIT／ROLLBACK，不用 BEGIN IMMEDIATE，不從搜尋啟動初始化或 migration；legacy 保留既有 `mode=ro`。

這是已授權的正常產品 runtime 讀取，不是開發代理對實體資料的測試，因此不要求物理唯讀證明。沿用連線、身分解析與 logging 可能有正常執行期副作用；沒有新增 WAL／SHM／檔頭檢查、權限修復類別或唯讀 resolver，也不保證外部並行刪換整個 DB 檔案時零寫入。自動化測試仍全用 sandbox／mock，不因此取得實體資料 probe 權限。

| 狀況 | 公開結果 |
|---|---|
| 公開參數不合法（含壞日期、反向日期、來源或分頁） | `validation_error` |
| 無痕、不合格 UI actor、歸屬矛盾、多家或退休家 | `permission_denied` |
| 已綁 reader 的庫不存在／沒有命中／offset 超過總數 | 成功空頁，保留實際來源與總數 |
| reader 未綁、不是資料庫、缺表／marker 欄、壞樹或讀取失敗 | `internal_error`，不回原始例外或路徑 |
| 持久還原標記存在 | `internal_error`，讀列前拒絕 ui／all；明示 legacy 不受影響 |

任一指定來源失敗就整次失敗，不回另一來源的部分內容、計數或下一頁提示。

## 已知限制

- 實體世代尚未追蹤；降 marker 不是世代 UUID，不能撤銷舊執行身分或已送出的結果。`update_my_soul` 經 `soul_io` 補建 config 不套建立前降標記：只能由同一靈魂自己的執行身分補建自己的資料夾，不會讓另一個人的對話變成可見；若補建日後可由其他身分觸發，須加上同一降標記步驟。還原持久標記與補降只在 Proxy 流程，直接執行備份 CLI 不含此步驟；行程中斷後以持久標記阻擋搜尋並在下次啟動補降，只涵蓋 Proxy 當次還原使用的資料根。還原子程序沒有和 Proxy 同生同死：非容器部署若只殺掉 Proxy 主程序，存活的還原子程序可能在下次啟動補降之後才寫入對話庫；容器重啟會一起結束它。還原的 `.env` 若改變資料根，新根的庫不在保護範圍內。回滾沿用 soul_io 的 best-effort 行為，檔案系統故障仍可能使回滾失敗。
- UI 在記憶體載入該靈魂所有候選列並投影後才篩選分頁；大資料量與大 offset 沒有新的資源上限。
- ADR-0276 前建立的聊天列永遠不可見，不回填 marker；非空換綁降為 0 後，即使換回、清空、設家或生成失敗也不恢復。嚴格空草稿只保留原值。
- currentId 位於內節點時，不包含畫面沿 childrenIds 向下補出的訊息；標題仍是目前中繼資料，可能留有已刪內容的文字。
- 確認刪除後的新 UI 查詢不含該列，獨立 legacy 歸檔仍可能在 all 出現；不能撤回已送出的結果。降版、混跑舊 writer、還原不可信快照與手動 SQL 不在 marker 保證內。
