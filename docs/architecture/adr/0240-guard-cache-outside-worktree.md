# ADR-0240：Guard #16 沙盒快取移出 worktree（git common dir 共用＋成員鎖）

- 狀態：✅ 已接受（Owner 2026-10-10 核准計畫，快取位置選 git common dir）
- 日期：2026-10-10
- 決策者：Owner
- 影響範圍：`scripts/guard_dependencies.py`（Guard #16）、`.gitignore` 註解、`tests/unit/test_guard_dependencies_*.py`
- 相關：ADR-0016（依賴沙盒驗證）、ADR-0019（Guard #16 residue 前置）、ADR-0117（uv workspace 單一鎖檔）、ADR-0222（工具暫存放 repo 外的先例）

## 背景

Guard #16 為每個 workspace 成員建立一個沙盒 venv，快取鍵是 `uv.lock`＋成員 `pyproject.toml`。快取原本寫死在 `scripts/.cache/guards/`，也就是每個 checkout 自己的資料夾內，主資料夾實測約 1.76 GB（Proxy 與 Memory 各有一份完整 torch）。

- 在 `.toukadev/worktrees/` 的工作資料夾跑過 Guard #16 後，資料夾內留下被 git 忽略的大型 `scripts/.cache/`，清理前檢查因此停下。
- 每個新工作資料夾都要重建一次約 1.8 GB，即使鎖檔完全相同。
- 成員都是虛擬套件（`package = false`），venv 內沒有專案原始碼；匯入測試用目前 checkout 的 `PYTHONPATH` 與 cwd。所以 venv 內容只取決於鎖檔、成員 pyproject 與直譯器，可以跨 worktree 共用。

## 決策

1. **位置**：執行時以 `git -C <PROJECT_ROOT> rev-parse --path-format=absolute --show-toplevel --git-common-dir` 解析，快取根為 `<git common dir>/touka-guard-cache/<sys.platform>/`。主資料夾與所有 linked worktree 解析到同一處（主資料夾的 `.git`），不寫死任何路徑；依平台分資料夾，Windows 與 WSL 的 venv 和鎖不會共用同一個資料夾。
2. **退回**：以下情況印出原因並退回舊位置 `scripts/.cache/guards`（沿用舊佈局，不分平台）：
   - git 無法執行；
   - 非零結束（例如 dubious ownership）或輸出不是兩行；
   - toplevel 不是這個 checkout（例如 Touka 被放在別的 git repo 的子資料夾裡），避免把快取寫進別人的 `.git`；
   - common dir 不是目錄。
3. **成員鎖**：每個成員一把 OS 檔案鎖 `<root>/<成員>.lock`（Windows `msvcrt.locking`、POSIX `fcntl.flock`，只用標準函式庫）。鎖住整段檢查：快取比對、重建、匯入測試與 Dockerfile parity，別的執行不會在匯入途中刪掉 venv。每個成員各自最多等 240 秒，逾時以 FAIL 結束並說明「another Guard #16 run holds the cache lock」，不會拿半成品通過。240 秒是每個成員各算，不是整次執行；guard_all 對整個 Guard #16 只給 300 秒，可能在印出原因前就殺掉整次執行，結果同樣是 FAIL。鎖檔在成員目錄外，重建時的 `rmtree` 不會刪到它；行程結束或被殺時 OS 自動釋放。
4. **`--cache-only` 仍零寫入**：不建立快取根；鎖檔已存在才上鎖，不存在就照原流程因快取缺失而 FAIL。
5. **順序不變**：residue 掃描（ADR-0019）仍在任何快取工作之前，位置解析排在它之後。
6. **舊快取**：解析出的根不是舊位置、而舊目錄存在時，只印提示「已不再使用、可手動刪除」，不自動刪除。
7. **快取鍵含建置配方**：`uv sync` 的 argv 只組一次，同一份同時用來執行與算鍵。鍵為 sha256(`{"schema": CACHE_KEY_SCHEMA, "argv": …, "platform": sys.platform}` 的 JSON ＋ `.python-version`（有的話）＋ `uv.lock` ＋ 成員 `pyproject.toml`)。同一份鎖檔但配方不同（例如換 `--package` 或加旗標）一定重建。**改變 argv 以外的建置方式（例如傳給 `uv sync` 的環境變數、快取內的 venv 佈局）時，必須遞增 `CACHE_KEY_SCHEMA`**，讓所有共用快取失效。

