# ADR-0311：本機小幫手：代理人碰使用者電腦上的檔案只經一支固定動作的程式

- 狀態：🟡 第一版試行中。一頁計畫由維護者核准，經三輪審查。
- 日期：2026-11-10
- 決策者：維護者
- 相關：[ADR-0194](0194-mcp-remote-shelf-and-manual-remote-entry.md)（MCP 網址的安全閘）、[ADR-0272](0272-iso-c2-agent-command-isolation-and-executor.md)（隔離環境與封鎖埠）、[ADR-0085](0085-ask-user-tool.md)（等人回答的做法）、[ADR-0269](0269-advertised-tool-list-enforcement.md)（只用這一輪列出的工具）
- 影響範圍：`ToukaProxy/src/models/local_bridge.py`、`src/services/local_bridge.py`、`src/services/local_bridge_pairing.py`、`src/routers/local_bridge.py`、`src/tools/builtin/local_helper.py`、`src/utils/local_bridge_gate.py`、`src/utils/local_bridge_token.py`、`src/models/safety.py`、`src/middleware/layers/tool.py`、`src/handlers/react_core.py`、`src/services/tool_executor.py`、`src/services/permission_service.py`、`src/services/authorization_gateway.py`、`src/tools/contracts.py`、`src/adapters/openai_to_gemini.py`、`src/config.py`、`ToukaChat` 設定頁、`ToukaLocal/`

## 背景

使用者希望代理人能整理電腦上的檔案、建立和刪除檔案與資料夾。Touka 跑在 Docker 裡，把使用者的資料夾掛進容器是最直接的做法，但掛進去的資料夾容器不用突破隔離就碰得到，代理人寫的 hook、編輯器設定之後也會在電腦上沒有隔離地執行。維護者比較過後，決定改由使用者電腦上的小幫手程式處理：容器不掛使用者的資料夾，只能請小幫手做幾個固定動作。先做第一版試行，試行後再調整。

## 決定

1. **能力分開**：容器裡可以跑任意程式但碰不到電腦；電腦上只有固定動作，不能執行程式。兩種能力不給同一方。
2. **小幫手主動連 Touka，電腦上不開連線入口**：長輪詢連到已經發布的 proxy 埠。不走 MCP：Touka 的網址安全閘本來就擋 `host.docker.internal` 明文連線（ADR-0194），放寬等於弱化既有防線；協議也固定在 modern 版本。
3. **開放名單和核准都在使用者電腦上**：名單存在使用者資料夾；清單由小幫手自己檢查檔案產生，雜湊綁定快照，Touka 被攻破也換不掉清單、按不到核准。
4. **動作分級**：
   - 看清單、讀文字檔：資料夾有開放就不問。
   - 新建文字檔或資料夾（不覆蓋）：資料夾要「可以改」；Manual 按 Touka 卡片，Auto、YOLO 不問。
   - 搬移、改名、刪除：三種模式都要在小幫手視窗按核准。
   - 第一版不能修改或覆蓋既有檔案。
5. **只給配對的人本人和靈魂的前景對話**：`utils/local_bridge_gate.py` 條件和使用者各自授權的 MCP 相同，但刻意另寫一份，MCP 放寬時這裡不跟著放寬。心跳、社群、派工、子代理、群聊全部拒絕，連讀取也是。`Subagent` 角色沒有這個工具。
   - 靈魂有自己的工具清單時要勾了才有，不算必備工具。碰使用者電腦的能力不繞過這份清單。
   - 無痕對話不提供：小幫手的紀錄會留下路徑和說明。
   - 靈魂要沒有別的擁有者。別人擁有的靈魂看得到自己的對話和記憶，讀進來的檔案就等於給了那個人。
