# ADR-0273：MCP 連線由專屬 owner task 持有（一條斷不再連坐）

> C2-3 現況補註：專屬 owner 結構保留；隔離 shared stdio 的 EOF 會通知失聯並退避重試三次，direct／遠端不自動重連。generation、連線鎖及關機閘已接上；restart 時間多了取鎖與關舊連線，原約 22 秒不再適用。 詳見 ADR-0275。

- 狀態：✅ 已接受（life1；維護者 2026-10-16 核准計畫，含一次對抗式審查；修正的裁定經審查同意）
- 日期：2026-10-16
- 決策者：維護者
- 影響範圍：ToukaProxy（新 `protocols/mcp/owner.py`；`protocols/manager.py` 的 `__init__`、`connect_server`、新 `_install_handle`、新 `_on_connection_lost`、新 `_connection_lost_hook`、`cleanup`、`_close_handles`、`_stop_subscriptions_for_installation`、`_start_subscription`、`_protocol_version`、`_feature_support`；`protocols/mcp/handles.py` 的 `swap()` 與 `LegacyMcpHandle.close()`）；tests（新 `tests/unit/test_mcp_connection_isolation_security.py`、`tests/unit/test_mcp_connection_owner.py`、`tests/helpers/mcp_asgi_peers.py`、`tests/fixtures/mcp_v2/lifecycle_stdio_server.py`；`tests/unit/test_mcp_midflight_transport_failure.py` 三個 xfail 轉綠）；SECURITY.md 一列
- 相關：本 ADR 修訂 ADR-0199 dep1 補記的「已知未解決」（改為已解決），ADR-0203 dep1 補記的同一點也改為已解決；補註 ADR-0191（restart 的 15 秒逾時從此安全，最壞約 22 秒）。ADR-0194（握手失敗的取消轉成連線錯誤）、ADR-0198（principal handle 隔離）、ADR-0206／0207（OAuth 與 workload OAuth）決策都不變；`client._connect_once`、SDK、`main.py` 都沒改

## 背景

1. SDK v2 的 `Client`、streamable HTTP 與 stdio transport、`ClientSession` 都把 anyio task group 掛在「進入它們的那個 task」（host）上，而 Touka 在呼叫端 task 進入、之後一直不退出：
   - 啟動時的 shared 連線 host 是 lifespan；聊天請求在 ToolLayer 第一次用到時建立的連線 host 是那個請求；restart、存 MCP 設定、Hub 安裝重建的連線 host 是那個 HTTP 請求；工具呼叫時延遲建立的 host 是工具 task。
2. **途中失敗會取消 host**：每個請求的 POST 是 transport task group 的子 task。連線錯誤、對方斷線、讀取逾時、dep1 轉址 hook 擋下的 3xx、OAuth auth flow 例外、workload fence 取消 POST，都會讓 anyio 取消整個 task group，取消送回 host。lifespan 被取消後，清理時其他 MCP 連線、Dispatch 監督與背景任務一起停，畫面卻看起來正常（dep1 以 xfail 重現）。
3. **從別的 task 關閉也會取消 host**：restart、重載（`initialize_all` 開頭的 cleanup）、`add_server` 重加、workload fence 在別的 task 呼叫 `stack.aclose()`：`ClientSession.__aexit__` 先取消自己的 scope（送給 host），之後在 `__exit__` 拋「different task」被吞掉；scope 沒正常退出，anyio 會一直重送取消。life1 第一步用 xfail(strict) 證實：從另一個 task restart 或重載，建立者都被取消。
4. **連坐**：同一個 host 依序建立的連線，scope 一層包一層，取消遞迴殺到後建的連線。
5. 依禁令 20 視為可被外部觸發的可用性弱點：惡意或故障的遠端 MCP 伺服器只要在呼叫途中斷線或回 3xx，或管理員在 MCP 頁按 restart，就能停掉背景自主。

## 決策

### owner（`protocols/mcp/owner.py`）

