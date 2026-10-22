# MCP 2026-07-28 完整遷移實作計畫

> **當前狀態（2026-10-08）**：P0～P8 已完成各自選定範圍；ADR-0221 modern-only 已生效，Dispatch `/sse`
> 與 IDE `/mcp/sse`／`/mcp/messages` 退役，modern request-scoped SSE 保留。P11 原規劃的 rollout evidence
> producer、trusted verifier／qualification、release authority、本機發版 profile 與冷切換程序已依
> [ADR-0238](../adr/0238-retire-release-qualification-machinery.md) 整體刪除；發行改為最小 GHCR workflow。
>
> **仍未交付的選配**：P7-B durable MRTR、P9 Tasks（HOLD）、P10 Apps、企業 SSO 與
> `private_key_jwt`／DPoP／JWT bearer。P9 的上游結論保留原查證日期，本次只同步既有決策，未重新查證上游。
> ToukaIDE `0.3.0`（modern-only）已在 Visual Studio Marketplace 公開，發行步驟見
> [IDE 發行指南](../../guides/toukaide-marketplace-release.md)。
>
> **目標**：ToukaProxy MCP client、ToukaDispatch／Grok Imagine／ToukaIDE 自有 MCP server、Creation Engine
> 與 ToukaChat host 支援 `2026-07-28`。選定本機範圍已退役 legacy HTTP+SSE，相容期為歷史；
> 保留 modern request-scoped SSE，OAuth、Tasks、MCP Apps 等各按其獨立能力閘宣告。
>
> **本文件性質**：這是實作計畫，不是已落地 ADR，也不預占 ADR 編號。每個實作批次通過維護者核准後，才依連續編號規則建立 ADR、更新索引及對應 SSOT 文件。
>
> **與既有提案的關係**：本計畫保留 [`mcp-connector-parity-and-auth-alignment.md`](mcp-connector-parity-and-auth-alignment.md) 已落地批次 1～3 與 ADR-0191／0192／0194 的歷史事實；但以 2026-07-28 正式規格與 Python SDK v2.0.0 重新定義尚未實作的 OAuth 批次 4、協定生命週期、server 遷移與 extension 路線。若兩份提案對未來工作有衝突，以本文件為準。

## 1. 執行結論

這不是把 `mcp~=1.26.0` 改成 `mcp~=2.0` 就能完成的升版。Touka 現有邊界把 MCP 工具結果壓成文字、永久快取 `tools/call`、以伺服器名稱維持全域 session；Python 自有 server 與 Creation Engine 依賴 v1 `FastMCP`／`initialize()`，ToukaIDE 則依賴 Node SDK v1 與 legacy HTTP+SSE。若直接升依賴，最可能出現的是：

1. import 與欄位名稱立即破壞；
2. 舊連線可靠性修復被機械重寫掉；
3. `structuredContent`、`isError`、`resultType` 與 MRTR 在 Proxy 邊界遺失；
4. 私有工具目錄或工具結果跨使用者快取；
5. OAuth token、MRTR continuation、Tasks 或 Apps 繞過 Touka 的 Gate 1／Gate 2；
6. 對外宣稱支援 Extensions，但實際只解析部分成功路徑。
7. uv 單一 workspace 先換 SDK、後改 v1 import，造成測試全紅或 Dispatch 容器 restart loop。
8. 既有 Creation Engine 產物被新 SDK 破壞，卻因不覆寫主權而無法自動修復。

因此採用「**先固定語意邊界，再做原子 SDK cutover；先完成 base conformance，再逐一啟用 feature family 與 extension**」的遷移方式。Python SDK v2 cutover 必須把依賴、所有 v1-only import、Creation smoke/scaffold 與 server constructor 放在同一批，不能拆成必紅的中間狀態。runtime rollout 則使用 SDK v2 的 `Client(target, mode="auto")` 與 `MCPServer` 保留新舊協定相容，不做流量 big-bang。

### 1.1 「完全支援」的定義

| 宣稱 | 完成定義 | 是否規格必要 |
|---|---|---|
| A. `MCP 2026 base conformant` | JSON-RPC base、versioning、per-request metadata、request/response、MRTR、subscribe/notify，以及 Touka 選定的 stdio／Streamable HTTP transport 全部符合規格 | **MUST** |
| B. `Touka MCP host product complete` | ToukaProxy／ToukaChat 完整提供已選擇的 Tools、Resources、Prompts、HTTP authorization 等 feature family，且仍經 Gate 1／Gate 2 | Touka 產品必要；規格上為 MAY／HTTP auth 為 SHOULD |
| C. `Touka-owned servers migrated` | ToukaDispatch、Grok Imagine、ToukaIDE 與 Creation Engine 新產物能服務 `2026-07-28`，並在相容期服務舊 client；不再新增 legacy HTTP+SSE transport | Touka 產品必要 |
| D. `[extension] supported` | Tasks、MCP Apps、OAuth Client Credentials、Enterprise-Managed Authorization 逐項協商、逐項測試、逐項宣告 | 選配 |
| E. `legacy compatibility available` | 舊協定 initialize/session 與 legacy HTTP+SSE 只在有期限的相容層維持，不把棄用功能帶入新設計 | 過渡必要 |

Resources、Prompts、Tools、Authorization 與各 Extensions 都不是 base protocol 的共同 MUST。「base conformant」與「Touka 產品功能完整」是兩個不同的 release gate。Touka 只能宣告實際完成且通過測試的 capability；未完成的 feature/extension 不得放進每次 request 的 `clientCapabilities` 或 `server/discover`。

## 2. 不可破壞的不變量

1. **ToukaMemory 維持同行程整合模式**：禁令 1 與 ADR-0116 不變，不把內部 Memory 重新暴露為 MCP server。`ToukaMemory/pyproject.toml` 的 MCP 依賴若確認無使用，應移除，而不是跟著升版。
2. **Gate 1／Gate 2 仍是授權主權**：MCP 的 OAuth scope、server annotation 或 Apps permission 都只是外部約束，最後仍須與 Touka `PermissionService`、`PolicyContext`、`auth_level` 取交集。
3. **執行身分不可由 server 決定**：`serverInfo.name`、tool annotation、resource metadata 與 `_meta` 都是不可信外部資料；Touka 的 stable installation/server key 與 `AgentIdentity` 才是授權鍵。
4. **請求生命週期錨定不可退化**：一個已通過 Gate 1 的執行生命週期要固定使用同一個不可變 `AgentIdentity`；不得因 MCP 改為 stateless 就在重試、MRTR 或 task polling 時重建成字串身分。
5. **不得永久快取副作用工具呼叫**：`tools/call` 的完成結果不具備可重播或可共用語意。`ttlMs`／`cacheScope` 只適用規格列出的 list/read 結果，不能拿來合理化現有 `tools/call` 結果快取。
6. **失敗必須可見**：transport error、JSON-RPC error、`isError=true`、`input_required`、task failure 與降級必須保留結構化差異，不得壓成「看似成功的文字」。
7. **Creation Engine 保持唯一入口與不覆寫原則**：既有使用者生成 server 只做檢查與遷移報告；不得自動改寫、覆蓋或移動。
8. **不自動修改 `data/`**：本案不包含任何既有 `data/` 遷移。未來 token/task/runtime state 的實體位置必須先由維護者核准 SSOT，並透過 `ResourceManager` 或正式儲存抽象處理。
9. **HTTP redirect 預設關閉**：即使 SDK 範例預設 `follow_redirects=True`，Touka 仍依禁令 22 使用 `False`。若特定服務必要，必須驗證 `Location` 後套用具名 allowlist。
10. **MCP 新 header 不得成為洩密旁路**：`Mcp-Method`、`Mcp-Name`、`MCP-Protocol-Version` 交由 SDK 產生；`x-mcp-header` 實際映射成 `Mcp-Param-*`，不會直接覆蓋 Authorization，但可能把敏感 tool argument 暴露給 proxy/access log。必須限制 annotation、數量、名稱、單值與總 bytes，疑似 secret/PII 預設停用並可由 owner 逐工具核准。
11. **OAuth outbound policy 獨立於一般 remote URL 閘**：PRM、AS metadata、OIDC discovery、JWKS、CIMD/DCR、authorization/token/revocation endpoint 每一跳都要重新驗證；不得沿用只檢查 scheme/hostname、允許 DNS 殘餘風險的一般 helper。
12. **OTel 不繼承不可信 baggage**：跨外部 MCP 邊界只產生 Touka 自己的 trace context；預設丟棄對端 `tracestate`／`baggage`，外部 trace 最多作受限 link，不成為內部 trusted parent。
13. **圖示也是不可信網路輸入**：icon 只接受安全的 HTTPS／data URI；同源、無 credential fetch、禁止危險 redirect、MIME sniff、大小/維度/動畫上限，SVG 預設拒絕或經獨立 sanitizer。

## 3. 現況與缺口

### 3.1 依賴與 client

