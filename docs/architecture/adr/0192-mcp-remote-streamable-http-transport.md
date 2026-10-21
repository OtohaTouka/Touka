# ADR-0192: MCP remote 傳輸（streamable HTTP＋靜態 header）＋Sentry 上架（大廠 connector 對齊批次 2）

## 狀態

✅ 已接受（[MCP connector 大廠對齊提案](../proposals/mcp-connector-parity-and-auth-alignment.md) 批次 2 / L1）

## 日期

2026-07-25

## 背景

三家桌面版調查（ADR-0191 §背景）顯示 remote MCP 已是主流形態，而 Touka 的 MCP 連線**只支援本地 stdio**——`ProtocolServerConfig` 僅有 `command/args/env`，`connect_mcp_server` 只走 `stdio_client`。這道缺口的實際代價在批次 1 就現形：Stripe 的官方套件其實是 stdio↔HTTP 橋接器，本質屬 remote，只好 hold。同時 `registry_item_to_hub_entry` 為此刻意濾掉所有 remote-only 條目（ADR-0187），等於把官方 Registry 上 437 個遠端端點全擋在貨架外。

批次 2 即提案的 **L1（remote transport＋靜態 header）**，是四階授權階梯裡投報比最高的一階：解鎖 vendor-hosted 貨架，且**零新依賴**——鎖定的 `mcp ~=1.26.0` 已內建 `client/streamable_http.py`。

owner 另拍板：**Sentry 收錄**（批次 1 因 FSL-1.1 非 OSI 授權而 hold 的政策題，已裁示收）。

## 決策與實作

### 1. 憑證留在 env，header 只寫 `${VAR}` 引用（本批的核心設計）

remote MCP 的憑證走 HTTP header。最直觀的做法是把 token 直接存進 `headers`，但那會**繞過既有的密鑰保護**——`routers/mcp.py` 的 `_preserve_masked_env` 只認得 `env`（遮罩回填＋防止前端存檔時清掉磁碟既值）。

因此採「模板＋引用」形狀：

```json
{
  "url": "https://api.example.com/mcp",
  "headers": {"Authorization": "Bearer ${API_PAT}"},
  "env": {"API_PAT": "<secret>"}
}
```

`headers` 本身非機密（只是模板），憑證實值仍在 `env`，於是**安裝精靈的 `env_requirements` 表單、遮罩回填、防清除保護全部自動沿用，零額外密鑰程式碼**。這也與 Codex 的 `bearer_token_env_var` 同一思路，但泛化到任意 header。

解析 SSOT＝新增的 `utils/mcp_remote.py`（純函式，體例同 `mcp_category`／`mcp_official_map`）：

- `resolve_headers(headers, env)`：`${VAR}` 代換；**缺變數即 fail-closed 拋錯**。刻意不放行帶著 `${VAR}` 字面值的 header——那只會換來一台「連得上、但每次工具呼叫都 401」的殭屍伺服器（ADR-0191 的 Notion fail-open 教訓）。
- `validate_remote_url(url)`：只認 http/https，且**明文 http 僅限 loopback**。header 帶著使用者的 PAT，允許 `http://` 連外部主機等於把金鑰明文過網。此閘同時掛在 `ProtocolServerConfig` 與 wire 層 `MCPServerItem`，壞條目不會活到連線階段（禁令 17：從磁碟讀取的設定必須經 `model_validate`）。

### 2. 雙形狀互斥契約

`ProtocolServerConfig` 與 `MCPServerItem` 同步支援兩種形狀，`command` 與 `url` **恰須其一**——同時給或都不給一律整筆拒絕，不靜默挑一個。`enabled` 咽喉（ADR-0188）對 remote 一視同仁：沒有子進程可殺，排除即代表不建立連線。

`connect_mcp_server` 依 `is_remote` 分流；stdio 路徑抽成 `_open_stdio_transport` 保持原狀。注意 `streamablehttp_client` 回傳 **3-tuple**（多一個 session-id callback），stdio 為 2-tuple。兩條路徑共用 ADR-0191 的握手逾時。

