# ADR-0205：MCP ephemeral MRTR 與 Touka HITL

- **狀態**：Accepted
- **日期**：2026-07-31
- **相關**：[ADR-0198](0198-mcp-sdk-neutral-boundary-and-principal-handle-isolation.md)、
  [ADR-0203](0203-mcp-feature-families-and-schema-boundary.md)、
  [ADR-0204](0204-mcp-cache-subscriptions-and-boundary-hardening.md)、
  [MCP 2026 完整遷移計畫](../proposals/mcp-2026-07-28-protocol-migration.md)

## 脈絡

MCP `2026-07-28` 的 `tools/call`、`resources/read` 與 `prompts/get` 都可能回
`input_required`。Python SDK v2 的高階 client 會自動回呼並 retry，但沒有 Touka
可證明的「人類回覆後、上游 retry 前」重新授權 hook。直接使用高階路徑會讓等待
期間被撤銷的帳號、installation generation、MCP grant、工具政策或 PaC 仍可續行；
把 continuation state 送到 UI 或持久化則會擴張 bearer-like replay 面。

## 決策

### 1. 同 coroutine 的 ephemeral driver

- modern 三種 operation 使用低階 `client.session.*(...,
  allow_input_required=True)`；driver 保留 SDK 產生的新 request ID，將 opaque
  continuation state 原樣 echo，但只存在 coroutine stack frame。
- 每輪先完成 interaction，再重新驗證 active human／owner、installation enabled
  與 generation、MCP server/tool grant、per-installation tool policy、session owner、
  原始與目前較嚴格的 auth level，以及 PaC；失效即終止，不送 retry。
- 每輪、總生命週期、round 數、input 數、input/state/response bytes 都有硬上限；
  串流取消會取消等待與 sibling interactions。restart／stream loss 不續接、不 replay。

### 2. 行程內 interaction broker

- `ApprovalService` 泛化為 schema/payload/accepted/declined/cancelled 的一次性 broker，
  上限與 async lock 沿用既有 HITL。
- ToukaChat 只接收 `mcpint_*` local ID、訊息、扁平 primitive Draft 2020-12 form
  schema 與 deadline；接受、拒絕、取消使用三條具人類 ownership＋Origin 閘的端點。
- 上游 continuation state、使用者 response payload 與 schema 內容不進 DB、公開 API
  response、前端 history、log 或 OTel。巢狀／組合式 schema 與已棄用
  Sampling／Roots 明確 unsupported。

### 3. Headless 與 capability

- 缺精確 request context 或聊天 interaction sink 時立即 decline；heartbeat、
  dispatch、SafeAuto 或 FullYOLO 都不推導人類同意。
- Client 只宣告 elicitation callback；Sampling／Roots callbacks 不宣告。SDK 意外走
  無 operation context 的高階 callback 時亦 fail-closed decline。

## 結果與邊界

- 三種 operation 的 accept／decline／cancel／timeout／round cap／permission revoked
  與 state/response limits 皆有回歸測試。
- 接受 payload 會在消耗 pending interaction 前依受限 JSON Schema 驗證；不合法
  payload 回 422，pending 保持可重試。
- retry 前撤權保留精確 authorization error，且不計入上游 server 斷路器。
- 這是 P7 預設 ephemeral 模式。沒有建立 pending continuation list/respond API，
  process restart 後不可恢復；跨 restart 長工作應使用 Tasks。
- P7-B durable MRTR 未獲 owner 明示核准，刻意不實作。
- 未修改、掛載或遷移主機 `data/`。