| 現況證據 | 缺口 | 遷移方向 |
|---|---|---|
| `ToukaProxy/pyproject.toml`、`ToukaDispatch/pyproject.toml`、`ToukaMemory/pyproject.toml` 都是 `mcp~=1.26.0` | v1 已進 maintenance；三個 package 的實際需要不同 | Proxy／Dispatch 升 v2；Memory 經 import 與測試證明無使用後移除依賴 |
| `ToukaProxy/src/protocols/mcp/client.py` 手組 transport＋`ClientSession`＋`initialize()` | 新協定無 initialize/session；`streamablehttp_client` 名稱已移除 | 封裝 v2 高階 `Client(target, mode="auto")`，在抽象層保留逾時、取消、清理與錯誤消毒 |
| client 連線成功只回傳 session | 無 negotiated version、capabilities、server identity 的正式快照 | 回傳 typed connection handle，明確保存 negotiated metadata 與 transport lifecycle |
| HTTP transport 沿用 SDK 內建 HTTP client | v2 改用 `httpx2`；例外類別、TLS trust store、OTel 行為都改變 | 建立 Touka 統一 `httpx2.AsyncClient` factory；redirect、timeout、TLS、header 與 telemetry 由 SSOT 注入 |
| `ProtocolManager.initialize_all()` 在開機時以 server name 建全域 session | 開機時沒有 human principal，無法安全建立 per-user OAuth handle | 拆成 installation state 與 request-time lazy handle pool；只有公開無認證 handle 可共享 |

### 3.2 manager、cache 與結果邊界

| 現況證據 | 缺口 | 遷移方向 |
|---|---|---|
| `ProtocolManager._active_sessions` 只以 `server_name` 為 key | 無 installation、principal、協定版本與 auth context 隔離 | 改為 stable connection key；不同人類 principal 不共用有認證的 handle/cache |
| `call_tool_on_server(server, tool, arguments)` 沒有 identity/request context | MRTR、OAuth、Tasks、Apps callback 無法回到正確使用者與安全等級 | 強制傳入 `McpRequestContext`，由 ToolExecutor 建立並在完整生命週期透傳 |
| `ToolCacheEngine` 對 server/tool/args 做持久於程序生命週期的結果快取 | 副作用重播語意錯誤，且 key 不含身分 | P1 先移除 `tools/call` 完成結果快取；若要降低併發重複，只允許短生命週期、同 identity/request 的 in-flight coalescing |
| `get_all_tools()` 只投影 name/description/inputSchema | output schema、annotations、icons、title、`_meta`、extension metadata 遺失 | 建立 canonical descriptor；provider-specific projection 在最後一步生成 |
| `call_tool_on_server()` 只取 `result.content` | `structuredContent` 任意 JSON、`isError`、`resultType`、`_meta` 遺失 | 回傳 discriminated typed outcome，不在 manager 內轉成字串 |
| `ToolExecutor` 再以文字 heuristic 判斷成功 | 與協定 `isError`／JSON-RPC error 相衝突 | 以 canonical outcome 為 SSOT；文字只作模型可讀呈現 |

### 3.3 Touka 自有 server 與 Creation Engine

| 現況證據 | 缺口 | 遷移方向 |
|---|---|---|
| `ToukaDispatch/server.py` 使用 `FastMCP(... host, port)` 與 `run(transport="sse")` | v2 政名 `MCPServer`，transport 參數移出 constructor；HTTP+SSE 已 deprecated | 改為 Streamable HTTP 主路徑；設明確 server name/version；舊 SSE 只留有期限的相容路徑 |
| `grok_imagine_server.py`、整合測試 stub 使用 `FastMCP` | 升 v2 import 即中斷 | 同步改用 `MCPServer`，並驗證 structured output 與錯誤語意 |
| `services/creation/scaffold.py` 生成 v1 `FastMCP` | 新建立的 server 會一出生就過時 | scaffold 改生 v2；產物 manifest 記錄生成器與 MCP SDK major |
| `services/creation/smoke.py` 以 stdio＋`ClientSession.initialize()` | 只能驗證舊協定，且 smoke 與正式 client 重複生命週期邏輯 | 改用共用 client adapter；至少驗證 modern auto 與 legacy compatibility |
| 既有 `config/mcp_servers/*` 產物可能含 v1 import | Python 單一環境升 v2 後會立即失效，Creation 主權又禁止自動覆寫 | P0 升版前唯讀掃描並設 cutover gate；維護者手動修正/停用，或核准隔離 v1 runner |

### 3.4 ToukaIDE Node MCP server

| 現況證據 | 缺口 | 遷移方向 |
|---|---|---|
| `ToukaIDE/package.json` 使用 `@modelcontextprotocol/sdk ^1.27.1` | Python 升版不會自動帶動 Node 面；若漏掉就不能宣稱 Touka 全面遷移 | 獨立更新 Node SDK 與 package lock，執行 IDE tests/build |
| `ToukaIDE/src/mcp/server.ts` 使用 `Server`＋`SSEServerTransport` | endpoint 是 `/mcp/sse`＋`/mcp/messages`、依 session ID 路由 | 新增 Streamable HTTP `/mcp` 主路徑；舊端點雙掛觀察後退場 |
| IDE 以 Bearer middleware 保護 `/mcp/*` | transport 改版不能弱化 local token、multi-connection 與 auth level | 每 request 驗 Bearer；保持 AuthLevel/HITL，不能以 clientInfo/session 代替認證 |

### 3.5 ToukaChat 與產品能力

目前 UI 只有 server/tool 與安裝／啟用概念，沒有：

- negotiated protocol version、capability 與 degraded mode；
- OAuth pending/connected/expired/revoked/reconnect 狀態；
- `input_required` 的結構化互動；
- resources/prompts 的 discovery 與選擇；
- Tasks 的 durable status；
- MCP Apps 的 sandbox host；
- subscription loss、cache freshness 與重新整理狀態。

這些狀態不能塞回單一設定元件；依 SOLID 規則拆成 protocol health、authorization、interaction、resources/prompts、tasks、apps 等領域 hooks 與視圖。

## 4. 目標架構

### 4.1 邊界物件

實作前先定義下列 `Protocol`／Pydantic discriminated union；名稱可在 ADR 階段微調，但責任不可合併：

```text
McpClientHandle (Protocol)
├─ negotiated_version
├─ server_capabilities / server_info
├─ list_tools/resources/prompts(...)
├─ call_tool(..., request_context)
├─ call_once(..., allow_input_required=True)  # 僅耐久/低階 MRTR 選案
├─ listen(...)
└─ close()

McpRequestContext
├─ request_id / session_id
├─ identity: AgentIdentity
├─ human_principal_id
├─ installation_id / server_key
├─ auth_level / policy context
├─ foreground | heartbeat | dispatch
└─ cancellation / trace context

McpOperationOutcome
├─ CompleteOutcome
│  ├─ content
│  ├─ structured_content: Any
│  ├─ is_error
│  └─ meta
├─ InputRequiredOutcome
│  ├─ input_requests
│  └─ request_state
└─ TaskOutcome                  # 只在 Tasks extension 啟用後
   ├─ task_id / status
   ├─ ttl_ms / poll_interval_ms
   └─ input_requests
```

`McpOperationOutcome` 是 SDK-neutral canonical contract。P1 的 legacy adapter 只能提供 `source_protocol_version + complete + dict|None structured_content`；modern 任意 JSON/resultType 的 wire 驗收要等 v2 cutover。JSON-RPC protocol error 不偽裝成 `CompleteOutcome(is_error=True)`；它先保留原 code/data，再由單一 mapper 轉成 Touka ErrorCode SSOT。`is_error=True` 則仍是合法 `tools/call` 結果，必須讓模型看見並有機會自我修正。

### 4.2 連線與身分模型

1. `server_key`／installation ID 是 Touka registry identity；`serverInfo.name` 僅供顯示。
2. 拆成兩層：
   - `InstallationState`：registry config、enabled、全域 health 摘要、generation、公開 metadata；
   - `HandlePool`：`installation_id + auth/delegation principal + protocol mode + config generation` 對應的 process/HTTP handle。
3. 開機只載入 installation；authenticated handle 在 request-time lazy connect。無認證公開 server 才可共用 handle。
4. restart、cleanup、circuit breaker 與 health 都要標示作用層級。installation disabled 會關閉所有 handles；單一 principal auth failure 不得打開全 installation circuit。
5. stdio process、HTTP client 與 subscription listener 仍有本地生命週期；「協定 stateless」不代表可以刪除本地 process/connection supervisor。
6. manager 不得保存可變的「目前使用者」全域欄位；每個 call/list/read 都顯式接收 request context。
7. foreground、heartbeat、dispatch 的 token 委派規則分開。沒有 owner 明示授權時，互動式人類 OAuth token 不得自動供無人值守 heartbeat 使用。

### 4.3 快取模型

將現有單一 `ToolCacheEngine` 拆成：

| 類型 | 可否快取 | key／隔離 |
|---|---|---|
| `tools/call` 完成結果 | 禁止 | 不適用 |
| 同一請求的 in-flight coalescing | 可選 | identity＋request lifecycle＋server＋tool＋canonical args |
| `tools/list`／`prompts/list`／`resources/list`／`resources/templates/list` | 依 `ttlMs` | server installation＋negotiated version＋capability fingerprint；`private` 再加 principal |
| `resources/read` | 依 `ttlMs` | URI＋server＋version；`private` 再加 principal |
| OAuth discovery／CIMD metadata | 依 HTTP/spec freshness | issuer/resource server 分區，不能與 access token 同庫 |

