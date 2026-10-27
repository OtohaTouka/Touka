# ADR-0288：舊審查報告乙組：記憶讀寫排在一條專用的工作執行緒上

- 狀態：✅ 已實作（乙-3）。維護者 2026-10-25 核准修正計畫（「照建議：1～5 都選甲」）。
- 日期：2026-10-27
- 決策者：維護者
- 相關：ADR-0103（測試等待一律有逾時）、ADR-0116（整合模式）、ADR-0207（Proxy 單一行程）、ADR-0251（測試沙盒釋放步驟）、ADR-0256（記憶庫是正本）、ADR-0267（無痕不記活動事件）、ADR-0270（清除與升級備份鎖）、ADR-0286（乙-1、乙-2）、ADR-0287（丙-4 刪除靈魂）。決策都不變。
- 影響範圍：ToukaMemory 新 `utils/memory_worker.py`；`services/manager.py`、`vector.py`、`tools/__init__.py`、`tools/diag.py`、`tools/recall.py`、`tools/legacy_vault.py`；ToukaProxy `services/agent_reset/delete_guard.py`、`services/agent_reset/primitives.py`、`routers/souls.py`、`routers/stats.py`、`routers/setup.py`、`routers/observability.py`；`scripts/native_tool_contract_baseline.json`；tests 新 `test_memory_worker_thread.py`，以及 `conftest.py`、`test_session_sandbox_cleanup.py`、`smoke/manual/memory_eval_probe.py`、`memory_eval/test_probe_bounds.py`、`test_memory_collection_collision_security.py`、`test_soul_delete_sandbox_home_security.py`、`test_soul_session_reuse_security.py`；MEMORY_INTERFACE、tests/README、HANDOVER。

## 背景

10/15 的程式審查（Touka #89～#105）乙組第 3 題（HIGH），在 dev 上核對後仍有效的：

- 記憶工具（存、查、取代、封存、掃描、重建索引、診斷、隨機回想、舊對話查詢）是 async 函式，裡面直接同步讀寫 SQLite、Qdrant，並用嵌入模型編碼；第一次用還要載入模型，要好幾秒到幾十秒。這段期間 Proxy 的事件迴圈整個停住，其他對話的串流、心跳與所有 API 都在等。
- 刪除靈魂前的記憶檢查、重置的清記憶與重算、舊對話統計與手動同步端點，也都在主迴圈上同步讀。
- 記憶清除在自己的工作執行緒裡數向量點，和主迴圈上的記憶操作同時碰同一個 Qdrant client（本機模式），是既有的跨執行緒競態。
- `get_vector_store` 建單例沒有鎖：兩條執行緒同時第一次用，會建出兩個、載入兩次模型。
- 計畫審查第 1 輪另外指出：只是改用一般的執行緒池，存記憶的「查重」和「寫入」分屬兩個交易，同時兩筆一樣的內容會都存進去。

## 決定

1. **記憶工作者**：新 `memory_server/utils/memory_worker.py`，整個行程一條單執行緒的工作者（執行緒名稱 `touka-memory`）。所有記憶讀寫排成一列，一次只跑一個。
   - 主迴圈用 `await run_memory_job(fn, ...)`。
   - 不在事件迴圈上的執行緒（記憶清除的工作執行緒）用 `run_memory_job_blocking(fn, ...)`，同步等。
   - 在工作者裡呼叫兩者都直接執行：巢狀呼叫不會排到自己後面而永遠等不到。
   - 每個工作在呼叫端 contextvars 的複本裡跑（`contextvars.copy_context().run`）。無痕不記記憶事件（ADR-0267）照樣有效；工作裡改的值不會傳回呼叫端，也不會留給下一個工作。
   - 等待被取消時，還在排隊的工作不跑，已經開始的會跑完（執行緒無法中途打斷）；之後才失敗的，例外記到 log（沒有人會收到它）。
   - 持鎖做破壞性操作的呼叫端用 `await run_memory_job_to_end(fn, ...)`：等待被取消時，已經開始的工作做完才把取消往外丟，再被取消也繼續等；還沒開始的照樣不跑。現在只有重置的清記憶用它：重置鎖（`destructive_op`）不會在清到一半時就放掉，讓還原或另一個重置進來。
2. **三條規矩**（寫在模組說明）：
   1. 工作者裡的工作不得同步等主迴圈（例如 `run_coroutine_threadsafe(...).result()`）：主迴圈可能正在等這個工作。
   2. 主迴圈不得同步等工作者：在有事件迴圈的執行緒上呼叫 `run_memory_job_blocking` 會丟 `MemoryWorkerMisuseError`，什麼都不做。
   3. 持有工作者裡的工作可能要的鎖（例如升級備份鎖 `_UPGRADE_BACKUP_LOCK`）時，不得同步等工作者。
