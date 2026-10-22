# ADR-0191: MCP Hub Tier A 貨架擴充＋條目釘版＋初次連線逾時（大廠 connector 對齊批次 1）

> C2-3 現況補註：restart 增加最多 5 秒取鎖，關舊連線最多 8 秒、握手預算 15 秒、逾時後收尾另最多 8 秒；後面還有健康資訊查詢，約 22 秒不再是目前端點的最壞時間。 詳見 ADR-0275。

## 狀態

✅ 已接受（[MCP connector 大廠對齊提案](../proposals/mcp-connector-parity-and-auth-alignment.md) 批次 1；維護者決定 §6 五個決策點）（2026-10-16 補註：[ADR-0273](0273-mcp-connection-owner-task.md) 起連線由 owner task 持有，restart 的 15 秒逾時從此安全，最壞約 22 秒）

## 日期

2026-07-25

## 背景

維護者要求調查 Claude Code 桌面版／OpenAI Codex／Google Antigravity 三家的 connector 支援，據以評估「授權對齊」與「貨架補齊」兩件事。調查（2026-07-25，官方文件與 changelog 實查）顯示三家已收斂到同一形態：streamable HTTP 為傳輸主線、OAuth 走 2.1 式（PKCE＋DCR）、目錄一鍵安裝＋表單收 secret、per-tool 治理。

據此立提案，把工作切成四階授權（L1 remote＋靜態 header → L2 OAuth → L3 token 儲存 → L4 平台級 hosted＝結構性不可對齊）與三檔貨架（Tier A 現成 stdio＋API key／Tier B 需 remote／Tier C 需 OAuth）。維護者決定做批次 1→3、批次 4 擱置。**本 ADR 為批次 1（Tier A 策展），零新基建。**

owner 五決策點裁示：①核准 1→2→3、4 擱置；②新條目必釘版、既有種子同批補釘；③Slack 社群版先不收（列觀察名單）；④瀏覽器自動化（Playwright 類）另案；⑤Tier A 首發不含 Perplexity。

## 決策與實作

### 1. 釘版政策（禁令 55 精神延伸至 hub 的 npm/pypi 條目）

未釘版＝安裝當下抓上游最新版，等同把供應鏈信任交給「未來某個尚未存在的版本」。本批把釘版**完整覆蓋三個來源**，無例外：

| 來源 | 作法 |
|---|---|
| 官方配方 SSOT（`OFFICIAL_LAUNCH_SPECS`） | 7 條全釘（npm `pkg@x.y.z`／PyPI `pkg==x.y.z`）；版本經 registry 實查 |
| 種子 `mcp_hub.json.example` | curated＋official 全釘；種子＝配方快照，parity 測試續守 |
| registry 動態條目（`registry_item_to_hub_entry`） | 取條目自帶的 `packages[].version` 釘死；**無版本＝不上架**（fail-closed） |

- `uvx pkg==ver` 語法經本機實測可用（uv 0.10.5）。
- **fallback 條目刻意不釘版**：未知官方目錄的 npx 慣例 fallback 版本未知，無從釘起，維持浮動＋`known=False` WARNING；種子只收 `known=True`，故棘輪不受影響。
- registry 的 fail-closed 有實測支撐：對前 5 頁（＝生產掃描上限）逐條統計，**405 個 stdio×npm/pypi 套件全數帶 version**，故此政策零貨架損失。
- 新增**釘版棘輪測試**：種子每條的套件名位都須帶版本，且版本段須數字起頭——`@latest`／`@next` 這類「看似釘版、實則浮動」的偽釘一併擋掉。

**刻意的副作用**：釘版讓前端 `computeMcpStatus` 的「可更新」徽章第一次有真實意義（策展人升版本＝使用者看得到可更新）。釘版前該偵測對「套件有新版本」恆無訊號——`mcp_version.ts` 原註解已據實改寫，避免留下一句過時的謊。