初次升 v2 時建立 `Client(cache=None)`，關掉 SDK 預設 response cache；等 Touka 自己的 partition、TTL、失效與測試完成後，才決定直接採 SDK `CacheConfig` 或保留 Touka adapter。`subscriptions/listen` 收到 list changed 或 listener loss 時，對應目錄標記 stale；listener 不能把 request-scoped progress/message 混進 catalog invalidation。

### 4.4 相容策略

- client 預設 `mode="auto"`：先 `server/discover`，只有 SDK 判定 peer 不支援 discover 時才回退舊 `initialize`；modern `-32602/-32020/-32021/-32022` 不得被自行吞掉後降級。
- 可在單一 server 設定覆寫 `auto | legacy | 2026-07-28`，另有全域 emergency override；設定需進 Pydantic／Env SSOT。
- Touka 自有 `MCPServer` 先同時服務新舊協定；健康頁記錄實際 negotiated version，而非只顯示 SDK 版本。
- **legacy HTTP+SSE transport** endpoint 設明確 sunset 日期與使用量指標；新 Streamable HTTP 的單次 response 仍可合法使用 request-scoped SSE，不能因退役舊 transport 而刪掉。
- 相容層只保證既有核心行為，不在舊協定上模擬 Tasks／Apps 等新 extension。

### 4.5 發布與回滾模型

- Python v2 cutover 是單一 workspace 的原子變更；不能以「P2 先升 package、P3 再修 import」製造不可執行的 commit。
- 真正 rollback 是已驗證的 previous immutable image＋舊 lock＋相容 config schema，不是把 per-server mode 切成 `legacy`。
- 每次 config 新增欄位都要保證舊 image 可忽略或有明確 downgrade 測試；token/task 等新 state 不得由舊 image誤讀。
- runtime transport rollout 可獨立於 SDK cutover：先讓 v2 server 繼續跑 legacy HTTP+SSE，再雙掛 `/mcp`，最後才 sunset `/sse`。

## 5. 分期實作

每一階段都必須先加失敗測試、再改實作；通過 acceptance criteria 才可進下一階段。P2 是單一 uv workspace 的原子 cutover，不製造「package 已升、v1 import 尚未修」的假里程碑；其餘階段則以 feature flag、雙掛 endpoint 或 capability advertising 控制 rollout。

### P0｜v1 基線、全 repo inventory 與 cutover gate

**目的**：固定現有行為、找齊所有會被 v2 立即破壞的程式與使用者產物，先建立可信 rollback。

> **實作狀態（2026-07-30，ADR-0197）**：P0 已收官。characterization matrix、
> v1 raw-wire、唯讀 inventory、generated-artifact／跨 principal／P2 fail-closed
> gate、lock/config fixture 與 sandbox rollback runner 均已落地；維護者明確接受
> 本機 content-addressed digest，live smoke 已通過 Proxy／Heartbeat／Chat 探針並
> 完整清理隔離資源。P0 inventory 腳本與 rollback runner 已依 ADR-0238 刪除，本段僅為歷史。

**工作**

1. characterization tests 固定 ADR-0191／0194 的 connect timeout、401、取消、雙重 cancel、teardown、startup 隔離、circuit breaker、enabled gate、cleanup、static header 消毒、vault jail 與 per-tool permission。
2. 保存 stdio／remote v1 raw-wire fixtures；P0 不假裝能在單一 v1 lock 內跑 modern-v2 四格矩陣。
3. 全 repo 盤點：
   - Python `mcp.*` imports、`FastMCP`、camelCase attribute、`initialize()`、legacy HTTP+SSE、`httpx` 例外與 monkeypatch target；
   - ToukaIDE Node SDK、SSE transports、package lock、IDE tests/build；
   - Docker/CI/release workflow、外部 Dispatch guide/skill、架構圖、README/API docs。
4. 唯讀掃描所有 registry command 與 `config/mcp_servers/*` 既有生成產物。含 v1-only import 者列出維護者待處置項：手動修正、停用，或核准隔離 v1 runner；任何未處置 blocker 都禁止 P2。
5. 建立前一版 immutable image＋lock＋config fixture，實跑 downgrade/rollback smoke。
6. 新增跨 principal 安全紅燈：private catalog、handle、tool result 不可共享。
7. 維護者決定 §9；未決項用 fail-closed 預設。

**驗收**

- 所有 v1 可靠性修復有測試錨點。
- dependency/import/generated-artifact/IDE/deployment inventory 可追蹤且無未分類項。
- P2 cutover gate 可機械拒絕未處置的 v1 產物，不修改產物或 `data/`。
- previous image rollback smoke 可重現。

### P1｜SDK-neutral canonical boundary、cache 止血與 handle 骨架

**目的**：先在 v1 上切斷文字壓平、完成結果快取與全域「目前使用者」假設。

> **實作狀態（2026-07-30，ADR-0198）**：P1 已收官。canonical descriptor／
> outcome／error、必填 `McpRequestContext`、principal-keyed `HandlePool`、
> installation/handle health 分層、完成結果 cache 移除、production/Creation launch
> env 分流及 config wire/runtime parity 均已落地。runtime 仍是 SDK v1；modern wire、
> OAuth 流程與任意 JSON structured output 沒有提前宣稱完成。

**工作**

1. 定義 `McpRequestContext`、canonical descriptor/outcome/error，加入 `source_protocol_version`。
2. legacy adapter 把缺省 `resultType` 視為 complete；只承諾 v1 可表達的 `structuredContent: dict | None`。scalar/array/null 的 wire 驗收移到 P3。
3. `call_tool_on_server` 強制接收 request context；ToolExecutor 從已驗證 `AgentIdentity` 建立。
4. 刪除 `tools/call` 完成結果快取；可選 in-flight coalescing 只限同 identity＋同 request lifecycle，task 完成即移除。
5. JSON-RPC、transport、tool result error 分流；單一 mapper 轉 Touka ErrorCode SSOT，連線錯誤維持消毒。
6. 抽出 `InstallationState`＋`HandlePool` 介面；先包住既有 v1 session，authenticated handle 改成 request-time lazy connect。
7. restart/cleanup/circuit breaker/health 明確分 installation 與 principal handle。
8. 抽 `LaunchEnvironmentPolicy`：production 可用完整必要 env；Creation smoke 永遠 clean env＋spec env。
9. `ProtocolServerConfig` 與 `MCPServerItem` 新欄位同批保持 parity，防一端 ignore、另一端 forbid。

**驗收**

- `isError=true` 不被當成功或 500；同參數副作用 tool call 會執行兩次。
- 兩 principal 無 cross-context/handle/cache。
- Creation candidate env 不含 Touka/Dispatch/Heartbeat/provider secrets。
- 本階段只驗 canonical model，不宣稱 modern wire 支援。

### P2｜Python SDK v2 原子 cutover

**目的**：單一批次讓 workspace 重新全綠；依賴、client、所有 Python v1-only import 與 Creation 同時切換。

**工作**

1. package：
   - ToukaProxy、ToukaDispatch 釘實際測過的 patch（初始為 `mcp==2.0.0`，不是無效的 `2.0.x`）；
   - 直接使用 `httpx2` 的 package 明列直接依賴；ToukaProxy/ToukaDispatch 自身仍用 `httpx` 就保留直接 pin；
   - ToukaMemory 確認無 import 後移除 stale `mcp`。
2. 依 ADR-0117 執行 `pyproject.toml → uv lock → uv sync --all-packages`，Docker/CI 同源。
3. client adapter 改用 public v2 `Client(target, mode=...)`；不得依 private dispatcher。初期 `Client(cache=None)`。
4. 統一 `httpx2.AsyncClient` factory：redirect false、四類 timeout、SSLContext/CA、禁環境 proxy、header redaction、正確 exception types。
5. 同一批修完：
   - `ToukaDispatch/server.py`、Grok Imagine、所有 integration stub 的 `MCPServer` import/constructor；
   - Creation scaffold/smoke、camelCase attributes、removed aliases；
   - sync handler worker-thread 影響與低階 handler error wrapping。
6. Creation scaffold 寫 `sdk_major=2`；smoke 共用 adapter 但強制 `LaunchEnvironmentPolicy.clean`。
7. P2 只把 Dispatch 程式 API 升 v2，transport 暫留 legacy HTTP+SSE；transport rollout 在 P4-A。
8. connection health 暴露 negotiated version/capabilities/server info，但 `serverInfo` 只供顯示。
9. 重做 ADR-0191／0194 timeout/cancel/teardown，不因 v2 自帶 cancel propagation 刪 startup deadline。

**驗收**

- Python 3.12 全 suite/guards 全綠，所有 production module 可 import。
- Proxy 容器不會因 Dispatch/Grok/Creation import crash-loop。
- Creation 新產物 modern/legacy smoke 都可列工具；舊產物零自動覆寫。
- 本地與 container 無隱性 `httpx`/`httpx2` 傳遞依賴。

**回滾**：使用 P0 驗證的 previous image＋lock＋config；`protocol_mode=legacy` 是相容模式，不冒充 package rollback。

**完成紀錄（2026-07-30，ADR-0199）**

