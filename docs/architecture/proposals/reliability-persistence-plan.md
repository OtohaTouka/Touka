# 可靠性與持久化結果實作計畫 R2

> **狀態**：T2–T5 來源實作已完成，契約見 [ADR-0231](../adr/0231-session-persistence-outcomes-and-revisions.md)。下文保留原計畫的設計、分批與驗收矩陣作為歷史；現況以根 HANDOVER 為準。頁面／程序消失會失去記憶體中的未保存內容，TTL 只涵蓋仍在 memory buffer 的結果；來源回復不含真資料。

## 實作結果摘要

B1：保存失敗拋固定安全的 Memory 領域例外，Proxy 原生邊界映射既有 `PERSISTENCE_ERROR`（1302）與 `outcome=unknown`；原任務鎖、權限及 Dispatch token／invoke 順序維持。
讀取因果門檻：empty／JSON 壞檔／I/O 錯誤令 update、review、finalize 拒絕，寫入 0。未發現所選 mutation 自動將不可讀當新板寫回，因此不修改讀取降級；明確 initialize 的覆寫行為不擴改。
Memory wrapper 與其既有 `tools/__init__.py` 導出可自然傳播新例外，不需修改。

B2：刪除結果等待、嚴格成功內容、失敗／未知保留、無痕零 HTTP 與確認期間防連按。`deleteSession` 的 `Promise<boolean>` 僅表示刪除是否已確認，false 包含未知，不能推論未提交。
`SessionItem.tsx`：真 Radix 第二次確認原會自動關閉並清狀態，現在保留選單等待結果；手動關閉仍清確認。

### B3 交付（9/16）

B3 提前納入原 B5 生成型別及 B4 connections scope/wire、registry 前置屏障與固定 node ID helper。
純 `session_persistence.py` 為保存／started／terminal模型SSOT，`update_openapi.merge_schemas` 與既有整份schema同步測試共用；不以fake端點掛模型。
同session有限writer以已確認樹重新套用純轉換，pending／failed／unknown富樹不被refresh或其returned list覆蓋；logical operation依scope／session／generation去重。
每次明確登入／clear使舊scope失效並best-effort abort；同principal內部refresh維持epoch。wire前、refresh後及body await後重驗；client JWT主體只作錨點，server auth不變。
`SessionListProvider`單一同步state/ref入口與keyed子樹清除舊scope的registry、Chat靜態history／input／attachment／drafts；`SessionContext.tsx`結構已核對涵蓋，不作形式修改。

| 原9個updater區塊 | B3實際consumer與結果處置 | 主要驗證 |
| --- | --- | --- |
| SessionList rename | 純title轉換，單次timestamp；hook等待full結果才關編輯器 | syncSession／useSessionActions.persistence |
| Registry persistDone（原2個sync分支） | `completeProducer`釋放同session屏障；full成功才清entry，unknown保留富樹／非active | syncSession／StreamRegistryProvider.persistence／既有registry |
| Registry user turn | 從受控current.messages追加固定user ID；saved(full)才beginProducer和模型；拒絕不佔active | registry persistence／SessionContext |
| useChatStream agent bind | await語意變更，再讀最新session；保存／scope失敗保留input、模型0 | useChatStream.incognito／真scope view |
| Chat auth level | 固定timestamp純轉換；明示背景命令，結果由writer固定提示處理 | ChatProvider.authLevel重播 |
| Chat delete message | view純計算與writer語意刪除分離，保留其他分支 | 同上delete案例 |
| Chat switch branch | writer對current樹切換；副作用不在React updater | 同上branch案例 |
| import loop | updater只合併，保存Promise在外面逐筆執行；全部已存才全成功，部分失敗分列 | useSessionEvents重播／partial |
| SkillSelector toggle | writer對current activeSkills切換，模型／時間值在轉換外；固定失敗提示 | SkillSelector重播 |

另5個直接sync sites：Chat新建／home新建維持本機預覽＋明示背景sync，下一個user保存命令排在同一writer後，前者halt則模型0；Chat整樹更新回傳`SaveResult`供split consumer判斷；project更新await writer且不另外報保存成功；useChatStream新建await full／incognito skip後才能送出。
正常split的legacy匯出與session縮減不是原子操作：前者成功、後者failed／unknown只提示「已匯出、縮減未確認」，不自動重做。無痕split拒絕縮減、HTTP0、保留完整記憶體樹，改用既有下載；不把下載呼叫當成檔案已保存證明。
EOF 只表示 unknown，error／cancelled 不自動保存部分結果。`skipped(no_text)` 且 generation=completed 會嘗試 full 富樹保存；text/no_text 不能解除 user-turn full 屏障。

## 1. 基線、證據地位與目標

本文件是本工作包新版 R2 SSOT。規劃的歷史輸入是一份 2026-09-13 的交接筆記（原件未入版控）；
其中 `c790e03` 的抽查、未發布背景及建議順序不代表現況或授權。
已結案的 `tool-observability-and-reliability.md` 是另一份工具觀測提案，不併入本工作包。

