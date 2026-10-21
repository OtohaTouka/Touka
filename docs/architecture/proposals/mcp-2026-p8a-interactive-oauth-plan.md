# MCP 2026 P8-A：互動式 OAuth authorization-code 實作計畫

> **狀態**：owner 已回報 HUMAN_ONLY DEV-B 通過並已 commit；P8-B successor candidate 見 ADR-0207
>
> **風險**：HIGH（認證、憑證、SSRF、跨程序一致性、瀏覽器 callback）
>
> **上位提案**：[MCP 2026-07-28 protocol migration](mcp-2026-07-28-protocol-migration.md)
>
> **修訂日期**：2026-08-03

## 1. 已確認的 owner 決策

1. OAuth credential 使用動態 get_config_dir() 下的加密 SQLite；不得位於或碰觸 data/。
2. 敏感 payload 使用專用 TOUKA_MCP_OAUTH_KEY 與 AES-256-GCM；不得重用 JWT、Dispatch 或 Heartbeat 金鑰。
3. 一般備份排除 OAuth SQLite main/WAL/SHM 與 current／previous OAuth key。
4. P8-A 只做互動式 authorization-code；client credentials、enterprise authorization、無人值守 heartbeat delegation、DPoP、JWT bearer、durable MRTR 均屬 P8-B 或後續工作。
5. Scenario 8 的可執行保證縮限為：
   - raw state 不得留在 Touka 控制的 application/config storage、browser storage、log、trace、error、backup、prompt、tool argument、popup message 或 callback 當前 history entry。
   - raw state 只允許作為一次性協定資料出現在 outbound authorization URL 與 inbound callback。
   - 授權伺服器或瀏覽器自行管理的跨來源歷史不在 Touka 可控制範圍；不得再宣稱 Touka 能刪除它。
   - 殘餘風險以高熵、短 TTL、HMAC-only server lookup、原子單次消耗與 callback 立即清理降低。
6. 第 2 輪是自動修正／重審上限。本文件吸收審查意見，但不等同新 reviewer PASS；進入 TDD 前仍須 owner 明確核准 DEV-A。

## 2. 使用者可見驗收場景

### Scenario 1：owner 連接 OAuth MCP server

Given 已驗證的人類 owner 正使用 auth_mode=oauth_user 的安裝<br>
When owner 啟動授權、必要時明確選擇 authorization server，並在 popup 完成同意<br>
Then Touka 只把該 human＋execution identity＋installation generation 的 authorization slot 標為 connected，後續 MCP request 使用該 slot 的 active grant，browser 永遠拿不到 token。

### Scenario 2：startup 不阻塞

Given OAuth server 沒有可用 credential<br>
When Touka 啟動<br>
Then lifespan 正常完成，connector 只回報 auth_required；不得做 discovery、refresh、開 browser 或等待 callback。

### Scenario 3：身分與 generation 完整隔離

Given 不同 human、同 human 的不同 soul、不同 installation 或 generation 各自授權<br>
When 任一方執行、refresh、status 或 disconnect<br>
Then credential、lease、error、audit、fallback 與 deletion 都不得跨 authorization slot 或 full grant tuple。

### Scenario 4：不安全 discovery／resource egress fail closed

Given metadata、token endpoint 或 credential-bearing MCP resource 使用不安全 scheme／port、解析至受限位址、發生 DNS rebinding、redirect、oversized body、wrong content type 或安全 metadata 漂移<br>
When Touka 將送出 code、client secret、refresh token、revocation token 或 bearer token<br>
Then 在送出 credential 前拒絕或 freeze exact grant，回傳消毒 ErrorCode，不 fallback 至 static、其他 principal 或其他 origin。

### Scenario 5：refresh 與 DCR 跨 worker 可收斂

Given 多個 worker 同時 refresh 或註冊同一 authorization slot<br>
When lease、network response、CAS commit 或 process crash 交錯<br>
Then 同一 live lease 只有一個 network winner；只允許一個 authoritative local result，stale response 永遠不能覆寫、刪除或啟用較新的 grant／registration。

### Scenario 6：callback 抵抗 replay／mix-up

Given callback 的 state 已過期／消耗、route／method／response mode 不符、PKCE 非 S256，或 iss 違反 RFC 9207／MCP 四格規則<br>
When callback 到達<br>
Then 依本文件的 parser outcome table 決定是否查詢與消耗 state；一旦進入 semantic phase，CAS consume 必須早於 code exchange，且永不做 URL normalization 或 issuer fallback。

### Scenario 7：owner 精確 disconnect

Given authorization slot 有一個 active grant<br>
When 該 slot 的 human owner disconnect<br>
Then Touka 對 frozen exact revocation endpoint 作有界 revocation，最後只 tombstone/delete 該 grant 與 active pointer；其他 slot／generation 不受影響。

### Scenario 8：Touka 控制面不殘留 OAuth secret

Given 任一 OAuth success、failure、timeout、crash 或 rollback path<br>
When 檢查 Touka 控制的 config/application/browser storage、callback 當前 history entry、log、trace、API、prompt、tool argument、error、backup 與 popup message<br>
Then access／refresh token、authorization code、PKCE verifier、client secret 與 reusable raw state 均不存在；server 只保存 HMAC(state) 與加密 transaction payload。授權伺服器／browser 管理的跨來源歷史是明示的不可控邊界，不列入此保證。

## 3. 範圍、基線與禁止事項

### 3.1 可重用基線

- ProtocolServerConfig 已有 auth_mode／auth_profile_id。
- McpRequestContext 已承載 verified AgentIdentity、human_principal_id、installation_id、auth_level 與 PolicyContext。
- P1 已有 lazy principal-scoped handle；P3～P7 已有 modern negotiation、canonical data plane 與 ephemeral MRTR。
- none 與 static_headers 必須保留原 client、startup、pool、failure、shutdown 行為。

### 3.2 P8-A 不做

