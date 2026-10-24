# ToukaProxy 標準化介面文件 (Standardized Interface)

> **TL;DR** — JWT 雙軌 Token（Access 1h + Refresh 30d；`auth_service.py` `now_sec + 3600` 為 SSOT）；所有端點需 `Depends(get_verified_identity)` 或在 `endpoint_auth_allowlist.py`；三種模式 Manual/Auto/YOLO＋程式常數底線＋Auto 審查員（ADR-0246）；`/heartbeat/awaken` token 驗證唯一入口 `verify_heartbeat_token()` Depends；MCP workload OAuth（ADR-0207）只開 HEARTBEAT、要求單一 Proxy process＋local filesystem lock，enterprise 維持 fail-closed；MCP client／server 為 modern-only `2026-07-28`（ADR-0221），discovery 錯誤 fail-loud 不重連 legacy；`/v1/creation/*` 創建端點限 Owner/admin。

## 刪除靈魂清隔離家目錄、密碼重輸錯誤碼（2026-10-21）

- `DELETE /v1/souls/{soul_id}`：記憶與活動檢查之後、刪任何東西之前，清這個 ID 在隔離暫存區的 HOME／TMP（proxy 的 `ST/proxy/agents/<id>`；預期是執行器時另經既有 START 清 `<state_root>/agents/<id>/{home,tmp}`，ADR-0272 限制 26 補記）。清不掉回 503 `SOUL_SANDBOX_HOME_NOT_CLEARED`（1418），什麼都不刪，不帶原因與路徑。
- 已登入者重輸密碼打錯一律 403 `AUTH_REAUTH_FAILED`（1111），比照靈魂步升：重置（含清除連備份）、還原、設定匯入、`/auth/clear-my-data`、`/auth/export-my-data`、`/auth/change-password`。原本的 401 會讓 `fetchTouka` 續期重送，同一次點擊驗兩次密碼；`AUTH_INVALID_CREDENTIALS`（401）只留給登入。

## 對話歸屬、UI 換綁與 SNS（ADR-0276）

- 心跳、排程執行、SNS、工具及一般 append/upsert/conditional 保存都不能跨靈魂換綁；角色特權不能代替精確歸屬。儲存層在 session 鎖＋IMMEDIATE 交易內讀最新列、驗 scope／revision 後寫入，缺席與失敗分開。
- 只有真正人類的 UI 專用保存或明示持久串流能換綁，須有舊對話存取權及舊、新靈魂的驅動權。非空換綁保留原樹、內部 `session_scope_version` 降為 0；原列與待存列均是無節點、無摘要、非家且非保留頻道的嚴格空草稿，才保留標記。家對話須先取消家；不能用 A→NULL→B 繞過。
- `POST /v1/ui/sessions/{id}` 忽略 `is_home`／`home_for_agent`（包括 false／null）；家欄只由 `POST /v1/ui/sessions/set_home` 修改，受管首次建家除外。set_home 在交易內先驗目標再清舊家；公開模型不增加 marker，list／GET／snapshot 也不回傳它。
- HTTP 歸屬拒絕為 403 `SESSION_SCOPE_MISMATCH`（1221）；UI 條件保存沿既有 `persistence.status="failed", error_code=1221` 外形，不把每個保存拒絕改成 HTTP 403。stale revision 沿 `SESSION_SAVE_FAILED`，不明保存為 `unknown`。明示串流准入衝突回 409、不明回 503；生成／保存終態仍分開，保存成功帶提交後 revision。
- UI 不可建立 `touka.` ID，admin 亦同；保留頻道也不能換綁。心跳獨立模式的省略／空字串／`pulse_channel_default` 解析為 `touka.hb.v1.<hash>`；SNS 為 `touka.sns.v1.<hash>`，完整雜湊分別涵蓋靈魂與靈魂＋平台＋頻道。舊 `pulse_channel_default`／`sns_*` 保留、不搬、不續寫；詳見 `HEARTBEAT_INTERFACE.md`。
- 家定位／注入共用 `read_home_snapshot`，驗 `is_home=1` 且正式化的 `agent_id`、`home_for_agent`、請求靈魂三者相同；多家、退休家與矛盾家拒絕，不任取一筆。顯示不回 ID、注入不回歷史；心跳／SNS 不把拒絕當成沒有家。儲存讀取失敗維持 typed persistence failure。
- SNS 單靈魂偵測僅能指向 adapter 自己，或其唯一人類擁有者同時擁有的靈魂；不確定就拒絕，不借 admin／Debugger 放寬。閘道以可信身分存檔，pipeline 只收固定的目標靈魂 ID 與 `request.session_id`、不帶身分（外人訊息的技能與特權維持零）；assistant 保存失敗不外送，例外原文不送給平台。`ToukaGroup` 不選／不寫單靈魂 session，也未新增群聊儲存。
- marker 只在受管 INSERT 設 1，舊列預設 0、更新永不提升；部署加欄與重啟會作廢舊 revision，須重讀 snapshot。准入換綁後生成失敗／取消／no_text 不回滾，不能自動重送模型；所有 writer 須一併升級。
- `POST /v1/souls` 與 `POST /v1/souls/{id}` 補建靈魂時，在寫 config 前、後各以單一交易降低正式 ID／別名的 agent_id 或 home_for_agent 列 marker，第二次涵蓋期間的人類存檔；失敗回固定 `SESSION_SAVE_FAILED`，第二次失敗回滾新靈魂／復原原設定。新增或轉換的 ID 以 `ToukaSoul` 開頭（不分大小寫）時，在降標記前回 `VALIDATION_ERROR`。既有靈魂的一般更新保留 marker；不刪對話、不改內容或擁有者。工具補建仍限同一靈魂，見 ADR-0277。
- `POST /v1/system/restore/run` 的背景還原先經 ResourceManager 在當次資料根原子寫入 `.session-restore-pending`，寫入失敗不啟動子程序；標記存在時，ui／all 對話搜尋在讀列前回固定 `internal_error`。還原成功或失敗都在報完成或重啟前經 Proxy SessionService 將現存對話庫全部 marker 降為 0，成功才刪持久標記；備份不含 DB 亦同。降標記或刪標記失敗以 `failed`／`SYSTEM_BACKUP_FAILED` 回報、不重啟。缺庫不建立，缺表／欄視為無列可降，交正常 startup migration（預設 0）。啟動在 migration 後、服務請求與 bots 前補降待處理還原；失敗保留標記並記固定 `SESSION_RESTORE_RECOVERY_FAILED`，不阻擋啟動、搜尋仍拒絕。還原的 UI 對話不可搜尋，只有還原後新建的能依正常條件搜尋；續寫不恢復舊標記。還原 `.env` 改用另一資料根不在此保護範圍，見 ADR-0277。

## 演化部署與驗證

- 技能名稱只收 `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`，拒絕 Windows 保留名稱及非字串。已部署能力不能再演化：原始設定中的停用、改名、巢狀路徑與實體別名也算；設定讀不到時拒絕，`reason=config_unreadable`。使用新 `skill_id`，或先由人於設定頁移除舊伺服器。
- 部署判定會凍結明確引用的 `managed/<skill>`；command、載入程式的 env（如 `PYTHONPATH`／`NODE_PATH`）或選項（如 `uv run --directory`）指向 managed 根或其上層時，全部技能都凍結。只有普通參數或資料哨兵解析到 data 根時不因此凍結全部技能，安裝 Filesystem 不會阻擋未部署技能。設定無法讀取或結構錯誤仍全部拒絕演化。
- 部署凍結與啟動分類共用 Proxy cwd、自建與第三方的 state home 作為解析基準，涵蓋尚不存在、沒有分隔符但帶副檔名的檔名；計算位置不建立目錄。
- 同一技能的演化與部署不排隊；忙碌回 `validation_error`／`skill_busy`。部署名稱被占用或審批期間程式、依賴有變更時拒絕；只有 `server.py`、`requirements.txt` 兩檔各以原始位元組 SHA-256 核對，最後核對與寫入在同一 worker 完成。預覽後快照檔案消失時仍綁定身分送審，但任何核准都不能部署；目錄內其他可執行內容未綁定，見 `SECURITY.md`。
- 驗證必須真的啟動支援 modern MCP 的伺服器並收到非錯誤工具回應；輸出字串不決定成敗。部署存檔與啟動結果分開回報；未啟動時提示查看 MCP 隔離狀態。
- 熱掛載先同步 registry 與安裝版本，再判定及連線；只有目前版本的連線仍存活才回報 `started`。技能安裝與演化部署都檢查 `add_server` 的結果；沒有 manager、握手中止、等待隔離或被重載取代時回報 `saved; not started`，設定仍已儲存。這是回報當下的結果：並行重載可能稍後連上，請以 MCP 清單狀態為準；隔離連線失聯後可沿目前版本重新連線。
- principal-scoped 的遠端伺服器在 `add_server` 沒有 request context 時回 `False`，表示設定已登記但連線延後，並非安裝失敗；取得對應 principal 的 request context 才能建立連線。

## MCP 隔離連線與手動啟動

本節記錄 C2-3 已接線的行為；決策見 ADR-0275。以下端點均有 `/v1` 前綴。

來源與資料夾權限存於 `get_config_dir()` 下的 `mcp_server_isolation.json`，`server_config.json` 格式不變。`origin` 只由寫入程式指定：Hub＝`hub`、技能安裝＝`skill`、人類新增＝`manual`、創建＝`creation`、模板＝`scaffold`、演化部署＝`evolution`；設定頁只能單向標為 `agent`。匯入不採納來源與額外資料夾，來源也不在政策 carry-over 清單內。

非人類存檔只要 env 名稱或值改變（含刪除），第三方／scaffold／未標示來源就改為 `agent` 並清掉額外資料夾；已是自建的來源保留。比較在遮罩回填後進行，人類直接換 token 不會撤銷資料夾。快照匯入的 env 變動一律按不可信內容處理，同樣撤銷來源與資料夾；遮罩回填而沒有實際變動則保留。

快照中的非物件 MCP 條目逐筆拒絕，摘要 `warnings` 指出名稱與原因；merge／replace 都保留該名稱既有有效設定，其餘合法條目繼續匯入。預覽也顯示警告但不寫入。用合法條目修復非物件的既有設定時，按來源變動撤銷舊授權；`mcpServers` 本身不是物件則回 `VALIDATION_ERROR`。

command、所有參數及 path-like env 值均解析路徑，不依啟動器名單放寬；引用到代理人可寫根內的位置即當自建，包含資料夾、尚不存在的路徑與普通資料參數。程式參數中的現存資料夾也從嚴當自建，shell 文字中的引用亦會檢查；npx／uvx／bunx 的 scoped package 規格保留套件名稱語意。UNC／裝置命名空間從嚴當自建。

解析結果只供分類與 profile 資料夾授權，不改寫啟動的 command／args；既有哨兵解析與 APPDATA 對映除外。Docker image／volume、deno 的 npm／jsr 規格、版本與模組名原樣交給 runner。state home 的引用命中時改走自建，所有基準找到的部署技能均保留。profile 授權以實際 cwd 為基準，只認絕對路徑、含分隔符或實際存在的引用，不因套件／模組名帶點而授權。需要從 Proxy cwd 取得的相對程式須改用絕對路徑。

分類器不展開動態字串：command、argv 或普通 env 的值只要含 `$`（含 `$VAR`、`${VAR}`、`$()`）、反引號、`%xx%`／`!VAR!`、萬用字元或大括號 `* ? [ {`、單字內的引號，就從嚴當自建；tilde 展開亦同。秘密值含 `$` 或 URL 編碼字串含兩個 `%` 夾住非空文字（如 `token%2F%3A`）也會觸發，即使內容實際不會展開；完整 URL 內的 `$`／`%xx%` 同樣觸發。其餘完整非 file URL 不當本機路徑。合法 scaffold 的 APPDATA 哨兵資料參數沿用第三方 profile 的 personal_drive 對映，試跑與正式啟動一致；其他 argv、載入程式的 env 與未通過形狀檢查的條目沒有此例外。

重載先同步作廢舊 generation，再清理與連線；設定快照綁定該次重載，過期工作不能關閉或替換新連線，握手重試不得取得較新的 generation。人類同意只准一次握手嘗試。執行器獨立處理 stdin 與控制通道，輸入佇列滿就終止該次執行；STOP／relay 斷線不受 pipe 背壓阻擋。stderr 先遮罩，再跳脫控制字元與文字方向控制字元。

| 類別 | 啟動方式 |
|---|---|
| `self`（`creation`／`evolution`／`agent`，或引用代理人可影響的程式） | 全域層級 `executor`／`landlock` 時經 runner；`none` 等隔離，只有人類可確認這次的降級去處 |
| `third_party`（`hub`／`skill`／`manual` 或未標示）與合法 `scaffold` | 看本機 Landlock 判定；可用就包住，尚在檢查或暫時故障就等待，穩定的不可用原因才直接啟動並標示未隔離 |
| `remote`（有 URL） | 不啟動本機子程序，不納入這套隔離 |

啟發式先於來源，包含一般資料夾引用；Filesystem／docgen 的哨兵也會解析後判定，可能因此成為自建、不能沿用第三方的額外資料夾授權。來源不明、紀錄讀不到時不放寬；讀不到的 stdio 一律當自建、不用額外資料夾。`bound` 只綁 command／args／url 的正規 JSON SHA-256，不含 env；不符時自建來源保留，第三方／scaffold 當未標示，額外資料夾失效。

- `GET /mcp/{name}/isolation`：需 Owner／Debugger／admin，回 `server`、`class`、`origin`、`record_state`（`ok/none/stale/unreadable`）、`sandbox_paths`、`bound`、`can_mark_agent`。
- `PUT /mcp/{name}/isolation`：另需人類請求者與 CSRF；body 必帶目前 `bound`，選填 `origin="agent"`、`sandbox_paths=[{path, access}]`。最多 16 個絕對資料夾路徑，access 只收 `ro/rw`；只有第三方／scaffold 可有非空清單，存檔及啟動都重驗永不授權。改為自建會清掉清單。
- 條目已變回 422 `mcp_entry_changed`；來源／路徑拒絕沿用 `VALIDATION_ERROR` 的代碼字串；紀錄讀不到回 500 `PERSISTENCE_ERROR` 且不覆寫。一般設定存檔的 `isolation_cleared` 列出因程式或非人類 env 變動而清除資料夾授權的名稱。
- 隔離設定寫入鎖、同意帳本與技能鎖以單一 Proxy 行程為前提（ADR-0207）；改多行程前須先換跨行程鎖。

- stdio 的 principal-scoped 設定在建立 owner 前拒絕；`GET /mcp/list` 沿用 `last_error` 回 `MCP_STDIO_PRINCIPAL_UNSUPPORTED`，前端透過錯誤目錄顯示原因與移除 `auth_mode`／改用遠端的改法。
- `POST /mcp/{server_name}/restart` 等同台連線鎖最多 5 秒；超過時仍回既有物件形狀，`status="busy"`，`server` 是目前狀態。取得鎖後握手預算仍是 15 秒；背景重連使用握手本身的預算。
- 啟動判定、principal stdio 拒絕與關機入口檢查在連線重試外；非遠端連線必須帶 `McpLaunch` 或 `_judge` 產生的直接啟動標記。直接啟動不傳 `launch`／`isolated`；等待不記連線錯誤。
- 只有隔離 shared stdio 失聯（含 OOM、伺服器自己結束）會在 5／10／20 秒後自動重連，連續三次後要人按重啟；連線穩定 600 秒、手動 restart 或重新初始化會重設次數。direct 與遠端照舊不自動重連。到期、延遲與穩定時間皆使用 `get_monotonic_ms()`，不受系統校時影響。
- `begin_shutdown()` 後不再接受連線、重連排程或安裝 handle；`initialize_all()` 不會解除關機。重試退避結束時也重查關機狀態；只有測試重用單例時明確呼叫 `reopen_for_test()`。
- `GET /mcp/list` 的 `isolation` 由啟動帳本提供；自建且 `waiting_isolation` 時多 `manual_start={fallback, features, generation, bound}`，features 排序，只有代碼與雜湊。第三方、遠端與已啟動者不提供手動啟動資訊。
- 手動重啟 body 的 `isolation_ack` 必須同時帶 `features`、`generation`、`bound`；features 最多 16 項，必須與畫面和當前去處完全相同，否則 422 `mcp_isolation_ack_mismatch`。取得鎖後再驗狀態與同意，拒絕原因記入 AUDIT；同意不保留給下次重啟。
- 層級 `none` 時，自建 MCP 每次開機或重啟都要重新同意；人同意以降級 Landlock 啟動後再失聯，重連不沿用同意，會回到等隔離。隔離恢復會重新判定；執行器 `boot_id` 改變也會排重連。
- 重啟回 `restarted`／`busy`／`waiting`／`failed`；`waiting` 涵蓋 `waiting_isolation`、`waiting_probe`、`suspended`。等待鎖期間已啟動且公開隔離狀態為 `running` 時回 `restarted`；拒絕安裝過期 handle 不回成功；失聯 owner 不再列工具，保留遮罩後的失聯原因。
- `PUT /mcp/{name}/isolation` 先取得 manager 再寫入；成功後排背景重連，`applies_on="background_relaunch"`；422／500 不排。
- `GET /isolation` 增加必填 `mcp={unisolated, waiting, suspended, store_issue}`。暫時性探測故障以單調時鐘等滿 660 秒轉 `suspended`，等待計數不重複包含暫停；首次 `probe_pending` 不計時，後續重探保留前次結果。設定讀不到只回 `mcp_isolation_store_unreadable`，不含名稱或路徑；警告只在進入讀不到狀態時記一次，恢復後再失敗才重記。
- 診斷使用傳輸共用的環境值遮罩，涵蓋解析後的多行值、失聯、工具與資料 API 例外鏈、清單問題與伺服器顯示資訊；關閉錯誤只記型別。
- 診斷文字（`last_error`、log、回給模型的 MCP 錯誤）裡的網址：http(s) 顯示主機、埠與白名單路徑段，其餘換成 `<redacted>`；`file:` 與其他 scheme 整段遮。欄位與型別不變。
- MCP 工具參數放進標頭後超過上限：工具結果回 `validation_error`（`reason=mcp_header_argument_invalid`、`denied_tool`、`suggestion`），不寫 `last_error`；這類拒絕與清單拒絕都不影響斷路器（不記失敗也不記成功）。
- 執行器已知限制：先關本機 relay 不代表遠端群組已收完；遠端清理依執行器 `REAP_SECONDS`，同一 state 的新舊遠端行程仍可能短暫重疊，未宣稱同 owner 排隊。
- 包住的條目 env 只進 `exec_env`，在啟動器套完限制後才交給目標。`checked_exec_env` 先移除保留名稱與 `exec_env_stripped` 命中的名稱，只記 log 警告（名稱、不含值）。保留名稱不分大小寫：`HOME`、`TMPDIR`、`TMP`、`TEMP`、`GIT_CEILING_DIRECTORIES`、`PYTEST_ADDOPTS`、`TOUKA_EXEC_ID`、`HF_HOME`、`HF_HUB_CACHE`、`TRANSFORMERS_CACHE`、`TORCH_HOME`、`PIP_CACHE_DIR`、`UV_CACHE_DIR`、`UV_TOOL_DIR`、`UV_PYTHON_INSTALL_DIR`、`NPM_CONFIG_CACHE`、`NPM_CONFIG_PREFIX`，以及 `XDG_`、`GIT_CONFIG_` 前綴。
- `exec_env_stripped` 對名稱做不分大小寫的子字串比對：`PYTHON`、`NODE_`、`JAVA_`、`GEM_`、`PERL`、`LD_`、`DYLD_`；任何位置命中就移除，包含 `NODE_ENV`、`MY_PYTHON_TOKEN`。另有顯式移除清單 `PYTHONHOME`、`PYTHONSTARTUP`、`PYTHONINSPECT`、`LD_PRELOAD`、`LD_LIBRARY_PATH`、`DYLD_INSERT_LIBRARIES`、`DYLD_LIBRARY_PATH`，均已被上述規則涵蓋。清單正本在 `sandbox_kernel/spec.py`。
- 過濾後留下的名稱若不符合 `[A-Za-z_][A-Za-z0-9_]{0,127}`，kernel 以 `spec_invalid` 拒絕啟動，不會自動改成 direct。請改成英數與底線、首字不能是數字；名稱被移除時，只有伺服器支援的不衝突名稱或原本允許的 direct 路徑可用。
- 自建只寫自己的 state，部署程式目錄唯讀＋可執行，不給建立者資料夾、專案或 `sandbox_paths`。第三方引用檔案只開所在資料夾唯讀＋可執行，不往上找專案；引用資料夾可讀寫但先過永不授權。APPDATA 哨兵／等於資料根的絕對路徑對映到 personal_drive 根，docgen 輸出夾另外開放；scaffold 另開自己的模板程式目錄唯讀＋可執行。

