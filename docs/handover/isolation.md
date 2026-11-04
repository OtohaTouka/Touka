# 隔離與連結防護交班

範圍：隔離執行、MCP 來源、代理人資料夾不跟隨連結（ADR-0272、0275、0293）。

這份只放這個範圍沒做完、有未解限制、做完還沒部署或要特別操作的事。總覽見 [HANDOVER](../../HANDOVER.md)，舊內容見 [封存](../changelog/handover-archive-2026-11.md)。做完一項就從這裡刪掉，紀錄寫進 [CHANGELOG](../CHANGELOG.md)。

## 現況

- **代理人資料夾不跟隨連結 PR 2（2026-10-30，ADR-0293）**：演化的工具資料夾（`data/mcp_servers/managed`）、文件輸出（`data/temp/docgen_output`）與 sandbox state 也只經區內方法讀寫，路上有連結就以固定原因拒絕（演化回 `skill_changed`，清除沙盒 home 回 `proxy_state_link`）；計分卡只讀輸出區裡的產出檔；靈魂檔名只能是單層；還原只寫回備份會寫的 data 資料夾。執行器改成只掛 `data/mcp_servers/managed`：**GHCR 安裝升級前先建好這個資料夾**（擁有者與 `data` 相同），`docker-compose.yml` 的 `touka-init` 會自動建；單獨開發用的 `ToukaProxy/docker-compose.yml` 沒有 `touka-init`，Linux 上也要先建（擁有者 1001；這份 compose 的資料夾是 `${MEMORY_APP_DATA_DIR:-./data}`，相對於 `ToukaProxy/`）。技能名稱不能再用執行器保留的 7 個名字（`soul`、`personal_drive` 等，見 ADR-0293「PR 2」的後果）。測試多了 TF、G3、G4 三道靜態檢查。ADR-0292 的「暫時不要啟用 command hook」解除。
- **隔離規則只給字面位置（2026-11-03，ADR-0293 後續 PR）**：Touka 幫沙盒算規則時不再跟著連結走：MCP 條目經連結引用另一個技能資料夾時，還是判成自建、那個技能還是凍結，但不給它的讀與執行（`literal_managed_skills`、`frozen_managed_skills`）；公開設定檔從字面位置往上找，解析後和字面不同的不給；專案根核對用解析後的位置、授與用字面位置。人經連結設定 data 根時兩種寫法都認（`data_root_spellings`），第三種寫法不認（ADR「升級」）。G4 的已知界線清空。
- **代理人資料夾的讀寫不跟隨連結（2026-10-30，ADR-0293，PR 1）**：個人空間（`personal_drive/<id>`、`custom/<id>/drive`）裡的讀寫、列出、刪除、備份、GDPR 匯出、診斷統計與 `/v1/outputs` 一律經 ResourceManager 的區內方法，路上有連結就拒絕或略過；Linux 從開好的資料夾往下走，Windows 本機仍有時間差（ADR-0293「已知的界線」）。人自己把個人空間做成連結時，那個代理人的檔案功能拒絕，重置、刪除靈魂、刪除與改名自訂模型回 409 `AGENT_DRIVE_LINKED`、什麼都不改；上層資料夾（錨點）本身是連結照常可用。重置、刪除靈魂、刪除與改名自訂模型會先停掉那個代理人的全部指令（設定頁模型分頁的刪除也走同一條路）。docgen 這類 vault-jailed MCP 的工具暫時不能用（任何平台，`vault_jailed_tool_unavailable`），上傳 PDF、Office 檔時給模型的提示不再叫它用這類工具讀。`/v1/outputs` 的檔名只能是一段（不合格回 403），`outputs` 或檔案是連結時回 404。測試多了絆線（Z9，CI 全量開 `TOUKA_Z9_FULL_RUN`）與改壞清單（`tests/zone_mutations.json`，每個 PR 手動跑一次），見 tests/README。
- **刪除靈魂殘留與密碼重輸（2026-10-21，ADR-0272 限制 26 補記）**：刪除靈魂前先清它在隔離暫存區的家目錄（proxy 那一層直接刪；執行器那一層走既有 START），清不掉 503 `SOUL_SANDBOX_HOME_NOT_CLEARED`、什麼都不刪；執行器沒在服務時不能刪靈魂。重置不清家目錄（保留清單與重置卡照實列出）；這之前刪掉的 ID 要照 README_QUICKSTART 手動刪；自訂模型刪除與改名不清、和同 ID 靈魂共用家目錄（原本就有，見 ADR-0272 限制 26）。密碼重輸打錯（重置、清除連備份、還原、設定匯入、清除／匯出我的資料、改密碼）改回 403，不再被前端重送、驗兩次。`.gitignore` 排除首裝一次性密碼檔。尚未部署。
- **C2-3 已接線（MCP 伺服器隔離）**：來源分類、啟動、創建試跑、演化凍結、手動同意與設定頁已完成；不代表已部署。決策見 ADR-0275。
  - 熱掛載在等待前同步 registry 與安裝版本，重載快照不會關閉較新的掛載；技能安裝與演化部署只有目前版本連線仍存活才回報啟動。並行重載可能稍後連上，principal-scoped 遠端沒有 context 時延後連線，兩者以 MCP 清單為準。來源檢查涵蓋所有參數與 path-like env，不依啟動器名單放寬；代理人可寫位置的資料夾與尚不存在的路徑都算，普通資料參數與 Filesystem／docgen 也可能改判自建。快照匯入逐筆拒絕非物件並回報警告，保留該名稱既有有效設定；修復壞的既有條目時撤銷舊來源與資料夾授權。
  - 非人類改 env 與匯入的 env 變動會撤銷第三方來源及額外資料夾；遮罩回填不算改動，未提供 human 時預設非人類。路徑解析只供分類與授權，啟動 argv 保留原樣（既有哨兵除外）；啟動前也用隔離 state home 分類，可展開的路徑值從嚴當自建。profile 只給實際引用的路徑，不因套件名帶點而要求授權；依賴 Proxy cwd 的相對程式須改為絕對路徑。合法 scaffold 的 APPDATA 試跑與正式啟動均映到 personal_drive。重載與重試綁原版本，同意只試一次；stdin 背壓不再阻擋停止訊號。Linux 真核心測試、容器端到端與 Windows 掛載（9p）上的 Linux 模組都已在開發機 Docker 跑過；9p 上 8 項因 drvfs 檔案特性失敗，都是更嚴，與 C2-2 相同。
  - 部署凍結涵蓋特定 `managed/<skill>`；command、載入程式的 env 或選項指到 managed 根或其上層時凍結全部技能，只有普通 data 根資料參數／哨兵排除。凍結與啟動共用 Proxy cwd、自建及第三方 state home；尚不存在的無分隔符檔名（帶副檔名）也檢查，不建立目錄。Filesystem 不阻擋未部署技能，設定讀不到仍拒絕演化。Hub Filesystem／docgen 的自建分類不變，none 下仍須逐次同意且不能沿用第三方額外資料夾。
  - 動態字串從嚴當自建，涵蓋命令替換、延遲變數、萬用字元、大括號及單字內引號；秘密值含 `$` 或 URL 編碼字串符合 `%xx%` 也會觸發，完整說明見 `PROXY_INTERFACE.md`。
  - 非遠端又是 principal-scoped 的 stdio 條目在建立連線前就拒絕，MCP 頁顯示原因與改法；判定出錯時五條連線路徑都不連線；非遠端條目沒有判定結果就不能連線。
  - 握手與執行期錯誤先遮罩再記錄。隔離 shared stdio 失聯（含 OOM、自己結束）退避 5／10／20 秒重試三次，之後要人按重啟；direct 與遠端不自動重連。關機時先封住排程再取消背景工作，之後不會被重新打開。
  - 手動啟動已接線：確認內容綁定去處、features、generation 與 bound，取得連線鎖後重驗；同意只用一次。隔離設定存檔後背景重連，缺 manager 時在寫入前拒絕。
  - MCP 清單顯示等待／暫停與手動啟動資訊；暫時性探測故障等滿 660 秒才算暫停，`probe_pending` 不計時。`/isolation` 回互斥計數；失聯健康檢查保留原始原因，診斷遮罩解析後的環境值。重啟忙碌有兩語提示。
  - 創建試跑已接 runner：SMOKE／EVAL 共用一次授權，scaffold 看本機層級；有隔離時 YOLO 依層級執行，none 仍只有人能核准，Auto 仍受 F5 約束。HTTP 沒有降級同意，none 不試跑；來源由程式路徑決定。
  - 演化已拒絕修改部署中的能力，涵蓋巢狀路徑、Windows 尾點／空白與實體別名；技能名稱改收 ASCII 白名單並拒絕保留名稱。部署審批綁定身分、持鎖，只核對 `server.py` 與 `requirements.txt` 的原始位元組雜湊；目錄其餘內容未綁定，設定頁並行存檔仍有競爭（見 `SECURITY.md`）。驗證走 MCP 協定，子程序用精簡 traceback 保留有界診斷中的錯誤原因。
  - MCP 設定頁已接隔離徽章、依原因顯示的恢復說明與常駐「啟動…」確認入口；重啟等待有專屬提示。側欄顯示未隔離／等待／暫停台數，預期為 none 時不計未隔離台數，無效計數不影響其他警告。執行器關掉本機 relay 後遠端群組回收是非同步的，見 `PROXY_INTERFACE.md` 的隔離連線說明。
  - 升級要重建隔離核心與執行器；步驟及給部署者的完整告知見 `README_QUICKSTART.md`。第三方首次下載快取會變慢；需要專案的 Git MCP 須在人類設定頁加資料夾授權。包住的 env 名稱只收英數與底線且首字不能是數字，不符會以 `spec_invalid` 拒絕啟動。
  - none 下自建每次開機／重啟都要重新同意；降級 Landlock 失聯後回等隔離。原生 Windows 不因第三方未隔離台數顯示 MCP 橫幅，但自建等待仍提示。
  - 原生 Linux 孫行程可能在 Proxy 被強殺後殘留；群組長先結束但孫行程仍拿著 stdout 時，EOF 不會到。Filesystem／docgen 共用 personal_drive、custom drive 不在其內；自建 state 可由獲授權的其他代理人共用。其餘限制見 `SECURITY.md`。
