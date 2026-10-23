# ADR-0194: remote 貨架首發＋手動新增 remote 伺服器（大廠 connector 對齊批次 3）

## 狀態

✅ 已接受（[MCP connector 大廠對齊提案](../proposals/mcp-connector-parity-and-auth-alignment.md) 批次 3 / Shelf-B）

## 日期

2026-07-25

## 背景

批次 2（ADR-0192）開通了 remote streamable HTTP 傳輸，但**貨架上一條 remote 條目都沒有**，且前端只有安裝彈窗跟上了新形狀——伺服器列表與手動新增表單仍是 stdio-only。批次 3 補齊這兩件事：把 remote 條目真的放上貨架，並讓使用者能自己新增／編輯 remote 伺服器。

維護者於本批點名加入 MS Learn 與 DeepWiki（批次 2 E2E 調查時實測連通的免認證公開端點）。

## 決策與實作

### 1. remote 貨架首發兩條（免認證）

| 條目 | 端點 | 工具 |
|---|---|---|
| MS Learn（Microsoft 官方） | `https://learn.microsoft.com/api/mcp` | 3（docs_search／code_sample_search／docs_fetch） |
| DeepWiki（Cognition 官方） | `https://mcp.deepwiki.com/mcp` | 3（ask_question／read_wiki_contents／read_wiki_structure） |

刻意選**免金鑰**的兩條作為 remote 形態首發：使用者裝完即可用，不必先去申請憑證，等於讓 remote 這條路徑有一個零摩擦的驗證入口。兩條皆以種子條目原樣走生產 `connect_mcp_server` 實證（見下）。

### 2. 前端補齊 remote 形狀（批次 2 的遺留缺口）

批次 2 只改了 `McpInstallModal`，另外兩處仍假設 `command` 必存在：

- **`McpServerItem`（伺服器列表）**：`{command} {args}` 對 remote 條目渲染出 `undefined`。改為 remote 顯示端點。
- **`McpServerForm`（手動新增／編輯）**：只有 command/args 欄位，且存檔鈕 `disabled={!command}`。後果是 **remote 條目「裝得進去、改不了」**——使用者想補憑證會被永遠 disabled 的按鈕卡住；而編輯態載入時把 remote 當成空 command 的 stdio，一旦存檔還會抹掉 `url`/`headers`。

  修法：加傳輸形態切換（本機 stdio／遠端 HTTP），依既有條目的形狀自動判定初始模式，欄位、必填驗證、風險文案隨模式切換；headers 比照 env 走 JSON 輸入並有獨立的解析失敗提示。

  這條路徑同時是 **per-user URL 型服務的唯一出口**——若某服務的端點因人而異（每個使用者在後台產生專屬 URL），就無法做成共用的 curated 條目，只能由使用者手動新增。

### 3. 技能↔MCP 接線健檢納入 remote

`skill_mcp_wiring.validate_skill_mcp_wiring` 原本「無 `command` 就略過」，註解寫著「非 hub-installable（SSE/url）」——那是 stdio-only 年代的正確判斷。remote 條目現在裝得起來，卻會靜默逃過接線健檢（Guard #37 的盲區）。改為「stdio 或非 SSE 的 remote」皆納檢；SSE 仍略過（ADR-0192 明定不支援該傳輸）。

### 4. LIVE 生產缺陷修復：傳輸層取消洩漏繞過所有 `except Exception`

上架 GitHub 條目後，以**假 PAT** 走生產路徑做反例實測，暴露出批次 2 遺留的嚴重缺陷：

遠端握手失敗（401）時，`streamablehttp_client` 的 anyio 子任務因 `raise_for_status` 取消其 cancel scope，呼叫端收到的是 **`asyncio.CancelledError`——它繼承 `BaseException` 而非 `Exception`**。於是：

