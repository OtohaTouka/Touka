# 測試與守衛交班

範圍：`tests/**`、`scripts/**`、評估工具與 CI。

這份只放這個範圍沒做完、有未解限制、做完還沒部署或要特別操作的事。總覽見 [HANDOVER](../../HANDOVER.md)，舊內容見 [封存](../changelog/handover-archive-2026-11.md)。做完一項就從這裡刪掉，紀錄寫進 [CHANGELOG](../CHANGELOG.md)。

## 現況

- **兩份凍結清單（2026-11-05，ADR-0298）**：`tests/lint/vulture_baseline.json`（後端未用的函式、方法、類別 108 筆）和 `ToukaChat/scripts/contract_type_baseline.json`（和後端合約同名的手寫型別 10 個）都沒有清掉，也沒有期限，清理在候選裡。
  - 死碼清單只有多出來才紅。只有測試在用的新函式、先加函式下個 PR 才接上、搬動或拿掉檔案（可能一次多好幾筆）都會紅：接上，或 `python scripts/lint_ratchet.py vulture --update` 並在 PR 說明逐項列出。`--update` 也會收下新增項目，`# noqa` 和白名單也能藏項目，靠審查看。
  - 合約清單要完全相等，後端改 schema 名時常常要一起重拍（`npm run guard:api -- --update`）。
  - 這兩道都可能和同時進行的 PR 撞：各自綠、合起來紅時 dev 會紅，合併前先把 dev 合進來重跑 CI。
  - 清死碼時先看名字和安全有關的：`is_safe_outbound_url`、`verify_secret_strength`、`create_mission_token`、`validate_mission_token`、`revoke_mission_token`、`_has_chaining_metacharacters` 在清單裡，產品程式查不到呼叫端，沒有查過是不是漏接。
- **實際驗證（2026-11-04，ADR-0295）**：`cd ToukaChat && npm run test:e2e:real` 用 Playwright 走打包好的正式版畫面與真的 Proxy，模型是假的，資料在沙盒，不花額度。目前走 Hook 全部事件與審批卡（`ToukaChat/e2e-real/`；假 MCP 在 `tests/e2e/fake_mcp_server.py`，心跳由測試帶權杖打喚醒端點）。走新路徑時走完再送一則：這次的既有問題都是送第二則才看到的（ADR-0295）。`hooks-lifecycle.spec.ts` 的 `recoverSave`、`sendRecovering` 是繞過既有問題用的，修好之後要拿掉。GitHub 上每個進 dev、main 的 PR 都跑（`e2e-real.yml`），先不列為必過。
- **CI 的 `contracts` 工作上限 60 分鐘（2026-11-04）**：全量 pytest 在 GitHub 上已經跑到 36～40 分鐘，原本 40 分鐘的上限會把它取消（看起來是失敗，其實是時間到）。再變長時先看哪些測試慢，不要只加上限。
- **維護流程的本機紀錄資料夾 `.toukadev/`（2026-10-10）**：開發流程本身（不是產品功能）的變更。
  - AGENTS.md「文件協議」加上一行：流程或規則出問題時，記到主要專案資料夾的本機 `.toukadev/lessons/`（不進版控）。
  - `lint_ssot.py`、`guard_noqa_registry.py`、`guard_adr_integrity.py` 這三支從 repo 根目錄遞迴走訪的守衛，依資料夾名稱略過 `.toukadev`（只比對名稱，不是路徑逐段比對，所以在 `.toukadev/worktrees/` 裡的工作資料夾跑守衛仍會正常掃描）。
  - `.dockerignore` 排除 `.toukadev`（proxy 映像用 root context＋`COPY . .`）；`.gitignore` 加 `.toukadev/`，並由測試確認版控中沒有任何 `.toukadev` 路徑。
  - 三支守衛各自維護一份幾乎相同的略過清單（`IGNORED_DIRS`），是既有狀況；本次只各加一項，沒有合併成共用清單。之後改略過清單要三處一起改，`tests/unit/test_guard_source_data_boundary.py` 會對三支跑同一組案例。
  - 合併後實地驗證已做過：在主資料夾直接跑這三支守衛，確認不會掃到 `.toukadev/worktrees/` 底下的副本。
