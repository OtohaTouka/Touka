# ADR-0255：記憶系統第一批段 5：個人空間目錄（`.touka/catalog.json`，只當快取）

- 狀態：✅ 已接受（維護者 2026-10-15 核准記憶系統第一批計畫；本 ADR 是段 5；[ADR-0272](0272-iso-c2-agent-command-isolation-and-executor.md) 補註：`.touka/` 在權限自癒白名單內，只補擁有者位元）
- 日期：2026-10-15
- 決策者：維護者
- 影響範圍：ToukaMemory（新 `services/personal_catalog.py`、`protocols.py` 的 `IResourceProxy`、`tools/__init__.py` 的 `vault_tools`、`tools/core_file_ops.py` 的 `core_list`／`core_find`／`core_grep`）；ToukaProxy（`services/resource_manager.py`、vault 橋接層 `tools/vault_tools.py`、重置原語 `services/agent_reset/primitives.py`）；Guard #45 基準（`scripts/native_tool_contract_baseline.json`）
- 延伸：ADR-0119（9 個檔案裸動詞＋scope）。相關：ADR-0078（原子寫入、失效可觀察）、ADR-0116（Memory 只經資源代理做 I/O）、ADR-0214（測試沙盒）、ADR-0228（Guard #45）。這些 ADR 的決策都不變。

## 背景

- 靈魂的個人空間沒有索引；檔案一多，只能靠 `list`／`find` 一層層找。
- 寫進個人空間的路徑很多，只有檔案動詞經過 Memory 的 vault 分發器。終端機（工作目錄就是個人空間）、只能碰個人空間的 MCP、聊天上傳、生圖、任務板、派工結果、編輯器都直接寫檔。
- 所以任何「寫入時順手更新」的索引一定會漏，必須在讀的時候和實際檔案比對。

## 決策

### 1. 位置與內容

- 每個個人空間一個 `.touka/catalog.json`。
- 只放能 100% 由檔案系統重建的 metadata：相對 POSIX 路徑、依副檔名的類型、大小、`mtime_ns`、不超過讀取上限的檔的內容 SHA-256、Markdown 第一個 ATX 標題（有字數上限）。沒有任何 LLM 摘要。
- 讀寫都經 Pydantic 模型 `PersonalCatalog`、`CatalogEntry`（`extra="ignore"`）。路徑驗證器只收個人空間內的相對 POSIX 路徑：絕對路徑、`..`、反斜線、冒號、空字串都拒絕。

### 2. 目錄只當快取，正本是檔案系統

- **只信任系統自己寫出的內容**：記住本行程最後一次寫出的目錄文字的 SHA-256。目錄檔被其他方式改過（例如用終端機寫一份假目錄）、壞掉、不存在，或行程剛啟動時，整份不信任、全部重算。
- **信任時也照實際檔案修正**：實際不存在的項目丟掉；大小或 `mtime_ns` 對不上的重算 hash 與標題；不認得的欄位忽略。
- 內容有變才寫回；寫回走 `ResourceManager.write_file_text`（temp＋fsync＋replace，ADR-0078）。
- **更新時機**（都在 Memory vault 分發器的 personal 分支，`personal_catalog.run_vault_action`）：
  - `write`／`append`／`str_replace`／`delete` 成功後重建；失敗只記 log，不影響寫入結果（下次讀時會再比對）。
  - 個人空間根目錄的 `list` 成功後重建，並在輸出最後附一行提示（`CATALOG_HINT`：目錄可用：N 個檔，read .touka/catalog.json）。子目錄的 `list` 不附。
  - `read .touka/catalog.json` 時持鎖先重建再讀；重建失敗就回錯誤，不給沒核對過的內容。
- 模型怎麼讀到目錄：用既有的 `read`，不加新工具、新 action 或新公開欄位。

### 3. 拒寫與隱藏

- personal 的寫入類動作（`write`／`append`／`str_replace`／`delete`），只要解析後的路徑有一段是 `.touka`（不分大小寫；含 `..`、絕對路徑、symlink 解析後落入）就拋 `CatalogPathDeniedError`（`PermissionError` 子類，訊息固定、不含實體路徑），不寫入。`.touka/` 還沒建立時也一樣擋。
- Memory 不能 import Proxy 的 `build_tool_error`（禁令 14、Guard #21）。照既有慣例（Memory 拋領域例外、Proxy 入口組結構化錯誤），Proxy vault 橋接層把 `CatalogPathDeniedError` 對應成 `build_tool_error(permission_denied)`，附「讀 `.touka/catalog.json` 查看」的建議；這是預期中的拒絕，只記一行 warning、不記 stack trace。
- `list`／`find`／`grep` 由系統強制帶 `exclude={".touka"}`（`CATALOG_HIDDEN_NAMES`）。Memory `vault_tools` 先丟掉呼叫端自帶的 `exclude`，模型關不掉隱藏。
- 不用 `is_protected_path`：它擋在 `write_file_text` 本身，系統自己也會寫不進去。

### 4. 並行