- ToukaProxy／ToukaDispatch 固定 `mcp==2.0.0`，Proxy 明列 `httpx2`；ToukaMemory
  stale dependency 已移除，`uv.lock` 與 3.12 venv 同步。
- runtime 改用 public `Client(target, mode, cache=None)`；remote HTTP 統一走顯式
  SSLContext、四 timeout、禁 redirect／環境 proxy、header redaction 的 factory。
- Dispatch／Grok／integration stub／Creation scaffold 全改 `MCPServer`；Creation
  handoff 標記 `sdk_major=2`，auto／legacy 真 subprocess smoke 皆可列工具。
- health 暴露 negotiated version/capabilities/server info；server info 僅顯示。
- Node SDK v1 與 Dispatch legacy SSE 當時留給 P4-B／P4-A 處理。未修改既有生成產物或 `data/`。

### P3｜Base conformance、dual-era 矩陣與容器發版閘

**目的**：證明 v2 cutover 不只是 import 成功，而是符合 base/versioning/message-pattern/transport wire contract。

**工作**

1. 建立 `stdio × Streamable HTTP × modern v2 peer × isolated legacy peer` 四格矩陣。
2. modern request/response：
   - 每 request `_meta` 必有 protocolVersion/clientCapabilities；
   - serverInfo 只顯示；
   - missing meta=`-32602`＋HTTP 400；
   - header mismatch=`-32020`；
   - missing capability=`-32021`＋HTTP 400；
   - unsupported version=`-32022`；
   - 上述 modern errors 不觸發 legacy fallback。
3. Streamable HTTP MUST：
   - 每訊息獨立 POST；
   - `Accept` 同時支援 JSON＋`text/event-stream`；
   - response 可為 JSON 或 request-scoped SSE；
   - cancellation 關閉 response stream；
   - modern path 無 GET、`Mcp-Session-Id`、`Last-Event-ID`/redelivery；
   - server 驗 `Origin`，本機預設 loopback。
4. modern wire 驗 `resultType`、scalar/array/object/null structured content、method-not-found 與 error code allocation；legacy 缺 resultType 視為 complete。
5. release workflow 增 container import/runtime smoke、真 `server/discover`／legacy initialize／tools-list、modern exact `2026-07-28`／legacy exact `2025-11-25` 與 Dispatch supervisor running assertion。
6. 以本地 TLS mock CA 驗 `httpx2` truststore/SSLContext，不依 live provider。
7. 重跑所有 teardown/cancel/restart/cleanup 與 no-residual-task 檢查。

**驗收**

- 四格矩陣、wire negative tests、本機與 Docker 全綠。
- modern request-scoped SSE 正常；只有 legacy HTTP+SSE 被標記 deprecated。
- release workflow 不能在 Dispatch import/協定壞掉時假綠。

**實作結果（2026-07-30，ADR-0200）**

- stdio／Streamable HTTP × modern／isolated legacy 四格、negative code/HTTP、
  request-scoped SSE、Origin、無 modern GET/session/redelivery、TLS mock CA 與
  no-residual-task 全綠。
- Touka `auto` 改由本地 adapter 掌控：真送 `server/discover`，只在 `-32601`
  關閉並重開 transport 走 legacy；modern validation/capability/version errors
  不再被 fallback 掩蓋。
- canonical complete outcome 已保留 object/array/string/number/boolean/null 與
  欄位 presence；modern 缺 `resultType` fail-loud，legacy 缺省相容不變。
- 當時的 release workflow 與 Guard #42 曾加入容器內 dual-era probe 與 Dispatch import 檢查；
  兩者與後續 release probe 已依 ADR-0238 刪除，modern 協定行為改由 sandbox 整合測試保護。

### P4-A｜ToukaDispatch Streamable HTTP 雙掛與認證不變量

**目的**：把 Dispatch 主路徑移到 `/mcp`，同時保留可觀測的 `/sse` 相容窗。

**工作**

1. 先建立 Dispatch request-auth ADR。相容預設保留 application handshake/session token，但每次 tool call 都顯式攜 token；不得綁 MCP session、connection 或 self-reported clientInfo。
2. internal/global credential 與 external mission credential 分 audience/method：
   - master `DISPATCH_TOKEN` 不進外部子程序；
   - external token 固定 `is_external=True`、bound mission、禁止 initialize mission；
   - store 只存 token hash＋audience＋mission/generation＋expiry＋caller type，逐 request constant-time 驗證；
   - mission replacement 原子撤銷舊 generation。
3. `/mcp` 使用 Streamable HTTP；`/sse`＋messages 以獨立 compat namespace/endpoint 保留，不共用未分面 transport session state。
4. 保持 loopback default、compose 顯式升開、supervisor/watchdog 與 callback安全。
5. `DISPATCH_URL` 契約、外部 guide、外部 dispatch skill、架構圖與 README 同批改成 modern endpoint；compat 指引保留期限。
6. 先加 telemetry 再倒數 sunset：anonymous connection、成功 handshake caller type、last-used、protocol/transport；觀察持久性與期限由維護者決定。

**驗收**

- modern/legacy client 都能完成 handshake、board read/update；全部 Mission Token/FSM/env whitelist/security tests 不退化。
- stale/跨 mission/跨 transport token、external initialize、restart/rotation 全部 fail-closed。
- container 從發布 port 8083 真連 `/mcp`，不是只做 TCP readiness。

**實作結果（2026-07-30，ADR-0201）**

- Dispatch 主路徑已改為 stateless Streamable HTTP `/mcp`；legacy `/sse`＋
  `/sse/messages/` 使用另一個 `MCPServer` 實例與 transport 分面，不共用 MCP
  session state。
- application session 每次 tool call 重驗 expiry／transport／audience／method／
  mission generation；credential store 不留 bearer 明文或 caller 自述資料。
  mission token 改為鎖內原子消耗，新 mission rotation 同鎖撤銷舊 credential 與
  external session。
- `DISPATCH_URL` 現為完整 `/mcp` endpoint；外部 guide／skill、README 與架構圖已同步。
  `/health` 提供不含 token／caller name／mission ID 的匿名 transport telemetry；legacy `/sse`
  後續已依 ADR-0221 退役。
- supervisor readiness 以真 modern tools-list 驗證 `/mcp`，不使用裸 TCP；當時的容器發版閘
  與 Dispatch release probe 已依 ADR-0238 刪除。

### P4-B｜ToukaIDE Node SDK v2 與 transport 遷移

**目的**：補齊 Touka 非 Python MCP server 面。

**工作**

1. 移除 v1 umbrella `@modelcontextprotocol/sdk`，改用已測的 v2 分拆套件並同步
   `package-lock.json`。
2. 以 Node v2 public API 提供 `/mcp` Streamable HTTP；legacy `/mcp/sse`＋`/mcp/messages` 雙掛觀察。
3. Bearer middleware 每 request 執法；不得以 protocol session/clientInfo 代替 token。
4. 保留多連線路由、AuthLevel、VS Code modal HITL、path safety 與 terminal policy。
5. 更新 discovery file/endpoint contract，確保 token 不進 log/webview。
6. 同批更新 `IDE_INTERFACE.md`、`docs/ide/spec_IDE.md` 與 package/build 文件。

**驗收**

- Node modern/legacy conformance、Bearer missing/invalid、多 client、disconnect/cancel、AuthLevel/HITL 全綠。
- `npm test`、TypeScript/esbuild/package build 全綠。
- 未完成 P4-B 前不得宣稱 `Touka-owned servers migrated`。

**完成紀錄（ADR-0202）**

- 舊 umbrella `@modelcontextprotocol/sdk` 已移除；穩定分拆套件
  `server`／`node`／`server-legacy`／`client` 精確釘在 `2.0.0`，Node engine
  提高為 20（VS Code `^1.90.0`）。
- `/mcp` 以 v2 public API 提供 modern `2026-07-28`；legacy
  `/mcp/sse`＋`/mcp/messages` 使用隔離 server／transport 相容窗。
- Bearer 對每個 modern／legacy request 執法，空 token fail-closed；服務只綁
  loopback，Host／Origin 再驗證。discovery token 不進 log／Webview。
- Proxy IDE adapter 改走共用 v2 conservative client；discovery 只接受本機 path，
  不再手刻未 initialize 的 `tools/call`。
- AuthLevel／modal HITL、workspace path jail、多 client 與 disconnect/cancel
  已有行為測試。
- 當時 P4-B gate 與全域 blocker 均為 0，可宣稱 `Touka-owned servers migrated`；
  完整 base／host／extension 宣稱仍待 P5 以後。
- `npm audit fix` 與 esbuild 0.28.1 升級已把 ToukaIDE 11 項 advisory 降到 2 項；
  剩餘均為 v2 SDK 鎖住、目前無相容上游修法的 moderate。

### P5｜Feature families：Tools、Resources、Prompts 與 Schema

**目的**：完成 Touka 選定的 host product data plane，再建立共用 cache/MRTR。

**工作**