- 不實作 oauth_client_credentials 或 enterprise；這兩種模式必須 fail-closed 為 unsupported，不得誤入 oauth_user service、key store、discovery、refresh、resource transport 或 fallback。
- 不將 human OAuth token 委派給 heartbeat、Dispatch 或 headless request。
- 不遷移既有 static header credential。
- 不支援 cleartext HTTP bearer resource；即使 loopback／private origin 已 allowlist，credential-bearing resource 在 P8-A 仍必須 HTTPS。
- 不新增 token export、secret debug endpoint、browser token storage 或 data/ migration。
- 不自動刪除 owner 的 OAuth DB／key，不自動 publish／deploy。

## 4. SOLID 邊界與 SSOT

### 4.1 Protocol／service 邊界

新增窄介面，全部以 typing.Protocol 注入：

- OAuthTransactionRepository：state lookup／CAS consume／expiry／cancel。
- OAuthGrantRepository：slot pointer、grant、refresh lease、exact delete／freeze。
- OAuthRegistrationRepository：CIMD／DCR registration lease、version、replacement。
- OAuthProfileRepository：非秘密 profile 的驗證、generation、fingerprint 與 mutation fence。
- OAuthKeyRingProvider：managed／external key mode、current／retiring key、reload epoch。
- OAuthOutboundClient：metadata、token、DCR、revocation 與 credential-bearing resource transport。
- OAuthClock／OAuthRandomSource：Time SSOT epoch、monotonic wait 與 CSPRNG。
- ConnectorAuthService：唯一 FSM／authorization-slot／grant lifecycle 編排者。

Router、ProtocolManager、ToolExecutor 與 UI 只能依賴上述介面或 generated API contract，不直接碰 SQLite、AES、HTTP transport 或 browser storage。

### 4.2 SSOT 分工

| 定義 | 唯一 SSOT |
|---|---|
| API request／response／status | ToukaProxy Pydantic models → openapi.json → generated api.ts |
| ErrorCode 與 i18n key | errors.json |
| OAuth feature gate／limits／key env | ToukaProxy src.config 與 .env.example |
| 非秘密 profile／scope policy | OAuthProfileRepository 的 Pydantic document |
| runtime profile fence、transaction、slot、grant、registration | get_config_dir()/mcp_oauth.sqlite3 |
| current／retiring secret keys | OAuthKeyRingProvider；不得在其他 service 解析 env |
| time | src.utils.time_utils；elapsed wait 用 injected monotonic clock |
| permissions | PermissionService／PolicyContext；OAuth scope 只作額外交集 |
| callback route／method／response mode | 全域 OAuthCallbackPolicy；profile 只能引用已核准 binding |
| verification command | scripts/verify_mcp_oauth_p8a.py |

## 5. Stable authorization slot 與 full grant tuple

### 5.1 Stable slot

所有 status、start、reauthorize、token lookup 與 disconnect 先從 verified request 建立 OAuthAuthorizationSlotKey：

1. human_principal_id（interactive OAuth 必填）；
2. effective execution AgentIdentity.agent_id；
3. 若存在顯式 delegation，使用 verified delegation subject／generation digest；
4. installation_id；
5. installation_generation；
6. auth_profile_id。

slot key 不含 runtime 才取得的 issuer、client ID 或 granted scope，因此 server＋principal API 能決定性定位。其 canonical length-prefixed bytes 只以 HMAC index 落庫；不得接受 caller 自述 human／agent／generation。

每個 slot 只有一個 versioned active_grant_id pointer。沒有 pointer 等於 auth_required；多於一個 active row 是 corruption，fail closed，不掃描挑第一個。

### 5.2 Full grant tuple

OAuthGrantBinding 包含：

- slot key 全欄位；
- exact issuer；
- canonical resource；
- client_id；
- grant_family 固定為 authorization_code；refresh 是 lifecycle operation，不把 tuple 改成 refresh_token；
- exact granted-scope set；
- profile generation／fingerprint；
- registration version；
- metadata security fingerprint。

grant_id 是 CSPRNG 128-bit opaque ID，grant_version 單調遞增。所有 handle 都帶 slot digest＋grant_id＋grant_version；active pointer 移動後，舊 handle 必須在 network egress 前被 fence。

### 5.3 Mutation 規則

- 首次授權：transaction 綁 slot version；credential commit 與 active pointer 建立在同一 SQLite transaction。
- reauthorization／scope narrowing／DCR replacement：建立新 full tuple；CAS 移動 pointer，同 transaction tombstone 舊 grant。
- metadata/profile/generation change：不以 partial tuple 找替代品；freeze active grant 並回 auth_required。
- status／disconnect：只解析 stable slot → active pointer → exact grant；禁止 whole-table scan 或 issuer/client/scope fallback。
- ambiguity、stale slot version、stale grant handle、pointer mismatch 一律 fail closed。

## 6. Runtime store、key ring 與 rotation

### 6.1 SQLite schema

同一 runtime DB 至少含：

- oauth_transactions；
- oauth_slots；
- oauth_grants；
- oauth_registrations；
- oauth_profile_fences；
- oauth_schema_migrations。

敏感 payload、exact tuple 與 client material 使用 AES-GCM；公開狀態、version、expiry、lease owner、HMAC index 可作索引但不得含秘密。所有 SQL 使用固定字面量與參數化值。

SQLite 使用短 transaction、BEGIN IMMEDIATE、busy timeout、version predicates；WAL 不可用時明示降級至受測 fallback，不得靜默改變 durability。blocking SQLite／crypto／fsync 經 async repository offload。

直接 import sqlite3 仍受禁令 11 約束。實作必須重用已核准 storage adapter；若確定需要新的 SQLite boundary，P8-A 的下一個連號 ADR 必須先明列單一 repository allowlist／精確 noqa，並同步 NOQA_REGISTRY 與 Guard，禁止把 sqlite3 import 散到 service／router。

### 6.2 State lookup