## 替代方案

| 方案 | 不採用的理由 |
| --- | --- |
| B. 使用者快取（`%LOCALAPPDATA%`／XDG） | 要自寫三平台規則或新增依賴；刪專案後留下孤兒。跨 clone 共用本專案用不到 |
| C. 系統暫存（`tempfile`） | 會被系統清理，隨機觸發數分鐘重建；不適合長期快取 |
| D. 只放 `CACHEDIR.TAG` | 清理前檢查不認；uv 已在 venv 放標記，仍被擋 |
| E. 改名混進 `.venv/` | 只是騙過檢查，且會被 `uv venv` 重建時清掉 |
| F. 每個 worktree 各自放在 repo 外 | 不共用，worktree 刪除後留下看不見的 1.8 GB 孤兒 |

## 後果

- 工作資料夾內不再留下 Guard #16 快取；依賴相同的新工作資料夾直接命中快取。
- 快取跟專案同一顆磁碟、同一個資料夾，刪專案即全清。它在 `.git` 內但不是 git 物件，不會被 commit、push 或進 bundle；Docker context 本來就排除 `.git`。
- 合併後第一次執行會在新位置重建一次。只用 `--dependency-cache-only` 的流程會先 FAIL，要先跑過一次一般模式。
- 舊的 `scripts/.cache/guards` 不再使用，是否刪除由人類決定；`.gitignore` 規則保留給退回情況。

### 已知限制

- **不同鎖檔會互相重建**：每個平台每個成員只有一個目錄。兩個工作資料夾的 `uv.lock`（或成員 pyproject）不同時，輪流執行會在兩者之間來回重建，每次數分鐘。這次刻意不依 hash 分資料夾，避免快取無限長大。
- **`--dependency-cache-only` 會被別的工作資料夾打斷**：另一個 `uv.lock` 不同的工作資料夾重建過之後，本資料夾的 cache-only 執行會 FAIL，要再跑一次一般模式。
- **直譯器的實際版本不在快取鍵裡**：`.python-version` 的內容在鍵裡，但 uv 實際選到的直譯器（例如 3.12.13 換成 3.12.14，或 `UV_PYTHON` 指定別的直譯器）不在。這是原本就有的限制，不是本決策新增的。
- **`git clean -xfd` 清不到這個快取**：它在 `.git` 裡。要強制重建某個成員，刪除 `.git/touka-guard-cache/<平台>/<成員>`。
- 別的執行冷重建超過 240 秒時，等待的一方會 FAIL，需要重跑。
- POSIX 上，鎖被握住時鎖檔仍可被刪除，之後的執行會建立新鎖檔而不互斥。只要不手動刪鎖檔就不會發生（只記錄，不處理）。

## 回復

不要直接 revert 整個合併：之後新增的 ADR 會讓編號連續性檢查（Guard #28）因 ADR-0240 消失而變紅。改為：

1. 只 revert 程式與測試：`scripts/guard_dependencies.py`、`tests/unit/test_guard_dependencies_cache_location.py`（刪除）、`tests/unit/test_guard_dependencies_native.py`、`tests/unit/test_guard_dependencies_pytest_residue.py`。Guard #16 回到 `scripts/.cache/guards`，下次執行時在舊位置重建。同時還原 `.gitignore` 中 Guard #16 快取的註解（說明退回位置的那兩行）與 `docs/architecture/SSOT_MASTER_MAP.md` 的「依賴主權 (Deps)」列。
2. 保留本 ADR 檔，狀態改為「⛔ 已撤回」並寫明原因；`adr/README.md` 索引列同步改狀態；`docs/CHANGELOG.md` 加一列。
3. `.git/touka-guard-cache/` 會留下（約 1.8 GB／平台），它是可重建快取，留下或刪除由人類決定。

沒有資料格式或 schema 變更。
