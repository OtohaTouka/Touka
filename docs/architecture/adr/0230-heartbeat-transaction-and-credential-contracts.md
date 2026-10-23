# ADR-0230：Heartbeat 持久化交易、真人管理與服務憑證契約

- 日期：2026-09-16
- 狀態：維護者核准計畫；B2／B3 階段經審查接受；B6必要來源驗證完成，來源實作與驗證已通過最終審查（審查原件未入版控）。
- 範圍：Heartbeat／Proxy human bridge／Chat 排程管理；來源候選，未發布／未部署。
- 前置：[ADR-0229](0229-trusted-tool-execution-classification.md)；[Heartbeat 介面](../HEARTBEAT_INTERFACE.md)。

## 問題

舊 StorageService 將缺檔、讀取與解析失敗合併成預設設定，可能以新 token 或空排程
覆蓋損壞原件；持久檔與 scheduler 更新缺乏同一失敗／補償邊界。
Chat 原本將人類 JWT 送往只接受服務 secret 的 Heartbeat，且路徑、回應與更新方法不一致。
管理端讀 StorageService token，而 outbound client 另持啟動時常數，fallback 可能出現身分裂腦。

## 決定

### 領域模型與出貨來源

`ToukaHeartbeat/models.py` 是 stdlib／Pydantic-only SSOT。排程及設定驗證保留未指定欄位與
合法 extensions；拒絕無效工作、重複 schedule ID、同一排程等價 daily 時間與非有限 JSON。
舊合法 `9:00`／`09:00` 拼法保持，不能藉型別化默默改寫既有檔案。
`scripts/sync_heartbeat_models.py` 產生 Proxy 投影，禁止人工雙編或 import HB app/settings。
OpenAPI 生成器合併獨立 HB app；同名 schema 只有內容一致才合併，否則 fail-loud。

錯誤定義的交付來源為 `ToukaProxy/config/errors.json.example`，由 `sync_errors.py` 生成
Heartbeat／Chat 所需代碼。Proxy loader 先取出貨定義，再套選定 registry 的明確覆寫；
舊 registry 缺的新定義只在記憶體補齊，不覆寫設定，也不探測第二條私有 fallback。

### 持久化與排程交易

StorageService 只在確認 filesystem entry 不存在時初始化；dangling symlink、讀取或解析錯誤
保留原件並明確失敗，不生成 token。保存採同目錄暫存、flush、fsync、close、replace、
directory sync；`committed`／`durability` 明示已知、未知與失敗，不以 exit 0 模糊覆蓋。

ScheduleService 用單一 transaction lock 串起讀取／驗證 revision／提交／更新引擎。
變更前先建立完整合法 job plan；job ID 使用長度界線避免 `foo`／`foo_bar` 等互相刪除。
舊 jobs 保存實際 trigger、next_run_time 與選項，不按補償時刻重建 interval。
套用失敗只對受影響 jobs 與原 bytes 補償一次；恢復不能證明時進 degraded／recovery_required。
runtime 無法重新讀取或驗證持久檔時，persisted state 為 UNKNOWN。

degraded 嘗試 pause 引擎；pause 失敗仍記 engine UNKNOWN。每個新 job 的薄 callback
在同一 lock 下檢查 degraded，故此後 outbound 為零；不能宣稱物理 pause 必定成功，
也不能撤銷故障前已獲准或進行中的 callback。

### 真人管理橋接

Proxy 提供 `GET/POST /v1/heartbeat/schedules` 與 `DELETE /{schedule_id}`，只使用 fresh
`get_current_user`。一般使用者只管理 owned agents，admin 可全域；X-Agent-ID 不增加權限。
更新既有 ID 同時驗證舊及新 agent；隱藏／不存在 ID 的刪除均 NOT_FOUND。