- `connect_mcp_server` 的 `except Exception` 攔不住 → `stack.aclose()` 不執行（exit-stack 洩漏）
- `connect_server` 的 `@with_retry(exceptions_to_catch=(Exception,))` 攔不住
- `initialize_all` 的 `except Exception` 攔不住 → **整個開機迴圈被中斷**

決定性重現（伺服器順序 `[壞金鑰, 健康]`）：迴圈在第一台於 0.32s 中斷，**第二台連嘗試都沒嘗試**。這與 ADR-0191 修掉的「錯金鑰＝全域開機拒絕服務」是同一類事故換機制重現，且**已上架的 GitHub 條目當下就有此曝險**——使用者貼錯或過期的 PAT 即可讓 ToukaProxy 開不完機。

另有一層遮蔽：teardown 的 `aclose()` 常在不同 task 退出 cancel scope 而拋 `RuntimeError: Attempted to exit cancel scope in a different task`，該例外發生在 `except` 內會**取代**原始錯誤，讓「token 無效」變成一句沒人看得懂的 anyio 內部訊息。

**修法**（`connect_mcp_server`）：
1. 改捕 `BaseException`，並以 `contextlib.suppress` 保護 teardown——清理盡力做，但其例外不得遮蔽原因。
2. 對 `CancelledError` 用 **`task.uncancel()` 的回傳值**精確區分來源：回 `0` ＝ 取消完全源自本次連線內部的傳輸層，轉成 `ConnectionError` 讓上游照常處理；回 `>0` ＝ 呼叫端也要求取消（關機／中止），原樣尊重不吞。

修復後實測：GitHub 壞 PAT 0.66s、Stripe 壞 rk 0.28s 皆為乾淨的 `ConnectionError`，取消殘留歸零；`[壞金鑰, 健康]` 順序下健康伺服器成功連上、`initialize_all` 正常完成。

### 5. Shelf-B 四服務現勘結果

| 服務 | 結果 | 依據 |
|---|---|---|
| **GitHub** | ✅ 上架（`/readonly` 端點） | PAT 走 `Authorization: Bearer` 有官方文件明載（非從 401 推論）；存在 `/mcp/readonly` 變體，出廠取唯讀 |
| **Zapier** | ✅ 上架 | **原「per-user URL」假設證偽**——現行是共用端點 `/api/v1/connect` ＋ per-user Bearer token；官方明文「does not support SSE」正合 Touka 只收 streamable HTTP |
| **Stripe** | ✅ 上架（最保守配方） | 官方文件明載可用 API key 當 bearer；**強烈建議 restricted key（rk_）** |
| **Figma** | ❌ 兩種形態皆 hold | 見下 |

**Figma 的兩條路都不通，但性質不同**：

- **遠端版 OAuth-only**——與 ADR-0191 認定的 L4 平台級 hosted 同屬**結構性不可對齊**。現勘中出現一條極具誘導性的線索（回應寫「figd_ tokens must be passed via X-Figma-Token header」，看似只要換 header 就能用 PAT），經三路證偽：該訊息是 Figma REST gateway 的全域規則（對 `api.figma.com/v1/me` 送同樣 header 得到逐字相同回應）；行為級反證是帶 `X-Figma-Token` 與**完全不帶認證**的回應位元組相同（MCP 層根本不消費該 header）；Figma 官方支援亦明覆「does not support authentication using personal access tokens and this cannot be enabled」。
- **本機 Dev Mode 版形狀完美卻被部署形態擋死**——端點 `http://127.0.0.1:3845/mcp`、免認證、streamable HTTP，且 Touka 的 URL 閘對 loopback 明文 http 本就豁免。但 `touka-proxy` 跑在 bridge 網路容器內，容器的 `127.0.0.1` 是容器自己、不是跑著 Figma 桌面版的主機；慣用解 `host.docker.internal` 又會被同一個安全閘擋下（明文 http 非 loopback）。**這是唯一需要維護者決策的項目**：原生部署即可用；容器部署則須評估是否為此放寬 URL 閘——該端點不帶任何憑證，明文外傳沒有金鑰可洩，風險論證與一般 remote 條目本質不同，值得單獨評估而非一概否決。