| 層次 | 本輪核對結果 | 可以／不可以據此宣稱 |
| --- | --- | --- |
| 開發來源 | `dev`／`bf946d662ad40512d6a7a52046966b56fe85afa3`，起始工作樹與 index 乾淨 | 最近兩批精確 112 檔已提交，未 push |
| 本機 main | `1093e8d8e9a355c32b363c9dfa9b335dca474cb2` | 不等於 dev；不切分支、不自動合併 |
| T1A／T1B | dev 已有 StorageService／ScheduleService 修復；main 相關舊檔仍與 9/13 基準 blob 相同 | **ALREADY_FIXED_SOURCE**；不重新修 Heartbeat，不稱 main 已修 |
| runtime／映像 | 既有交班記錄的現行來源 `5c48a5bd51e61081bdeb58493fea1343bc4a10cd` | 本輪未 probe／拉映像；不可說 dev 修復已發布或生效 |
| T2–T5（規劃當時） | 以下來源呼叫路徑已確認，規劃時尚無故障注入；本輪進度依上方當前段 | 當時 **NEEDS_REPRO**；不是已量測的生產資料遺失事故 |

成功結果：任務寫入失敗不宣稱已完成；對話生成與落盤分別可見；刪除失敗不無聲消失；
React 狀態計算可重播且不重送寫入；正常對話、背景續跑、完整訊息樹、權限與無痕保持一致。

不做：全面 Memory／安全稽核、通用交易或 queue 平台、持久化工作佇列、換狀態管理庫、新模型呼叫、
全面消除單例、資料 migration、遠端 Git／發布／live。O1 Memory 耦合、O2 初始化成本維持觀察。

## 2. 當前問題與有限修復責任

以下路徑均相對 repo；行號以本次 dev 基線為定位輔助，實作 B0 須依函式內容重綁。

| 項目 | 現況證據 | 新行為與有效 RED 理由 |
| --- | --- | --- |
| T2 保存 | `ToukaMemory/src/memory_server/services/mission_manager.py:125` 捕捉寫入例外；initialize:209、finalize:251 之後仍回 state | 注入受管寫入失敗，公開入口不得回已保存 state／成功；舊行為會誤成功 |
| T2 讀取 | 同檔 `_get_current_mission_unlocked():99` 把讀取／解析失敗變 None | 先重現是否使本批 mutation 把既存不可讀看板當缺席；只有這條直接因果成立才納入同批 |
| T2 底層 | `ToukaProxy/src/services/resource_manager.py:2406` 的 atomic write 會 raise | 沿受管 I/O 傳播，不能為上層測試另做 Path.write_text 或改底層失敗為成功 |
| T3 producer | `ToukaProxy/src/services/dispatcher.py:67` 缺 session 返回；:121–136 保存失敗仍 trim／finish(done) | 保存結果不得默認成功；失敗喚醒訂閱者，成功保存前不得啟動裁切 |
| T3 buffer／解析 | `stream_buffer_service.py` finish 只有內部狀態／EOF；`ToukaChat/src/lib/connections.ts:498,517,599` 忽略 DONE、EOF 呼叫 onDone | 真 parser／resume 必須看得到 typed 終態；單改內部 status 不足 |
| T3 富樹 | `StreamRegistryProvider.tsx:98–123,175–196` 未 await 補存便清 entry，onError 也清內容 | 延遲／失敗補存時不顯示已存、不丟 steps／thinking／attachments／分支 |
| T4 刪除 | `SessionListProvider.tsx:86` 忽略 response／rejection；`useSessionActions.ts:34` 不等待便清 confirmation | 403／500／網路中斷後項目保留，其他 session 新變更保留；無痕 DELETE HTTP=0 |
| T4 後端 | `ToukaProxy/src/routers/ui_sessions.py:453–472` 缺 session 仍 200；`src/utils/async_db.py:486` 返回 False | 沿現行「已不存在亦成功」契約，不憑空把 404 或任何 4xx 都視成功 |
| T5 updater | 下列 9 區塊／10 個 sync 呼叫點 | 同一 updater 重播應純計算；舊 mock 只執行一次不能證明此要求 |

T5 精確盤點：SessionListProvider rename（1）；StreamRegistry persistDone（1 區塊、2 分支呼叫）與 user turn（1）；
useChatStream agent binding（1）；ChatProvider auth level、刪訊息、切分支（3，後兩者還巢狀於 setHistory）；
useSessionEvents import loop（1）；SkillSelector toggle（1）。合計 **9 個 updater 區塊／10 個 sync sites**。
另 5 個非 updater sync sites：ChatProvider 新建／分支／其他既有 action 共 4 處與 useChatStream 新建 1 處，
也必須消費新結果或明示受控 background 呼叫；不因傳回 Promise 已 resolve 就宣稱成功。

## 3. 要採用的契約

### 3.1 T2：領域例外，保持現有鎖與任務審查

`_save_mission_unlocked` 成功才回傳；受管 write 失敗轉成固定、無原內容的 `MissionPersistenceError`，
保留 exception chaining 供內部診斷，對外不得輸出原始路徑／JSON／驗證輸入。類別留在 Memory 領域，
不讓 Memory import Proxy exceptions，也不另建全系統 Result framework。
Memory tools／Proxy mission_tools 的真正邊界映射既有 `build_tool_error`／出貨錯誤碼；
Dispatch 既有 caller 必須收到失敗而非完成通知，不改 MCP 認證或任務路由。

寫入前以 copy 作待提交 state（包含 updated_at）；只有成功後才將結果作為已提交狀態返回。
測試固定 initialize、save、update、review、finalize 等實際共享保存入口，避免只修一個回覆字串。
保留 MissionLock／FileLock 的取得及 finally 釋放、目標身分解析、依賴 DAG、DONE 審查、
verification_cmd_locked、review lock、FAILED 重設及 singular／plural artifact 獨立語意。

