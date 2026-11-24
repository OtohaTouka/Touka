# ADR-0330：設定頁看得到本機小幫手開放的資料夾，並能縮小範圍

- 狀態：🚧 部分實作（2a：協議第 3 版、小幫手回報開放名單、配對的人在設定頁看清單、版本不符時說清楚是哪一邊；2b 還沒做：拿掉與改成只能讀、已開放的直接回、包住上一層的說明，協議第 4 版）。一頁計畫由維護者核准（2026-11-24，總覽板第 103 項），細節設計審三輪。
- 日期：2026-11-24
- 決策者：維護者
- 相關：[ADR-0312](0312-local-helper-for-host-files.md)（本機小幫手第一版；本 ADR 修訂它的決定 3）、[ADR-0317](0317-local-helper-follows-touka-approval-mode.md)（配對鑰匙與蓋章；2b 修訂它的決定 1、2）、[ADR-0320](0320-ask-the-local-helper-to-open-a-folder.md)（開放請求；本 ADR 修訂它的決定 2、3 與「不採用」最後兩條）
- 影響範圍（2a）：`ToukaLocal/touka_local/`（`folder_report.py` 為新檔；`protocol.py`、`grants.py`、`connection.py`、`service.py`、`view.py`）、`ToukaLocal/install.py`、`ToukaProxy/src/models/local_bridge.py`、`src/services/local_bridge.py`、`src/routers/local_bridge.py`、`scripts/endpoint_auth_admin_required.py`、`config/errors.json.example`（`LOCAL_HELPER_PROTOCOL_MISMATCH` 訊息）與重產的 `ToukaChat/src/lib/error_codes.ts`、`ToukaChat` 設定頁（`useLocalHelperFolders`、`LocalHelperFolders` 為新檔；`LocalHelperTab`、兩份翻譯）

## 背景

試用時，維護者在設定頁請小幫手開放一個資料夾，Touka 說沒有開放，小幫手主視窗卻顯示開放中：那個資料夾早就開放了，重疊所以被拒絕。設定頁看不到小幫手開放了哪些資料夾，只能到小幫手主視窗看；小幫手 0.4.0 退到背景後，主視窗平常是收著的。

## 決定

### 1. 協議第 3 版；2b 再升第 4 版

- 輪詢的請求與回應欄位都不變，只改版本號。舊小幫手送 `protocol: 2` 通過欄位驗證，在 handler 收到 409，變成「版本不符」；不會因為新欄位被 422 擋下而被當成離線。
- Touka 在驗過權杖、回 409 之前，記下小幫手送來的版本號（只收 1～99）。`/status` 多一個欄位 `version_problem`：20 秒內有版本不符的輪詢、而且現在不是連線中時，是 `helper_older` 或 `touka_older`，否則是 `null`；換配對時清掉。只有權杖對的小幫手留得下紀錄。這不是敏感資訊，其他管理員看到也沒關係。設定頁據此說要重裝小幫手還是更新 Touka，不再顯示「小幫手沒有開著」。
- 小幫手分不出是自己舊還是 Touka 舊，主視窗的說法（`view.py`）與 `LOCAL_HELPER_PROTOCOL_MISMATCH` 的訊息改成兩種都說：先重裝小幫手，重裝後還是這樣就是 Touka 要更新。
- 2b 會改到工作與結果的格式，到時再升第 4 版：2a 之後的 dev 就有一個能安裝的第 3 版小幫手，兩個版本混用時也由 `version_problem` 說清楚，不會收到不認得的工作或結果。

### 2. 小幫手回報開放名單：`POST /local-bridge/folder-report`

- 認證同輪詢：小幫手權杖，只代表這條通道，不轉成身分（禁令 16）。回 200（小幫手只把 200 當成送到）。
- 內容 `{protocol, instance, revision, exposed, folders, omitted}`，每筆 `{name, path, writable, usable}`。
  - 最多 `MAX_REPORTED_FOLDERS = 64` 筆；名稱 300 字、路徑 `MAX_REPORTED_PATH_CHARS = 1024` 字以內；不收控制字元、改變顯示方向的字元與單獨的代理字元。不收的字元是同一份 `UNSAFE_TEXT_PATTERN`（Touka 的 `models/local_bridge.py` 是準，小幫手另存一份，parity 測試核對）。
  - 小幫手送出前先照同一組規則過濾，不合的與超過 64 筆的只算進 `omitted`：手改過的名單有一筆壞資料，整份也不會被拒。設定頁說「還有 N 個沒列出，請看小幫手主視窗」。
- `instance` 是小幫手每次啟動產生一次的亂數；`revision` 是這次啟動以來名單存檔成功的次數（`GrantBook.revision`）。Touka 收到不同的 `instance` 一律取代；同一個 `instance` 只收不比手上舊的（相同的是重送）。較舊的照樣回 200、不存：小幫手比的是「上次送成功的修改次數」，下一次變動或重送就會補上。
- 什麼時候送（`touka_local/folder_report.py`，只在輪詢的執行緒）：輪詢成功之後，名單變了、這次連線還沒送成功過（剛連上、重連、換配對）、或距上次送成功滿 30 秒（暫停中可能因為隨身碟拔掉而變）。送失敗等 30 秒再試，不是每一輪都重送。小幫手被看見（exposed）時照送，並註明。送不出去不影響輪詢。
- 清單從 `GrantBook.snapshot()` 拿：修改次數和名單在同一把鎖裡一起取，鎖外才算每一筆現在能不能用。`usable` 讀開放資料夾本身與它的每一層上層，和模型的 `list`、主視窗的暫停判斷相同。
- Touka 只放記憶體（重啟就沒有），換配對時清掉，別的配對世代的回報丟掉。