1. **每條連線一個 owner task**：`McpConnectionOwner` 用 `asyncio.create_task` 建立自己的 task，`connect_mcp_server`／`_connect_once` 原樣在這個 task 裡跑，`stack.aclose()` 也在同一個 task；別的 task 只透過 owner 的方法要求關閉，永遠不碰 stack。owner 在 anyio 裡沒有 parent scope，呼叫端的取消碰不到連線，連線的取消也只落在 owner。
2. **整條連線包在 owner 自己的 anyio `CancelScope`**：中止握手、關閉期限到都只取消這個 scope；SDK 包在 shield 裡的 stdio 收尾（關 stdin、等、殺行程樹）照常跑完。scope 必須包住整條連線：只包關閉那一段會違反 anyio scope 的後進先出。
3. **關閉期限**：`OWNER_CLOSE_SECONDS`＝7 秒（SDK stdio 在 POSIX 最壞約 6.5 秒才殺完行程樹），從第一次要求關閉起算；期限到＝硬關（取消 scope）並記一行 WARNING。`OWNER_CLOSE_GRACE_SECONDS`＝1 秒寬限。
4. **介面**：
   - `start()`：回傳已 discover／adopt 的 SDK Client；握手失敗把原例外交給呼叫端（不是 Exception 的 BaseException，例如 BaseExceptionGroup，轉成 ConnectionError，owner 不留未讀例外；取消、KeyboardInterrupt、SystemExit 照樣往外拋）。呼叫端在握手中被取消時中止 owner，在期限＋寬限內吞掉後續取消（asyncio 多次取消、anyio 一直重送都算），等 owner 收完才把取消拋回。
   - `aclose()`：任何 task 可呼叫、可重複；等 owner 收完，最多到期限＋寬限。呼叫端自己被取消就重拋，owner 在背景照常關，不因此硬關。
   - `abort()`（cleanup 用）：還在握手就中止，`start()` 拋 `McpConnectionAborted`（`ConnectionError` 子類）；已連上就要求關閉。
   - `alive`：已連上、沒斷線、也沒人要求關閉。
5. **失聯通知**：傳輸自己出事、沒有人要求關閉時，owner 在自己的 task 關 stack，從 ExceptionGroup 取出根因；等 anyio 離開 scope、取消狀態清乾淨後，呼叫 `on_lost` 恰好一次（`on_lost` 拋錯只記 log）。被要求關閉（含期限到的硬關、關閉途中又出錯）不通知；關完後還有外部原生取消（關機時事件迴圈取消全部 task）也不通知，task 以取消結束。owner task 不會以例外結束。
6. **不記錯誤內容**：關閉錯誤的 WARNING 只記根因的型別名稱（禁令 23）。
7. **contextvars**：`create_task` 複製呼叫端的 context（含 workload 一次性票）；連上後 owner 在自己的 context 把 `_CURRENT_WORKLOAD_AUTHORIZATION` 設回 None。每次呼叫的 POST 在呼叫端的 context 送出（SDK 的 sender context）。
8. **不用 `spawn_logged`**：關機時 `cancel_all_background_tasks` 先於 `protocol_manager.cleanup` 執行，會硬取消它。

### manager（`protocols/manager.py`）

1. `__init__`：`_connection_owners` 是 owner 的強參考集合（asyncio 只留弱參考），owner task 結束時移除。
2. `connect_server`：簽章與 `@with_retry` 不變；內部 `owner.start()` 再 `_install_handle`。
   - 握手被 cleanup 中止（`McpConnectionAborted`）：不安裝、正常返回，`with_retry` 不重試。
   - 裝好時 owner 已經不活（裝的期間就斷了或被要求關閉）：不清 `last_error`、不啟動訂閱。
3. `_install_handle(key, handle)`：唯一放置點。`handle_pool.swap()` 先換上新的，再停該 key 的舊訂閱、關被換掉的舊 handle（被換掉的舊連線一定會在期限內關閉（SDK 收尾會殺行程樹））。呼叫端的取消照常往外拋，舊 owner 在背景關完。
4. `_on_connection_lost(key, owner, error)`：在 owner task 內執行。只在這個 owner 仍是 pool 裡那條時：記錯誤（`_record_error`，去敏）、停該 key 的訂閱、記 WARNING，再呼叫 `_connection_lost_hook(key, error, owner)`。不拿鎖、不重連；handle 留在 pool，之後的呼叫快速失敗、斷路器照常，維持手動 restart。被換掉的舊 owner 失聯時不記錯誤、不呼叫 hook。
5. `_connection_lost_hook(key, error, owner)`：同步、預設什麼都不做，是 U3e 的重連入口（見下一節）。
6. `cleanup`：
   - pool 裡的 handle 並行關（`_close_handles(..., concurrent=True)`），共用一個總期限＝7＋1 秒，不論幾台都不疊加；restart、`add_server`、`_install_handle` 照舊逐台關（本來就只關一台）。
   - 不在 pool 裡的 owner：還在握手的中止（不重試）；已連上還沒裝進去的要求關閉；等待期間才裝進 pool 的再正常收掉，不取消。
   - 總期限到還沒結束的 owner 記一行 WARNING（只記件數與名稱），之後不再等，強參考集合不保證清空。
   - `_connect_locks.clear()` 不動。
