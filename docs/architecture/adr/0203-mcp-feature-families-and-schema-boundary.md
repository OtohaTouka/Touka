# ADR-0203：MCP feature families 與 Schema 安全邊界

- **狀態**：Accepted（2026-10-16 dep1 補記：呼叫 MCP 工具當下核對伺服器工具清單，見末節；dep1 補記已知限制的 lifespan 那一點已由 [ADR-0273](0273-mcp-connection-owner-task.md) 解決；2026-10-21 補上標頭參數超限，見 dep1 補記決策末條）
- **日期**：2026-07-30
- **相關**：[ADR-0198](0198-mcp-sdk-neutral-boundary-and-principal-handle-isolation.md)、
  [ADR-0199](0199-mcp-python-sdk-v2-atomic-cutover.md)（dep1 補記：mcp 2.2.0 與轉址 hook）、
  [ADR-0200](0200-mcp-base-conformance-and-release-gate.md)、
  [ADR-0269](0269-advertised-tool-list-enforcement.md)（h2：這一輪列給模型的清單）、
  [MCP 2026 完整遷移計畫](../proposals/mcp-2026-07-28-protocol-migration.md)

## 脈絡

P0～P4-B 已完成 SDK v2、modern/legacy transport 與 Touka 自有 server 遷移，但 host
資料面仍只把 tool descriptor 提早壓成 provider function schema，且沒有 canonical
Resources／Prompts API。若直接把 SDK model、URI 或外部 prompt 交給 UI／模型，會產生
descriptor 資訊遺失、分頁只取第一頁、URI 被誤當本機路徑、prompt injection／敏感資料
外洩及惡意 JSON Schema 消耗資源等問題。

## 決策

### 1. Canonical descriptor 先完整保存，provider 最後投影

- Tools 保存 title、input/output schema、annotations、icons 與 `_meta`；所有 cursor
  走完後才依安裝政策過濾，provider function schema 是最後一步投影。
- 單一不合法 descriptor 進 `issues` 隔離，不讓整個 catalog 失效，也不得進廣告面。
- Resources、resource templates、Prompts 與讀取／取得結果皆使用 SDK-neutral Pydantic
  canonical model；ToukaChat 型別只引用 OpenAPI codegen。

### 2. Feature capability 與 dual-era 行為

- manager 提供 tools/list、resources/list、resources/templates/list、resources/read、
  prompts/list、prompts/get 的完整分頁 API。
- 對端未宣告 capability 或回 `-32601` 時回 `supported=false`；其他 protocol／transport
  錯誤 fail-loud，不把故障偽裝成「未支援」。
- modern `2026-07-28` 與 isolated legacy `2025-11-25` 真 stdio peers 共用相同
  canonical 驗收。ToukaMemory 維持 ADR-0116 integration-only，不重新包成 MCP resource。
- P6 前 `Client(cache=None)` 維持不變；本 ADR 不提前建立第二套 cache owner。

### 3. URI、內容與權限邊界

- MCP resource URI 只是不可信的絕對識別字串；`file:`、相對 URI、Windows drive path、
  控制字元與反斜線皆拒絕，永不交給本機 filesystem API。本機資料仍只經
  `ResourceManager`。
- catalog page/item/aggregate/cursor、resource text/blob/MIME/encoding、prompt
  message/argument/content 與序列化結果皆有有限上限；base64 使用 strict validation。
- resource/prompt 文字執行 prompt-injection 標記與高信心 DLP redaction；外部 prompt
  結果固定 `trust="untrusted"`，UI 明示只能當資料。
- P5 data plane 除 Owner／Debugger／admin 管理閘外，仍須通過
  `PermissionService.is_mcp_server_allowed()`；read/get POST 另保留 CSRF Origin 閘。

### 4. JSON Schema 2020-12

- 無 `$schema` 時明示補成 Draft 2020-12；顯式其他 dialect 隔離該 tool。
- input schema root 必須宣告 object；output schema 可為任何 JSON root。
- 支援 composition、conditionals、本地 JSON Pointer／anchor refs；`$ref` 與
  `$dynamicRef` 的外部來源一律不抓，unresolved internal ref 亦拒絕。
- 在 `jsonschema.Draft202012Validator` 前先執行 encoded bytes、深度與 subschema
  上限，再施加 validation-time budget。`jsonschema` 升為 Proxy 直接依賴，不依賴
  MCP SDK 的傳遞依賴。

## 結果與邊界

- Proxy 新增具 Pydantic response model 的 resources/templates/read 與 prompts/get
  端點；OpenAPI 與 ToukaChat generated API 同步。
- ToukaChat MCP 設定面拆為 Tools／Resources／Prompts hooks 與 views；只顯示對端
  `supported=true` 的功能，binary 只顯示 MIME／大小，不把 base64 當文字渲染。
- schema corpus、pagination loop、惡意 URI、DLP、prompt injection、權限、完整
  descriptor、任意 JSON output 與 modern/legacy 真對端皆有回歸測試。
- P5 只完成 host data plane；Subscriptions/cache/header projection/icons fetch/OTel
  屬 P6，MRTR/HITL 屬 P7。故仍不可宣稱 `Touka MCP host product complete`。
- 未修改、掛載或遷移主機 `data/`。

## dep1 補記（2026-10-16）：呼叫 MCP 工具當下核對伺服器工具清單

Owner 核准 dep1，作為 GHSA-rwrf-2pqf-9j8j 的縱深防禦；SDK 本身的修正是升級 mcp 2.2.0（見 [ADR-0199](0199-mcp-python-sdk-v2-atomic-cutover.md) 的 dep1 補記）。

### 背景

