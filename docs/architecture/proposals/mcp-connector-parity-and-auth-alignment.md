# MCP Connector 大廠對齊：貨架擴充＋授權分階（Connector Parity & Auth Ladder）

> **狀態**：**批次 1 🏁（[ADR-0191](../adr/0191-mcp-hub-tier-a-shelf-and-version-pinning.md)）／批次 2 🏁（[ADR-0192](../adr/0192-mcp-remote-streamable-http-transport.md)）／批次 3 🏁（[ADR-0194](../adr/0194-mcp-remote-shelf-and-manual-remote-entry.md)）全數落地**（2026-07-25～26）；**批次 4 經 2026-07-26 專案覆核後維持擱置，但擱置理由已整批換掉——詳 §8**。維護者已決定 §6 五個決策點（①核准 1→2→3、4 擱置；②新條目必釘版＋既有種子同批補釘；③Slack 社群版先不收；④瀏覽器自動化另案；⑤Tier A 首發不含 Perplexity）。
> **來源**：依維護者要求所做的三家大廠桌面版 connector 調查（Claude Code desktop／OpenAI Codex／Google Antigravity；2026-07-25 查證官方文件與 changelog）＋ MCP Hub F1–F6 修復線（[ADR-0186](../adr/0186-mcp-hub-official-launch-spec.md)～[ADR-0190](../adr/0190-mcp-hub-badge-server-key.md)）收官後的自然續章。
> **範圍**：①把大廠共同的 MCP 能力放進 Touka 貨架（有公開現成套件優先，無合適者才考慮自製）；②授權方式對齊大廠（remote transport／靜態 header／OAuth 分階；不為對齊而對齊）。
> **建議序**：批次 1（Shelf-A 策展）→ 批次 2（L1 remote＋header）→ 批次 3（Shelf-B 上架）→ **停下來看需求** →（僅在 §8-5 啟動條件成立時）批次 4（L2＋L3）。
>
> **後續遷移計畫（2026-07-30）**：本提案已落地批次 1～3 的歷史與 ADR 保持有效；尚未實作的 OAuth 批次 4、MCP `2026-07-28` 協定生命週期、自有 server／ToukaIDE 遷移及 Extensions，改由 [`mcp-2026-07-28-protocol-migration.md`](mcp-2026-07-28-protocol-migration.md) 統籌。未來工作若有衝突，以新計畫為準。

## 1. 背景

### 1.1 三家大廠調查結論（2026-07-25 快照）

| | Claude Code 桌面版 | Codex（ChatGPT desktop 內） | Antigravity |
|---|---|---|---|
| 目錄規模 | connectors 目錄 ~400+（verified/community 兩級）＋官方 plugin marketplace | 官方 plugins 90+＋ChatGPT connectors 生態（`codex_apps`） | MCP Store 42 個 supported servers |
| 傳輸 | stdio／streamable HTTP（SSE 已 deprecated） | stdio／streamable HTTP | stdio／SSE／streamable HTTP／WS |
| OAuth | 2.0＋PKCE 必帶、DCR、CIMD | 2.1＋PKCE、DCR、CIMD、`auth="chatgpt"` | 內建 OAuth＋DCR 零設定、ADC |
| secret 收集 | 目錄 Connect 流程＋header 設定 | 表單/env、`bearer_token_env_var` | Store 一鍵安裝表單（安全儲存） |
| 治理 | allowlist/denylist＋per-tool 政策 | `enabled_tools`/`disabled_tools`＋逐工具審批 | `mcp(server/tool)` 權限規則＋`disabledTools` |

三家已收斂到同一形態：**傳輸主線 streamable HTTP**（SSE 全面 deprecated、WS 邊緣）；**OAuth 走 2.1 式**（PKCE＋DCR，token 進 OS keyring／安全儲存）；**目錄一鍵安裝＋表單收 secret**（raw secret 不進聊天流）；**per-tool 治理**。

### 1.2 Touka 現況（本提案地基，2026-07-25 全部實查）

