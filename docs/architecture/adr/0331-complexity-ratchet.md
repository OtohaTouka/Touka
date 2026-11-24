# ADR-0331：函式複雜度只減不增（上限 15）

- 狀態：✅ 已實作
- 日期：2026-11-24
- 決策者：維護者（總覽板第 102 項選甲、第 104 項核准一頁計畫）
- 相關：[ADR-0278](0278-lint-ratchets-in-ci.md)（`lint_ratchet.py`、基準一起提交、在 CI 的 pytest 裡跑）、[ADR-0298](0298-rule-text-matches-checks.md)（vulture 單向清單；撤回時照它的做法標狀態）。
- 影響範圍：`scripts/lint_ratchet.py`（`complexity`）、`tests/lint/complexity_baseline.json`、`tests/lint/test_complexity_ratchet.py`、`tests/lint/test_ci_pr_checks.py`、`CONTRIBUTING.md`「PR 前必做」第 2 項、`docs/handover/tests.md`。產品程式不動，守衛數量不變。

## 背景

2026-11-24 的外部審查指出幾個核心函式越寫越複雜。實測（ruff 0.15.20 的 C901，五個產品資料夾）：

- 從 main（11/10）到 dev：處理工具呼叫的 `execute_to_queue` 53→58，列工具的 `ToolLayer.process` 46→51，超過 15 的函式 35→36 個。
- 現在超過 15 的有 36 個（ToukaProxy 32、ToukaMemory 3、ToukaDispatch 1），最高是 `run_react_loop` 70；超過 20 的 16 個、25 的 9 個、50 的 3 個。剛好 15 的 13 個、14 的 11 個。
- 融合段 2、3 正要往 `execute_to_queue`、`ToolLayer.process` 裡加東西。專案沒有任何東西擋複雜度變高。

## 決定

1. **上限 15**：`lint_ratchet.py complexity` 用 ruff C901 算每個函式的複雜度。超過 15 的記進 `tests/lint/complexity_baseline.json`，鍵是 `相對路徑::完整名稱`，值是複雜度。範圍和其他棘輪相同，是 `PRODUCT_SOURCE_ROOTS` 五個資料夾；不管測試、`scripts/` 與前端。
2. **兩個方向都紅，訊息分開**：
   - 「新超標，要拆」「變複雜，要拆」：拆到模組或方法層再送，不要直接重拍。
   - 「變簡單，請跑 --update」「不見了（改名、搬檔或拆掉），請跑 --update」：在同一個 PR 跑 `python scripts/lint_ratchet.py complexity --update`，改善才留在差異裡，之後不會被悄悄用掉（同 ADR-0278 §1）。
   - 不放進 `ONE_WAY_TOOLS`：數字只跟著函式自己的分支變，沒有 vulture 那種「別處改了、這裡消失」的雜訊。
3. **審查規則**：`--update` 會照單全收，所以基準檔的差異要人看。只接受兩種：數字變小或消失；改名或搬檔、數字沒變大，而且在 PR 說明逐項列出。其他新增或變大的一律退回去拆。
4. **壓不掉、藏不起來**：收集固定用 `--isolated --ignore-noqa --no-respect-gitignore`，再清掉預設排除、加掃 `.pyw`（`COMPLEXITY_ARGS` 整組由測試釘住）。`--isolated` 不讀 `ruff.toml`，per-file-ignores 關不掉；`--ignore-noqa` 讓 def 行的 `# noqa: C901`、檔頭的 `# ruff: noqa` 都壓不掉；後三個讓列進 `.gitignore`、放在 `dist/`、`venv/` 這類預設排除的資料夾、存成 `.pyw` 的檔案照樣被掃（DEV-B 實測這幾種原本都會漏掉；本機產品資料夾裡沒追蹤的雜檔也會被掃，只會誤紅、不會漏）。參數裡不能有 `--ignore`、`--exclude`、`--extend-exclude`、`--per-file-ignores`。
5. **看不懂就丟錯，不當成 0 筆**：
   - 收集前逐一確認五個資料夾都在：ruff 碰到不存在的資料夾只在 stderr 印一行，結束碼不變。
   - stderr 有任何輸出、結束碼不是 0 或 1、輸出不是 JSON、代號不是 C901（含 `invalid-syntax`）、訊息格式認不得、訊息裡的上限不是 15、結束碼和結果數量矛盾，都丟錯。資料夾預檢和 stderr 檢查是故意重疊的兩層。
   - 「基準不是空的，結果卻一筆都沒有」：檢查和 `--update` 兩條路都丟錯，收集壞掉時才不會被一次 `--update` 寫成空基準、等於關掉檢查。真的全部拆完時，手動把基準改成 `{}`，並在 PR 說明。
6. **完整名稱**：ruff 只報短名稱與行號。用標準函式庫 `ast` 找出那個 def 的完整名稱（例如 `ToolLayer.process`、`outer.<locals>.inner`、`factory.<locals>.Inner.method`，和 Python 的 `__qualname__` 一致）：名稱相同，而且 def 的起始行 ≤ 回報行 ≤ 本體第一行（多行裝飾器、`async \` 換行都對得到）。必須恰好對到一個，否則丟錯，不退回短名稱、也不略過。同檔同完整名稱（property 的 getter／setter、`if`／`else` 兩邊各定義一次）依在檔內的順序加 `#2`、`#3`，不管有沒有超標，兩個互換時才會紅；現在 0 組。副作用：在凍結的函式上方加 `@overload` 存根或其他同名定義，它的鍵會變成 `#2`，要當成改名重拍。路徑先解析符號連結再比對。
7. **在 CI 的 pytest 裡跑**：`tests/lint/test_complexity_ratchet.py`。不改 CI 設定、不加套件。CI 完整測試要 36～40 分鐘才會紅，本機跑這道不到一秒，`CONTRIBUTING.md` 與測試交班寫「推送前先跑」。

## 後果

- 剛好 15 的 13 個、14 的 11 個函式，再加一兩個分支就會紅，要先拆。改到凍結函式的 PR：變簡單要多跑一行 `--update`，變複雜要先拆。
- **巢狀會算進外層**：巢狀函式與巢狀類別裡的方法，分支都算進外層，外層等於內層的全部分支再加 1（36 筆裡有 9 個含巢狀函式，`run_react_loop` 有 6 個）。拆成巢狀 closure 外層反而多 1，要拆到模組或方法層。第 3 步拆 `execute_to_queue`（継燈）也照這條。
- 兩個 PR 同時重拍同一個檔的基準會衝突，各自綠、合起來紅時 dev 會紅；合併前先把 dev 合進來重跑（同 ADR-0278）。
- ruff 升級時算法或訊息格式可能變。格式認不得會直接丟錯；數字變了會在升級那個 PR 紅，要在那個 PR 重拍並寫明。

## 碰不到的地方

- 只看函式，不看模組層級的程式碼與類別本體。
- 前端（TypeScript）沒有對應的檢查，要的話另列候選。
- 改名或搬檔時會變成「舊的不見、新的超標」，只能靠審查確認數字沒變大。
- 只算分支數，不代表函式長度或可讀性；長而沒有分支的函式不會紅。條件運算式（`a if c else b`）與 `and`／`or` 不算分支：把 if 鏈改寫成這些寫法數字會下降，函式卻沒變簡單，審查要留意。

## 撤回

還原 `lint_ratchet.py` 的 `complexity`、基準檔、兩支測試與文字，本 ADR 改標「已撤回」（編號不能刪，照 ADR-0298 的做法）。
