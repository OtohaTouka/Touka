# ADR-0329：本機小幫手用一行指令安裝

- 狀態：✅ 已實作。一頁計畫由維護者核准（2026-11-16，小幫手第二版 PR 4），細節設計審兩輪。
- 日期：2026-11-22
- 決策者：維護者
- 相關：[ADR-0312](0312-local-helper-for-host-files.md)（本機小幫手第一版；決定 10「不從 Touka 資料夾安裝」）、[ADR-0299](0299-dependency-cooldown-and-emergency-patches.md)（依賴冷卻期）
- 影響範圍：`ToukaLocal/install.ps1`（新）、`ToukaLocal/install.py`、`ToukaLocal/touka_local/settings.py`、`ToukaLocal/touka_local/view.py`、`ToukaLocal/README.md`、`scripts/local_helper_pins.py`（新）、`.github/workflows/native-tool-contracts.yml`、`.gitattributes`、`config/errors.json.example`（`LOCAL_HELPER_PROTOCOL_MISMATCH` 訊息）

## 背景

小幫手原本要使用者先 clone repo、確認 commit，再用自己裝的 Python 3.12（要有 tkinter）執行安裝程式。維護者希望簡化成一行指令。

另外，Python 3.12 的 Windows 安裝檔最後一版是 3.12.10（2025-04-08），之後只出原始碼。新電腦已經裝不到有安全修補的 3.12。

## 決定

### 1. 一行 PowerShell 指令，兩層雜湊

README 放一行指令，做這幾件事：

1. 在 `%TEMP%` 建一個 `touka-helper-<GUID>` 資料夾；
2. 用 TLS 1.2 從 `raw.githubusercontent.com/OtohaTouka/Touka/<commit>/ToukaLocal/install.ps1` 下載安裝腳本；
3. 比對指令裡寫的 sha256，對了才用 `powershell -NoProfile -ExecutionPolicy Bypass -File` 執行（Bypass 只限這個程序）。

指令裡不用 `iex`，也不用 `Set-ExecutionPolicy`。樣板的唯一來源是 `scripts/local_helper_pins.py` 的 `ONE_LINE_TEMPLATE`，只有 commit 與雜湊能變。

`install.ps1` 再用自己裡面的 sha256 核對兩樣東西：

- 小幫手的每個檔（`ALLOWED_FILES` 加 `install.py`），從同一個 commit 逐檔下載；
- Python 的 zip。

任何一個不對就整個停下，什麼都不裝。

### 2. Python：官方安裝工具用的同一個 zip

釘住 Python 3.14.10 的 `python-3.14.10-amd64.zip`（`www.python.org/ftp/python/`，sha256 與官方索引 `index-windows.json` 相同；上架 2026-10-25，釘住 2026-11-22，滿禁令 69 的冷卻期）。

