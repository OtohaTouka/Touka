# ADR-0231：任務與對話持久化結果、條件提交及恢復

> ADR-0276 修訂對話條件提交：儲存交易內增加精確歸屬檢查，UI 串流准入可由授權人類換綁並傳遞提交後 revision；assistant 最後保存無換綁權。內部 marker 納入 revision，部署後須重讀 snapshot；原保存結果型別沿用。

- 日期：2026-09-21
- 狀態：Owner DEV-A approved／SUPERVISOR_DELEGATED；來源實作與完整Python R4完成，最終DEV-B以本輪`ROOT_DEV_B_ACCEPTANCE_R1.json`為準。
- 範圍：Memory 任務保存、Proxy 明示 UI session 契約、Chat writer／串流／恢復。未發布、未部署。
- 計畫：[可靠性 R2](../proposals/reliability-persistence-plan.md)。既有 T1 Heartbeat 保持 ADR-0230，不重做。

## 問題與取捨

舊任務寫入例外只記錄後仍回成功；對話 EOF 與資料保存沒有獨立結果，前端直接送全樹可能覆蓋較新資料。
React updater 的 HTTP 副作用可在重播時重送，刪除與匯入也可能在保存未確認時宣告成功。
只加 FIFO 或只檢查 DB 鎖，無法判定來自舊畫面的全樹是否仍有效。

## 決定

1. Memory 的 `MissionPersistenceError` 保持固定安全文字，由既有工具橋接自然傳播；Proxy 才映射
   既有 1302／`outcome=unknown`。受管寫入完成後才更新呼叫端時間戳；任務 review、identity、鎖及
   Dispatch token／invoke 順序不變。不把一般 I/O 例外當作確定未提交，也不全面重構 Memory 讀取。
2. 純 `src/models/session_persistence.py` 定義 SaveResult、snapshot、started／terminal；沿共同
   OpenAPI merge → api.ts → types.ts alias。saved 必須含 revision／representation；failed、unknown
   不附舊 token／原輸入。snapshot 的 exists 是必要 boolean，present／absent 內容互斥。
3. `async_db.py` 對選定 session 路徑 strict 讀取原 row，將 14 欄原始 readset、presence、owner、
   requester、process epoch 與 session generation 綁到不透明 revision。同一 SQLite transaction
   使用 BEGIN IMMEDIATE 完成 read／compare／write；舊 token、重啟、刪除重建或其他 writer 變更皆拒絕。
   不新增 DB migration，不將 Python FIFO 宣稱為 CAS。其他非 conditional writer 不獲得本次完整保證。
4. 正常 GET 保留形狀與 derived round_count；明示 `snapshot=true` 才提供 typed 存在／缺席。
   POST 必帶 expected revision，既存 DELETE 必帶 If-Match；strict 確認已缺席的 DELETE 仍 200。
   省略欄位保留、合法 null／空列表明確清除；auth_level:null 仍是不變更／新建 Manual。
   owner、agent、project 與安全級原授權不變；token 不是認證。讀取毀損／提交不明為 unknown。
5. 只有明示 `ui_persistence` 且已授權的 UI request 使用新 producer 契約。pipeline 前以同一
   snapshot 綁 owner＋revision，dispatcher admission 再檢查 user 節點與基底；不一致時 model0。
   admission requester 與後續 soul 執行身分分開。內部 revision／UI envelope 不送外部 provider。
   `_pump_ui_stream`／`_persist_ui_assistant_turn` 負責新契約；未明示的既有外部路徑不擴承諾。
6. buffer 在單一 event loop 以無 await 原子 transition 同時記 terminal／status 並通知 waiter；
   append／attach／finish／replace／stop 帶 stream fence。generation 與 persistence 分開：fatal
   RoundError 是 error、rhythm interruption 是 cancelled；不自動保存部分內容或 trim。
   完成保存 text 後才允許富樹補存；no_text 且有 steps／tools／attachments 仍需 full 保存。
   trim 保持非阻塞，但以原 snapshot conditional commit，不能用舊整樹蓋掉 UI 新內容。