讀取修訂採因果門檻：若 B1 的既存壞檔／read-error 案例會被 mutation 當 None 再寫，
就在同一 `_get_current_mission_unlocked` 區別「確認缺席」與讀取／解析失敗，後者固定領域錯誤、禁止寫回；
空白既存檔也是錯誤，不當新檔。若所有受影響 mutation 均未覆寫且其他驗收不依賴此改動，記後續，
不擴掃所有 Memory I/O。測試用 owned 實體／mock，不能藉診斷打開真看板。

### 3.2 保存結果 SSOT

新增純 Pydantic `SaveResult`／串流 metadata 模型於 `ToukaProxy/src/models/session_persistence.py`，
由既有 OpenAPI 產生鏈輸出前端 alias；SSE 模型顯式納入同一生成鏈，不新增假 API 只為掛 schema。
使用有 discriminator 的最小結果，不回傳 raw exception：

| status | 必要內容 | 意義 |
| --- | --- | --- |
| saved | representation=`text` 或 `full`、必要的 revision | 已收到保存確認，或唯讀核對確認後端內容與待保存內容相符；後者不證明原請求的提交時點。text fallback 不等於富樹完整保存 |
| skipped | reason=`incognito` 或 `no_text` | 按指定模式刻意不寫；無痕正常，不叫已保存 |
| failed | error_code（出貨 registry 的安全碼） | 已確認操作被拒絕／未提交；不憑任意 500 推定未寫 |
| unknown | error_code | timeout／斷線／提交點不明，結果未知，不自動重送 |

skipped 不需要 revision；failed／unknown 不得沿用舊 token 冒充本次有效 revision。恢復操作須重新取得並核對當前 revision。

`syncSession` 改為 `Promise<SaveResult>`；已處理的 HTTP／網路錯誤轉結果，不產生無人接收 rejection。
成功需 `res.ok` 與合法成功 envelope；非法／空成功 body 不當成功。原 POST 的 `status=success,session_id` 保留，
附型別化 persistence 結果，既有錯誤仍走正常 HTTP error envelope。前端未知 detail／Error.message 不直接顯示。
`no_text` 只代表 producer 無文字 fallback；如果前端有工具步驟或其他富內容，仍要走 full 保存。
缺身分或缺 session 不是 skipped；前者沿授權失敗，後者明確 failed，不能趁補存悄悄重建未知 session。

### 3.3 T3：真正可消費的終態與恢復

只擴充 UI resumable 路徑；heartbeat、外部 OpenAI／Claude／Gemini passthrough 不套用新保存承諾。
保留 OpenAI delta 等原內容 frame。新增同一模型來源的 `touka_stream_started` 與 `touka_stream_terminal` data 事件：

- started 綁 `session_id,stream_id,user_parent_id,assistant_node_id`；ID 在一次生成開始時固定，非模型生成值。
- terminal 綁同一 ID、`generation=completed|error|cancelled` 及 `persistence: SaveResult`。
- producer 的 append／finish 接收已解析 stream_id，只作用於該 entry；晚到舊 producer 不可終結新串流。
- terminal 與 entry 終態在同一 Condition 臨界區提交，只有一次；正常訂閱與 resume 讀同一 frame，喚醒全部 waiter。
- terminal 保留可重播的 seq；滿 chunk、超齡、error、cancel 也有終態空間與喚醒，不被容量上限吞掉。
- `[DONE]` 只代表上游生成結束，EOF 只代表 transport 結束；對 resumable UI，缺合法 terminal 的 EOF 結果為 unknown，不能走成功 onDone。
- decoder flush／最後未換行 frame、重複 terminal、半包與格式錯誤必測。相同 stream 終態只消費一次；其他 stream 的結果不得覆蓋目前 session。

後端 `_persist_assistant_turn` 回傳真 SaveResult；缺 session、storage exception 及受管方法回 `False` 都不再隱藏。
現行 `upsert_session`／append 的 False 是 catch-all，可能發生於 commit 附近；有效 RED 必須分別覆蓋 False 與 exception。
無法證明提交點的 False／exception 一律 **unknown**，只有明確的缺席、授權拒絕、驗證或 conditional conflict 等 precommit 結果才為 failed。
不得以「方法回 False」推定資料沒寫入；後續唯讀核對才可能解除 unknown。
只有 `saved(text)` 才進現有家對話裁切入口，維持禁令 28／57 的位置、per-session 鎖、子對話排除與非阻塞。
生成 error／cancelled 永遠不被保存成功改成 completed；預設不因取消新增後端部分內容寫入。
取消前已完成的寫入不能宣稱撤回；提交點未知則回 unknown，前端保留記憶體內容、停止 spinner。

沿既有 `async_db.append_message_to_session`（:454）的 per-session lock／unlocked helper，有限延伸固定 parent/node ID 與必要 owner 歸屬，
讓 server fallback 的「讀當前樹→組樹→保存」保持同一臨界區；不重造 store，不在 get 與 upsert 之間釋鎖，不重入同一鎖而死鎖。
該方法現行 missing／exception 回 False；呼叫端必須檢查結果，不能因 await 完成就轉 saved。
`TreeMessageManager.append_node` 增加可選固定 node_id，其他 caller 預設不變；相同 ID 不重複 append，
衝突內容／非預期 parent 不覆蓋。這是同一回合身分，不是全庫去重平台或資料庫 migration。