| 事實 | 出處 | 對工程量的意義 |
|---|---|---|
| MCP 連線只支援本地 stdio；`ProtocolServerConfig` 僅 `command/args/env/vault_jail_arg/enabled`，無 `url` | `src/protocols/mcp/client.py`、`src/protocols/registry.py` | 真正缺口＝remote transport，不是授權本身 |
| 鎖定依賴官方 `mcp ~=1.26.0` **已內建** `client/streamable_http.py` 與 `client/auth/oauth2.py`（OAuth 2.1＋PKCE＋DCR＋discovery＋refresh） | `uv.lock`、venv 實勘 | **協定機械免費、零新依賴**；工程量剩接線與 UX |
| 容器已裝 Node.js/npm | `ToukaProxy/Dockerfile` | npx 類套件可生成，貨架擴充無 runtime 缺口 |
| `install()` 已有 env_requirements 逐項表單 → 寫 `server_config.json` | `src/services/mcp_hub_service.py` | 「安裝時收 secret 表單」大廠三家都有，Touka 已具備（API key 類授權＝現成） |
| registry 映射**刻意**濾掉 remote-only 條目（誠實回 `None`） | `src/utils/mcp_registry_entry.py`（ADR-0187） | L1 落地後為現成擴充點，整排貨自動解鎖 |
| 治理面已齊：enabled 咽喉（ADR-0188）、vault jail、shell 注入防護、MCP 工具預設 `dangerous` 進 HITL（[ADR-0143](../adr/0143-hitl-unification.md)）、徽章鍵（ADR-0190） | 各 ADR | 新 connector 天生受管，零新治理基建 |

## 2. 目標

1. Touka 的授權方式對齊大廠，同時控制工程量。
2. 把大廠共同提供的 MCP 功能放進 Touka；有公開可用的現成套件就用現成的，沒有合適的再考慮自製。

## 3. 授權對齊拆解（L1–L4 四階，工程量遞增）

### L1｜remote transport＋靜態 header（PAT／API key／bearer）——中小，1 個 ADR 批次

- `ProtocolServerConfig` 增 remote 形狀（`url`／`headers`／transport 判別欄；與 stdio 欄位互斥驗證，Pydantic 層擋混填）。
- `connect_mcp_server` 分支走 SDK `streamablehttp_client`。**只做 streamable HTTP**：SSE 業界已 deprecated、WS 三家皆邊緣，皆不做。
- hub entry schema＋安裝表單支援 header 類需求（`env_requirements` 的 header 對等物；沿用既有表單 UX）。
- `mcp_registry_entry.py` 增 remote 映射分支（解除 remote-only 過濾；不可無人值守安裝者維持誠實 `None`）。
- enabled 咽喉對 remote 的語意＝斷線關 session（無進程可殺；ADR-0188 對等執法）。
- E2E 真連線實證（比照 F1/F2 體例）。

### L2｜OAuth 2.1（PKCE＋DCR）——中，1–2 個批次；**預設擱置**

> ⚠️ **OAuth 不是萬用的安全升級**（2026-07-26 Stripe 實測所得）：`access.stripe.com/mcp` 的 `scopes_supported` **只有 `["mcp"]`**——無逐資源 scope、無 read/write 分離，token 繼承授權者的 Dashboard 角色。對 Stripe 這種含通用寫入原語的服務，**OAuth 反而比 restricted key 更難限縮權限**（RAK 可逐資源設 None/Read/Write），且 Connect 情境官方明示不支援 OAuth。常被引用的官方句「OAuth is more secure than using your secret key」其比較對象是 `sk_` 而非 `rk_`。故批次 4 的價值須**逐服務評估**：對 OAuth-only 的服務（Linear／Figma 遠端）是唯一解，對 Stripe 則否——不能假設「上了 OAuth 就更安全」。

- SDK `OAuthClientProvider` 承擔**訊息層**全部協定（PKCE 產生、PRM/ASM discovery、DCR 與 CIMD 註冊、授權 URL 組裝、token 交換與 refresh 請求組裝、RFC 8707 resource、401/403 分支、Bearer 注入）；Touka 側工作＝`TokenStorage` 落地實作、`/v1/mcp/oauth/callback` 端點、前端 Connect 按鈕→開瀏覽器→回跳的流程編排、token 失效重授權 UX。
- ~~自架 web app 的回跳現實：redirect URI 須與使用者實際存取 ToukaChat 的 origin 一致~~ ❌ **這句是錯的（2026-07-26 覆核）**：OAuth 對 redirect_uri 與使用者瀏覽 origin **無同源要求**，規範只要求與**已註冊值**逐字精確比對。真正的硬限制是 MCP spec 2025-11-25 的 MUST：redirect URI 必須是 `localhost` 或 https（故明文 LAN IP 無法註冊）——比原句寫的「多數授權伺服器拒絕」更絕對，**但只約束「授權那一刻」，不影響日常 LAN 存取**。正確敘述與三條繞法見 §8-4。
- **SDK 沒做、Touka 必須自扛的部分**（2026-07-26 實勘，決定真實工程量）：跨重啟的 token 到期簿記與 refresh、失效與撤銷（`TokenStorage` Protocol 無 delete、client 端無 RFC 7009 撤銷呼叫）、授權流與開機迴圈的解耦、pending-flow 與 state 的關聯橋接、per-server provider 快取與併發、假授權伺服器測試替身。詳 §8-3。

