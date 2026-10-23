# ADR-0278：靜態檢查只減不增，並在 CI 執行

- 狀態：✅ 已接受
- 日期：2026-10-22（2026-10-23 增補 §7，以及 §2 升級帶進新規則）
- 影響範圍：`ruff.toml`（加 ERA）、`scripts/lint_ratchet.py`、`tests/lint/ruff_baseline.json`、`tests/lint/test_ruff_ratchet.py`、CI `frontend` job（Guard #35）、`scripts/guard_eslint_ratchet.py` 與 `scripts/eslint_baseline.json`、`scripts/lint_ssot.py` 的業務層判斷、`CONTRIBUTING.md`「PR 前必做」、`tests/lint/test_ci_pr_checks.py`、root 開發依賴 ruff 釘版；`tests/README.md`、`tests/lint/test_tests_readme_listing.py` 與 `tests_readme_unlisted.json`、AGENTS.md 文件協議一列；程式與測試的註解（ERA、內部用語、決定代號）；pyproject.toml 的 `[tool.pyrefly]` 與 root 開發依賴 pyrefly 釘版、`tests/lint/pyrefly_baseline.json`、`tests/lint/test_pyrefly_ratchet.py`
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
- **升級帶進新規則時（2026-10-23 增補）**：做法和 §1 的 ruff 升級相同，新規則抓到的既有違規在同一個 PR 凍結進基準，並在 PR 說明；新寫的程式照樣被擋，舊的之後只准減少。第一次是 Next.js 16.4.2：eslint-config-next 16.4.2 要求 eslint-plugin-react-hooks `^7.1.0`，7.1 新增 error 級的 `react-hooks/set-state-in-effect`（18 處）與 `react-hooks/refs`（1 處），基準從 `{}` 變成這 19 個。不把外掛留在舊版，因為 `test_cve_2026_75604_nextjs_security.py` 要求 eslint-config-next 和 next 同版。

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

### 5. `tests/README.md` 只列檔名與說明，不寫「現在有幾個案例」（2026-10-22 增補）
- **為什麼不自動核對數量**：README 原本約 420 行寫了數量，寫法不一：
  - 4 張表有數字欄；
  - 有的寫在粗體或標籤裡，例如 `**41 cases**`、`**ADR-0245（8）**`、`**15 collected**`、`（28 個）`；
  - 還有合計句。
  - 至少 9 列已經對不上。Linux 專用的隔離測試在 Windows 收集到 0 個，自動核對數量在本機和 CI 一定有一邊對不上。
  - 所以拿掉「現在有幾個案例」的寫法，CI 的結果才是正本。
- **保留的數字**：這些不是「現在有幾個」，內容不會因為之後改測試而變錯，所以保留：
  - 某一批加了幾個的歷史紀錄（例如「C2-2 +3」「ADR-0263 加 8 個」）；
  - HTTP 狀態碼、檔案操作數；
  - 有日期的實跑結果（passed 數）；
  - 人工 live 評測的預算（案例數與請求上限，禁令 61 的同意依據）。
- **列表核對**：`tests/lint/test_tests_readme_listing.py` 要求版控中的每個 `tests/**/test_*.py` 都在 README 用反引號列出：
  - 有相對路徑就比對相對路徑；
  - 只寫檔名時，檔名在 `tests/` 裡必須唯一；
  - 沒加反引號的不算。
- **舊名不能留反引號**：反引號裡的 `test_*.py` 都要對得到實際檔案（萬用字元寫法除外）。改名或刪掉的舊名要拿掉反引號，免得之後出現的同名新檔被當成已列。
- **凍結清單**：當時沒列的 209 個凍結在 `tests/lint/tests_readme_unlisted.json`，清單只准變短。已補列或已刪除的檔案還留在清單裡，也算失敗。
- **其他**：
  - 同一支測試也擋 README 再寫「現在有幾個案例」，但只擋固定格式：數字欄、粗體或標籤裡的數量、「N 個測試／TDD／案例」、合計句；
  - 用 git 取得檔案清單，沒有 git 就失敗、不跳過；
  - AGENTS.md「任務後文件更新協議」改成「新增測試檔 → tests/README 加一列（檔名、說明）」。

### 6. 註解與內部用語清理（2026-10-22 增補）
- **ERA**：產品程式的 17 處全是誤判，沒有任何被註解掉的舊程式，所以改寫而不刪除：
  - 16 處是中文註解後面接半形括號或冒號，例如 `# 初始化路徑 (SSOT)`，Python 會把它讀成函式呼叫或型別標註，改成全形括號、全形冒號；
  - 1 處是用字典字面值寫的結構說明，改寫成一般句子。
  - 基準的 ERA001 從 17 降到 0。
