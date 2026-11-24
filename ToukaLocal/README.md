# Touka 本機小幫手

讓靈魂在你開放的資料夾裡看檔案、新建文字檔，或提出搬移與刪除清單給你核准。設計與邊界見 [ADR-0312](../docs/architecture/adr/0312-local-helper-for-host-files.md)。

目前只有 Windows（64 位元 x64），介面只有繁體中文，只用 Python 標準函式庫。

## 安裝

> **只從 GitHub 官方頁面複製下面那一行**：<https://github.com/OtohaTouka/Touka/blob/main/ToukaLocal/README.md>（和你執行的 Touka 同一個分支，一般是 main）。這一行會在你的電腦上、Docker 外面執行程式。你電腦上 Touka 資料夾裡的這份檔案，Docker 裡的代理人改得到；靈魂或聊天裡給的指令也不要貼。

1. 小幫手開著的話，先結束它：在小幫手主視窗按「結束小幫手」（0.3.0 以前的版本直接按右上角 ×）。主視窗收起來看不到時，從桌面（或 `%LOCALAPPDATA%\ToukaLocal`）開 `ToukaLocal.cmd` 叫出來。
2. 開一般的 Windows PowerShell（不要用系統管理員，也不要用 PowerShell ISE），貼上這一行，按 Enter：

   ```powershell
   $d=Join-Path $env:TEMP ("touka-helper-"+[guid]::NewGuid()); New-Item -ItemType Directory $d | Out-Null; $f=Join-Path $d 'install.ps1'; [Net.ServicePointManager]::SecurityProtocol=[Net.ServicePointManager]::SecurityProtocol -bor 3072; Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/OtohaTouka/Touka/0ad0b979e3761b47c4ee2cd61ff0835cb84927a1/ToukaLocal/install.ps1' -OutFile $f; if ((Get-FileHash $f -Algorithm SHA256).Hash -eq '86A30E2A7D9B21AD934625392B714C3632BDBE60BD4AA7A4B3796FB4A3B77395') { powershell -NoProfile -ExecutionPolicy Bypass -File $f -Commit '0ad0b979e3761b47c4ee2cd61ff0835cb84927a1' -WorkDir $d } else { Remove-Item -Recurse $d; Write-Error 'install.ps1 does not match' }
   ```

3. 它會問 Touka 資料夾在哪（完整路徑，例如 `D:\Touka`；重裝時按 Enter 沿用上次的），以及要不要開機自動啟動。小幫手會記下 Touka 資料夾，不讓你把它開放出去。
4. 用桌面上的 `ToukaLocal.cmd` 啟動（第一次會顯示主視窗，讓你貼配對碼）。

**它做什麼**（[ADR-0329](../docs/architecture/adr/0329-one-line-install-for-the-local-helper.md)）：先下載安裝腳本，比對指令裡的指紋（sha256），對了才執行。腳本再下載 Python 3.14.10 的官方壓縮檔（約 33 MB）和小幫手的每個檔，逐一比對指紋，任何一個不對就停下，什麼都不裝。Python 放在 `%LOCALAPPDATA%\ToukaLocal\python-3.14.10\`，小幫手放在 `%LOCALAPPDATA%\ToukaLocal\app\`。不需要管理員權限、不改系統設定、不寫登錄檔，也不碰你電腦上原本的 Python。

**怎麼確認這一行是官方的**：在瀏覽器打開 `https://github.com/OtohaTouka/Touka/commit/` 加上指令裡那串 40 字的 commit，頁面要顯示它在 main（或 dev）上，不能有「This commit does not belong to any branch on this repository」的警告。這只能確認版本，看不出這一行其他地方有沒有被改，所以一定要從官方頁面複製。

**版本不符時**（Touka 或小幫手顯示「版本不符」「不相容」）：先把 Touka 更新到 main 最新，再照 GitHub 上 main 的這個頁面重新安裝。配對和開放的資料夾會保留。

### 疑難排解