- DB 只保存 state_kid＋HMAC-SHA-256(state)；raw state 不進 encrypted payload。
- callback 以 current 與仍在 retiring 狀態的 bounded key ring 計算候選 digest，依 kid＋digest 精確查詢。
- 同一 digest 命中多列、跨 kid collision 或 payload binding 不符皆視為 corruption。
- transaction payload 保存 verifier、verifier hash、slot／profile／issuer／resource／redirect／response-mode binding 與 expiry。

### 6.3 Key source mode

OAuthKeyRingProvider 支援兩種互斥模式：

1. **Managed mode**：TOUKA_MCP_OAUTH_KEY_SOURCE=managed；resolved authoritative .env 是 SSOT。TOUKA_MCP_OAUTH_KEY 格式為 v1:kid:base64url-32-byte；TOUKA_MCP_OAUTH_RETIRED_KEYS 是 versioned JSON array。所有 .env writer 共用一個跨程序 secret-writer lock，並以 temp＋fsync＋atomic replace 持久化。
2. **External mode**：TOUKA_MCP_OAUTH_KEY_SOURCE=external；deployment 直接注入 non-empty key ring。應用內 bootstrap／rotation API 停用；operator 必須以 coordinated restart 切換，避免程序看到不同 key。

source mode、格式或檔案權限不一致時 OAuth unavailable；不得 fallback 至臨時 key 後寫 credential。

### 6.4 Rotation state machine

| Phase | Write | Read | 退出條件 |
|---|---|---|---|
| stable | current | current | owner 啟動 rotation |
| prepare | old | old＋new validation | new key crash-safe 持久化 |
| dual-read-new-write | new | new＋old | 所有 payload row version-CAS re-encrypt 完成 |
| state-drain | new | new＋old HMAC | old-kid pending state 全部 consumed／cancelled／expired，且超過最大 state TTL |
| worker-converge | new | new＋old | rotation start 只 snapshot lease 尚存活的 worker；其皆回報已載入 key epoch才可退出 |
| retire | new | new | DB 無 old kid／old encrypted row，驗證通過後才移除 old key |

- 一次只允許一個 rotation；上一個 retiring key 尚未符合退出條件時拒絕再轉。
- state digest 不 rekey，因 raw state 不保存；舊 HMAC lookup key 必須留到 state-drain。
- crash 後由 persisted phase 恢復；prepare 前可 rollback，進入 new-write 後 rollback 必須恢復 dual-read 並重新驗證所有 row，不得直接丟棄 new key。
- worker heartbeat lease 的 SSOT 為 `OAUTH_WORKER_LIVENESS_SECONDS`，不得短於三個
  maintenance interval；過期 worker row 在 snapshot 前清除，避免 per-boot worker ID 永久阻擋。
  過期 worker 若恢復，必須先 reload authoritative dual ring，再加入當前 convergence set。
- wrong key、mixed epoch 或退休條件不確定時 fail closed。

## 7. Callback policy、parser 與 state consumption

### 7.1 Callback binding

OAuthCallbackPolicy 是 deployment-wide 非秘密 SSOT：

- root 與 /v1 callback 都存在，但每個 transaction 綁一個 exact redirect URI／route。
- 兩個 parity route 共用一個 expected response_mode 與 HTTP method：form_post → POST application/x-www-form-urlencoded；query → GET。
- profile 只能引用 callback binding，不得自行讓同一路徑同時接受 GET／POST。
- redirect URI 不從 Host、Forwarded 或 X-Forwarded-* 建立。
- query mode 預設關閉；只有 ingress canary 證明 access＋error log 對正常、malformed、oversized 與 pre-ASGI rejection 均不含 request target secret，profile 才能啟用。否則必須使用 form_post。

### 7.2 Parser limits

命名常數由 src.config 唯一定義：

- OAUTH_CALLBACK_MAX_BODY_BYTES = 16 KiB；
- OAUTH_CALLBACK_MAX_FIELD_BYTES = 8 KiB；
- OAUTH_CALLBACK_MAX_FIELDS = 16；
- state／code／iss／error 各最多一個且不得空白；
- exactly one of code or error；
- error_description／error_uri 可各出現一次，只作 bounded input，不進 log／API；
- 未知 extension field 在 aggregate bounds 後忽略，不保存、不記錄；
- mixed query＋form、wrong content type、wrong method、duplicate 或 ambiguous encoding 拒絕。

### 7.3 Deterministic outcome table

| Case | Lookup | Consume | HTTP／ErrorCode 類別 | Exchange | Replay |
|---|---:|---:|---|---:|---|
| wrong method／response mode | 否 | 否 | 405／MCP_OAUTH_CALLBACK_METHOD | 0 | 正確 method 仍可用 |
| wrong content type | 否 | 否 | 415／MCP_OAUTH_CALLBACK_CONTENT_TYPE | 0 | 正確 callback 仍可用 |
| oversized body／field／count | 否 | 否 | 413／MCP_OAUTH_CALLBACK_TOO_LARGE | 0 | 正確 callback 仍可用 |
| missing／empty／duplicate state、mixed source | 否 | 否 | 400／MCP_OAUTH_CALLBACK_INVALID | 0 | 不適用 |
| code＋error、都缺、duplicate semantic field | 否 | 否 | 400／MCP_OAUTH_CALLBACK_AMBIGUOUS | 0 | 正確 callback 仍可用 |
| well-formed state 不存在／expired／已 consumed | 是 | 否 | 400／MCP_OAUTH_STATE_INVALID | 0 | 永遠拒絕 |
| state 命中但 route／redirect binding 不符 | 是 | 否 | 400／MCP_OAUTH_CALLBACK_ROUTE | 0 | exact route 仍可用 |
| binding 正確，CAS winner | 是 | 是 | 進 semantic phase | 至多 1 | 後續全拒 |
| AS error response | 是 | 是 | 400／sanitized OAuth error | 0 | 拒絕 |
| iss 缺失／不符四格規則 | 是 | 是 | 400／MCP_OAUTH_ISSUER_MISMATCH | 0 | 拒絕 |
| owner／generation／profile／policy 在 consume 後失效 | 是 | 是 | 403 或 409／對應 ErrorCode | 0 | 拒絕 |
| code 正確但 exchange 失敗 | 是 | 是 | 502／sanitized upstream error | 1 | 不重換 code，需重新授權 |
| exchange 成功但 commit fence 失效 | 是 | 是 | 409／MCP_OAUTH_STALE_AUTHORIZATION | 1 | token 不落 active slot，需重新授權 |