**OAuth 解決不了 Stripe 的權限問題（批次 4 的決策依據，2026-07-26 查證）**：直覺上會以為「等批次 4 上了 OAuth，Stripe 就安全了」。**實測推翻此假設**——`https://access.stripe.com/.well-known/oauth-authorization-server/mcp` 的 `scopes_supported` **只有一個值 `["mcp"]`**：沒有逐資源 scope、沒有 read/write 分離、沒有任何可勾選細項。OAuth 授權綁定的是一個 Stripe Dashboard 使用者，token **繼承該使用者的角色權限**（授權者若是 Administrator，token 就約等於 Administrator）。

官方文件那句常被引用的「OAuth is more secure than using your secret key because it allows more granular permissions」——**比較對象是 secret key（`sk_`），不是 restricted key（`rk_`）**。Stripe 官方從未直接比較 OAuth 與 RAK；其唯一的比較表是 `sk_` vs `rk_`，結論為 RAK "Preferred"。而對本案情境，官方立場明確：

> "If you're building agentic software, you can pass a Stripe API key as a bearer token… **We strongly recommend using restricted API keys**"
> "Stripe recommends always using RAKs instead of unrestricted secret keys, **especially when giving a key to an AI agent**."

決勝點是**弱點的可補性不對稱**：RAK 的弱點（長期有效）可用工程手段補（vault、短週期輪替、request log 稽核）；**OAuth 的弱點（無細粒度 scope）無法用任何設定補**，那是 Stripe 目前的產品缺口。另：Connect 連結帳戶情境官方明示**不支援 OAuth**，只能用 RAK。

結論：**Stripe 條目維持走 restricted key 是正確選擇，且不會因批次 4 落地而改變。**

**Stripe 的風險必須單獨記帳**：它是目前**唯一無法出廠鎖唯讀**的條目。官方工具表共 13 個，其中兩個容易被漏看的風險面：`stripe_api_write` 可**建立 webhook endpoint**（把金流事件導向任意外部 URL＝資料外洩管道），`send_stripe_mcp_feedback` 會把內容外送給 Stripe。另注意官方文件自相矛盾——`stripe_api_write` 描述寫「any Stripe API POST/PATCH/PUT/DELETE method」（無界），但同頁「Supported API methods」是有限清單，官方未澄清何者為實際邊界，**不應假設有限清單就是硬邊界**。Treasury 工具（真正的移錢、付帳單、發卡）需另行申請 preview，目前預設關閉。GitHub 有 `/readonly` 端點、Atlassian 有 `READ_ONLY_MODE` env，Stripe 兩者皆無——其工具面含 `stripe_api_write`（可對任意 Stripe API 發 POST/PATCH/DELETE）與 `create_refund` 這類直接動錢的原語，且「哪些工具存在」與「金鑰能做什麼」完全脫鉤。**安全邊界 100% 落在使用者的受限金鑰權限上，Touka 這端一個檢查點都沒有。** 因此配方取最保守值：env 變數刻意命名 `STRIPE_RESTRICTED_KEY`（而非慣例的 `STRIPE_API_KEY`）作為 UI 上唯一的提示槓桿，description 明講「金鑰權限是唯一的安全邊界、Touka 無法代為限制」並建議先用 `rk_test_`。若維護者認為金流風險不該由文案承擔，撤下此條目即可（使用者仍可自行手動新增）。

## 測試