- 顯示「指令碼執行已停用」或群組原則鎖定了執行原則：公司或學校的電腦可能不允許，請找電腦的管理員。
- 顯示「受限語言模式」：這台電腦的 PowerShell 被限制，不能用這個方式安裝。
- 顯示「小幫手正在執行」：在小幫手主視窗按「結束小幫手」（0.3.0 以前的版本直接按右上角 ×）再貼一次；主視窗收起來時，從桌面（或 `%LOCALAPPDATA%\ToukaLocal`）開 `ToukaLocal.cmd` 叫出來。
- 開機自動啟動時，主控台視窗會閃一下；已經配對的話，小幫手在背景啟動，不顯示主視窗。

### 解除安裝

先在小幫手主視窗按「結束小幫手」（不然資料夾刪不掉），再刪掉 `%LOCALAPPDATA%\ToukaLocal` 資料夾（裡面也有換版本後留下的舊 Python），以及桌面和「啟動」資料夾（`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`）裡的 `ToukaLocal.cmd`。

### 進階：不用一行指令

1. 把 GitHub 上的 repo clone 到 Touka 資料夾以外的地方，切到要安裝的版本，確認 commit 和 GitHub 上顯示的相同。
2. 用 Touka 資料夾以外的 Python 3.14（要有 tkinter，不要用 `.venv`）執行 `python -I -S ToukaLocal/install.py`，可以加 `--touka-folder "D:\Touka"`、`--autostart` 或 `--no-autostart`。不要從 Touka 資料夾安裝：那個資料夾有掛進容器，安裝程式會拒絕。

### 換版本（給維護的人）

小幫手的檔改了要分兩個 commit：先跑 `python scripts/local_helper_pins.py`，把印出的雜湊清單貼進 `install.ps1`；提交後，再跑 `python scripts/local_helper_pins.py --commit <剛才的 commit>`，把印出的那一行換進上面的指令區塊。兩個 PR 都改了小幫手的檔時，後合併的那個要在合併後重做這兩步。Python 要重新釘的時機寫在 ADR-0329。

## 配對

Touka 網頁：設定 → 本機小幫手 → 產生配對碼，貼到小幫手視窗按儲存。配對碼只顯示一次。如果 Touka 不是跑在 8000 埠，連接埠也要一起改。

配對碼裡有一把鑰匙，Touka 用它證明「照模式核准」的決定真的是它給的。2026-11-16 以前產生的配對碼沒有鑰匙，小幫手主視窗會提示；要照 Touka 的模式核准，請重新產生配對碼貼上，再勾主視窗的「照 Touka 的模式核准」（預設不勾，安裝也不會幫你勾）。

鑰匙存在 Touka 資料夾裡。這台電腦還有別人的 Windows 帳號、或有同步工具讀得到 Touka 資料夾時，對方可以在 Touka 沒開時冒充它，所以這種情況請不要勾，或把 Touka 資料夾的權限限縮到自己的帳號。

## 開放資料夾

- **只能看**：靈魂可以看檔案清單、讀文字檔。
- **可以改**：另外可以新建資料夾和文字檔（txt、md、log），也可以提出搬移與刪除清單。新建或搬到的名稱不能以點開頭，新取的名稱也不能有全形斜線、全形空白這類容易看錯的字元（搬移時沿用原檔名不受限）。

資料夾要開得窄。靈魂讀到的內容可能被它用上網工具送出去，這條路小幫手擋不到。

小幫手連上了，靈魂卻沒有這個工具時，請看：靈魂有自己的工具清單時要勾選 `local_files`；無痕對話不能用；靈魂還有別的擁有者時也不提供。

這些位置不能開放：系統資料夾、`Program Files`、`AppData`、Touka 資料夾、已經掛進容器的資料夾、使用者資料夾本身、磁碟根目錄、網路磁碟、Git 專案。隨身碟這類不是固定磁碟的位置，和 `.ssh`、`.aws`、`.gnupg` 這些放金鑰的資料夾，也不能開放。

### 從 Touka 設定頁請小幫手開放

小幫手 0.2.0 起，產生配對碼的人可以在 Touka 設定頁的「本機小幫手」貼上資料夾路徑（在檔案總管的網址列複製），按「請小幫手開放」（[ADR-0320](../docs/architecture/adr/0320-ask-the-local-helper-to-open-a-folder.md)）。