- **Guard #16 快取移出工作資料夾（2026-10-10，ADR-0240）**：
  - 新位置：git common dir 下的 `.git/touka-guard-cache/<平台>/`，主資料夾與所有 worktree 共用；每個成員一把 OS 檔案鎖，同時執行時等待，每個成員最多 240 秒後 FAIL。沒有 git、或這個 checkout 不是 git toplevel 時，退回 `scripts/.cache/guards`。
  - 快取鍵含建置配方（`uv sync` argv、平台、`CACHE_KEY_SCHEMA`）與 `.python-version`；改變 argv 以外的建置方式要遞增 `CACHE_KEY_SCHEMA`。
  - 合併後第一次執行會在新位置重建一次（數分鐘）；只用 `--dependency-cache-only` 的流程要先跑過一次一般模式。
  - 已知限制見 ADR-0240：`uv.lock` 不同的工作資料夾會互相重建、cache-only 會因此 FAIL、`git clean -xfd` 清不到（強制重建請刪 `.git/touka-guard-cache/<平台>/<成員>`）。
  - 回復：不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。只 revert `scripts/guard_dependencies.py` 與三個 Guard #16 測試檔，並還原 `.gitignore` 的 Guard #16 快取註解與 SSOT_MASTER_MAP 的 Deps 列；ADR-0240 保留並改為「⛔ 已撤回」，CHANGELOG 加一列；`.git/touka-guard-cache/` 會留下，舊位置會在下次執行時重建。
- **規則減負：根 AGENTS.md 小修（2026-10-15，ADR-0252）**：規則減負的一部分。
  - 核心守則 15 句被禁令原文完整涵蓋，改成「見禁令 N」或刪；「核心能力清單」移到 README「核心功能」（只加 README 沒有的 10 項）；禁令 41、47、49 的把關名稱改成現況，禁令 15 補一句指向 `config.PUBLIC_SECRET_VALUES`。禁令編號與開頭格式不變；根檔 77,874 → 74,628 B（LF）。
  - ADR 歸檔（第二個 PR）：已退役或只記流水帳的 42 份 ADR 已換成存根，已歸檔清單在 ADR-0252。已知限制與之後的候選見 ADR-0252。
- **記憶系統第二批 e1b：評估工具測得了 5.5 家族（2026-10-15，ADR-0268）**：只改評估工具與測試，產品程式 0 行。
  - 輸出上限改比對 Touka 實際要送的值（Anthropic 照規則表，5.5 是 8,192；其他廠商照請求值），工具天花板 8,192，超過整批不送；核准範圍同時印請求值、實送值、天花板與規則。
  - 每個請求送出前檢查「已花＋最壞花費」不超過 `--budget`：輸出上限用滿，加上請求內容每字元 1 token＋1,024，輸入取一般與寫入快取較貴的價；已花的請求照實際金額、沒回報用量的照最壞。
  - 改名只認句首、沒有否定、不是問句的自稱；「句尾加喵」看真正的句尾；口令洩漏照舊嚴格；10/15 三個誤判原文當回歸案例。
  - 結束先印報告（含逐請求輸入、快取、輸出含思考 token），再等稽核寫入、關紀錄檔、SQLite、Qdrant 後刪沙盒；刪不掉以非零結束。回應摘要去敏並截 200 字。
  - **合併順序**：M1-d 已在舊版工具上重跑完，本段才合併；5.5 實測（`claude-haiku-5-5`）要另外逐次同意，等本段合併後才跑。核准範圍的上限欄名改了（見 `docs/proxy/live-check-backlog.md`）。
  - openapi、Guard #45 基準、NOQA、AGENTS.md、權限設定檔都不變。

## 待排小項