- **隔離第一批（iso-c1）：小件修補與執行路徑缺口（2026-10-15，ADR-0254）**：第二批「隔離執行環境」在設計確定後另做。
  - 三個執行路徑上的既有漏洞：Manual 模式下演化工具在建環境、裝套件、跑代理人寫的程式之前，現在會先跳一張一般審批卡（以前只有最後部署才問，沒人在場也照跑）；已安裝的 MCP 伺服器啟動時不再拿到 Touka 與各廠商的金鑰，只拿白名單裡的系統變數加上它自己設定的 `env`（創建引擎試跑用同一份白名單）；終端指令失敗後的自我修復提示，現在也涵蓋 `execute_command` 與背景指令的結果。
  - 金鑰與擁有權：設定精靈拒收歷來公開過的金鑰值（回 422，`.env` 不寫）；自訂模型改名失敗時回錯誤、擁有權不動，Windows 上只差大小寫的改名可以成功；建立靈魂時先驗主要模型（不在清單 404、只差大小寫分不出 409、空白當沒指定），不合格就什麼都不建。SECURITY.md 新增四列。
  - 小修：演化沙盒用 venv 裡 Python 的完整路徑（含部署寫進設定的那支，以前部署後的伺服器會起不來）；互審驗證指令的審批卡會顯示審查員轉給你的理由與「只有你能核准」；刪掉走不到的 Proxy 排程增刪改查與 `ENABLE_SCHEDULER`；Windows 上停止 ToukaDispatch 不再卡住；模型工坊的模型下拉和靈魂頁用同一套過濾；設定搜尋拿掉已不存在的取樣參數；IDE 行內補全不再送 temperature；Anthropic 格式入口接非 Claude 模型時帶上 `max_tokens`（子代理與評分卡這兩條內部路徑照舊：子代理不帶上限、評分卡的上限只給 Claude）。
  - 演化從這批起真的會執行：以前 venv 的 Python 路徑錯，裝套件、跑代理人寫的程式、部署那幾步一律失敗，從沒真的跑過；H3 修好路徑後才第一次會真的跑。Manual／Auto 由缺口 1 擋住（先問你）；YOLO 照 YOLO 的定義，連沒人在場也會裝套件、跑代理人的程式。
  - 還原失敗時畫面會顯示錯誤、不再自動重整：新的 admin 唯讀端點 `GET /v1/system/restore/status`，前端重整前先查；還在跑就每 5 秒再查一次。
  - CI：`contracts` job 加鎖定版 ruff `--select F`（四個產品子專案），新增 `toukaide` job（install、test、typecheck、compile）；`CONTRIBUTING.md` 每項標「CI 會跑」或「只在本機」，完整 ruff 與 ESLint 照舊要求。ADR-0238 寫「CI 有跑 Ruff」其實沒有，已在 ADR-0254 更正。新 CI 步驟要等 PR 實跑才算確認。
  - **升級時要做的事**：合併後到 MCP 設定頁看一次每個伺服器的連線狀態。某個 MCP 若靠 Touka 自己的環境變數（例如 `PYTHONPATH`、`HF_HOME`、`HTTP_PROXY`／`HTTPS_PROXY`、Windows 的 `HOME`、公司內部憑證變數 `SSL_CERT_FILE`／`REQUESTS_CA_BUNDLE`／`NODE_EXTRA_CA_CERTS`，或沒寫進它設定的廠商金鑰）會連不上，在那個伺服器的 `env` 補上需要的值即可；這項建議只適用於直接啟動。隔離啟動會移除保留名稱與命中子字串規則的名稱，上述 `PYTHONPATH`、`HF_HOME`、`HOME`、`NODE_EXTRA_CA_CERTS` 都不會傳給目標，詳見 README_QUICKSTART 的 MCP 隔離告知。這次更新不讀使用者的 MCP 設定檔。
  - MCP 環境白名單只擋繼承；C2-3 已另加 stdio 檔案隔離。穩定不可用時的第三方與人同意的 direct 仍有 Touka 的檔案權限，原生 Windows 也在此列；完整限制見 SECURITY.md。ADR-0254 的其他限制列在 §3。