parser rejection 不洩漏 state 是否存在。semantic phase 一律先 CAS pending→consuming，再驗 iss／code／error 與做 token I/O。

## 8. FSM

### 8.1 Transaction persisted states

| From | Trigger | To | Rule |
|---|---|---|---|
| pending | exact callback CAS winner | consuming | network exchange 前完成 |
| pending | TTL exact boundary now >= expires_at | expired | cleanup 或 callback lazy-expire |
| pending | profile／generation／owner change | cancelled | 不可恢復 |
| consuming | AS error／issuer error／policy fence | consumed_error | exchange count 0 |
| consuming | token exchange returned，commit 成功 | consumed_success | grant＋pointer 原子 commit |
| consuming | exchange failure／stale commit | consumed_error | authorization code 不重試 |

consumed_success、consumed_error、expired、cancelled 都是 terminal。

### 8.2 Grant persisted states

| From | Trigger | To |
|---|---|---|
| active | refresh lease winner | refreshing |
| refreshing | version-CAS rotation success | active(new version) |
| refreshing | lease timeout／transient failure | active 或 expired，依 token validity |
| refreshing | invalid_grant | tombstoned＋slot pointer cleared |
| active／refreshing | metadata、profile、DCR、generation fence | frozen |
| active／refreshing | now >= access expiry 且無 usable refresh | expired |
| active／frozen／expired | disconnect begins | revoking |
| revoking | bounded revocation outcome＋local delete | tombstoned |
| active／frozen／expired | reauthorization commit | tombstoned，被新 grant 取代 |

frozen／expired／tombstoned 不得 bearer egress。只有 slot pointer 指向的 exact active／valid grant 可用。

### 8.3 API derived status

API status 只由 persisted slot／transaction／grant 衍生：

- disabled；
- auth_required；
- discovering；
- selection_required；
- awaiting_user；
- exchanging；
- connected；
- refreshing；
- frozen；
- expired；
- error。

status 不保存第二份 FSM，也不依 process memory 推測。重複 start 在同一 pending transaction 回同一 opaque operation handle，不產生第二份 state；terminal 後 start 建新 transaction。

## 9. Profile、scope 與跨 worker mutation fence

### 9.1 非秘密 profile

Pydantic profile document 保存於專用 mcp_oauth_profiles.json，包含 immutable profile generation、callback binding、trusted public origin、CIMD、DCR issuer allowlist、private origin/CIDR exception、scope policy與 enabled flag；不含任何 credential。

只有 verified human system administrator 可 mutation，且必須 JWT＋verify_origin。一般 human 只能對自己 slot start／select／status／disconnect；admin 不因管理權自動取得別人的 token。

### 9.2 跨程序 profile mutation

JSON 與 SQLite 不宣稱可跨檔原子 commit；改採 fail-closed mutation intent：

1. mutation 與所有 credential egress 共用 per-profile inter-process lock；
2. mutation 先在 SQLite 將 profile fence 設為 updating 並升 epoch；
3. 以 ResourceManager 的 atomic JSON write 寫新 profile；
4. 同一 lock 內以 SQLite transaction 更新 applied fingerprint、cancel pending transaction、freeze bound grant／registration，再把 fence 設 active；
5. crash 留下 updating 時，所有 worker 拒絕 egress；reconciler 只能在同一 lock 下完成或回復，不得採 cached profile；
6. egress 持有同一 lock，重新讀 JSON＋SQLite epoch／fingerprint，直到 hardened transport 回報 Authorization header 已送入已驗證 socket 才釋放；private CIDR 亦由同一次讀取投影到 request extension，禁止快照於長生命 handle。

因此 mutation 不會與 stale worker 的最後 credential write 交錯。所有 cache 只可作效能提示；每次 egress 必須比對 persisted epoch。

### 9.3 OAuthScopePolicy

操作 vocabulary 固定為 tools/call、resources/read、prompts/get。selector grammar：

- tools/call 與 prompts/get：exact case-sensitive descriptor name 或單一字元 *；
- resources/read：exact MCP descriptor URI string（只作識別字，不轉 filesystem path）或 *；
- 不支援 prefix glob、regex、case folding、URL normalization 或多個 *。

規則 precedence 只有 exact selector > 同 operation 的 *；重複 exact、重複 wildcard、未知 operation、空 scope、重疊表示或非法 selector 使整份 profile 驗證失敗。全域 default 永遠是 deny，不能配置成 allow。沒有 exact 或 operation wildcard 即 deny。

initial returned scope 必須是 requested set 子集；omitted 代表 requested。refresh omitted 保留既有 granted set；explicit subset 建新 grant tuple並移動 active pointer；expansion 拒絕。scope token case-sensitive、order-insensitive，不做 normalization。

執行允許條件是：

1. current RBAC／PaC／auth_level 允許；
2. installation tool policy 允許；
3. scope rule 存在；
4. required scopes 是 active exact granted set 的子集。

任一項否決即拒絕；OAuth scope 不擴權，Touka permission 也不能替代缺少的 scope。

## 10. CIMD／DCR lifecycle

### 10.1 優先順序

依 MCP authorization draft，CIMD／pre-registration 優先，DCR 只作具名 trusted issuer 的 deprecated compatibility fallback。CIMD client ID、document HTTPS URL、redirect、grant／response type與 token auth method必須 exact 驗證；DCR 另須依 callback deployment 正確送出並驗證 application_type。