前端 started 事件將 placeholder 對齊固定 assistant ID，重播先以該回合的 user_parent_id 建立／對齊基底，
即使後端已存在 fallback assistant，也只更新同一節點，不能再 append 一份。最終 full tree 保存包括整個 messages map、
currentId、非目前路徑分支、steps、thinking、attachment、tool calls 與既有擴充欄位；不得只存線性文字。

resume 保留現有 active 查詢語意，新增 `GET /v1/ui/sessions/{session_id}/stream/state`（TTL 內含 terminal），沿原 session 權限；
已知 session 的斷線／切換可先查 state 再接回 terminal；不把 `/streams/active` 改為所有歷史清單。
重新載入只查一次 active 清單；當前選定 session 另查一次 state，切換到其他 session 時才各查一次，不全清單 fan-out、不定時 polling。
server 重啟或 600 秒 TTL 到期：只能載入已落盤歷史；未保存富樹若也不在瀏覽器記憶體，**無法保證恢復**。
不得為補救將無痕／富樹寫入 localStorage、IndexedDB 或自造持久 queue；也不重送模型／工具請求。

### 3.4 T3／T5 共用的 session 寫入順序

SessionListProvider 內以同一 sessionsRef／純 state transition 持有最新快照，React updater 只計算值；
既有 setSessions 保留為純 state 接口，不能另留不一致鏡像。新增有限 session 命令入口處理 create／metadata patch／tree update／delete，
每 session 一條記憶體 Promise chain；不同 session 可並行，結束即清理。不是通用 queue，不增加服務或套件。
ID／timestamp／operation token 在 action 邊界產生一次；promise 去重只認同一 logical operation token，
不得把兩次合法相同改名誤當重複。React StrictMode 重播 reducer 不再產生 token／HTTP／toast。

所有 setSessions ingress 都經同一個**同步純 transition** 更新 ref 與 render 值，包括 ChatArea、SessionContext 傳遞的既有 caller；
不得只遷移 9 處 sync 而讓直接 setSessions 寫到另一份 state。`refreshSessions` 的 setState merge 與其**回傳給呼叫端的 list**
使用同一 reconciliation：pending／failed／unknown 的 local tree 與未提交欄位保留，server snapshot 另存供核對；
背景 GET、切換／登入刷新不能抹掉富樹。只更新該 scope/session 的已確認乾淨欄位，不以整份舊 list 回滾。

每次 command／GET／terminal 綁 `{proxyOrigin, authenticatedPrincipalId, authEpoch, sessionId, sessionGeneration}`；
開始發送、異步完成與套用結果前都重驗。沿 `getActiveProxyUrl` 與 AuthContext／`touka_auth_changed` 的真來源，不能只比 session ID。
logout／principal 變更使 epoch 失效並 abort 舊 request；proxy 切換時舊 scope 結果／排隊命令不得改投新 origin 或採用新登入憑證。
舊 draft 只屬原 scope 的記憶體，登出後不可顯示給下一人；不能為保存它而外傳／持久化。fetch 的 refresh-auth 等待期間變更也要 abort，
確保舊命令沒有在 await 之後改用 B 的身分發到 A/B。不同 scope 的相同 session ID 不共用 queue、提示或 generation。

同 Tab 正常生成的明確屏障：

1. 新 session／agent 綁定與 user tree 經同一入口依序保存；`saved(full)` 後才啟動模型，無痕則 skipped 後只走記憶體。
2. failed／unknown 時保留輸入與 user tree，解除等待，不發送模型；onSuccessResetInput 延到真的接受啟動。
3. producer 活動期間，該 session 的 metadata／tree 修改保留為**語意操作**與未保存狀態，不把早期整樹 snapshot POST 到後端。
4. 收到 terminal 後先以其 revision 處理該回合 full tree，再以最新 session snapshot 重放已排定的語意操作，最後才各自確認保存。
5. full 保存未完成時保留富樹與未保存狀態；較晚操作不得用舊 snapshot 覆蓋。unknown 會停止該 session 自動寫入並要求重新核對，不重送同一 mutation。
6. delete 與同 session 寫入共用順序；終態／取消／屏障未解決時不發 DELETE。已確認刪除設本地 generation fence，拒絕晚到 callback 把它復活。

FIFO 本身不保證整樹正確，還須協調本流程會觸發的 sliding-window writer：

- 在既有 SessionService／`_get_session_lock` 內加入有限的 content revision／conditional commit；不新增 DB 欄位或 store。
  canonical projection 包含**所有本次可覆寫欄位**：完整 `messages`、`title`、`updatedAt`、`agent_id`、`project_id`、`auth_level`、
  `active_skills`、`active_mcp_servers`、`is_home`、`home_for_agent`、`last_activity_at`、`context_summary`，以及 `id`、`owner_user_id`、exists。
  保留每個欄位的 presence，明確區別 absence／null；B0 按實際序列化與 SQL 寫入清單核對，不漏掉新欄位，不能只 hash 線性文字或最後訊息。
- token 同時綁服務 process epoch 與受管 session generation；在同一鎖內建立／刪除／重建時轉代，重啟令舊 token 失效。
  這是既有 per-session 同步的記憶體 metadata，不是持久佇列；缺席建立也需 conditional-absence 檢查，不能拿舊 token 復活刪除項目。
