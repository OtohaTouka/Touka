# ADR-0207：MCP workload OAuth 與單一 Proxy process

- **狀態**：Accepted；P8-B implementation 已提交為 `8e9c59a5`，部署與未支援模式限制不變
- **日期**：2026-08-03
- **相關**：[ADR-0205](0205-mcp-ephemeral-mrtr-and-hitl.md)、
  [ADR-0206](0206-mcp-interactive-oauth-authorization-code.md)、
  [P8-B 實作計畫](../proposals/mcp-2026-p8b-workload-oauth-plan.md)

## 脈絡

P8-A 只處理前景人類 authorization-code。Heartbeat 是無瀏覽器的 headless 執行面，
若沿用人類 token、把 client secret 放進安裝設定，或在多 worker 間以 process-local permit
協調撤權，皆會造成 principal 混用、秘密外洩或 revocation race。現階段部署限制是單一
ToukaProxy process；尚無企業 IdP，因此 enterprise authorization 必須維持 fail-closed。

## 決策

1. P8-B 只對 `ExecutionKind.HEARTBEAT` 開放 `oauth_client_credentials`；foreground、
   dispatch、enterprise、`private_key_jwt`、DPoP、JWT bearer 與 assertion exchange 一律拒絕。
2. 直接 workload authority 只接受明示 profile 的 `client_secret_basic` 或
   `client_secret_post`。non-secret profile 位於 config JSON；client ID／secret 與 metadata
   snapshot 位於專用 AES-256-GCM SQLite，不進安裝設定、API response、log、audit payload 或備份。
3. owner delegation 不複製 P8-A bearer。每次呼叫以 exact owner＋execution agent＋installation/
   generation＋profile/generation＋active grant ID/version＋raw tool name＋scope＋expiry 解算；P8-A
   source disconnect 必須先 tombstone、取消並 drain 所有衍生 delegation，否則 source disconnect
   不得完成。
4. `ProtocolManager` 先簽發短命、單次、不可變 authorization ticket；request-aware HTTP auth 在
   最後 trusted serialization boundary 解析實際 JSON-RPC body，拒絕 batch／duplicate field，並逐一
   比對 `tools/call`、raw wire tool name 與 arguments SHA-256。initialize/discover/catalog/transport GET
   使用獨立短命 bootstrap authority，絕不重用 tool ticket。workload handle key 亦納入 authority
   kind、profile/delegation generation 與 exact tool，禁止跨 authority 重用。
5. bearer egress 前必須先 durable 接受 audit intent，再發 boot-local permit，並在 permit issue 後、
   `begin_send` 前做最後 exact authority revalidation；permit 綁 boot ID、
   authority epoch、task、ticket、method、payload digest 與 attempt。permit 到期只代表未發送者不可
   開始，不代表已開始的 send 完成；revocation/rotation 必須取消 task，等待 send task exit 後的
   acknowledge，逾時即保留 pending/recovery fence，不得猜測完成。
   token exchange 與 delegated refresh 的 method/payload digest 必須來自已凍結的 final OAuth `POST`
   request binding（endpoint、grant、resource、scope、client-auth shape、profile/credential/grant generation），
   不得沿用 `tools/call` 或 arguments digest；binding 不含 client secret、refresh token 或 bearer。
6. process-local permit 的正確性只成立於單一 Proxy process。因此 feature 啟用時必須同時：
   `TOUKA_MCP_OAUTH_WORKLOAD_FILESYSTEM_KIND=local`、取得 lifespan-owned OS exclusive lock，且啟動
   命令不得配置多 worker。remote/unknown filesystem、第二個 process 或 lock 無法取得皆拒絕啟動；
   此鎖不是可過期 worker lease。
