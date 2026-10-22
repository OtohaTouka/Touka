# ADR-0187: MCP Hub 社群來源改接官方 MCP Registry（汰換失效的 Smithery CLI 路線）

## 狀態

✅ 已接受（MCP connectors 調查 F3，維護者點名修復；接續 ADR-0186 的 F1/F2）

## 日期

2026-07-25

## 背景

舊社群腿 `fetch_smithery_servers` 把 api.smithery.ai 匿名清單（本身仍活，07-24 實測 228 條 verified）映射成安裝命令 `npx -y @smithery/cli run <qualifiedName>`，三重失效：

1. **`run` 命令已不存在**：現行 `@smithery/cli` v4.11.1 `--help` 實證（2026-07-25）命令面＝mcp／tool／trigger／skill／auth／setup／namespace——**無 `run`**＝貨架 ~100 條全數安裝即死。
2. **launcher 未釘版**：`npx -y @smithery/cli` 恆抓 latest＝本次漂移的直接成因（第三方 CLI 子命令無 registry 可查證、漂移無聲）。
3. **`env_requirements` 硬編 `[]`**：安裝精靈永不提示憑證＝盲裝。

釘舊版 CLI＝復活已被棄用的介面、且 `run` 歷來需 Smithery 帳號 key——違背 local-first，不是修復。

## 決策與實作

**社群來源改接官方 MCP Registry v0**（registry.modelcontextprotocol.io——匿名可用、官方維護、條目自帶 `packages`／`transport`／`environmentVariables`，形狀 07-25 逐項實證含 `_meta["io.modelcontextprotocol.registry/official"]` 鍵名）：

- **`src/utils/mcp_registry_entry.py`（新純模組，SSOT）**：`registry_item_to_hub_entry(item) -> dict | None`——
  - 只收**最新版且 active**（`isLatest`＋`status`）；
  - 只收 **stdio** transport 的 npm/pypi 套件：npm → `npx -y <identifier>`、pypi → `uvx <identifier>`（與 `mcp_official_map` 同一套執行面，**零第三方 launcher 依賴**）；
  - `environmentVariables.isRequired` → `env_requirements`（安裝精靈逐項提示＝修盲裝）；
  - `packageArguments` 僅取有具體 `value` 者（named → `[name, value]`）；必填無值＝無法無人值守安裝 → `None`；remote-only／oci／非 stdio → `None`（stdio-only client，不可用者誠實不上架）；
  - `repository.url`／`websiteUrl` → `source_url`（供應鏈可溯性）；npm 條目帶 `npm_package` 沿用既有真實下載量排序。
- **`fetch_registry_servers`**（取代 `fetch_smithery_servers`，後者連同 `SMITHERY_API_URL` 移除）：`metadata.nextCursor` 分頁、上限 `_REGISTRY_MAX_PAGES=5` 頁 ×100 條——達上限仍有 cursor 即 **WARNING 截斷有聲**（no silent caps）；納入／略過計數落 log。
- **`sync()`**：合併序 curated → official → registry；「雙外源全空不覆寫」保護不變。
- **`MCPHubServerItem` 補 `icon`／`source_url`**：FastAPI `response_model` 會剝除未宣告欄位——前端 `McpHubServerView` 早已消費這兩欄（`mcp_version.ts` 自註「後端 schema 未涵蓋」），先前實際被靜默剝除；本批補列＋pin 測試釘死。
- **前端零改動**（型別與渲染早已就緒）。

### 變異驗證（逐條隔離、皆已還原）

| 變異 | 結果 |
|------|------|
| M1 mapper npm runtime 錯置為 uvx | 4 條 npm 斷言測試紅（pypi/uvx 與其餘全綠） |
| M2 移除 isLatest 過濾 | 僅 `test_non_latest_or_inactive_returns_none` 紅 |
| M3 sync 合併丟 registry 腿 | 僅 `test_sync_includes_registry_leg` 紅——此 pin 係變異演練發現原測試組殺不死 M3 後補上（mutation-driven 補強） |
| M4 response model 刪 `source_url` | 僅 `test_hub_response_model_carries_icon_and_source_url` 紅 |
| baseline | MCP 相關六檔 36 passed 全綠 |

### E2E 實跑（走生產碼路徑、hub/config DI 至 tmp）

真 `sync()`（官方 GitHub＋官方 Registry 皆 live）＝42 條入檔：official 7（ADR-0186 規格不變）＋**registry 35**（500 條掃描、誠實過濾後；npx 31／uvx 4；**5 條帶真實 `env_requirements`**——如 `AGENTTRUST_API_KEY`＝安裝精靈自此會逐項提示；**35/35 帶 `source_url`**）。

spawn 邊界：**僅**啟動發行者為 Anthropic 官方的套件（`@modelcontextprotocol/*`／`mcp-server-{git,time,fetch}`）。經掃描頁內過濾＋`search` 四組探測＋`modelcontextprotocol` 命名空間查證（count=0）確認**官方套件目前未發佈於 Registry** → spawn 腿誠實跳過；install→connect 機械與 npx/uvx 執行面已由 ADR-0186 E2E 以官方套件實證（同一代碼路徑），本批 F3 特有增量（registry 條目建構）即由上述 live 同步證實。任意社群套件的本機執行不在本批授權內。

## 測試

- `tests/unit/test_mcp_registry_entry.py` 新檔 +9（純映射：npx/uvx／required-env／remote-only／isLatest／混合套件挑選／packageArguments 有值附加＋必填無值拒絕／title 後備＋website 後備／畸形容錯）
- `tests/unit/test_mcp_hub_service.py`：smithery 測試改寫為 registry ×3（映射＋分頁 cursor 傳遞＋截斷有聲）＋sync 換腿 ×2＋sync registry 腿 pin +1＋response model pin +1

TDD：6 行為紅＋1 ImportError 先行 → 實作轉綠 → 四變異單點紅互證。

## 後果

- 社群貨架從「~100 條全死」→「誠實過濾後 N 條可裝」：面積縮小（registry 條目 stdio 佔比低，首頁樣本 7/50）換真實可用性＋真實 env 提示。
- Smithery 匿名 API 仍活但只剩目錄價值、無可靠啟動路徑——不再使用。技能市場的 smithery.ai URL 白名單（`skill_service`）是另一功能、不受影響。
- F4（enabled 半承接）／F5（種子單薄）不在本批；**F6＝本批調查新發現**：前端 `computeMcpStatus` 以 `hub.id` 比對 `server_config` 鍵，但安裝寫入鍵＝`derive_server_key(name)`（ADR-0102）→ Hub 卡片「已安裝／可更新」徽章恆判 `not_installed`——由維護者點名，僅記錄。

## 教訓

外部供應鏈條目的「安裝命令」必須綁**可實證的介面契約**：npm/pypi 套件名可向 registry 逐條查證（F1 的修法）；第三方 CLI 的子命令沒有任何 registry 可查、漂移無聲（F3 的死法）。

## 關聯

- 前作：ADR-0186（同一調查源 F1/F2；npx/uvx 執行面與哨兵機制＝本批直接複用）
- 前例：`mcp_category.py`／`mcp_official_map.py`（純函式 SSOT 體例）
- 台帳：memory `touka-mcp-hub-review`（F1–F6）