3. **包進工作者的**：
   - Memory：
     - `save_memory` 從查重到寫入整段是一個工作：同時存兩筆一樣的，只存一筆。
     - `query_memory` 讀事實庫、向量庫與舊版本（`describe_history`）是一個工作；來源行（`describe_sources` 要 await）留在主迴圈。
     - forget（`resolve_duplicate`）、`manage_dups` 的 scan、resolve、supersede，以及 reindex。
     - 診斷的向量點數、索引狀態與安全標記筆數。
     - 隨機回想的舊對話抽樣與事實；對話搜尋的舊對話庫（UI 對話要 await，留在主迴圈）。
     - 匯出（`export_facts`，全部事實、不設上限）。
   - Proxy：
     - `GET /v1/memory/list`（最多 5000 筆事實）。
     - 切換嵌入模型：偵測有沒有記憶（讀事實庫）與寫設定、重置單例（`reset_singletons` 要等向量庫單例的鎖，工作者第一次建向量庫、載入模型時拿著它）。
     - 刪除靈魂前的記憶檢查：`assert_memory_cleared` 改成 async，兩個呼叫點都 await。
     - 重置的清記憶（`purge_memory`，用 `run_memory_job_to_end`）與重算（`remaining_memory`）；清除工作在自己的執行緒裡重算（`count_remaining_memory`）時同步等工作者。
     - 舊對話統計與手動同步端點（讀資料庫、算檔案雜湊、同步）。
   - 嵌入模型的延遲載入也在工作者上：第一次載入時其他記憶操作要等，主迴圈與其他對話不用等。
   - 診斷的資料夾大小、舊版 `facts.jsonl` 檢查與搜尋紀錄改用 `asyncio.to_thread`：要走整個資料夾或讀檔，但不碰記憶庫，不必佔著工作者。寫成 `memory_diagnostics` 裡的巢狀函式：工具檔多出頂層函式會被 Guard #45 當成工具入口改了形狀，`agent_id: str` 參數也違反身分規則（lint_ssot `IDENTITY_SSOT`）。
4. **保險**（平常只有工作者在用）：
   - `VectorStore` 每個公開方法整段持有一把整個行程共用的 `RLock`（Qdrant client 依路徑共用）：「先查再建 collection」與延遲載入模型不會被別的執行緒插進來。
   - `get_vector_store` 建單例與 `reset_singletons` 共用一把鎖：只建一個，換嵌入模型重置之後也不會留下舊模型的那個。
5. **測試沙盒**：刪沙盒前多一步「等記憶工作者做完」（`drain_memory_worker`，在關 SQLite、Qdrant 之前）；人工記憶評估探針也一樣。
6. **不變的**：
   - 工具回覆、端點、參數、錯誤碼與 OpenAPI 都不變。
   - 管理器碰儲存的方法（`resolve_duplicate`、`find_duplicates` 等）仍是同步的，呼叫端經工作者呼叫，不直接 await。只建物件或正規化 ID 的（`get_legacy_sync_service`、`normalize_agent_id`）照舊直接呼叫。
   - Guard #45 基準只多 `tools/__init__.py` 的一行匯入。`manager.py` 的 `save_memory`、`query_memory` 裡多了巢狀的 `save()`、`find()`；`diag.py` 的 `memory_diagnostics` 裡多了三個巢狀函式（資料夾大小、舊版 `facts.jsonl`、搜尋紀錄；原本就有巢狀的 `get_dir_size`）。Guard #45 不看巢狀定義；這些都不是工具入口，不收工具參數。

## 後果

- 記憶操作不再卡住主迴圈：查詢很慢、第一次載入模型時，其他對話與 API 照常。
- 所有記憶操作排一列：一個慢的（載入模型、重建索引、大量同步）會讓後面的記憶操作等。
- 已知限制：
  - 取消：等待被取消（例如用戶端斷線）時，還在排隊的工作不會跑，已經開始的會跑完。存記憶可能已經存了，但回覆沒送出；之後才失敗的只記到 log。
  - 只讀事實庫、本來就用 `asyncio.to_thread` 的（`_scrub_dependencies`、`assert_id_free`）沒改；換嵌入模型重置單例之後，第一次取事實庫的若是它們，結構升級會在那條執行緒上跑（升級有自己的備份鎖）。舊對話歸檔 `archive_payload`（滑動視窗與 UI 對話）仍在預設執行緒池：SQLite 有自己的鎖，但 `LegacySyncService.stores` 字典第一次建立時有競態（記在 HANDOVER §3）。
  - 還在主迴圈上同步讀的：來源行裡的舊對話 `get_message`（一次一筆）、`normalize_agent_id` 讀代理人註冊表（快取 60 秒）。記在 HANDOVER §3。
  - 測試沙盒、記憶評估探針結束前等工作者做完；若工作者真的卡死（例如有人違反第 1 條規矩），行程結束時會等不到它（工作者不是 daemon 執行緒，`ThreadPoolExecutor` 結束時會等它）。所有測試的等待都有逾時，會先變紅。
  - 沒加 `force_disable_check_same_thread`：它只在 THREADSAFE=2（macOS 這類）有作用；Windows、Debian 上 qdrant 本機模式本來就不檢查執行緒。
  - 計畫提到的 tokenizer 多執行緒問題是推測，沒有驗證；現在嵌入只在工作者上做（另有鎖），不會同時被兩條執行緒用。
