# ADR-0332：專用空間的專案在執行器裡跑

- 狀態：✅ 已實作（融合段 2 PR B：從網頁下載專案〔決定 9〕；PR A：專用空間、執行器 kind `project`、登記規則、部署、兩個列檔不跟隨連結的小洞、專案頁的原因說明）。一頁計畫由維護者核准（2026-11-25，總覽板第 58 項全部照建議）；產品程式超過計畫的停止線，維護者 2026-11-25 決定照計畫做完（PR A 總覽板第 108 項、PR B 第 109 項）。
- 日期：2026-11-25
- 決策者：維護者
- 相關：[ADR-0272](0272-iso-c2-agent-command-isolation-and-executor.md)（執行器；本 ADR 修訂它「綁了專案一律不在執行器範圍」：只剩專用空間以外的專案）、[ADR-0292](0292-hooks.md)（決定 12：檔案動詞不跟隨連結）、[ADR-0293](0293-agent-folders-no-follow.md)（區與 ResourceManager 的區內方法）、[ADR-0326](0326-deleted-soul-leftovers-not-inherited.md)／[ADR-0328](0328-retired-archives-for-reused-soul-names.md)（同名不繼承）、[ADR-0331](0331-complexity-ratchet.md)（複雜度棘輪）
- 影響範圍：
  - 執行器：`services/isolation/executor_session.py`（`PROJECT_ROOT_NAME`、`PROJECT_KIND`、`KIND_LAYOUT["project"]`）、`executor_server.py`（`--project-root`、`checked_project_root`、`adopt_project_root`、READY 的 `project_root`、`_prepare_dirs`、`_maintain`）、`executor_client.py`（`parse_ready`）
  - 範圍判定：新 `services/isolation/project_space.py`（結構層）；`status.py`（`LevelDecision.refused`、`level_for(..., attended)`、`_dedicated_level`、`_dedicated_executor_issue`、`executor_scope_issue`）；`config.py`（`TOUKA_PROJECTS_DIR`）；`profiles.py`（`configured_projects_root`、`literal_path`、`SandboxLayout.projects_root`、`build_project_command_profile`）
  - 政策與執行：`models/safety.py`（`PROJECT_SPACE_DENY_PREFIX`）、`services/isolation/policy.py`（`attended`、`denial_text`）、`services/tool_executor.py`（environment 的 `isolation_refused`）、`agent_command.py`（`command_decision`、`attended`）、`runners.py`（`ProfileSource.dedicated`、`_consented`、`_executor`、relay 的本機 cwd）、`tools/terminal.py`、`services/review/evidence.py`、`middleware/layers/context.py`
  - 登記：新 `services/isolation/project_binding.py`；`home_wipe.py`（`wipe_project_home`）；`routers/ui_sessions.py`、`routers/admin.py`、`routers/auth.py`；`config/errors.json.example`（1225～1227）與重產的 `ToukaChat/src/lib/error_codes.ts`
  - 部署：三份 compose、`ToukaProxy/Dockerfile`、`utils/hard_floor.py`（`mounts_separate` 的 `project_space`）
  - 列檔：ToukaMemory `utils/no_follow.py`（`LIST_HIDDEN`、`render_tree`、`tree_lines`）、`tools/core_file_ops.py`（`core_list`）；`services/resource_manager.py`（`tree_entries`）
  - 畫面：`ToukaChat` 的 `lib/project_save_wording.ts`、`hooks/useProjects.ts`、`components/projects/ProjectsView.tsx`、兩份翻譯

## 背景

ADR-0272 之後，綁了專案的對話一律不在執行器裡跑：每條代理人指令都要人核准，核准後在 Touka 主容器內以檔案隔離執行；沒人在場（心跳、SNS）時一律拒絕。代理人要長時間在專案裡工作，每條都要按核准太累；把整個工作區掛進執行器又會把 `.env` 與 `data` 一起帶進去。所以另開一個只放專案的空間，只有這裡的專案進執行器。

## 決定

### 1. 專用空間與它的位置