- 每個 event loop、每個個人空間一把 `asyncio.Lock`；同回合多個工具並行時依序重建。
- 掃描與寫檔放 `asyncio.to_thread`，不卡事件迴圈。
- 只有 Proxy 行程寫目錄，所以不加跨行程檔案鎖；原子置換保證讀的人不會看到寫一半的檔。

### 5. 排除與上限

- 常數都在 `personal_catalog.py`（禁令 18），本文不抄值：
  - `CATALOG_MAX_ENTRIES`：最多收幾個檔，超過標 `truncated`。
  - `CATALOG_READ_MAX_BYTES`：hash 與標題只讀這個大小以內的檔，超過的 `sha256`、`title` 是 `null`。
  - `CATALOG_TITLE_MAX_CHARS`：標題字數上限。
  - `CATALOG_SKIP_DIR_NAMES`：任何深度都略過的目錄名（`.touka`、`.git` 等）。
  - `CATALOG_SKIP_ROOT_DIRS`：只在根目錄略過的目錄（任務板 `missions`、派工結果 `dispatch_results`）。
  - `CATALOG_SKIP_FILE_GLOBS`：略過的檔名樣式（寫檔暫存、鎖檔）。
- 不跟隨 symlink 與 junction：只收 `lstat` 是一般檔案的項目。
- 聊天上傳（`outputs/uploads/`）照列，類型依副檔名。

### 6. 資源代理新增的方法

- 所有 I/O 經 `IResourceProxy`（禁令 6、14）。協定與 `ResourceManager` 補：
  - `read_file_binary(path)`：讀二進位內容（算 hash 用）。
  - `stat_files(path, exclude_names, exclude_paths, exclude_globs, max_files)`：遞迴列一般檔案的（相對 POSIX 路徑, 位元組數, `mtime_ns`），同層依名稱排序，不跟隨連結，路徑不存在時回空清單。
- `list_directory`／`glob_search` 加選填 `exclude`，預設不額外隱藏，既有呼叫者（技能掃描、舊事實檔偵測）行為不變。`core_list`／`core_find`／`core_grep` 收同樣的選填 `exclude`。
- `iter_files` 不動（GDPR 匯出在用）。

### 7. Guard #45

- `personal_catalog.py` 加進基準檔的 consumers：personal 路徑經它轉發 `**kwargs`，不納入就會成為守衛的盲點。基準檔只改段 5 造成的形狀變化。

## 對 GDPR 匯出、系統備份、重置的影響

GDPR 匯出與系統備份不改程式；重置多一個清除項目。

- **GDPR 匯出**（`gdpr_export_service` 走 `iter_files`）：會把 `.touka/catalog.json` 一起放進 `agents/<id>/files/…`。內容只是同一包裡那些檔的 metadata（路徑、大小、hash、標題），不另含個資。
- **系統備份**（`manage_backup.py` 打包整個 `personal_drive`）：會帶上目錄檔。還原後行程重新啟動，目錄不在信任紀錄裡，第一次讀時整份重算。
- **重置**（`agent_reset` 清 outputs〔保留 `outputs/uploads`〕與 missions）：outputs 在個人空間裡，目錄會記下其中的檔名、hash 與標題，所以重置一併刪掉該靈魂的 `.touka/catalog.json`。
  - 清除項目 `catalog`（`StorePurger`，排在 outputs 之後）；路徑經 `resource_manager` 的個人空間解析，檔名常數取自 `personal_catalog`。
  - 檔不存在算 0、不算錯；remaining 依目錄檔是否還在算 0 或 1，重置報告照實反映。
  - 刪掉後不在信任紀錄裡，下次讀目錄時由實際檔案整份重建，不含被清掉的檔。目錄檔是快取，不列進 `RETAINED_STORES`。

## 考慮過但沒採用

- **目錄放在個人空間外**：模型讀不到，只能另開工具，或讓 `list` 加旗標。另開工具違反 ADR-0119 避免大工具的方向，還要過 Guard #32／#45 與風險表；`list` 加旗標要先把 `list` 遷成型別化合約。
- **只靠大小＋`mtime_ns` 判斷能否沿用**：終端機寫的假目錄只要大小與時間都抄對，就能留下假標題或假 hash。改成只信任自己寫出的內容，代價是行程重啟後每個個人空間第一次讀要重算一次 hash。
- **HMAC 簽章目錄檔**：要多一把金鑰與輪替，效果和「只信任本行程寫出的內容」相同。
- **寫入時只更新單一項目**：繞過動詞的寫入很多，讀時一定要全掃；兩條路徑反而容易不一致，所以寫入後也走同一個全掃重建。

## 已知限制