- **隔離第二批（iso-c2）第一、二段：代理人指令的隔離與執行器容器（2026-10-16，ADR-0272）**：依核准的隔離設計（五項決定與細部設計第 2～8 項），10/16 決定把 C2-1＋C2-2 合成這一個 PR，**C2-3（MCP 伺服器的隔離）另開 PR、另一份 ADR、另審一輪**。
  - 代理人撰寫或指定的程式（終端、互審驗證指令、演化的建 venv、pip 與本機驗證）改在常駐容器 `touka-executor` 裡經 Landlock 啟動器執行：看不到金鑰、設定、原始碼與別的代理人資料，連不到也搶不到 Touka 內部埠、Docker 控制埠與從設定推得的本機上游埠。層級只由 `services/isolation` 判定；`none` 時 Auto／YOLO 只有人能核准、無人在場拒絕，審批卡照實標示「在執行器裡／隔離降級／未隔離」並綁定核准。YOLO 無人在場時 dangerous 的 MCP 工具只有人能核准（決定 4）。
  - **MCP 現況由 C2-3 接續**：自建依層級隔離啟動，第三方／scaffold 看本機 Landlock；創建卡依實際層級，部署能力禁止再演化。來源分檔、手動同意與不可用時的處理見本檔最前面的 C2-3 項目。
  - 設定讀不到時不放寬：`auth_policies.json` 啟動讀不到全部拒絕、`autonomy.json` 讀不到取最嚴（這時不要在安全設定頁按存檔）、MCP 設定讀不到時寫入改丟錯；側欄 `system_notices` 寫原因與恢復方法（AGENTS §11 一句）。代理人工作區權限被改時依白名單補回擁有者位元，Touka 自己的資料夾不修（`touka-init`＋重啟）。
  - **升級要做的事**（細節見 ADR-0272 的升級說明）：`docker compose up -d --build`，以後每次更新都要 `--build`；約 90 秒內確認 `touka-executor` 是 healthy，而且 `docker compose logs touka-executor` 沒有 `Executor is not serving`；聊天分頁先重新整理再看橫幅；Manual 下讓代理人跑 `id -u; echo $HOME`，看到 `1001` 與 `/app/executor_state/agents/…`。回復：`docker rm -f touka-executor` 後切回舊版再 `up -d --build`。
  - **會改變的行為**：心跳、SNS 等無人在場的執行不能在專案跑指令；綁專案的對話與 DebugAgent 每條要人核准、在主容器內以檔案隔離跑；代理人在執行器裡起的服務，主容器的工具與 MCP（例如 Fetch）連不到（目前不支援，C2-3 之後再評估）；`~` 換到執行器的暫存區；多三個 volume 與 `touka-exec-net` 網路，dev compose 的映像改名 `touka-proxy:local`。
  - CI：新增 `.github/workflows/isolation.yml`（`landlock` job 每個 PR、push、手動都跑；`container` job 在 PR 改到隔離路徑時自動跑、`isolation` 標籤強制跑），還沒在 GitHub 上跑過。AGENTS.md §2 隔離層級、§11 一句、禁令 66；SECURITY.md 多列；NOQA `IO_PRISON_VIOLATION` +3、`S314` +1；openapi、`api.ts` 重產。經對抗式審查，沒有未解的 BLOCKING／HIGH。

