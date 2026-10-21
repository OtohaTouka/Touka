# ADR-0276：心跳、SNS 與對話歸屬防護

- 狀態：✅ 已接受
- 日期：2026-10-17
- 後續延伸：[ADR-0277](0277-chat-conversation-search.md) 已實作聊天搜尋，沿用本 ADR 的建立標記、精確歸屬及家檢查；marker 不作逐則作者證明。
- 影響範圍：ToukaProxy 的 `utils/session_scope.py`、`utils/async_db.py`、心跳／UI／聊天路由、dispatcher、SNS gateway、滑動視窗、技能與 MCP 啟用工具及啟動 context 注入；Chat 錯誤碼與兩語翻譯；沙盒安全測試。
- 修訂：修訂 [ADR-0132](0132-session-owner-attribution.md) 的對話寫入授權與人類身分透傳、修訂 [ADR-0231](0231-session-persistence-outcomes-and-revisions.md) 的條件提交與 UI 串流准入、修訂 [ADR-0245](0245-model-layer-stage-2a.md) 的對話綁定與 SNS 目標邊界。人類歸屬欄、持久化結果型別及靈魂驅動權繼續沿用。
- 相關：ADR-0035／0092（回合後剪裁）、ADR-0230（排程交易與憑證）、ADR-0260（家對話外送欄位）。

## 背景

心跳與排程可帶入其他靈魂的 session ID，SNS 的共享頻道也未按靈魂分開；追加或 upsert 可能讀入別人的樹，再把 `agent_id` 換成此次執行者。只驗角色、對話存取權或人類同時擁有兩個靈魂，不能阻止這種跨靈魂續寫。

家定位原本只查 `home_for_agent`，沒有核對同列 `agent_id`，也可能在多個候選中任取一筆；注入時又另讀一次樹。一般 UI 存檔可改家欄位，能留下互相矛盾的家。工具、串流與剪裁在先讀後寫之間，也可能遇到綁定已變更。

## 決策

### 1. 歸屬判斷與儲存閘

`utils/session_scope.py` 集中純規則：退休 ID、精確綁定、家欄一致、可換綁條件、嚴格空草稿及 UI 生成基底。正式靈魂 ID 由 `agent_service.identify_agent` 解析；同一靈魂的舊別名不視為換綁，既有 `agent_id` 原值保留。

非人類執行者必須與對話綁定相同，Debugger 或 admin 代打都不例外。人類需有舊對話存取權，舊、新目標是靈魂時都要有驅動權；`owner_user_id` 仍只在 INSERT 記錄，不能靠換靈魂換擁有者。

`SessionService._save_session` 收斂 append、upsert 與條件式保存：取得同一 session 鎖、`BEGIN IMMEDIATE`、嚴格讀最新列、驗 revision 與歸屬，再於同一連線寫入及提交。缺席、損壞樹、讀取失敗與提交結果不明分開處理；失敗不能當成空對話覆寫。一般 conditional/upsert/append、心跳、排程、SNS、工具與剪裁都無換綁權。

讀取執行歷史走 `read_execution_snapshot`；心跳在准入、每次脈衝及每次中間／最後保存重驗。retrigger 同時驗執行靈魂與人類對話存取權。新舊 dispatcher、`connect_mcp_server`、`activate_skill` 使用條件提交；剪裁在歸檔／摘要之前先驗 scope，最後保存仍有 CAS。

### 2. 人類 UI 換綁與家欄位

換綁只開放 `is_human=True` 的 UI 專用保存 `save_ui_session` 與明示持久串流 `admit_ui_stream`；一般保存即使帶人類身分，也不能換綁。目標由伺服器解析，請求參數沒有可自行打開的換綁開關。

| 原列與操作 | 結果 |
|---|---|
| 同一正式靈魂續寫 | 保留綁定原值與建立標記 |
| 嚴格空草稿換目標（含 NULL→目標） | 兩側授權成立才允許，標記保留原值 |
| 非空對話換目標（含一般模型→靈魂） | 授權人類可延續原樹，標記同交易降為 0 |
| 空樹但有摘要，或任一旁支仍有節點 | 按有內容換綁，標記降為 0 |
| 非人類、非 UI 換綁，或 A→NULL→B | 拒絕 |
| 家對話、殘留家標籤、退休 ID 或 `touka.` 內部頻道換綁 | 拒絕，不能利用空草稿例外 |

嚴格空草稿要求原列及待存列的整棵樹都沒有節點、`context_summary IS NULL`、不是家且 `home_for_agent IS NULL`，並排除退休與保留 ID。只清目前分支、先在 payload 清掉舊內容，或同一次保存帶入新內容，都不能保留標記。