1. tool descriptor 保留 name/title/description/input/output schema、annotations、icons、`_meta`；provider projection 最後才做。
2. manager 增 resources list/templates/read、prompts list/get canonical API；Touka 自有 server 只宣告真實提供者，不自動暴露 ToukaMemory。
3. URI 是不可信字串；`file:`/相對 URI 不直通本機 filesystem，本機資料仍經 ResourceManager。
4. resource 設 MIME/size/encoding/binary bounds 與 DLP/permission；外部 prompt 強型別驗證且只作 untrusted context。
5. JSON Schema：
   - 無 `$schema` 預設 2020-12且 MUST 支援；
   - 顯式不支援 dialect 要 graceful error；
   - input root object、output unrestricted；
   - composition/conditional/internal refs；
   - 外部 network `$ref` 預設不抓，unresolved 時拒絕；
   - 深度/subschema/time bounds。
6. 所有 list 走完 cursor，設頁數/總項目/payload 上限與 loop detection。
7. ToukaChat 拆成 tools/resources/prompts hooks/views；每個 API 同批有 Pydantic response model、OpenAPI、generated alias、i18n、介面文件與 tests README。

**驗收**

- tools/resources/prompts modern/legacy、分頁、惡意 URI、prompt injection、schema corpus 全綠。
- scalar/array output 與 structured content 保真。
- 未支援 feature 不廣告、不顯示。

**完成結果（ADR-0203）**

- canonical tools/resources/templates/prompts 保存完整 descriptor；所有 list 走完整
  cursor，invalid item quarantine，provider function schema 最後才投影。
- resources/read 與 prompts/get 經 URI、MIME、encoding、base64、aggregate bytes、
  permission、CSRF、DLP 與 prompt-injection 邊界；prompt 固定標記 untrusted。
- JSON Schema 預設 Draft 2020-12，支援 composition/conditional/internal refs，
  external `$ref`／`$dynamicRef` 不抓；input object、output 任意 JSON root。
- ToukaChat 已拆 Tools／Resources／Prompts hooks/views，只顯示對端支援的功能；
  Pydantic/OpenAPI/generated aliases/i18n 與介面文件同批同步。

### P6｜Cache、Subscriptions、Headers、Icons 與 Telemetry

**目的**：在三個 feature family 都存在後只建一套 freshness/notify/observability。

**工作**

1. 依 §4.3 建 catalog/resource cache；初期仍 `cache=None`，單一 cache owner 完成後才啟用。
2. `ttlMs/cacheScope` 完整執法；public 不跨 installation，private 再加 principal。
3. `subscriptions/listen` supervisor 綁 installation＋principal，驗 subscription ID/filter；掉線標 stale、有上限退避，request-scoped progress/log 不混入。
4. `Mcp-Method/Mcp-Name/MCP-Protocol-Version` 交 SDK；body/header mismatch fail。
5. `x-mcp-header`：
   - ingest 時驗 `x-mcp-header` annotation、primitive、visible ASCII、case-insensitive unique；
   - 展開本地 refs，違規只隔離該 tool並告警；
   - count/name/value/aggregate byte bounds；
   - secret/PII 欄位預設停用，owner 可逐 tool核准；
   - `Mcp-Param-*` 全面 log/OTel redaction，絕不參與 Touka RBAC/tenant。
6. icons：HTTPS/data only、same-origin、無 credential fetch、redirect/scheme限制、MIME magic/大小/維度/動畫；SVG 預設拒絕或 sanitize。
7. OTel：
   - span attribute allowlist；
   - 對端 trace 不作 trusted parent，預設 drop tracestate/baggage；
   - SDK/HTTP instrumentation 只開一層；
   - exporter 預設 off/local且 endpoint allowlist；
   - raw args/header/token/resource/prompt/content/query/exception 不輸出。
8. error mapper接受 legacy `-32002`，modern使用 `-32602`；不得挪用 `-32020..-32099`。

**驗收**

- private/public partition、TTL、listener loss/invalid filter、stale semantics 全綠。
- header annotation/DoS/secret、icon SSRF/MIME/SVG、OTel export payload fuzz 全綠。
- 可關 cache/listener退回每次讀取，不影響協定連線。

**完成結果（ADR-0204）**

- cache owner 已收斂為 per-installation SDK backing store，private 再依 principal
  partition；TTL/scope、關閉回退與 subscription stale/invalidation 已接線。
- `x-mcp-header` primitive/ASCII/bytes/sensitive opt-in、icon HTTPS/data/MIME/SSRF、
  OTel allowlist/redaction 均有有界政策與回歸測試。
- listener 綁完整 handle key 與 generation，掉線有限退避；request-scoped
  progress/log 不進 freshness 通知。

### P7｜MRTR base pattern 與 Touka HITL

**目的**：對 tools/call、prompts/get、resources/read 都正確處理 `input_required`，不把 MRTR 偽裝成可關 extension。

**預設路徑：SDK-managed ephemeral MRTR**

1. 高階 `Client` 注入 elicitation/sampling/roots callbacks，由 SDK 管理 fresh request ID、opaque `requestState` echo、round loop。
2. callback 以 closure/contextvars 綁精確 `McpRequestContext`：human principal、frozen AgentIdentity、session/conversation、installation generation、method/tool、args digest、原 auth_level。缺 context 立即 decline。
3. 把既有 ApprovalService 泛化為行程內、可攜 schema/payload 的 interaction broker；UI 只拿 local approval ID，永遠看不到 requestState。
4. callback 回應前重新查 human active/owner、installation generation、MCP grant、tool enabled、PaC；有效 auth level 取原始與目前較嚴格者。
5. 若 SDK 沒有可證明的 retry 前授權 hook，Touka 改用同 coroutine 的低階 `client.session.*(..., allow_input_required=True)` 手動 driver；仍不落庫、不把 requestState給前端，每輪先 Gate 2。
6. 設 per-round/total deadline、max rounds、input/state/response bytes、disconnect/cancel propagation。
7. heartbeat/headless 預設 decline/暫停；SafeAuto/FullYOLO 不推導全部同意。Sampling/Roots 已 deprecated，預設不宣告。
8. process restart/stream斷線明確不可恢復且不自動 replay；跨重啟長工作應使用 Tasks。

**選配 P7-B：durable MRTR**

只有維護者明確要求「跨頁面/worker/restart續接」且 Tasks 不適用時另立 ADR。store 必須綁 original canonical request/args digest、human＋AgentIdentity、session、installation generation、method、protocol era、原 Gate2/auth level、round、expected keys/schema hash、expiry；前端只拿 local handle；CAS `pending→consuming` 在 upstream I/O 前完成，continue 不接受替換 args，每輪重驗授權。此選案不是 base migration 預設。

**驗收**

- 三種 operation 的 accept/decline/cancel/timeout/round cap/權限撤銷都有測試。
- requestState 不進 DB/API/frontend/log/OTel。
- 未能履行的 input kind 明確 surface unsupported；MRTR parser 永遠存在，不能以「關 capability」回滾。

**完成結果（ADR-0205）**

- Python SDK v2 高階 client 缺 retry 前授權 hook，因此採同 coroutine 低階
  `client.session.*(..., allow_input_required=True)` driver；opaque continuation
  state 只存在 stack frame，每輪 retry 前重驗 human/owner、generation、grant、
  tool policy、session/auth level 與 PaC。
- `ApprovalService` 已泛化為 schema/payload interaction broker；ToukaChat 只接
  local `mcpint_*` ID 與扁平 primitive form，accept/decline/cancel 分流。
- 三種 operation、timeout/round/bytes/cancel/revocation/headless/unsupported
  均有回歸鎖。P7-B 未經維護者明確核准，未建立 durable store 或 continuation API。

### P8｜OAuth 核心授權與 authorization extensions

**目的**：以 per-request/per-principal 架構實作 HTTP OAuth，不阻塞 lifespan。

#### P8-A｜互動式 authorization code

詳細設計與 TDD SSOT：[P8-A 互動式 OAuth 實作計畫](mcp-2026-p8a-interactive-oauth-plan.md)。
若本節摘要與專屬計畫衝突，以專屬計畫為準。

1. ConnectorAuthService 依 stable human／execution／installation slot，透過 versioned active-grant pointer 精確定位 full issuer／resource／client／scope tuple；禁止 partial-key scan 或跨 principal fallback。
2. transaction、grant、refresh、profile mutation、key rotation 與 DCR 均有 persisted FSM、跨程序 lease／CAS、crash oracle 與 fail-closed recovery。
3. callback 綁 deployment callback policy、exact route／redirect／response mode／method；parser rejection、state lookup／consume、exchange count 與 replay outcome 由表格定義。
4. OAuth outbound／bearer resource 使用 HTTPS-only hardened transport；每次 connection 驗全部 A／AAAA，保留原 SNI／certificate／Host，禁 redirect／env proxy／HTTP2 cross-origin coalescing。
5. raw state 只作 authorization URL 與 callback 的一次性 transit；不得留在 Touka 控制的 storage、log、prompt、tool args、backup、popup message 或 callback 當前 history entry。授權伺服器／browser 管理的跨來源 history 為明示不可控邊界。
6. CIMD 優先，DCR 只作具名 trusted compatibility fallback；scope algorithm、profile mutation fence、key ring state-drain、clean／in-place restore 與 global rollback kill switch 均以專屬計畫為準。
7. P8-B 的 client credentials、enterprise 與 headless delegation 保持 unsupported／fail-closed，不得進 P8-A service。
8. API、Pydantic／OpenAPI／generated types、ToukaChat hook/UI/i18n、SECURITY／interface／SSOT／ADR／deployment／backup／tests 文件同批更新。

