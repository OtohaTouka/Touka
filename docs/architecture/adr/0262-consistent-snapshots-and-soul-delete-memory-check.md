# ADR-0262：系統快照一致且不互蓋、快照路徑單一來源；刪除靈魂前要先清記憶

- 狀態：✅ 已接受（維護者 2026-10-15 核准記憶系統第二批計畫，刪除靈魂時還有記憶就要求先重置；記憶系統第二批段 6；[ADR-0272](0272-iso-c2-agent-command-isolation-and-executor.md) 修訂：`child_python_env()` 回傳完整環境〔含金鑰〕，隔離邊界〔`sandbox_kernel/**`、`services/isolation/**`〕不得使用它、`get_full_env` 或 `get_clean_env`）
- 日期：2026-10-15
- 決策者：維護者
- 影響範圍：ToukaProxy（`scripts/manage_backup.py`、`services/system_backup.py`、`services/backup_scheduler_service.py`、`services/dispatch_supervisor.py`、`routers/system.py`、`routers/souls.py`、新 `services/agent_reset/delete_guard.py`、新 `utils/child_env.py`、`config/errors.json.example`）＋ ToukaMemory（`factual.py`、`vector.py`）＋ ToukaChat（`AgentDataResetCard.tsx`、`lib/error_handler.ts`、i18n、`lib/error_codes.ts` 產生檔）
- 相關：ADR-0030（sqlite3 匯入白名單）、ADR-0052（多 `.env` 根目錄偵測）、ADR-0082（dispatch 子程序）、ADR-0104（重置前先備份）、ADR-0109（自動備份排程：檔名改到秒，前綴與保留策略不變）、ADR-0113（每靈魂重置）、ADR-0142（刪除靈魂步升）、ADR-0151（容器裡 `.env` 在 `/app/host_root`）、ADR-0254（還原狀態端點）、ADR-0256（事實庫是正本、升級備份），決策都不變

## 背景

系統快照（`manage_backup.py` 打包的 zip）與重置前的「先備份」一直沒有真的運作：

1. **腳本路徑錯**：`system_backup._backup_script()` 與 `routers/system._get_scripts_dir()` 指向 `<repo>/scripts/manage_backup.py`，腳本實際在 `ToukaProxy/scripts/`。手動快照、排程快照、設定匯入前與重置前的快照全部以「Backup script missing」失敗；每靈魂重置與 DebugAgent 重置都要先備份，所以一律中止、什麼都沒刪。
2. **容器裡做不出來**：映像沒有 `.env`（`.dockerignore`），容器裡的 `.env` 掛在 `/app/host_root/.env`；腳本從自己的位置往上找含 `SANCTUARY_DATA_PATH` 的 `.env` 找不到。就算找到，`SANCTUARY_DATA_PATH` 是給 compose 用的宿主路徑，容器裡的資料在 `/app/data`；快照寫到的 `/app/backups` 也沒有掛載，容器重建就消失。
3. **資料路徑對不上時會產生空快照**：腳本在資料根不存在時靜默跳過整個資料區，快照照樣「成功」。重置前的快照若是這種空快照，接著真的刪資料，就沒有還原點。本機 `SANCTUARY_DATA_PATH` 與 Proxy 實際用的 `MEMORY_APP_DATA_DIR` 不一致時也會這樣。
4. **同名互蓋**：檔名只到分鐘、用 `"w"` 開檔，同一分鐘做兩份會蓋掉前一份。
5. **複製到寫一半的資料庫**：快照直接讀執行中的 `core_memory.db` 與 Qdrant 各 collection 的 `storage.sqlite`（連 `-journal` 一起），可能夾到未提交的頁面；Qdrant 的 `meta.json` 與 collection 資料夾也可能對不上。
6. **快照失敗回應帶腳本 stderr**：`SYSTEM_BACKUP_FAILED` 的 `error_output` 是腳本 stderr 原文，經 API 回給前端，可能含絕對路徑與像金鑰的字串（禁令 23）。
7. **刪除靈魂不清記憶**：`DELETE /v1/souls/{id}` 只清人格、設定與註冊；刪完後重置會拒絕（重置要求靈魂存在），記憶變成孤兒，同名新靈魂會接手舊記憶。

## 決策