### L3｜token 儲存——小，隨 L2

- Docker 內無 OS keyring。現實選項：data/ 下限權明文檔（與現行 `.env` 存供應商 API key 同級姿態＝不劣化），或以 `TOUKA_AUTH_SECRET` 衍生鍵加密落盤（鑰匙在隔壁、增益有限——採用與否誠實標註，不自欺）。

### L4｜結構性不可對齊——不做，寫進誠實邊界

- **平台級預註冊 client**（Claude 的 `oauth_anthropic_creds` 模式）：Google Workspace／Microsoft 365／Slack 官方 hosted connector 的 IdP 只認大廠平台的 redirect URI——自架 Touka 無中心化平台，永遠進不了這條門。唯一替代＝使用者自建 OAuth app 手填 clientId/secret（Google 自家 Antigravity Workspace Codelab 亦如此）。
- CIMD、enterprise managed auth（IdP assertion）：平台廠專用件，Touka 不需要。

## 4. 貨架擴充分檔（Tier A／B／C）

三家交集按「Touka 現況能不能直接吃」分檔。**套件名為候選**——批次 1 第一步即逐條現勘（套件名／env 名／維護狀態／license），以 spawn 實證定案配方（ADR-0189 體例），提案不預先宣稱為事實。

### Tier A｜現成 stdio 套件＋env/API key——零新基建，批次 1 直接上架

| MCP | 候選現成套件（runtime） | 授權 | 出現於 |
|---|---|---|---|
| Notion | `@notionhq/notion-mcp-server`（npx） | integration token（env） | 三家 |
| Atlassian（Jira/Confluence） | `mcp-atlassian`（uvx） | API token（env） | 三家 |
| Figma | `figma-developer-mcp`（npx，社群） | API key（env） | 三家 |
| Stripe | `@stripe/mcp`（npx） | secret key（env） | 三家 |
| Context7（框架文件） | `@upstash/context7-mcp`（npx） | 免授權（選配 key） | Codex＋Claude plugins |
| Sentry | 官方 npm stdio 版（現勘定名） | auth token（env） | Claude＋Codex |
| Perplexity 類搜尋 | 官方/社群 npx（現勘定名） | API key（env） | Antigravity（視需求收錄） |

誠實排除（不入批次 1）：
- **GitHub 官方本地版**＝Docker/Go 二進位，proxy 容器內 docker-in-docker 不可行 → 正解在 Tier B（官方 remote）。
- **Playwright／Chrome DevTools**＝需瀏覽器，slim 映像要多揹數百 MB → 另案評估（§6 決策點 4），不混進策展批。
- **Slack**＝官方 stdio 版已 archived，僅剩社群 bot-token 版、質量需審 → §6 決策點 3。

### Tier B｜需 L1 才能上——批次 3

- **GitHub 官方 remote**（`api.githubcopilot.com/mcp/`＋PAT header；GitHub 官方推薦路徑）。
- **Zapier**（per-user endpoint＋bearer；一條打通 8,000+ app 的最高槓桿件）。
- **Figma 官方 remote**（bearer）。
- registry 映射解除 remote-only 過濾後**自動流入**的官方 Registry remote 條目。

### Tier C｜需 L2 或結構性不可——批次 4 之後（預設擱置）

> 📌 **本節於 2026-07-26 逐服務實查後重寫**（原文三條斷言有兩條為誤）。逐條出處見 §8-2。