UI `SessionModel.to_changes` 忽略 `is_home`／`home_for_agent`，包括明確的 false／null；公開欄位仍保留相容。家欄位只由專用 `set_home` 操作變更，受管建立家對話除外。家對話須先經 `POST /v1/ui/sessions/set_home` 取消家，再換靈魂；一般 save 不偷偷清家。`set_home` 在 IMMEDIATE 交易內先驗目標，通過才清舊家及設定新家，錯目標不會先清掉合法家。

### 3. 家定位與注入

`read_home_snapshot` 一次讀取正式 ID 與舊 `ToukaSoul` 前綴的全部家候選及樹。零候選才代表沒有家；多筆候選、退休列，或正式化後 `agent_id`、`home_for_agent`、請求靈魂三者不完全相同，均拒絕。不能先濾掉壞列再挑一筆好列。

顯示用 `get_home_session_id` 與 `get_home_messages` 共用此快照；scope 拒絕時只記固定 `SESSION_SCOPE_MISMATCH`，不回 ID／不注入歷史。DB／JSON 錯誤仍是 `SessionPersistenceError`。心跳與 SNS 直接使用嚴格入口，壞家不能當成沒有家而新建或改走獨立頻道。

### 4. 每靈魂頻道與 SNS

`channel_session_id` 將陣列以 `ensure_ascii=False`、無多餘空白的 JSON 編碼為 UTF-8，使用完整 SHA-256；座標不是用分隔符串接。

| 用途 | ID |
|---|---|
| 獨立心跳預設頻道 | `touka.hb.v1.<sha256(["heartbeat", 正式靈魂ID])>` |
| SNS 獨立頻道 | `touka.sns.v1.<sha256(["sns", 正式靈魂ID, platform, channel_id])>` |

`touka.` 是保留命名空間。只有受管頻道入口可建立自己計算的 ID；UI 缺席建立一律拒絕，admin 亦同。心跳手填其他保留 ID，不論列是否存在都拒絕；自己的預設 ID 可沿原頻道解析。計算出的 ID 已被其他靈魂占用時拒絕，不覆寫、不換 ID 重試。知道 ID 不等於取得授權。

心跳 `session_mode="independent"` 時，省略 `session_id`、空字串、舊 `pulse_channel_default` 都解析到該靈魂的新預設頻道；以欄位 presence 區分省略與模型預設的 `home`。明確 `session_id="home"` 或 home mode 仍選家。Heartbeat wire model、client、排程保存格式與 bridge 不變；外靈魂 session 可被存入排程設定，執行到 Proxy `/awaken` 才重驗並拒絕。

舊 `pulse_channel_default` 與所有 `sns_*` 列保留，不搬移、不匯入新頻道歷史、不續寫或換綁；新頻道從空白開始。純專案 unlink 中繼資料操作維持原合約，這不是「舊列每個欄位永久不變」的保證。

SNS 單靈魂目標只可為 adapter 本身的正式靈魂，或 adapter 唯一人類擁有者同時擁有的靈魂。查不到唯一擁有者、目標不是靈魂或解析失敗時，跨目標拒絕；不借 Debugger／admin 全域權力放寬。拒絕早於建列、user append、typing、pipeline 與 send。home 模式只有確定沒有家才走該靈魂的 SNS 頻道。

SNS 閘道以可信 `AgentIdentity` 寫入使用者與助理訊息；送入 pipeline 的只有固定的目標靈魂 ID 與 `request.session_id`，不帶身分，所以外人訊息可用的技能與特權維持修補前的零；啟動時注入服務 context，不在事件函式延遲匯入 main。assistant 保存失敗不外送。`@別名` 群聊仍交給 `ToukaGroup`，不選取或寫入任何單靈魂 session，也未新增群聊儲存；每個事件先清除診斷欄位。

### 5. 建立標記與未來搜尋

`session_scope_version INTEGER NOT NULL DEFAULT 0` 由既有 startup migration 補欄；舊列不回填。只有受管 INSERT 建立的新列寫 1，普通 UPDATE 保留原值，合法人類 UI 非空換綁降為 0；建立新靈魂前也須撤銷同名／別名綁定或家引用列的標記（已接線入口與尚未涵蓋的入口見 ADR-0277）。標記不可提升：清空樹、刪摘要、換回原靈魂、設家、匯入或改成未來時間都不能把 0 升成 1；空草稿換綁也只保留原值。

