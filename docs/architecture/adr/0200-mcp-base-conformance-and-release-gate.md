# ADR-0200：MCP 2026 base conformance 與容器發版閘

- **狀態**：Accepted
- **日期**：2026-07-30
- **相關**：[ADR-0197](0197-mcp-2026-p0-cutover-baseline.md)、
  [ADR-0198](0198-mcp-sdk-neutral-boundary-and-principal-handle-isolation.md)、
  [ADR-0199](0199-mcp-python-sdk-v2-atomic-cutover.md)、
  [ADR-0201](0201-toukadispatch-streamable-http-and-request-auth.md)、
  [MCP 2026 完整遷移計畫](../proposals/mcp-2026-07-28-protocol-migration.md)

> **後續修訂**：ADR-0201 已將本 ADR 的 Dispatch supervisor socket 閘升級為
> 已發布 8083 的真 modern `/mcp`＋legacy `/sse` 協定 probe；P3 的 TCP-only
> 描述保留為當時歷史事實，不再是現行發版契約。
>
> **2026-09-01 後續修訂**：容器內 `src.protocols.mcp.release_probe` 亦從鎖定
> MCP SDK 的公開 version registry 取得版本 SSOT；modern discover 必須 exact
> `2026-07-28`，legacy initialize 必須 exact `2025-11-25`。版本不符即回報
> era／expected／actual 並非零退出，成功的 `tools/list` 不得掩蓋錯版。

## 脈絡

P2 已把 Python runtime 原子切到 SDK v2，但 package/import 成功不等於 wire contract
正確。尤其 SDK v2 的一般 `mode="auto"` 會把多種 `server/discover` 失敗交給 legacy
fallback；對 Touka 而言，missing metadata、header mismatch、missing capability 或
unsupported version 都代表 modern peer 壞掉，若靜默改走 `initialize`，會把真正的協定
回歸偽裝成相容成功。

此外，P1 canonical complete outcome 仍把 `structuredContent` 限成 object 或缺席，
無法區分欄位缺席與顯式 JSON `null`。CI 也只驗映像可啟動，尚未在 publish 前驗證
modern/legacy 協商、Dispatch import 與 supervisor runtime。

## 決策

### 1. Touka 擁有保守式 auto negotiation

`connect_mcp_server` 不再把 `auto` 原封不動交給 SDK：

1. 以最新 modern mode 建立一次全新 transport 與 public `Client`。
2. 明確呼叫 public `session.send_discover()`，驗證為 `DiscoverResult` 後 `adopt()`。
   不使用已帶 synthetic discovery state 的 `session.discover()`，避免零 wire traffic
   的假 probe。
3. 只有明確 JSON-RPC `-32601 Method Not Found` 才關閉 modern stack、重開全新
   transport，改以 `legacy` 執行 `initialize`。
4. `-32602`、`-32020`、`-32021`、`-32022`、validation、transport 與 timeout
   全部保留，不得觸發 legacy fallback。

每個 attempt 都有獨立 `AsyncExitStack`；失敗 teardown 不遮蔽原始原因，transport
內部洩漏的取消仍正規化為一般連線錯誤，外部 task cancellation 原樣傳播。

### 2. complete outcome 保留完整 JSON 值域

`McpCompleteOutcome.structured_content` 改為任意 JSON 值，另以
`structured_content_present` 保存欄位是否出現。adapter 透過 Pydantic
`model_fields_set` 或 dict key presence 區分：

- 欄位缺席；
- 顯式 `null`；
- object、array、string、number、boolean。

`ToolExecutor` 最末端 projection 對顯式 `null` 輸出 `null`，不再誤降成空 content
陣列。modern wire 缺 `resultType` 必須 validation fail；legacy 缺省仍由既有 P1
adapter 視為 `complete`。

### 3. conformance 使用隔離四格與 negative corpus

新增 `stdio × Streamable HTTP × modern v2 × isolated legacy` 測試：

- modern peer 使用真 SDK v2 `MCPServer`；
- legacy peer 不 import MCP SDK，手刻最小 JSON-RPC，防止新 SDK 替測試偷偷補齊語意；
- modern HTTP 驗每訊息 POST、per-request `_meta`、JSON／request-scoped SSE、
  無 session/redelivery header、Origin 與無 GET；
- negative corpus 固定 `-32602/-32020/-32021/-32022/-32601` 的 HTTP/code 分配；
- lifecycle 驗 caller/transport cancel、teardown 與 no residual task；
- TLS 由測試現場簽發本機 CA，透過既有 `httpx2` factory 注入，不依賴 live provider。

測試簽發 CA 直接依賴 `cryptography~=49.0.0`，納入根 dev group 與 `uv.lock`。
P0/P2 inventory 明列排除上述 raw-wire conformance 檔；其中 camelCase 是被測 wire，
不是 production v1 model surface。

### 4. 發版前必須在真容器內通過 MCP/Dispatch 閘

新增 `src.protocols.mcp.release_probe`，在真 `MCPServer` 上分別驗證：

- modern `server/discover` 與 exact `2026-07-28`；
- legacy `initialize` 與 exact `2025-11-25`；
- 兩者 `tools/list`。

兩個 exact version 均取自鎖定 MCP SDK 的公開 version registry；協商版本不符時，
probe 必須回報 era／expected／actual 並以非零狀態結束，不得只憑 negotiation 或
`tools/list` 完整便讓發版閘通過。

`.github/workflows/release-images.yml` 的 `gate` job 在 fresh-volume boot 後執行：

1. 容器內 release probe；
2. `/app/ToukaDispatch` production `server` import；
3. supervisor 的 `127.0.0.1:8083` socket。

任一失敗都阻止 `publish`。Guard #42 同步檢查三類 marker，且有移除整個 conformance
step 即轉紅的回歸測試。cleanup 改成 `if: always()`，前一步失敗也會 `down -v`。

## 結果與邊界

- P3 四格、negative wire、任意 JSON、TLS、SSE、cancel/teardown 與本機 runtime
  conformance 全綠。
- 本機映像 `touka-proxy:p3-local`
  （manifest `sha256:160517c91095413804ffe93a06674c6a55ba080c0264723ab8fc915ad3c00f84`）
  建置成功；無 host volume、無 published port 的隔離容器已驗 Proxy health、release
  probe、Dispatch import 與 supervisor socket，驗後已移除。
- 新增 P3 conformance pytest 20；完整 pytest 4087 passed／4 skipped，Ruff F/I、
  ADR／release／inventory 閘全綠；全域 guards 43/44，唯一紅燈仍是既知
  Guard #23 npm audit。
- **2026-09-01 hardening follow-up**：Proxy 容器 release probe 新增 modern／legacy
  exact-version negative oracle；P3 檔增至 22 tests 並全綠，固定 seed `20260901`
  的完整 Python 3.12 suite 為 5181 passed／9 skipped／1 warning。
- CI workflow 尚未因本次工作推送或實際發布 GHCR；使用者仍保有發版時機主權。
- P3 完成的是 Python host 的 base wire/transport conformance 閘，不代表
  ToukaDispatch 已提供 modern `/mcp`、ToukaIDE 已升 Node SDK v2，也不代表 OAuth、
  Resources/Prompts、cache/subscription、MRTR、Tasks 或 Apps 已完成。
- 依完整計畫，只有後續 P6/P7/P11 等門檻一起完成後，才能宣稱整體
  `MCP 2026 base conformant`。
- 未修改或掛載主機 `data/`。