- ❌ ~~Linear 官方（OAuth-only）~~ → **實為 L1 現況可用**：官方文件明示可用 Linear API key 直接走 `Authorization: Bearer`（含 Read-only scope），互動式 OAuth 只是預設路徑。它不是 Tier C，是**尚未上架的 Tier B**。
- **批次 4 也救不了的（做完 L2 仍進不去）**：**Slack 官方 hosted**（明文不支援 DCR＋須預註冊 confidential app＋硬編 app ID＋須 directory-published 或 internal app）、**Figma 官方 remote**（拒絕 DCR＋客戶端白名單「只有 Figma MCP Catalog 內的客戶端能連」）、**Vercel**（客戶端白名單，明列 12 家核准客戶端）。這三條是**結構性排除，性質同 L4**，只是排除機制從「平台級預註冊」換成「客戶端白名單」。
- **做完 L2 還要再加手填欄位的**：**Asana**（OAuth-only 且明確不支援 DCR，須自建 app）——使用者要貼 client_id＋client_secret **兩個**秘密，比社群 stdio 版貼**一個** PAT 更麻煩。
- **hosted 版 OAuth-only，但官方 stdio＋token 版已在貨架＝零能力增益**：**Notion**（`curated-notion`）、**Sentry**（`curated-sentry`）。**Square／PayPal** 同型（官方 stdio＋env token，且 Square 有 `DISALLOW_WRITES` 唯讀開關）。
- ❌ ~~Google Workspace／Microsoft 365 hosted＝L4 結構性不可~~ → **分層錯誤**：不可達的只有 **hosted 形態**；可達形態是 **stdio 委派**（`workspace-mcp` 自己跑 Google OAuth、`ms-365-mcp-server` 走裝置碼流程且有 `--read-only`）。這兩家**確實沒有長效靜態憑證**（Google API key 只達 public data、service account 需 Workspace 超管做 domain-wide delegation；Graph access token 僅 60–90 分鐘），故它們**不是**「Tier A 已有 token 版替代」——正確的說法是「有 stdio 委派替代」。
- **唯一真解鎖**：**Canva**（無任何 token 替代，且仍接受 DCR）。但 Canva 自己已把 DCR 標為 deprecated、改推 CIMD。
- 結論：14 條逐一結算後，批次 4 的**淨解鎖＝1 條**。這是批次 4 維持擱置的依據之一（完整依據見 §8）。

## 5. 批次計畫

- **批次 1｜Shelf-A 策展 🏁 已落地（ADR-0191）**：Tier A 六條現勘＋對抗式覆核 → **四條上架**（Notion／Atlassian／Figma／Context7，全數 E2E CONNECTED），**兩條 hold**（Stripe＝實為 stdio↔HTTP 橋接器、本質屬 remote 應歸批次 2/3；Sentry＝FSL-1.1 非 OSI 開源＝待維護者決定收錄準則）。釘版政策三來源全覆蓋（官方配方／種子／registry 動態條目）＋釘版棘輪測試。**額外修掉兩個現勘揭露的既有缺陷**：MCP 初次連線無逾時（LIVE：一個格式合法的錯金鑰即可讓 ToukaProxy 永久開不了機）與前端 IconMap 無聲失效（既有四條種子圖示全數失效無人察覺）。
- **批次 2｜L1 remote＋靜態 header 🏁 已落地（ADR-0192）**：`ProtocolServerConfig`／`MCPServerItem` 雙形狀互斥＋`streamablehttp_client` 分支＋URL 安全閘（明文 http 僅限 loopback）＋**憑證留 env、header 只寫 `${VAR}`**（零成本沿用既有遮罩保護）＋registry remote 解鎖（local-first 優先序）＋前端 remote 風險文案分寫。E2E 真連線：MS Learn（stateful）／DeepWiki（stateless）各 3 工具＋真 `call_tool`。**順帶修掉 ADR-0190 的殘留**：前端安裝寫 `hub.id`、徽章查 `server_key`＝徽章永不亮且更新時既有密鑰遺失（抽 `hubEntryConfigKey`／`buildInstalledEntry` 純函式為 SSOT）。Sentry 經維護者決定上架＝出廠貨架 9 條。
- **批次 3｜Shelf-B 上架 🏁 已落地（ADR-0194）**：**GitHub（`/readonly` 端點）／Zapier／Stripe／MS Learn／DeepWiki 五條上架**（貨架 14 條）；**Figma 兩形態皆 hold**（遠端 OAuth-only＝L4 結構性；本機 Dev Mode 形狀完美但容器網路打不到＝待維護者決定）。前端補齊 remote 形狀（列表顯示端點／手動新增與編輯支援 url+headers＝per-user URL 型服務的唯一出口）＋技能接線健檢納入 remote。**另修一個 LIVE 缺陷**：遠端握手失敗的 `CancelledError` 繼承 `BaseException`、穿透三層 `except Exception`，單一台憑證錯誤的遠端伺服器即可中斷整個開機迴圈。
- **批次 4｜L2＋L3 OAuth（維持擱置，需維護者明確啟動）**：§3-L2/L3 全項＋Tier C 可行子集上架。**2026-07-26 專案覆核後，擱置理由整批換掉、啟動前置條件改寫**——原條件「出現具體想接的 OAuth-only 服務」**已作廢**（該條件其實現在就滿足，但滿足它不代表該動工）。新條件見 §8-5；真實工程量由原估「中，1–2 批」上修為「共用語意 2–3 批／per-user 語意 4–6 批」，見 §8-3。