**SSE 刻意不支援**：三家桌面版均已標為 deprecated，主線是 streamable HTTP。

### 3. registry remote 條目解鎖

`registry_item_to_hub_entry` 增 remote 分支，並確立 **local-first** 優先序：同時具備本地套件與遠端端點時取本地（子進程可控、無外呼依賴）。remote 分支只收 `streamable-http` 且通過 URL 安全閘者；必填 header 轉成 `${VAR}` 模板＋對應 `env_requirements`（`Authorization` → `MCP_AUTHORIZATION`）。

Registry 的 `remotes[].headers[]` 與 `environmentVariables` 結構對稱（`name`／`isRequired`／`isSecret`／`description`）。實查 5 頁掃描窗口內有 437 個 remotes、300 個帶 header 描述。這些描述被**併入 hub entry 的 description 尾部**——安裝表單只顯示變數名，使用者無從得知「`Authorization` 要填 `Bearer xxx`」這類格式要求，不揭露就是必然安裝失敗。此為零 schema 成本的折衷（結構化的 `env_descriptions` 欄位留待批次 3）。

釘版棘輪對 remote 條目豁免：版本由服務端掌握，無從釘起。

### 4. 前端：remote 的風險文案與 stdio 不同

`McpInstallModal` 原本無條件顯示 `{command} {args}` 與「**會在伺服器上執行的外部指令程序**」警示。remote 條目走這條路會是雙重錯誤：畫面渲染出 `undefined`，而且**文案在說謊**——remote 不在本機起任何進程，真正的風險是「把你填入的憑證送到外部端點」。故兩種形狀分寫警示與預覽（新增 `security_warning_remote`／`endpoint_review` 兩組 i18n 鍵，zh-TW／en 同步）。

`computeMcpStatus` 增 URL 比對：remote 無版本號，**端點就是配方**，策展換端點＝可更新。

### 5. Sentry 上架（批次 1 的 hold 解除）

owner 裁示收錄非 OSI 授權條目。配方沿用批次 1 現勘結果：`npx -y @sentry/mcp-server@0.37.0`＋`SENTRY_ACCESS_TOKEN`（E2E 實證 9 工具）。例外的代價是**揭露義務**：description 明載「授權為 FSL-1.1，非 OSI 開源」，並由測試釘死該揭露不被日後文案整理抹掉。出廠貨架自此 9 條。

## 測試

- `tests/unit/test_mcp_remote.py`（新，14）：形狀判別／URL 安全閘（https、loopback 例外、非 http scheme）／header 插值（多重引用、缺變數 fail-closed、空值直通）
- `tests/unit/test_mcp_remote_config.py`（新，8）：stdio/remote 雙形狀、互斥拒絕、URL 閘於 parse 時執法、`enabled` 對 remote 生效、**install 落盤形狀**（憑證只進 env、headers 僅模板、落盤結果可被 runtime 模型接受）
- `tests/unit/test_mcp_registry_entry.py` +4（remote 映射／local-first 優先序／SSE 不收／明文 http 不收）
- `tests/unit/test_mcp_hub_seed_integrity.py` +2（Sentry 存在性／非 OSI 授權揭露）；釘版棘輪對 remote 豁免
- `ToukaChat`：`mcp_version.test.ts` +3（remote 端點比對三態）、`McpInstallModal.test.tsx` +2（顯示 URL 不顯示 `undefined`／風險文案講憑證外送）

**契約變更**：`test_registry_servers_use_functional_category` 原斷言「remote-only 略過」＝stdio-only 年代的契約，本批刻意翻轉並更新該測試。

### E2E 實跑（真實遠端伺服器，走生產 `connect_mcp_server`）

