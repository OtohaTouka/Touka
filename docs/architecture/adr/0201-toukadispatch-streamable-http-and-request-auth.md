# ADR-0201：ToukaDispatch Streamable HTTP 與逐請求應用認證

- **狀態**：Accepted
- **日期**：2026-07-30
- **相關**：[ADR-0064](0064-toukadispatch-mission-token.md)、
  [ADR-0082](0082-dispatch-in-proxy-consolidation.md)、
  [ADR-0200](0200-mcp-base-conformance-and-release-gate.md)、
  [ADR-0221](0221-modern-only-local-cold-cutover.md)、
  [MCP 2026 完整遷移計畫](../proposals/mcp-2026-07-28-protocol-migration.md)

> **2026-09-04 適用狀態修訂**：Owner 已依 ADR-0221 選定 `modern-only cold cutover`
> 目標，但仍為 `implementation pending`。在 exact removal candidate 完成 inventory、modern
> 證據、rollback seal 與 fresh 核准前，本 ADR 的 dual-era endpoint／request auth／release probe
> 仍是現行事實；modern request-scoped SSE 不是 legacy endpoint，不得誤刪。

## 脈絡

P2 已將 ToukaDispatch 升到 Python SDK v2 `MCPServer`，但正式入口仍是已棄用的
legacy HTTP+SSE。P3 的容器閘也只證明 Dispatch 可 import、8083 TCP socket 可連，
沒有證明已發布的服務真的提供 modern `/mcp`。

Dispatch 另有獨立於 MCP transport 的應用授權：內部 Touka 以 master
`DISPATCH_TOKEN` 換 session token，外部 AI 則以 mission token 換受限 session。
若遷移時把授權綁在 MCP session、連線或呼叫方自述的 `clientInfo`，stateless modern
request、transport 重連或 legacy 相容層都可能繞過既有 mission／method 邊界。舊
`SessionManager` 還把 bearer 明文當 dict key，mission 替換與舊外部 session 的撤銷
也不是同一個原子操作。

## 決策

### 1. `/mcp` 是主路徑，legacy SSE 是隔離相容層

ToukaDispatch 以單一 Starlette app 明確掛載：

- `/mcp`：SDK v2 Streamable HTTP，`stateless_http=True`；
- `/sse`＋`/sse/messages/`：legacy HTTP+SSE 相容窗；
- `/health`：readiness 與匿名相容窗 telemetry。

modern 與 legacy 各自使用獨立 `MCPServer` 實例及 lifespan transport 分面，不共用
MCP transport session state。應用 session 仍可共用同一個 manager，但每筆 session
固定綁定建立它的 transport；拿 modern session token 呼叫 legacy，或反向使用，
一律拒絕。

legacy sunset 日期不在本 ADR 擅自決定。先觀察使用量，再由維護者明示期限；在此
之前 `/sse` 是 deprecated compatibility endpoint，不是新整合的預設入口。

### 2. 應用授權逐 tool request 執法

`dispatch_handshake` 仍保留參數式 application credential exchange；之後每次
`dispatch_initialize_mission`、`dispatch_update_task`、`dispatch_get_board` 都
顯式帶 `session_token`，並重新驗證：

- expiry；
- server-owned transport；
- internal／external audience 與 caller type；
- method allowlist；
- external mission generation。

MCP session ID、連線生命週期、`serverInfo`、`clientInfo` 或 client `_meta` 都不參與
授權。直接呼叫 Python 函式的舊測試使用獨立 `direct` transport 分面，避免測試假裝
成 modern 或 legacy。

### 3. bearer 明文不進 credential store

`SessionManager` 只在建立時回傳 bearer 明文；記憶體 store 以 SHA-256 digest 為鍵，
驗證時對固定長度 digest 使用 `secrets.compare_digest`。紀錄只保留執法必要分面：
token hash、固定 DispatchAgent、audience、caller type、transport、allowed methods、
mission/generation 與 expiry；呼叫方自述的名稱／版本不保存。

mission token 是一次性 handshake credential：驗證與消耗在同一把鎖內完成，第二次
使用立即拒絕。建立新 mission 時，generation 遞增、舊 mission credential 清除及舊
external session purge 在同一把鎖內完成。若輪替恰發生在 credential 消耗與 external
session 建立之間，新 session 仍帶舊 generation，第一次 tool call 即 fail-closed。
程序重啟不還原任何 session 或 mission credential。

### 4. `DISPATCH_URL` 是完整 modern endpoint

外部子程序收到：

```text
DISPATCH_URL=http://localhost:8083/mcp
```

呼叫方不得再自行附加 `/sse`。需要舊 client 時，必須明示改連
`http://localhost:8083/sse`；modern 與 legacy 設定不可混用同一個 application
session token。master `DISPATCH_TOKEN` 仍不得進外部子程序，外部只拿 mission
token。

### 5. readiness、發版閘與 telemetry 驗真協定

`DispatchSupervisor` readiness 不再用裸 TCP socket，改以受控 `httpx2` client 對
`/mcp` 執行 modern `server/discover`／`tools/list`，並以一秒外層 deadline 限制
每次 probe。release workflow 則從已發布的 8083 真跑：

- modern `/mcp` discover＋tools/list；
- legacy `/sse` initialize＋tools/list。

2026-09-01 hardening：發版 probe 另須精確接受 modern `2026-07-28` 與
legacy `2025-11-25`；任一協商版本不符即非零失敗，不得只因工具集合完整而產生
合格結論。兩個版本以鎖定 MCP SDK 的公開 version registry 為 SSOT，分別引用
`LATEST_MODERN_VERSION` 與 `LATEST_HANDSHAKE_VERSION`，不得在 probe 重複硬編碼。

`/health` 只提供行程內匿名聚合：modern／legacy connection/request 計數、成功
handshake 的 internal／external caller type，以及各 transport 最近使用時間與
protocol version。不保存 bearer、caller name 或 mission ID；不先建立 persistence，
也不先寫 sunset 日期。

## 結果與邊界

- modern／legacy client 都能 handshake、讀板與更新；跨 transport、外部 initialize、
  mission replay、舊 generation、rotation 及 restart 都 fail-closed。
- Python legacy SSE inventory blocker 已清除；全域剩餘 blocker 屬 P4-B ToukaIDE
  Node SDK／transport。
- container publish gate 現在驗真 `/mcp` 與 `/sse`，不再讓「TCP 可連、協定已壞」
  的映像假綠。
- Starlette 是 ToukaDispatch 直接 import，故明列於其 `pyproject.toml` 並由單一
  `uv.lock` 管理。
- P4-A 目標測試 **24 passed**；完整 pytest **4100 passed／4 skipped**，Ruff F/I
  全綠，guards **43/44**（唯一紅燈仍為既有 Guard #23 `npm audit`）。
- inventory **1726 files／260 findings／24 blockers**；Python legacy SSE blocker
  已清除，剩餘 blocker 全屬 P4-B ToukaIDE Node 面。
- 無 host volume 的本機映像 `touka-proxy:p4a-local`
  （`sha256:22f08620a289f98b9ab08378a88c6519dc8e8a3b7907187fac7ddd95f1bf59e0`）
  已從隨機發布埠真連 modern `/mcp` 與 legacy `/sse`；未推 GHCR、未重建現行
  stack。
- 本 ADR 不代表 ToukaIDE 已遷移，也不宣稱 Resources／Prompts、cache/subscription、
  MRTR、OAuth、Tasks 或 Apps 已完成。
- 未修改、掛載或遷移主機 `data/`。