## 創建試跑隔離

- `/creation/mcp` 與 `/creation/scaffold` 的 SMOKE／EVAL 使用同一次 `CreationLaunchContext`；HTTP 只給當前層級，不給未隔離同意。SMOKE 拿不到隔離時沿用 HTTP 200、`status="rejected"`、`committed=false`，`errors` 帶 `isolation_unavailable`。
- `run_smoke=false` 仍可只登記設定；EVAL 也要過隔離判定，none 時不啟動候選程式。`activate=false` 只產生 scaffold，不試跑。
- 自建用 `mcp_self`，scaffold 用本機 `mcp_scaffold`，和正式啟動同一個 profile；工具從帳本取一次 grant，SMOKE／EVAL 共用，來源只由呼叫程式決定（`creation`／`scaffold`）。
- `create_mcp` 看全域層級，`scaffold_mcp` 看本機層級。有隔離時 YOLO 依層級執行；`none` 時仍只有人能核准，無人在場拒絕。Auto 仍受 F5 約束，Manual 照舊要人同意；HTTP 入口沒有這種降級同意。
- 直接呼叫 MCP 引擎也必須提供同一次 `CreationLaunchContext`；缺少時 SMOKE 回 `isolation_unavailable: launch_context_missing`，EVAL 回空清單、不啟動候選程式，也不重新取得層級。
- 隔離下 scaffold 的 APPDATA 參數對映至 personal_drive 根；試跑 env 保留 `clean` 語意：不解析哨兵、不加 `TOOL_BASE_URL`。試跑的 EOF 只關閉連線，不取消呼叫端任務。

## 模型溝通層第 0 段（ADR-0241）

- 請求、靈魂與模型設定裡的 `temperature`／`top_p`／`top_k` 照常接受（schema 不變），但三個 adapter 都不送給廠商；Gemini 不送 `generationConfig`（3b 起只送輸出上限與停止字串，見第 3b 段）。
- Claude：4.7 以後、5.x 與認不出的名稱，`thinking.enabled` 改 adaptive、不帶 `budget_tokens`；5.x 以後或開思考時不強制 `tool_choice`。
- 沒設主要模型的代理人：串流前回 409，body `{"detail":{"code":"AGENT_BRAIN_NOT_SET","id":1215,...}}`，不再改用寫死模型；`POST /v1/souls/{id}` 存檔什麼時候設主要模型，ADR-0250 起改成下方 Touka 待排小任務一節的規則（只有人類改了值或還沒有主要模型才換，新值要在模型清單裡）。
- 主要模型失敗時只改用同一路由家族（`route_family`）的上次可用模型，跨家族回原錯誤。
- 研究工具 Google 搜尋只用代理人自己的 Gemini 模型送一次；工具 schema／handler 不變。

## 模型溝通層第 1b 段：計費與 complete_once（ADR-0244）

- 用量：三家 adapter 回同一組四欄，`prompt_tokens` 只含未快取輸入，快取讀（`cache_read_input_tokens`）、快取寫（`cache_creation_input_tokens`）另列，加上 `completion_tokens`。`token_usage.json` 鍵名與稽核 CSV 欄位都不變；價格表沒填快取費率的模型，快取 token 照 input 價計。
- 每輪記帳：ReAct 迴圈每一輪解析後就寫進 `token_usage.json`，成本上限在同一次 run 中途就看得到已花的錢。E-Index 歷史每次 run 一筆；稽核 CSV 每次 run 一列，`cost_usd` 為整次加總、含快取。
- SSE `token_usage` 進度事件（前端「本回合用量」）：位置與欄位不變，`total` 改為整次 run 所有模型呼叫的四欄加總，`cost` 改為整次加總且含快取（以前只算最後一次回覆、不含快取）；`usage_pct` 與 Claude 的 `context_breakdown` 仍看最後一輪。只呼叫一次模型的回合數字不變，多輪的回合數字會變大。
- 原生 `POST /v1/messages`：照常記帳並寫 CSV（以前完全不記），但串流不送 Touka 的 `token_usage` 事件，回應 wire 不變。
- 摘要、互審 critic、創建評分改走 `complete_once`，記帳到對應身分（provider 標籤 `summarizer`／`critic`／`judge`）並受成本上限約束。超過上限時，摘要略過、只做純無損剪裁；critic 視同傳輸失敗＝REJECT；評分回 0.0。critic 依 critic 模型路由；`/v1/creation/*` 帶 `eval_scoring=true` 時，評分記到發起建立的身分。
- 公開 endpoint／schema 沒有改變，不重新生成 OpenAPI。

## 模型溝通層第 2a 段：只留靈魂（ADR-0245）

- 聊天要登入：`/messages`、`/chat/completions`（含 `/v1`）沒有已驗證身分回 401 `AUTH_INVALID_TOKEN`，不進 pipeline；首次安裝前的本機領卡照舊（禁令 60）。`/chat/upload` 只放行人類、admin 代打與 Owner／Debugger，其餘回 403 `AUTH_UNAUTHORIZED_ACCESS`。
- 指定靈魂要有權（修補）：`model`、`X-Touka-Soul-ID`、session 連結或身分解析出的目標是靈魂時，人類要擁有它或是 admin，代理只能是靈魂本身或 DebugAgent（admin 經 X-Agent-ID 代打仍算 admin），否則在套人設之前回 403 `AUTH_PERMISSION_DENIED`。群聊每位成員同樣檢查，有一位不行整輪不派發，回串流內錯誤訊息（HTTP 200，和其他群聊錯誤相同）。SNS 內部路徑照舊。SoulLayer 注入前對真的要注入的靈魂再驗一次；同名自訂模型不能換掉已授權的靈魂。`POST /ui/custom_models/{id}` 建立新模型與 `POST /ui/custom_models/{id}/rename` 的新 ID：和靈魂撞名回 409 `MODEL_ALREADY_EXISTS`（ADR-0250 起不分大小寫，也擋和其他自訂模型只差大小寫），含 `/`、反斜線或 `..` 回 422 `VALIDATION_ERROR`。`POST /v1/debug/scorecard/run` 的 `model` 解析成靈魂時，呼叫者也要通過同一條規則，否則 403。沒有新增錯誤碼。
- 代理角色：`Owner`／`Base`／`Subagent`／`Debugger`／`System`（Guest 退役，Subagent 只給 in-process 子代理）。只有靈魂、DebugAgent、DispatchAgent 套用 `identity_map` 的覆寫、類型規則與能力；其他 ID 一律 Base、沒有能力，live 設定的舊值讀取時略過並記 AUDIT 警告、不改檔。舊 `ToukaSoul<靈魂>` 前綴 ID 不算靈魂：token 驗證與 `get_identity` 都解析成 Base、沒有能力。
- X-Agent-ID 只能切進靈魂：人類 token（擁有者或管理員）帶 X-Agent-ID 指向非靈魂 ID 回 403 `AUTH_PERMISSION_DENIED`；管理員切進靈魂仍是 ADR-0141 選項 A。
- `/identity`：`GET /identity` 只回這三種 ID 的覆寫；`GET /identity/agents` 只列這三種 ID（附 `effective_role`／`effective_capabilities`）；`POST /identity` 角色只收 `Owner`／`Base`／`Debugger`，覆寫寫給其他 ID 回 422 `VALIDATION_ERROR`，body 帶 `rejected_agent_ids`；檔內被略過的舊覆寫存檔時原樣保留。
- 群聊：`GET`／`POST /group/whitelist` 只有 `ToukaSoul` 一區（送 `Native`／`Custom` 回 422）；別名只解析到 `agents_registry` 裡經靈魂檢查的成員（白名單值不是靈魂時不退回比對別名本身），群聊與 SNS `@別名` 一律以靈魂身分執行。只驗最新一則 user 訊息的點名；更早歷史裡解析不到的點名略過。

## 模型溝通層第 2b 段：審批管線（ADR-0246）

- 審批事件形狀不變：`tool_approval_request`（`id`＝exec_id、`call_id`、`tool`、`args`、`agent_id`、`risk`、`timeout`）與 `tool_approval_resolved`（`decision`＝`approved`／`rejected`／`timeout`），核准仍走 `/v1/tools/approve/{exec_id}`。`args.command` 改為完整送出，超過 8 KB 才截斷並附 `…[truncated: N more chars not shown]`；其他參數仍截到 200 字。
- 互審驗證指令：CONFIRM 級 `verification_cmd` 在互動會話裡各自跳一張卡，`call_id` 為 `<request_review 的 call_id>:verify:<n>`（n 從 1 起），`tool` 為 `execute_command`、`risk` 為 `dangerous`。核准後同一 `call_id` 依序送 `tool_start` 與 `tool_result`（審查中途失敗為 `error_class=internal_error`）；拒絕、逾時在解算事件之後、佇列滿（不出卡）直接送 `tool_result`（`error_class=permission_denied`），指令不執行。`request_review` 卡本身不再代表指令已核准。
- `request_review` 的驗證指令沒被核准時，回結構化 `permission_denied` JSON：`reason` 為 `human_approval_required`（無人在場，例如心跳或子代理，或卡片逾時、佇列滿）或 `human_rejected_command`（被拒），另帶 `review_status=BLOCKED_FOR_HUMAN`、`review_locked=false`、`evidence`。這一輪不呼叫 critic、不扣次數、不上鎖；往返次數達上限的永鎖仍回一般審查文字。
- 公開 endpoint／schema 沒有改變，不重新生成 OpenAPI。

## 模型溝通層第 3a 段：Claude SDK 1.x 與每模型規則表（ADR-0247）

- Claude 走官方 SDK 1.11，目的地固定為 `config.ANTHROPIC_API_BASE_URL`（`https://api.anthropic.com`），不理會環境變數 `ANTHROPIC_BASE_URL`，也不跟隨轉址；模型發現與健康檢查用同一個目的地。
- Claude 模型名只轉小寫、去掉 `-thinking`，不再把舊簡稱（如 `claude-4-5`）改寫成完整 ID，舊簡稱會原樣送出。`-thinking`：只收預算的型號送 `enabled`＋預設預算 4,000，4.6 以後送 `adaptive`。
- OpenAI 形狀入口的 `max_tokens` 會照用（以前固定 8,192／20,000）；沒給時 4.5 以後 16,000、其他 8,192；超過非串流上限（21,333，Opus 4／4.1 為 8,192）會壓低，不報錯（3b 起只在主迴圈不串流時壓，見第 3b 段）。
- Gemini 模型名原樣送出（只去掉 `models/`）；空名、只有家族名（如 `gemini`）或含 URL 路徑字元時回 `MODEL_NOT_FOUND`。
- Claude 快取：UI 入口的 system 和原生字串入口一樣切出穩定前綴；多則 system 依序合併；新對話第 2 輪起也有訊息斷點。
- 公開 endpoint／schema 沒有改變，不重新生成 OpenAPI。

## 模型溝通層第 3b 段：上游串流、重試、finish_reason、Responses（ADR-0248）

- 主迴圈改成串流接上游：Claude 用 SDK 的 `messages.stream()`；Gemini 與規則表的已知相容廠商（OpenAI、DeepSeek、Grok，看路由的 provider）自寫 SSE；自訂連線、Ollama、LiteLLM 維持非串流。下游 SSE 的形狀不變（golden 每個情境三種切法都和原本相同）；`complete_once` 照舊非串流、只送 1 次。
- 逾時：第一個位元組前 120 秒、之後閒置 60 秒（推測值，待實測）、整輪 600 秒；ping 與註解行也算還活著。上游沒送完整訊號就斷線、200 之後才出錯、壞資料，都回錯誤訊息，下游不會看到半截文字。
- 重試由主迴圈做（四家共用，最多送 3 次）：只重試狀態 429／500／502／503／504／529 與首事件、閒置逾時；整輪上限、400 與其他錯誤直接顯示。每次重試前多一則 `touka_status`，`status` 為「<狀態>, retrying... (attempt N)」（Gemini 第一次仍是「Gemini busy (503), retrying... (attempt 1)」），等待照 `retry-after-ms`／`retry-after`（上限 30 秒），否則 2、4 秒；失敗那次的錯誤不送下游。
- 終端 chunk 的 `finish_reason` 照實回報：截斷 `length`、拒答與安全過濾 `content_filter`，其他維持 `stop`（Claude 原生 `/v1/messages` 出口不動）。拒答多一則正文 delta「⚠️ 模型拒絕回答。」（模型給了拒答文字時附上），安全過濾多一則「⚠️ 回應被廠商的安全過濾擋下。」。
- 沒跑完的一輪（取消、逾時、斷線、重試放棄的那次）：廠商已回報的用量照記，其餘估算；RoundError 時多一則 `token_usage` 事件帶 `estimated: true`，整次 run 有任何一輪是估的，結尾的 `token_usage` 也帶 `estimated: true`。`token_usage.json` 與稽核 CSV 欄位不變。
- OpenAI Responses：只有非自訂連線、provider 是官方 OpenAI、規則表標了 `uses_responses_api` 的模型才走 Responses（目的地固定 `config.OPENAI_RESPONSES_BASE_URL`，不讀 `OPENAI_BASE_URL`）；`store=false`；`complete_once` 對這類模型改用非串流 `responses.create`。其他 OpenAI 模型照舊走 Chat Completions。
- 相容 Chat 不再外送 Touka 內部欄位（`incognito`、`deep_research`、`mcp_dispatcher`、`session_id`、`project_id`、`auth_level`、`ide_context`）、MCP 工具的 `server`／`tool`／`category` 與空的 `tools`（KF-8）。Gemini 送 `generationConfig`，但只有請求的 `maxOutputTokens`／`stopSequences`。
- Anthropic 形狀入口（IDE 的 `/v1/messages`）接非 Claude 模型時，`tool_use`／`tool_result` 轉成 OpenAI 的 `tool_calls`／`role=tool`（Gemini 再轉成 functionCall／functionResponse），list 形狀的 system 合成字串（KF-9）。
- Claude 的非串流上限（21,333，Opus 4／4.1 為 8,192）只在主迴圈不串流送出時套用；串流與 `complete_once` 照請求值。
- 公開 endpoint／schema 沒有改變，不重新生成 OpenAPI。

## 模型溝通層第 2c 段：三種模式、底線與審查員（ADR-0246）

- 模式值：對話存檔（`POST /v1/ui/sessions/{session_id}`）、`/debug/execute`、`/heartbeat/awaken` 的 `auth_level` 只收 `Manual`／`Auto`／`YOLO`，舊名稱回 422。讀取已存的舊值時 `ReadOnly`、`SafeAuto` 與看不懂的值當 `Manual`、`FullYOLO` 當 `YOLO`；快照、清單、ToolLayer、MCP MRTR 都只看到新名稱，下一次存檔自然寫回新名稱。
- 模式授權在對話存檔時判定：存檔後是 Auto／YOLO 的對話，只有 admin 或該靈魂的擁有者能寫；能存取這個對話、但兩者都不是的人，寫入照樣成功、模式存成 `Manual`（前端重讀後顯示）。請求 body 的 `auth_level`（聊天、IDE 帶的）不是模式來源，MonitorLayer 不再處理模式。無痕請求一律 `Manual`，即使帶著已存成 Auto／YOLO 的 session id（ToolLayer 在所有模式來源之後最後套 `enforce_incognito_manual`，子代理執行 ctx 也套）。
- 決策：`AuthPoliciesConfig.evaluate` 先看程式常數底線（F1–F7），再看黑名單、風險分類與三模式表。新效果 `HUMAN_ONLY`（只有人能核准）與 `REVIEW`（Auto 的終端與 dangerous 工具交審查員）。
- 工具錯誤：無人在場遇到 `HUMAN_ONLY` 回 `permission_denied`（`reason=human_only`）；審查員拒絕、無人在場不跑終端、需要人但沒人在場分別帶 `touka_error_code` 1118 `AUTO_REVIEW_DENIED`、1119 `AUTO_REVIEW_HEADLESS_TERMINAL`、1120 `AUTO_REVIEW_UNAVAILABLE`。
- 審批事件：`tool_approval_request` 多兩個選用欄位，`review_reason`（審查員轉人的理由，截到 300 字）與 `human_only: true`（只有人能核准）；沒有新事件種類，核准仍走 `/v1/tools/approve/{exec_id}`，只有人類核准能滿足 `HUMAN_ONLY`。
- 審查員設定：`GET`／`POST /v1/autonomy` 多 `auto_review` 區塊（`model`、`max_tokens`、`timeout_s`、`headless_terminal`；`model` 預設空白＝每次轉人或拒絕）。`auto_review` 有變更時只有人類 admin 能改（其他回 403 `AUTH_ADMIN_REQUIRED`，其他欄位照舊 Owner／Debugger／admin）；新設定的 `model` 必須空白或在 reviewer-models 清單內，否則 422 `VALIDATION_ERROR`（R-2c）。新端點 `GET /v1/autonomy/reviewer-models`（Owner／Debugger／admin）回 `{"models":[{"id","name","provider"}]}`，只列供應商金鑰已設定、啟用中的一般模型。費用經 `complete_once` 記在被審的靈魂、provider 標籤 `auto_reviewer`。
- 心跳：`security_level` 的 `safe`→Auto、`full`→YOLO、其他→Manual；`/retrigger` 固定 Auto。脈衝在 `dispatch_request` 前跑 ToolLayer，請求第一次帶工具（KF-4）。
- `/debug/execute`：YOLO 下遇到底線 F1–F4、F7 指令也拒絕（`reason=human_only`）。
- IDE：只有人類在 Touka 卡片核准時，`call_ide_tool` 才在 tools/call 的 `_meta` 放 `"touka.dev/human-approval"`（HMAC 標記，見 IDE_INTERFACE）。
- OpenAPI 已在沙盒內重生（模式 Literal、`auto_review`、審查員模型清單）。

## 模型溝通層第 4a 段：插話佇列與端點（ADR-0249）

