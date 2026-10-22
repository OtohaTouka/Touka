# ADR-0199：MCP Python SDK v2 原子切換

- **狀態**：Accepted（2026-10-16 dep1 補記：改釘 mcp 2.2.0、轉址改由 response hook 一律擋下，見末節；dep1 補記的「已知未解決」已由 [ADR-0273](0273-mcp-connection-owner-task.md) 解決）
- **日期**：2026-07-30
- **相關**：[ADR-0191](0191-mcp-hub-tier-a-shelf-and-version-pinning.md)、
  [ADR-0192](0192-mcp-remote-streamable-http-transport.md)、
  [ADR-0194](0194-mcp-remote-shelf-and-manual-remote-entry.md)、
  [ADR-0197](0197-mcp-2026-p0-cutover-baseline.md)、
  [ADR-0198](0198-mcp-sdk-neutral-boundary-and-principal-handle-isolation.md)、
  [ADR-0203](0203-mcp-feature-families-and-schema-boundary.md)（dep1 補記：呼叫當下核對工具清單）、
  [MCP 2026 完整遷移計畫](../proposals/mcp-2026-07-28-protocol-migration.md)

## 脈絡

P1 已把 Touka 內部工具描述、結果、錯誤與 principal handle 從 SDK v1 欄位解耦，但
workspace 仍固定 `mcp 1.26.0`，client 仍手動組 transport／`ClientSession.initialize()`，
自有 server 與 Creation scaffold 則依賴 `FastMCP`。只升 package 會讓 production
imports、測試替身與新生成產物同時失效；把 dependency、import 與 scaffold 拆成多個
commit 也會產生不可執行的中間狀態。

MCP Python SDK v2 同時改變 public client/server API、欄位命名、HTTP stack 與取消傳播。
因此 P2 必須是單一 uv workspace 的原子 cutover，且不得以 `protocol_mode=legacy`
冒充 package rollback。

## 決策

### 1. workspace 精確固定 SDK 與直接依賴

- ToukaProxy 與 ToukaDispatch 固定 `mcp==2.0.0`。
- ToukaProxy 直接使用 `httpx2`，故明列 `httpx2~=2.5.0`；兩 package 仍直接使用
  `httpx`，原依賴保留。
- ToukaMemory 已無 MCP import，移除 stale `mcp` dependency。
- 依 ADR-0117 以根 workspace 執行 `uv lock` 與 `uv sync --all-packages`；Docker、
  本機與 CI 共用同一 `uv.lock`。

### 2. runtime 只使用 SDK v2 public client

`connect_mcp_server` 統一建立 `Client(target, mode=..., cache=None)`：

- stdio target 由 `StdioServerParameters` 與 SDK transport 建立；
- remote target 使用 SDK v2 `streamable_http_client`，但 HTTP client 由 Touka factory
  注入；
- `protocol_mode` 預設由 `legacy` 改為 `auto`，單一 installation 仍可明示
  `legacy` 或 `2026-07-28`；
- negotiated `protocol_version`、`server_capabilities`、`server_info` 投影至 health；
  `server_info` 僅供顯示，禁止成為身分或授權依據。

P1 canonical boundary 改由 `sdk_adapter.py` 承接 v2 snake_case 欄位與 `MCPError`；
舊 `legacy_adapter.py` 移除。P2 仍不宣稱 scalar／array／null structured output 已完成
wire conformance，該驗收留在 P3。

### 3. remote HTTP stack 有唯一受控 factory

新增 `build_mcp_http_client()`，固定：

- 顯式 `SSLContext`，可注入私有 CA；
- connect/read/write/pool 四類 timeout；
- `follow_redirects=False`、`trust_env=False`；
- 診斷 header 採 allowlist，其餘全部遮罩；
- exception family 使用 `httpx2.TimeoutException`／`TransportError`。

HTTP client 必須早於 transport 建立、晚於 Client／transport 關閉。連線失敗、transport
內部取消與 teardown 二次失敗不得遮蔽原始原因；外部 task cancellation 仍須原樣傳播。
ADR-0191 的 startup deadline 保留，不因 v2 自帶取消傳播而刪除。

### 4. 所有 Python server 與 Creation 同批遷移

- ToukaDispatch、Grok Imagine、integration stub 改用 `MCPServer`，constructor 明示
  name/version；transport 參數移至 `run()`。
- Dispatch 在 P2 只升程式 API，仍使用 deprecated legacy HTTP+SSE；`/mcp`
  Streamable HTTP 雙掛與退場量測屬 P4-A。
- Creation scaffold 生成 SDK v2 `MCPServer`，handoff spec 明示 `sdk_major=2`。
- Creation smoke 與 runtime 共用 `connect_mcp_server`，但傳入
  `LaunchEnvironmentPolicy.clean` 的精確環境；新產物以 `auto` 與 `legacy` 兩模式
  都可真啟動並列出工具。
- 既有 Creation 產物與 `data/` 不自動修改。

SDK v2 同步 handler 會在 worker thread 執行；低階 handler 丟出的 `MCPError` 必須保留
JSON-RPC code/data，兩項以行為測試鎖定。

### 5. P2 有獨立、可機械執行的 cutover gate

inventory 保留全域 blocker gate，另新增 `--check-phase p2`。P2 gate 只阻擋：

- Python SDK 仍為 v1 或 stale dependency；
- production／generated Python 仍有 v1-only import、`FastMCP`、手動
  `initialize()` 或 camelCase SDK 欄位；
- Creation scaffold 未標記 SDK major 2。

ToukaIDE Node SDK v1 與 Dispatch legacy SSE 仍是全域 blocker，但屬 P4-A／P4-B，
不得讓它們把已完成的 P2 永久判紅。