7. 持久串流 state／resume／stop／active 同時核 requester、fresh DB 存在與原 ownership；無痕只用
   經認證的 memory owner，DB0。受控刪除退休精確舊 stream，晚到 producer 不可寫入重建的新代。
   完成 buffer TTL 600 秒、上限與 orphan 保護維持；server 重啟會失去 memory buffer，未保存草稿
   關閉頁面可能遺失，不能承諾持久恢復。
8. Chat 每 scope／session 以一個有限 writer 保存語意命令，operation 去重。state/ref 使用同一純
   transition；public ingress 不是保存證據，變更持久欄位須重新核對內容，不能借 fresh token 背書
   舊樹。pending／failed／unknown／unconfirmed 草稿不被列表、refresh return 或 resume 蓋掉。
   user full 保存後才送模型；React updater 不做 HTTP。純轉換拋錯回 typed failed，釋放 pending／
   producer，保留可核對草稿；無痕錯誤不永久鎖住下一回合。
9. scope 綁 proxy＋credential principal／epoch，wire／refresh／body await 後再驗；scope 失效
   best-effort abort，清除 writer、history、輸入、attachment、Debug payload 與 artifacts。
   JWT subject 只作 client 排隊錨點，不替代 server auth。selected URL session 清單載入後才消耗
   一次 state 核對；已完成 TTL stream 亦可接回，不 fan-out 或 poll。真 UTF-8 decoder 以同 generation
   started／terminal 判斷，DONE／EOF 不證保存，游標重播可用已授權 started 座標。
10. unknown 只提供唯讀「重新核對保存結果」，相同內容才確認 full；不同保留草稿及 conflict。
    取得有效 revision 且明確 failed 後，使用者可「僅重試保存」單送一次 POST，model／tools0。
    recheck／retry 不競逐，慢回應不能覆蓋較新 intent。刪除 await 合法 200 才移除；無痕零 HTTP。
    split 綁 export 前依賴內容，等待期間 drift 拒絕縮減；無痕拒絕 split 縮減、改用本機匯出。

## 驗證與限制

shared `tests/fixtures/reliability_session_stream.json` 穿過真 Python producer／buffer、真 TS decoder／
registry／writer與 owned SQLite，保留 extension、null、owner／agent及完整富樹；只 alias 不穩定的
stream_id／revision，破壞 terminal／富欄位的反例必失敗。故障、並行、scope、無痕與人工恢復另有 focused 測試。
完整Python R4為10048P／9個既有條件skip／5warnings，凍結來源／runtime零漂移。Chat1409P／243檔、typecheck／ESLint／smoke24/24按最終適用性重用，43非audit與兩audit原查詢回讀分列，詳細材料見`COMPLETED_RECEIPT_R1.json`；DEV-B由Root另存原件裁決，不宣稱嚴格獨立資格。
歷史R3失敗、incognito背景trim fixture修訂、Windows cleanup兩次FAIL及其後NOT_REPRODUCED全部保留。ReadFile109的EOF契約缺漏已決定性RED→GREEN後修正、整檔與完整R4通過；歷史兩FAIL原因仍UNKNOWN，原Thread938唯一來源仍NOT_PROVEN。
原`BLOCKED_RECEIPT_R2`只識別修訂前101檔中斷材料，不包含新的EOF修復，不可用作目前來源驗收或回復入口。

9/16 Temp 原件已遺失；歷史 RED／批准副本不得重建冒充。9/21 resume baseline 保存當前 60 檔，
新原件位於 `.cache/reliability-resume-20260921-r1`，搬移清單見 RELOCATION_R1；對話 DEV-A 仍有效。
AnyIO 官方 patch 4.14.2 為本輪 audit 發現的有限依賴修正；原 FAIL 保留，不做漏洞重現。
來源驗證不是 live／發布／Owner GUI 見證。回復只審閱精確來源 patch後普通 revert；不回復或管理真資料。