- `POST /v1/ui/sessions/{session_id}/interjections?stream_id=…`：body 只有 `text`（必填、不可全空白、最多 4,000 字）與 `urgent`（預設 false；給 Ctrl+Enter），多送任何欄位回 422 `VALIDATION_ERROR`。成功 201，回一則 `Interjection`：`id`、`text`、`urgent`、`status`、`step`（還沒交給模型時為 null）、`created_at`（epoch 秒）。只能加到**進行中**的同一串流代。
- `GET /v1/ui/sessions/{session_id}/interjections?stream_id=…`：回 `{"interjections":[…]}`，照加入順序；收回的不列。
- `DELETE /v1/ui/sessions/{session_id}/interjections/{interjection_id}?stream_id=…`：只收回 `queued` 的插話，回該則（`status: "retracted"`）。
- 狀態：`queued` → `pending`（交給模型，帶 `step`）→ `delivered`；或 → `undelivered`、`retracted`。queued 超過 600 秒改成 `undelivered`，不再交給模型。
- 權限：只收人類呼叫者；admin 經 X-Agent-ID 代打可以，其他代理身分（非 admin 的 X-Agent-ID 代打、靈魂 token、DebugAgent）回 403 `AUTH_PERMISSION_DENIED`。接著沿用停止端點的 `_authorized_stream_state`：要是這個串流的發起者、能存取這個對話（擁有者或 admin），且 `stream_id` 是目前這一代，否則 403 或 404 `STREAM_NOT_FOUND`（缺 `stream_id` 回 422）。
- 上限與錯誤碼：每個串流最多 10 則（含已送達，收回的不算），超過回 429 `INTERJECTION_QUEUE_FULL`（1216）；超過 4,000 字回 422 `INTERJECTION_TOO_LONG`（1217）；別的串流或對話的 id 回 404 `INTERJECTION_NOT_FOUND`（1218）；已交給模型的收回回 409 `INTERJECTION_NOT_RETRACTABLE`（1219）。
- 生命週期：佇列只放記憶體，重啟就清；串流結束（完成、錯誤、停止、刪除退休、逾時收割）或同一對話開新串流時清空。無痕對話也可以插話，同樣不存檔。
- 取出與送達：AI 在下一個工具步驟之間收到插話（`append_tool_results` 之後，只在 UI 對話、串流進行中）；下一輪送出成功後串流內送 `interjection_delivered`，回合結束時沒送達的送一次 `interjection_undelivered`（格式見 CHAT_INTERFACE）；urgent 讓下一輪 `tool_choice=none`，送出後還原。
- 存檔：UI 存檔的助理節點可能帶 `interjections[]`（`Interjection` 形狀、只有已送達的）；沒有插話時節點不變。心跳脈衝、家對話注入與滑動視窗摘要組給模型的歷史，會在該節點 content 前加標註（ADR-0249）。
- 第 4b 段：urgent 插話在上游串流接收中直接打斷那一輪（取消、丟掉半截、不重試、不算一步、計費照廠商回報或估算），串流內送 `round_interrupted_by_user`（`step`），接著用打斷範本送出插話。佇列 Protocol 多 `urgent_signal`；端點與 openapi 不變。
- OpenAPI 已在沙盒內重生。

## Touka 待排小任務：靈魂主要模型、模型名大小寫、沒設模型（ADR-0250）

- `POST /v1/souls/{soul_id}`（`update_soul`）存檔規則（認證、擁有權與步升密碼不變，成功回應形狀不變）：
  - 寫入前先讀已存的 `config.base_model`（你指定的）與 active brain（目前在用的）。新值的 `null`、空字串、只有空白都當沒指定，`config.json` 存成 `null`。
  - 換主要模型的條件全部成立才換：請求者是人類（只看已驗證的 `AgentIdentity.is_human`，admin 經 X-Agent-ID 代打不算）、新值有指定、新值和已存的不同或目前沒有主要模型。要換時新值必須通過 `is_selectable_brain`（模型清單裡的一般模型，或啟用中的自訂連線），否則回 404 `MODEL_NOT_FOUND`（1202），`config.json` 與 `soul.md` 都不寫；只差大小寫的兩個自訂連線模型都在、寫法又都對不上時回 409 `MODEL_NAME_AMBIGUOUS`（1220），同樣都不寫。
  - 其他情況照存、不驗、不動主要模型：代理人存檔、沒指定、值沒變而且已有主要模型（舊的無效值不擋人格存檔）。
- `GET /v1/souls`：每筆的 `active_brain` 沒設或讀取出錯時是 `null`（以前回 `"未配置"`、`"錯誤"` 字串，畫面現在自己翻譯）；存成舊 `toukasoul` 路由 ID 的主要模型當沒設。路由沒有 response_model，OpenAPI 不變。
- 模型名查表：靈魂的主要模型名一律經 `ModelManager.request_model_name` 轉成請求用的名字（靈魂聊天、群聊、子代理、心跳脈衝都一樣）。自訂連線（啟用或已知）先找寫法完全相同的，沒有才不分大小寫找，只有一個符合就送原名；兩個以上回 409 `MODEL_NAME_AMBIGUOUS`，聊天在串流開始前回，不派發。不是自訂連線的名字照舊轉小寫。
- 自訂模型（L2）`POST /ui/custom_models/{id}` 建立新模型與 `POST /ui/custom_models/{id}/rename` 的新 ID：和靈魂 ID 不分大小寫相同（`@名稱`、`toukasoul` 前綴的寫法也算），或和其他自訂模型只差大小寫，回 409 `MODEL_ALREADY_EXISTS`（Windows 與 Linux 結果相同）；改名時排除被改名的自己（只改大小寫的自我改名不擋）；已經存在的資料不動。
- 群聊：每位成員都有權之後，再檢查每位成員的主要模型；有一位沒設或名字模糊，整輪不派發，回串流內錯誤訊息（HTTP 200，和其他群聊錯誤相同）並點名該成員，伺服器記 warning（錯誤碼名稱與成員 ID）。
- 心跳 `POST /v1/heartbeat/awaken`：沒有 `model_override` 而且靈魂沒有主要模型時回 409 `AGENT_BRAIN_NOT_SET`（1215），不啟動背景迴圈；探針請求照舊在這之前就回。背景迴圈每次脈衝前解析模型，拿不到（喚醒後主要模型被清掉、經 `/retrigger` 進來或名字模糊）就記 log、結束這次 run，不丟例外、不寫進對話。
- 用量：Gemini 的輸出 token＝`candidatesTokenCount`＋`thoughtsTokenCount`，OpenAI 形狀回應的 `completion_tokens` 也是；OpenAI 相容的快取讀先取 `prompt_tokens_details.cached_tokens`，沒有才用 DeepSeek 頂層的 `prompt_cache_hit_tokens`，兩者不相加。成本上限照這個數字判斷。
- 研究工具：`source="google"` 沒有模型時回結構化 `validation_error`（`param=model`，禁令 59），工具 schema 與 handler 簽章不變。
- OpenAPI：只有 `AnthropicThinking` 的兩個元件（`src__adapters__schemas__AnthropicThinking__1`／`__2`）合成一個 `AnthropicThinking`，`$ref` 跟著改；端點不變。重生腳本 `scripts/update_openapi.py` 先開沙盒再載入產品程式，合併函式在 `scripts/openapi_merge.py`。
- Proxy 內走不到的排程引擎已刪（`start_engine` 那條路徑）；Proxy 照舊不做主動排程。沒有掛載的排程 CRUD（`routers/scheduler.py`、`SchedulerService`）與 `ENABLE_SCHEDULER` 也已刪除（ADR-0254，見下一節）。

## 隔離第一批（iso-c1）：小件修補與執行路徑缺口（ADR-0254）

- 新端點 `GET /v1/system/restore/status`（根路徑 `/system/restore/status` 對等）：admin 限定（`require_admin_authority`，登記在 Guard #24 的 admin 清單），唯讀。回 `RestoreStatus`：`state` 為 `idle`（這次啟動後沒有還原紀錄）、`running`（還原進行中）或 `failed`（還原失敗），`error_code` 只在失敗時有值，目前是 `SYSTEM_BACKUP_FAILED`（1303），其他時候 `null`；不帶路徑或例外原文。沒登入 401 `AUTH_INVALID_TOKEN`，不是 admin 403 `AUTH_ADMIN_REQUIRED`。紀錄只在記憶體：`POST /v1/system/restore/run` 接受時記 `running`（蓋掉上一次的失敗；回應形狀不變），背景還原丟例外時記 `failed`；成功時進程結束重啟，新進程回 `idle`。OpenAPI 已重生。
- `POST /v1/souls`（建立靈魂）：`config.base_model` 比照存檔（ADR-0250）先正規化，`null`、空字串、只有空白都當沒指定、`config.json` 存 `null`，有值時去掉前後空白後寫回。有指定時在建任何資料夾之前先驗：不在模型清單裡（`is_selectable_brain` 不過）回 404 `MODEL_NOT_FOUND`（1202），只差大小寫的兩個自訂連線都在、寫法又對不上時回 409 `MODEL_NAME_AMBIGUOUS`（1220）；兩者都不建資料夾、不註冊、不加擁有權。驗模型排在人類 ID 撞名與 409 `SOUL_ALREADY_EXISTS` 之後。可選或沒指定照舊建立（可選時設成主要模型），成功回應不變。
- `POST /ui/custom_models/{id}/rename`（含 `/v1`）：搬資料夾失敗不再回 200。來源不見 404 `MODEL_NOT_FOUND`（1202）、新 ID 已被占用 409 `MODEL_ALREADY_EXISTS`（1403）、其他失敗（沙箱檢查不過、搬移出錯）500 `SYSTEM_ERROR`（1000）；三種都不改 `config.json`、不搬擁有權。只差大小寫的改名（Windows 上是同一個資料夾）現在會真的改名並搬擁有權。成功回應照舊 `{"status":"success","old_id","new_id"}`；撞名檢查（ADR-0250 A5）不變。
- `POST /v1/admin/setup/apply`：`dispatch_token` 或 `auth_secret` 是歷來公開過的值（`config.PUBLIC_SECRET_VALUES`，比對前去掉前後空白）時回 422 `VALIDATION_ERROR`（3001），`.env` 不寫任何欄位；訊息只寫欄位名、不帶值。非公開值照舊寫入。
- 演化工具：`evolution_implement_and_verify` 不再是自帶審批，照一般工具過政策。Manual 有人在場時先送一張一般的 `tool_approval_request`（很長的參數可能只預覽前段），核准才建 venv、pip、驗證；無人在場時 executor 回 `permission_denied`（`reason=approval_required`）、什麼都不跑。`submit_for_deploy=True` 時部署前仍有 `skill_creation_approval_request` 卡（無人在場照舊 fail-closed）；Auto、YOLO 行為不變。自帶審批工具剩 `request_skill_install`、`request_mcp_creation`。
- MCP stdio 伺服器的啟動環境＝`LAUNCH_ENV_ALLOWLIST`（`protocols/mcp/environment.py`）內的父環境變數＋條目自己的 `env`（解析哨兵）＋`TOOL_BASE_URL`；`PATH`、`HOME`、`SYSTEMROOT` 一類由 MCP SDK 照舊帶。不再繼承 Touka 與廠商金鑰，也不再拿到 `PYTHONPATH`、`HF_HOME`、`HTTP_PROXY`／`HTTPS_PROXY`、Windows 的 `HOME`；需要的值寫進 `POST /mcp/ui/config` 條目的 `env`。創建引擎試跑用同一個白名單＋spec 宣告的 `env`（不解析哨兵）。
- 終端自我修復：`execute_command` 與 `get_command_result` 的結果帶非 0 `[EXIT_CODE: N]` 時，回給模型的工具結果後面附修復提示，串流送 `repair` 進度事件（`attempt`、`max_attempts`）；0 時重置次數。同一個 session 的同一個 `execution_id` 只計一次。只是提示文字，下一次執行照常過政策。
- 互審驗證指令的審批卡（`call_id` 為 `<request_review 的 call_id>:verify:<n>`）：Auto 審查員轉人時帶 `review_reason`，底線（`HUMAN_ONLY`）的指令帶 `human_only: true`，欄位和第 2c 段的終端卡相同。
- Anthropic 形狀入口（`/v1/messages`）接非 Claude 模型時，呼叫端明確給的 `max_tokens` 會帶過去（Gemini 是 `generationConfig.maxOutputTokens`）；沒給時照舊不帶。仍不送取樣參數。內部呼叫端照舊：子代理不帶上限（Claude 用預設 8192），評分卡（`POST /v1/debug/scorecard/run`）的情境上限只給 Claude。L2 自訂模型設定的 `max_tokens` 在這個入口也會帶給非 Claude 的基礎模型（和 openai 協定一致）。
- 設定：`ENABLE_SCHEDULER` 從 Settings 與 `ToukaProxy/.env.example` 刪除；舊 `.env` 還留著這個鍵時照常載入（`extra="ignore"`）。

## 記憶系統第二批段 6：系統快照與刪除靈魂（ADR-0262）

- `DELETE /v1/souls/{soul_id}`：檢查順序為 admin 步升（人類 admin＋密碼）→ DebugAgent 不可刪 → 路徑監獄 → **記憶檢查** → 清除三目錄與註冊。事實庫該 ID 任何狀態的列（含已封存、待補索引）、向量點、或向量 collection 還在（空的也算）時回 409 `SOUL_MEMORY_NOT_RESET`（1408），`detail` 另帶 `facts`、`vectors`、`vector_collection`，什麼都不刪；先用 `POST /v1/admin/agents/{id}/reset` 重置記憶再刪。記憶系統不可用、無法確認時回 503 `SOUL_MEMORY_CHECK_FAILED`（1409），不刪、不帶例外原文。步升沒過照舊 403，不先透露有沒有記憶。成功回應不變。
- 系統快照（`POST /v1/system/backup/run`、排程、重置前、設定匯入前）與還原（`POST /v1/system/restore/run`）：
  - 腳本 `system_backup.backup_script()`＝`ToukaProxy/scripts/manage_backup.py`（以前指向不存在的 `<repo>/scripts/`，快照與重置一律失敗）。
  - 快照目錄 `system_backup.backup_dir()`＝`resolve_env_file_path()` 所在資料夾的 `backups`；`GET /v1/system/backups`、`/backups/stats`、下載、刪除、還原與排程清理都用它。本機整合模式＝repo 根的 `backups`；容器裡＝`/app/host_root/backups`（宿主 compose 目錄的 `./backups`）；本機獨立模式＝`ToukaProxy/backups`。
  - Proxy 傳 `--data-root`、`--env-file`、`--config-dir`、`--output-dir` 給腳本（快照與還原同一組），子程序環境用 `utils/child_env.child_python_env()`；資料根不存在時快照失敗（`SYSTEM_BACKUP_FAILED`），重置因此中止、什麼都不刪。
  - 檔名 `{prefix}_YYYYmmdd_HHMMSS.zip`，同一秒撞名加 `_2`、`_3`…，絕不覆蓋既有快照；`core_memory.db` 與 Qdrant 各 collection 的 `storage.sqlite` 是 SQLite backup API 取的一致複本，不含 journal 類檔。
  - `SYSTEM_BACKUP_FAILED` 只帶錯誤碼，不再有 `error_output`（腳本 stderr 消毒後只寫伺服器 log，禁令 23）。
- 前端：重置卡收到 `SYSTEM_BACKUP_FAILED` 時顯示「系統快照沒有做成，所以這次重置沒有執行，什麼都沒刪。你的操作沒有錯…」（`settings.agent_reset.backup_failed`）；刪除靈魂被擋時 toast 顯示 `error.soul.memory_not_reset` 的引導。
- OpenAPI 不變（端點與模型都沒改）。

## Claude 工具定義：根層組合子（ADR-0266）

- 送給 Claude 的每個工具 `input_schema`：根層不帶 `oneOf`／`anyOf`／`allOf`（各分支的 `properties` 併進根層，同名以根層為準；`required` 只留根層原有的；其他根層鍵如 `$defs` 照留；根層 `type` 一律 `object`），內層照留。OpenAI 形狀與 Anthropic 形狀（含 `/v1/messages` 帶來的工具、外部 MCP 工具）都一樣；沒有 `input_schema` 的伺服器工具原樣送出。Gemini、OpenAI 相容、Responses 送出的工具定義不變；執行前的參數檢查不變。端點與 OpenAPI 不變。

## 記憶系統第二批段 4：可信活動時間軸與時間感（ADR-0267）

- **事件表**：`ui_sessions.db` 的 `continuity_events`（`init_db_async` 建，索引 `(agent_id, kind, occurred_at)`），讀寫只在 `utils/continuity_events_db.py`。只存 id、計數與固定列舉值；`trust` 寫死 `'system'`、寫入介面沒有這個參數；時間是 epoch 毫秒。14 種事件與 event_id 規則見 ADR-0267。沒有任何端點或工具能寫這張表。
- **記錄器**：`services/continuity_events.ContinuityEventRecorder`，`main.py` 啟動時綁定主事件迴圈並登記給 Memory、關機時最先解除。呼叫同步、不阻塞；在主迴圈裡依呼叫順序用鎖逐筆寫，其他執行緒（含短命迴圈）排回主迴圈；寫失敗只記 warning、不重試，關機時排隊中的寫入可能丟。只記正式靈魂；無痕、子代理、派工不記。
- **掛點**（都在事情做成之後）：SoulLayer 的 user_turn（人類送來、已保存、屬於這個靈魂、不是心跳）；心跳迴圈 in-flight 標記寫入後的開始與 `finally` 的結束（脈衝數、輪數、結束原因）；`mission_tools` 的 initialize、update_status（同一任務同一狀態不另記；看板全部完成另記）、finalize；記憶閘道用 `events_scope` 擋住無痕的記憶事件。跨看板（改 DispatchAgent 看板）記在動手的靈魂名下，metadata 帶 `board_owner`。
- **`<continuity>` v1**：前景對話（SoulLayer）與心跳（`_execute_single_pulse`，工具層之後、只給靈魂、心跳模式）都附在送出的最後一則 user 尾端，不寫回對話樹。每行標來源：現在時間（伺服器時鐘）、這段對話上一輪（記憶體 → user_turn → 對話樹，對話樹時間標成可能來自使用者裝置；心跳不顯示這行）、上次人類訊息、上次自主執行（只算有結束紀錄的）、最近完成（7 天、最多 3 筆）、任務看板，以及只在有「沒有紀錄」時出現的可信紀錄起點。讀不到事件表時照實寫 unavailable，不丟例外。心跳的 nudge 前綴格式不變，時間和區塊同一個值。
- `POST /v1/admin/agents/{id}/reset` 與 DebugAgent 重置：`stores` 多一列 `timeline`（屬「記憶」類別），清除時同一個交易寫這個靈魂的可信紀錄起點。
- `DELETE /v1/souls/{soul_id}`：記憶檢查之後再查活動事件；還有事件時 409 `SOUL_MEMORY_NOT_RESET`，`detail` 帶 `activity_events`，什麼都不刪；讀不到時 503 `SOUL_MEMORY_CHECK_FAILED`。順序：步升 → DebugAgent 不可刪 → 路徑監獄 → 記憶檢查 → 活動紀錄檢查 → 清除。
- `POST /v1/auth/export-my-data`：每個自有 agent 多 `agents/<id>/activity_timeline.json`；manifest `counts.activity_events`、`included` 加 `agent_activity_timeline`；讀不到時列在 `skipped`（`activity_timeline_unavailable`）。
- OpenAPI、錯誤碼、i18n 都不變。

## 只執行這一輪列給模型的工具（ADR-0269）

- 範圍：所有經 `run_react_loop` 的模型迴圈（前景 UI／IDE、`/v1/messages`、`/v1/chat/completions`、SNS、群聊、心跳 awaken／retrigger、子代理與子代理群組、`POST /v1/debug/scorecard/run`）。模型叫的工具必須在這一輪實際列給它的清單上（ToolLayer 去重後注入的伺服器端工具；請求自帶的同名 tools 蓋掉的伺服器端工具不算）。
- 核對排在原生角色權限（`native_tool_rbac`）之後、MCP 軸 B／PaC／審查員／審批卡之前，三種結果都不叫審查員、不送 `tool_approval_request`、不執行，YOLO 也一樣；串流送一則 `tool_result`（`status` 為錯誤、帶 `error_class`），工具結果是結構化錯誤 JSON（禁令 59）：

  | 情況 | `error_class` | `reason` |
  |---|---|---|
  | 完全找不到（不是 builtin／memory／plugin，解析出的 MCP 伺服器不在 registry、清單上也沒有任何一對用這台） | `resource_not_found` | （無） |
  | 這一輪的清單不可用（缺失或型別不對；接線問題，伺服器記 error log） | `internal_error` | `advertised_tools_unavailable` |
  | 這一輪沒列出 | `permission_denied` | `not_advertised` |
  | 列出了，但這是使用者各自授權（`oauth_user`）的 MCP，而這次不是人在場、和靈魂的前景對話（心跳、SNS、群聊成員、子代理、派工等；ADR-0281 T1 的第二層防線，排在審查員與審批卡之前、不寫同意紀錄） | `permission_denied` | `user_scoped_mcp_unavailable` |

  `internal_error`／`permission_denied` 兩種另帶 `denied_tool`（模型叫的名稱）與 `suggestion`。