### 10.2 DCR registration slot

stable registration key 為 profile ID／generation＋installation ID／generation＋exact issuer。registration row 包含 version、lease owner／expiry、attempt fence、client binding、secret expiry與 status。

- 無 crash 的 concurrent contenders：一個 lease、一個 outbound registration call、一個 authoritative commit。
- network call 在 DB transaction 外；response 只有 lease/version/attempt fence winner 可 commit。
- crash before network response：lease expiry後可重試；舊 process response 永遠 fenced。
- crash after response before commit：因 RFC 7591 無通用 idempotency key，重試可能產生 remote orphan；本機最多只有一個 active registration。此殘餘風險必須記錄，不能宣稱遠端 exactly-once。
- registration_client_uri／registration_access_token 在 P8-A 一律拒絕，不保存也不嘗試 management lifecycle。
- client_secret_expires_at 的 exact boundary now >= expiry 會 freeze 該 registration、pending transaction 與 bound grants。
- replacement 建立新 registration version；client identity 改變時舊 grant 全部 freeze，強制 reauthorization，不以新 client secret 繼續舊 authorization-code grant。

## 11. Hardened OAuth outbound／resource transport

OAuthOutboundClient 專用於 PRM、AS／OIDC metadata、JWKS、CIMD、DCR、token、revocation 與 bearer MCP resource。none／static_headers 保持原 client。

1. credential-bearing resource 與所有接受的 security metadata endpoint 必須 HTTPS；P8-A 沒有 cleartext private／loopback exception。
2. URL 禁 userinfo、fragment、zone ID、ambiguous host、invalid IDNA、disallowed port。
3. 每次新 TCP connection 解析全部 A／AAAA；任一受限位址使整組拒絕。private exception 必須 exact installation＋origin＋CIDR。
4. custom public httpcore2 network backend 只連 just-validated IP，同時保留原 hostname 作 TLS SNI／certificate 與 HTTP Host／:authority。
5. redirect、environment proxy、trust-env、cross-origin Authorization forwarding 與 HTTP/2 origin coalescing一律停用。
6. pool key 至少含 exact origin＋slot digest＋grant ID/version＋profile epoch；pointer move／freeze 立即淘汰舊 pool。
7. alternate address retry 只限同次已驗證集合，不重新解析後偷偷換集合；每次 retry 前重驗 slot／grant／profile／metadata fence。
8. connect/read/write/pool timeout、body bytes、JSON depth／item count、content type 都用 src.config 命名常數。
9. metadata使用 versioned presence-aware projection；缺值與 explicit null 不合併。影響 acceptance 的 PRM AS list、issuer、endpoint、response mode、PKCE、iss capability、token auth、scope、CIMD／DCR binding 任一變化都 freeze exact grant。
10. discovery／CIMD／DCR、token refresh、callback exchange 與 bearer resource 前都重驗
    feature、installation enabled/generation、human/owner、MCP grant 與 current profile；
    revoke 發生於 network call 前時不得再送 credential 或 registration metadata。

## 12. Logging、callback page、API 與 UI

### 12.1 Log boundary

- 所有 shipped Uvicorn entry point 將 built-in access log 關閉。
- Touka ASGI logger 只記 method、route template、status、request ID、duration；不記 raw path/query/header/body。
- logging filter 必須覆蓋 Uvicorn pre-ASGI parse/error logger，以 state/code canary 測正常、malformed、oversized、404、413、422、500。
- shipped nginx 對 exact callback location 關 access log；form_post body 不進 error log。若 query-mode canary 無法證明 nginx error path 不含 request target，shipped query mode保持 disabled。
- application exception、audit、OTel attribute 與 circuit breaker 只保存 ErrorCode／opaque IDs，不保存 upstream body、request target 或 header。

### 12.2 Callback page

callback response使用 no-store、no-referrer、frame-ancestors none、script nonce/hash、零 subresource。query mode 的 success、parser/runtime failure 與 runtime unavailable 均須先用 history.replaceState 清除 Touka callback 當前 entry；failure page 不發 postMessage，success page才對 exact configured ToukaChat origin 與 exact WindowProxy 發送只含 opaque outcome handle 的 message，最後關閉。不得傳 code、state、issuer、token 或 upstream detail。

這個清理不宣稱能刪除 authorization server／browser 管理的跨來源 history；文件與 UI 不得誤導。

### 12.3 API

- POST /mcp/servers/{server}/oauth/start 及 /v1 parity；
- exact-bound GET／POST callback parity；只有 OAuthCallbackPolicy 指定的 method 可進 parser；
- GET /mcp/servers/{server}/oauth/status 及 /v1 parity；
- DELETE /mcp/servers/{server}/oauth 及 /v1 parity；
- admin profile CRUD 使用獨立 route、verified human admin＋verify_origin。

start／status／disconnect 依一般 JWT、ownership、installation generation 與 MCP grant；callback只依一次性 state capability。所有 model 由 Pydantic 產生，錯誤走 ErrorCode SSOT。
start／disconnect 另須 verify_origin；status 是只讀。所有 mutation route 都不得只靠一般 get_verified_identity 取代角色／ownership guard。

### 12.4 Frontend

useMcpOAuth hook 只用 fetchTouka／generated contracts；UI 拆成 OAuth status、AS selection、popup controller 等小元件。popup correlation 只留 component memory，禁止 localStorage／sessionStorage／URL／console。exact origin＋source 驗證後才開始單一 bounded polling loop；unmount／server switch／duplicate connect 必須取消舊 loop。

## 13. Time、retry 與 deterministic bounds

