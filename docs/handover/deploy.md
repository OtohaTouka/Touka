# 部署與升級交班

範圍：Dockerfile、compose、發行；更新或升級前要做的事。

這份只放這個範圍沒做完、有未解限制、做完還沒部署或要特別操作的事。總覽見 [HANDOVER](../../HANDOVER.md)，舊內容見 [封存](../changelog/handover-archive-2026-11.md)。做完一項就從這裡刪掉，紀錄寫進 [CHANGELOG](../CHANGELOG.md)。

## 現況

- **GHCR 的 `touka-init` 遇到特殊項目改成略過（2026-10-23，ADR-0279）**：以前資料裡只要有一個符號連結（Hugging Face 模型快取一定有）、FIFO、socket、多連結檔、setid、延伸屬性就整個拒絕，用過記憶的 GHCR 部署下一次 `up` 起不來。現在這些項目不開啟、不跟進、不修改，結果多一個 `skipped` 計數；四個掛載根目錄與兩個代理人工作區根目錄照舊嚴格；最後驗證連略過的一起比對，中途被換掉仍失敗；代理人或伺服器資料夾底下 `SCAN_SKIP_NAMES` 那些工具資料夾不往下走（init 裡的副本由測試釘住）；還要補擁有者權限位元的 setgid 資料夾在盤點時就拒絕、什麼都不改（補位元會被核心清掉 setgid）。手動跑 init 前先停 executor、heartbeat 與 proxy。CI 容器 job 用建好的 Proxy 映像在真核心上跑 `tests/docker/permission_init_real.py`；觸發路徑加 `permission_init.py`。原始碼版 compose 不受影響。

## 也和這裡有關

- 工具資料夾與暫存區也不跟隨連結；GHCR 升級前要先建資料夾：見 [isolation.md](isolation.md)。
- 沙盒規則不再跟著連結給權限；data 根只認兩種寫法：見 [isolation.md](isolation.md)。
- 刪除湧現指數與端點；外部客戶端要自己拿掉這些欄位：見 [proxy.md](proxy.md)。
- 兩個調度員技能檔改啟動時產生；更新前要先 git restore：見 [proxy.md](proxy.md)。
- 刪靈魂前先清隔離家目錄；舊 ID 要手動刪，密碼錯回 403：見 [isolation.md](isolation.md)。
- 心跳與 SNS 改每靈魂頻道；所有 writer 要一起升級：見 [heartbeat.md](heartbeat.md)。
- MCP 伺服器隔離已接線；升級要重建，仍有多項限制：見 [isolation.md](isolation.md)。
- 不送取樣參數、沒設模型回 409、uv 冷卻期；有後續未做：見 [proxy.md](proxy.md)。
- 全停 Gemini 顯式快取、補金鑰範本；uv 映像待確認：見 [proxy.md](proxy.md)。
- 聊天要登入、Guest 退役、群聊只限靈魂；舊設定讀取時略過，群聊頁存檔會刪掉舊區塊：見 [proxy.md](proxy.md)。
- 升 anthropic SDK、加規則表；舊簡稱不再自動轉，有剩餘風險：見 [proxy.md](proxy.md)。
- 主要模型分兩欄、模型名不分大小寫；現有值要自己確認：見 [proxy.md](proxy.md)。
- 本機測試前置、建置產物宣告、擋公開金鑰；升級要 compose up：見 [proxy.md](proxy.md)。
- 演化先問人、MCP 不再拿金鑰；升級要檢查 MCP 連線：見 [isolation.md](isolation.md)。
- 記憶庫升級、向量失敗不刪事實、記憶來源；有限制與回復步驟：見 [memory.md](memory.md)。
- 修好快照與重置；有記憶的靈魂不能直接刪；快照目錄改位置：見 [proxy.md](proxy.md)。
- 記憶可取代並保留舊版、加安全標記；啟動時自動升級記憶庫：見 [memory.md](memory.md)。
- 對話有時間戳、舊對話直接歸檔；剪裁會開始真的生效：見 [memory.md](memory.md)。
- 門檻依模型查表；EMBEDDING_MODEL 要寫完整；只量 e5-base：見 [memory.md](memory.md)。
- 新增可信活動時間軸，心跳也注入；關機時事件可能丟：見 [memory.md](memory.md)。
- 模型只能叫這一輪列出的工具；舊短名靈魂要重新勾選：見 [proxy.md](proxy.md)。
- 可選擇一併清除備份裡的記憶；VPS 的 nginx 要補設定：見 [memory.md](memory.md)。
- 5.5 小工作少想、價目修正；審查員等待要手動改成 60：見 [proxy.md](proxy.md)。
- 代理人指令進執行器容器；升級要 --build 並確認 healthy：見 [isolation.md](isolation.md)。
- ADR-0293 的後續含 `touka-init` 改成不跟隨連結的權限初始化、部署後重核程式內容：見 [isolation.md](isolation.md)。
- Docker Desktop 上還沒手動驗證 ADR-0293，部署後要用 DebugAgent 跑一輪檔案操作：見 [isolation.md](isolation.md)。
- Heartbeat 的 8082 排程 API 在容器重建前仍接受原本的金鑰值，執行一次 `docker compose up -d` 後關上：見 [heartbeat.md](heartbeat.md)。

## 待排小項

- 容器裡另掛 `/app/backups` volume（不讓快照位置綁在 `.env` 上）留作候選，要動兩份 compose 與 GHCR 的 `permission_init.py`（ADR-0262）。
- 快照腳本手動執行時 PYTHONPATH 要含 `ToukaMemory/src`；快照中途當機會在 `backups` 留下 `.*.partial`／`.snapshot-*` 暫存（勾選清除連備份時會清掉夠舊、形狀完全符合的，其他要手動刪，ADR-0270）；`ui_sessions.db`、`feedback.db`、`legacy_history.db` 仍直接複製；還原子程序的 stderr 直接流進伺服器 stderr、沒有消毒（ADR-0262）。
- 命令列快照連續 600 秒沒有寫入（行程被暫停、或檔案系統時鐘比 Proxy 慢 10 分鐘以上）時，勾選清除連備份會把它的暫存當成中斷殘留刪掉，那次備份在最後改名時失敗、不留壞檔；暫存檔本身被占用刪不掉時會留下 `.partial`，只警告（ADR-0262 與 ADR-0270 的 2026-10-25 補記）。