**實作狀態（2026-08-02）**：ADR-0206 與上述 runtime／UI／部署邊界均已落地。真
`nginx -t`／exact callback logging canary 與零掛載、`network=none` 的精確 Proxy image
lifespan／OAuth route smoke 亦已通過；smoke 揭露的 dev Compose `--no-access-log` 覆寫漂移已
TDD 修復。

審查另確認並修正：所有 OAuth 外連前 dynamic authority fence、none/static 零
key/DB I/O、每 request 私網 CIDR、query callback 所有失敗路徑 history scrub、active
worker lease、encrypted-row corruption fail-loud，以及 return-boundary 撤權的 exact-grant
freeze。

#### P8-B｜無人值守與企業 extension

專屬第三版設計與 TDD SSOT：[P8-B workload OAuth 計畫](mcp-2026-p8b-workload-oauth-plan.md)。

- OAuth Client Credentials 只對明確 `HEARTBEAT` workload principal 啟用，與 user token 分庫、分 key、分 audit；只支援 `client_secret_basic`／`client_secret_post`。
- heartbeat 使用 P8-A user token 必須由 owner 建 exact delegation policy；token 不複製，browser 不自述 source grant。
- 單一 Proxy process 是正確性前置：local filesystem＋exclusive lifespan lock；remote/unknown、第二 process 或多 worker 一律 fail-closed。
- Enterprise-Managed Authorization 因目前無企業 IdP 延後至整個 MCP 更新完成後另案；Touka admin JWT 不能冒充 assertion。
- SDK v2.0.0 尚缺 client DPoP 與 workload `jwt-bearer`；`private_key_jwt`／DPoP／JWT bearer 均列 upstream/後續 gate，不自動降級。

**歷史實作 checkpoint（2026-08-03；後續已收斂）**：第四版修復計畫完成 RED→GREEN。
候選包含獨立 encrypted workload store/profile/delegation、metadata/client-secret exchange、
pre-egress audit outbox、boot-local exact permit、revocation/rotation/recovery、direct/owner authority、
ProtocolManager immutable ticket＋handle partition、P8-A source-disconnect fence、admin/owner API、
OpenAPI/generated types、ToukaChat control UI 與 backup/restore 邊界。enterprise 仍未實作且
fail-closed。之後再以 TDD 補 durable admission、單一 mutation coordinator、final OAuth POST
binding、bounded replay、typed transition reconcile 與 owner-safe API。此段只記錄當時 checkpoint，不代表現況。

**目前落地狀態（2026-08-11）**：後續修復、驗證與文件已收斂，P8-B implementation 已提交為
`8e9c59a5`。這只完成 ADR-0207／0208 所界定的單一 Proxy process、local filesystem 與
`ExecutionKind.HEARTBEAT` 能力；enterprise／SSO、`private_key_jwt`、DPoP、JWT bearer、
foreground 與 dispatch workload authority 仍為 unsupported／fail-closed，runtime feature flags 也不因
commit 自動啟用。

**驗收**

- mock AS 覆蓋 discovery SSRF/DNS rebind、CIMD/DCR、PKCE、issuer四格、state replay、refresh race、scope、revocation。
- 多 user/soul/installation/generation 無 token mix-up；lifespan 在 auth_required 仍完成。

### P9｜Tasks extension（上游 gate）

**目前 gate（2026-08-10 重新查證）**：本地鎖定的 `mcp==2.0.0` 沒有可用 Tasks public symbols；
官方 Python SDK 的 `ext-tasks` 仍標為 experimental／under development。因此 P9 維持 **HOLD**，
只在官方 Python SDK 提供穩定 SEP-2663 支援，或維護者明確核准並承擔 low-level adapter 時重開；
此 gate 不阻擋 base conformance。

**工作**

1. 雙方 opt-in 才宣告/回 task；canonical outcome 加 `resultType=task`。
2. 對外只用 Touka opaque task handle；upstream task ID 不入路徑。
3. unique key 綁 human principal＋execution/delegation AgentIdentity＋installation/generation＋upstream task ID＋origin method/tool＋args digest。
4. task handle 回覆前 durable insert，碰撞拒絕；get/update/cancel 每次 Gate1同人/identity/owner＋重新 local permission。
5. input response只接受 outstanding keys，以 CAS 一次更新；terminal immutable，poll interval/TTL/backoff/cancel執法。
6. `McpTaskStore/McpTaskId/McpTaskStatus`、route/store/FSM 與 MissionManager 完全分離；靜態 guard 禁 Tasks import/呼叫 MissionManager。
7. notifications/tasks 走 P6 listener，polling為 fallback；task input重用 P7 broker但回 tasks/update。
8. extension 停用時進 drain mode，不建新 task，既有 task 可查至 terminal/TTL。

**驗收**

- 全狀態、crash resume、IDOR、task-id reuse、input replay、Mission payload crossover 全綠。

### P10｜MCP Apps

**目的**：先安全顯示，再開雙向 app RPC。

**工作**

1. 前置 P5 resources/read、P7 HITL、P8 identity/token。
2. Proxy 依權限抓 `ui://` resource；瀏覽器不拿 MCP bearer。
3. display-only 先行：不同 origin 的 sandbox proxy＋inner View；proxy依官方模型使用受控 sandbox/CSP，View不能碰 host DOM/storage/token。
4. interactive 使用 ready/initialize/teardown FSM＋`MessageChannel`。Host 只信綁定 MessagePort/source/app instance/單調 request ID；不把 bearer capability交給 View，View只拿不可攜 instance handle。
5. consent record 綁 resource content hash、CSP、permissions、installation generation、human＋AgentIdentity、conversation；hash/config改變即撤銷。
6. 保存 `_meta.ui.visibility`：app-only tool不廣告給 model；app `tools/call/resources/read` 只限原 installation與允許 visibility。
7. 全 app RPC 用 method allowlist、schema/size/rate bounds：
   - tools/call 每次 Gate1 ownership＋ToolExecutor Gate2/HITL；
   - resources/read 同 server/permission；
   - ui/message/update-model-context 當 untrusted prompt input；
   - open-link 只 allowlisted HTTPS且人類確認；
   - camera/mic/clipboard/download 預設拒絕。
8. iframe/view 絕不收到 Touka JWT、MCP/refresh token、cookie/localStorage。

**驗收**

- double-iframe isolation、XSS/sandbox escape、postMessage spoof、confused deputy、visibility、same-server、CSP/open-link、跨對話 replay 全綠。
- display-only 絕無 tool invocation；interactive 每次動作進 audit/HITL。

### P11｜Rollout、sunset 與終局稽核

**現況（2026-10-08）**：legacy sunset 已依 ADR-0221 完成——Dispatch `/sse`、IDE `/mcp/sse`／
`/mcp/messages` 退役回 404，modern request-scoped SSE 保留；維護者已確認所有 client 只在同一台機器上、
沒有舊版使用需求。原 P11 為發版建立的 rollout evidence producer／ledger／oracle（ADR-0211～0213）、
trusted verifier 與 qualification receipt（ADR-0215）、release authority（ADR-0216／0217）、本機發版
profile 與冷切換／升級程序（ADR-0220／0221 發行部分、ADR-0225～0227）已依 ADR-0238 整體刪除，
不再是任何宣稱的前置條件。

**後續變更仍須維持的技術閘**

1. 觀察 negotiated version、認證、peer-close、取消及所選能力；modern-only 不恢復 legacy fallback。
2. 依變更面同步 ADR／interface／OpenAPI／generated types／tests README。
3. 使用 Python 3.12 venv、適用 tests／guards／audit 及 IDE／Chat 原驗證。
4. ToukaIDE 新 VSIX、Windows Stable 隔離 smoke 及 Marketplace readback 獨立驗收，不能由 runtime 映像結果替代。

**最終宣稱**

- P0～P3＋P6 的 base metadata/subscription 部分＋P7＋modern-only sunset 通過，才宣稱 `MCP 2026 base conformant`。
- P0～P3＋P5～P7＋P8-A＋modern-only sunset 通過，才宣稱 `Touka MCP host product complete`；不隱含 durable MRTR、企業授權、Tasks 或 Apps。
- P0、P2～P4-B 中所有 Touka 自有 server／Creation 驗收通過，才宣稱 `Touka-owned servers migrated`。
- P7-B、P8-B、P9、P10 各自通過，才宣稱對應選配能力／extension supported。

## 6. 測試與驗證矩陣

### 6.1 協定／transport

| Touka 角色 | Transport | Peer | 必測 |
|---|---|---|---|
| Proxy client | stdio | v2 modern | discover、call/list/read/get、MRTR、cancel、stderr 汙染 |
| Proxy client | stdio | isolated v1 legacy | initialize fallback、缺省 resultType、cleanup |
| Proxy client | Streamable HTTP | v2 modern | per-request metadata、JSON/request-scoped SSE、non-2xx、subscription |
| Proxy client | Streamable HTTP | isolated legacy peer | initialize compatibility、401、timeout、teardown |
| Dispatch Python server | Streamable HTTP | v2 client | discover、per-request metadata、header mismatch、逐 request app auth |
| IDE Node server | Streamable HTTP | v2 client | discover、Bearer、multi-client、AuthLevel/HITL |
| 自有 server compat | modern transport＋legacy protocol | legacy client | initialize 相容、無 modern-only capability |
| 自有 server compat | legacy HTTP+SSE | legacy client | 僅退場期間，usage、隔離 namespace 與 sunset |