| 常數 | 初始值／語意 |
|---|---|
| OAUTH_STATE_TTL_SECONDS | 600 |
| OAUTH_ACCESS_REFRESH_LEEWAY_SECONDS | 60 |
| OAUTH_REFRESH_LEASE_SECONDS | 30 |
| OAUTH_DCR_LEASE_SECONDS | 30 |
| OAUTH_POPUP_POLL_INTERVAL_MS | 1000 |
| OAUTH_POPUP_TIMEOUT_MS | 120000 |
| OAUTH_CLEANUP_INTERVAL_SECONDS | 300 |
| OAUTH_MAX_CLOCK_ROLLBACK_SECONDS | 5；超過即 freeze／fail closed |
| OAUTH_RETRY_MAX_ATTEMPTS | 3 |
| OAUTH_RETRY_BASE_MS／MAX_MS | 250／5000；jitter 由 injected source 決定 |
| OAUTH_DISCONNECT_REVOCATION_TIMEOUT_SECONDS | 5 |

- valid 唯一定義為 now < expires_at；now == expires_at 已失效。
- lease 在 now >= lease_expires_at 才能被新 contender 取得。
- durable deadline 使用 Time SSOT epoch；單程序 sleep／timeout 使用 monotonic。
- unit/integration 不以真 sleep 決定邊界，全部使用 injected clock／barrier。
- responsiveness oracle：slow repository fake 在 worker thread 等 threading.Event；event loop 必須在 worker 尚未釋放時完成至少 20 個 asyncio scheduling turns，取消後 1 秒內完成 cleanup；外層 5 秒 watchdog 只防 test hang，不作產品 latency 判準。

## 14. Crash／restart oracle

| Fault point | Restart 後 durable state | 允許 upstream call | Recovery／final invariant |
|---|---|---:|---|
| transaction committed，URL 尚未回傳 | pending | 0 | 可取消／expire；不得另生 state |
| state consumed，exchange 前 | consuming | 0 | terminal consumed_error；callback replay拒絕 |
| exchange response後，grant commit前 | consuming | 1 | 不重用 code；不建立 grant，owner重授權 |
| refresh lease acquired，request前 | refreshing＋lease | 0 | exact deadline後新 winner可接手 |
| refresh response後，CAS前 | refreshing＋lease/version | 每個有效 lease至多1 | stale response丟棄；不得覆寫新 version |
| grant rotation committed | active new version | 1 | old token／handle fenced |
| revocation response後，delete前 | revoking | 至多再1次 | RFC 7009 idempotent retry後 local exact delete |
| exact delete committed | tombstoned／pointer空 | 0 | status auth_required；不再 revocation |
| schema migration interrupted | migration journal＋old或new完整 schema | 0 | roll-forward／rollback；不得半 schema |
| DCR lease acquired，request前 | registering＋lease | 0 | deadline後接手 |
| DCR response後，commit前 | registering＋attempt fence | crash後可多1 | 本機只一個 authoritative registration；remote orphan明示 |
| profile updating intent後 | fence=updating | 0 credential egress | 同 lock reconcile，先 freeze再恢復 active |
| key dual-read-new-write 中 | persisted rotation phase | 0 額外OAuth call | 恢復 re-encrypt；不得退休仍需 key |

每個 subprocess test 使用 ready／release／response-received／commit-allowed IPC acknowledgement，不用 sleep 猜競態。

## 15. TDD 計畫與精確路徑

### 15.1 先建立仍為 GREEN 的 baseline characterization

- tests/unit/test_mcp_oauth_mode_baseline.py：
  - none／static_headers 不載入 OAuth key／DB，不碰 discovery／refresh／callback；
  - oauth_client_credentials／enterprise 明確回 unsupported_auth_mode，不進 oauth_user service；
  - startup、routing、pool、headers/redaction、failure、concurrency、shutdown 舊行為。

### 15.2 Backend RED files

- tests/unit/test_mcp_oauth_store.py；
- tests/unit/test_mcp_oauth_keyring.py；
- tests/unit/test_mcp_oauth_profile.py；
- tests/unit/test_mcp_oauth_outbound.py；
- tests/unit/test_mcp_oauth_protocol.py；
- tests/unit/test_mcp_oauth_service.py；
- tests/unit/test_mcp_oauth_secret_surfaces.py；
- tests/integration/test_mcp_oauth_mock_as.py；
- tests/integration/test_mcp_oauth_multiprocess.py；
- tests/integration/test_mcp_oauth_dcr_multiprocess.py；
- tests/integration/test_mcp_oauth_backup.py；
- tests/integration/test_mcp_oauth_logging.py。

RED collection 必須成功；只允許因缺 model／service／route 或行為 assertion 失敗。import、syntax、fixture、dependency、collection error 都不算 RED。

### 15.3 Frontend／browser RED files

- ToukaChat/src/hooks/settings/__tests__/useMcpOAuth.test.ts；
- ToukaChat/src/components/settings/tabs/mcp/__tests__/McpOAuthStatus.test.tsx；
- ToukaChat/src/components/settings/tabs/mcp/__tests__/McpOAuthSelection.test.tsx；
- ToukaChat/e2e/mcp-oauth.spec.ts。

Playwright 只斷言 Touka 可控制的 callback current entry 已清理、storage／console／popup message 無 secret；不得斷言跨來源 authorization-server history 被刪除。

### 15.4 Secret canary oracle

測試為 access token、refresh token、code、verifier、client secret、state 各植入唯一 canary，檢查：

- config／Pydantic serialization；
- structured log、Uvicorn／nginx error path、trace／OTel、audit；
- API model、callback HTML、exception；
- prompt builder、ToolExecutor args、MCP serialized request、retry payload；
- browser localStorage／sessionStorage／console／callback current history；
- backup ZIP／archived .env。

唯一 raw-state例外是測試內明列的 returned authorization URL 與 inbound callback fixture；任何其他命中都失敗。

## 16. Hermetic commands