1. **檔名不互蓋、原子發布**：檔名 `{prefix}_YYYYmmdd_HHMMSS.zip`，同一秒撞名加 `_2`、`_3`…。先寫到同資料夾的 `.{stem}.*.partial`（不符 `*.zip`，清單、下載、排程都看不到）並 fsync，再以 `O_EXCL` 佔一個沒人用過的檔名、用暫存檔取代自己剛佔的空檔。絕不覆蓋既有快照；失敗時刪暫存檔。
2. **SQLite 一致複本**：`core_memory.db` 與 Qdrant 各 collection 的 `storage.sqlite` 用 SQLite backup API 取複本（來源開唯讀連線、絕不改動；有寫入交易時等它結束，拿到的是某個提交點），複本 `integrity_check` 不是 `ok` 就失敗。`core_memory.db` 旁的 `-journal`／`-wal`／`-shm` 不收。helper 是 `FactualStore.copy_sqlite_snapshot`，放在 factual.py 是因為 sqlite3 只能在禁令 11 的白名單檔匯入；不加 noqa、不改白名單，也不經 `get_resource_proxy()`（快照子程序不載入 Proxy）。
3. **Qdrant 以 `meta.json` 為準**：只收 `meta.json` 列出的 collection；複製前後讀到的 `meta.json` 要一字不差，不一樣就重來（最多 5 次），一直在變就讓快照失敗。`.lock` 與沒列在 meta 的殘留資料夾不收。列了但沒有 `storage.sqlite` 的 collection 照實不收它的 storage，還原後 QdrantLocal 自動建一份空的、打得開（QdrantLocal 建 collection 時就會建出空的 `storage.sqlite`，這是例外狀況）。
4. **路徑單一來源**：腳本 `system_backup.backup_script()`（`ToukaProxy/scripts/manage_backup.py`）；快照目錄 `system_backup.backup_dir()`；`routers/system._get_backup_dir` 與排程的 `backups_dir` 是它的別名，快照、清單、下載、刪除、還原、排程清理共用。
5. **Proxy 把路徑交給腳本，不讓它猜**：快照與還原都傳同一組 `--data-root`（`resource_manager.get_base_path()`）、`--env-file`（`resolve_env_file_path()`）、`--config-dir`、`--output-dir`（`system_backup.script_path_args()`）。四個參數要一起給；腳本收到參數就不往上找 `.env`，資料根不存在就失敗——不會產出不含資料的快照，重置因此中止、什麼都不刪。還原把 `.env` 寫回參數給的位置、資料寫進參數給的資料根。手動執行腳本（不給參數）維持舊的猜路徑行為。
6. **快照目錄＝`.env` 所在資料夾的 `backups`（不動 compose）**：容器裡是 `/app/host_root/backups`，也就是宿主 compose 目錄的 `./backups`，容器重建不消失；本機整合模式是 repo 根的 `backups`，和以前相同。
7. **子程序 PYTHONPATH 一個來源**：新 `src/utils/child_env.child_python_env()` 回傳 `settings.get_full_env()` 的複本，PYTHONPATH 依序是 PROXY_ROOT、`ToukaMemory/src`、原本的值（去重保序）。dispatch 子程序、快照、還原三處共用；dispatch 原本那段寫法換成呼叫，行為不變。理由：子程序已經是同一個直譯器（`sys.executable`），但 memory_server 不是安裝的套件（`ToukaMemory/pyproject.toml` 的 `package = false`），只能靠 PYTHONPATH 找到，改用 `-m` 也解決不了；先例是 `DispatchSupervisor.build_child_env` 原本的同一段寫法，Docker 映像的 `ENV PYTHONPATH` 與 `ToukaProxy/run_proxy.ps1` 本來也這樣設。
8. **快照失敗不回腳本輸出**：`SYSTEM_BACKUP_FAILED` 只帶錯誤碼；stderr 先經 `utils/secrets.redact_secrets_from_content` 消毒，再只寫伺服器 log（禁令 23）。還原失敗本來就只記狀態與錯誤碼（ADR-0254）。
9. **刪除靈魂前檢查記憶**：步升、不可刪名單、路徑監獄之後，事實庫該 ID 任何狀態的列（含已封存、待補索引）、向量點、或向量 collection 還在（空的也算）就回 409 `SOUL_MEMORY_NOT_RESET`（1408，附 `facts`、`vectors`、`vector_collection`），什麼都不刪；記憶系統不可用、無法確認時回 503 `SOUL_MEMORY_CHECK_FAILED`（1409），不刪。用 `MemoryManager.count_agent_memory` 與新的 `VectorStore.has_agent_collection`，Guard #45 基準不變。
10. **畫面講清楚**：刪除被擋時顯示「這個靈魂還有記憶，請先到『代理人資料重置』重置它，再刪除」；重置因快照失敗中止時，重置卡顯示「系統快照沒有做成，所以這次重置沒有執行，什麼都沒刪。你的操作沒有錯」（前端依錯誤碼 `SYSTEM_BACKUP_FAILED` 選文字，後端錯誤碼不變）。

## 後果

- 重置前的「先備份」第一次真的會做出含資料的快照；做不出來就不刪。以前在本機與容器裡都沒成功過，所以沒有舊快照要搬。
- 快照位置綁在 `.env` 位置上：本機獨立模式（工作目錄在 `ToukaProxy` 底下、沒設 `TOUKA_INTEGRATED`）會變成 `ToukaProxy/backups`（`.gitignore` 的 `backups/` 不限層級，已忽略）。在容器裡另掛 `/app/backups` volume 的做法留作之後的候選（要動兩份 compose 與 GHCR 的 `permission_init.py`）。
- 腳本現在會載入 `memory_server.factual`：手動執行時 PYTHONPATH 要含 `ToukaMemory/src`，沒有時一開始就失敗、不會產出半份快照。
- 快照中途當機會在 `backups` 留下 `.*.partial` 或 `.snapshot-*` 暫存（不符 `*.zip`，畫面看不到），要手動刪。
- 刪除前檢查對所有 ID 生效：本變更前就被刪掉、只剩孤兒記憶的 ID，要「重建同名靈魂 → 重置 → 刪除」才能清（重置要求靈魂存在）。
- Qdrant collection 名把非英數字元換成 `_`，只差符號的兩個 ID 共用一個 collection：其中一個還有向量時另一個刪不掉，重置會一起清掉（既有問題，另案處理碰撞檢查）。
- 刪除前還沒檢查活動時間軸（事件表）；事件表做好後再補。〔ADR-0267 已補：記憶檢查之後再查活動事件，還有就同樣 409 `SOUL_MEMORY_NOT_RESET`（detail `activity_events`），讀不到 503。〕
- `ui_sessions.db`、`feedback.db`、`legacy_history.db` 等其他 SQLite 仍直接複製，不在本次範圍。
- 還原子程序沒有攔截輸出，它的 stderr 直接流進伺服器自己的 stderr、沒有消毒；不經過任何 API 回應。
- 測試：`tests/unit/test_snapshot_consistency.py`、`test_backup_paths_ssot.py`、`test_backup_error_output_security.py`、`test_soul_delete_memory_guard.py`；前端 `AgentDataResetCard.test.tsx`、`SoulStudio.crud.test.tsx`。

---


*Last Updated: 2026-10-15*