- 撤回：revert 即恢復原樣；不改任何資料。本 ADR 檔保留，在狀態註記撤回。

## 驗證

- 新檔 `test_memory_worker_thread.py`：假的向量庫、舊對話庫（與只讀事實庫的幾個呼叫）被呼叫時記下所在的執行緒，並等主迴圈放行；在主迴圈上跑會等不到，放行之後才在主迴圈上跑的由「執行緒不只一條」抓到。
  - 先寫的 15 個測試在修正前 14 個紅；「同時存四筆一樣的只存一筆」修正前後都綠（主迴圈上本來就不會交錯），是防回歸用。
  - 之後補的：resolve 與手動同步端點兩個呼叫點；工作者本身（巢狀呼叫在原地跑、主迴圈上同步等被拒、contextvars 帶過去且不外流、等待取消時排隊的不跑）；向量庫的鎖（兩條執行緒同時第一次用時 collection、模型、單例都只建一次，用 barrier 讓兩條真的同時到）。所有等待都有逾時。
  - DEV-B 第 1 輪之後補的：記憶列表、匯出、切換嵌入模型三個呼叫點；診斷的資料夾掃描與讀檔不在主迴圈上；「在工作者上」改成核對執行緒名稱（原本只核對不是主執行緒、只有一條，改用一般執行緒池也會過）；取消測試先確定排隊的取消了才放行正在跑的（原本偶發變紅）；巢狀呼叫改在暫時當作工作者的 daemon 執行緒上測（原本捷徑壞掉時真的工作者卡死，pytest 結束時等不到）；取消後才失敗的記到 log；`run_memory_job_to_end` 與重置的清記憶被取消（兩次）時等做完。
  - `test_session_sandbox_cleanup.py` 釘住第五個釋放步驟與順序；`test_probe_bounds.py` 釘住探針的清理順序。
- `assert_memory_cleared` 改成 async 後，3 個測試檔的替身與呼叫改成 await／`AsyncMock`；用 `-W error::RuntimeWarning` 跑過，沒有沒被 await 的 coroutine（改之前其中一個測試是空過的）。
- 相關測試（88 個檔）2623 個通過；新測試檔與沙盒清理、活動事件測試用 `pytest-randomly` 亂序連跑三次都綠。
- 全量：pytest 15664 個通過、8 個環境跳過，失敗只有已知的本機 8 個（compose 6 個、q48 2 個）和 ruff 基準（`purge_memory` 補了說明文字，D103 少一個，已更新基準）；vitest 2059 個通過；guard_all 只剩 #22（msgpack 1.2.1 新公布的弱點，只在測試工具 pip-audit 的依賴裡，和本變更無關，另批升級）與已知的 #23。
- DEV-B 第 1 輪之後的全量：pytest 15673 個通過、8 個環境跳過；失敗是已知的本機 8 個，加上 `test_run_usage_tiers.py` 的一個（測試替換的 `resource_manager` 和程式取到的不是同一個，看執行順序；本變更沒碰它，單獨跑與接在本檔之後跑都過，同一輪的前一次全量也沒出現）；vitest 2059 個通過；guard_all 只剩已知的 #22、#23。相關 6 個測試檔亂序連跑三次都綠。
- 變異檢查 43 個全部變紅，每個都由對應的測試抓到，捷徑壞掉的也不再卡住：
  - 工作者：不複製 contextvars、兩條工作執行緒、拿掉主迴圈誤用檢查、拿掉工作者內的捷徑（await、做完才放手、同步等各一）、拿掉取消後的失敗記錄（兩種各一）；`run_memory_job_to_end` 不 shield、會跑排隊的、第二次取消就放手。
  - 保險：拿掉向量庫的鎖、拿掉單例的鎖。
  - 呼叫點：25 個包進工作者的呼叫點逐一改回直接呼叫（存、查、forget、匯出、診斷三處、reindex、supersede、scan、resolve、對話搜尋、隨機回想兩處、統計三處、同步、記憶列表、切換模型兩處、刪除前檢查、重算、清記憶，以及清除執行緒的同步重算）；清記憶改回一般的 `run_memory_job`；診斷的一處改用一般執行緒池；診斷的 3 個讀檔改回在主迴圈上跑。