從 repo root：

    .\.venv\Scripts\Activate.ps1
    python --version
    python -m pytest tests/unit/test_mcp_oauth_mode_baseline.py -q
    python -m pytest tests/unit/test_mcp_oauth_store.py tests/unit/test_mcp_oauth_keyring.py tests/unit/test_mcp_oauth_profile.py tests/unit/test_mcp_oauth_outbound.py tests/unit/test_mcp_oauth_protocol.py tests/unit/test_mcp_oauth_service.py tests/unit/test_mcp_oauth_secret_surfaces.py tests/integration/test_mcp_oauth_mock_as.py tests/integration/test_mcp_oauth_multiprocess.py tests/integration/test_mcp_oauth_dcr_multiprocess.py tests/integration/test_mcp_oauth_backup.py tests/integration/test_mcp_oauth_logging.py -q
    python scripts/verify_mcp_oauth_p8a.py

從 ToukaChat：

    npm run test -- --run src/hooks/settings/__tests__/useMcpOAuth.test.ts src/components/settings/tabs/mcp/__tests__/McpOAuthStatus.test.tsx src/components/settings/tabs/mcp/__tests__/McpOAuthSelection.test.tsx
    npx playwright test e2e/mcp-oauth.spec.ts --project=chromium

scripts/verify_mcp_oauth_p8a.py 是 hermetic SSOT，必須：

- 驗 Python 3.12、locked workspace、Node/npm/Chromium；
- 建立 temp config/workspace roots，bootstrap只在指定 temp case開啟；
- 清除 proxy env，patch DNS／socket 預設拒絕所有 external network，只允許 test fixture 明列的 loopback TLS endpoints；
- 對任何解析至真實 data/ 的路徑 fail-fast；
- 執行上述 targeted tests、Ruff check/format、ToukaChat ESLint/full Vitest、Playwright、guard_all、guard_security、guard_dependencies、full pytest、MkDocs、nginx config/canary、mock-AS/container/lifespan smoke、backup ZIP inspection；
- 保存 RED／GREEN 原始輸出供 DEV-B；
- dependency 變更只走 pyproject → uv lock → uv sync --all-packages，嚴禁裸 uv sync。

**實作結果（2026-08-01）**：verifier 以 `sitecustomize` 在 Python DNS／socket
層預設拒絕非 loopback egress，並將 config、workspace、uv/npm cache、MkDocs
輸出與 evidence 全部錨定於系統 temp；Windows 的 npm/npx `.cmd` 與 venv PATH
亦由 verifier 決定性解析，不依賴外層 shell。repo 沒有獨立 `guard_security.py`，
安全閘實際由 `guard_all.py` 的 44 guards、P8-A secret/security targeted tests 與
完整 pytest 覆蓋，未建立名稱存在但不執法的空殼 guard。2026-08-01 首輪因 Docker
daemon 未啟動，只完成 nginx 靜態 canary 與 Compose config，未宣稱真環境 smoke。

**真環境補驗（2026-08-02）**：以
`nginx@sha256:4a73073bd557c65b759505da037898b61f1be6cbcc3c2c3aeac22d2a470c1752`
唯讀掛載 shipped config 與一次性自簽憑證，`nginx -t` 通過；兩個 callback exact
location 均由 mock upstream 回 204，控制路徑確實進 access log，而 callback 路徑與
fake state/code canary 均未進 log。精確 Proxy image
`sha256:3af50ecd36b6d11894a6f37afdda0804564c5bf71f79f3323ac478197767d81c`
以零掛載、`network=none` 啟動，281/281 個 `ToukaProxy/src/*.py` SHA-256 與 HEAD
一致，lifespan、`/health`、14 個 OAuth production imports、8 個 OpenAPI parity path
與 access-log 關閉均通過。smoke 同時揭露兩份 dev Compose command 覆寫 Dockerfile
CMD 時漏掉 `--no-access-log`；新增回歸斷言先 RED，再只修正 root／standalone Compose
為 GREEN。DEV-B 第 1 輪修正後 verifier 重新全跑為 targeted 195、pytest 4392／5 skipped、Vitest
1144／225 files、Playwright 1、guards 44/44、dependencies 4/4、Ruff／ESLint／MkDocs／
Compose config 全綠；第一次以 `C:\tmp` 作 child temp 因 Windows sandbox EPERM 停在
Vitest collection，改用 repo 外 session artifact root 後通過，失敗證據保留。

## 17. Backup／restore 與 rollout

### 17.1 Backup

- backup manifest明列排除 DB／WAL／SHM。
- archived .env 是記憶體中的 filtered copy；不得修改 live .env。
- filter移除 current／retiring key與 rotation metadata。
- real ZIP test驗 entry name與內容。

### 17.2 Restore

- clean destination：OAuth DB/key不存在，restore 後必為 auth_required。
- populated destination：預設 fail closed 為 MCP_OAUTH_RESTORE_CONFLICT；不得宣稱 archive exclusion 會刪掉既有 credential。
- 只有 owner 明示 reset／quarantine 選項才可移動既有 OAuth DB／WAL／SHM 與 key；不自動刪除。操作需列出目標、建立可回復 quarantine manifest，再回 auth_required。
- preserve-existing 模式若未獲明示不得執行；一旦使用，狀態必須標示 preserved_existing，不能宣稱 forced reauthorization。

### 17.3 Feature gate／rollback

src.config 的 TOUKA_MCP_OAUTH_USER_ENABLED 是 global kill switch，預設 false；profile 另有 enabled。

Rollback順序：

1. 設 global gate false並 rolling restart；
2. status回 MCP_OAUTH_DISABLED／auth_required，拒絕新 start、callback commit、refresh與 bearer egress；
3. cancel pending transaction、close OAuth handle/pool，但保留 encrypted DB/key；
4. smoke none／static_headers 舊路徑；
5. 確認無 in-flight credential egress後才允許 code rollback。

rollback 不 migration token 至 config、不刪 DB/key、不 fallback他人/static。

## 18. 文件、ADR 與交付

實作時先重查 ADR index，再建立當時的下一個連號 ADR，記錄：

- stable authorization slot／active grant pointer；
- encrypted store與 key-ring rotation；
- callback parser／logging／Scenario 8可控邊界；
- profile mutation fence與 scope algorithm；
- hardened DNS/TLS resource transport；
- DCR residual orphan與 restore policy。