## 待排小項

- ADR-0293 PR 2 留下的後續（理由見 ADR-0293「PR 2」的已知界線）：G4 的 `stat` 類改成測試期間包住 `os.stat`；`touka-init` 改成不跟隨連結的權限初始化；建立靈魂時套用執行器保留名單；`lint_ssot.py` 白名單改成逐條規則或比對 repo 相對路徑；部署後重核程式內容；proxy 本機的 subreaper。
- Docker Desktop（Windows 主機資料夾掛進容器）上還沒手動驗證 ADR-0293。部署後用 DebugAgent 在個人空間依序寫一個檔、讀回、列出、改名、刪除，上傳一個檔，再從 `/v1/outputs` 取一個產出的檔；每一步都應該成功。實際沒有連結卻出現「連結被拒」或 `AGENT_DRIVE_LINKED`，表示這種掛載上檔案身分比對不可用，記下錯誤後回報。
- 讓 docgen 一類的工具安全讀寫代理人檔案（每次呼叫一個新的暫存資料夾、只複製需要的檔案進去、產出以區內寫入搬回；或每個代理人一個程序）：要權限邊界審查，另開任務；完成前 `document-generator` 技能的工具一律被拒。
- C2-3 決策已記於 ADR-0275，仍待整批整合驗證；原先待重跑的執行器突變已拆成 9 項，依既有驗證紀錄全部因預期防線而失敗，不再列為未完成。容器 CI 現有 29 個觸發路徑，包含隔離模型、設定儲存與 stdio 傳輸。
- ADR-0272 的後續候選：執行器層失敗時模型只看到 exit 125；演化在執行器裡裝套件的自動測試與連外網實測；區網網域名（例如 `nas.lan`）的埠自動擋；MPTCP 與 ABI 3 的真核心證據；執行器重啟那一刻讀寫根讀不到時不會自己重試（候選：遇到可恢復的問題每 60 秒重跑 `start()`，不修讀寫根）；綁專案時卡片原因不夠準；自主設定讀不到時安全設定頁的存檔防呆；讓主容器的工具看到代理人在執行器裡起的服務（C2-3 之後評估）。隔離程式的 CI 型別檢查已由 ADR-0278 §7 的 pyrefly 棘輪接上。