- 舊名工具（`bash`、`vault_tools`、`project_tools`、`update_my_soul`、`sns_control` 與四個演化舊名）：它的後繼正式工具**全部**在這一輪清單上才放行，不看參數裡的 action（對應表 `tools/builtin/__init__.py` 的 `LEGACY_ADVERTISED_SUCCESSORS`）。舊名各自的角色權限照舊。
- MCP：核對（伺服器, 裸工具名）這一對。`connect_mcp_server` 掛上的伺服器，下一輪對話才列出（背景執行是下一次脈衝），同一輪叫回 `not_advertised`。伺服器當下沒連上時同樣回 `not_advertised`，不會現場連線。
- 使用者各自授權的 MCP（ADR-0281 T1）：只在人在場、和靈魂的前景對話時，ToolLayer 為 `oauth_user` 的伺服器帶身分列出（先做只讀的授權查詢，沒授權不連；第一次連線最多等 5 秒，逾時這一輪不列；連線還在跑或失敗後 60 秒內這一輪直接略過）；其他情況、`oauth_client_credentials` 與 enterprise 一律不列。還沒連線的共享 MCP 不列成工具，名字寫在每輪會變動的 `<mcp_unavailable>` 區塊。
- 靈魂與自訂模型的工具清單（`enabled_tools`，ADR-0281 T2）：比對只用 `utils/soul_tool_selection.py`。內建與記憶工具比名稱；MCP 只認恰好一個斜線的 `伺服器/工具`，比（伺服器, 工具）這一對；舊長名 `伺服器__工具` 照舊比廣告名；舊短名、只寫伺服器名、含萬用字元、兩個以上斜線都不生效（舊短名只在靈魂自己的執行檢查，每個靈魂記一次警告）。外掛（plugin）照舊不列。`/v1/tools_stats` 的 MCP 條目附加 `server`、`tool` 欄位給畫面存檔用。
- 子代理：用自己組裝出的清單，不沿用父代理；`dispatch_subagent` 與 `dispatch_subagent_group` 從請求與清單都拿掉。
- 成對的工具一起開：`execute_command` 的非同步流程會叫模型之後呼叫 `get_command_result` 取結果（`tools/terminal.py`）。工具清單勾了 `execute_command` 而且它真的列出時，`get_command_result`、`stop_command` 一起列出（ADR-0281 T2 的 `TOOL_COMPANIONS`）；它們照樣先經過政策與角色權限，舊名 `bash` 不帶。`/v1/tools_stats` 的 `tool_companions` 給畫面標示。
- `/v1/debug/execute`：人按的執行，不核對清單（行為不變）。
- 模型迴圈建立的執行器若沒開核對，`run_react_loop` 在送出上游之前以 `ToukaException(SYSTEM_ERROR)`（1000）中止，不執行任何工具。
- 端點、請求／回應 schema 與 OpenAPI 不變；沒有新錯誤碼。

## 記憶系統第二批段 7：清除連備份（ADR-0270）

- `POST /v1/admin/agents/{target}/reset`（根路徑對等）：
  - 請求 `AgentResetRequest`：`confirm`、`confirm_agent_id`、`current_password`，新增 `scrub_backups: StrictBool = false`、`scrub_acknowledged: StrictBool = false`；`extra="forbid"`（多送或打錯欄位 422）。**預設不勾選時，重置仍保留備份裡的記憶**。
  - 檢查順序：Origin → 字面確認 → 勾選時 `require_admin_human`（代打 403，不驗密碼）與第二段確認（422 `MEMORY_SCRUB_NOT_ACKNOWLEDGED`，1410）→ 還原進行中（409 `SYSTEM_RESTORE_IN_PROGRESS`，1414，勾不勾都擋）→ 密碼（403 `AUTH_REAUTH_FAILED`）→ 重置。重置或清除進行中 409 `AGENT_RESET_IN_PROGRESS`（1415，取代原本寫死中文的 422）。勾選時協調器再確認一次人類 admin；預檢在工作執行緒讀事實庫：記憶系統不能用 503 `MEMORY_SCRUB_UNAVAILABLE`（1412），撞名 409 `MEMORY_SCRUB_COLLECTION_SHARED`（1411，`detail.collides_with`）；這些都在備份與重置之前，什麼都不刪。
  - 回應 `AgentResetResponse`：`agent_id`、`backup`（`BackupResult`：`status`、`message`、`filename`）、`clean`、`stores`、`retained`、`scrub`（`MemoryScrubReport | null`）。清除有跑時 `retained` 不再列升級備份。
  - `MemoryScrubReport`：`requested`、`ran`、`complete`、`reason`、`categories`（`leftovers`、`live`、`upgrade_backups`、`snapshots`，各 `done`／`failed`／`not_started`）、`items`（`kind`、`name`（只有檔名）、`status`、`reason`、`facts_removed`、`collection_removed`、`members_dropped`、`count`）、`own_snapshot`、`memory_remaining`（`null` 表示重查失敗）、`leftovers_removed`。`complete` 與 `reason` 的規則見 ADR-0270 決策 8（重置清掉的記憶大於 0 時，重置前做的那份快照要真的清掉東西，否則 `own_snapshot_unverified`）。29 個原因代碼。
  - 清除開始後任何意外都回 200 加 `complete=false` 的報告；每一種結局都寫一行稽核（`AUDIT: memory-scrub rejected …` 或 `AUDIT: memory-scrub result …`，不帶內容與路徑、檔名 repr）。
- `POST /v1/system/restore/run`：清除進行中 409 `MEMORY_SCRUB_IN_PROGRESS`（1413）；重置進行中 409 `AGENT_RESET_IN_PROGRESS`；只接受清單上的檔名。
- `POST /v1/debug/reset`（DebugAgent）：還原進行中 409 `SYSTEM_RESTORE_IN_PROGRESS`。DebugAgent 重置不能勾清除（請求模型沒有那兩個欄位）。
- 快照清單規則（`system_backup.list_snapshot_files`／`is_listed_snapshot`）：頂層、`lstat` 判為一般檔（連結、junction 等 reparse point 不算）、不是點開頭、名稱不含 `:`、副檔名不分大小寫是 `.zip`。`GET /v1/system/backups`、`/backups/stats`、`POST /v1/system/backups/{filename}/download`、`DELETE /v1/system/backups/{filename}`、還原、排程修剪、清除都照這個規則；名稱不在清單上 404 `SYSTEM_BACKUP_NOT_FOUND`。**快照目錄（`backups`）本身是連結或 junction 時清單是空的**（不跟進去；清除記失敗）。刪除經備份執行鎖（清除或備份進行中要等），稽核在工作裡寫、檔名 repr。
- `POST /v1/system/backup/run`：回應 `{"status","message","filename"}`，拿掉 `output`（腳本輸出含絕對路徑）；`filename` 是這次新增的快照檔名（前後清單比對，不明時 `null`）；前後列目錄失敗也回 `SYSTEM_BACKUP_FAILED`。設定匯入前快照的 `pre_import_backup` 同形狀。
- 建立靈魂（`POST /v1/souls`）與自訂模型（`POST /v1/ui/custom_models/{model_id}` 新建、`POST /v1/ui/custom_models/{model_id}/rename`）：新 ID 和既有 ID 共用向量 collection（`a-b`／`a_b`、只差大小寫）時 409 `AGENT_ID_COLLECTION_COLLISION`（1416，`detail.collides_with`）；讀不到事實庫或 registry 時 503 `AGENT_ID_CHECK_FAILED`（1417）。**改名時舊 ID 的記憶照算撞名**（只從名稱來源排除舊 ID）。檢查在工作執行緒裡跑；建立靈魂時排在撞名人類 ID 之後、`SOUL_ALREADY_EXISTS` 之前。
- `DELETE /v1/souls/{soul_id}` 的記憶檢查（ADR-0262）：撞名時只數不屬於別人任何事實的點、不看共用 collection 在不在。
- 前端重置卡：閘道逾時（502／504／524）、沒有 Touka 錯誤碼的錯誤回應、斷線都顯示「結果不明」（伺服器可能照樣跑完）；伺服器自己的錯誤碼照寫拒絕原因。nginx 範例只有重置端點的讀取逾時是 30 分鐘，其他 REST（含其他 admin）仍是預設 60 秒。
- 錯誤碼 1410～1417 新增；openapi、`api.ts`、`error_codes.ts` 已重產。

## Claude 5.5 依用途少想、送前最壞花費（ADR-0271）

- 請求標頭 `X-Touka-Purpose`（`/messages`、`/chat/completions`，含 `/v1`）：只認 `inline-completion`（ASCII、不分大小寫、去頭尾空格與 Tab），效果只是讓 Claude 5.5 少想（同 `complete_once` 的 `inline_completion` 用途）；其他值一律忽略、不報錯。不影響路由、工具、權限、模式、身分、花費上限；沒登入只帶標頭照樣 401 `AUTH_INVALID_TOKEN`。子代理與群聊會繼承這一輪的少想。
- `complete_once` 用途→思考強度：`judge`、`summarizer`、`inline_completion` 少想；`auto_reviewer`、`critic` 照模型預設、送出內容不變。少想＝Haiku 5.5 送 `thinking: {"type":"disabled"}`、Sonnet 5.5 送 `between_tools`（effort ≤ high 時），三型號都送 `output_config.effort: "low"`；思考真的關掉時輸出上限照請求值（不拉到 8,192），Opus 5.5 仍 8,192。4.x、5.0、5.6 以後、Fable、Mythos 不變。有 `response_schema` 時 `output_config` 同時帶 effort 與 format。
- 原生 `POST /v1/messages`：新欄位 `output_config`（只轉送 `effort`，字串 ≤16 字，只給 5.5 三型號；其他鍵與不收的等級丟掉並記 warning，不 422）；`thinking.disabled` 給 Opus 5.5 不送、給 Sonnet 5.5 改 `between_tools`（xhigh／max 不送）、給 Haiku 5.5 照送（xhigh／max 不送）；5.x、Fable、Mythos 的 `max_tokens` 照實際值補到 8,192（含沒標 fields_set 的計分卡情境），`enabled`＋budget 補到 budget＋1,024，思考真的關掉時照請求值。原生 `stop_sequences` 仍不轉送。
- OpenAI 格式送 Claude：`stop` 以 `stop_sequences` 送出（丟掉空字串與純空白、保序去重、空就不送）。以前 Claude 路徑把它丟掉。
- `complete_once` 送前另估最壞花費（輸出上限用滿＋估計輸入，實送欄位的上限），花費上限有開且已花＋最壞 > 上限時回 `over_budget`（沒送、不記帳）；剛好等於照送。
- 價目：Haiku 5.5 每個請求依含快取讀寫的提示長度分兩檔（≤10 萬 $0.10／$0.50，超過 $0.50／$2.50）；Sonnet 5.5 快取命中 $0.10。`token_usage` 事件的 `cost` 與稽核 CSV 的整次 run 金額改為每輪分檔相加。`model_pricing.json` 覆寫仍是單一費率（覆寫 Haiku 5.5 就不分檔）。
- `AutoReviewConfig.timeout_s` 預設 60（範圍 >0、≤120 不變；逾時仍是轉人／拒絕）。openapi 新增 `AnthropicOutputConfig` 與 `AnthropicRequest.output_config`，`timeout_s` default 改 60；`api.ts` 已重產；沒有新錯誤碼。

## 隔離第二批（iso-c2）第一段：代理人指令的隔離（ADR-0272）

- 範圍：終端（`execute_command`／`bash`）、互審驗證指令、演化的建置與驗證，spawn 前才經 `services/isolation` 的 runner 改寫成 `python -I -S <kernel>/launcher.py --spec-fd N -- argv`（人核准「未隔離」時照原樣）。ADR-0272 的原範圍不含 MCP；C2-3 已接上 MCP 伺服器與創建試跑，見本文前面的專節。IDE 終端與 Dispatch `auto_invoke` 仍不在範圍。這一段沒有新端點；執行器與 `GET /isolation` 見下一節「第二段」。
- 政策：`PolicyContext.environment` 多 `isolation`（政策當下的層級）與 `human_present`，由 executor 依身分、ctx 放入，模型參數改不動。要隔離的動作在層級 `none`（缺值、看不懂也算）時，Auto、YOLO 都是 `HUMAN_ONLY`；YOLO 下 dangerous 的 MCP 工具在無人在場時也是 `HUMAN_ONLY`。F5 多 `write_mcp_implementation`、`verify_mcp_locally`。審查員的 `<environment>` 多 `isolation`（只供參考）：只有經 runner 的內建工具給政策層級，其餘（含所有 MCP 工具）一律 `none`，決策表的層級不變。隔離判定只看內建工具，MCP 工具和隔離動作同名也取全域層級、不記同意帳本。
- 審批事件 `tool_approval_request` 多選用欄位 `isolation`，只有政策時記過層級的呼叫（終端、驗證指令、要隔離的工具）才帶，其他卡不變；目前沒有未接 runner 的工具。只有未接 runner 的工具才依工具固定 `level: none`、`fallback: direct`、`features: []`（核准的 `isolation_ack` 只收 `direct`；帳本作廢或過期時核准不執行）；創建卡依帳本層級，scaffold 的 features 來自本機判定：
  - `level`：`none`／`landlock`／`executor`；
  - `fallback`：只在 `level` 是 `none` 時有，`landlock`（核准後在 Touka 容器內以 Landlock 執行）或 `direct`（未隔離）。`landlock` 要本機 Landlock 可用（ABI≥3）、暫存區可用、kernel 雜湊一致、需要的路徑都能授權；`kernel_outdated` 或讀不到 kernel（`isolation_check_failed`）時一律 `direct`，因為 `/opt` 的舊啟動器會以 `spec_invalid` 拒絕新的 spec 鍵；
  - `features`：排序過的名稱清單（`fs`、`tcp`、`ioctl`、`signal`、`abstract_unix`）；`none` 時是降級去處的，`direct` 是空清單；網路設定不安全（`net_tcp_fastopen`、`net_mptcp`）時不含 `tcp`，但啟動器仍照 spec 套用封鎖埠的 TCP 規則；
  - `reason`、`reason_detail`：政策當下的原因碼；`reason_detail` 只在原因和目前狀態相同時帶（例如 `not_writable`）。
- 核准 `level: none` 的卡：`POST /v1/tools/approve/{exec_id}` 的 body 必須是 `{"payload": {"isolation_ack": "<卡上的 fallback>"}}`。缺少或不符回 422 `VALIDATION_ERROR`（3001，`Interaction payload does not match its schema.`），卡片仍待決、不記同意。`landlock` 卡與沒有 `isolation` 的卡不需要；拒絕照舊。繞過端點、ack 又不符時，工具不執行，模型拿到 `permission_denied`、`reason: approval_isolation_unacknowledged`。
- 同意綁定被核准的參數（正規化後的 sha256）、只能用一次、10 分鐘過期。拿不到 runner 時不執行，回 `upstream_error`、`reason: isolation_unavailable`、`isolation_reason: <原因碼>`（暫存區沒設或寫不進去時，同意是 `direct` 照常直接執行）；驗證指令回 `requires an isolated execution environment, but none is available`。
- 原因碼：

| 代碼 | 意思 | 出現在 |
|---|---|---|
| `native_platform` | 不是在 Linux 上執行 | 卡片、工具錯誤 |
| `probe_pending` | 剛啟動，自測還沒有結果 | 同上 |
| `executor_unreachable` | 預期是執行器（設了 `TOUKA_EXECUTOR_DIR`）卻連不上；其他執行器原因見下一節 | 同上 |
| `kernel_not_installed` | 映像裡沒有啟動器（舊映像） | 同上 |
| `kernel_outdated` | 映像裡的啟動器和原始碼不同（改了沒重建）；核准後一律未隔離（`fallback: direct`） | 同上 |
| `landlock_unavailable` | 核心沒有開 Landlock | 同上 |
| `landlock_abi_too_old` | ABI 小於 3 | 同上 |
| `landlock_abi_insufficient` | ABI 3，擋不住 TCP | 同上 |
| `fs_9p` | data 所在的檔案系統上自測失敗（每 10 分鐘重測） | 同上 |
| `selftest_failed` | 其他自測失敗 | 同上 |
| `net_tcp_fastopen`、`net_mptcp` | 自測讀到 TCP Fast Open 開著（讀不到也算）、MPTCP 開著：Landlock 的 TCP 規則擋不住這些連線（ABI≥4 才讀）。比照 ABI 3：自測照樣跑完檔案、bind 與一般 TCP 的檢查，這件事另外回報在自測結果的 `net`；本機 Landlock 仍可用但 features 不含 `tcp`，層級 `none`，核准後是 `fallback: landlock`（隔離降級）。不走 9p 的回復路徑：本機 Landlock 照每 10 分鐘在暫存區重做自測，設定改好就回到 `landlock` | 同上 |
| `sandbox_state_unconfigured` | 暫存區沒設（`reason_detail: unset`）或寫不進去（`not_writable`）；組 profile 時才發現的也是這個原因 | 同上 |
| `protected_location` | 專案根在受保護位置，或本身是 Touka 目錄 | 卡片 |
| `scan_limit` | 專案太大或 2 秒內掃不完（背景掃完後下一條指令會用結果）；結果快取 10 分鐘，之後只有專案根第一層新增或換掉資料夾時補掃那些，所以多半只在第一次綁定或 TTL 到期時出現 | 卡片 |
| `runner_not_wired` | 要隔離但不在 `ISOLATION_WIRED_ACTIONS` 的工具 | 卡片 |
| `isolation_check_failed` | 判定本身出錯，或健康檢查讀不到 kernel（讀得到後自動恢復），當 `none` | 卡片、工具錯誤 |
| `executor_runner_unavailable`、`spec_too_large`、`legacy_approval_channel` | 預期層級沒有對應的 runner／要開放的路徑太多／舊 ToukaUI 審批通道標不出隔離 | 工具錯誤 |
| `kernel_changed` | 原生 Linux：檢查之後啟動器被改過（這次回 `kernel_outdated` 並請求重探） | 重探 |

- 啟動器失敗時 exit 125、stderr 一行 `TOUKA_SANDBOX_ERROR:<code>`（第二段起只給人看，代碼另寫進 `--status-fd`，runner 只認那條），代碼只認 `spec.FAILURE_CODES`（`process_setup` 也涵蓋 spawn 端在 PDEATHSIG 生效前就結束、spec 的 `parent_pid` 對不上，指令不執行）。看到標記只請求重探、不直接改層級，不分代碼 60 秒內最多一次。
- 核心版本（Landlock ABI）：

| 核心 | ABI | 代理人指令 |
|---|---|---|
| 6.12 以上 | 6～7 | `landlock`：檔案、TCP 埠、信號與抽象 unix socket 都隔開 |
| 6.7～6.11 | 4～5 | `landlock`：檔案與 TCP 埠；信號沒隔開 |
| 6.2～6.6 | 3 | `none`（`landlock_abi_insufficient`）；核准後只隔離檔案 |
| 更舊或沒開 Landlock | 0～2 | `none`；核准後未隔離 |