同步更新 SECURITY、PROXY_INTERFACE、CHAT_INTERFACE、SSOT_MASTER_MAP、ADR index、deployment/nginx、backup/restore、.env.example、errors.json、OpenAPI/generated types、tests README、CHANGELOG、HANDOVER與上位 MCP proposal。

交付順序固定為：DEV-A owner核准 → baseline GREEN → backend/frontend RED → minimal GREEN → refactor → hermetic/full verification → 文件 → immutable DEV-B bundle → owner選擇 DEV-B模式。Agent 不自行 commit。

## 19. 第 2 輪 finding traceability

| 審查問題 | 本版處理 |
|---|---|
| browser global history保證不可實現 | §1、Scenario 8、§12.2、Playwright oracle縮限 |
| full tuple無 deterministic locator | §5 stable slot＋active grant pointer |
| key rotation找不到舊 pending state | §6.2、§6.4 retained HMAC keys＋state-drain |
| FSM／callback consume語意不完整 | §7 outcome table＋§8 persisted FSM |
| bearer resource可能cleartext | §3.2、§11 HTTPS-only，無例外 |
| P8-B exclusion無回歸鎖 | §3.2、§15.1 explicit unsupported baseline |
| rollback不可執行 | §17.3 global kill switch＋順序＋smoke |
| DCR缺cross-worker/crash proof | §10、§14、§15.2 |
| prompt／tool args無secret oracle | §15.4 |
| crash point無精確預期 | §14 |
| time／responsiveness無界線 | §13 |
| RED／hermetic command不可重現 | §15、§16 |
| callback method／response mode未綁 | §7.1、§7.3 |
| scope default／pattern矛盾 | §9.3 exact／single-*／default-deny |
| profile JSON與SQLite無原子fence | §9.2 updating fence＋cross-process lock |
| backup exclusion不等於in-place reauth | §17.2 conflict／explicit quarantine |
| DCR expiry／replacement不明 | §10.2 freeze＋reauthorization |
| pre-ASGI／nginx error log洩漏 | §12.1 canary與query fail-closed |

## 20. 參考標準

- [MCP draft authorization](https://modelcontextprotocol.io/specification/draft/basic/authorization)
- [MCP 2026-07-28 release candidate](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)
- [RFC 9207：Authorization Server Issuer Identification](https://datatracker.ietf.org/doc/html/rfc9207)
- [RFC 9700：OAuth 2.0 Security Best Current Practice](https://datatracker.ietf.org/doc/html/rfc9700)
- [RFC 7591：Dynamic Client Registration](https://datatracker.ietf.org/doc/html/rfc7591)
- [RFC 7009：Token Revocation](https://datatracker.ietf.org/doc/html/rfc7009)

## 21. DEV-A 第 2 輪治理紀錄

| 欄位 | 紀錄 |
|---|---|
| Review mode | ADVERSARIAL |
| Review round | 2 |
| Reviewers | 3 個全新、唯讀、禁止 delegation 的 Codex CLI sessions |
| Requested model | gpt-5.6-sol，reasoning high |
| Actual model | unknown；CLI event contract 未提供 positive attestation |
| Fallback | not_observed；JSONL 無 fallback／reroute event，不等同供應端證明 none |
| Immutable bundle | 7 個去密檔案；manifest SHA-256 794d19858c805c99605550b9feccbae7ab3aaf767d5c34003d19a88af25d7414 |
| Verdict | 3/3 CHANGES_REQUIRED；合計 2 BLOCKING／15 HIGH／5 MEDIUM |
| Reviewer qualification | BLOCKED；actual-model attestation 未知，因此不得標示 AI PASS |
| Human decision | 2026-08-01 owner 核准 Scenario 8 可控邊界縮限 |
| Current gate | owner 已核准修訂版 DEV-A；TDD、實作、本機驗證與真 nginx/container smoke 完成，等待 immutable DEV-B 與 owner 選擇複審模式；`349a126d` 已先包含 P8-A 累積內容，因此 DEV-B 必須以其 parent `b6e7d169` 至當前工作樹作完整候選，不改寫歷史，也不得自動啟動第 3 輪 |

## 22. DEV-B 第 1 輪治理紀錄（2026-08-02）

| 欄位 | 紀錄 |
|---|---|
| Review mode | ADVERSARIAL；owner 明示同意啟動 3 個 reviewer |
| Reviewers | 3 個全新、唯讀、禁止 delegation、彼此隔離的 Codex CLI sessions |
| Requested model | gpt-5.6-sol，reasoning high |
| Actual model／qualification | unknown／BLOCKED；CLI JSONL 無 positive model attestation，不得計為正式 AI approval |
| Advisory verdict | 3/3 CHANGES_REQUIRED |
| Confirmed fixes | 動態 authority egress fence（含 return-boundary 撤權 freeze exact grant）、none/static 零 key/DB I/O、request-time CIDR、query failure history scrub、worker lease、corrupt-row fail-loud |
| Rejected finding | `trust_env=False` 缺失不成立；MCP resource client 與 OAuth outbound client 都已明示關閉 environment proxy |
| Current gate | TDD 修正與完整 verifier 已通過；需重建 immutable DEV-B package，再由 owner 明示是否啟動第 2 輪或改 HUMAN_ONLY |

## 23. 收官與 P8-B successor（2026-08-03）

Owner 已回報 P8-A 以 HUMAN_ONLY 通過並完成 commit；本文件前述 DEV-B 紀錄保留為歷史證據，
不再是當前 gate。P8-B 不修改本計畫的 P8-A 邊界，而以
[ADR-0207](../adr/0207-mcp-workload-oauth-single-proxy.md) 與
[P8-B 第三版計畫](mcp-2026-p8b-workload-oauth-plan.md)建立獨立 workload store/key/audit、
HEARTBEAT-only direct client credentials 與 exact owner delegation。enterprise／SSO 仍未支援。
