# ADR-0202：ToukaIDE Node SDK v2 與 Streamable HTTP 遷移

- **狀態**：Accepted
- **日期**：2026-07-30
- **相關**：[ADR-0199](0199-mcp-python-sdk-v2-atomic-cutover.md)、
  [ADR-0200](0200-mcp-base-conformance-and-release-gate.md)、
  [ADR-0201](0201-toukadispatch-streamable-http-and-request-auth.md)、
  [ADR-0221](0221-modern-only-local-cold-cutover.md)、
  [MCP 2026 完整遷移計畫](../proposals/mcp-2026-07-28-protocol-migration.md)

> **2026-09-04 適用狀態修訂**：Owner 已依 ADR-0221 選定 `modern-only cold cutover`
> 目標，但仍為 `implementation pending`。在 IDE 自有的 modern auth／version smoke、tracked
> consumer inventory、rollback seal 與 fresh exact removal 核准完成前，本 ADR 的 dual-era
> routes 與 discovery projection 仍是現行事實。

## 脈絡

P4-A 後，Touka 自有 Python MCP servers 已提供 modern transport，但 ToukaIDE 仍使用
舊的 `@modelcontextprotocol/sdk` v1 低階 `Server` 與 legacy HTTP+SSE。Proxy 的 IDE
adapter 也直接手刻未 initialize 的 `tools/call` HTTP JSON-RPC，無法履行 P3 的
保守式 `server/discover` 協商。

實作前另確認：Node 穩定 v2 不再由舊 umbrella package 提供，而是
`@modelcontextprotocol/server`、`node`、`server-legacy` 與 `client` 四個分拆套件；
因此原計畫中的「升級 `@modelcontextprotocol/sdk`」在落地時修正為移除 umbrella
package，將四個穩定 v2 package 精確釘在 `2.0.0`。這些套件要求 Node 20，故
ToukaIDE 的 VS Code engine 同步提高至 `^1.90.0`。

## 決策

### 1. modern `/mcp` 為主路徑

- `/mcp` 使用 v2 `createMcpHandler`＋`McpServer`，拒絕 legacy wire；
- `/mcp/sse`＋`/mcp/messages` 使用 `server-legacy` 的獨立 server／transport；
- legacy session 以 `sessionId` 分流，多個 client 不共用 transport；
- server 只綁 `127.0.0.1`，並以 SDK 的 localhost Host／Origin validator 再擋一層。

legacy endpoint 是不設期限的受控相容窗；新整合只讀 discovery 的 `/mcp`。sunset
日期仍待 owner 根據實際使用狀況明示，不由本 ADR 擅自設定。

### 2. Bearer 是逐 HTTP request 的必要條件

modern POST、legacy SSE GET 與 legacy message POST 都先經同一個 middleware：

- 未帶或錯誤 token 回 401；
- server 啟動時 token 若為空，回 503 fail-closed，不存在「開放本機模式」；
- 固定長度 token 使用 constant-time 比對；
- token 只存在 OS temp discovery file 與 Proxy process memory，不寫入 log，也不送
  進 Webview。

MCP session、`clientInfo`、protocol `_meta` 或 loopback 身分都不能取代 Bearer。

### 3. discovery 與 Proxy client 共用 v2 協商

discovery contract 新增：

```json
{
  "transport": {
    "type": "streamable-http",
    "endpoint": "/mcp",
    "protocolMode": "auto",
    "legacy": {
      "sseEndpoint": "/mcp/sse",
      "messagesEndpoint": "/mcp/messages"
    }
  }
}
```

Proxy 只接受本機絕對 path、合法 port、非空 token 與已知 protocol mode；外部 URL、
query／fragment 與 `//host` 全拒。IDE handler 不再手刻 JSON-RPC，改走 P2 共用
`connect_mcp_server()`，因此 `auto` 真送 `server/discover`，且只有
`-32601 Method Not Found` 才能依 ADR-0200 規則降級。

### 4. 保留 IDE 安全語意並讓取消進到子程序

- AuthLevel 決策抽為純政策；Manual 每次執行、SafeAuto 非白名單與 FullYOLO 黑名單
  都進 VS Code `{modal: true}` HITL；
- ReadOnly 禁止寫入與終端；工作區檔案輸入先經 `path.relative` 邊界，拒絕 traversal、
  外部絕對路徑與無 workspace 狀態；
- modern response 固定為 SSE，100ms keepalive 讓斷線可及時被 Node 看見；HTTP stream
  關閉後 AbortSignal 會傳到 terminal handler，殺掉子程序且只結算一次；
- tool schema 與 executor 分離，transport 不直接依賴 VS Code handler 實作。

## 結果與邊界

- Node conformance 涵蓋 modern `2026-07-28`、legacy、缺少／錯誤 Bearer、多 client、
  disconnect/cancel、AuthLevel、modal HITL 與 workspace path jail。
- ToukaIDE 測試 **43 passed／10 files**；Python P4-B 目標組 **34 passed**；typecheck、
  esbuild compile 與 minified package 全綠；完整 pytest **4104 passed／5 skipped**；
  guards **43/44**，唯一紅燈仍為既有 ToukaChat Guard #23 npm audit。
- inventory 已加入 `p4b` phase gate，會攔截缺 package、`^/~` 浮動 v2 版本與
  manifest/lock 漂移；P4-B 後全 repo blocker 為 0。
- `npm audit fix` 加上 esbuild 0.28.1 升級，將 ToukaIDE advisory 由 11 降至 2；
  剩餘均為 `@modelcontextprotocol/node@2.0.0` 上游鎖住且目前無相容修法的
  moderate `@hono/node-server` advisory。未以 `--force` 或 npm override 越過
  SDK 鎖定。
- 至此可宣稱 **`Touka-owned servers migrated`**；仍不可宣稱完整
  **`MCP 2026 base conformant`** 或 host product complete，Resources／Prompts、
  subscriptions、MRTR、OAuth、Tasks 與 Apps 仍屬 P5 以後。
- ToukaIDE 是 VS Code extension，沒有對應 container image；本階段以 loopback
  ephemeral-port 真 SDK transport conformance 與 package build 取代假容器探針。
- 未修改、掛載或遷移主機 `data/`。