- 小幫手一定跳視窗。能開放的，在視窗裡選權限（預設只能看），按核准才加進名單；不能開放的，視窗寫原因，按「知道了」。
- 視窗開 2 分鐘，沒決定就當作沒開放。關掉 Touka 的頁面不會收掉這個視窗。
- 連續 3 次沒核准（含按「知道了」），小幫手暫停接收這種請求 10 分鐘；可以在主視窗按「現在恢復」，或結束小幫手再重新開啟。
- 如果你沒有在 Touka 送出，卻跳出這個視窗，請按拒絕。

## 核准

搬移和刪除照 Touka 的模式核准：Manual 在聊天畫面的卡片上按（卡片會顯示小幫手掃描到的清單）、Auto 交給審查員、YOLO 直接做。清單是小幫手自己檢查檔案產生的，代理人的說明可能不準，以清單為準。刪除是丟到回收桶；搬移可以在「最近批次」復原。

碰到小幫手自己的底線時，一定要你在小幫手視窗按，Touka 的任何模式都跳不過，視窗上方會寫原因：

- 主視窗的「照 Touka 的模式核准」沒勾（關掉就每份都在這裡按）；
- 一份超過 20 項，或碰到的檔案和資料夾超過 200 個；
- 有像金鑰或密碼的檔、可執行檔，或超過 100 項的資料夾；
- 最近 1 小時照模式做的已經有 10 份、100 項或 300 個（如果都是你知道的，可以在主視窗按「重新計算」）；
- 搬移跨了兩個開放的資料夾、搬到 7 天內有東西被搬走或刪掉的位置，或搬走的資料夾裡面 7 天內有東西被搬走或刪掉；
- 小幫手的紀錄有檔案讀不懂（主視窗會寫是哪個檔，請移到別的資料夾）。

7 天內有東西被搬走或刪掉的位置，不能新建同名的檔案或資料夾，請自己建或換一個名稱。很大的資料夾 2 分鐘內掃不完時，Touka 不再等，這次不做。照模式搬走的資料夾，之後又在裡面新建檔案時，「最近批次」的復原會跳過它，要自己搬回。等你在小幫手按的時候，聊天畫面的那個工具步驟會顯示提示。

## 在背景執行

小幫手 0.4.0 起：

- 開機自動啟動、已經配對時，小幫手在背景執行，不顯示主視窗，也沒有工作列按鈕；它照常服務。
- 想看主視窗（例如確認它有沒有在跑、改開放的資料夾），從桌面開 `ToukaLocal.cmd`：小幫手已經在跑時，會叫出原本那個主視窗，不會開第二個。Windows 不讓它跳到最前面時，工作列按鈕會閃。
- 桌面上找不到 `ToukaLocal.cmd` 時，用 `%LOCALAPPDATA%\ToukaLocal\ToukaLocal.cmd`。「啟動」資料夾裡那一份是開機用的，雙擊它不會顯示主視窗。
- 少數情況下小幫手沒辦法用「再開一次就叫出來」（紀錄檔會寫原因）：這時主視窗照舊一開始就顯示，按 × 只縮到工作列。
- 按主視窗右上角的 × 只是收起來，小幫手繼續服務。要完全結束，按主視窗的「結束小幫手」，結束前會先問你；結束之後靈魂就不能用你電腦上的檔案。
- 要你決定的事（超過底線的清單、開放資料夾）照樣跳小幫手自己的視窗。需要你處理的狀況（例如還沒配對、版本不符、為了安全停止服務、開放請求暫停）剛出現時，主視窗會以最小化出現在工作列，不搶你正在用的視窗。
- 確認它有沒有在跑：Touka 設定頁的「本機小幫手」會顯示是否連線；也可以從桌面開 `ToukaLocal.cmd` 叫出主視窗。
- 0.5.0 起，Touka 設定頁的「本機小幫手」也列出開放中的資料夾（完整路徑、只能看或可以改、暫停中），只有產生配對碼的你看得到（[ADR-0330](../docs/architecture/adr/0330-local-helper-folders-in-settings.md)）。以小幫手主視窗為準；要改權限或拿掉，還是到主視窗。小幫手和 Touka 要一起更新：只更新一邊時，小幫手主視窗說版本對不上，設定頁會說是哪一邊要更新。

螢幕縮放大於 100% 時，視窗會跟著放大，字用微軟正黑體（電腦上沒有時用系統預設）。