- 專用空間是 compose 的 named volume `touka-projects`，proxy 與執行器都掛在 `/app/workspace/touka-projects`（工作區掛載點正下方）。映像預建這個資料夾（appuser、0700），空 volume 第一次掛載時照映像複製擁有者，所以根是 1001 的（容器端到端檢查驗的是沒有工作區 bind 的配置；預設 compose 掛在工作區 bind 底下，依 Docker 從映像複製的行為推論相同，第 7 項驗收時在實際部署看一次）。不在 Touka 的備份裡。
- 位置只從部署設定 `TOUKA_PROJECTS_DIR`（compose 設）來：`profiles.configured_projects_root()` → `SandboxLayout.projects_root`。和工作區設定脫鉤：管理員改工作區設定不會讓錨點和實際掛載對不上。
- 執行器由 `--project-root` 帶入，和讀寫根分開驗（`checked_project_root`：絕對路徑、名稱是 `touka-projects`、不經連結的資料夾、和讀寫根、唯讀根、state、ipc 都不巢狀）。驗不過時 READY 的 `project_root` 是 null，kind `project` 一律拒絕，其他 kind 照常服務。讀寫根不能用 `touka-projects` 這個名稱（設定問題 `roots`）：不然驗不過時仍可能經讀寫根接受 kind `project`。
- proxy 比對 READY 的 `project_root` 要等於錨點，否則這些專案被拒（`executor_no_project_root`、`executor_project_root_mismatch`）。不併進一般的 READY 檢查：其他代理人的指令照舊在執行器裡。

### 2. 範圍判定分兩層

- 結構層 `project_space.structural_scope`（純函式，只看路徑；PR B 會共用）：不在錨點底下（錨點本身不算）是 `outside`，和今天完全一樣；錨點第一層的資料夾是 `ok`；錨點本身（`project_space_root`）、第二層以下（`project_not_top_level`）、不安全的名稱（`project_name_unsafe`）、路上有連結或 `..`（`project_linked`），以及這個部署沒有專用空間卻用了這個名稱（`project_space_unavailable`）是 `refused`。字面位置與解析後的位置任一個在錨點底下就算在裡面。解析不了（連結迴圈；3.12 的 `resolve` 會丟錯）時只看字面：在錨點底下是 `project_linked`，其他是 `outside`。
- 執行層 `IsolationMonitor.level_for(identity, project_root, attended=...)`：結構層 `ok` 之後，執行器層要可用、READY 的專案根要對、掃描（`authorize_derived_path`）要過；任何一項不過都是 `refused`。都過之後有人在場是 `executor`，沒人在場是 `none`（`project_unattended`），交給今天的沒人在場的拒絕。範圍判定不看代理人資料夾。
- 「有人在場」：終端看 `is_human_present(ctx)`；互審的驗證指令看有沒有人能回卡（有 approver 或 consent）。

### 3. `refused` 一律拒絕，不提供降級

- 工具：範圍結果放進政策的 environment（`isolation_refused`），`safety.py` 在任何模式（Manual、Auto、YOLO）都回 DENY，不跳卡。判定本身出錯時，專用空間的專案也是 `refused`（`isolation_check_failed`，暫時的），其他專案照舊當成 none。給模型的訊息由 `policy.denial_text` 挑：暫時的（`scan_limit`、`probe_pending`、`block_ports_incomplete`、`executor_unreachable`、relay 回報的執行器重啟或拒絕）說可以稍後再試，其他說原因、請它告訴使用者；不用通用的「不要再試這個工具」。
- 互審的驗證指令回既有的 `NO_ISOLATED_ENVIRONMENT`（不送審、不計次、不上鎖）。
- 最後一道：`RunnerBroker` 對專用空間的專案（`ProfileSource.dedicated`）只接受執行器，有降級同意也不退回本機 Landlock 或直接執行；執行器位置組出的 profile 有沒授權的（`ungranted`）就拒絕。組 profile 失敗也一律拒絕，不會改用不完整的 profile。

### 4. 執行器 kind `project`

- `KIND_LAYOUT["project"] = ("touka-projects", ())`，owner 是專案的資料夾名稱。目標是 `<project_root>/<名稱>`，START 的 cwd 必須是它；讀寫只有它與 `state/project/<名稱>/{home,tmp}`。HOME 依專案分，不依靈魂分。
- profile `project_command`（`build_project_command_profile`）：只在執行器位置；再核對位置正好是錨點第一層；系統路徑、skills（唯讀＋執行）、HOME／TMP（讀寫＋執行），掃描過了才給專案本身讀寫＋執行。不給個人空間與公開設定檔。
- 指令從專案根開始；本機 relay 從 kernel 資料夾啟動，不 chdir 進代理人寫得到的資料夾（禁令 68 ③，同 kind `mcp`）。專案說明（context 層）對結構層 `ok` 的專案多兩句「指令從這裡開始、用相對路徑」「被擋時直接拒絕、不跳卡，告訴使用者原因」；只讀登記的根（不經 `get_project_path` 的工作區檢查，工作區外的專案不每輪記警告）。
- `_prepare_dirs` 對 kind `project` 不修 cwd 的路徑鏈（專案內容不動），HOME、TMP 照修；`_maintain` 只修專案根本身。