每批綠燈（pytest＋vitest＋guard_all）後由維護者 commit；每批各取一個 ADR 號。全案（批次 1–3）估計與 F1–F6 一線（5 ADR）同量級或更小。

## 6. 決策點（維護者已於 2026-07-25 全數決定）

1. **批次順序**：✅ 核准 1→2→3、批次 4 擱置。
2. **條目釘版**：✅ 新條目必釘、既有種子同批補釘。落地時再延伸涵蓋 registry 動態條目（見 ADR-0191）。
3. **Slack 社群版**：✅ 先不收，列觀察名單。
4. **瀏覽器自動化（Playwright 類）**：✅ 另案。
5. **Tier A 首發清單**：✅ 不含 Perplexity。實際上架四條（Stripe／Sentry 因落地現勘結果轉 hold，理由見 ADR-0191）。

### 待維護者後續決定

- ~~**Sentry 收錄與否**~~ ✅ 2026-07-25 決定收錄（ADR-0192；具名例外，非準則放寬——後續非 OSI 條目仍逐案決定）。
- ~~**Stripe 重評**~~ ✅ 批次 3 已上架（最保守配方）。**但這是唯一無法出廠鎖唯讀的條目**，安全邊界 100% 落在使用者的受限金鑰權限——若認為金流風險不該由文案承擔，撤下即可（使用者仍可手動新增）。
- ~~**MS Learn／DeepWiki**~~ ✅ 2026-07-26 維護者決定上架。
- **Figma 本機 Dev Mode 版＝唯一待決策項**：端點 `http://127.0.0.1:3845/mcp`、免認證、streamable HTTP，Touka 的 URL 閘對 loopback 明文 http 本就豁免——但 `touka-proxy` 在 bridge 網路容器內，`127.0.0.1` 指向容器自己；`host.docker.internal` 又被同一安全閘擋下。二選一：(a) 原生部署即可用、零改動；(b) 為此放寬 URL 閘＋compose 補 `extra_hosts`——該端點**不帶任何憑證**，明文外傳無金鑰可洩，風險論證與一般 remote 條目本質不同，值得單獨評估。
- **per-tool 治理**（Codex `enabled_tools`／Antigravity `disabledTools` 對等物）：Stripe 上架後這個缺口從「日後候選」升級為**有具體受害者**——含通用寫入原語的服務無法在 Touka 側收斂工具面。

## 7. 誠實邊界／範圍外

- **L4 結構性限制**（§3-L4）：平台級 hosted connector 自架學不來，這不是工程債、是結構差異。
- ~~**LAN redirect 限制**（L2 生效後）：OAuth connect 實務上限 localhost 存取或需自架 https 反代~~ → **2026-07-26 覆核後自「誠實邊界」移出**，降級為文件說明事項：限制只作用在「授權那一刻」，且有三條零成本繞法。正確敘述見 §8-4。
- **OAuth E2E 無法進 CI**：live 授權不可自動化——mock auth server 單元/整合測試＋人工實機實測（比照互審有效性五臂的「未實測不宣稱」紀律）。
- **secret 明文姿態**：`server_config.json` 內 env/header 值為明文（與 `.env` 同級；單使用者信任邊界內）。多使用者化時需 per-user token store——範圍外，屆時另案。
- **上游漂移維護**：候選套件的 env 名／參數形狀可能隨上游改版漂移；配方 parity 測試（ADR-0189 體例）是防線，但無法防上游 breaking release——釘版（§6-2）同時緩解此項與供應鏈風險。
- **per-tool 治理**（Codex `enabled_tools`／Antigravity `disabledTools` 對等物）：業界共同點之一，但 Touka 現行 per-server enabled＋HITL 逐呼叫審批已覆蓋主要風險面——不入本案，列日後候選。
- **調查時效**：§1.1 為 2026-07-25 快照；大廠月更，批次 3/4 動工前建議重驗關鍵事實（GitHub remote endpoint、Zapier 接入形狀）。
- **自製 MCP**：目標二的「沒有合適的再自製」在本案零觸發——三家交集全部有現成公開選項（含替代品）；若日後出現缺口，走既有創建引擎（ADR-0069）而非本案擴充。