7. `_close_handles(handles, *, concurrent=False)`：owner 自己掌握期限；沒有 owner 的 stack（測試替身）用同一期限 `wait_for`。cleanup 路徑吞取消的既有行為不變。
8. `_stop_subscriptions_for_installation`：失聯處理先停掉的就跳過（原本 `pop(key)` 會 KeyError）。
9. `_start_subscription`：訂閱 supervisor 用清掉 workload 一次性票的 context 建立。
10. `_protocol_version`／`_feature_support`：已關閉的 SDK Client 讀屬性會拋 RuntimeError，回「未知」。transport 子 task 被原生取消時 task group 不帶例外，`Client.__aexit__` 正常跑完、`_session` 變成 None；失聯後留在 pool 的這個 Client 若讓 `get_active_server_info` 拋錯，MCP 頁的伺服器清單會整個變空。

### `protocols/mcp/handles.py`

- `HandlePool.swap()`／`LegacyHandlePool.swap()`：換上新的、回傳被換掉的。
- `LegacyMcpHandle.close()`：stack 是 owner 時呼叫 `owner.aclose()`（期限由 owner 掌握），其他 stack 照舊 `wait_for`。

## 給隔離執行的後續單位（U3e）

- **入口**：建立 `connect_server`；安裝 `_install_handle`（先換再關）；關閉 `_close_handles` → `owner.aclose()`（正常在 SDK 自己的收尾時間內完成；期限 7 秒到時取消 owner 的 scope，隔離執行的 Transport 取消路徑在這時 killpg；SDK stdio 的 shielded 收尾照常跑完）；取得 `_ensure_handle`（語意不變）；斷線通知 `_on_connection_lost` → `_connection_lost_hook(key, error, owner)`。
- **hook 只能排程、不能 await**：它在失聯那條的 owner task 內同步呼叫，會繼承建立者的 contextvars（測試確認：建立者設的 ContextVar 在 hook 裡讀得到）。排重連要用全新的 context。不要在 `_on_connection_lost` 或 hook 裡 await 重連或拿每台的鎖：會等到正在結束的 owner 自己。
- **重連前用 `is` 比對**：重連 task 開始時，再確認 pool 裡那條的 owner 是不是還是這個 `owner`，不是就放棄。
- **不觸發的情況**：被換掉的舊 owner 失聯；硬關；cleanup；和要求關閉同時發生；stdio 伺服器自己結束（見已知限制 1）。HTTP 要到下一次送出才發現。
- **可能在 `connect_server` 結束前觸發**：`connect_server` 不再把失聯紀錄清掉。
- **沒有「關機中」旗標**：hook 排的重連可能在 cleanup 之後又建出連線，由 U3e 加旗標。
- 本 ADR 不加鎖，owner 也不拿 manager 的鎖，U3e 拿著每台的鎖等 `owner.aclose()` 不會卡住。被換掉的舊連線關閉期間（最長約 7＋1 秒）新舊並存；同一台嚴格互斥要 U3e 的每台一把鎖。

## 已知限制