### 6.2 資料與錯誤 corpus

- `structuredContent`：object、array、string、number、boolean、null。
- schema：composition、conditionals、internal `$ref`、external `$ref` 拒絕／不抓取、深度炸彈、巨大 enum。
- errors：transport、HTTP、JSON-RPC、`isError`、invalid result type、每 request `_meta` 缺漏、header mismatch、unsupported version、resource not found。
- pagination：0/1/N pages、cursor loop、重複 cursor、超量結果。
- lifecycle：timeout、caller cancel、server cancel、listener loss、process crash、shutdown during connect。
- fallback：只有 SDK 判定 discover 不支援時可降級；modern validation／capability／version error 一律保留。

### 6.3 身分與安全 corpus

- human A/B、owner/admin/guest、foreground/heartbeat/dispatch 的排列。
- private/public cache、authenticated/public connection sharing。
- OAuth token/state、MRTR requestState、task ID、app instance handle/consent 的 cross-user/cross-generation replay。
- serverInfo/tool annotation/resource/app metadata 冒充已信任 server。
- redirect、issuer mix-up、DNS rebind、credential refresh race、scope escalation。
- `Mcp-Param-*`、icon fetch、OTel/log/error/frontend network payload 的 SSRF／secret snapshot。
- app double-iframe、MessageChannel source/port binding、same-server、visibility 與 confused deputy。

### 6.4 Conformance 與 live smoke

1. 優先接官方 MCP conformance suite；本地 fixture 只補 Touka 的身分、Gate、cache 與 UI 邊界。
2. CI 不依賴真 OAuth；使用本地 mock issuer/resource server。
3. Python 與 Node 自有 server 都要有實際協定 smoke（sandbox 整合測試）；TCP readiness 不算協定驗收。
4. 每個支援層至少有一次維護者手動 live smoke，但未實測的 provider 不宣稱可用。
5. smoke 永遠使用 sandbox env，不讀寫實體 `data/`。

## 7. 設定與 API 遷移

### 7.1 建議的加法式設定

現有 stdio／remote 形狀不覆寫，新增欄位走 Pydantic SSOT：

- `protocol_mode: auto | legacy | 2026-07-28`；
- `auth_profile_id` 或 `auth_mode: none | static_headers | oauth_user | oauth_client_credentials | enterprise`；
- `capability_policy`：只列 Touka 願意宣告的 client capabilities/extensions；
- `sse_compat_enabled`：只供 Touka 自有 server 退場期；
- cache/listener timeout 與上限進 settings，不散落常數。

禁止把 access/refresh token、PKCE verifier、authorization code 放入一般 server config。headers 繼續只存 `${ENV_VAR}` 參照。

### 7.2 API 形狀

實際 endpoint 在 ADR 階段定案，至少需要：

- connector negotiated status/capabilities；
- OAuth start/callback/status/disconnect；
- resources/prompts list/read/get；
- Tasks list/local-status/respond/cancel（P9）；
- App instance bootstrap/revoke（P10）。

P7 預設的 SDK-managed ephemeral MRTR 重用 Touka interaction/approval broker，不建立可列舉的 MRTR continuation API。只有維護者核准 P7-B durable MRTR 時，才新增 local pending/respond/cancel endpoint；前端永遠不得取得上游 `requestState`。

所有 endpoint 都要有明確 auth dependency、response model、ErrorCode SSOT 與 `/v1` parity；frontend 只能透過 `fetchTouka` 與 generated contract。

### 7.3 Runtime state 主權

預設 P7 不持久化 MRTR continuation。OAuth credential、MCP task、app consent/instance，以及選配的 durable MRTR 都需要各自的儲存合約，不可塞進 `server_config.json` 或共用一張無 discriminator 的表。每個 store 必須定義：

- owner/principal key；
- installation ID/generation 與精確 resource/issuer/origin binding；
- encryption/at-rest 姿態；
- TTL/rotation/revocation；
- atomic write/concurrency；
- audit/redaction；
- backup/restore；
- 刪除與「disconnect」語意。

實體位置與加密方案是維護者的決策點；在核准前不得實作成 `data/` 下的方便 JSON。

## 8. 風險、制衡與回滾

| 風險 | 制衡 | 回滾 |
|---|---|---|
| Python v2 原子切換仍一次破壞大量 import/test doubles | P0 全 repo/generated artifact inventory、P1 canonical boundary、精確 pin、同批修完 v1-only import | 部署 previous immutable image＋舊 lock/config；不以 `legacy` mode 假裝 package rollback |
| uv 單一 workspace 出現不可執行的中間 commit | P2 dependency/import/scaffold/server constructor 同一原子批次，容器 import gate | 整批回退至上一 image，不混裝 v1/v2 |
| 既有 Creation Engine 產物在共享 Python 環境失效 | P0 唯讀掃描與 cutover blocker；維護者手動修正/停用或核准隔離 runner | 不執行 P2；絕不自動覆寫產物 |
| ToukaIDE 被 Python 遷移範圍漏掉 | P0 Node inventory、P4-B 獨立 SDK/transport/test/build gate | 保留已驗證 IDE 舊 image/endpoint，禁止宣稱自有 server 全遷移 |
| legacy fallback 掩蓋 modern 壞掉 | health 顯示 negotiated version、modern-only CI case | per-server pin modern 找出問題 |
| SDK/Touka 雙 cache 導致 stale 或跨人 | 初期 `cache=None`、單一 cache owner | 全關 cache/listener |
| protocol stateless 被誤解為不用 lifecycle | handle/supervisor 明確擁有 process/listener | 回到 per-installation handle |
| OAuth discovery/JWKS/token outbound SSRF、DNS rebind | 專用 outbound policy、每跳/每連線重驗 IP、redirect false、metadata freeze | 停 OAuth capability，保留 static header/無 auth |
| OAuth confused deputy/token 共用 | exact issuer/resource、完整 principal key、state CAS、refresh version CAS | 撤銷精確 installation credential，停 OAuth capability |
| MRTR SDK 自動重試繞過已撤銷權限 | callback 綁 exact request context，回覆前重驗 Gate 1/Gate 2；無可靠 hook 就改同 coroutine low-level driver | decline/timeout 當次 interaction；parser 仍保留，不宣稱可關閉 base pattern |
| Tasks 與 Mission Board 混域 | 獨立 store/FSM/keyspace | extension drain mode |
| Apps 成為 XSS/tool proxy | different-origin proxy＋inner View、MessageChannel 綁定、instance handle 非 bearer、ToolExecutor 重驗 | 撤銷 app instance、關 AppBridge |
| `Mcp-Param-*`／icon／OTel 洩漏秘密或發起 SSRF | annotation/size/secret policy、safe icon fetch、attribute allowlist、drop baggage、export snapshot | 關 header mapping/icon fetch/exporter |
| 文件與 API 合約到最後才同步造成假綠 | 每 phase 同批更新 ADR/interface/OpenAPI/generated types/tests README | 不合約發布閘即不合併該 phase |

## 9. 已確認決策與選配的待定事項

已完成主線不重開舊核准；尚未選定的選配沿用括號內 fail-closed 預設：

1. **完成宣稱的邊界已選定**：base conformance、Touka host product 與 Touka 自有 server 遷移分別驗收；
   主線依各 phase 驗收完成。Tasks／Apps／企業授權不在本次完成宣稱，仍為各自選配。
2. **Heartbeat OAuth authority 已確立**：不得直接沿用互動式 human token。ADR-0206／0207 已定義前景
   authorization-code 與 heartbeat workload authority 分離；heartbeat 只接受明示 workload profile 或
   owner delegation，後者按 owner／agent／installation／generation／tool／scope／expiry 逐次驗權，
   不複製 P8-A bearer。既有單一 Proxy process 限制與未啟用時 fail-closed 保持。
3. **OAuth state SSOT 與 at-rest 保護已完成**：ADR-0206 的 `config/mcp_oauth.sqlite3` 採 AES-256-GCM，
   callback raw state 僅保留 digest；ADR-0207 的 workload SQLite、profiles／delegations JSON 與獨立 key ring
   分別管理秘密及 policy。ADR-0208 的自訂模型 endpoint／key 採記憶體內原子路由，不混入 OAuth durable store。
   僅未選的 Tasks／Apps／durable MRTR state、authority 與 at-rest 政策仍待各自計畫決定；未定前不落地、
   不啟用，既有 ephemeral MRTR 不持久化。
4. **ToukaDispatch legacy HTTP+SSE 相容期**：ADR-0221 選定本機 modern-only 已完成；`/sse`入口退役，
   modern request-scoped SSE 保留。已確認沒有舊版使用需求，不再安排 legacy 使用調查。
5. **ToukaIDE legacy HTTP+SSE 相容期**：modern-only source已完成，`/mcp/sse`／`/mcp/messages`回404；
   新版VSIX真實Windows／Marketplace發行另驗，不將舊0.2.0證據轉移。