7. durable SSOT 為 `mcp_oauth_workload.sqlite3`、`mcp_oauth_workload_profiles.json` 與
   `mcp_oauth_workload_delegations.json`。JSON mutation 以 generation CAS；SQLite mirror、authority
   transition、revocation、rotation 與 audit outbox 以 persisted FSM/fence 收斂。任何 JSON/SQLite
   不確定狀態都只可透過統一 recovery status 顯示；transition、pending revocation 與非 stable
   rotation 使用同一 minimal projection，drain 必須經明示 retry/finalize，資料面維持 fail-closed。
   `runtime_authority_ready()` 是 permit admission 的 durable blocker SSOT；process 內所有 authority
   mutation 與 key rotation 共享一個 coordinator、lock 與 exact global-fence token。transition 的
   人工收斂只接受 admin 明示 `confirm_present`／`confirm_absent`，server 以 operation ID 重新核對
   generation/digest 及實際 JSON/SQLite mirror；不得由 caller 提供 authority facts 或自動猜測成功。
   coordinator 的每個 fence 必須先以 `operation_id`＋mutation kind 寫入 runtime state，`complete()`
   只接受同一 process fence，並以同一 durable owner 做 CAS clear；遺失、過期或外來 fence 一律拒絕。
   rotation `start/maintain`、admin recovery/retry、profile、credential、metadata、delegation 與 source
   disconnect 皆共用 bootstrap 建立的同一 coordinator；`start()` 在持鎖時只呼叫私有 maintain 路徑，
   不重入 public lock。process crash 後，rotation/revocation 只可由 store 中的 exact owner 重建 fence。
   revocation row 另保存由 exact SQLite delegation mirror 在同一 tombstone transaction 重算驗證的
   canonical source-slot SHA-256 與 mutation operation ID；P8-A disconnect restart
   retry 以 durable row 為準，process-local pending set 只可當 cache。舊 blocking row 若無法證明 owner，
   forward migration 固定改為 `recovery_required`，不得猜測 backfill 或自動完成。
8. workload key ring 使用獨立 `TOUKA_MCP_OAUTH_WORKLOAD_KEY*`，不得重用 P8-A key。managed rotation
   採 prepare → dual-read/new-write → drain → retire；external source 不由 API 產生或回傳 key material。
9. 一般備份排除 workload DB/WAL/SHM 與 current/retired/rotation key env；profile/delegation policy
   可備份。restore 若目的地已有 P8-A 或 P8-B store/key，統一以 `MCP_OAUTH_RESTORE_CONFLICT` 拒絕。
10. tool authorization ID 的 process-local replay index 採固定容量＋expiry min-heap；entry 僅在 ticket
    expiry 後 prune，容量滿時必須在 audit、token exchange 與 MCP network egress 前 fail-closed。

## 結果與邊界

- 直接 workload 與 owner delegation 可分別以 global kill switch 關閉；未啟用或 registry 沒有
  對應 installation 時不建立 workload key、DB、HTTP client 或 topology lock。
- admin 控制面只回 non-secret profile、credential status、minimal metadata status（profile ID／
  generation／verified time／status）與 rotation/recovery；token endpoint、CIDR、scope、metadata
  fingerprint 與完整 snapshot 不進 API response；
  secret 輸入採一次性 password field，前端成功或失敗後皆清空。
- owner 只能管理自己擁有 agent 的 exact delegation；source grant/profile/generation 由 server
  推導，browser 不得自述；owner response 亦不輸出 source slot、profile/grant generation 或 grant ID。
- 多 worker／多主機支援不是橫向擴展設定，而是另一個架構階段：需 durable distributed permit、
  fencing token、task exit oracle 與一致的 audit backpressure，未實作前不得移除 topology gate。
- Phase 3 的 durable owner 補強不放寬此邊界；仍只有單一 Proxy process 可建立 boot-local permit。

## 2026-08-10 審查後修訂