### 2. Tier A 四條上架（六條候選，兩條擋下）

候選逐條經「現勘→對抗式覆核」雙代理處理（覆核者被要求試圖推翻而非附和），再由種子條目原樣走生產 `connect_mcp_server` 做 E2E：

| 條目 | 配方 | 授權 | E2E |
|---|---|---|---|
| Notion | `npx -y @notionhq/notion-mcp-server@2.5.0` | `NOTION_TOKEN` | CONNECTED 24 工具 |
| Atlassian | `uvx mcp-atlassian==0.23.0`＋`TOOLSETS=default`／`READ_ONLY_MODE=true` | `JIRA_URL`／`JIRA_USERNAME`／`JIRA_API_TOKEN` | CONNECTED 7 工具 |
| Figma | `npx -y figma-developer-mcp@0.13.2 --stdio`＋`IMAGE_DIR=@TOUKA_OUTPUT@`／`FRAMELINK_TELEMETRY=off` | `FIGMA_API_KEY` | CONNECTED 2 工具 |
| Context7 | `npx -y @upstash/context7-mcp@3.2.5` | 免金鑰 | CONNECTED 2 工具 |

配方上的三個非顯而易見處，皆為實測所得而非照抄 README：

- **Atlassian 必須收斂工具面**：不設 `TOOLSETS` ＝ 98 個工具、schema 約 22.5k token，足以淹沒模型 context；`TOOLSETS=default` 降為 35／9.5k，再加 `READ_ONLY_MODE=true` 為 16／4k。兩個旋鈕**正交**（default 工具組本身仍含 `jira_delete_issue`／`confluence_delete_page`），須同時下。出廠取最保守組合，要寫入權限的使用者可自行於設定頁調整。
- **Figma 的 `--stdio` 是必填**：不帶則起 HTTP 模式、stdio 握手永不回應＝連線逾時。另金鑰走 env 而非 README 建議的 argv（argv 在行程列表可見）。
- **Notion 對憑證 fail-open**：完全不給 env 仍 CONNECTED 並廣告全部 24 個工具，只有真正打 API 時才 401。故 `NOTION_TOKEN` 必須由安裝精靈端強制收，不能倚賴 server 自我把關；同時意味著 spawn 探測不可作為金鑰有效性驗證。

誠實標示（禁令 54）：四條 `is_official` 皆為 `false`（非 Anthropic 官方）；Atlassian／Figma 為社群維護，description 明文寫「非 Atlassian 官方」「非 Figma 官方」，並由測試釘死該字串。

### 3. 兩條擋下（hold，非 reject）

- **Stripe（`@stripe/mcp`）**：套件本身無可挑剔（第一方、SLSA provenance、MIT、無 install hook），但它實為 **stdio↔HTTP 橋接器**，轉發至 `mcp.stripe.com`。金鑰格式合法但無效時，遠端 401 只被 `console.error` 吞掉、stdio 不關 → **靜默無限掛住**。擋點不在套件而在 Touka 自家連線路徑（見下節），且它本質上屬 remote，歸屬應為批次 2/3 而非 Tier A。
- **Sentry（`@sentry/mcp-server`）**：技術面零缺陷（第一方、9 工具 E2E 通過、極活躍），但授權為 **FSL-1.1-ALv2（Functional Source License）＝非 OSI 開源**，與提案「只收 Apache-2.0／MIT／BSD 系」的收錄準則牴觸。覆核者實讀 FSL 全文後認為侵權風險被高估（Touka 非 Sentry 的競品、且散布的只是指向 npm 的 config），**真正待解的是「種子可否收錄非 OSI 條目」的內規**——這是 owner 政策題，不是工程題，故留 hold 待明示。

### 4. LIVE 生產缺陷修復：MCP 初次連線無逾時

Stripe 現勘揭露的缺口**不是 Stripe 專屬**，是任何「進程起得來但不回應 JSON-RPC」的伺服器都能觸發：

