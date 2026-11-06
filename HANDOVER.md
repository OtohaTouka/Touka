# Touka 目前交班

更新：2026-11-05。這份是寫給維護者與 AI 程式代理人的現況交班，先讀 [AGENTS.md](AGENTS.md)。本檔只放總覽；各範圍的細節在下面的模組交班檔，歷史見 [CHANGELOG](docs/CHANGELOG.md)。

## 1. 現況

- **分支**：開源快照（2026-10-22）時 `main` 與 `dev` 內容相同。PR 開到 `dev`，`main` 由維護者從 `dev` 合入。
- **產品版本**：GHCR 最新是 `0.1.1`、`latest`（2026-10-24，公開 main 的 `e6bcfc18`）；`modern-*` 開頭的 tag 是開源前的舊發行。下一次發版：手動執行 GHCR workflow 並填入 `version`，再依 [README_QUICKSTART](README_QUICKSTART.md) 第 4 節升級。
- **發行與 dev 的差距**：上面那個 commit 之後進 `dev` 的改動，要等下一次發版才進 GHCR；有哪些看 [CHANGELOG](docs/CHANGELOG.md)。
- **最近進度**：連結防護（ADR-0293）的 PR 都已合併，後續項目見 [isolation](docs/handover/isolation.md)；Hook 全部事件已接線（[proxy](docs/handover/proxy.md)）；實際驗證（ADR-0295）已上線，見 [tests](docs/handover/tests.md)；隔離的 MCP 部分（ADR-0275）仍待整批整合驗證。
- **目前卡住的事**：
  - 實際驗證找到的既有問題還有兩個沒修（2026-11-04）：模型失敗要按兩個按鈕才能繼續（[chat](docs/handover/chat.md)）、聊天側自動剪裁和畫面保存搶版本，多半剪裁存不進去、有時反過來聊天停住要手動恢復（[proxy](docs/handover/proxy.md)；滑動視窗預設關閉），不擋發版。
  - docgen 一類的工具在安全讀寫做好之前一律被拒（2026-10-30，[isolation](docs/handover/isolation.md)）。
  - Docker Desktop 掛載還沒手動驗證 ADR-0293（2026-10-30，[isolation](docs/handover/isolation.md)）。
  - 審查員的準確率等實測要照禁令 61 逐批取得同意（2026-10-12，[proxy](docs/handover/proxy.md)）。

### 模組交班檔

改到哪個範圍，就讀哪一份。

| 檔案 | 範圍 |
| --- | --- |
| [proxy](docs/handover/proxy.md) | `ToukaProxy/**`：API、工具、模型層、對話流程 |
| [memory](docs/handover/memory.md) | `ToukaMemory/**` 與記憶、舊對話、備份裡的記憶 |
| [chat](docs/handover/chat.md) | `ToukaChat/**`：網頁介面 |
| [ide](docs/handover/ide.md) | `ToukaIDE/**`：VS Code 擴充 |
| [heartbeat](docs/handover/heartbeat.md) | `ToukaHeartbeat/**`：排程與喚醒 |
| [dispatch](docs/handover/dispatch.md) | `ToukaDispatch/**`：對外派工 |
| [isolation](docs/handover/isolation.md) | 隔離執行、MCP 來源、代理人資料夾不跟隨連結（ADR-0272、0275、0293） |
| [deploy](docs/handover/deploy.md) | Dockerfile、compose、發行；更新或升級前要做的事 |
| [tests](docs/handover/tests.md) | `tests/**`、`scripts/**`、評估工具與 CI |

2026-11-04 之前的完整交班內容在 [封存檔](docs/changelog/handover-archive-2026-11.md)。

### 開工前、交付前、審查時

跑 `python scripts/applicable_rules.py`（計畫階段還沒有改動時加 `--paths`，可以給資料夾），讀它列出的規則全文與交班檔。規則怎麼分流見 [docs/rules/README.md](docs/rules/README.md)（ADR-0296）。

### 交班怎麼寫

- 新的未完成事項、已知限制、待部署與操作注意事項寫進對應的模組交班檔，做完就刪掉，紀錄寫進 CHANGELOG。
- 本檔只改總覽、最近一次驗證與產品層的未完成事項。
- 「目前卡住的事」每項寫問題第一次記下的日期，超過 30 天還沒有人動手，就搬到第 3 節或模組交班檔的「待排小項」（已經在那裡的就從這裡拿掉），或寫明原因。第 3 節和待排小項就是候選清單，不算天數。

## 2. 最近一次驗證

每個 PR 的完整驗證結果寫在該 PR 的說明裡；這裡只留一份在 `dev` 上跑完整驗證的摘要，註明日期與 commit，當作本機基準的參考；之後有人在 `dev` 上重跑完整驗證時再換成新的。開源前最後一份逐筆紀錄（2026-10-09～10-16）在公開 repo 第一個 commit `4479c47f` 的 HANDOVER.md 第 2 節，更早的只留在開源前的私人歷史。

- **2026-11-06，依賴批與既有問題 ①②⑥ 之後（dev `93dbfa96`）**：本機 Windows、Python 3.12 venv、Node 24.22.1。
  - 全量 pytest：19213 passed、21 skipped、6 failed。6 個失敗是本機 Docker Desktop 的 `test_local_compose_bindings`（以 CI 為準）。跑的是併入 #87 之前的 `9cff0f3d`，#87 只動前端與文件。
  - guard_all：全綠。

## 3. 尚未完成的產品工作

- 可靠性 R2、prerelease 排程／批准 UI、新來源的登入／模式 UI：尚待實機 GUI 確認。
- P7-B durable MRTR、P9 Tasks（HOLD）、P10 MCP Apps：尚未交付。
- 真實首次使用者安裝與正式回復演練：未執行。
- 已知未解：Bash 事故原因不明。
- 依賴：liblzma 與其他 Debian 套件改跟 `apt-get upgrade` 的官方更新；Python／Node CVE 由 Guard #22／#23 檢查。
- 指令安全修正任務已結案（2026-10-14），由模型層第 2b、2c 段取代；隔離執行環境歸模型層第 5 段（iso-c2）：代理人指令的隔離（含先寫檔再執行、shell 變數與萬用字元繞過）由 ADR-0272（iso-c2 C2-1＋C2-2）處理；C2-3 已接上 MCP 檔案隔離，但直接啟動例外仍無此保護（見 SECURITY.md）。其餘列在下面的待排小項。
- 各範圍的待排小項在模組交班檔的「待排小項」；上面說的「下面的待排小項」就是這些。

## 4. 邊界

- 不直接修改或管理真 `data/`、`soul/`；沒有必要時不讀取 `data/`，不小心讀到或搜尋到不算違規，照實回報即可。`.env` 與私有設定照舊不直接瀏覽、修改或管理；自動化測試一律 sandbox／mock。
- 發版、部署與付費 provider probe 都需要維護者當次授權。