| 標的 | 結果 |
|---|---|
| MS Learn（`learn.microsoft.com/api/mcp`，**stateful**、微軟官方） | CONNECTED 3 工具＋`microsoft_docs_search` 真呼叫 `isError=false` |
| DeepWiki（`mcp.deepwiki.com/mcp`，**stateless**、Cognition 官方） | CONNECTED 3 工具＋`read_wiki_structure` 真呼叫 `isError=false` |
| MS Learn＋`${VAR}` header 模板 | header 解析後連線成功 |
| 反例：明文 http 外部端點 | parse 期即拒（安全閘生效） |
| 反例：header 引用未設定的 env | fail-closed，未送出半成品憑證 |

兩種 session 形態刻意都測：**stateless 伺服器真實存在**（`get_session_id()` 回 `None`），Touka 的 remote 路徑只取 3-tuple 的前兩項、不依賴 session id，故不受影響。

## 後果

- Touka 的 MCP 貨架自此涵蓋 vendor-hosted 端點；官方 Registry 的 remote 條目不再被整批擋掉。
- 批次 3（Shelf-B：GitHub 官方 remote／Zapier／Figma remote）與 Stripe 重評的前置條件備齊。
- remote 條目的密鑰保護與 stdio 完全同源，無第二套機制要維護。

## 誠實邊界

- **E2E 只驗了免認證端點**：真實連線已實證（見上表），但帶**真憑證**的 remote（GitHub PAT 等）未實跑——無可用 token，且不應為測試索取。header 模板路徑已以自訂 header 驗過解析與送出，憑證正確性的端到端驗證留待批次 3 上架 GitHub remote 時由 owner 實機確認。
- **無 OAuth**：僅支援靜態 header（PAT／API key）。OAuth 2.1（PKCE＋DCR）為批次 4，owner 已裁示擱置；`mcp` SDK 已內建 `client/auth/oauth2.py`，屆時仍是零新依賴。查證附帶取得 GitHub 的權威形狀供批次 4 參考：authorization server 為 `github.com/login/oauth`，且其 protected-resource metadata **只在路徑後綴式位址**（`/.well-known/oauth-protected-resource/mcp`）有效、根路徑回 404——實作須走 RFC 9728 的後綴發現或直接採用 401 標頭的 `resource_metadata`。
- **registry 的 `headers` 欄位不可當免認證判準**：實測發現 HuggingFace 宣告了 `Authorization` 卻能匿名連上（工具減量），反向也存在宣告空 header 實際走 OAuth 者。故 hub 上的 remote 條目「要不要填憑證」只能靠 description 提示與使用者實試，貨架不宣稱其認證需求為權威。
- **SDK 關閉期噪音**：部分伺服器在收線時會讓 SDK 印出 `Session termination failed: 404`（非致命，連線與工具呼叫皆已成功）。若日後把關閉期日誌當錯誤處理會產生假陽性。
- **header 格式揭露只到 description**：結構化的 per-variable 說明（`env_descriptions`）留待批次 3；目前使用者靠 description 尾部的認證提示。
- **URL 閘不防 SSRF**：`validate_remote_url` 的職責是「憑證不得明文外傳」，不是限制可連的主機。remote 端點由使用者/策展人指定，與 MCP 的本質一致。
- **Sentry 授權**：FSL-1.1-ALv2 屬 source-available 而非 OSI 開源，收錄係 owner 具名例外，非準則放寬——後續非 OSI 條目仍須逐案拍板。

## 關聯

- 提案：[mcp-connector-parity-and-auth-alignment.md](../proposals/mcp-connector-parity-and-auth-alignment.md)（批次 3＝Shelf-B 上架＋remote E2E；批次 4＝OAuth，擱置）
- 前作：ADR-0187（registry 映射 SSOT，本批解除其 remote-only 過濾）／0188（enabled 咽喉）／0190（徽章比對鍵，本批增 URL 維度）／0191（釘版、握手逾時、Tier A 貨架）
- 政策：禁令 17（設定須經 Pydantic 驗證）／禁令 54（誠實標示）