## 8. 批次 4 專案覆核（2026-07-26）——維持擱置，理由整批換掉

> 觸發：批次 3 收官後，維護者要求評估是否執行批次 4，以及做與不做的優缺點。方法＝從工程量實勘、逐服務價值查證、阻礙面現實查核、機會成本四個面向調查，再對承重結論做對抗式覆核，關鍵斷言另以直接讀原始碼覆核。**本次全程唯讀，未取得或使用任何憑證，未跑任何真連線。**

### 8-1. 裁決

**維持擱置。但原擱置理由（§4 舊文「Tier C 多數服務在 Tier A 已有 token 版替代」）已被推翻**——該句把「token」誤當成「長效靜態憑證」，而 Touka 的 L1 **結構上吃不下任何會過期的憑證**：[`client.py`](../../../ToukaProxy/src/protocols/mcp/client.py) 的 `streamablehttp_client(...)` 呼叫**未傳 `auth=`**（SDK 有此參數）、`resolve_headers` 只在 connect 當下代換一次、`manager.call_tool_on_server` 無任何 401 重授權路徑。故對 Google Workspace／M365（唯一憑證即短效）而言，「有 token 替代」不成立。

**新的擱置理由**：OAuth-only 的遠端伺服器，Touka **今天就能以 stdio 委派接上**——`npx -y mcp-remote <url>`（通用橋接器，自帶 DCR/PKCE/token 儲存/refresh）、`workspace-mcp`（stdio 模式自行完成 Google OAuth）、`ms-365-mcp-server`（**裝置碼式流程，不需回跳端點**，且有 `--read-only`）。批次 4 等於自己重寫一支已存在、可釘版的橋接器。

⚠️ **此理由有一個未實測的承重點**：容器內完成 `mcp-remote` 回跳、容器內完成 `workspace-mcp` 首次 Google 授權，兩項皆為文件推論。動工前應先實測其一（M365 裝置碼路線不受此影響）。

### 8-2. 逐服務結算（14 條實查，2026-07-26）

| 分類 | 服務 | 批次 4 的作用 |
|---|---|---|
| L1 現況即可用（僅未上架） | Linear、Cloudflare（17 端點）、Intercom、Atlassian 官方 Rovo | 零——靜態 Bearer/Basic 直接可行 |
| 官方 stdio＋token 已在貨架 | Notion、Sentry、GitHub | 零——重複路徑 |
| 官方 stdio＋token 可上架 | Square（有 `DISALLOW_WRITES`）、PayPal（⚠️ 金流寫入，比照 Stripe） | 零 |
| **做完仍進不去**（白名單／無 DCR） | Slack 官方、Figma 官方 remote、Vercel | **負**——投入後仍被擋 |
| 做完還要再加手填欄位 | Asana | 負——貼兩個秘密 vs 貼一個 PAT |
| stdio 委派可達（無靜態憑證） | Google Workspace、Microsoft 365 | 低——換 UX，不換能力 |
| **唯一真解鎖** | **Canva** | **正**——無 token 替代且仍接受 DCR |

**淨解鎖 1 條。** 另一項趨勢事實：大廠正在**離開自助註冊**——往客戶端白名單（Figma、Vercel）、預註冊 confidential client（Slack、Asana）或 CIMD（Canva）走。自架小平台正是這個趨勢的受害者。

（勘誤：覆核過程中一度出現「釘版 SDK 無 CIMD」的說法，經直接 grep 原始碼推翻——`mcp 1.26` **有** CIMD，見 `mcp/client/auth/oauth2.py` 的 `client_metadata_url` 參數與 `mcp/client/auth/utils.py` 的 `is_valid_client_metadata_url`／`should_use_client_metadata_url`。但 CIMD 要求 client_id 等於一個公開可達的 HTTPS metadata 文件 URL，對躲在家用 NAT 後的自架單機是額外的結構性課題。）

### 8-3. 工程量上修：三個由 SDK 形狀製造的坑

原估「中，1–2 個批次」**低估**。修正後：**共用語意 2–3 批；per-(server,user) 語意 4–6 批**。三個承重坑（皆經直接讀原始碼覆核）：