- GET 與成功保存回傳 revision；本計畫選定的 UI writes、producer append、trim 都必須帶 expected revision／generation，
  同鎖 read→compare→write，衝突寫入 0 並回固定 conflict。現有 API 的其他 caller 不在此保證內，不能宣稱所有 writer 已全面 CAS 化。
  路由沿 fresh auth，revision 包含 owner 不代表 client 可指定 owner；human owner／agent／auth_level／project 的原授權規則不放寬。
- `sliding_window_service.py:332–422` 在 summarize 前保留 snapshot/revision，**摘要期間不持 session 鎖**；
  之後同鎖 conditional commit。UI full save 先完成，trim 丟棄過時摘要並跳過該次裁切，不自動再呼叫模型。
- trim 先提交，UI full save 的舊 revision 回 conflict：保留完整 draft、明示尚未保存，禁止覆蓋已裁切／更新的樹或盲重送。
  下一次保存只能走下述使用者明確的保存操作。原禁令 28／57 的觸發位置、非阻塞、home／子對話與 trim lock 全部保留。
- delete 也在既有同 session 鎖內作條件檢查／刪除及 generation fence；它現在沒有持該鎖，B4 作這個必要有限修訂。

三個選定 writer 的衝突不等於任意多分頁／外部 writer 已可合併；其他寫入若改內容會使 token 失效，
本計畫保證本次 conditional writer 不覆蓋它，沒有宣稱能阻止所有未採契約的 writer 自行覆寫。
若此有限內容 token 無法守住指定因果，先修訂計畫，不臨時新增 DB migration 或通用分散式協調。

sync 的所有 consumer 都要處置結果：user turn／full tree／import／明確保存 UI 要 await；
不阻斷 UI 的背景 metadata action 仍交同一受控入口並接收結果／固定提示。不要散落 `void syncSession` 當成錯誤處理。
import 逐 session 匯總 saved／skipped／failed／unknown；只有全部符合預期才顯示成功，部分失敗保留成功項與失敗清單，
不把整批重送、不重建其他 session、不顯示含原內容的錯誤。

### 3.5 T4 與可見狀態

採最小的「後端確認後刪除」。200 合法成功（包括現行缺席結果）才移除該 ID；403／500／拒絕／unknown 保留。
404 不自行視成功；沒有理由改後端既有冪等缺席契約。home session 保護與 human／agent ownership 不變。
無痕只本地移除、零 DELETE／POST；不得把無痕缺席當成需要後端確認。
useSessionActions await 結果，失敗保留 confirmation 與可操作項；pending 期間防雙按，成功才清除。
既有 confirmation timer 不得在 pending 時掩蓋失敗；改名失敗同樣保留可理解的未保存狀態，不默默關閉編輯器當成功。

在 ChatArea 放一個小型 SessionPersistenceNotice，資料來自 provider，視圖不自己 fetch。
狀態至少可區分「生成中」「正在保存」「已生成、未完整保存／結果未知」「已取消／生成失敗且內容保留」。
failed／unknown entry 不計入 active stream 上限，也不能因清 spinner 刪掉富樹；切換對話再回來仍可看到。
只存瀏覽器記憶體，關閉／重新載入不承諾保留；不新增自動 retry／模型 resend。提示沿 i18n 與生成 error codes。
notice 的「重新核對」handler 只 GET 同一 scope/session，**不經一般 refresh 覆蓋 draft**：
server 正規化保存內容與待保存內容全部相符才標 saved；明確不同則保留 draft／conflict 與新 revision，GET失敗仍 unknown。
若已確認缺席，不暗中重建；只能走使用者明確的新建操作。unknown 不自動 POST。
在確認失敗／衝突且已取得當前有效 revision 後，提供「僅重試保存」：使用者明確決定保存所保留的 draft，
單次 conditional POST，再衝突就停止，沒有模型／工具執行。原 scope/owner/generation 不符則按鈕失效。
成功才解除該 session 的保存屏障並續行仍有效的語意命令；再次 unknown 保留內容及阻塞，沒有永久無操作的黑洞。

## 4. 分批與精確預計修改清單

以下為原規劃的分批與預計修改清單；各批依序寫入，共用檔集中在同一處整合。
新增檔有明確職責；沒有列出的必要新路徑須先修訂計畫範圍。

### B0：重綁來源與隔離

核對 dev HEAD、main 與工作樹；不切分支／stash／reset。保存每批修改前來源，
以 exact path、原命令／exit／stdout／stderr 串起證據即可，不新增封存服務。T1A／B 逐項復核 reuse 適用性。
確認既有 conftest／smoke owned env、public seeds、HB first-log 隔離與 Node/Python runtime；不讀真設定來做檢查。
本批不預定產品修改；若隔離失效先停 dependent 測試，按根因修訂計畫。

### B1：T2 任務持久化

- 修改：`ToukaMemory/src/memory_server/services/mission_manager.py`（含同檔領域錯誤）、`ToukaMemory/src/memory_server/tools/mission_tools.py`、`ToukaProxy/src/tools/mission_tools.py`（實際失敗邊界）；不改 ResourceManager I/O 合約。
- 新增：`tests/unit/test_mission_persistence_failures.py`，集中受管 write/read 故障、成功回讀及 state／鎖驗證。
- 擴充／回歸：`tests/unit/test_mission_finalize.py`、`tests/unit/test_mission_manager_bugfixes.py`、`tests/unit/test_mission_concurrency.py`、`tests/unit/test_mission_filelock.py`、`tests/unit/test_mission_cross_identity_idor.py`、`tests/unit/test_mission_manager_cross_identity_path.py`；crossproc 使用既有 `tests/integration/test_mission_filelock_crossproc.py`。
- 文件：本計畫、HANDOVER、MEMORY_INTERFACE、PROXY_INTERFACE、tests/README；共用錯誤來源見 B5。Guard45 如因本批 consumer 形狀發現實際變動，只核有限差異，不重錄基線求綠。

