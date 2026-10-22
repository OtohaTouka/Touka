# ADR-0278：靜態檢查只減不增，並在 CI 執行

- 狀態：✅ 已接受
- 日期：2026-10-22
- 影響範圍：`ruff.toml`（加 ERA）、`scripts/lint_ratchet.py`、`tests/lint/ruff_baseline.json`、`tests/lint/test_ruff_ratchet.py`、CI `frontend` job（Guard #35）、`scripts/guard_eslint_ratchet.py` 與 `scripts/eslint_baseline.json`、`scripts/lint_ssot.py` 的業務層判斷、`CONTRIBUTING.md`「PR 前必做」、`tests/lint/test_ci_pr_checks.py`、root 開發依賴 ruff 釘版
- 相關：[ADR-0098](0098-eslint-ratchet-guard.md)（ESLint 棘輪 Guard #35）、[ADR-0254](0254-iso-c1-small-fixes-and-execution-gaps.md)（CI 加 ruff F、CONTRIBUTING 標示；其已知問題中的「ESLint 基準過時」「完整 ruff 沒有棘輪」由本 ADR 處理）

## 背景

- GitHub CI 只跑 ruff 的 F 規則。完整 ruff 在四個產品資料夾還有 264 個舊違規（10/22 量，33 條規則），只在本機要求「改到的檔不比改之前多」，沒有機器執法。
- ESLint 棘輪 Guard #35 只在本機的 guard_all 跑，CI 沒有；基準記 6 個，實際已是 0 個。
- `lint_ssot.py` 用子字串比對整條路徑判斷「業務層」。頂層 `tests/` 底下檔名含 `tools`、`service` 的 32 個測試被當成業務程式，結構測試不准讀檔，只能改檔名避開。

## 決定

### 1. 完整 ruff 只減不增，在 pytest 裡執法
- `scripts/lint_ratchet.py`（只用標準函式庫）是唯一的收集點：
  - 對四個產品資料夾跑 `ruff check --no-cache --config ruff.toml --output-format json --exit-zero`，範圍和 CI 的 F 步驟相同；
  - 只讀 JSON 結果，依規則代號計數；
  - `--update` 重拍基準。
- 基準是 `tests/lint/ruff_baseline.json`。`tests/lint/test_ruff_ratchet.py` 要求每條規則的數量**完全相等**：
  - 變多要修；
  - 變少要在同一個 PR 重拍基準，改善才會留在差異裡、之後不會被悄悄用掉。
- CI 本來就跑 `pytest tests/`，所以不改 workflow、也不新增 guard_all 守衛，守衛數量和寫著這個數字的文件、測試都不用動。
- `ruff.toml` 加 ERA（被註解掉的程式），產品程式現有 17 個先凍結在基準。
- 開發依賴的 ruff 改成 `==0.15.20`。之後升級 ruff 要在同一個 PR 重拍基準，並在 PR 說明數字的變化。

### 2. ESLint 棘輪進 CI
- CI `frontend` job 最後一步從 repo 根目錄跑 `python3 -B scripts/guard_eslint_ratchet.py`。守衛只用標準函式庫，runner 內建的 python3 即可。
- `tests/lint/test_ci_pr_checks.py` 釘住兩件事：這一步要在安裝依賴與 braces 補丁之後跑，而且不能被 `if` 或 `continue-on-error` 跳過。
- 基準收緊成 `{}`。守衛原本把空基準當成「沒設定」而失敗，改成只有基準檔不存在才算沒設定。
- 兩者規則不同：Guard #35 照 ADR-0098 是「超過才失敗」，ruff 棘輪是「完全相等」。ESLint 不改，因為兩支既有測試直接替換它的內部函式，改動風險大於好處。

### 3. `lint_ssot` 不把頂層 `tests/` 當業務層
- **只認 repo 頂層的 `tests/`**：
  - 路徑裡有 `..` 一律照舊判斷，因為它可能從 `tests/` 繞回產品資料夾；
  - 相對路徑用字面判斷，第一段必須是 `tests`；
  - 絕對路徑用 `absolute()` 後，看是否在 repo 根的 `tests` 底下。兩種判斷都不跟隨連結。