1. **stdio 伺服器自己結束時不會即時通知**：伺服器崩潰、被 OOM 或被 kill 時，owner 不知道，`on_lost` 與 hook 都不呼叫、訂閱不停；要等下一次呼叫失敗才記錯誤，MCP 頁也要到那時才顯示錯誤。查證結論：SDK 2.2.0 沒有公開、可 await 的「連線已關閉」訊號（dispatcher 讀到 EOF 只把私有旗標設成 True 後正常返回；`Client`、`ClientSession` 沒有關閉 callback，參數又在 `_connect_once` 裡給定）。選項摘要：輪詢私有旗標或撈私有 task（不採用，依賴私有結構）；owner 定期 ping（背景流量、誤判；workload OAuth 伺服器上反而會把好連線判成失聯）；在 `_connect_once` 包一層讀端（life1 不改，適合 U3e 換隔離傳輸時做）；工具呼叫拿到 `CONNECTION_CLOSED` 時補走失聯流程（不是即時）；向 SDK 上游提議。測試 `test_stdio_server_exit_is_not_reported_until_the_next_call` 釘住目前行為。
2. **workload 一次性票**：owner 自己的 context 與訂閱 supervisor 的 context 已清掉票；**transport 子 task（`post_writer`、`JSONRPCDispatcher.run` 等）仍帶握手時的 bootstrap 票**——它們在握手期間由 owner 的 context 複製出來，life1 不改 `_connect_once`／SDK。這是既有問題，列候選，**啟用 heartbeat workload OAuth 之前必須先修**（目前 `HEARTBEAT_WORKLOAD_OAUTH_ENABLED = False`）。
3. **孤兒行程**：期限到的硬關仍讓 SDK 跑完收尾並殺掉行程樹（Windows 約 2.5 秒、POSIX 最壞約 6.5 秒；只在 Windows 實測：stdin 關了也不結束的伺服器、期限縮成 0.5 秒，owner 結束、子行程被收掉）。Proxy 被強殺時，Docker 內的 PID namespace 會一起收掉；原生 Linux 會留孤兒（life1 之前就有，交隔離執行 Transport 的 killpg）。SDK 自己的收尾卡在 shield 裡超過期限＋寬限時，cleanup 記 WARNING 後不再等，行程留到 SDK 收完或 Proxy 結束。
4. **關機**：MCP cleanup 最壞約 7＋1 秒，不論幾台；cleanup 開始後才建立的新連線不在這個總期限內。整個 lifespan 收尾不保證在 10 秒內（`cancel_all_background_tasks` 本身就可能用到 10 秒）；超過時 Docker 會 SIGKILL（compose 沒有 `stop_grace_period`，預設 10 秒；要不要設列候選）。
5. **restart 最壞約 22 秒**（15＋7）：`restart_server` 的 15 秒 `wait_for` 逾時後，呼叫端還要等 owner 收尾才回失敗。呼叫端或中間代理的逾時小於約 22 秒時，restart 可能顯示失敗，但背景仍會完成。只讀 tracked 檔核對過路上的逾時：restart 端點本身沒有逾時（回應前另跑 `get_active_server_info`，每台已連線的伺服器依序最多 3 秒）；middleware、uvicorn 沒有每請求逾時；`nginx/nginx.conf.example` 一般 REST 是預設 60 秒；ToukaChat 沒有代理逾時。
6. **舊 generation 晚完成會蓋掉新設定**（MEDIUM-N1，life1 之前就有，沒變更糟）：由 U3e 的每台一把鎖或 generation 比對處理。
7. **失聯 handle 讓工具清單每回合記一筆錯誤**：`get_all_tools` 對失聯那台記一筆帶 traceback 的 ERROR；MCP 頁那一列的錯誤訊息顯示健康檢查的結果（例如「Connection closed」），失聯原因被蓋掉（真正原因在失聯當下的紀錄與 WARNING log），下次工具呼叫也會蓋掉。`get_all_tools`、健康檢查不在 life1 範圍，交 U3e。
8. 【推測，未實測】同一個 key 兩個 connect 並行、又開了訂閱時，先裝上的那條若在自己的 `_start_subscription` 之前就被換掉，可能對已關閉的 session 起一個沒人停的 supervisor；U3e 每台一把鎖後消失。
9. **只在 Windows 實測**（Proactor；SDK stdio 的 Windows 路徑），POSIX 等 CI。

## 驗證

本機 Windows、3.12 venv、mcp 2.2.0、anyio 4.14.2、fnm Node 24.21.0；全部走 conftest 沙盒與 mock、真 stdio 只用 `sys.executable`，不讀 `data/`、不連外、不打付費 API，也沒有啟動 Touka。

- TDD：第一輪先紅後綠（xfail(strict) 證實從另一個 task restart／重載、遠端途中斷線、302 都會取消建立者；dep1 的三個 xfail（連線錯誤、讀取逾時、307）轉綠）；修正輪先實作、後補測試，紅燈靠暫時還原產品檔與突變證明；KeyboardInterrupt 小修先紅後綠。
- 新測試：安全檔 5 個（restart、重載、遠端斷線、302、I1：遠端斷線後隔離執行的背景重測仍在跑；檢查建立者沒被取消、另一條照常、斷掉那台快速失敗且不自己重連、失聯通知恰好一次、沒有未讀取的 task 例外）；owner 檔 38 個（見 tests/README）。
- 突變：owner 改回在呼叫端進入、不關舊的、`on_lost` 跳過或兩次、不取根因、吞 ExceptionGroup、在 anyio 取消範圍內通知、硬關也通知、不清強參考、cleanup 不中止握手、不清票、失聯時重連、改用 `spawn_logged`、握手被取消不中止、先關再換、`_protocol_version` 不接 RuntimeError、不核對 owner、硬關改回原生 `task.cancel()`、呼叫端被取消就硬關、cleanup 逐台關、不收期間裝進來的、不記 WARNING、裝的期間斷線仍清錯誤、`pop(key)`、收尾等待不吞取消、`_install_handle` 吞取消、中止的握手照樣重試、非取消 BaseException 也重拋、KeyboardInterrupt／SystemExit 被吞、WARNING 記全文、hook 沒收到 owner、supervisor 不清票、`_install_handle` 不停舊訂閱、失聯不停訂閱、不看外部原生取消，全部有測試變紅。
- I1 在 life1 之前的產品程式上是紅的（背景重測被 lifespan 收尾連帶取消），之後是綠的。
- 全部 MCP 單元測試連跑 5 次穩定；全量結果見 HANDOVER §2。

## 回復

revert 這個 PR 即可。不寫任何資料、不改設定檔，撤回後沒有要清的東西；SECURITY.md 那一列、ADR-0191／0199／0203 的註記要一併還原，`tests/unit/test_mcp_midflight_transport_failure.py` 的三個案例要放回 xfail。撤回後背景第 2～4 點的缺口全部回來。