- **內部角色稱呼與決定歸屬**：程式與測試裡的 CTO、PM、技術主管、A 線、C 線，以及「人類 10/12」「擁有者定案」「使用者拍板」「owner decision」這類歸屬，拿掉或改寫成它實際指的東西，例如記憶系統、隔離層、個人空間目錄、系統快照與記憶庫升級備份。
  - `permission_repair_exempt` 的分類 `a_line_swap_semantics` 改名為 `swap_semantics`。這個字串只存在函式屬性上，沒有寫進持久資料、日誌或 API。
- **決定代號**：一律寫成「ADR-NNNN 決定 N」，拿掉甲乙丙。
  - ADR 裡沒有那個標籤的，只寫 ADR 編號：r55 的審查員設定寫 ADR-0271，第三方 MCP 未隔離啟動寫 ADR-0275。
  - ADR-0246 有兩套編號：「決定 N」與圈號 ①～⑨。兩者是不同的事，圈號寫成「ADR-0246 ③」，不改成「決定 3」。ADR-0246 沒有「決定 5」，原本寫「決定 5」的只寫 ADR-0246。
  - ADR 內部的「決策 N」編號（例如 ADR-0146 決策 2）保留。
  - openapi.json 與 api.ts 跟著重新產生，只有說明文字改變。
- **保留不改的**：
  - 查證來源與事故的日期，例如價目表的查證日期；
  - 子代理提示裡的「主代理」，這是產品用語；
  - 單元代號（U2c-3、r55 等），開源審查報告建議保留，它們對得到 ADR 的小節；
  - 審查流程的稱呼（DEV-A、reviewer ①），以及 `scripts/guard_harness_context.py` 錯誤訊息裡的 owner decision，改了會改變輸出。
- **順手拿掉的豁免**：頂層 `tests/` 不再算業務層之後，用不到的 3 個 `# noqa: IO_PRISON_VIOLATION`（NOQA_REGISTRY 16 → 13）。

### 7. Python 型別檢查只減不增（pyrefly，2026-10-23 增補）
- **工具**：pyrefly，開發依賴釘 `==1.4.0`（MIT，各平台有預先編好的 wheel）。經 uv 鎖檔，有雜湊與 7 天冷卻期；不進 Docker 映像。
- **範圍先從隔離與安全邊界開始**：`sandbox_kernel/`、`services/isolation/`、`authorization_gateway.py`、`execution_broker.py`。錯誤數少，只減不增才有意義；之後再逐步擴大。範圍寫在 pyproject.toml 的 `[tool.pyrefly]` `project-includes`。
- **設定**：
  - `python-platform = "linux"`、`python-version = "3.12"`，Windows 本機和 CI 才會算出同一組錯誤。10/23 實測同一範圍用 win32 平台是 65 個，用 linux 是 34 個。
  - 不在設定裡寫直譯器路徑。收集指令用 `--python-interpreter-path` 傳入目前的直譯器（`sys.executable`）。
  - `search-path` 是 `ToukaProxy` 與 `ToukaMemory/src`。原本的 `./ToukaMemory` 對不到 `memory_server`，還可能和 ToukaProxy 的 `src` 撞名；目前範圍內兩種寫法算出的錯誤相同，沒有匯入錯誤。
  - `use-ignore-files = false`：pyrefly 預設會跳過 `.gitignore`、`.ignore`、`.git/info/exclude` 列的檔案，寫一行就能讓範圍內的檔案不被檢查，而且 `.git/info/exclude` 不進版控，本機和 CI 可能不同。
- **收集**：`scripts/lint_ratchet.py pyrefly` 跑 `python -m pyrefly check --config pyproject.toml --output-format json`，依錯誤種類（例如 `bad-argument-type`）計數。ruff 與 pyrefly 共用同一個子程序入口與比對、重拍函式。
- **跑完的判定**：pyrefly 有型別錯誤和設定壞掉時，結束碼都是 1。所以只有結束碼是 0 或 1、輸出是完整的 JSON、而且錯誤輸出每一行都是 INFO 時才算跑完，其他一律當失敗，不當成 0 個錯誤。設定欄位打錯、`search-path` 不存在時，pyrefly 都只印 WARN 就照跑，範圍會悄悄改變。用「只准 INFO」而不是「擋 WARN」，升級後日誌格式變了也會失敗。
  - 本機設了 `PYTHONPATH`（即使是空值）時 pyrefly 會印 WARN，收集一定失敗；跑之前先取消 `PYTHONPATH`。CI 沒有設。