標記留在內部 strict row 與 revision，list／GET／snapshot 的公開投影移除；公開 `SessionModel` 忽略額外 marker，內部 `SessionChanges` 保持 extra-forbid。加欄會讓部署前的整列 revision 失效一次，重啟 epoch 也會作廢舊 revision；UI 應重讀 snapshot 後處理保存，不自動重送模型。

未來對話搜尋可將 `session_scope_version=1` 作為受管生命週期條件，仍須核對精確靈魂歸屬、對話存取權與退休 ID。舊列及曾非空換綁的列均不可納入；不能用時間 cutoff 或非空 `agent_id` 代替。這次沒有實作搜尋，也不保證人類沒有貼入舊內容、或模型沒有引用舊家歷史。

### 6. 串流與失敗回報

持久 UI 串流只收人類 requester；准入在建立 buffer／producer 前，以同一交易驗 revision、目前 user parent、assistant ID 未存在、兩側授權與家／命名空間。若換綁，提交後的精確新 revision 同時交給 buffer 與 producer。生成前重驗基底，最後 assistant 保存僅能同綁定續寫；期間又換成另一個靈魂時，舊 producer 的 CAS 失敗，不能再次換綁。

scope 錯誤只有 `SESSION_SCOPE_MISMATCH`（1221，HTTP 403），沒有另一個退休頻道錯誤碼。條件式存檔維持 `FailedResult`，以 `error_code=1221` 表示歸屬拒絕；stale revision 沿 `SESSION_SAVE_FAILED`，不明保存沿 `UnknownResult`。串流准入的條件衝突為 409、結果不明為 503；終態仍分開回報生成與保存，`saved` 才附實際 revision。

心跳背景的歸屬／持久化錯誤只記固定代碼、結束為 error，不追加錯誤文字，也不剪裁；其他錯誤的追加仍經儲存閘。SNS 不將例外原文外送；技能／MCP 啟用保存錯誤經 `build_tool_error` 分為 permission_denied／resource_not_found／internal_error。不能把保存失敗回成成功。

## 已知限制與部署邊界

- 家對話換靈魂是「先取消家、再換綁」兩個操作，並非單一交易；不自動修復既有矛盾列或清理退休內容。
- UI 換綁在生成前已提交；生成失敗、取消或 no_text 都不回滾綁定與 marker=0。舊畫面的 revision 可能衝突，需重讀 snapshot；沒有新前端互動或自動重送。
- SoulLayer 仍在 dispatcher 准入之前；換靈魂第一輪不注入舊 session 摘要，目標的家另經嚴格驗證。人類請求自帶的原樹仍是明示延續內容。
- 家候選讀取與首次建家不是同一個交易，沒有新增跨 session 的唯一家索引；並發可能留下多家，後續嚴格讀取會拒絕，不任選一筆。
- 保留頻道建立使用 CAS；同時搶建造成的非 saved 結果回 `SESSION_SAVE_FAILED`，不自動重試。既有同綁定保留頻道仍可由有權人類保存；禁止的是冒名建立與換綁。
- 心跳每次保存都比對 revision；人類同時在同一對話保存時，進行中的心跳以 `SESSION_SAVE_FAILED` 結束並記錄，不再整樹覆寫人類剛存的內容，也不自動重跑。
- 已綁靈魂的對話中途改用一般模型時不換綁、不降標記，一般模型的回覆存在該靈魂的對話下；這是人類明示的操作，不涉及其他靈魂的內容，但未來搜尋不能把標記當成「只有這個靈魂產生的文字」。
- 所有 writer 必須一起升級，不混跑舊、新版本，也不部署只有部分防線的版本。降版、還原不可信快照與手動 SQL 不在 marker 保證內；驗證只用 sandbox／mock，不操作實體資料。

## 驗證範圍

五個安全檔合計 97 案：storage 44、provenance 11、heartbeat 18、SNS 9、dispatch 15；共用 `tests/support/session_scope.py` 使用沙盒 SQLite 與合成身分。覆蓋交易內拒絕、marker、UI 換綁與生成失敗、家快照、HTTP 403、頻道碰撞、SNS 同擁有者限制、工具競態及剪裁前閘；逐檔清單與既有回歸調整見 `tests/README.md`。

Guard #45 基準只同步 `system.py` 的 `_activation_error`、`_save_activation` 與 `connect_mcp_server` 三筆 consumer 形狀，不改守衛或重拍其他條目。既有回歸改從快照與條件提交觀察；SNS 改驗目標靈魂與對話固定、pipeline 不帶身分、DebugAgent 跨靈魂寫入改驗拒絕，並發追加以真沙盒 SQLite 驗十筆內容完整保留。分類與案例數見 `tests/README.md`。