6. **既有 v1 Creation Engine 產物的處置**：P0/P2歷史驗收已完成；不自動改寫真data或重新建立v1 runner。
7. **Durable MRTR 是否有真實需求**：是否必須跨頁面、worker 或 process restart 續接？（預設：否，採 SDK-managed ephemeral callback）
8. **Tasks 上游策略**：等待官方 Python SDK，或由 Touka 維護低階 extension adapter？（預設：等待 SDK）
9. **MCP Apps 網路與裝置權限政策**：允許哪些 CSP origin、open link、clipboard、camera/mic/download？（預設：全部拒絕，先 display-only）
10. **OAuth DCR fallback**：是否允許未預註冊 server 使用 DCR？（預設：只在具名可信 server 開啟；CIMD 優先）
11. **Enterprise-Managed Authorization** 是否有實際企業 IdP 需求？（預設：不實作、不宣告）

## 10. 明確不做

- 不把 ToukaMemory 改回獨立 MCP server。
- 不新建 Roots、Sampling、Logging 或 legacy HTTP+SSE transport；modern Streamable HTTP 的 request-scoped SSE response 仍依法支援。
- 不以 `serverInfo.name` 取代 registry/server key。
- 不把 server 宣告的 `public` cache scope 當成本機授權許可。
- 不讓 app、task、MRTR 或 OAuth callback 自行指定 AgentIdentity／auth_level。
- 不把 MRTR 當可關閉 extension，也不預設持久化上游 `requestState`。
- 不自動抓外部 `$ref`。
- 不自動修改既有 Creation Engine 產物或 `data/`。
- 不在 Python SDK 尚未支援時，默默宣稱 Tasks、DPoP 或 `jwt-bearer` 已完成。
- 不把所有階段合成單一 ADR 或單一不可回滾 commit。

## 11. 完成檢核表

- [x] P0 v1 基線、全 repo／generated artifact／IDE／deployment inventory 與 previous-image rollback（本機 immutable digest＋live smoke 已通過；GHCR durability 延後）
- [x] P1 canonical boundary、ErrorCode mapping、`tools/call` cache 移除、InstallationState／HandlePool 骨架
- [x] P2 Python SDK v2 dependency/import/scaffold/server 原子 cutover（ADR-0199）
- [x] P3 base conformance、dual-era 四格矩陣（ADR-0200；當時的 container 發版閘已依 ADR-0238 刪除）
- [x] P4-A Dispatch逐 request application auth；原dual-era雙掛為歷史，ADR-0221已退役legacy入口
- [x] P4-B ToukaIDE Node SDK v2／modern-only source（ADR-0202／0221）；最新VSIX另行驗收
- [x] P5 Tools／Resources／Prompts、pagination、Schema 2020-12（ADR-0203）
- [x] P6 cache scope/TTL、subscriptions、headers、icons、OTel（ADR-0204）
- [x] P7 SDK-managed ephemeral MRTR＋HITL（實際採同 coroutine 低階 driver；ADR-0205）
- [ ] P7-B durable MRTR（僅在維護者明確核准時）
- [x] P8-A 互動式 OAuth（ADR-0206；已 commit）
- [x] P8-B workload client credentials／owner heartbeat delegation（ADR-0207／0208；implementation commit `8e9c59a5`）；enterprise／SSO 與其他未支援模式延後另案
- [ ] P9 Tasks（HOLD；等待官方穩定 SDK 支援或維護者明確核准 low-level adapter）
- [ ] P10 MCP Apps（待維護者決定）
- [x] P11 legacy sunset（ADR-0221 modern-only）；原 rollout evidence／qualification／release authority／冷切換機制已依 ADR-0238 刪除
- [x] 已完成 phase 依變更面同步文件／合約
- [x] ToukaIDE `0.3.0`來源整合、原始一次建置VSIX與Windows Stable隔離CLI安裝／版本回讀（PR56；source `ff711dc622ee76274289af993d5530a85af780a0`）
- [x] ToukaIDE `0.3.0`發布前在 Windows 上的 GUI／network 人工見證、既有Marketplace登入／單次upload、公開VSIX原bytes回讀及新profile Marketplace安裝／版本回讀
- [x] ToukaIDE `0.3.0`發布後新profile實際Windows GUI／network四項經人工回報通過（Windows Stable 1.137.0／x64）

## 12. 官方依據（2026-07-30 查證）

- [MCP `2026-07-28` 正式規格](https://modelcontextprotocol.io/specification/2026-07-28)
- [MCP `2026-07-28` Key Changes](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
- [MCP `2026-07-28` GitHub release](https://github.com/modelcontextprotocol/modelcontextprotocol/releases/tag/2026-07-28)
- [MCP Base Protocol](https://modelcontextprotocol.io/specification/2026-07-28/basic)
- [MCP Streamable HTTP Transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
- [MCP Python SDK v2.0.0 stable release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0)
- [MCP Python SDK v1→v2 migration guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md)
- [MCP Python SDK v2 MRTR client behavior](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client.md)
- [MCP TypeScript SDK v2 upgrade guide](https://ts.sdk.modelcontextprotocol.io/v2/migration/upgrade-to-v2)
- [MCP TypeScript SDK `2026-07-28` support guide](https://ts.sdk.modelcontextprotocol.io/v2/migration/support-2026-07-28)
- [MCP `2026-07-28` Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
- [Authorization Extensions](https://modelcontextprotocol.io/extensions/auth/overview)
- [Tasks Extension](https://modelcontextprotocol.io/extensions/tasks/overview)
- [MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview)

## 13. 對抗式複審紀錄

> 2026-07-30 初稿完成後，由三位獨立審查者唯讀檢查，再逐項對照官方規格、SDK 文件與 Touka 現況。複審沒有修改程式碼或 `data/`。

| reviewer | 對抗面 | 發現 |
|---|---|---|
| 協定核心 | base/feature 邊界、versioning、MRTR、structured output、transport、conformance 宣稱 | 6 High |
| Extensions／安全 | OAuth、Tasks、Apps、Dispatch、headers、icons、OTel、跨身分重播 | 8 High |
| SDK／transport／交付 | Python/Node SDK 遷移、uv 原子性、Creation 產物、rollback、Docker/CI、文件同步 | 3 Critical、8 High |

### 13.1 已接受並改入計畫

1. 把「MCP 完全支援」拆成 base conformant、Touka host product complete、自有 server migrated、逐 extension supported 與 legacy compatibility 五種可驗證宣稱。
2. P1 只建立 SDK-neutral canonical model；v1 無法證明的 scalar/array/null structured output 與 `resultType` wire conformance 移到 P3。
3. Python SDK v2 改為單一原子 cutover：依賴、所有 v1-only import、Creation scaffold/smoke、Grok、Dispatch constructor 與 container import gate 同批完成。
4. 把 ToukaIDE 納入正式範圍，獨立安排 Node SDK v2、Streamable HTTP、Bearer/HITL 與 legacy endpoint 雙掛。
5. 把 existing Creation Engine 產物列為 P0 cutover blocker；只能報告並由維護者手動處置，或另核准隔離 v1 runner。
6. 把 installation metadata 與 authenticated runtime handle 拆成 `InstallationState`／`HandlePool`；認證連線改 request-time lazy connect。
7. 明確區分 deprecated legacy HTTP+SSE transport 與 modern Streamable HTTP 合法的 request-scoped SSE response。
8. MRTR 預設採 SDK-managed ephemeral callback，callback 綁 exact request context 並在 SDK retry 前重新授權；不持久化或對前端暴露 `requestState`。
9. OAuth 新增專用 outbound SSRF/DNS-rebinding policy、exact issuer 四格、PKCE S256、完整 token key、state/refresh CAS。
10. Dispatch token 改為逐 application request 驗證，不能依賴 MCP session/clientInfo；external mission token 綁 audience、mission、generation、expiry。
11. Tasks 與 Mission Board 的 type/store/route/FSM 全隔離，並以靜態 guard 防止跨域引用。
12. Apps 改成 different-origin sandbox proxy＋inner View＋MessageChannel；View 不拿 bearer token，只拿不可攜 app instance handle。
13. `x-mcp-header` 的實際威脅修正為 `Mcp-Param-*` argument 洩漏，並加入 annotation、大小、secret、log/OTel 控制；icon 與 OTel 也補齊網路／資料外洩界線。
14. 真 rollback 改為 previous immutable image＋舊 lock/config；每階段都要同批更新 ADR/interface/OpenAPI/generated types/tests README，不能把文件與合約延到最後。

### 13.2 部分接受

- reviewer 建議以低階 `allow_input_required=True` 完全手動驅動 MRTR。計畫保留 SDK 高階 callback 作預設，因 v2 SDK 會自動 consume/retry；但加入硬閘：若無法證明 callback 後、retry 前完成 Touka 重新授權，就改用**同 coroutine、非持久化**的低階 driver。跨 restart durable driver 另列 P7-B，必須經維護者明確核准。

### 13.3 未接受

沒有實質發現被完全駁回；重複或屬同一根因的項目已合併到對應 phase、風險與驗收條件，未另造平行工作流。