- **其他路徑完全照舊比對**。比對不上時照舊判定，只會更嚴，例如 `ToukaProxy/tests/`、`tests_old/`、`src/tests/`、Linux 上含反斜線的字串。
- **證據**：用 `run_guard(".")` 的相對路徑和絕對路徑兩種寫法，比對全部 1370 個版控中 .py 的改前改後判定。只有 32 個頂層 `tests/` 的檔案從「業務層」變成「不是」，沒有任何檔案從「不是」變成「是」，產品程式一個都沒變。
- 這是政策改變：測試本來就不受 IO 監獄等業務層規則約束，只是以前被檔名誤判。

### 4. CONTRIBUTING：CI 綠燈就能開 PR
- 「PR 前必做」每一項標「CI 會跑」或「維護者合併前補跑」。
- CI 綠燈就能開 PR；guard_all 與 ToukaIDE 打包由維護者在合併前補跑。
- 完整 ruff 與 ESLint 改標「CI 會跑」。

### 5. `tests/README.md` 只列檔名與說明，不寫案例數（2026-10-22 增補）
- **為什麼不自動核對數量**：README 原本有 606 列手寫的數量，寫法不一：
  - 4 張表有數字欄；
  - 有的寫在粗體標籤裡，例如 `**41 cases**`、`**ADR-0245（8）**`；
  - 還有合計句。
  - 至少 9 列已經對不上。Linux 專用的隔離測試在 Windows 收集到 0 個，自動核對數量在本機和 CI 一定有一邊對不上。
  - 所以拿掉全部案例數，CI 的結果才是正本。
- **保留的數字**：HTTP 狀態碼、檔案操作數、歷史驗證紀錄裡的 passed 數，以及人工 live 評測的預算（案例數與請求上限，禁令 61 的同意依據）。這些不是案例數，所以保留。
- **列表核對**：`tests/lint/test_tests_readme_listing.py` 要求版控中的每個 `tests/**/test_*.py` 都在 README 用反引號列出：
  - 有相對路徑就比對相對路徑；
  - 只寫檔名時，檔名在 `tests/` 裡必須唯一；
  - 寫在說明文字裡的不算。
- **凍結清單**：當時沒列的 212 個凍結在 `tests/lint/tests_readme_unlisted.json`，清單只准變短。已補列或已刪除的檔案還留在清單裡，也算失敗。
- **其他**：
  - 同一支測試也擋 README 再寫案例數；
  - 用 git 取得檔案清單，沒有 git 就失敗、不跳過；
  - AGENTS.md「任務後文件更新協議」改成「新增測試檔 → tests/README 加一列（檔名、說明）」。

## 已知限制

- **每條規則只算總數**：在 A 檔修一個、在 B 檔加一個，數字不變，擋不到。這是刻意的取捨，和 Guard #35 相同，搬移程式才不會誤擋。
- **壓掉不修，不會被計數**：用 `# noqa: E501` 這類標準規則代號或 per-file-ignores 壓掉的違規不算數。Guard #19 只核對 NOQA_REGISTRY 統計表有列的規則。
- **兩個 PR 同時重拍基準**：兩個 PR 各自把同一條規則從 45 修到 44 並重拍時，兩邊的基準改動一模一樣，git 會乾淨地合成 44，但實際已經是 43，合併後 dev 會轉紅。合併規則只核對 PR 自己的 CI。所以合併前如果 dev 已經進了另一個重拍基準的 PR，要先把 dev 合進來重跑 CI。
- **自訂豁免標記的警告**：Touka 的 `# noqa: ENV_SSOT_VIOLATION` 這類標記會讓 ruff 印出約 110 個「無效的 noqa」警告。ruff 只認「字母＋數字」格式的代號，10/22 實測 ruff 0.15.20 的 `lint.external` 消不掉。改寫成別的標記要動 166 行、Guard #19 與規則文字，這次不做；棘輪只讀 JSON 結果，不受影響。

## 後續

同一任務的後續 PR 會在本 ADR 增補：
- Python 型別檢查（pyrefly）只減不增；
- 註解與內部用語清理，ERA 降到 0。