- 終端機、MCP、上傳等仍然可以改或刪 `.touka/`；靠「讀時以實際檔案為準」修正，不靠禁止。終端機的 `ls -a` 也看得到它，隱藏只在 personal 的檔案動詞。
- 同大小、同 `mtime_ns` 的內容修改（例如保留時間戳的複製）不會被發現，直到大小或時間改變、或行程重啟。
- 每次根目錄 `list`、讀目錄、成功寫入都會全掃一次（stat；只有變動的檔才讀內容）。個人空間很大時（接近上限）會慢一些；行程重啟後每個個人空間第一次讀要重算所有檔的 hash。
- 巢狀的 `.touka` 資料夾（不在根目錄）也被隱藏、也不能用檔案動詞寫。
- 目錄檔只在上面三個時機更新（重置時則整個刪掉）。在下一次更新之前，用終端機等其他方式刪掉的檔，名字與標題仍留在磁碟上的目錄檔裡；GDPR 匯出與備份會照當下內容帶出。
- 信任紀錄只在記憶體、以行程為單位。若將來有兩個行程服務同一個個人空間，彼此會判定對方寫的不可信而重算：結果仍正確，只是多花時間。

## 決策落點表

| 決策 | 檔案 | 位置 | 測試／守衛 |
|---|---|---|---|
| 1 位置與內容 | `ToukaMemory/src/memory_server/services/personal_catalog.py` | `CATALOG_DIR_NAME`、`CATALOG_FILE_NAME`、`PersonalCatalog`、`CatalogEntry._relative_inside`、`_describe` | `test_personal_catalog.py` |
| 2 只當快取 | 同上 | `_trusted_digests`、`_load_trusted`、`_rebuild`、`refresh`、`run_vault_action` | `test_personal_catalog.py` |
| 3 拒寫與隱藏（Memory） | 同上；`ToukaMemory/src/memory_server/tools/__init__.py` | `CatalogPathDeniedError`、`_resolve_inside`、`CATALOG_HIDDEN_NAMES`；`vault_tools`（丟掉 `exclude`、personal 交給 `run_vault_action`） | `test_personal_catalog.py` |
| 3 拒寫的結構化錯誤（Proxy） | `ToukaProxy/src/tools/vault_tools.py` | `vault_tools` 的 `except CatalogPathDeniedError` | `test_personal_catalog.py` |
| 4 並行 | `personal_catalog.py` | `_catalog_locks`、`_lock_for` | `test_personal_catalog.py` |
| 5 排除與上限 | `personal_catalog.py` | `CATALOG_MAX_ENTRIES`、`CATALOG_READ_MAX_BYTES`、`CATALOG_TITLE_MAX_CHARS`、`CATALOG_SKIP_*` | `test_personal_catalog.py` |
| 6 資源代理 | `ToukaMemory/src/memory_server/protocols.py`、`ToukaProxy/src/services/resource_manager.py`、`ToukaMemory/src/memory_server/tools/core_file_ops.py` | `IResourceProxy.read_file_binary`、`stat_files`、`list_directory`／`glob_search` 的 `exclude`；`core_list`／`core_find`／`core_grep` | `test_personal_catalog.py` |
| 重置刪目錄檔 | `ToukaProxy/src/services/agent_reset/primitives.py` | `purge_catalog`、`remaining_catalog`、`MEMORY_STORES` 的 `catalog` | `test_personal_catalog.py` |
| 7 Guard #45 | `scripts/native_tool_contract_baseline.json` | `personal_catalog.py` consumer、`vault_tools` 與 core ops 的參數 | Guard #45 |

## 驗證

本機 Windows、3.12 venv、fnm Node 24.21.0。自動測試都在 pytest 的 `tmp_path` 裡用真的 `ResourceManager` 跑，不碰 `data/`，不打付費 API。

- 新測試：`tests/unit/test_personal_catalog.py` 39 個（`pytest --collect-only` 實數）：重置刪目錄檔後讀時重建、自動建立、寫入／刪除更新、壞檔重建、繞過動詞的寫入讀時補上、`list`／`find`／`grep` 隱藏、檔案動詞拒寫（含 `..`、絕對路徑、大小寫、還沒建立時）、經 Proxy 橋接層回 `permission_denied`、假目錄讀時修正、路徑沙盒（越界項目、junction／symlink 不跟隨、不碰別人的個人空間）、並行寫入、排除與上限、資源代理協定。都先紅後綠。
- 突變：25 個（每個只改一處，pytest exit 1 才算紅）全部變紅，還原後回綠。
- 文件與計數檔：`guard_noqa_registry.py`、`lint_ssot.py` 通過；文件、Guard #45 與段 5 測試 121 passed；`guard_adr_integrity.py` 在 merge dev `8f64ce0e` 後通過（0001..0255 連續）。
- 全量（`a53eac6d`）：pytest 固定順序 9068 passed／7 skipped／6 failed（6 個是本機 Docker Desktop 的 `test_local_compose_bindings`，以 CI 為準）；guard_all 43／43 PASS。

## 回復

- 不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）；程式部分可以單獨 revert：`personal_catalog.py`、Memory `vault_tools` 的 personal 分支、Proxy 橋接層的對應、資源代理的新方法與 `exclude`、Guard #45 基準檔一起撤。
- 撤回後，各個人空間已建出的 `.touka/` 會留著，變成一般檔案（看得到、檔案動詞改得到），不影響其他功能。要清由使用者手動刪；開發代理不碰 `data/`。