ABI≥4 但 Fast Open 或 MPTCP 沒關時，上表前兩列改成 `none`（`net_tcp_fastopen`／`net_mptcp`），核准後是隔離降級（檔案照樣隔離、一般 TCP 仍擋封鎖埠，但擋不住這兩種連線）。

- 設定：`TOUKA_SANDBOX_STATE_DIR`（compose 設成 named volume `touka-sandbox-state` 的掛載點）、`TOUKA_SANDBOX_KERNEL_DIR`（映像 `ENV`）、`TOUKA_EXECUTOR_DIR`（有值時預期是執行器層，見下一節）；`PROXY_PORT`、`HEARTBEAT_PORT`、`CHAT_PORT` 是字串，只用來算封鎖埠。

## 隔離第二批（iso-c2）第二段：執行器容器與隔離狀態（ADR-0272）

- 範圍：預期是執行器（`TOUKA_EXECUTOR_DIR`，compose 設成 `/app/ipc/executor`）時，終端、互審驗證指令、演化改經本機 relay 交給 `touka-executor` 容器執行；呼叫方式、背景、停止、逾時不變（執行器端的上限是本機逾時＋30 秒、最多 900 秒）。控制通道是 ipc volume 裡的 unix socket（frame 格式的 SSOT 是 `sandbox_kernel/framing.py`），不是 HTTP 端點，也不在 OpenAPI 裡。
- 層級 `executor` 要 READY 的版本、kernel 雜湊、伺服器程式雜湊、封鎖埠雜湊、roots、自測、ABI≥3 全部相符；ABI≥4 時網路設定也要安全、封鎖埠不能擋不全。ABI 3 的執行器仍判 `executor`、features 只有 `fs`。不在執行器範圍（綁了專案、代理人資料夾是 `custom/<id>/drive`）時那次判 `none`。
- 審批事件的 `isolation`：多一律帶的 `expected`（部署預期的層級），有值才帶 `net`（降級去處那一層的網路設定問題）、`block_ports_issue`（`block_ports_incomplete`／`block_ports_overflow`）、`local_issue`（讓降級只能 `direct` 的本機原因）；`fallback` 多 `executor`（核准後在執行器裡執行，features 不含 `tcp`），`isolation_ack` 照卡片送 `executor`。核准後的 runner 種類不能低於卡片、features 要包含卡片的。
- 新原因碼（卡片、`GET /isolation`、工具錯誤；畫面文字，不在 `errors.json`）：

| 代碼 | 意思 |
|---|---|
| `executor_auth_failed` | 連得上但權杖讀不到或不對（執行器剛重啟，或控制通道的權限被改） |
| `executor_version_mismatch` | 協定、伺服器程式或封鎖埠雜湊和 proxy 不同（只重建了一邊） |
| `executor_roots_mismatch` | 執行器掛的資料夾和預期不同（compose 不一致） |
| `executor_selftest_failed` | 執行器的自測沒有通過 |
| `executor_restarted`、`executor_rejected` | relay 經狀態 fd 回報：執行器在這條指令前重啟過、START 被拒（monitor 照這個原因判 `none` 到下一次健康檢查） |
| `agent_dir_not_in_executor`、`project_not_in_executor` | 這次不在執行器範圍：代理人資料夾不在執行器裡、對話綁了專案 |
| `block_ports_incomplete` | `connections.json` 還沒讀成功或讀不到（任何一條連線的 `url` 是空字串也算：可能代表用預設端點，不知道要擋哪個埠；連線頁存檔從 2026-06-18 起拒收空網址，設定快照匯入不檢查）；修好後約半分鐘自動恢復 |
| `block_ports_overflow` | 封鎖埠超過 1,024 個；只擋核心的埠，要重新啟動才恢復 |
| `interpreter_untrusted` | 可信直譯器不合格（容器裡擁有者鏈不是 root 或可被別人寫）；降級只能 `direct` |

  另外：`kernel_outdated` 也表示 READY 的 kernel 和原始碼不同；`landlock_abi_too_old` 也表示執行器的 ABI 小於 3；`probe_pending` 也表示第一次健康檢查還沒完成。
- `GET /isolation`（也有 `/v1/isolation`，`require_authenticated`；沒登入 401）：走 `ensure_ready`，回 `IsolationStatusResponse`：`level`、`expected`、`reason`、`detail`、`features`、`local`（`status`、`abi`、`reason`、`net`）、`local_issue`、`executor`（只有 `abi`、`net`，沒有可用執行器時是 null）、`block_ports_issue`、`repairs`（自癒計數：`attempted`、`repaired`、`refused_outside`、`refused_protected`、`refused_not_owner`、`nothing_changed`、`throttled`、`retried`、`retry_failed`、`permission_relock`、`denied_burst`、`window_resets_at`；不是 Linux 時是 null）。不帶路徑、roots、權杖、digest、`boot_id`、金鑰與代理人 ID。
- `GET /health`、`GET /system_health`（含 `/v1`）多 `system_notices`：`[{"code", "level"}]`，目前的代碼是 `autonomy_config_unreadable`、`auth_policies_unreadable`（都是 `warning`），文字由前端翻譯。`autonomy.json` 讀不到時 `status` 也是 `degraded`（沒有另外的欄位），`GET /v1/autonomy` 回的是暫代的最嚴值，這時 `POST /v1/autonomy` 照收到的整份寫檔並清掉 `autonomy_config_unreadable`（原檔被蓋掉；前端沒有擋存檔）；啟動時讀不到 `auth_policies.json`（`OSError`）時政策維持未載入、所有工具都拒絕，`permission_config_degraded_reason` 是 `auth_policies_unreadable`。
- MCP 設定（`server_config.json`、`mcp_hub.json`）存在卻讀不到時，安裝、同步與建立回 `PERSISTENCE_ERROR`、不改檔；設定資料夾列不出來時啟動健檢中止。
- 封鎖埠多了從 Settings 的上游 URL、`connections.json` 全部條目、路由快照與 MCP registry 推得的非公網主機埠（80、443、53 除外；非公網只看字串、不查 DNS：IP 不是公網位址，或主機名是 `localhost`、單段名稱、`*.localhost`、`*.internal`、`*.local`、`*.home.arpa`，像 `nas.lan` 這類區網網域名不算），行程存活期間只增不減；`ModelManager.route_endpoints()`（唯讀）與 `reload_connections()`（設定快照匯入、啟動補檔、系統還原後呼叫）讓新連線立刻進名單。匯入含連線的設定快照後自訂模型路由先關閉，要重新掃描或重啟才恢復（同連線頁存檔）。系統還原失敗時（`routers/system.py` 的 except 分支，服務不重啟）也呼叫 `reload_connections()`，`connections.json` 沒變也一樣關閉自訂模型路由，要重新掃描或重啟。
- 終端：隔離內（層級不是 `none`）的指令失敗、輸出有 Permission denied／EACCES 又出現封鎖埠時，給模型的輸出多一行 `[isolation] Port <埠> is blocked inside the sandbox …; use a different port.`。
- 設定：`TOUKA_EXECUTOR_DIR`；執行器的資源上限 `TOUKA_EXECUTOR_MEM_LIMIT`、`TOUKA_EXECUTOR_PIDS_LIMIT`、`TOUKA_EXECUTOR_CPUS` 只給 compose 用（`.env.example` 有註解，預設 2g／512／2）。

## 初始管理員密碼交付（ADR-0232）

`ensure_admin_user` 保持 `_users_file_lock → _atomic_users_write` 與 SHA256→bcrypt；初始密碼檔受管寫入 OSError 傳固定 `ToukaException(PERSISTENCE_ERROR)`，在新增 admin／users commit 前停止，不回原 cause／路徑／密碼。既有 users bytes 或缺席狀態保持；既有 admin 不被重設。交付成功後 users 另一步失敗仍沿既有錯誤，沒有跨檔交易保證。

此來源修正不等於既有真實 admin 登入已恢復；沒有改角色、EmailStr、登入 rate limit／cookie／JWT 契約，也沒有 raw bcrypt fallback、匿名復原端點或自動帳戶修改。公開 endpoint/schema 未變，不重新生成 OpenAPI。


## 任務保存結果（可靠性 R2 B1，來源階段）

原生 `mission_tools` 收到 Memory `MissionPersistenceError` 時，以既有 `build_tool_error` 回傳
`error_class=internal_error`、`error_code=1302`（出貨 `PERSISTENCE_ERROR`）與 `outcome=unknown`；不回原 exception 內容，也不假稱任務初始化／更新／收案成功。
unknown 表示無法證明提交結果，建議先讀取看板再決定保存；不是可以自動重送的「確定未寫」。角色、目標 agent、review 與任務鎖邊界不變。
Dispatch 使用同一 Memory 保存入口，失敗先於 mission token 輪替與外部啟動；本批只驗 sandbox/mock，不啟動外部工具。本契約來源驗證狀態當時記在 HANDOVER，現在只留在開源前的私人歷史；發布／live 未執行。

## 對話保存／終態模型（可靠性 R2，來源候選）

`src/models/session_persistence.py` 是純Pydantic SSOT；saved含representation／revision，skipped分incognito／no_text，failed／unknown只回固定error_code。
同一生成helper登錄模型並拒絕重名差異；snapshot exists為必要boolean，present／absent內容互斥。
正常GET保持原形並含revision／derived round_count；`?snapshot=true`回strict typed存在或缺席，讀錯不發token。
POST必帶revision；existing DELETE需If-Match，strict absent仍200。SQLite同transaction read／compare／write，
revision涵蓋完整14欄readset、owner、absence、process epoch與generation；省略保留、合法null清除，auth_level:null不變。
原owner／agent／project／安全級授權保持；unknown不能當作未提交。

只有已授權明示 `ui_persistence` 使用 `_pump_ui_stream`／`_persist_ui_assistant_turn`。pipeline前同一snapshot
綁owner＋client revision，dispatcher admission再驗user節點；原requester不隨soul執行身分切換。
UI envelope不送上游provider。無UI契約的已認證OpenAI／Anthropic及內部舊路徑不取得新保證。
fatal RoundError／rhythm中止分別為error／cancelled，不自動保存部分內容；只有確認保存才排非阻塞trim，
trim以strict原基底CAS，不能覆蓋UI新樹。詳 [ADR-0231](adr/0231-session-persistence-outcomes-and-revisions.md)。

## 真人 Heartbeat 排程橋接（ADR-0230，來源候選）

`GET/POST /v1/heartbeat/schedules` 與 `DELETE /v1/heartbeat/schedules/{schedule_id}` 使用
`get_current_user` 的 fresh human 身分；拒絕 service JWT、停權使用者，X-Agent-ID 不代替權限。
一般 user 只可見／管理 owned agents，admin 全域；既有 ID 更新同時驗舊與新 agent，刪除隱藏／不存在 ID 均 NOT_FOUND。

GET 回裸 list，輸出型別預設以正確呈現 sparse 舊排程；ETag 保持 HB schedules-only revision。
POST 為 upsert，mutation 必須 If-Match；bridge 先取得當前 revision／所有權，HB 在同一 transaction lock 內再驗，
競態回409且無副作用。沒有重新啟用 Proxy scheduler。

固定 `HEARTBEAT_EXTERNAL_URL` origin、拒絕 userinfo/query/fragment、無 redirect、單送且整體10秒。
空 shared secret 不送 HTTP；服務401/403轉上游失敗，mutation timeout回結果未知。
只有新 schedules APIRoute 對 RequestValidationError 做安全轉碼，不回 input/content/ctx；其他路由契約不擴改。
HB模型由純SSOT生成投影；出貨error example補齊舊選定registry的缺項，保留既有覆寫且不寫回。
詳 [ADR-0230](adr/0230-heartbeat-transaction-and-credential-contracts.md)；source 通過不等於已部署。

## 工具批准分類（ADR-0229，來源候選）

終端及自帶批准例外綁定可信 builtin 註冊 callable，模型的 `command` 不會把普通工具變成終端。
RoleGrants／MCP grant→PaC→HITL→公開參數驗證→可信 context→同一已解析 handler；批准等待中的 registry 變動不替換本次 handler。
真終端 `execute_command`／`bash` 保留既有批准通道及黑白名單；self-approval 工具保留自身人類介面（ADR-0254 起剩 `request_skill_install`、`request_mcp_creation` 兩個，`evolution_implement_and_verify` 改為一般分類）。
冒用例外工具名但 handler 不符回 `execution_classification_mismatch`。來源修復尚未發布／部署，詳[ADR-0229](adr/0229-trusted-tool-execution-classification.md)。

## 原生工具公開輸入與執行授權（ADR-0228）

七個typed native入口共用Pydantic public-input模型生成schema及驗證；Memory定義留Memory，其餘在 `src/tools/contracts.py`。
ToolExecutor先依verified identity的RoleGrants核對實際native名稱（builtin優先，再Memory），無grant在PaC/HITL前拒絕；schema廣告或YOLO不能擴大角色權限。
模型指定identity/session/project/runtime值先移除，再採可信Context；runtime SSOT仍為EXECUTION_CONTEXT_KWARG_NAMES。
只selected public入口拒絕未知/錯型別/非法日期，build_tool_error不回傳Pydantic原input或秘密。外部MCP與內部GDPR橋接保持原路徑。
evolution timeout_override/max_storage_mb僅收緊驗證sandbox上限且只clamp一次；create_skill author僅metadata；ide_open_diff session自動注入。
edit_soul沿既有0-based/min50/protected delete；dispatch_subagent_group每批最多8，同時最多4。
研究工具三欄位與handler/schema不變；R2通用native角色grant執法適用其原角色權限。詳見[ADR-0228](adr/0228-native-tool-public-input-contracts.md)。

本文件定義了 ToukaProxy 對外暴露的端點、資料結構以及單一事實來源 (SSOT)。本文件旨在作為系統整合參考，不包含內部實作細節。

---

## 🛡️ 認證與身分識別 (Authentication & Identity)

全系統實施 **JWT (JSON Web Token)** 強制認證。

### 1. 雙軌認證機制 (Dual-Track Auth)
- **人類使用者 (Human Auth)**：
    - **登入端點**: `/auth/login` (核發 Access & Refresh Token)。
    - **憑證儲存**: Access Token 存於前端記憶體；Refresh Token 存於 `HttpOnly` Cookie。
    - **Cookie Secure 旗標 (H1/P15 fix)** ⭐：`SECURE_COOKIES` 預設值為 `True`（生產安全優先）；開發環境透過 `.env` 明確覆蓋 `SECURE_COOKIES=false`。
    - **登入速率限制 (H2 fix)** ⭐：`/auth/login` 採 IP 滑動視窗（預設 10 次/15 分），超限回 HTTP 429 `AUTH_RATE_LIMITED`；成功登入後計數歸零。
    - **註冊/刷新速率限制 (P6 fix)** ⭐：`/auth/register` 與 `/auth/refresh` 同樣採 IP 滑動視窗速率限制，防止暴力攻擊。
    - **換發機制**: `/auth/refresh` 支援靜默刷新，前提是 `users.json` 中的 Refresh Token 尚未過期。
    - **Token Rotation (H6 fix)** ⭐：每次 `/auth/refresh` 同時簽發新 access token + **新 refresh token**，舊 token 立即失效（限制竊取後的曝險窗口）。
    - **jti Replay Detection (P13 fix)** ⭐：Refresh token 包含唯一 `jti` claim（UUID）；`users.json` 的 `refresh_token_jti` 欄位儲存當前合法 jti；重用舊 token 觸發 `AUTH_REFRESH_TOKEN_REUSE`（HTTP 401）並撤銷全部 session。
    - **自助改密碼 (DA25)** ⭐：`POST /v1/auth/change-password`（`get_current_user`，僅能改自己）——驗當前密碼→設新密碼（`min_length=8`）→清 `is_temp_password`→撤銷 refresh token→刪 bootstrap 一次性密碼檔。補上臨時/一般密碼使用者原本缺失的自助改密通道。
    - **帳號停權強制 (DA23)** ⭐：`User.is_active`（`status=="active"`，SSOT property）在四閘門強制——`authenticate_user`（登入→403）、`verify_token`（中央 Bearer 閘→token 失效）、`get_current_user`（拒絕）、`refresh_access_token`（不換發、不誤觸 replay 撤銷）。停權即時生效、解除即恢復。
- **代理人身分 (Agent Identity)**：
    - **身分物件**: 認證後生成 `AgentIdentity`，包含 `owner_user_id` 與技能清單。
    - **路由攔截**: 針對受保護路由，系統強制檢查請求者是否為該 Agent 的合法擁有者。
    - **沒登入不能聊天（C3 fix；ADR-0245 收緊）** ⭐：`/messages`、`/chat/completions`（含 `/v1`）掛授權閘道 `require_authenticated`，沒有已驗證身分一律 401 `AUTH_INVALID_TOKEN`，不再降級成 Guest；**禁止**讀取 `X-Agent-ID` Header 作為 fallback（防止 header 注入身份冒充）。
- **優雅降級 (Local Dynamic Handshake)** ⭐: 僅限**完全未附 Authorization** 的 `is_private`／`is_loopback` 來源請求，系統自動在回應標頭 `X-New-Auth-Token` 發放新 Token。**帶失效 token（過期/簽章不符）一律 401**（不分本地/外部、不降級、不發卡），客戶端應走 `/auth/refresh` 續期；前端已登入時亦不採納 `X-New-Auth-Token`（ADR-0131／禁令 60，杜絕 DebugAgent 降級卡挾持）。
- **Query Token 降級驗證 (H3 fix)** ⭐: 針對無法手動注入 `Authorization` Header 的客戶端（例如瀏覽器原生 `EventSource` 的 SSE 串流連線），後端認證核心中介 `get_optional_identity` 支援從 URL Query 參數 `token` 中提取 JWT Token 並執行合法性與效期驗證。
- **會話終止 (Logout)**:
    - **端點**: `POST /v1/auth/logout`
    - **行為**: 伺服器會發送 `Set-Cookie` 標頭撤銷 `refresh_token`。
    - **SSOT**: Cookie 必須設為 `path="/"`, 前端請求必須帶入 `credentials: 'include'`。

### 1.5 終端機審核 API 安全 (Terminal Approval Security) ⭐
- **端點**: `POST /terminal/approve/{exec_id}` / `POST /terminal/reject/{exec_id}`
- **C8 fix**：必須帶有效 JWT；只接受 `Owner`、`Debugger`、`admin` 角色。
- **M1 fix**：必須通過 `verify_origin()` — 跨域請求（`Origin` 不在 `{localhost:3000/3001, UI_DEFAULT_URL}` 內）一律回 403。
- **M2/N5 fix**：`pending_approvals` 佇列上限 `MAX_PENDING_APPROVALS = 50`；所有佇列修改在 `_approval_lock`（asyncio.Lock）內執行。