### B2：獨立 T4 刪除

- 修改：`ToukaChat/src/lib/providers/SessionListProvider.tsx`、`ToukaChat/src/hooks/useSessionActions.ts`；必要追加 `ToukaChat/src/components/sidebar/left/SessionItem.tsx` 的 Radix 關閉接縫。B3/B4 階段再接共用寫入／revision屏障，不平行改同檔。
- 新增：`ToukaChat/src/lib/providers/__tests__/SessionListProvider.mutations.test.tsx`、`ToukaChat/src/hooks/__tests__/useSessionActions.persistence.test.tsx`。
- 擴充：`ToukaChat/src/test/components/SessionItemDeletion.test.tsx`；後端契約回歸 `tests/unit/test_ui_sessions_idor_security.py`，必要在原檔補 missing=200／false-result 情境。
- 文件：CHAT_INTERFACE、tests/README、本計畫／HANDOVER。B2不改後端缺席=200契約；B4才接相同鎖／revision／generation，不能削弱權限。

### B3：T3／T5 共用型別與命令入口

- 新增：`ToukaProxy/src/models/session_persistence.py`（純模型 SSOT）；`ToukaChat/src/lib/session_persistence.ts`（純 session transition／operation 結果工具，不自訂 HTTP）；`tests/unit/test_session_persistence_contract.py`（typed schema／安全 error 反例）。
- 修改：`ToukaChat/src/lib/providers/SessionListProvider.tsx`、`ToukaChat/src/lib/providers/ChatProvider.tsx`、`ToukaChat/src/lib/SessionContext.tsx`、`ToukaChat/src/hooks/useSessionEvents.ts`、`ToukaChat/src/components/chat/SkillSelector.tsx`、`ToukaChat/src/hooks/useSessionActions.ts`。只遷移保存／純 updater 與結果提示，不改 auth authority 或其他 UI 功能；ChatArea的state ingress在B4一併接合。
- 修改入口預備：`ToukaChat/src/lib/providers/StreamRegistryProvider.tsx`、`ToukaChat/src/hooks/useChatStream.ts`；先用 mock terminal 驗命令屏障，B4 才與真 transport 接合，不宣稱本批端到端已完成。
- 擴充：`SessionListProvider.syncSession.test.tsx`、`SessionListProvider.refreshMerge.test.tsx`、`ChatProvider.authLevel.test.tsx`（皆在 `ToukaChat/src/lib/providers/__tests__/`）、`ToukaChat/src/hooks/__tests__/useSessionEvents.test.tsx`、`ToukaChat/src/components/chat/__tests__/SkillSelector.test.tsx`；新 mutations／actions 測試同批續用。
- 新增：`ToukaChat/src/lib/__tests__/session_persistence.test.ts`，驗純計算重播與同 session／跨 session 受控操作，不能只跑一次 updater。

### B4：T3 producer→buffer→真 parser→registry→保存

- 修改後端：`ToukaProxy/src/services/dispatcher.py`、`ToukaProxy/src/services/stream_buffer_service.py`、`ToukaProxy/src/services/sliding_window_service.py`、`ToukaProxy/src/routers/ui_sessions.py`、`ToukaProxy/src/utils/async_db.py`、`ToukaProxy/src/utils/history_manager.py`，只加指定原子 append、有限revision／conditional commit、trim協調、metadata／terminal／state查詢與型別回覆。
- 修改前端：`ToukaChat/src/lib/connections.ts`、`ToukaChat/src/lib/providers/StreamRegistryProvider.tsx`、`ToukaChat/src/hooks/useChatStream.ts`、`ToukaChat/src/lib/stream_tree.ts`（固定 node ID／重播）；`ToukaChat/src/components/chat/ChatArea.tsx` 接合呈現與既有 state ingress，不另建 state／HTTP 來源。
- 新增：`ToukaChat/src/components/chat/SessionPersistenceNotice.tsx`、`ToukaChat/src/components/chat/__tests__/SessionPersistenceNotice.test.tsx`、`ToukaChat/src/lib/providers/__tests__/StreamRegistryProvider.persistence.test.tsx`、`ToukaChat/src/lib/__tests__/connections.streamTerminal.test.ts`。
- 擴充後端：`tests/unit/test_dispatcher_resumable.py`、`tests/unit/test_stream_buffer_service.py`、`tests/unit/test_stream_resume_endpoints.py`、`tests/unit/test_session_owner_attribution.py`；前端 `ToukaChat/src/hooks/__tests__/useChatStream.incognito.test.tsx`、`ToukaChat/src/components/chat/__tests__/ChatArea.test.tsx`、`ToukaChat/src/lib/__tests__/stream_tree.thinking.test.ts`／`stream_tree.attachment.test.ts`／`stream_tree.approval.test.ts`。
- 既有 parser auth／timeout／reasoning／skill event 測試與 session grants、stream stop 回歸保留；mock 僅在 transport／受管 backend，不能 mock 掉待驗 parser／provider 串接。
- 新增 `tests/unit/test_session_persistence_ordering.py`，集中 raw trim/UI兩種完成順序、False提交點未知、conditional delete／缺席建立與generation；摘要使用mock，不呼叫provider。