`connect_mcp_server` 的 `session.initialize()` 過去無任何逾時，而 `ProtocolManager.initialize_all` 是**序列** `await connect_server(...)` 且位於 `main.py` 啟動 lifespan。掛住不是 exception，`try/except` 完全接不住 → **ToukaProxy 啟動流程永不完成，排在其後的每一台 MCP server 都連不上**。等於「一個打錯字但格式合法的金鑰＝全域開機拒絕服務，且無任何錯誤訊息」。

修法為 `ClientSession(read_timeout_seconds=60s)`。**刻意不用 `asyncio.wait_for` 包住連線函式**：那樣的取消會跨 anyio 取消域，`except` 分支的 `stack.aclose()` 隨即拋 `RuntimeError: Attempted to exit cancel scope in a different task`＝teardown 破損反而更糟（已實測）。`read_timeout_seconds` 在同一 task 內計時，乾淨拋 `McpError`。
（2026-10-16 補註：[ADR-0273](0273-mcp-connection-owner-task.md) 起連線在專屬 owner task 進入與退出，`restart_server` 的 15 秒 `wait_for` 逾時只取消呼叫端；owner 在自己的 task 中止握手並關 stack，不再拋「different task」。呼叫端還要等 owner 收尾，restart 最壞約 22 秒。`MCP_INIT_TIMEOUT_SECONDS` 與 `read_timeout_seconds` 的設計不變。）

60s 的取值理由：此等待**涵蓋首次 npx/uvx 的套件下載**（下載期間 initialize 已送出、尚無回應），過短會誤殺慢速網路下的正常首裝。實測冷快取安裝 Atlassian 的 104 個相依僅 9.9s、暖快取 3.3s，故 60s 有充裕餘裕。

### 5. 前端 icon 跨層守衛

`McpHubItem` 的 `IconMap` 缺鍵時靜默 fallback 成 `Box`。實查發現**既有種子四條的 icon 全數失效**（`FileText`／`ShieldCheck` 兩個鍵根本不在表內），出廠貨架的圖示一直是通用方塊而無人察覺——與 ADR-0187 的 response_model 剝除、ADR-0188 的 pydantic `extra='ignore'` 靜默吞屬同一「無聲失效」家族。補齊 IconMap 並新增跨層守衛：讀後端種子檔比對每個 icon 是否可查到元件（真 SSOT 比對，非硬編清單）。

### 變異驗證（逐條隔離、皆已還原）

| 變異 | 結果 |
|------|------|
| M1 種子條目去掉版本標記 | 僅釘版棘輪紅 |
| M2 `ClientSession` 移除 `read_timeout_seconds` | 逾時兩測皆紅，且耗時由 20s 暴增至 150s＝卡死的直接證據 |
| M3 `IconMap` 移除 `FileText` | 僅 icon 跨層守衛紅 |

### E2E 實跑

四條種子條目原樣走生產 `connect_mcp_server`：Context7 2 工具／Figma 2 工具／Notion 24 工具／Atlassian 7 工具，全數 CONNECTED（全程 dummy 金鑰）。

Atlassian 首次以**受污染的 uv cache** 執行時觸發了 60s 逾時——查明為本機環境問題（ADR-0186 已記載的 Windows 全域 uv cache pywin32 毒化），改用乾淨 `UV_CACHE_DIR` 後 3.77s 連上。此次意外**反向驗證了逾時機制的價值**：若無本批修復，該次連線會永久掛住而非乾淨失敗。

## 測試

- `tests/unit/test_mcp_hub_seed_integrity.py` +5（釘版棘輪／Tier A 存在性／誠實標示／Atlassian 工具面收斂／Figma `--stdio`）
- `tests/unit/test_mcp_connect_timeout.py`（新，2）：真子進程模擬掛住的伺服器；測試自帶護欄並以**時長**為判準（E74 教訓「驗逾時的測試必須自有界」）
- `tests/unit/test_mcp_registry_entry.py` +1（無 version → 不上架）＋既有 5 條 fixture 補 version
- `tests/unit/test_mcp_hub_service.py`：三條官方條目測試改為「套件名正名前綴」斷言＝與釘版棘輪關注點分離（版本升級不誤紅正名測試）
- `ToukaChat/src/lib/__tests__/mcp_hub_icons.test.ts`（新，2）：跨層 icon 涵蓋守衛