1. **開機必然死鎖（不是機率問題）**。`await protocol_manager.initialize_all()` 在 lifespan 內（`main.py`），而 uvicorn 是 `await self.lifespan.startup()` **跑完才建 listener**（`uvicorn/server.py`）。SDK 的授權流在第一個 POST 收到 401 時就地展開，而那個 POST 正是 `session.initialize()`——**回跳端點物理上不存在**，100% 走到逾時，再乘上 `connect_server` 的 `@with_retry(max_retries=2)`。這是 [ADR-0191](../adr/0191-mcp-hub-tier-a-shelf-and-version-pinning.md)、[ADR-0194](../adr/0194-mcp-remote-shelf-and-manual-remote-entry.md) 修過兩次的同一家族第三次。另：Touka 的 `MCP_INIT_TIMEOUT_SECONDS=60` 會把人類授權視窗壓到 60 秒，而放寬它就是重演 ADR-0191 的「一台掛住＝全域開機拒絕服務」。
2. **跨重啟 refresh 是死的**。`OAuthToken` 只有相對的 `expires_in`；絕對時間 `token_expiry_time` 僅在「剛換到 token」時算出，而 `_initialize()` 從 storage 還原時**只還原 `current_tokens` 與 `client_info`、不還原 `token_expiry_time`**；`is_token_valid()` 的判斷是 `not self.token_expiry_time or ...` → 還原後恆為 True → **refresh 分支永不進入** → 直接送出已死的 Bearer → 401 → 走完整互動式重新授權。且該整段路徑在上游標 `# pragma: no cover`。⇒「SDK 自動 refresh 免費」只在**單一 provider 實例的生命週期內**成立；跨重啟要讓 refresh 生效，必須自建到期簿記並戳 `provider.context.update_token_expiry(...)`＝依賴上游無測試覆蓋的私有狀態。
3. **state 只能靠反解授權 URL**。`state = secrets.token_urlsafe(32)` 是 `_perform_authorization_code_grant` 的區域變數、只在同函式內以 `compare_digest` 比對；`callback_handler` 型別為 `Callable[[], Awaitable[tuple[str, str | None]]]`＝**不帶參數**。唯一不改 SDK 的關聯手段是從 `redirect_handler` 收到的完整授權 URL 反解 state——把安全關鍵的關聯鍵建立在解析 SDK 內部組出的字串上，上游改組法即靜默斷裂，且斷掉的表徵是「按 Connect 永遠轉圈」。

其餘自建項：失效與撤銷 100%（`TokenStorage` Protocol 無 delete、`clear_tokens()` 只清記憶體不寫回、client 端全樹無 RFC 7009 撤銷呼叫）；假授權伺服器測試替身（ToukaProxy 無 respx／pytest-httpx，「零新依賴」在測試面已破）；新洩漏面（`manager._sanitize_for` 的精準遮罩比對的是該伺服器 `cfg.env` 的**實值**，OAuth token 依設計不在 env，只剩通用 `_SECRET_RE` 兜底，對含點號的 JWT 只命中片段——而錯誤訊息會經 `/mcp/list` 回前端）。

**最尖銳的一項不是工程量、是安全姿態**：`_active_sessions` 以伺服器名稱單鍵、`call_tool_on_server(server_name, tool_name, arguments)` 不帶身分。把綁人的 OAuth token 塞進這個結構＝**管理員授權自己的 Gmail，等於全系統所有靈魂共用他的信箱**。現行共用 PAT 觸及面相同、故非劣化，但那正代表**花 L2+L3 的錢買不到 OAuth 的賣點**。

### 8-4. redirect URI：正確敘述（取代 §3-L2 舊句與 §7 舊項）

- **OAuth 對 redirect_uri 與使用者瀏覽 origin 無同源要求。** 規範只要求與已註冊值逐字精確比對。
- 真正的硬限制：MCP spec 2025-11-25 明訂 redirect URI **MUST** 為 `localhost` 或 https ⇒ 明文 LAN IP 無法註冊。這比舊句寫的「多數授權伺服器拒絕」**更絕對**（是 MUST，不是市場慣例）。
- **但它只作用在「授權那一刻」**：MCP session 以伺服器名單鍵全域共用、token 取得後全系統共用，故只需授權一次。三條繞法：**(a)** 在 Docker 宿主機瀏覽器完成授權（`http://localhost:<port>` 真的打得到，Touka 側零工程）；**(b)** `ssh -L <port>:localhost:<port> <host>`（零工程，業界標準）；**(c)** 註冊 `http://localhost:.../v1/mcp/oauth/callback`，使用者從網址列**複製整條回跳 URL 貼回 Touka**——`callback_handler` 由 Touka 自行提供、SDK 只 await 它，故授權碼不必經 HTTP 回跳送達；且 PKCE 的 `code_verifier` 從不離開 proxy，落到錯誤本機監聽者的 code 無法兌換。需全自動回跳時，Tailscale Serve／Caddy／Cloudflare Tunnel 提供有效憑證與穩定 https 主機名，Touka 側成本僅為「一個可設定的對外基底 URL」。
- **RFC 8628 裝置授權碼流程不是可用退路**（誠實封死）：釘版 SDK 全樹零實作、MCP spec 2025-11-25 授權章未收錄、官方 ext-auth 無此擴充，且實查 Linear／Notion 的 AS metadata 皆無 `device_authorization_endpoint`。
- **DCR 註冊多組 redirect_uri 解不開此限制**：SDK 恆送 `redirect_uris[0]`，且可註冊成員本就限 localhost/https。