### 5. 登記規則（`project_binding.py`）

- 順序寫死：權限 → 根目錄檢查（呼叫端）→ 結構層 → 工作區寫法（`is_within_workspace`）→ 資料夾存在 → 重複 → 清 HOME → 寫入。結構層起整段在 `binding_lock` 裡（proxy 是單一程序）；權限在鎖外先查一次，拿到鎖之後再查一次（等鎖最長 60 秒，同一個新編號可能已被別人建好）。被拒的登記不會清到任何 HOME。
- 被拒的形狀、寫法不對（在錨點底下但工作區檢查不過）：422 `VALIDATION_ERROR`，`detail.reason` 是結構層的原因或 `project_path_form`。資料夾不存在：422 `PROJECT_FOLDER_MISSING`。
- 一個資料夾只能是一個專案：比結構層解析出的資料夾名稱（不分大小寫，偏嚴），不比字串；只有名稱完全相同才算這個專案自己的資料夾（HOME 依實際名稱分，只差大小寫的是另一個資料夾，改指過去照樣先清）；所有身分都套用（含管理員）；更新同一個專案不算重複。讀不到登記就拋錯，不當成沒有重複。重複：409 `PROJECT_FOLDER_TAKEN`。
- 資料夾第一次被這個專案綁定（新列，或改指到另一個資料夾）時先清它的 HOME（`home_wipe.wipe_project_home`，經執行器、和刪靈魂清 home 同一套）。清除前先核對 READY 的專案根，不對就不送 START（送了會被執行器拒絕，relay 回報的拒絕讓整台執行器暫時被判 none）。清不掉：503 `PROJECT_HOME_NOT_CLEARED`，不寫入；`detail.reason` 是清除的原因，多半暫時，掃描不過（`protected_location`、`scan_limit`）與執行器設定不對（`executor_no_project_root`、`executor_project_root_mismatch`）要人處理，專案頁說原因。清除不放在資料庫交易裡（最長 60 秒，會鎖住其他存檔）。
- 刪除之後（專案頁刪除、刪使用者、自己清資料）盡力清一次，失敗（含讀不到登記）只記 log、不擋刪除；拿到鎖時已有別的專案登記同一個資料夾就不清（那是它的 HOME）。下次登記本來就會先清。
- 專用空間以外的專案完全照舊（不清、不查重複）。

### 6. 底線的掛載證明

- `hard_floor.mounts_separate` 多一個參數 `project_space`：正好掛在專用空間位置、又是實體區塊裝置上的 Docker volume 的子掛載，和錨點在不同裝置時算分開（主機共享不可能包含 VM 磁碟裡的 volume）；同裝置仍比路徑。不改 `_apart`。沒有這條時，工作區另外放（9p）的部署一掛上專用空間就證明不了分開。
- 預設 compose 的工作區是 repo 本身，本來就證明不了分開：帶 `/app/workspace/...` 絕對路徑的寫入類指令照舊是 F4。所以指令從專案根開始、用相對路徑；底線不改。

### 7. 兩個列檔不跟隨連結的小洞（禁令 20）

- 沒有 proxy 的 `core_list`（專案工具走這條）與 `GET /ui/projects/{id}/files` 都是先判斷再用會跟隨連結的呼叫，判斷之後被換成連結就會洩漏根外面的名稱（後者還有大小）。改成走訪本身不跟隨（`walk_pruned`），代理人資料夾剪掉。檔案清單頁另套結構層：根是錨點本身、第二層以下，或路上有連結（含一般專案的根之後被換成連到專用空間的連結）一律 403；根目錄檢查本身不拒絕連結。根被換成連到專用空間以外的一般資料夾時照舊會列出（ADR-0292）；根仍先解析再走訪，檢查與走訪之間根被換成別處的窗口照舊（既有）。樹狀排版 `render_tree` 與預設隱藏 `LIST_HIDDEN` 從 ResourceManager 搬到 `no_follow`，兩種列法共用；檔案清單頁經新的 `ResourceManager.tree_entries`。
- 檔案清單頁連指向根裡的連結也不再列出（以前會跟隨）。