## 後果

- fresh install 開箱 8 條貨架（自家文件引擎＋官方三條＋Tier A 四條），全數 spawn 實證可連。
- 供應鏈姿態升級：hub 三來源全釘版，安裝的版本＝人工已審的版本。
- 「一個壞金鑰讓 Proxy 開不了機」的 LIVE 缺陷關閉。
- **既有部署注意**：`.example` 為 fresh install 種子，bootstrap 只在 live 檔缺席時植入。已在跑的部署其 `config/mcp_hub.json` 不會自動獲得新條目與釘版，需按 Hub 的手動 sync 鈕或由部署者自行更新。

## 誠實邊界

- **逾時與 retry 放大**：`connect_server` 掛有 `@with_retry(max_retries=2)`，故單一掛死伺服器最壞等 3×60s。仍是**有界**且有錯誤日誌，與修復前的「永久靜默卡死」是質變；要再收斂需改動全系統共用的 `with_retry`（加例外跳過清單）或把 `initialize_all` 並行化（三家中 Antigravity 已如此做），屬另案。
- **Atlassian 的 env 是 one-of 形狀**：Cloud 走 `JIRA_USERNAME`＋`JIRA_API_TOKEN`、Server/DC 走 `JIRA_PERSONAL_TOKEN`；Jira 與 Confluence 完全獨立。種子取最常見的 Jira Cloud 三欄，Confluence 需自行於設定頁補 `CONFLUENCE_*`。安裝精靈目前對未填欄位會寫入 `FILL_ME_IN_*` 佔位符，對 one-of 場景不理想——精靈的「選填/多選」形狀屬另案。
- **名稱混淆風險（供應鏈）**：npm 上存在無 scope 的 `notion-mcp-server`（版本號 2.12.0 **高於**官方 2.5.0）與 Notion Labs 全無關係，且它**收錄在官方 MCP Registry、而真官方 `@notionhq` 反而沒收錄**。任何「挑版號大的」或「照 registry 抓」的流程都會抓錯套件。本批已逐字釘死 scope；另實查確認該山寨條目**不在生產 5 頁掃描窗口內**，故目前不會混入貨架，但此風險須留在台帳上。
- **上游漂移**：Notion 官方 README 已明文預告可能 sunset 本地 repo、issues/PR 不再主動監看（但 repo 未 archived、v2.5.0 於現勘前一天發版、週下載 148k）。Figma 條目的 `dist/lib/` 內含與 Figma 無關的 Context7 程式碼（經三重佐證不可執行：無 import 路徑、相依未宣告、無寫死憑證），下次升版時應複查是否已被上游清除。
- **Tier A 條目不進 registry 純映射**：四條皆為手動種子條目（Atlassian 不在 registry、Notion 官方未收錄），registry 同步不會自動更新它們，版本升級需人工複審。

## 關聯

- 提案：[mcp-connector-parity-and-auth-alignment.md](../proposals/mcp-connector-parity-and-auth-alignment.md)（批次 2＝L1 remote＋靜態 header，批次 3＝Shelf-B）
- 前作：ADR-0186（啟動規格 SSOT）／0187（registry 社群來源）／0188（enabled 咽喉）／0189（種子＋配方 parity）／0190（徽章 server_key）
- 政策：禁令 54（不得宣稱未經驗證的來歷）／禁令 55（只裝人工已審、pinned 來源）
- 教訓：E74「驗逾時的測試必須自有界」；ADR-0186 的 Windows uv cache 毒化環境事