- `tests/unit/test_mcp_connect_teardown.py`（新，5）：teardown 失敗不遮蔽原因（remote／stdio 各一）、**取消洩漏轉一般例外**、**外部取消仍被尊重**（陰性對照）、成功路徑仍正常關閉
- `tests/unit/test_mcp_hub_seed_integrity.py` +3（remote 種子形狀／帶憑證條目的 header 只放 `${VAR}` 模板／Stripe 無界寫入風險揭露）；`test_curated_entries_honest_and_safe` 依形狀分寫安全檢查（stdio 驗命令注入／remote 驗 URL 安全閘）
- `tests/unit/test_skill_mcp_wiring.py` +2（未註冊的 remote 應告警／已註冊則否）
- `ToukaChat`：`McpServerForm.test.tsx` +4（編輯 remote 載入 url/headers／存檔後仍是 remote／遠端模式以 url 為必填／headers JSON 無效不存檔）、`McpServerItem.test.tsx` +1（顯示端點不顯示 `undefined`）

### E2E 實跑

- 兩條免認證種子條目原樣走生產 `connect_mcp_server`：MS Learn CONNECTED 3 工具、DeepWiki CONNECTED 3 工具。
- **反例（本批 LIVE 缺陷的直接證明）**：以假憑證連 GitHub／Stripe 真實端點，修復前拋 `CancelledError`＋`RuntimeError` 噪音、逃逸 `except Exception`；修復後 0.66s／0.28s 乾淨 `ConnectionError`，取消殘留歸零。
- **開機迴圈韌性**：真 `ProtocolManager.initialize_all` 以 `[github(壞PAT), deepwiki(健康)]` 順序執行，壞條目重試後放棄並留下可讀日誌，**健康伺服器成功連上**、迴圈 5.5s 正常完成（修復前第二台不會被嘗試）。

## 後果

- 出廠貨架涵蓋 stdio 與 remote 兩種形態，remote 有零摩擦的入門條目。
- 使用者可自行接任何 streamable HTTP 端點（含 per-user URL 型服務），不受策展清單限制。
- remote 條目在列表、編輯、接線健檢三處的行為與 stdio 對等。

## 誠實邊界

- **帶憑證的條目未做真連線 E2E**：GitHub／Zapier／Stripe 三條的**成功路徑**未實跑——不應為測試索取真實憑證。已驗證的是形狀（模型層、header 模板解析）與**失敗路徑**（假憑證乾淨失敗）。首次使用前建議使用者自備短效憑證各跑一次確認。
- **免認證條目的可用性依賴第三方**：MS Learn／DeepWiki 是外部免費服務，可能變更政策或下線。種子條目失效時使用者會看到連線錯誤（有逾時與取消洩漏保護、不會卡死啟動），但 Touka 無從預警。
- **手動新增的 remote 未做端點可達性預檢**：存檔即寫入設定，連不連得上要等 agent 實際使用時才知道。stdio 路徑有 mcp-scan 安全掃描，remote 沒有對應機制（掃描的是本地指令）。（2026-10-09 補記：stdio 的 mcp-scan 掃描也已移除，見 SECURITY.md SP3。）
- **Stripe 無 per-tool 治理**：Touka 目前只有 per-server 的 `enabled` 開關與逐呼叫 HITL 審批，沒有 Codex `enabled_tools`／Antigravity `disabledTools` 的對等物。對含通用寫入原語的服務（Stripe）而言，這代表無法在 Touka 側收斂工具面——per-tool 治理已列於提案 §7 的日後候選。
- **GitHub PAT 類型的官方文件自相矛盾**：README 指向 fine-grained token 頁、另一份安裝指引指向 classic token 頁；且**官方從未給出建議 scope 清單**（只說「授予你願意授予的權限」）。條目描述取 fine-grained 為建議值，但這是判斷而非官方背書。classic PAT 的 `repo` scope 沒有粒度——勾了等於交出所有私有儲存庫的完整讀寫權，這是選 fine-grained 的實質理由。

## 關聯

- 提案：[mcp-connector-parity-and-auth-alignment.md](../proposals/mcp-connector-parity-and-auth-alignment.md)
- 前作：ADR-0192（remote 傳輸與 header 模板設計）／0191（釘版、Tier A 貨架）／0190（徽章比對鍵）
- 相關守衛：Guard #37（技能↔MCP 接線一致性）