- mcp 2.0.0 的客戶端驗證工具結果時，會依伺服器的 outputSchema 同步、無逾時地抓 http／https／file 的外部 `$ref`。決策 4 只擋 Touka 自己驗的那份 schema（深拷貝）；SDK 用的是自己快取的原始 schema，工具不在快取時還會自己重列、直接採用新 schema。
- 決策 1 的隔離只擋給模型看的清單。呼叫前雖然會重列，但原本只擋標頭隔離，所以有三條路會走到 SDK 驗證：給模型的清單乾淨、呼叫當下換成惡意 schema；呼叫當下故意不列這個工具；人按的 `/debug/execute`（不看這一輪的清單）。

### 決策

`_call_tool_on_authorized_server` 重列之後，下列情況一律丟 `McpToolCatalogRefusal`、不呼叫 SDK：

| 情況 | 錯誤類別 | reason |
|---|---|---|
| 同名工具有任何隔離紀錄（`McpIcon:` 開頭的除外）；同名不只一筆 | `permission_denied` | `mcp_tool_quarantined` |
| 當次清單找不到；伺服器不支援列工具（沒宣告 tools、沒有 list_tools、回 -32601） | `resource_not_found` | `mcp_tool_not_listed` |

- icon 問題只記紀錄、工具仍在清單上（icon 預設開啟、遠端抓 icon 可能失敗），算成隔離會讓正常工具隨機叫不到，所以排除。
- 同名多筆：SDK 只留最後一筆 schema，無法確定它驗的是哪一份，當成隔離。
- 拒絕不是上游故障：不計入斷路器、不寫健康頁的 last_error，只記 warning。順帶修好既有錯誤：標頭隔離原本被包成 `upstream_error` 並計入斷路器，同一個被隔離的工具叫 3 次就讓整台伺服器熔斷 30 秒。
- 重列本身失敗照舊回 `upstream_error`，同樣不呼叫 SDK。
- 所有入口都適用，`/debug/execute` 也不例外：威脅來自伺服器的 schema，人按的呼叫一樣會被抓外部 ref。
- `tool_executor.py` 在既有 MCP 例外處理旁接住這個例外，用 `build_tool_error` 回結構化錯誤（禁令 59），不新增錯誤碼。
- **2026-10-21 補記：標頭參數超限**。上表只涵蓋清單；最後一步 `validate_header_arguments`（模型給的參數放進標頭後單值超過 2048、總和超過 8192 bytes）原本仍走一般例外：寫健康頁、計入斷路器、回 `upstream_error`。改丟 `McpToolArgumentRefusal`，執行器回 `validation_error`（`reason=mcp_header_argument_invalid`，訊息點名標頭、不含參數值）。兩種拒絕共用 `McpToolCallRefusal`，對斷路器中立（`core/circuit_breaker.py` 的 `BreakerNeutralError`）：不記失敗也不記成功。原本「記成功」會讓穿插的拒絕把真的失敗計數歸零、把半開的斷路器關回去，而清單可能走快取、這次根本沒碰到伺服器。呼叫當下 `inspect_tool_header_policy` 失敗（只在核准於呼叫開始與列清單之間改變的競態）不改，仍是 `upstream_error`。測試同 `test_mcp_call_time_catalog_security.py`。
- **和 h2（[ADR-0269](0269-advertised-tool-list-enforcement.md)）的分工**：h2 在角色權限之後、PaC 之前，依「這一輪列給模型的清單」核對，只在模型迴圈；本節在審批之後、呼叫當下重列之後，依「伺服器當下的清單與隔離紀錄」核對，沒有開關、所有入口都適用。manager 不讀請求 ctx 與這一輪的清單。

### 已知限制

- **已由 [ADR-0273](0273-mcp-connection-owner-task.md) 解決（life1）**：連線途中斷線會取消 lifespan（`tests/unit/test_mcp_midflight_transport_failure.py` 的三個 xfail 已轉綠）。
- 【推測】同一條共用連線上，別的呼叫若同時重列，可能在 Touka 檢查之後、SDK 驗證之前換掉 SDK 快取的 schema；真正的修正是 SDK 2.2.0 本身不再抓外部 ref。
- 【推測，未測】同源 icon 的抓取若在網路層丟例外（不是回非 200 狀態碼），整份清單會失敗，工具呼叫回 `upstream_error`。這是既有行為，本節沒有改。

### 測試

- `tests/unit/test_mcp_call_time_catalog_security.py`：外部 `$ref`／dialect／上限／形狀四種隔離都拒絕、SDK 呼叫 0 次；標頭隔離改回 `permission_denied`；同名兩筆；不在清單；不支援列工具三種；icon 問題（跨源、抓取失敗）照常可呼叫的陽性對照；連續拒絕 4 次斷路器仍是 CLOSED、last_error 是 None；重列失敗仍不呼叫；真 ToolExecutor＋真 manager 在模型迴圈與 `/debug/execute` 兩種開關下分類都正確。
- `tests/unit/test_mcp_external_ref_security.py`：GHSA-rwrf-2pqf-9j8j 的回歸測試。這筆公告不在 OSV／PyPI，弱點掃描（Guard #22）看不到，這個檔是唯一的守門員；在 2.0.0 上先跑出紅燈（SDK 每條呼叫路徑都抓了外部 ref），升級後轉綠。
- 既有替身補 `list_tools`：`test_mcp_2026_p1_boundary.py`、`test_mcp_health.py`、`test_mcp_workload_manager.py`，斷言不變。
- 突變：拿掉 icon 排除、拒絕計入斷路器、不在清單放行、照寫 `_record_error`、執行器不接這個例外，各自都會讓測試轉紅。