## 結果與邊界

- Python runtime 已切至 `mcp 2.0.0`，P2 phase blockers 為 0；全域 inventory 仍誠實
  保留 P4 的 Node／legacy transport blockers。
- production Client、Creation、Grok 與 Dispatch imports 通過；Creation modern
  auto／legacy 真 subprocess smoke 通過。
- OpenAPI 與 ToukaChat 生成型別已同步，對外唯一變更為 `protocol_mode` 預設
  `legacy → auto`。
- 終態 inventory 為 1716 files／233 findings／25 個全域 blockers／0 unclassified，
  P2 blockers 為 0；pytest 4067 passed／4 skipped，Ruff F/I 全綠，44 道 guards
  除既知 #23 npm audit 外全綠。
- 本機 `touka-proxy` 映像建置成功；無 host volume 的暫用 container 已驗證
  `mcp 2.0.0`、`httpx2 2.5.0` 與 Proxy／Dispatch／Grok／Creation production imports。
  現行 compose stack 未重建。
- P2 不代表 MCP `2026-07-28` base conformance、modern Streamable HTTP server、
  OAuth、MRTR、Tasks 或任意 JSON structured output 已完成；下一階段是 P3 四格
  dual-era wire/conformance 與 container release gate。
- 真正回滾仍是 P0 驗證的 previous immutable image＋舊 lock＋相容 config；
  切到 `protocol_mode=legacy` 只改 negotiation，不會降回 SDK v1。

## dep1 補記（2026-10-16）：mcp 2.2.0 與轉址一律擋下

維護者核准 dep1。決策 1 的 `mcp==2.0.0`、決策 3「`follow_redirects=False` 就能禁止轉址」以本節為準。

### 版本

- ToukaProxy、ToukaDispatch 改釘 `mcp==2.2.0`；依禁令 53 跑 `uv lock --upgrade-package mcp`，`uv.lock` 只變 `mcp`、`mcp-types`（都是 2.0.0 → 2.2.0）。
- 原因有兩個：GHSA-rwrf-2pqf-9j8j（Proxy 實際受影響，見 [ADR-0203](0203-mcp-feature-families-and-schema-boundary.md) 的 dep1 補記）；CVE-2026-59951（Touka 實際不受影響，升級當縱深防禦，細節見 SECURITY.md）。
- 2.1.0 的兩個破壞性變動（未預期例外只回通用訊息；回傳型別標 content block 的工具不再給 outputSchema）不影響 Touka：Touka 的工具都回字串，`test_mcp_2026_p2_cutover.py` 相關測試在 2.2.0 照常通過。

### 轉址一律擋下

- 2.2.0 起 SDK 的 streamable HTTP 每次送出都自己蓋成 `follow_redirects=False`，再自己跟同源轉址（上限是 client 的 `max_redirects`）；httpx2 只在跨源時拿掉 Authorization，所以同源目標會收到憑證。Touka 傳的 `follow_redirects=False` 因此不再生效。
- `build_mcp_http_client` 加 response hook `_reject_redirect`：任何 3xx（包括沒有 Location 的 300）在 SDK 看到之前就丟 `McpRedirectRefusedError`，轉址目標不會收到任何請求。
  - 例外直接繼承 `httpx2.TransportError`：仍在 `MCP_HTTP_EXCEPTIONS` 裡，Dispatch probe 照樣接得到；不繼承 ConnectError／NetworkError，模型層的重試不會重試它。
  - 丟錯前先記一條 warning（連線階段上層會吞掉例外訊息）。網址去掉 query，訊息只有狀態碼與網址，不帶 Location、不讀回應內容（上層 log 會寫原始例外字串）。
  - `follow_redirects=False` 保留；OAuth、靜態 header、Dispatch probe 都經這個 factory，`client.py` 不改。
- **取捨**：2.0.0 時 3xx 只讓那一次請求失敗；現在整條連線失效。握手階段沿用 `connect_server` 的重試（共 3 次）與斷路器（失敗 3 次熔斷 30 秒）；已連線途中失效不會自動重連，工具呼叫以 `upstream_error` 回給模型，要手動重啟伺服器（`POST /v1/mcp/{name}/restart`）、重載設定或重新加入伺服器才會恢復。
- **已由 [ADR-0273](0273-mcp-connection-owner-task.md) 解決（life1）**：連線途中斷線會取消 lifespan（`tests/unit/test_mcp_midflight_transport_failure.py` 的三個 xfail 已轉綠；每條連線改由專屬 owner task 持有）。以下是當時的說明：shared 連線是在 lifespan task 裡建立的；途中任何一個 POST 失敗（ConnectError、ReadTimeout，以及加了 hook 之後的 3xx），anyio 都會把取消送到 lifespan，清理時其他連線也一起關掉。mcp 2.0.0 一樣有這個問題，hook 多了 3xx 這個觸發條件。

### 測試

- `tests/unit/test_mcp_redirect_security.py`：factory（同源 307／308、跨源 302、http→https 301、沒有 Location 的 300；呼叫時帶 `follow_redirects=True` 也擋；不讀回應內容、訊息與 log 不帶 query 和 Location）、真 SDK（轉址目標從未被請求、Authorization 只出現在原網址、OAuth 憑證提供者只被叫 1 次）、127.0.0.1 兩個 port 的真 socket；另有拿掉 hook 時 SDK 會跟同源轉址並重送憑證的對照組，SDK 行為改變時會提醒。
- `tests/unit/test_mcp_2026_p2_cutover.py`：版本改為 2.2.0，加 response hook 斷言。
- 突變：拿掉 hook、只擋跨源、例外改繼承 ConnectError、訊息帶 Location，各自都會讓測試轉紅。