6. **風險分類**：`list`、`read`、`propose` 是 `read_only`（比照 `ask_user`；`propose` 由人在小幫手核准，不再多一張 Touka 卡），`create_*` 是 `safe`。`BUILTIN_TOOL_RISK` 只在設定檔成功載入、又沒列到這個工具時併進去；設定檔讀不到時退回預設，仍是 `dangerous`。
7. **權杖**：明文只回一次，Touka 只存 SHA-256 與配對的人。標頭 `X-Touka-Local-Token` 只代表這條通道，不轉成任何身分（禁令 16）。換新或解除時世代號加一，掛著的輪詢立刻作廢，待處理的工作全部失敗。
8. **兩段確認與期限**：
   - 讀類 30 秒。
   - 清單等核准 600 秒，小幫手用自己的時鐘在 540 秒自動拒絕，不會發生 Touka 已經回「沒核准」、小幫手才按下去。
   - 按核准後小幫手先 `confirm`，Touka 回 200 才動手；工作被取消、逾時、Touka 重啟、換權杖時都不是 200，不會執行。
   - 執行階段小幫手限 300 秒，Touka 等 330 秒，比小幫手寬，結果才送得回來。等到逾時回「結果不明，不要重試」。
   - 確認後使用者按停止：做完當前項就停，照紀錄回報。
   - 使用者在聊天按停止時，Touka 只把這個工具的背景工作一起收掉，等核准的清單會撤回、執行中的清單會叫小幫手停；其他工具維持原本的行為。
   - 失敗要照實說。可能已經動手的情況（清單已確認、新建檔案或資料夾已交給小幫手），逾時、小幫手離線或換配對都回「結果不明，不要重試」；讀取類逾時回「沒有回應，沒有改動」；清單等不到人回「沒有決定，沒有改動」。
   - 長輪詢最多 5 秒。Docker 預設 10 秒就強殺，還要留時間給關機清理。
9. **主機掛載根只用來多擋**：Touka 把 `HOST_WORKSPACE_ROOT`、`SANCTUARY_DATA_PATH` 傳給小幫手，和它們重疊的資料夾不給開放，否則代理人可以不經小幫手直接動那些檔案。POSIX 絕對路徑是容器裡的預設值，當作取不到。Touka 給的值只會讓小幫手多擋。
10. **小幫手不從 Touka 資料夾安裝**：repo 和 `.venv` 都在容器寫得到的地方。安裝時從 GitHub 取審過的版本，放到使用者資料夾，用 repo 以外的 Python，以 `-I -S` 啟動。
11. **`ToukaLocal/` 不走 Touka 的 SSOT**：程式在容器外跑，載入 `src` 會帶進依賴和初始化寫入（比照 `sandbox_kernel` 只用標準函式庫）。適用範圍逐條寫明：
    - 禁令 7：照樣用 `logging`，不用 `print`。
    - 禁令 10、44：有自己的錯誤代碼，不用 `ToukaException`。
    - 禁令 41：路徑一律來自開放名單與使用者資料夾，不寫死。
    - 禁令 42：只讀 Windows 的使用者資料夾環境變數。
    - 禁令 43：拿不到 `time_utils`，時間集中在一個 `Clock`。
12. **用 `SHFileOperationW` 進回收桶**：官方建議改用 `IFileOperation`，但那是 COM 介面，用標準函式庫呼叫太重。第一版用舊 API，每項一次呼叫。

## 後果

- 讀取不跳視窗。檔案裡的惡意文字可能誘導代理人把讀到的內容用上網工具送出去，這條路不經小幫手；所以資料夾要開得窄。
- Touka 被攻破時，開放資料夾的內容讀得到，也能多出文字檔，不能搬也不能刪。
- 核准要人坐在電腦前。
- 只有 Windows。OneDrive 佔位檔是 reparse point，第一版拒絕。
- 配對資料一次只有一組；Touka 假設單一行程，佇列只在記憶體。
- 小幫手自己的設定和紀錄不能用會覆蓋的改名（能力邊界禁掉了），所以是暫存檔落盤、刪舊檔、改名三步；當機在中間時，讀的人改讀暫存檔。
- 紀錄寫不進去時，這批停在當前項，後面的回報沒做；沒有紀錄的搬移沒辦法復原。
- 代理人可以一直提清單讓視窗反覆跳出：目前只靠一次一個視窗、拒絕或撤回後冷卻 5 秒來節流，沒有封鎖機制。

## 不在這次範圍

修改既有檔案、依內容搜尋、其他作業系統、安裝包、英文介面；「讀過本機檔案的對話，對外動作要人核准」；網路閘道、專案專用空間、金鑰閘道；收緊預設掛進去的工作區與專案裡的直接刪除。