- `EnvSovereigntyRule` 的 `os.system`、`os.chdir` 仍只看 `/src/`，Heartbeat、Dispatch 掃不到（禁令 42；ADR-0298 只處理 7、10）。Ruff TID251 掃得到這兩個資料夾，但 `ToukaDispatch/server.py`、`**/awaken.py`、`**/config.py` 豁免，`server.py` 用了 `subprocess.Popen`。
- vulture 不看 `.gitignore`：有人在四個產品資料夾底下放沒進版控的 `.py`（例如 `ToukaHeartbeat/.venv`），死碼檢查會多紅（ADR-0298）。
- `scripts/update_docs.py` 改了匯入順序後沒有實際執行過（會跑 mkdocs build），下次要產生文件時順便確認。
- Guard #22 的 pip-audit 空輸出：2026-10-21 起自動重跑一次，仍空就回報「掃描沒跑成」並判紅；【推測】是查詢服務暫時沒回應，根因沒抓到，下次遇到用 `guard_all.py --audit-evidence-root` 保留 stderr 看原因。
- 【推測】禁令 22 的轉址守衛用檔案系統掃描，以後若有人在 ToukaHeartbeat 或 ToukaDispatch 底下建自己的 `.venv`，守衛會掃到第三方套件而變紅。
- 強制結束（工作管理員、kill、當機）的 pytest 仍會在系統暫存留下 `touka_test_session_*`；「下次執行時清掉以前留下的」延後，再累積時做（ADR-0251）。
- `tests/unit/test_soul_primary_model_choice.py` 在 Windows 本機時好時壞：建立靈魂時 `invalidate_agent_sessions` 偶爾回 `SESSION_SAVE_FAILED`（例外被吞掉，沒有記下原因）。10/25 在 dev `4e5c85c5` 的乾淨複本上單獨跑 4 次，3 次各有 1～4 個失敗；整套 pytest 時常常剛好過。還沒查原因，CI 上是否也會發生未知。
- Guard #45 基準檔裡 `ToukaProxy/src/tools/mission_tools.py` 的 `mission_tools`（typed）讀取快照和程式不一致，是早期改動留下的、不在 ADR-0256 範圍；守衛不比對這部分所以不會變紅，但這份快照不能拿來當現況參考。
- Guard #45 基準裡 5 條記憶相關的有型別快照（`MemoryManager.save_memory`／`query_memory`／`resolve_duplicate`、`diag.manage_duplicate_memories`、Proxy 閘道 `memory_tools`）比程式舊；守衛不比對這部分，不會變紅（ADR-0263）。
- 「對話不再自動刪除」（P28 撤回）的測試只看資料庫初始化（`init_db_async`）：`main.py` 啟動流程的其他步驟或排程裡另外加的清除，測試測不到，要靠審查（ADR-0259 補記 2026-10-28）。
- 計分卡與 tool_eval episode CLI 拒絕 Gemini 的說明「Gemini adapter 目前含內建 retry」已不準（真正原因是只包了 Claude／OpenAI 相容的 send_round），仍維持拒絕（ADR-0261）。
- 記憶評估工具修好後（替身名稱固定、「昨天」逐子句判、時間軸套件；ADR-0284 每個情境獨立靈魂、兩題要靠記憶、判定改嚴、每次新口令、實測裝上 UI 對話讀取器）在 2026-10-27 照新版跑了 smoke 與 continuity 各 5 輪：smoke 44／55（80%）、continuity 10／10；記憶衝突與長對話是模型真的沒做到，相對時間大多是判定錯。同日 ADR-0284 修訂判定（日期與時刻不算時長、「摘要卻寫著」算轉述、判定細節先列衝突）後重跑：smoke 47／55（85%）、continuity 10／10；相對時間沒過的 3 次又是判定錯（「哪一天」「哪一個週」被讀成 1 天、1 週），同日再修訂（指某一天的說法與「週末」不算時長）後，2026-10-28 重跑當作新的基準：smoke 45／55（82%）、continuity 10／10，相對時間 5／5；記憶衝突與長對話都 0／5，多半是模型沒做到（記憶衝突 3 次沒存新偏好；2 次存了仍答拿鐵，是模型選錯還是查詢只回拿鐵無法確認；長對話只查長期記憶、沒查舊對話），比 10/27 差，但查過 10/27 重跑之後進 dev 的 #45～#47，看不出是產品改動造成（ADR-0284 之後三次合計 3／15、5／15，產品在三次之間也有改，只當粗估；長對話和 10/27 比接近要追查的程度）；結果記在 `docs/proxy/live-check-backlog.md`。ADR-0284 第二部分已加重跑（1～5 輪，輪數進核准範圍）、每題 k/n 與整批 Wilson 範圍、兩次結果的比較工具（`python -m tests.memory_eval.stats`）、評估程式雜湊、價目表查不到的型號拒跑與家族估價標示、請求上限 200；smoke 與 continuity 是兩批，揭露草稿在 `docs/proxy/live-check-backlog.md` 的 E1／E2，每批照禁令 61 另外取得同意（ADR-0280）。評估工具的後續候選：空回覆時記下廠商回的結束原因（M1 DeepSeek 空回覆查不出原因）；實測改用真的 e5（約 1.13 GB、只從 Hugging Face 下載，也測得到自動帶入記憶）；實測腳本現在把整份 `.env` 灌進環境，可以改成只取廠商金鑰。
- `scripts/lint_ssot.py` 用「完整路徑含 `tools` 等子字串」判斷業務層，測試檔名含 `tools` 時會被當成業務層而誤判 I/O 違規（h2 改檔名避開，守衛未改；已列為候選）。