### 8-5. 新啟動條件（取代 §5 舊條件「出現具體想接的 OAuth-only 服務」）

滿足**任一**即重啟批次 4：

- **(A)** 維護者明確要接某服務，且該服務同時滿足：無第一方長效 token、**且**以 `npx -y mcp-remote <url>` 作為 stdio 條目**實測失敗**（不是「沒試過」）。先試橋接，失敗才動工。
- **(B)** 出現「兩個以上人類各自綁自己帳號」的實際需求——這同時觸發 per-(server,user) session 改鍵，**應獨立成批**而非塞進 L2/L3。
- **(C)** 某已上架條目因憑證短效變成殭屍伺服器（連得上、每次呼叫 401），且無 stdio 委派版可用。可觀測：`get_active_server_info` 的 `last_error` 出現持續 401。
- **(D)** 上游 SDK 修好 `_initialize()` 不還原 `token_expiry_time` 這個缺陷（一行 grep 可驗），使跨重啟 refresh 真正免費。

**不應**再把「DCR vs CIMD 誰勝出」當啟動條件——依 stdio 委派路線，client 註冊機制的選擇本來就被委派出去了。

### 8-6. 比批次 4 更該先做的：per-tool 治理

§6 已把它從「日後候選」升級為「有具體受害者」。本次覆核把缺口坐實（直接 grep 原始碼確認）：

- `permission_service.get_allowed_tools` 對 Owner **直接短路回 `["*"]`**；
- `is_mcp_server_allowed` 只有 **per-server** 粒度，無工具面；
- **唯一會查 `denied_tools` 的 `permission_service.is_authorized` 生產零呼叫端**（全 repo grep 命中的 `is_authorized` 皆為其他模組的同名區域變數）。

⇒ **Owner ＋ 任一已安裝 MCP 伺服器 ＝ 該伺服器全部工具，沒有任何收斂旋鈕**；而出廠貨架已含 `curated-stripe`（含 `create_refund`）。

三個理由讓它勝過批次 4：①有已出貨的受害者；②形狀已存在（`mcp_vault_jail.apply` 就是「per-server 設定欄位 → 執行層依身分執法」的完整先例，廣告面單一咽喉在 `middleware/layers/tool.py`）；③**全程可進 CI**——這正是批次 4 依 §7 永遠做不到的事。

動工前須由維護者決定一個設計爭點（不是工程量問題）：**軸向**。大廠是 per-installation（`enabled_tools`／`disabledTools` 寫在伺服器條目），Touka 既有 RBAC 是 per-role。

**次選（更小一口）**：`initialize_all` 並行化——仍是序列 await，而貨架已 9→14 且新增全為遠端條目。**明確不建議現在碰**：Guard #23（brace-expansion）13 條全在 dev 鏈，上游各維護線至今無 backport。

### 8-7. 本次覆核的未證實清單

- **未實測**：容器內跑 `mcp-remote` 完成一次 OAuth 回跳（需發布 3334 埠或 `--host`）；容器內跑 `workspace-mcp` stdio 完成 Google 首次授權。**此二項是 §8-1 新擱置理由的承重點。**
- **未實測**：Canva 是否有可用的 stdio 委派路徑（只查到「無第一方 token」）。
- **未實測**：`mcp 1.26` 的 `OAuthClientProvider` 與任何真實授權伺服器的端到端握手。
- **未實測**：§8-3 第 2 點的 refresh 失效推論鏈——程式碼四處皆經親讀確認，但未實跑驗證。
- **查證範圍**：僅 §8-2 表列 14 條。若真實需求落在清單外（Salesforce／HubSpot 等），結論需重算。