- **基準**：`tests/lint/pyrefly_baseline.json`，10/23 是 34 個：`bad-argument-type` 19、`missing-attribute` 14、`bad-assignment` 1。規則同 §1，完全相等，變少要在同一個 PR 重拍。
- **升級**：升級 pyrefly，或升級會影響型別推論的第三方套件時，數字可能跟著變。在同一個 PR 重拍基準，並在 PR 說明變化。
- `tests/lint/test_pyrefly_ratchet.py` 另外釘住：
  - **整份設定**：範圍、`search-path`、平台、版本（和 `.python-version` 一致）、不讀 ignore 檔逐項相等，不准多出排除、錯誤開關或直譯器路徑；收集指令的參數也整組釘住，不准多加 `--ignore` 之類的參數。縮小範圍、排除檔案、關掉錯誤種類都會讓數字悄悄變好看，pyrefly 本身不會警告，所以要調整就得改測試，改動會出現在審查的差異裡。
  - `project-includes` 的每個樣式都還對得到檔案；範圍內的檔案不准用 `# pyrefly: ignore-errors` 整檔壓制。
  - 用真的 pyrefly 檢查暫存資料夾裡的小專案，核對輸出格式，並確認欄位打錯、`search-path` 不存在、設定讀不懂都會讓收集失敗。升級 pyrefly 後格式變了，這裡會先失敗。

## 已知限制

- **每條規則只算總數**：在 A 檔修一個、在 B 檔加一個，數字不變，擋不到。這是刻意的取捨，和 Guard #35 相同，搬移程式才不會誤擋。
- **壓掉不修，不會被計數**：用 `# noqa: E501` 這類標準規則代號或 per-file-ignores 壓掉的違規不算數。Guard #19 只核對 NOQA_REGISTRY 統計表有列的規則。
- **兩個 PR 同時重拍基準**：兩個 PR 各自把同一條規則從 45 修到 44 並重拍時，兩邊的基準改動一模一樣，git 會乾淨地合成 44，但實際已經是 43，合併後 dev 會轉紅。合併規則只核對 PR 自己的 CI。所以合併前如果 dev 已經進了另一個重拍基準的 PR，要先把 dev 合進來重跑 CI。
- **說明文字裡的數量只靠審查**：自由文字不可能用規則擋乾淨，規則只擋固定格式。沒有單位的寫法不擋 100～599 與 1900～2099 的數字（避開 HTTP 狀態碼與年份），所以「標籤（163）：」擋不到；帶 cases／collected／測／案 單位的不受這個限制。粗體只在成對的範圍裡比對。其他寫法靠審查。
- **凍結清單只靠審查擋住變長**：把新的未列檔加進 `tests_readme_unlisted.json` 測試也會過，要靠審查時看差異擋下。另外，出現同名檔時，錯誤訊息會把原本已列的那一個也列為要「加一列」，要看路徑判斷。
- **自訂豁免標記的警告**：Touka 的 `# noqa: ENV_SSOT_VIOLATION` 這類標記會讓 ruff 印出約 110 個「無效的 noqa」警告。ruff 只認「字母＋數字」格式的代號，10/22 實測 ruff 0.15.20 的 `lint.external` 消不掉。改寫成別的標記要動 166 行、Guard #19 與規則文字，這次不做；棘輪只讀 JSON 結果，不受影響。
- **型別檢查的範圍很小**：只涵蓋 §7 列的隔離與安全邊界，其他產品程式沒有型別檢查。
- **型別檢查看不到動態產生的成員**：`ErrorCode` 是執行時從 errors.json 產生的列舉，型別檢查看不到它的成員。基準 34 個裡有 7 個 `missing-attribute` 是這個原因，不是程式錯誤。
- **型別檢查的壓制不被計數**：單行的 `# type: ignore` 與 `# pyrefly: ignore` 壓掉的錯誤不算數（10/23 範圍內有 1 個），和 ruff 的 `# noqa` 一樣，靠審查擋。整檔的 `# pyrefly: ignore-errors` 由測試擋。
- **warn 等級的診斷不計數**：只算 error 等級。10/23 範圍內另有 3 個 warn 等級的診斷（`unreachable`、`deprecated`、`unreachable-except-clause`），不在基準裡。
- **兩個平台的套件不同**：平台固定只統一型別存根。Windows 的 venv 多了 `pywin32`、`colorama`、`tzdata` 等套件。目前範圍只匯入標準函式庫、`src` 和 `fastapi`，影響小；擴大範圍時要再確認本機和 CI 的數字一致。