### B5：錯誤、文件、生成與整體驗收

- 錯誤出貨 SSOT：`ToukaProxy/config/errors.json.example`；生成 `ToukaChat/src/lib/error_codes.ts`。不修改 ignored runtime errors.json。沿現有 loader missing-definition fallback；新碼／文案只加本批必要固定錯誤。
- API 模型生成：`scripts/update_openapi.py`、`openapi.json`、`ToukaChat/src/types/api.ts`、`ToukaChat/src/lib/types.ts`；必要顯式註冊 SSE 模型，測 `tests/integration/test_api_schema_sync.py`，不人工雙編 schema。
- 文案：`ToukaChat/src/locales/zh-TW/translation.json` 與 `ToukaChat/src/locales/en/translation.json`；無新 locale／套件。
- 文件：`docs/architecture/MEMORY_INTERFACE.md`、`PROXY_INTERFACE.md`、`CHAT_INTERFACE.md`、`SSOT_MASTER_MAP.md`、`tests/README.md`、`SECURITY.md`、`docs/CHANGELOG.md`、`HANDOVER.md`、本計畫。
- ADR：原規劃未預配編號；實作核准後已核對索引並同時建立 ADR-0231 與 `adr/README.md` 索引。AGENTS 只有新架構需對應規則時才列具體修訂，不動不可變前段；NOQA_REGISTRY 只有新增逐行合法抑制時更新實掃數字。
- 不因本案增設泛化 guard。以真行為與故意破壞結果／事件／updater 的負例守住缺口；既有 schema、SSOT、security、native contract 等全部保留。

## 5. 原規劃歷史： Gherkin 與可驗收 TDD 矩陣

以下是原規劃的 Given／When／Then；需求表本身不是測試原件。正常 baseline GREEN 只標回歸，不能硬說 RED。

| ID／Given | When | Then／預期 RED | 測試落點 |
| --- | --- | --- | --- |
| G1 我有合法任務與權限 | 對建立／更新／finalize 注入受管儲存提交前故障 | 收到安全失敗；讀回舊值，不能回新已保存 state；目前吞錯會紅。其他提交點不明錯誤不推定未寫入 | B1 failures＋finalize |
| G2 任務有 review／驗證鎖 | 未過審 DONE、跨 agent 或剝除驗證命令 | 寫入 0、原閘拒絕；write 失敗後鎖可再取得 | B1既有權限／lock |
| G3 已存在看板讀取失敗 | 受影響 mutation 嘗試進行 | 不覆蓋／不初始化；先證直接因果，再納同批 | B1 failures |
| G4 我確認刪除一般對話 | 403／500／斷線、另一對話同時更新 | 項目與其他新值保留、明確失敗／未知；目前無條件移除會紅 | B2 mutations／actions |
| G5 我刪除無痕或後端已缺席對話 | 合法刪除 | 無痕 HTTP=0；一般 missing 依200成功移除；home／他人仍拒絕 | B2＋IDOR |
| G6 我送出新一回合 | user tree 保存延遲／失敗 | 延遲前模型 HTTP=0；失敗保留輸入，不顯示已發送；目前未await會紅 | B3 registry／hook |
| G7 生成成功但文字保存失敗 | storage 回 False 或拋例外後 producer 完成 | 未知提交點為completed＋unknown；precommit拒絕才failed，訂閱者結束，trim=0 | B4 dispatcher／buffer |
| G8 後端 fallback 已存 | 前端富樹保存延遲／拒絕 | 顯示保存中／未完整保存；完整樹仍可見，不能只因text已存清entry | B4 registry／notice |
| G9 SSE 分段、無最後換行 | parser 收到 terminal／只有DONE或EOF | terminal正確解析一次；缺terminal結果unknown，無假成功 | B4真connections parser |
| G10 我斷線／切換對話 | TTL內resume含已完成fallback | 同一assistant ID只一份，steps/thinking/分支保真，A/B不混；過期不假恢復 | B4 resume＋tree |
| G11 我取消生成 | terminal、晚到callback或新stream競態 | 保留生成取消，舊事件不終結新stream；停止spinner而不丟已呈現內容 | B4 buffer／registry |
| G12 我用無痕生成／停止／刪除 | 同上成功與失敗分支 | 後端session持久化、前端persistent storage、刪除HTTP均0；只留記憶體 | B2/B4 incognito |
| G13 React重播同一updater | 9區塊各執行兩次或StrictMode | 同樣state、HTTP/toast=0；同一logical action只有一次受控write | B3純化矩陣 |
| G14 同session快速改名／綁定／分支 | 寫入promise亂序解決＋producer結束 | 較新欄位／樹不被舊snapshot覆蓋；跨session仍獨立 | B3/B4屏障 |
| G15 我匯入多個對話 | 一筆保存失敗或unknown | 只報實際成功數／部分未完成，不顯示全成功也不整批重送 | useSessionEvents |
| G16 已確認刪除 | 舊保存／onDone延遲返回 | 不復活session，不覆蓋他人內容；並行屏障必可觀察 | B2/B3/B4整合 |
| G17 有pending/failed/unknown富樹 | 背景refresh返回舊資料，再經ChatArea/SessionContext寫入 | 回傳list及render/ref都保留draft，不只保護一個setState | refreshMerge／registry／ChatArea |
| G18 A proxy/user有排隊操作或GET | 切換B或logout，再完成舊promise／auth refresh／terminal | A內容不送B／不配B憑證，不套用舊結果；同ID不同scope隔離 | mutations／registry／connections |
| G19 trim摘要與full保存同時進行 | 分別讓UI先完成、trim先完成 | 前者trim丟過時摘要；後者UI明確conflict保留draft；任何順序不靜默覆蓋 | session_persistence_ordering |
| G20 我看到未保存／unknown | 點重新核對，再按條件允許的僅重試保存 | GET相等才saved；不同保留draft；單次conditional POST、不送模型，scope變更拒絕 | notice／registry／mutations |