### 8. 專案頁說得出原因

- `useProjects.saveProject` 回 `{ok, code?, reason?}`；`lib/project_save_wording.ts` 的 `projectSaveFailureText` 選說明：專用空間的原因 → 清不掉 HOME 且不會自己好的原因（`home_reasons`）→ 專用空間的三個錯誤碼 → 一般的「存不了」。兩語都有；後端的每個原因都有文案（測試釘住）。
- 審批卡的 `project_not_in_executor` 改說「專案不在專用空間」與怎麼放進去。

### 9. 從網頁下載專案（PR B）

- `GET /ui/projects/{id}/download`（含 `/v1`）：只給專案的主人（人類本人）；沒有主人的舊專案只給人類管理員。比讀取窄：非主人的管理員、管理員代打、靈魂、診斷身分都 403。只有專用空間裡的專案：一般專案 422 `not_dedicated_space`，結構層 `refused` 照它的原因 422；資料夾不在是 `PROJECT_FOLDER_MISSING`（先於根目錄檢查：容器裡掃不到不在的資料夾，會被說成受保護）；之後套和登記相同的根目錄檢查（422 帶原因：`outside_workspace`、`protected_location`、`scan_limit`），登記裡還有別的專案綁著同一個資料夾（舊資料）是 `PROJECT_FOLDER_TAKEN`（`project_binding.owns_folder`）。git 還沒追蹤任何檔案是 `no_index`，不給空 zip。
- 放哪些檔案只看 `.git/index`：`utils/git_index.py` 純 Python 讀（不跑 git，proxy 與執行器都不跑；index 是代理人寫得到的，跑 git 會讓它的設定跑程式）。支援 v2～v4（含延伸旗標、v4 前綴壓縮）；連結、子模組分開；split index（`link`）、sparse index（`sdir`、資料夾項目）、不認得的小寫擴充段、SHA-256（加了 `index.skipHash` 的只能從讀不懂看出來，也算）是 `index_unsupported`；壞掉是 `index_invalid`；沒有 `.git` 是 `no_git`，`.git` 是連結是 `git_is_link`、是檔案（worktree 的指標）是 `git_not_folder`，沒有 index 是 `no_index`。
- 檔案經 `ResourceManager.project_walk` 從專案根不跟隨連結地走訪（根不解析，根或上層是連結就拒絕；途中遇到代理人資料夾整個拒絕）；名單以外的資料夾不進去、名單以外的檔案不開。名單由 `ResourceManager.read_git_index` 不跟隨連結地讀。
- 略過並寫進 zip 根的 `TOUKA_DOWNLOAD_REPORT.txt`（兩語，名稱的控制、格式字元與解不開的位元組寫成跳脫碼、超過 300 字截短，每個原因只列前 1,000 條、其餘只計數）；名稱的判斷是 `utils/archive_names.py`的純函式（禁令 18）：`.git`（含 NTFS 短名稱、HFS 忽略字元、結尾的點與空白）、解壓後會讓資料夾變成 repository 的（名為 `HEAD` 的檔案：git 認 repository 一定要有它，代理人可以追蹤一組 bare repository 的檔案，你在解壓的資料夾跑 git 或命令列提示字元自動跑 git 時就會讀它的 config；以及 `.hg`、`.svn`、`.bzr`、`_darcs`、`.jj`、`.sl`、`.pijul` 資料夾與 Fossil 的 `.fslckout`、`_FOSSIL_`，含 Windows 的短名稱如 `HG~1`，解壓蓋到既有的 checkout 時會寫進去）、Windows 不能用的名稱（保留字元、控制字元、結尾點或空白、`CON`／`COM¹`／`CONIN$` 等保留名稱）、雙向控制字元、檔案總管會自動處理的捷徑類檔案（`desktop.ini`、`.lnk`、`.url`、`.scf`、`.library-ms`、`.searchconnector-ms`、`.theme`、`.themepack`、`.deskthemepack`、`.website`，以及 KDE 會處理的 `.directory`、`.desktop`）、`.env`（共用 `hard_floor.is_env_file_name`，不分大小寫；`.env.example` 等照放）、不是 UTF-8、路徑超過 200 字或 32 層（位元組超過 800 的不解碼就算太長）、只差大小寫的重複（留第一個）、名單上的連結與子模組、磁碟上的連結（含在連結底下）、硬連結（可能是別處的檔案）、不是一般檔案、不見了、讀不到。
- zip 裡檔案在 `<專案資料夾>/` 底下；時間超出 zip 能存的範圍夾到 1980-01-01 或 2107-12-31；權限只留 `rwxr-xr-x` 以內（setuid、setgid、sticky 與別人可寫不帶進 zip：有些解壓工具還原權限時不套 umask）。兩者在共用的 `zip_open_file`，打包技能、GDPR 匯出的個人空間檔案與備份（`scripts/manage_backup.py`）也一樣。
- 上限（`services/project_download.py` 的模組常數）：名單 64 MiB、200,000 項（v4 展開後的名稱總長另有 32 MiB 的上限）；檔案 50,000 個、所在的資料夾 50,000 個、累計 512 MiB（依開檔時的 fstat）；走訪 1,000,000 項；從讀名單起，檢查名單、走訪加打包 300 秒。名單上每個名稱的檢查與比對有絕對上限（路徑最長 200 字、32 層；大約和長度乘層數成正比，Unicode 正規化在特別排列的字元上會更慢），惡意的名單撐不大記憶體（安全審查估最壞約 300 MB）與時間（約 25～40 秒，也算在 300 秒裡）；超過是 413（`index_too_large`、`too_many_files`、`too_large`、`walk_too_large`）。同時只做一個下載（全系統），忙時 429 `PROJECT_DOWNLOAD_BUSY`（1228）。
- zip 做在系統暫存資料夾；被拒時刪掉做一半的；請求取消時打包照樣做完（在執行緒裡），做完就刪；送出後一律刪掉（`utils/temp_file_response.TempFileResponse`：Starlette 的 `background` 在送到一半斷線或 Range 不合格時不跑）。回應 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`；稽核紀錄寫誰、哪個專案（`%r`，專案編號來自網址）、幾個檔案、略過幾個、多大；沒有權限與打包被拒也記。打包途中讀檔失敗記下是哪個檔案，回 500（多半是暫時的，畫面說稍後再試），不留下截斷的檔案。
- 前端：專案卡片一直看得到的下載按鈕；先跳提醒（zip 裡是代理人寫的程式，VS Code 選「不信任」），確認後 `useProjects.downloadProject` 經 `fetchTouka` 帶登入拿 zip、交給瀏覽器存檔（不做帶 token 的連結）；檔名是專案名稱（不安全的字元換掉、截短）加日期；打包時按鈕停用並顯示「打包中」，blob 網址 40 秒後才放掉（有些瀏覽器點下去之後才開始讀）。被拒時 `lib/project_download.ts` 依原因說明（兩語；沒有權限說只有主人能下載，不說稍後再試）。

## 後果

- 代理人可以在專用空間的專案裡長時間工作：有人在場時不用每條核准，看不到 Touka 的金鑰、設定、原始碼、其他專案與個人空間；專案之間的 HOME（例如 `~/.gitconfig`）不共用；換人登記拿不到上一個專案留下的暫存（上一個專案的指令都結束之後，見已知限制）。
- 專用空間不在 Touka 的備份裡；`docker compose down -v` 會連同裡面所有專案一起刪掉。PR B 之後可以從專案頁下載 git 追蹤的檔案（沒追蹤的仍要 `docker cp`）。
- 已知限制：
  - 你在 Touka 裝了會讀專案內容的 lint 類 hook 時，那個 hook 以 Touka 的權限跑，不受隔離；放寬之後這件事風險變大。
  - 刪除或改指專案時，當下還在跑的指令不停掉（背景指令最長 600 秒，執行器上限 900 秒）。這段時間內有人登記同一個資料夾，舊指令仍可能在清除之後寫進 HOME（計畫已列），也讀寫得到新擁有者放進專案的檔案（審查補充）；新登記的清除擋不住。README 請換人時等 15 分鐘。
  - 擋不住改權限位元（Landlock 不管 chmod）：一個專案的指令可以把別的專案或錨點本身的權限改掉，讓它被拒（掃描讀不到是 `scan_limit`），讀不到、也改不了內容。執行器每 60 秒修一次錨點本身；專案資料夾要照 README 修（`chown` 加 `chmod -R u+rwX`）。
  - 專案之間共用執行器的網路命名空間：只封鎖 Touka 自己的埠，一個專案開在 127.0.0.1 的服務別的專案連得到；`/proc/<pid>/cmdline` 也看得到別的專案正在跑的指令列（既有）。
  - 「有人在場」是經聊天端點送出的對話：用帳號權杖打 API 的腳本也算（計畫已列）。
  - 登記的回應（資料夾不存在 422、已被綁 409）讓登入的人知道專用空間裡有哪些資料夾名稱、哪些已被綁。
  - 所有專案（含專用空間以外）的存檔排同一把鎖，別人清 HOME 時最長等 60 秒；刪除、刪使用者、自己清資料在請求裡逐一等清除。
  - 規則只在存檔時查：管理員改工作區設定之後，原本判定在空間外的舊登記可能對到已被綁的資料夾。
  - 專用空間第一層有叫 `soul`、`personal_drive`、`ui_sessions.db` 的項目時所有專案都被拒；專案裡有 Touka 的標記（例如 Touka 自己的 repo）時那個專案被拒（`protected_location`，計畫已列）。
  - 專案裡的超大檔案或大量檔案，經 read／list 或檔案清單頁時會拖慢 proxy（個人空間原本就有，範圍擴大）。
  - `docker cp` 複製進去的檔案是 root 的，代理人改不了；README 寫怎麼改擁有者。
  - Docker 會在工作區掛載點的來源裡建一個空的 `touka-projects` 資料夾（巢狀掛載的掛載點），裡面沒有東西，git 也不會列出它。
  - 沒有執行器的部署（原生 Windows 開發模式）：路徑用了 `touka-projects` 的專案一律拒絕，不會悄悄改用別的方式跑。
  - `WORKSPACE_ROOT` 有設時，靈魂與子代理登記要用工作區根的寫法（`<工作區根>/touka-projects/<資料夾>`）；管理員不經根目錄檢查，但寫法不對照樣被拒並說明。
- 和計畫附錄不同、照實寫在這裡的地方：
  - 終端與互審交出個人空間的結構不改（附錄原本預想改成專案根的字面位置）：START 的 cwd 由專案的 profile 決定，個人空間有連結時專案指令也跑不了，偏嚴。
  - 「被擋時直接拒絕、不跳卡」寫在專用空間專案的說明裡（每輪附給靈魂，只在這種專案出現），`<approval_context>` 本身不改（一頁計畫寫「誰會核准」的說明跟著改，審查時改成這樣：不讓所有對話每輪多加字）。
  - READY 的專案根不符不併進一般的 READY 檢查（附錄有一處舊寫法寫成 `executor_roots_mismatch`），只讓專用空間的專案被拒。
  - `isolation_wording.ts` 不改：新原因不會出現在審批卡上（被拒絕的直接拒絕、沒人在場沒有卡）。
  - 不在 `touka-init` 加 chown：靠映像預建加 copy-up，容器端到端檢查核對根是 1001 的。
- PR B 的已知限制：
  - 放的是 git 追蹤的路徑「現在磁碟上的內容」（含還沒 commit 的修改）；還沒 `git add` 的新檔案不放。每個檔案是開檔那一刻的內容，打包途中被改的檔案之間可能不一致。
  - zip 在 proxy 的暫存資料夾，最大約 512 MiB；同時只做一個（全系統），大專案會讓別人等。
  - 走訪一個資料夾時先把它的名稱全部列進記憶體，項目上限在列完之後才算（和其他走訪相同）；時間上限在項目之間檢查，單一大檔的壓縮不中斷。
  - 只差大小寫的判斷用 Unicode casefold，不是 NTFS 的對照表，極少數字元會判得不同。
  - zip 裡是代理人寫的內容（例如 `.vscode/tasks.json` 的開資料夾就執行）：只靠下載前的提醒與 VS Code 的受限模式。
  - 下載的回應不支援續傳（送一次就刪）；瀏覽器把整個 zip 讀進記憶體再存檔（最大約 512 MiB）。
  - 設了連線逾時（`requestTimeoutMs`）時，打包比逾時久的下載會被前端中斷（伺服器打包完才回應）；預設不逾時。
  - 做好的 zip 要等送完才刪：同時只「做」一個，但送得很慢的下載會讓好幾個 zip 同時在暫存資料夾；proxy 當掉時留下的 `touka_project_*.zip` 不會自動清（審查的小問題，列為後續）。
  - 走訪的項目上限不算被剪掉、沒進去的資料夾；一個資料夾的名稱全部列進記憶體才判斷（和其他走訪相同；審查的小問題，列為後續）。
  - 名為 `HEAD` 的檔案一律不放（正常的專案很少有；被略過的會寫在報告裡）。
  - 既有的備份下載與 GDPR 匯出仍用 `BackgroundTask` 刪暫存檔，斷線時會留下（既有；不在本 PR 範圍）。
- PR B 和計畫附錄不同、照實寫在這裡的地方：
  - 多一個原因 `git_not_folder`（`.git` 是檔案，例如 worktree）；附錄只有 `git_is_link`。
  - sparse index 的資料夾項目算 `index_unsupported`；split index 的項目名稱是空的，所以先看擴充段再判空名稱。另加單一名稱 4,096 位元組與 v4 展開後總長的上限（審查前自己找到的：小檔案展開成巨量；安全審查後從 64 MiB 降到 32 MiB）。
  - 送完刪暫存檔改用 `TempFileResponse`，不是附錄寫的 `BackgroundTask`（理由見上）。
  - 走訪的時間上限包含打包（兩者交錯進行）。
  - 下載時另套和登記相同的根目錄檢查（422 帶原因）；資料夾不在沿用 `PROJECT_FOLDER_MISSING`，前端另有下載用的說明。
  - 審查後加的（維護者 2026-11-25 選甲）：解壓後會變成 repository 的檔案、名單的上限與報告的截斷、資料夾數上限、別的專案也綁著時拒絕、zip 裡不帶特殊權限、空的名單、`.themepack` 等捷徑類檔案、下載中的按鈕狀態。

## 不採用

- 把整個工作區掛進執行器：會連 `.env` 與 `data` 一起帶進去。
- 新的 ZonePath 錨點：讀寫在解析後已經逐段不跟隨連結（ADR-0292 決定 12），專用空間用結構層判斷就夠。
- 只在刪除時清 HOME：刪除有三個入口、資料夾可能已經不在、清不掉時刪不掉；改成綁定時先清，刪除後再盡力清。
- 專用空間的專案在沒人在場時也跑：照今天的規則交給沒人在場的拒絕，之後另議。

## 驗證

- 單元：`test_executor_project_kind_security.py`、`test_project_space_scope_security.py`、`test_isolation_project_level_security.py`、`test_project_space_policy_security.py`、`test_project_space_runner_security.py`、`test_project_space_commands_security.py`、`test_project_space_binding_security.py`、`test_core_list_no_follow_security.py`、`test_project_files_no_follow_security.py`；既有的 compose、底線、執行器命令列、容器資產測試照計畫放寬（只放過專用空間那一筆，`workspace` 仍在禁用清單裡）。
- 前端：`project_save_wording.test.ts`、`useProjects.save.test.ts`、`ProjectsView.saveFailure.test.tsx`。
- Linux 真核心（`test_isolation_linux.py`）：專案裡讀寫、執行都可以；旁邊的專案（直接或經連結）、個人空間、錨點本身都碰不到；HOME 依專案分開；沒給 `--project-root` 時 kind `project` 被拒、其他照常。
- 容器（`tests/docker/executor_check.py`、`isolation.compose.yml`）：READY 的專案根、有人／沒人在場的層級、volume 的根是 1001 的、uid／HOME／cwd、碰不到旁邊與個人空間、HOME 分開、執行器容器裡沒有 `.env` 與工作區其他內容。
- PR B：`test_git_index_security.py`（手工組的 index 與真的 git 產生的 v2～v4、split index、SHA-256 加 skipHash）、`test_archive_names.py`（名稱的判斷、比對鍵、顯示）、`test_project_download_security.py`（權限、範圍、根目錄檢查的原因、名單問題、每一種略過與報告、解壓後用真的 git 確認不是 repository、上限、碰到代理人資料夾、同時一個、取消與斷線時刪 zip、每個原因兩語都有說明）、`test_resource_manager_string_zone.py`（讀名單與走訪的 R4）、`test_no_follow_zone.py`（時間夾邊界、不帶特殊權限、只開名單上的檔案）與 `test_resource_manager_archive.py` 的時間夾邊界；前端 `project_download.test.ts`、`useProjects.download.test.ts`、`ProjectsView.download.test.tsx`。