HB GET 回原 schedules-only canonical revision；bridge 所有 mutation 必須帶 If-Match，
HB 在交易 lock 內 CAS 檢查，衝突 409 且副作用零，包含查無 ID 後被併發建立。
既有 secret caller 可不帶 If-Match，保留相容性。
Proxy GET 將已驗證預設 active/mode 等輸出給 UI；HB 原 bytes／presence／opaque ETag 不改。

bridge 只連固定驗證 origin，拒絕 userinfo/query/fragment、不跟 redirect、單送、整體 10 秒。
空 secret 不送 HTTP；service 401/403 不偽裝成人類登入失敗；mutation timeout 回結果未知。
新 schedules 路由的 validation error 不回傳原 input/content/ctx，其他舊路由錯誤契約不擴改。

### 單一服務憑證解析

StorageTokenProvider 同時提供管理認證、手動 trigger 與 ProxyClient。
每次 resolve 一次取得 value/source/shared_ready；token 欄位不進 repr。
非空共享設定優先，其次持久 token，最後產品生成；fallback 僅保障本機管理相容。
shared setting 未就緒，trigger 不建立工作，client 在讀 fallback／建 HTTP 前拒絕。
本機 shared_ready 不代表遠端健康或接受憑證。

ProxyClient 單次 HTTP、無 redirect／retry、整體 10 秒；失敗只記固定安全原因。
Proxy 原 plain-secret 及合法 `sub=heartbeat,type=service` JWT 驗證維持；空 shared secret
不使合法 service JWT 失效。支援的是兩服務重新載入設定後的一致性，不承諾 hot rotation。
owned A→B→C 三次 fresh process 已驗真正 Proxy config/router 與 HB settings/provider/client
消費同一持久設定，新值接受、舊值拒絕；未手填消費端常數。

### UI 與安全驗證來源

Chat 只透過既有 hook 呼叫 Proxy、帶 ETag，保留失敗草稿與舊列表；daily payload 複製陣列。
已儲存但刷新失敗與未確認儲存區分，重新整理不重送 mutation。未知原始錯誤只顯示固定 fallback，
已知訊息沿生成代碼。`sync_to_clock`／`max_runs` 不新增功能、不宣稱成功生效。

Guard32 驗必要出貨權限／風險範本與 fallback，Guard37 驗必要 hub 範本與明列 shipped declarations；
不讀私有設定、不 glob 私人宣告，缺失或格式錯誤 FAIL。smoke 強制 owned data/HB config，
產品 import 前保護 root logging，保留既有 capture；自動化沒有 live 例外。
Guard24按同檔module-level APIRouter變數及本地include鏈解析每個decorator的prefix；
明示動態prefix、展開kwargs、重新指派、未知receiver、循環／多父節點拒絕，
不修改既有auth／admin／allowlist清單，不宣稱能推導任意動態Python。

## 驗證、限制與回復

B2 130P、UI19P；B3 135P（含真正 fresh restart）；B6 目前 CI selectors512P、
source/smoke安全前置76P。B6 final full 9862P／9skip／5warnings，425.71秒、43fresh guards＋2audit REUSED_NOT_FRESH、24smoke、Chat1241P及78Python靜態通過；最終來源與doc-only閉包記錄在當時的交班文件（現在只留在開源前的私人歷史），不套歷史PASS。
原 RED、fixture／匯入錯誤及前置環境失敗分開保存；資料 I/O 歷史不作未存取保證。

F05 workflow 只承諾 SOURCE_READY：核心必跑、前端保守分類、最小 read-only 權限、精確來源與
命令／輸出／exit artifact。hosted NOT_RUN、required checks NOT_VERIFIED；本機額外 Bash
capture 為 BLOCKED_BEFORE_CAPTURE，OS 原因 UNKNOWN，非事故已修復。

來源回復需核對逐批 before bytes 與 exact patch，保留前次44檔成果；禁止整庫 HEAD 還原。
未執行 Git mutation、部署或資料 migration；來源回復不等於實際 runtime rollback。