每批先保存有效 RED 原件，再最小 GREEN；import error、fixture 錯誤或 runtime 前置失敗不是預期 RED。
故意令 SaveResult 丟失、terminal 被 parser 忽略、full-tree 保存未 await、updater 再呼叫 sync 的反例，
必須讓對應共同斷言 FAIL；不只驗新檔存在／mock 被呼叫，不用測試自己回報「已覆蓋」。
三層合成端到端：真 dispatcher／buffer輸出 → 同一 terminal fixture 的真 TS decoder／callbacks →
provider 寫入 mock／owned SQLite 回讀；含原 owner/agent、無痕與完整 tree。跨語言 fixture 由型別 SSOT 產生／比對，不能各造不一致事件求綠。

## 6. 原規劃歷史： 安全、SOLID 與驗證

風險 **HIGH**：改變任務與對話的保存承諾、刪除及人類／agent 資料歸屬，錯誤順序可能覆蓋內容；不表示已確認重大漏洞或事故發生率。
安全清單觸發前端資料儲存、資料庫／API、敏感資料與身分界線；不改密碼演算法、登入會話或 auth authority。
所有端點沿既有 fresh authenticated identity／`_can_access_session`；state／resume 權限與 DELETE 一樣不可洩漏他人 ID／終態。
資料操作沿 ResourceManager／現有參數化 SQL 與有限同 session 鎖；不新增無界查詢。錯誤固定 code，validation 不回 echo 原輸入。
不新增瀏覽器敏感資料持久化；credential／對話只在既有受管介面與本輪 memory 中處理。public example／合成 token 供測試，禁止讀真 .env／data／soul／私有設定。

SRP：模型定義結果，session provider 管順序，registry 管生成／富樹，notice只呈現；不把新邏輯塞入上帝元件。
DIP：使用既有 ResourceProxy／受管 storage/mock 接縫；前端HTTP沿fetchTouka。OCP／ISP採有限 typed discriminated results，
不建立泛化 adapter hierarchy。LSP 以成功、失敗、取消、無痕與原 passthrough 回歸驗契約；已有明確行為改變不偽稱零相容影響。

實作核准後的驗證順序：

1. 既有 `.venv` Python3.12 PATH first；原生 `python -B -m pytest <該批精確測試>`，使用 conftest sandbox。禁止 stdin／runpy／全域Popen包裝。
2. 每批 focused RED/GREEN、相關 TS/Vitest、精確Python Ruff check/format與SSOT；避免每小修重跑全部。
3. 最終凍結產品／測試／生成 bytes，再完整 `python -B -m pytest tests/ --basetemp=<新的owned目錄>`、standalone smoke，前後input manifest核對。
4. ToukaChat完整 Vitest、local `tsc --project tsconfig.json --noEmit`（含app/test）、ESLint；`guard:typecheck` 不是完整測試型別替代品。runtime按 package.json／guard preflight核對，不借ambient新版。
5. 全部守衛包含適用 audit；non-audit 與 audit 分批時，各自結果與適用性分別記錄。
6. Python runtime/build-test、Torch CPU/editable 與 Node 各 scope 稽核依授權執行；依賴變更或適用性不明則重新執行。舊 moderate 保留，不宣稱零漏洞。
7. schema/生成一致、全部必要文件與 MkDocs／diff check；Guard18生成檔跑前raw備份／後hash，不覆蓋他人漂移。既有warnings、skip及原FAIL逐項記，不能靠新增skip獲綠。

## 7. 原規劃歷史： 完成定義、回復及停止條件

本計畫實作完成需：T2–T5各有有效適用性判定及必要 RED→GREEN；G1–G20 全部可核對；完整回歸、靜態、
守衛含audit、型別、smoke與文件全部通過或逐項正式記為阻擋，不能以「大部分已完成」結案。
最終修改清單由各批核准paths與實際diff產生，核對無範圍外來源。

來源完成只證明這個實作的行為／契約；Git 提交、push、發布、部署與人工體驗各自需要授權。不要把T1 dev修復套到main／目前runtime。
回復以本次exact source patch與後續已授權commit的普通 revert為原則，不reset整庫、不改寫歷史；
本計畫不要求資料格式migration，不實際回復真對話／看板。記憶體未保存內容會隨頁面／程序消失的限制必須留在交付說明。

同方向兩輪無進展或同錯三次：停止盲試，先做根因分析；換批次或換工具都不重新計數。
平台拒絕不可換工具／說法繞過。
未知保存結果不得自動重送模型、刪資料或覆蓋檔案；真資料／外部writer／DB契約超出本範圍就先停 dependent 工作，交精確修訂。
無關風格、廣泛耦合與新功能記待辦；達驗收即收斂，不自動加下一輪稽核。

本 R2 選定保守刪除、失敗保留內容、唯讀核對與明確手動保存、無自動 retry、有限同Tab順序／內容revision及既有無痕。