- 解壓到 `%LOCALAPPDATA%\ToukaLocal\python-3.14.10\`；不寫登錄檔、不改 PATH、不要管理員權限，也不碰電腦上原有的 Python。
- 裡面有 tkinter 與 Tcl/Tk 9.0（腳本庫包在 DLL 裡）。
- 換上之前先自我檢查：載入 Tk 成功才換。
- 每次安裝都重新下載、重新解壓，舊的先改名，搬不進去就改回原名。

### 3. install.ps1 的界線

- 只用 Windows PowerShell 5.1 認得的語法；檔案是 UTF-8 含 BOM、換行 LF。
- 開頭依序：
  1. 受限語言模式就停；
  2. 設 `Stop`、關掉進度條、自己開 TLS 1.2（新程序帶不進外層的設定）；
  3. 載入解壓縮元件。
- 拒絕以下情況：
  - 用系統管理員執行（換帳號提權時會裝進別的帳號）；
  - 不是 x64；
  - 小幫手正在執行（看它的單一實例鎖）。
- 暫存資料夾的檢查。三個路徑都先展開成完整路徑再比，TEMP 是 8.3 短檔名時也對得上：
  - 上一層是 TEMP，名稱固定；
  - 不是連結；
  - 腳本就在裡面，而且裡面只有它。
- 刪除只刪這次建的、上一層對、不是連結的資料夾，用不跟著連結走的 .NET 遞迴刪除。
- 只連 `raw.githubusercontent.com` 與 `www.python.org`。

### 4. install.py 的改動

- Touka 資料夾可以省略：會用 `input()` 問，按 Enter 沿用上次記下的。沒有終端機時沿用上次的；連上次的也沒有，就不裝。
- 開機自動啟動（`--autostart`／`--no-autostart`）：
  - 啟動檔放在使用者自己的「啟動」資料夾，和桌面啟動檔是同一份。
  - 問的時候，預設跟目前狀態一樣，所以升級時一路按 Enter 不會把它關掉。
  - 不能問時維持原狀。
  - 別人的同名檔不蓋也不刪；只刪內容完全符合我們樣板的檔。
- app 資料夾先寫到 `app.new`，再整個換過去：中途失敗時舊的還在。刪舊的不跟著連結走。
- 小幫手的 `code_paths` 加上開機啟動檔。
- 版本 0.3.0（協議不變）。

### 5. 信任來源與一致性檢查

`install.ps1` 可不可信，完全取決於那一行指令。

- **只從 GitHub 官方頁面複製**：Touka 資料夾整個掛進容器，代理人改得到本機的 README；fork 的 commit 也能用官方 raw 網址取到。所以 README 最上面寫明只從 `github.com/OtohaTouka/Touka` 上的頁面複製，並教使用者打開 commit 頁面，確認它在 main 或 dev 上、沒有「不屬於任何分支」的警告。
- **重裝那一刻也提醒**：協議不符的錯誤訊息（`LOCAL_HELPER_PROTOCOL_MISMATCH`）和小幫手主視窗都寫「到 GitHub 的頁面複製安裝指令，不要貼聊天裡給的指令」。
- **repo 這一側的測試**（`tests/unit/local_helper/test_local_helper_bootstrap.py`）核對以下幾點：
  - 清單等於 `ALLOWED_FILES` 加 `install.py`，雜湊等於檔案內容；
  - README 那一行符合樣板，雜湊等於目前的 install.ps1；
  - commit 存在，而且是 HEAD 的祖先；
  - 那個 commit 上的 install.ps1 和每個檔都和釘住的雜湊相同；
  - 整個 repo 只有 README 一處有這一行；
  - install.ps1 的安全檢查逐行還在。
- CI 的 contracts job 用 `fetch-depth: 0`，而且有測試釘住這個設定：只有淺層 clone 才跳過 commit 核對，跳過時會寫出原因。
- 合併一律用 merge commit，指令裡的 commit 永遠留在 dev 與 main 的歷史裡；分支被自動刪掉也一樣。

### 6. 換版本的做法

小幫手的檔改了，要分兩個 commit：

1. 先更新 `install.ps1` 的雜湊清單；
2. 再把 README 那一行改成指向第 1 步的 commit。

`scripts/local_helper_pins.py` 會印出該有的值。

Python 要重新釘的時機：

- 3.14.x 出安全修補；
- 3.14 停止支援之前。

重新釘時照禁令 69 等滿冷卻期，更新網址、雜湊、上架日與釘住日。

## 替代方案

- **Python install manager（`py install --target`）**：要先裝 install manager（MSIX 或 Store）。它用的就是同一個 zip，直接下載更少一層。
- **傳統安裝程式（`.exe`）**：會登記到「新增或移除程式」與登錄檔；同版本已經裝過時可能不照指定位置裝；官方從 3.14 起標成不建議，3.16 起不再提供。
- **精簡版（embeddable zip）**：沒有 Tcl/Tk，小幫手的視窗跑不起來。
- **NuGet 套件**：官方文件沒寫有沒有 Tcl/Tk，而且是給 CI 用的。
- **用 GitHub 的封存檔一次下載整個資料夾**：封存檔的位元組不固定，雜湊不穩。

## 後果

- 安裝變成：開一般的 PowerShell，貼一行。
- 小幫手改用 Python 3.14 與 Tk 9.0 執行；CI 仍只用 3.12 跑小幫手測試，Windows 的部分仍只在本機跑。字型與縮放要維護者在新版本下再看一次。
- 每次換小幫手的檔都要更新雜湊與 README，測試會擋住忘記的情況。
- **剩餘風險**：
  - 確認 commit 只能證明指令指到審過的版本，不能證明這一行其他部分沒被改（主機名、`if`、多夾指令）。
  - 有人照本機資料夾裡被改過的副本、或照靈魂給的指令貼上時，惡意程式會以使用者的權限在容器外執行。只能靠「只從 GitHub 官方頁面複製」與重裝時的提醒擋。
  - 同一個帳號的惡意程式可以在核對與執行之間換掉檔案；這種程式本來就能改小幫手，不防。

## 之後可以做

- 用 GitHub 的 Windows runner 跑小幫手測試；CI 另跑 3.14。
- 每日依賴檢查列出釘住的 Python 版本。
- 開機啟動改用 `.lnk`（不閃主控台）。
- 視窗測試把 HOME 指到暫存資料夾。