### 1.6 MCP 配置端點安全 (M4/N3 fix) ⭐
- **端點**: `GET/POST /mcp/ui/config`
- **行為**：讀取磁碟上的 `server_config.json` 後**必須**經過 `MCPConfigResponse.model_validate()` 驗證。
- **`MCPServerItem.command`** 加 `@field_validator` 拒絕 shell metachar (`; | & ` $ < > ( ) \n \r`)；惡意設定回傳空 `mcpServers: {}` 並記錄 WARNING。
- **MCP 2026 合約（ADR-0198～0200／0221）**：現行 modern-only 的 runtime `ProtocolServerConfig` 與 wire `MCPServerItem` 只接受 `protocol_mode="2026-07-28"`；保留 `auth_mode`、`auth_profile_id`、`capability_policy`，移除 `sse_compat_enabled`。必須真送 `server/discover`，完整驗證 version／capabilities 後才 adopt；包含 `-32601 Method Not Found` 在內的 discovery 或 validation error 全部 fail-loud，不重連 legacy。
- **舊 UI 防覆寫**：POST 只寫 client 明示欄位；舊版 UI 省略上述遷移欄位時，必須從磁碟既有條目 carry over，不得以 schema 預設把 OAuth／protocol policy 洗回預設值。帶 headers 的舊 remote runtime 條目保守推導 `auth_mode=static_headers`。
- **`POST /mcp/hub/install` (N3 fix)**：必須帶有效 JWT；只接受 `Owner`、`admin` 角色安裝 MCP server。

### 1.6a MCP runtime request boundary（ADR-0198）⭐
- `ProtocolManager.call_tool_on_server` 必須接收由已驗證 pipeline identity 建立的 `McpRequestContext`；其中保留身分代打前的 `human_principal_id`，供 user OAuth／enterprise handle 隔離。
- principal-scoped handle key 為 installation＋principal＋protocol mode＋config generation；啟動時不連 private handle，identity-less catalog 不列 private installation。
- MCP SDK 結果先轉 canonical descriptor/outcome/error；`isError=true` 是合法工具錯誤，不當成功也不升成 HTTP 500。文字化僅在 `ToolExecutor` 最末端進行。
- `tools/call` 完成結果不快取；相同參數的兩次副作用呼叫必須各執行一次。
- health／restart／cleanup 分 installation 與 principal handle；單一 principal 的錯誤不得污染其他 principal 或 installation 全域健康。

### 1.6b MCP Python SDK v2 runtime（ADR-0199）⭐

- client 使用 public `Client(target, mode=..., cache=None)`；stdio 與 remote 共用同一
  lifecycle adapter，Creation 另以 clean launch environment 啟動候選產物。
- remote Streamable HTTP 必須使用 Touka 的 `httpx2.AsyncClient` factory：顯式
  SSLContext、四類 timeout、`follow_redirects=false`、`trust_env=false` 與 header
  allowlist redaction。
- health 顯示 negotiated `protocol_version`、`server_capabilities`、`server_info`；
  `server_info` 僅作診斷顯示，嚴禁參與授權。
- Dispatch 只提供 `/mcp`（ADR-0221 modern-only；舊 `/sse` 相容窗已退役）。

### 1.6c MCP 2026 P3 conformance boundary（ADR-0200）⭐

- modern 每 request 必須帶 protocolVersion/clientCapabilities metadata；Streamable
  HTTP 每訊息獨立 POST，接受 JSON 與 request-scoped SSE，無 modern GET、
  `Mcp-Session-Id` 或 `Last-Event-ID`。
- missing metadata、header mismatch、missing capability、unsupported version 分別保留
  `-32602/-32020/-32021/-32022`，不得被 legacy fallback 掩蓋；serverInfo 仍僅顯示。
- canonical complete result 保留 object/array/string/number/boolean/null，另保存欄位
  presence 以區分缺席與顯式 null。
- modern 協定行為由 sandbox 整合測試以實際 Proxy client 對 Node IDE 驗證 exact `2026-07-28` discover、完整工具集合、工具呼叫、Bearer／local request guard 與多 client 取消隔離（`tests/integration/test_mcp_modern_companion.py`）；不能以裸 socket 或 exit0 代替協定健康。

### 1.6d ToukaDispatch modern transport 與 request auth（ADR-0201）⭐

- 只提供 stateless Streamable HTTP `/mcp`；舊 `GET /sse` 與 `POST /sse/messages/` 回 404。modern request-scoped SSE 保留。維護者已確認沒有舊版使用需求，不再安排 sunset traffic 調查。
- MCP connection/session/clientInfo 不具授權力。Handshake 後每次 tool call 都
  顯式帶 application `session_token`，並重驗 expiry、server-owned transport、
  audience、method 與 external mission generation。
- credential store 不保存 bearer 明文或 caller 自述資料；只存 token digest 與
  執法分面。mission token 於首次成功外部 handshake 時原子消耗，新 mission rotation
  原子撤銷舊 generation credential 與 external session。
- `DISPATCH_URL` 是完整 modern endpoint（預設 `http://localhost:8083/mcp`），不得自行附加路徑或 fallback。
- supervisor readiness 真送 modern discover／tools-list；`/health` 只暴露匿名
  transport 計數、internal/external handshake 計數與最近 protocol/transport 使用。

### 1.6e MCP Tools／Resources／Prompts data plane（ADR-0203）⭐

- `GET /mcp/{server}/tools` 回完整 tool descriptor 與安裝政策狀態；不合法 schema
  只隔離該 tool。provider function schema 只能在 `ProtocolManager.get_all_tools`
  最後投影。
- `GET /mcp/{server}/resources`、`GET /mcp/{server}/resource-templates`、
  `POST /mcp/{server}/resources/read`、`GET /mcp/{server}/prompts`、
  `POST /mcp/{server}/prompts/get` 皆有 Pydantic response model 與 `/v1` 對等路徑。
- 所有 list 走完 cursor；頁數、項目、單筆與 aggregate payload 皆有限額，cursor
  loop fail-loud。capability 缺席或 `-32601` 回 `supported=false`，不發假廣告。
- resource URI 僅作不可信識別字：拒絕 `file:`、相對 URI、Windows path、控制字元與
  反斜線，永遠不交給本機 filesystem；本機資料仍只經 `ResourceManager`。
- Resources／Prompts data plane 須同時通過管理角色閘與
  `PermissionService.is_mcp_server_allowed()`；read/get POST 另經 `verify_origin()`。
  外部文字執行 DLP／prompt-injection 標記，prompt result 固定
  `trust="untrusted"`。
- tool schema 無 `$schema` 時補 Draft 2020-12；input root 必須是 object、output
  root 不限。只允許可解析的 internal `$ref`／`$dynamicRef`，外部 ref 不抓；深度、
  subschema、encoded bytes 與 validation time 皆有限額。

### 1.6f MCP ephemeral MRTR／HITL（ADR-0205）⭐

- modern `tools/call`、`resources/read`、`prompts/get` 若回 `input_required`，由同一
  coroutine 的低階 session driver 續行。上游 opaque continuation state 只留在
  driver stack frame，不進 DB、API response、SSE、log 或 telemetry。
- 每輪上游 retry 前重新驗證 active human／owner、installation enabled＋generation、
  server/tool grant、per-installation tool policy、session owner、較嚴格 auth level
  與 PaC；等待期間任一權限被撤銷即以 403／結構化 permission error 終止。
- 只有有聊天 interaction sink 的 foreground request 可詢問人類；headless、
  heartbeat、dispatch 或缺精確 context 一律 decline。Auto／YOLO 不推導同意。
- SSE `mcp_interaction_request` 只含 local `mcpint_*` ID、message、扁平 primitive
  Draft 2020-12 form schema 與 timeout；`mcp_interaction_resolved` 只含終態。
- 人類以 `POST /v1/tools/approve/{id}`（可帶 `{payload}`）、
  `POST /v1/tools/reject/{id}` 或 `POST /v1/tools/cancel/{id}` 解算；三端點皆需
  human ownership/admin 與 Origin 驗證。process restart／stream cancellation
  不恢復、不 replay。

### 1.6g MCP 互動式 OAuth authorization-code（ADR-0206）⭐

- P8-A 只支援前景 verified human 的 `oauth_user` authorization-code＋PKCE S256；
  client credentials、enterprise、heartbeat／dispatch delegation 與 DPoP 維持 unsupported。
- `POST /mcp/servers/{server}/oauth/start`、`GET .../oauth/status`、
  `DELETE .../oauth` 皆有 `/v1` 對等路徑；start／disconnect 另驗 Origin，並以
  human＋execution soul＋installation/generation＋profile/generation 建 stable slot。
- callback 固定為 `/mcp/oauth/callback`（及 `/v1` parity），只接受全域 policy 綁定的
  method／response mode。預設 `form_post`；query 模式須明示 canary。parser 對 body、欄位
  與單欄 bytes 早期限流，state 在 token exchange 前 CAS 單次消耗。
- callback HTML `no-store`／`no-referrer`／nonce CSP，先以 `history.replaceState` 清除當前
  Touka callback entry，再只向 exact UI origin 傳 `{type, operationId, status}`；API、log、
  popup message 與 browser storage 永不回傳 token、code、verifier、client secret 或 raw state。
- non-secret profile CRUD 與 managed key rotation 位於 `/admin/mcp/oauth/*`，只接受 verified
  human admin；mutation route 另驗 Origin。profile 私網例外須 exact installation＋exact HTTPS
  origin＋CIDR 三者同時匹配。
- credential SSOT 是 `${TOUKA_CONFIG_DIR}/mcp_oauth.sqlite3`（AES-256-GCM＋HMAC lookup＋CAS/
  lease/fence）；key ring 在 `.env`。一般備份排除 DB/WAL/SHM 與 key/rotation secret；restore
  若撞到現有 OAuth store/key 回 `MCP_OAUTH_RESTORE_CONFLICT`，不自動覆寫或刪除。
- `TOUKA_MCP_OAUTH_USER_ENABLED=false` 是預設 kill switch。關閉時拒絕新 start/callback commit/
  refresh/bearer egress，但保留加密 store/key，且 `none`／`static_headers` 舊路徑不做 OAuth I/O。

### 1.6h MCP workload OAuth／owner delegation（ADR-0207）⭐

- `auth_mode=oauth_client_credentials` 只接受 `ExecutionKind.HEARTBEAT`。direct authority 只支援
  `client_secret_basic`／`client_secret_post`；foreground、dispatch、enterprise、
  `private_key_jwt`、DPoP 與 JWT bearer 固定 fail-closed。
- 每次 `tools/call` 先建立短命單次 authorization ticket；HTTP auth 從實際 outbound JSON-RPC body
  驗 method/raw tool/arguments digest。initialize/discover/catalog/transport GET 使用獨立 bootstrap
  authority；workload handle key 使用同一組 security dimensions，禁止跨 authority 或 tool 重用。
- owner delegation 綁 exact P8-A active grant，但不複製 token。owner API 只送 installation/
  generation、exact tool、scope 與 expiry；profile/grant ID/version 由 Proxy 推導。P8-A source
  disconnect 必須先撤銷並 drain 所有衍生 delegation。
- 只有單一 Proxy process 可啟用：`TOUKA_MCP_OAUTH_WORKLOAD_FILESYSTEM_KIND=local` 且 lifespan
  取得 `mcp_oauth_p8b_runtime.lock`。remote/unknown filesystem、第二個 process 或多 worker 拒絕啟動。
- egress 次序固定為 durable audit intent → boot-local permit → permit 後 exact authority recheck → begin send →
  task exit/cancel acknowledge。permit TTL 不代表已開始 send 完成；撤權 drain 逾時保留 recovery fence。
  token exchange 與 delegated refresh 的 permit 綁的是已凍結之實際 OAuth `POST` endpoint／grant／
  resource／scope／client-auth shape 與 authority generation 的 secret-free digest，不得重用 MCP tool args digest。
  delegated refresh 的 permit 與 send 共用同一 frozen prepared request：Basic auth 只進 header，
  Post auth 才進 form；未知或 secret shape 不一致的 method 在 permit/network 前拒絕。
  已消耗 tool ticket replay index 有固定容量並按 ticket expiry prune；容量滿時在 audit/network 前拒絕。
- admin API：`/admin/mcp/oauth/workload/profiles*`、`.../credential`、`.../metadata/refresh`、
  `/admin/mcp/oauth/workload/recovery`、`.../recovery/retry`、
  `.../recovery/transitions/{operation_id}/reconcile`、`/admin/mcp/oauth/workload/keys/rotation`。
  metadata refresh 只回 minimal status；統一 recovery 投影涵蓋 transition/pending revocation/rotation。owner API：
  `/mcp/agents/{agent_id}/oauth/delegations*`，response 不含 P8-A source slot、grant ID/version 或 owner
  internal binding；皆有 `/v1` parity，mutation 驗 Origin。transition reconcile 必須由 admin 明示
  `confirm_present`／`confirm_absent`，再由 server 對 exact durable operation 與實際 JSON/SQLite state 驗證。
- secret/runtime SSOT 為 `${TOUKA_CONFIG_DIR}/mcp_oauth_workload.sqlite3` 與獨立
  `TOUKA_MCP_OAUTH_WORKLOAD_KEY*`；non-secret policy 為 workload profile/delegation JSON。
  備份排除 DB/WAL/SHM 與 key/rotation env；restore collision 與 P8-A 共用 fail-closed error。
- audit outbox sink 的暫時性一般例外保留 pending row，lifespan maintenance 以固定間隔重試；
  log 只帶 exception type，不帶可能含 credential 的 exception message，task cancellation 仍向外傳播。
- 每個 P8-B authority mutation 先在 SQLite claim exact `operation_id`＋kind；profile、credential、
  metadata、delegation、source disconnect、rotation 與 recovery/retry 共用單一 coordinator。
  fence completion 必須同時符合 process-local token 與 durable owner CAS；cancel/crash 不清 blocker。
- source disconnect 的 revocation row 保存由 exact delegation mirror 在 tombstone transaction 重算驗證的
  canonical source-slot digest 與 mutation operation ID。
  restart 後從 durable row 重建 fence、drain 並 finalize；`_pending_source_disconnects` 僅為 cache，
  不得單獨授權 P8-A grant disconnect。無法證明 owner 的 legacy row 固定 `recovery_required`。

### 1.7 連線管理端點安全 (N1/N2 fix) ⭐
- **端點**: `GET/POST /ui/connections`、`GET /ui/connections/status`
- **N1/N2 fix**：全端點加入 `_require_connections_role()` dependency；只接受 `Owner`、`Debugger`、`admin`。
- **N9 fix**：`/status` 端點回傳消毒後的錯誤狀態（`"ok"` 或 `"connection_error"`），不回傳含 API key 的原始 exception string。
- **控制面／上游隔離（ADR-0208）**：`provider=proxy` 只代表 ToukaChat → ToukaProxy 控制面，
  不參與 Proxy 的模型探索。其他受管理 connection 才可建立模型上游路由。
- **路由原子性（ADR-0208）**：自訂 endpoint＋key 只存在不可變 `CustomModelRoute` 記憶體
  快照；兩者整組採用，空 key 不得回退正式 provider key，且不進 Models API／`models.json`。
- **公開 chat 限制（ADR-0208）**：`OpenAIChatRequest`／`AnthropicRequest.base_url` 僅允許
  `null` 或省略；非空值於 Pydantic 邊界回 422。`baseUrl`、`base-url`、大小寫等語意別名
  也一律回 422，不得被 Pydantic 當未知 extra 靜默忽略。JWT／refresh／LDH 行為不變。

### 1.8 Project Overview IDOR 防護 (N10 fix) ⭐
- **端點**: `GET /ui/projects/{project_id}/overview`
- **N10 fix**：加入 `Depends(get_verified_identity)` + Owner/Debugger/admin 角色檢查，防止匿名枚舉其他使用者的專案稽核日誌。

### 1.9 全域安全中間件 (P11/P19 fix) ⭐
- **SecurityHeadersMiddleware**（`ToukaProxy/src/middleware/security_headers.py`）：每個回應自動加入：
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: strict-origin-when-cross-origin`
- **RequestSizeLimitMiddleware**（`ToukaProxy/src/middleware/request_size_limit.py`）：讀取 `Content-Length`，超過 `MAX_REQUEST_BODY_BYTES`（預設 10MB，由 `config.py` 定義）回 HTTP 413 `REQUEST_TOO_LARGE`。
- **CORS 來源正則放行（2026-07-02，E#17 security，ADR-0118）** ⭐：`main.py` 的 CORS 現以 `allow_origin_regex` 放行 `localhost` / `127.0.0.1` / `[::1]` / 私網 LAN（`10/8`、`172.16-31`、`192.168/16`）任意埠 ＋ `vscode-webview://.*`，同時保留 `allow_origins` 給 `ALLOWED_ORIGINS` 中的公開網域。修正：先前經 `127.0.0.1:PORT` 或 LAN IP（手機／第二裝置）開啟 UI 時所有 API 呼叫被 CORS 阻擋的問題。

### 1.10 觀測端點認證 (P22 fix) ⭐
- `/logs`、`/logs/recent`、`/logs/proxy`、`/memory`、`/memory/list`、`/memory/health`、`/tools`、`/tools_stats`、`/system_logs` 均需帶有效 `Depends(get_verified_identity)`。
- 僅 `/health`、`/system_health`、`/events`、`/monitor` 列為公開（allowlist）。

### 1.10b 韌性可觀察性 (Resilience Observability) — ADR-0078 ⭐
- **`GET /v1/stats` 折入 `interrupted_runs`**：ToukaProxy 啟動時 `autonomy_service.scan_and_clear_interrupted()` 掃描各 agent `autonomy_log.json` 殘留的 `heartbeat_in_flight` 標記（上次當機/被關時未跑完的背景脈衝），供前端「⚠️ 上次有 N 個未完成的背景任務」通知（沿用 `daily_quota`/`background_activity` 的 fold 技巧，免改 openapi/api.ts）。
- **`POST /v1/heartbeat/retrigger`**：人類擁有者一鍵重跑中斷會話（強制 `Auto`、只給 admin 或人類擁有者、會話須屬於該靈魂、`can_awaken` 能力守衛、`pulse` 夾 1..5），詳見 [HEARTBEAT_INTERFACE.md](HEARTBEAT_INTERFACE.md)。
- **系統降級可見化**：記憶系統未載入（degraded）由 `GET /v1/memory` 的 `system_degraded`/`degraded_reason` 暴露；前端 `SystemStatusContext` 解析後於側欄常駐 `DegradedBanner`（`role="alert"`）。
- **記憶提示（ADR-0253；結構提示 ADR-0256）**：`GET /v1/memory` 另回 `memory_notices`（list），每一項是 `{"code", "level", 選填欄位…}`，只放代碼；`level` 是 `info` 或 `warning`。代碼與等級的正本是 `routers/observability.py` 的 `MEMORY_NOTICE_*` 常數：舊記憶檔提示由 lifespan 的 `collect_memory_notices()` 算一次；記憶庫結構提示（5 個 `warning` 代碼）由 `memory_schema_notices()` 每次請求即時算，狀態讀自 `FactualStore.existing_shared()`。這是只給畫面看的提示，不是降級：不改 `system_degraded`、不進 prompt。既有三個欄位不變；端點沒有 `response_model`，openapi 不變，前端型別（`MemoryNotice`）自己宣告。前端：`MemoryNoticeBar` 放在右側欄降級橫幅下方（`info` 是 `role="status"`、`warning` 是 `role="alert"` 且和降級橫幅同色），有 `warning` 時左側身分列另加警告標記（`lib/memoryNotices.ts` 的 `hasMemoryWarning`）。
- **權限設定熱重載健康度（permission convergence P5）**：`GET /health` 與 `GET /system_health`（含 `/v1` 對等路徑）使用 `HealthResponse`／`SystemHealthResponse`，回傳 `permission_config_degraded` 與 `permission_config_degraded_reason`。熱重載解析失敗時服務保留 last-known-good、記錄 `AUDIT` warning，HTTP 仍為 200 但 `status="degraded"`、reason 固定為 `permissions_config_reload_failed`；下一次成功重載自動恢復 healthy。既有非空、非 `{}` 的壞設定在啟動期直接拒絕啟動；檔案不存在、空白或 `{}` 才是合法 bootstrap fallback。啟動時讀不到 `auth_policies.json` 與 `autonomy.json` 的情況見「隔離第二批（iso-c2）第二段」。

### 1.10c 可恢復串流 — ADR-0081／ADR-0231

明示 UI 契約以本節及前述 R2 規則為準，舊非 UI wire 另維持相容。
- **生成與連線解耦**：明示 `ui_persistence` 的 UI 對話（`/v1/chat/completions`、`/v1/messages`）設 `ctx["resumable_stream"]=True` → dispatcher 以 producer task（`spawn_logged`）抽乾 handler 串流寫入 `StreamBufferService`（per-session in-memory 緩衝，seq=chunk index），HTTP 回應改為緩衝訂閱者。**client 斷線只取消訂閱者，生成續跑**；完成時新 UI producer 先 conditional 保存 text，再發 generation／persistence 終態。既有 resumable_stream 旗標本身不足以啟用新契約；未明示 UI 的外部 request 保持原 wire。
- **`GET /v1/ui/sessions/{id}/stream?starting_after=N`**：SSE 重播＋跟流（游標續傳）；`_require_authenticated` ＋ `_can_access_session`（DA3 同款 IDOR 述詞，持久 session 不存在 fail-closed 拒絕，無痕以已驗 requester memory owner 判定且DB0）；無緩衝回 `STREAM_NOT_FOUND`(1212)。
- **`GET /v1/ui/sessions/{id}/stream/state`**：回同 stream 的 started／terminal及status，供已完成TTL接回；persistent須fresh DB owner／存在，無痕只驗memory owner。stop帶stream fence，受控delete只退休捕捉的舊entry。
- **`GET /v1/ui/streams/active`**：呼叫者可存取的進行中串流清單（逐條 ownership 過濾），前端重整後據此接回。
- **`POST /v1/ui/sessions/{id}/stop` 擴充**：原 rhythm interrupt ＋ process kill 之外，加 `stream_buffer_service.cancel`（producer 連坐取消＋訂閱者喚醒）。
- **保留策略**：完成後緩衝保留 `STREAM_BUFFER_TTL_SECONDS=600`；`STREAM_BUFFER_MAX_CHUNKS=50000` 記憶體保險；`STREAM_ACTIVE_MAX_SECONDS=3600` 孤兒保險。server 重啟＝進行中生成與緩衝俱滅（in-memory，誠實限制）。

### 1.10d 會話明確人類歸屬 (Session Owner Attribution) — ADR-0132 ⭐
- **`user_sessions.owner_user_id`**：human 建立 session 時由 `save_session` server-side 記錄（client 不可指定、`SessionModel` 無此欄位）；**僅 INSERT 寫入、UPDATE 永不改**（ADR-0107 projects 同構）。
- **授權聯集通道（additive）**：`_can_access_session` 三段——admin/Debugger 全域 → **owner 本人（human）放行** → 既有 agent-derived `is_owner`；NULL owner＝legacy 行為逐位不變。normal 角色自此可見/可管自己的裸模型對話，多用戶互相隔離。
- **指派防護收斂**：`save_session` 的「不得掛到別人 agent 名下」檢查僅對**真 agent**（`_is_registered_agent`，名冊不可用 fail-closed）執行；裸模型 id 為顯示標籤、一律放行。
- **出口消毒**：list/get 回應一律 `pop("owner_user_id")`（internal authz field）；openapi 合約零變。
- **identity 語意修正**：`upsert_session(identity=…)` 固定為「session 所屬 agent」——`_persist_assistant_turn` 的 human 呼叫者改傳 `identity=None`＋`owner_user_id`，根治 human UUID 污染 `agent_id` 的既有 bug。
- **legacy backfill**：`scripts/backfill_session_owner.py`（dry-run 預設、`--apply` 落盤、只填 NULL、冪等；僅 Owner 手動）。

### 1.11 Debug 工具執行安全 (P14 fix, DA20 修正) ⭐
- **端點**: `POST /debug/execute`
- Debugger 角色執行工具前，系統以 Gate-1 SSOT API `permission_service.get_allowed_tools(identity)` 取得不可變 `NameGrant`，再由 `is_tool_allowed(tool_name, grant)` 校驗；不獲授權回 403。空 grant＝不加端點專屬限制，`open_ended` 正確表達原本的 `"*"`。Owner 與 admin 免此端點專屬檢查。
- **DA20 修正**：原實作對回傳 `list[str]` 的 `get_capabilities_by_id(...)` 誤呼 `.get("allowed_tools")` → `AttributeError`／一執行即 500；改用上述既有 SSOT API（功能 bug 修復，非可利用）。

### 1.11a 權限表示法收斂（P2）⭐
- `PermissionService.resolve_grants(role_key)` 是工具、技能與 MCP 權限的唯一解析器；Gate 1 與 Gate 2 共用同一個 `Grant.allows()`，不得各自解讀 `"*"`。
- 角色繼承只累加 allow；`denied_tools`／`denied_mcp_tools` 僅作用於角色自身。跨角色禁令必須寫在 `PermissionsConfig.global_denied_tools`／`global_denied_mcp_tools`，且對 Owner 亦生效。
- `POST /permissions` 的 `PermissionsConfig` 新增上述兩個可選陣列，預設皆為 `[]`；既有 `RoleConfig` 陣列 wire 形狀不變。deny 陣列內的裸 `"*"` 表示 deny-all。

### 1.12 跨用戶 IDOR 防護：Debug 快照 / Souls 覆寫 (DA17) ⭐
- **端點**: `GET /debug/context-snapshot`、`POST /debug/context-snapshot/export`、`POST /souls/{soul_id}`（`update_soul`）
- **問題**：原僅檢 `role in {Owner,Debugger,admin}`，再以呼叫者提供的 `agent_id`/`soul_id` 建 `AgentIdentity(role='Owner')` 取/匯出/**覆寫**，完全跳過擁有權 → 非 admin 的 Owner（真人 assume 自己的 Owner agent 即可達）能讀/匯出/覆寫他人 soul/context。
- **修復**：三處加 `resource_manager.is_owner(target, caller)`（admin/Debugger 旁路、自身放行）；跨擁有者 read→回空（不洩）、export/update→403。

### 1.12 連線管理並發安全 (P9 fix) ⭐
- **端點**: `POST /ui/connections`
- `save_connections()` 以 `_connections_lock = threading.Lock()` 序列化並發寫入，防止多線程競爭導致設定損毀。

### 2. 服務間通訊 (Heartbeat to Proxy)
- **Service JWT**: Heartbeat 調度器呼叫 `/v1/heartbeat/awaken` 時，必須在 `X-Heartbeat-Token` 標頭帶入「服務級 JWT」。
- **自主權檢驗**: 喚醒時 Proxy 會連動檢查擁有者的 Refresh Token 狀態，確保使用者活性。

### 3. 資源監獄與隔離 (Resource Prison) ⭐
所有檔案存取必須通過 `ResourceManager`（整合模式）或透過記憶系統工具（Proxy + ToukaMemory）。
- **路徑解析**: 嚴禁在 API 處理器中拼接絕對路徑。必須使用 `ResourceManager.get_vault_path(identity)` 或 `get_project_path(project_id)`。
- **Symlink 防護 (P16 fix)** ⭐：`ToukaProxy/src/utils/path_security.py` 提供 `validate_no_symlink_escape(path, allowed_root)`；透過 `Path.resolve()` 解析所有 symlink 後再執行 `is_relative_to()` 邊界檢查，防止 symlink-based vault 逃逸。
- **物理隔離**: 系統強制將 Agent 鎖定在 `AppDataDir` 下的子目錄中，實現物理級別的資料隱私。
- **記憶系統集成 (ToukaMemory Integration)** ⭐：
  - **整合模式**：Proxy 直接掛載 ToukaMemory 模組，透過 `memory_tool_registry` 分發工具。所有 I/O 操作透過 `ResourceManager` 執行。
  - **邏輯下沉**：工具實作邏輯已完全下沉至 ToukaMemory。Proxy 扮演「薄橋接器」，負責身份錨定與權限透傳，不重複實作業務邏輯。
  - **依賴反轉接縫（ADR-0116：整合模式單一部署）**：`get_resource_proxy()` 只回傳 Proxy 注入的真實 `ResourceManager`；取不到即 fail-loud。ToukaMemory 獨立 MCP 模式與 `_FallbackResourceProxy` 已退役，不得靜默降級至無身分隔離或路徑保護的 I/O。
  - **記憶來源（記憶系統第一批段 4，ADR-0256）**：記憶閘道（`tools/builtin/memory.py`）持有 `services/memory_source_resolver.MemorySourceResolver` 單例，不在 `main.py` 注入。存記憶時只用執行器注入的 `session_id`／`request_id` 與 `PipelineContext.source_message_id` 組來源；`source_message_id` 只有兩處寫：`routers/proxy._authorize_ui_persistence`（明示 UI 持久模式、revision 驗過後的 `user_parent_id`，無痕不寫）與 `routers/heartbeat._execute_single_pulse`（每次脈衝的觸發 user 節點）。`query` 帶 `include_sources` 時才把查回器交給記憶系統。外面帶進來的 `source_context`／`source_resolver` 一律剝掉。細節見 [MEMORY_INTERFACE](MEMORY_INTERFACE.md)「記憶來源」。
  - **一步取代與查舊版本（記憶系統第二批段 L，ADR-0263）**：`manage_dups` 的 `operation="supersede"` 存的新記憶，閘道同樣組 `source_context`（和 `save` 同一條）。`memory_server.factual.SupersedeTargetError`（被取代的不是自己仍有效的記憶）轉成 `resource_not_found`，訊息固定、不回顯 id；其他例外照舊是 `internal_error`。`query` 公開欄位多 `include_history`。細節見 [MEMORY_INTERFACE](MEMORY_INTERFACE.md)「記憶會演變與安全標記」。

---

## 🛠️ 啟動引導與自癒 (Initialization & Bootstrap)

### 啟動序列
當 `ToukaProxy` 啟動時，會觸發 `ensure_system_bootstrap()`：
1. **環境自癒**：自動從 `.env.example` 復原缺失的 `.env`。
2. **配置自癒**：自動從 `config/*.json.example` 復原缺失的配置檔案。
3. **法律稽核**：確保環境變數與進程操作僅限於白名單檔案中 (TID251)。

---

## 🤖 模型與代理 API

### 1. 代理清單與發現
- **端點**: `GET /agents` 或 `GET /v1/models`
- **輸出**: 支援的模型清單（包含 L0, L1, L2）
- **ID 歸一化 (ID Unification)** ⭐: 系統全面移除 `ToukaSoul` 前綴。API 回傳之 `id` 欄位強制為簡潔正式 ID（如 `Alice`）。針對帶有前綴的舊 ID 請求，路由層會自動糾偏映射。
- **動態發現與合併**: `ModelManager` 掃描物理目錄（`soul/`, `custom/`）後，自動合併 `models.json` 中的顯示名稱與持久化設定。
- **健康檢查與保護**: 
    - `check_models_health()` 自動驗證所有連線。
    - **靈魂硬保護 (Soul Guard)**: 物理靈魂目錄（L1）受豁免保護，清理機制（Pruning）嚴禁移除存在物理實體的靈魂代理人。

### 2. 自定義模型 (Level 2) 管理
- **建立/更新**: `POST /v1/custom_models/{model_id}` (必須使用 `/v1` 前綴)
- **身份識別**: 依據認證 Token 判定操作主權。路由層會自動將 URL 中的 `model_id` 字串轉化為經認證的 `AgentIdentity` 物件傳遞至 Service 層。

---

## 🧩 技能與權限 API

### 1. 工具執行安全與三種模式 (HITL & Auth Modes) ⭐
所有具備執行能力的工具調用都會觸發二次攔截與 ABAC 策略判定：
1. **策略決策與參數透傳**：執行器構造 `PolicyContext`（包含 `AgentIdentity` 與這次執行的 `auth_level`：對話讀 session row，子代理沿用父代理，心跳用排程設定的模式；不採請求參數）。
2. **三種模式（ADR-0246）**：唯讀類工具三種模式都 `ALLOW`；黑名單恆 `DENY`；底線 F1–F4、F7 三種模式都是 `HUMAN_ONLY`。
    - **Manual**: 其餘工具與指令都要使用者確認（`CONFIRM`）。
    - **Auto**: safe 類 `ALLOW`；終端與 dangerous 工具 `REVIEW`（交審查員）；F5、F6 與動態 shell 寫法 `HUMAN_ONLY`。
    - **YOLO**: 黑名單與底線以外 `ALLOW`。
3. **模式授權 (Auth Mode Authority)**：只有 `admin` 或該靈魂的擁有者能把對話存成 Auto／YOLO，在對話存檔時判定，其他人存成 `Manual`。
4. **傳遞保證**：`ToolExecutor` 確保 `auth_level` 能完整滲透至底層工具（如 `bash`）以進行內核安全校驗。

### 2. 資源管理 (ResourceManager)
- **專案路徑**: `get_project_path(project_id, identity=None)` provide 安全的實體根目錄解析 (SSOT)。
- **主權路徑**: `get_vault_path(vault_type, identity)` 為 `vault_tools` 提供物理隔離的路徑解析。

### 3. 自主任務分派與特權代理 (AI Agent Dispatch & Cross-Identity Delegation) ⭐
為了支撐已存 Agent 與外部 AI 工具協同工作的「分派中心」提案，系統提供了跨身分看板存取的特權機制：
- **`DispatchAgent` 中介角色**：系統註冊了虛擬中介 `DispatchAgent`（Role: `Base`），用以作為內部/外部任務看板的數據中樞。
- **能力特權驗證 (`can_supervise_dispatch`)**：
  - 當 AgentIdentity 具有 `"can_supervise_dispatch"` 能力（於 `identity_map.json` 中靜態配置）時，呼叫 `mission_tools` (或調用 `MissionManager`) 存取目標 `target_agent_id="DispatchAgent"`，後端會將當前的身分上下文合法代理為 `DispatchAgent` 的 AgentIdentity。
  - 這允許 Supervisor 角色（如 `DebugAgent`、`Alice`）安全地繞過「物理路徑監獄」限制，初始化並讀寫 `personal/DispatchAgent/missions/current_mission.json`。
  - 未經該授權能力的身分嘗試讀寫 `DispatchAgent` 看板將直接被拒絕。
- **任務看板狀態轉換 FSM 規則**：子任務狀態更新強制遵循有限狀態機 (FSM) 轉換矩陣（如必須經過 `DOING` 才能到 `DONE`；`DONE` 狀態為終端狀態無法再次修改）。
- **FAILED 特權重置限制**：一旦子任務狀態進入 `FAILED`，只有具有 `can_supervise_dispatch` 特權標籤或角色為 `Owner`/`Debugger`/`admin` 的 Touka Agent 才可將其重置為 `TODO`，外部 AI coding agent 無法自行重置。
- **產出物欄位解耦**：產物屬性徹底解耦為單一產物 `artifact_ref: str | None` 與多產物列表 `artifact_refs: list[str]`，互不覆蓋踩踏，且均經過路徑穿越與絕對路徑安全驗證。
- **外部 AI 存取路徑（ToukaDispatch）**：外部 AI 工具須連 Port 8083 的 modern `/mcp`，以 `dispatch_handshake` 換取 transport-bound session token 後操作看板；舊 client 可明示連 `/sse` 相容窗。`ToukaMemory/server.py` 不再提供任務派發工具，原 standalone server 自動自癒授權機制已於 ADR-0046 C1 修復中永久移除。
- **Mission Token 隔離與自動召喚（ADR-0064 + ADR-0067 + ADR-0201）**：`dispatch_initialize_mission` 回傳一次性 `mission_token`（與 `mission_id`／generation 綁定，首次成功 handshake 即消耗），全域 `DISPATCH_TOKEN` 主金鑰永不離開 Touka 邊界。傳入選填參數 `auto_invoke=<executor>` 時，ToukaDispatch 依 `config/dispatch_hooks.json` 白名單以 `subprocess`（`shell=False`、env-only 傳 `mission_token`、whitelist 淨化子環境，`DISPATCH_URL` 指向完整 `/mcp`）fire-and-forget 召喚外部 AI（如 `claude -p`）。外部 session 不得呼叫 `dispatch_initialize_mission`；session 每次 tool call 另重驗 transport/method/generation。

### 4. 技能系統 (Skill System — Anthropic 標準) ⭐
Touka 的 Skill 系統已完全遷移至 [Anthropic Agent Skills 開放標準](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)（ADR-0066），對外完全相容、可雙向分發：
- **目錄式 SSOT**：每個 skill 為 `config/skills/<skill-id>/` 目錄，含標準 `SKILL.md`（YAML frontmatter：`name`+`description` 必填，`allowed-tools`/`version`/`argument-hint`/`model` 選填；Touka 擴充欄位 `display-name`/`category`/`author`/`is_system` 收於 `metadata:` 子欄位，通過標準 `quick_validate`）+ 選填 `.mcp.json`/`scripts/`/`references/`/`assets/`。
- **MCP 自動掛載**：`mcp_servers` 由同目錄 `.mcp.json` 的 top-level keys 推導（只取 keys，與 `${DISPATCH_URL}` 插值解耦）。同一份 `.mcp.json` 同時服務 Touka 內部自動掛載與外部 AI 安裝 plugin 時的客戶端 MCP 註冊，零客製橋接。
- **Progressive Disclosure（三層載入）**：① metadata（frontmatter）常駐 context；② body（SKILL.md 本文）於 skill 觸發時載入；③ resources（`scripts/`/`references/`/`assets/`）由 `SkillService.get_skill_resource()` 按需讀取（含目錄逃逸防護）。
- **Pull 模式 auto-trigger**：`SkillLayer` 常駐注入 `system-skill-dispatcher`（動態列出非系統 skill 的 name+description）；Agent 自主呼叫 `activate_skill(skill_id)` 寫入 `session.active_skills`，下一輪以 `header ∪ active_skills` 注入（仿 `connect_mcp_server` 範式，免 RBAC 的 dispatcher 常駐）。
- **端點**：`GET /v1/skills/list`、`GET /v1/skills/{skill_id}`、`POST /v1/skills`（建立/更新）、`DELETE /v1/skills/{skill_id}`（`is_system` 受保護不可刪）、`GET /v1/skills/{skill_id}/export`（打包為 `.skill` ZIP 下載）、`POST /v1/skills/install`（Owner/admin 由 URL 安裝）。安裝受 `ALLOWED_SKILL_DOMAINS` SSRF 白名單 + `follow_redirects=False` + zip-bomb 防護（entry/解壓/下載上限）+ `.bak` 原子覆寫保護。

---

## 🏭 創建引擎 API (Creation Engine — ADR-0069) ⭐

Touka 原生創建引擎將 **MCP 與 Skill 收斂為同一條創建流水線**：SPEC → VALIDATE → 🆕SMOKE（沙箱試跑）→ COMMIT（原子寫入＋上線）→ 🆕EVAL（後置、非阻斷評測）。對外開放（Phase D）後，Owner/admin 可經 HTTP 或 agent 對話從零創建產物，全程走既有安全閘門。

- **端點（皆 Owner/admin 限定）**：
  - `POST /v1/creation/skill`：由 `SkillCreateRequest` 創建 skill（委派 `SkillService.update_skill`，`config/skills/` SSOT）。
  - `POST /v1/creation/mcp`：由 `McpCreateRequest` 註冊 MCP server（委派 `MCPHubService.register_server`，`server_config.json` SSOT）。
  - `POST /v1/creation/scaffold`：由 `ScaffoldCreateRequest`（高階 `name`+`description`+`tools[]`）**決定性生成** SDK v2 `MCPServer` stub（零 LLM、零付費，handoff `sdk_major=2`）至 `config/mcp_servers/<name>/server.py`，可選 `activate=true` 直接餵入 create 流水線。
- **角色把關（SSOT）**：`_require_creation_role`（`_CREATION_ROLES = {"Owner", "admin"}`）。無 token → 401 `AUTH_INVALID_TOKEN`；角色不符 → 403 `AUTH_PERMISSION_DENIED`。
- **錯誤語意（不報 500）**：VALIDATE / SMOKE 失敗回 **200 + `errors[]`**（`committed=false`，產物未上線）；只有「無身分（401）」「錯角色（403）」與「多餘欄位（`extra="forbid"` → 422）」由 dependency / pydantic 擋下。非預期例外才 `SYSTEM_ERROR`。
- **不覆寫不變量**：CREATE 嚴禁覆寫既有 id（id 衝突於 VALIDATE 擋下）——以引擎層硬約束保護既有 skill / MCP 條目與 `data/soul|custom|personal_drive|legacy|vault`。
- **共用薄門面（DRY/SSOT）**：HTTP 路由（`routers/creation.py`）與 agent 對話工具（`tools/builtin/creation.py`：`create_skill`/`create_mcp`/`scaffold_mcp`）**共用同一編排層** `services/creation/api.py`（不依賴 fastapi、無角色邏輯）。
- **SMOKE / 注入防護**：MCP 經 `LaunchEnvironmentPolicy.clean` 使用白名單底（`LAUNCH_ENV_ALLOWLIST`，和正式安裝的 MCP 同一份，ADR-0254）＋spec 明示 env 真啟動握手（`initialize` + `tools/list ≥ 1`）+ 逾時 + 保證拆除；skill 做 SKILL.md round-trip 乾跑（不執行碼）。scaffold 生成以識別字校驗（`isidentifier()` + 非關鍵字）、自由文字 `repr()` 轉義、型別白名單、`ast.parse()` 兜底、名稱單段 slug（擋路徑穿越）。
- **EVAL / ModelJudge（預設關、零成本）**：commit 後生成每產物的唯讀評測基線 `config/evals/<kind>/<id>.eval.md`（衍生回歸物）。LLM-as-judge（`ModelJudge`）採**雙閘**：`TOUKA_EVAL_JUDGE_ENABLED` 環境旗標 ＋ 呼叫時 `eval_scoring=True`，兩者缺一即不計分、零付費；fail-safe 0.0 不阻斷 commit。
- **主權守衛**：**Creation Sovereignty Guard #29**（`scripts/guard_creation_sovereignty.py`）強制①每個創建端點都有 `_require_creation_role`、②creation 套件外不得繞引擎直呼 `update_skill`/`register_server`、③門面 `api.py` 不依賴 fastapi。
- **已知步驟**：變動創建端點的請求/回應模型後須重跑 `python scripts/update_openapi.py`（否則 Schema 同步守衛 #17 失敗）。

---

## 💓 心跳喚醒 API (Heartbeat Awakening) ⭐

### 1. 代理人喚醒 (Awaken)
- **端點**: `POST /v1/heartbeat/awaken`
- **預算判定 (Autonomy SSOT)**: 
    - **優先級鏈**: 請求指定 (`pulse_slots`) > 全域配置 (`autonomy.json`) > 系統保底 (5 Slots)。
    - **循環攔截**: 預算耗盡時由 `RhythmService` 強制終止 `ReAct` 循環，並注入 Nudge 提示。
    - **授權管理 (Capability SSOT)**: 喚醒權限不再使用白名單端點，統一由 `identity_map.json` 中的 `heartbeat_awakening` 能力標籤決定。
    - **配額限制**: `AutonomyService` 會監控每日總消耗，防止過度喚醒。
- **串流支持 (Streaming Parity)**: `AnthropicRequest` 現已完整支持 `stream` 參數，確保 ReAct 循環中的 Chunk 能夠實時推送並被測試套件觀測。

---

## 🏥 系統自癒與監控 (Resilience)

### 1. 配置自癒 (Self-Healing)
- **行為**: 啟動時自動檢查必需的 `.json` 與 `.env`。若缺失則從範本復原。這保證了系統在意外損毀時能自動恢復基礎運行。

---

## 🧪 除錯與診斷 (Debugging & Diagnostics) ⭐

### 1. 真實工具執行 (Real Action)
- **端點**: `POST /v1/debug/execute`
- **說明**: 允許 Debugger 實體執行指定工具。系統強制將身分鎖定為 `DebugAgent` 進行物理隔離執行與審計。

---

## 📜 介面合約主導開發 (Contract-Driven Development) ⭐
全系統遵循 **Contract as Code** 原則：
1. **後端驅動**：Pydantic 模型定義為 SSOT。
2. **合約驗證**：透過 `test_api_schema_sync.py` 確保模型與物理 `openapi.json` 強一致。
3. **合約工具**：提供 `scripts/update_openapi.py` 自動更新物理合約。
4. **前端守衛**：透過 `guard_frontend.ts` 強制校驗前端所有 API 呼叫是否符合 `openapi.json` 定義（包含 `/v1` 前綴與路徑格式）。
5. **前端同步**：前端呼叫 `npm run sync-api` 消費合約產出型別。

---

## 📅 時間主權 (Time SSOT) ⭐
全系統時間獲取必須透過 `src.utils.time_utils`。
- **內部基準**: 所有的時間戳記均以 **UNIX Epoch** 存儲與運算。
- **呈現轉換**: 僅在輸出時轉換為 **Asia/Taipei**。
- **強制執行**: 由 `lint_ssot.py` 靜態掃描攔截所有原生 `datetime.now()` 或 `time.time()` 調用。

---

## 🏗️ 配置與環境主權 (Env SSOT) ⭐
全系統配置遵循 Pydantic `BaseSettings` 規範。
- **中心化管理**: 所有配置必須映射至 `src.config`。嚴禁直接調用 `os.environ`。
- **強制執行**: 由 `lint_ssot.py` 靜態攔截業務邏輯中的環境變數訪問。
- **前端守衛**: 透過 `guard_env.ts` 攔截 `process.env` 與 `import.meta.env`，確保前端配置同樣遵循 SSOT 原則。
- **自癒能力**: 支援 `.env` 自動發現與缺失配置自動復原。
- **環境感知互斥載入 (ADR-0052)** ⭐：系統支援整合與解耦分開運行。當以大一統整合模式執行時，系統強制載入並統一讀取根目錄 `.env`；當分開獨立運行某個子專案時，強制載入並讀取子專案目錄下的 `.env`，不得混載或相互覆蓋。
- **`.env` 寫入路徑 SSOT 與 `TOUKA_ENV_FILE` 覆寫（2026-07-02，ADR-0118，延伸 ADR-0052／禁令 38）** ⭐：`routers/config.py` 的 `POST /v1/config/env` 讀寫 `.env` 一律經 `src.config.resolve_env_file_path()`（SSOT），移除裸相對 `".env"`。`resolve_env_file_path()` 新增最高優先權環境覆寫 `TOUKA_ENV_FILE`（高於整合／獨立模式自動偵測）；`docker-compose.yml` 設 `TOUKA_ENV_FILE=/app/host_root/.env`（**ADR-0151**：`./:/app/host_root` 恆掛 compose 目錄＝compose `env_file` 讀取的同一 .env，與 `WORKSPACE_ROOT` **解耦**；舊制 `/app/workspace/.env` 綁 `WORKSPACE_ROOT`，指向 repo 以外即寫幽靈 .env）→ 精靈與自動生成的金鑰**跨容器重建持久化**並於重啟重新注入。寫入後 `apply_runtime_env()` 同步 `os.environ` ＋ settings ＋ 模組層 `src.config` 常數（免重啟即時生效）。
- **`POST /v1/config/env` 寫入失敗錯誤消毒（2026-07-02，E#21 security）** ⭐：`update_env` 的 500 回應不再夾帶原始例外字串（移除 `error_output=str(e)`）；統一回乾淨的 `SYSTEM_ERROR`，詳情僅寫入 `logger.exception`。雖為 admin-only 端點，仍避免洩漏檔案系統路徑。

## 📦 部署主權 (Deployment Sovereignty) ⭐
全系統 Docker 鏡像遵循「等價性守衛」原則。

---

## 🔐 安全匯入禁令 (Import Security Mandates) ⭐

### 1. 密碼安全禁令 (BCRYPT_IMPORT_SSOT)
- **規則**：嚴禁在 `auth_service.py` 以外的任何檔案直接 `import bcrypt` 或 `from passlib import`。
- **風險**：bcrypt 原生 72-byte 截斷限制若被繞過，長密碼將靜默通過驗證。
- **執法**：`lint_ssot.py` 的 `BcryptImportRule`（ADR-0028）。
- **合規**：所有密碼雜湊透過 `AuthService.hash_password()` 執行 `SHA256 → Bcrypt` 預雜湊鏈。

### 2. 資料庫存取禁令 (SQLITE3_IMPORT_BAN)
- **規則**：嚴禁在授權白名單以外直接 `import sqlite3`。
- **風險**：繞過 `async_db.py` 的非同步佇列與物理鎖定，導致並發競態。
- **執法**：`lint_ssot.py` 的 `Sqlite3ImportRule`（ADR-0030）。
- **授權白名單**：`async_db.py`（ToukaProxy SSOT）、`factual.py`、`legacy_store.py`（ToukaMemory SSOT）。

### 3. NOQA 豁免同步驗證 (NOQA_REGISTRY_SYNC)
- **規則**：所有架構型 `# noqa` 豁免必須登記於 `NOQA_REGISTRY.md` 統計表，數量必須與代碼庫實際計數一致。
- **執法**：`scripts/guard_noqa_registry.py` 自動掃描並比對，整合於 `guard_all.py` 第 19 關 (Guard #19)（ADR-0029）。

### 4. 前端 Console 禁令 (CONSOLE_BAN)
- **規則**：前端生產代碼嚴禁直接呼叫 `console.log/warn/error()`，必須透過 `logSSOTError()` 封裝層。
- **執法**：`guard_console.ts`（CONSOLE_BAN），整合於 `guard_all.py` 第 15 關 (Guard #15)（ADR-0031）。
- **唯一授權**：`src/lib/error_handler.ts`（白名單）。

### 5. 模組邊界守衛 (Module Boundary Guard)
- **規則**：ToukaMemory 嚴禁直接 import ToukaProxy 內部模組（`src.services`、`src.utils` 等）。
- **執法**：`scripts/guard_module_boundaries.py`（`import-linter`），整合於 `guard_all.py` 第 21 關 (Guard #21)（ADR-0032）。
- **設定**：`.importlinter`（專案根目錄），`ignore_imports` 白名單涵蓋合法的 try-import 整合點。

---

## 📜 Legacy 統計端點與身分主權修復 (Legacy Stats — Identity SSOT) ⭐

- **端點**: `GET /v1/stats/legacy/{agent_id}` / `POST /v1/stats/legacy/{agent_id}/sync`
- **修復（2026-05-20）**：兩個端點原本忽略 URL `{agent_id}` 路徑參數，僅使用 JWT identity。修復後：
  1. URL `agent_id` 明確捕捉為 `str` 路徑參數
  2. 呼叫 `resource_manager.is_owner(AgentIdentity.from_str(agent_id), identity)` 進行授權驗證
  3. 所有服務呼叫使用 URL `agent_id`（而非 JWT 身分）
- **失敗照實回報（ADR-0253）**：兩個端點以前出錯也回 200（`{"status": "error", …}`），現在：
  - `GET` 讀統計失敗回 500 `SYSTEM_ERROR`；舊對話庫還不存在時回 0，不建目錄或資料庫。
  - `POST …/sync` 發生例外回 500 `SYNC_FAILED`；有任一檔寫入失敗也回 500 `SYNC_FAILED`，附 `synced`、`errors`。全部成功才回 `{"status": "success", "result": …}`，`result` 含 `synced`／`skipped`／`errors`／`total_in_db`。
  - 不新增錯誤碼；兩個端點沒有 `response_model`，openapi 的回應 schema 不變。
  - 前端 `useLegacySync` 只在 HTTP 成功且 `status == "success"` 時顯示「同步成功」，失敗顯示錯誤並重抓統計；統計失敗也顯示錯誤。
  - 已知限制：檔案都已寫入、但最後讀統計失敗時，整次仍回 `SYNC_FAILED`。
- **同步不搬檔（ADR-0264）**：`legacy/` 受保護，以前搬到 `backup/` 一律被擋（每次都回 `SYNC_FAILED`）。現在檔案留在原地，以（檔名, 內容 SHA-256）記為已處理，重跑跳過；`GET` 的 `archived_files` 改成已處理紀錄數、`pending_files` 是還沒處理的 JSON 數。
- **切分存入舊對話 `POST /v1/ui/sessions/legacy/export`（ADR-0264）**：存取檢查不變（要能存取該 agent）；`json_content` 直接歸檔進該 agent 的舊對話庫，不寫檔；`filename` 只當標籤。回 `{"status": "success", "archived": n}`，不再回實體路徑；沒有可歸檔的訊息回 `VALIDATION_ERROR`，寫入失敗回 `SYSTEM_ERROR`。以前寫檔被保護擋下，一定回 `SYSTEM_ERROR`。

---

## 🪟 滑動視窗對話紀錄 (Sliding Window History) ⭐

- **說明**：心跳全部 pulse 完成後，自動維護家對話長度在設定範圍內（ADR-0035）。
- **觸發**：PostHeartbeat hook（`_process_heartbeat_loop` 的 `finally` 區塊）；結束為 error 時不剪裁，歸檔／摘要前另驗精確靈魂歸屬（ADR-0276）
- **適用**：僅限家對話（`session_mode="home"`）；子對話完全排除
- **設定端點**：
  - `GET /v1/system/sliding_window`：讀取設定（已認證）
  - `POST /v1/system/sliding_window`：更新設定（admin only）
- **SSOT**：`ToukaProxy/config/sliding_window.json`（預設 `enabled: false`）
- **ACID 保證**：先歸檔進舊對話庫（`archive_payload`，一個交易）→ 確認成功 → 再更新 Session；任一失敗、或一則都沒歸檔到，安全跳過，下次重試（ADR-0264；以前先寫 legacy JSON，被保護擋下，正式環境從沒剪成）
- **身分**：剪裁、歸檔、摘要記帳都用家對話所屬的靈魂。心跳本來就是靈魂；聊天側（dispatcher 存檔後）從 session 的 `home_for_agent` 解析，解析不到不剪並記 warning，不用 UI 請求者（ADR-0264）
- **摘要**：`context_summary` 第一行是時間範圍；送摘要員的每行帶台北時間（ADR-0264）

---
## 👑 使用者管理與 RBAC (User Management) ⭐

系統現已支援多使用者權限隔離與管理員管理功能。

### 1. 使用者管理 (Admin Only)
- **端點路徑**: `/v1/admin/users`
- **角色限制**: 僅限 `role="admin"` 的使用者存取。
- **回傳模型 (DA22)** ⭐：四個端點全部 `response_model=UserOut`（消毒投影）——**不**回傳 `hashed_password`、`refresh_token_jti`/`previous_refresh_jti`/`refresh_jti_rotated_at`。完整 `User` schema 已退出 API 契約；前端 `lib/types.User` 指向 `UserOut`、`api.ts` 重生。
- **功能**:
    - `GET /`: 列出系統所有使用者及其指派的代理人。
    - `POST /`: 建立新使用者並設定初始代理人擁有權。
    - `PATCH /{user_id}`: 更新使用者權限、狀態或代理人清單。
    - `DELETE /{user_id}`: 移除使用者。

### 2. 個人檔案與身分同步
- **端點路徑**: `/v1/auth/me`
- **存取**: 任何已登入使用者。
- **功能**: 獲取目前登入者的完整 Profile（包含顯示名稱、頭像、Bio 與角色）。前端 `AuthContext` 依賴此端點進行身分感知與 UI 渲染決策。

### 3. 首次執行設定 (First-Run Setup) ⭐

- **端點路徑**: `/v1/admin/setup`
- **角色限制**: 僅限 `role="admin"` 的使用者存取。
- **功能**:
    - `GET /status`: 回傳系統設定狀態摘要（`SetupStatus`）。欄位：`dispatch_token_is_default`、`auth_secret_persisted`、`api_keys_configured`、`is_admin_temp_password`、`data_path_is_default`、`workspace_root_is_default`（新增，見下 2026-07-02）、`embedding_model`。
    - `POST /apply`: 將提供的設定值寫入 `.env`（使用 `python-dotenv set_key()`）。支援欄位：`dispatch_token`、`auth_secret`、`data_path`、`workspace_root`（新增，見下 2026-07-02）、`embedding_model`。`dispatch_token`／`auth_secret` 是歷來公開過的值時回 422、整份不寫（ADR-0254）。
- **實作**: `ToukaProxy/src/routers/setup.py` + `ToukaProxy/src/services/setup_service.py`。
- **is_temp_password 欄位**: `UserRecord.is_temp_password`（`users.json` SSOT）首次啟動設為 `True`，`admin_update_user` 變更密碼後自動清除。

#### 3.1 精靈介面擴充（2026-07-02，ADR-0118）⭐

首次執行精靈（ToukaChat SetupWizard）在既有欄位外補齊工作區根目錄與 Embedding 模型生命週期管理，並修補設定寫入的持久化與跨域存取。相關介面變更如下：

- **`POST /v1/admin/setup/apply` 新增 `workspace_root`**（bug②，延伸 ADR-0053）：精靈 Step5 現同時配置 `SANCTUARY_DATA_PATH`（資料目錄）與 `WORKSPACE_ROOT`（工作區根目錄）兩個 **HOST 路徑**，寫入根 `.env` 供 `docker compose` bind-mount。`setup_service.apply()` 具 Docker 感知驗證：容器內（`_is_running_in_docker()` 偵測 `/.dockerenv`）跳過對主機路徑的 `is_dir` 檢查（該路徑在容器內不可見），原生模式維持 `is_dir`。驗證失敗由 router 捕捉 `ValueError` → `ToukaException(VALIDATION_ERROR)` 回 **HTTP 422**。
- **`SetupStatus` 新增 `workspace_root_is_default`**：對應 `WORKSPACE_ROOT` 是否仍為預設值，供精靈判定是否需要引導設定。
- **Embedding 模型三動作端點（bug①，延伸 ADR-0070）**：Embedding 生命週期由 `ToukaProxy/src/services/embedding_service.py` 統籌，DOWNLOAD／SWITCH／RE-EMBED 三動作嚴格分離。新增三個端點（皆 admin 限定）：
    - `POST /v1/admin/setup/embedding/download`：以背景任務下載模型檔至 HF cache（**加法式**、僅補檔、絕不 `force_download`、不刪除任何既有模型；命中快取即略過）。安全動作，不觸碰任何向量／事實。
    - `GET /v1/admin/setup/embedding/download-status`：輪詢下載進度（逐檔 byte 進度）。
    - `POST /v1/admin/setup/embedding/switch`：請求體 `{model, force}`。寫入 `EMBEDDING_MODEL` 並重置 `MemoryManager` 單例（免重啟）。`evaluate_switch` 偵測既有記憶（`FactualStore.query_all_facts`）；當「已有記憶且模型不同」時 **要求使用者確認**（須帶 `force=true`），否則回可辨識的需確認狀態。RE-EMBED 屬離線維運（`reembed_memory.py`），永不自動觸發、絕不觸碰 `core_memory.db`（事實為無損 SSOT，向量為衍生索引）。
- **精靈 fail-loud（C，延伸 ADR-0053）**：`Step1SystemHealth` / `Step3ApiKeys` 現自行發起 apply-fetch、檢查 `res.ok`、顯示錯誤並僅在成功時前進。注意 `fetchTouka` 對 4xx/5xx **不拋例外**，呼叫端必須自行檢查 `res.ok`。

### 4. 身分隔離與代理優化 (Identity Isolation & Assumption) ⭐
- **隔離邏輯**: 在 `IdentityRouter` 攔截點，系統會驗證目標 `AgentIdentity` 是否屬於該使用者的 `owned_agents` 清單。
- **管理員代理優化 (Identity Assumption)**：管理員 (`admin`) 在切換身分（Assume/Proxy）時，系統會動態追蹤原始人類身分。若原始身分為管理員，即使在代理模式下存取 `/v1/admin/*` 端點，系統依然保留其管理特權。這解決了管理員在對話模式下無法管理使用者的問題。
- **安全**: 防止未經授權的使用者存取他人的靈魂資料、記憶或檔案庫。