### 3. 配對的人本人看清單：`GET /local-bridge/folders`

- 人類管理員（`require_admin_human`），而且是產生配對碼的那位（和開放請求共用同一個比對，ADR-0312 決定 5）：別人回 403 `LOCAL_HELPER_NOT_PAIRED_HUMAN`，沒配對回 409 `LOCAL_HELPER_NOT_PAIRED`。登記在 `endpoint_auth_admin_required.py`。
- 回 `{state, exposed, folders, omitted}`：`state` 是 `offline`（小幫手沒連線）、`waiting`（連線中還沒收到回報）、`stale`（超過 75 秒沒有新的回報）或 `ok`，只有 `ok` 時有清單。
- 回應帶 `Cache-Control: no-store`。不放進 `/status`：其他管理員也看得到 `/status`。端點與服務都不把清單內容寫進日誌（測試釘住）。
- 設定頁（`LocalHelperFolders`）：配對了、小幫手開著時顯示；每筆有名稱、完整路徑（可以選取複製）、「只能看」或「可以改」、暫停中的標記；被看見時警告；寫明以小幫手主視窗為準，並說怎麼叫出主視窗（從桌面或 `%LOCALAPPDATA%\ToukaLocal` 開 `ToukaLocal.cmd`）。每 10 秒更新；不是配對的人看到一句說明，之後不再重複讀。清單只放在記憶體，不存進瀏覽器。

### 4. 2b 預定（細節設計已審，還沒實作）

- **拿掉、改成只能讀**：`POST /local-bridge/folders/revoke`，參數 `{name, path, change}`（`change` 只能是 `remove` 或 `read_only`），配對本人、信任的 Origin。Touka 送一件 `revoke` 工作，用配對鑰匙蓋章：`act` 是 `revoke`、`by` 是 `owner_request`、另帶 `change`，`digest` 是路徑的指紋、`shown` 是名稱的指紋（都是 ASCII，三方算得出同一個值）。小幫手驗章後，名稱與路徑都對上才縮小，沒有任何路徑能放大範圍；不跳視窗、不算停收；寫紀錄檔（只寫名稱）並在主視窗說明。成功時 Touka 同步改記憶體裡的清單與修改次數，晚到的舊回報不會把清單蓋回去。
- **已開放的直接回**：開放請求的路徑等於或在某個開放資料夾裡面（只比路徑文字，含暫停中的；多出來的那幾段有受保護的名稱或系統資料夾就不算），小幫手不跳視窗，直接回 `already_open` 與目前權限。
- **包住已開放資料夾的上一層**：照舊跳視窗，是「不能開放」那種，視窗與設定頁都寫包住了哪些（只比路徑文字，不讀磁碟）；要開放上一層，至少要先拿掉它們。

## 後果

- 開放資料夾的完整路徑第一次常駐在 Touka（記憶體）。路徑裡有使用者名稱，算個人資料：只給配對本人；不給模型、工具結果、日誌、聊天串流、瀏覽器儲存與 HTTP 快取。模型的工具照舊只看得到名稱。
- 被攻破的 Touka 多知道開放資料夾在電腦上的位置（它本來就能用工具讀這些資料夾的內容）。它可以不理會或亂顯示回報，影響只在設定頁；小幫手照自己的名單做事，名單仍在使用者電腦上、Touka 換不掉（2b 之後能縮小，見下）。
- 假冒小幫手的程式要有小幫手權杖才能回報；最壞是設定頁顯示錯的清單。
- 已接受的風險：Touka 沒開時佔住同一個埠的程式（ADR-0312 已接受它拿得到權杖）現在也會收到完整路徑清單。
- 2b 之後：被攻破的 Touka 有鑰匙，可以把名單全部拿掉或改成只能讀，小幫手少做事；被拿掉的資料夾在主視窗「復原」會跳過，要先加回（同名）或改回可以改才能復原。拿到鑰匙檔的人可以在 Touka 沒開時默默清空名單。

## 不採用

- **清單放進輪詢的請求**：新欄位一定要帶時，舊小幫手會收到 422、被當成離線，設定頁也說「沒有開著」。改成另一個端點，輪詢欄位不變。
- **清單放進 `/status`**：其他管理員也看得到 `/status`，而完整路徑只給配對本人。
- **只用修改次數判斷新舊**：小幫手重開後從 0 算，Touka 會一直拒收新清單。加上每次啟動不同的編號。
- **請求上一層時核准後合併**（2b）：被併進去的資料夾在主視窗「復原」會失效，名稱可能被新的一層沿用，權限也會被默默改掉。
- **2a、2b 都用協議第 3 版**：兩個 PR 之間能安裝的版本混用時，會收到不認得的工作或結果。