- delegated refresh 的 permit 與 HTTP send 必須共用同一個 frozen prepared request。`client_secret_basic`
  的 `client_id`／secret 只存在 Basic Authorization header，不得列入 form fields；
  `client_secret_post` 才把兩欄放入 form。prepared request 以 `SecretStr` 遮罩，permit binding 仍完全去密。
- durable audit outbox 的單次 sink 失敗不得終止 lifespan maintenance task。一般例外只記錄 exception type，
  保留 pending row 並以固定、有限間隔重試；`CancelledError` 不攔截，確保 shutdown/cancellation 可終止 task。

## 替代方案

- **沿用 P8-A user token**：拒絕；會把前景 human grant 當長期 workload credential。
- **把 client secret 放入 MCP config/header**：拒絕；會穿過 UI、backup、registry 與診斷面。
- **以 permit TTL 當 send 完成**：拒絕；到期不能證明 socket write 或 task 已退出。
- **多 worker 加 file lock 即可**：拒絕；file lock 只保證單一 owner，不能共享 process-local permit/task。

## 驗證

- 後端：topology、profile/delegation CAS、encrypted store、metadata、exchange、audit、permit/drain、
  revoke/rotation/recovery、authority router、manager ticket、lifespan、API 與 backup/restore TDD。
- 前端：admin secret one-shot、status/recovery/rotation、owner delegation server-derived source binding、
  MCP form policy preservation、i18n、typecheck 與 lint。
- 第四版最終證據：workload 聚焦 **142 passed**、最終邊界精確套件 **159 passed**、完整後端
  **4552 passed／5 skipped**、ToukaChat **1150 passed／228 files**、guards **44/44**；兩個 CVE audit
  均為 0 vulnerabilities，Ruff F/I、MkDocs、Compose config 與 OpenAPI/generated types 全綠。
- Phase 只有最終審查與維護者的 commit 核准都通過後才可標示完成；本 ADR 的 accepted 表示決策已核准，
  不代表目前候選已獲最終複審或已發布。
- 2026-08-08 Phase 3 聚焦證據：26 個 workload test files 共 **181 passed**；包含 exact durable
  owner/CAS、public rotation serialization、crash/cancel blocker、source timeout→restart→finalize、
  同 source 多衍生 delegation 逐一收斂、retry 的 exact policy delete、legacy quarantine，以及
  profile/delegation 同 generation exactly-one-winner contention。Ruff E/F/I
  與 `git diff --check` 全綠，完整後端 **4604 passed**；44 guards 合併證據為 **43/44**，唯一阻擋
  是 2026-08-08 production npm audit 新回報 5 項弱點（3 high、2 moderate）。Phase 4、弱點處置與
  最終審查仍待後續 phase。
- 2026-08-08 Phase 4：兩個獨立 Python subprocess 同時競爭同一 temporary lock，exactly one
  `ACQUIRED`、另一個 `BUSY`；取消 owner wait 並完成 `aclose()` 後，新 subprocess 可立即取得鎖。
  這是既有 OS lock 行為的補證，沒有為了製造 RED 更動產品 lock。新增審查用的 builder/verifier 以
  content diff＋展開 untracked 建 manifest，將 normalized Git object 等於 base 的 status-only path
  另列 diagnostic；exact binary patch 可跨 Windows EOL 重建，另保留 human-readable review patch。
  tracked／review／untracked patch sections、manifest、live tree 與隔離重建的 size/SHA-256 必須全等；
  `data/` 在 Git pathspec 即排除，seal 後任何缺檔、多檔或 hash drift 皆 fail-closed。
  最終 Phase 4 聚焦 **39 passed**、完整後端 **4622 passed／5 skipped**、ToukaChat
  **1150 passed／228 files**、候選 Ruff（94 個 Python path）、MkDocs 與 diff check 全綠；
  pip-audit 通過，44 guards 為 **43/44**。唯一紅燈是 production npm audit 的 5 項
  弱點（3 high、2 moderate），因此尚未建立／封存最終 immutable 審查 package，也未進複審。
