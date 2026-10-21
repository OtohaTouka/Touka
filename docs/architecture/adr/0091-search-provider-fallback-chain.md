# ADR-0091: 搜尋供應者備援鏈（Tavily→SearXNG→DuckDuckGo，含自架隱私後端）

**狀態 (Status):** ✅ 已接受
**日期 (Date):** 2026-06-13
**決策者 (Deciders):** 專案擁有者 + 主程

## 上下文 (Context)

`research` 原本只有單一主來源 Tavily（外加正交的 Gemini 原生 Google）。三個問題：

1. **韌性**：Tavily 金鑰缺失/額度用罄/服務異常時，搜尋直接硬失敗（回 `Error: TAVILY_API_KEY not found.`），無任何後援。
2. **免金鑰選項**：擁有者明確問過「有無不需要 API key 的方案？」——單靠 Tavily 無法回答。
3. **隱私**：ADR-0089 讓搜尋在所有 auth_level 自動執行、ADR-0090 預設 advanced 深度——查詢量上升且全數外送 Tavily。需要一條「查詢不外洩」的路徑。

這是 Web Search 改進 **Phase C（韌性＋免金鑰＋隱私）**，一併補上 Phase A/B 記錄的兩個取捨缺口。

## 決策方案 (Decision)

把 `research` 從單來源改為**備援鏈**，且**零新增 pip 依賴**（SearXNG/DuckDuckGo 皆免金鑰、純 httpx）：

- **`source="auto"`（新預設）**：依序 **Tavily → SearXNG → DuckDuckGo**，第一個有結果者勝出；任一供應者不可用（缺金鑰/URL）或失敗即跳下一個。順序＝品質優先（Tavily）→ 隱私免金鑰（SearXNG）→ 零設定免金鑰（DuckDuckGo）。
- **顯式來源**：`tavily` / `searxng` / `ddg` 只用指定供應者；`google` 維持 Gemini 原生（與鏈正交、不變）。
- **供應者實作**：各為模組級 async 純函式，回傳正規化 `{"answer", "items"}` 或 `None`（不可用）。
  - **SearXNG**：`GET {SEARXNG_BASE_URL}/search?format=json`（httpx）。`results[]`→items、`answers[0]`→answer（相容字串/物件兩種版本形狀）。
  - **DuckDuckGo**：官方 Instant Answer API `GET api.duckduckgo.com?format=json`（httpx、免金鑰、零設定）。`AbstractText/Answer`→answer、Abstract+`RelatedTopics`（攤平巢狀 Topics）+`Results`→items。
- **降級進度**：落到第 2 個供應者起 emit `tool_retry` 事件（沿用既有形狀，前端 useProgress 已能渲染）。
- **設定 SSOT**：`config.py` 加 `SEARXNG_BASE_URL`（空＝停用，auto 鏈跳過）。Tavily 調校沿用 ADR-0090；無新金鑰需求（DuckDuckGo 免金鑰）。

### 自架 SearXNG（隱私後端，opt-in）
- 提供**選用覆蓋檔** `docker-compose.searxng.yml` ＋ 最小 `searxng/settings.yml`（啟用 JSON 格式、關閉 limiter、密鑰由 `SEARXNG_SECRET` 注入），預設不對外發布埠（僅 `touka-net` 內部）。
- 啟用：設 `SEARXNG_SECRET`＋`SEARXNG_BASE_URL=http://touka-searxng:8080`，以 `docker compose -f docker-compose.yml -f docker-compose.searxng.yml up -d` 啟動。
- **不**併入預設 `up`——保持 secure/精簡 by default，要隱私再顯式開。

## 後果與影響 (Consequences)

- 搜尋不再因 Tavily 缺金鑰/掛點而硬失敗——`auto` 一路降級到免金鑰的 DuckDuckGo，**零設定即有保底**。
- **免金鑰**：DuckDuckGo（零設定）與自架 SearXNG（需起容器）皆免 API key，回答擁有者的提問。
- **隱私**：自架 SearXNG 使查詢不外洩第三方（補上 Phase A/B 的隱私缺口）；DuckDuckGo IA 僅回 instant answer、對一般查詢常為空（保底而非主力）。
- **取捨**：DuckDuckGo IA 非通用網頁結果 API，對多數一般查詢回得有限——它是「最後保底」，主力仍為 Tavily（品質）或自架 SearXNG（通用＋隱私）。
- **隱私邊界**：auto 的最後備援 DuckDuckGo 仍會把查詢送往 `api.duckduckgo.com`（免金鑰、HTTPS、不帶 Touka 憑證）。要「零查詢外送」須自架 SearXNG 並固定 `source=searxng`；`.env.example` 已加註此邊界。
- **延遲**：鏈為序列，SearXNG/DuckDuckGo 各 httpx 逾時 **5s**（Tavily 用 SDK 預設）。最壞情況——已設但失聯的 SearXNG ＋ 空的 DuckDuckGo——auto 會序列等到逾時（約 10s）才回；若 `SEARXNG_BASE_URL` 指向失聯端點，每次 auto 查詢都會多吃一次逾時，建議清掉該變數。常見情況（Tavily 健康、或 Tavily 未設）皆快速。
- **驗證範圍（誠實記錄）**：消費 SearXNG/DuckDuckGo 的程式路徑已以 mock httpx 完整單測；自架 SearXNG **容器本身未在本次環境實際啟動驗證**（無 docker），compose／settings 依官方 searxng-docker 標準最小設定撰寫，首次啟用請確認容器健康。
- **無 schema／端點變動**（工具呼叫 schema 非 openapi 一環）；**無新增 pip 依賴**（純 httpx，免 lock 重編/CVE 擴面）；guard 仍 32 道。

## Action Items
1. [x] `config.py`：加 `SEARXNG_BASE_URL`（Settings + legacy 匯出）。
2. [x] `research.py`：重構為供應者備援鏈（`_search_tavily`/`_search_searxng`/`_search_ddg`/`_search_google` ＋ auto 分派 ＋ 降級 emit ＋ 正規化格式化）；DuckDuckGo URL 去尾斜線（API_PREFIX_SSOT）；`_flatten_ddg_topics` 逐項 isinstance 守衛（畸形 RelatedTopics 不吞整批）；SearXNG/DDG httpx 逾時 5s。
3. [x] `builtin/schemas.py`：`source` enum→`auto/tavily/searxng/ddg/google`、預設 `auto`、描述更新。
4. [x] opt-in `docker-compose.searxng.yml` ＋ `searxng/settings.yml`；`.env.example`（根＋ToukaProxy）文件化 `SEARXNG_BASE_URL`/`SEARXNG_SECRET`。
5. [x] TDD：`test_research_providers.py`（auto 降級 Tavily→SearXNG→DDG、顯式來源、answers 形狀、巢狀 RelatedTopics、降級 emit、未知來源、Tavily 短路不打 httpx）；Phase B `test_research_tavily.py` 仍綠。
6. [x] ADR-0091 ＋ `adr/README`；`README_HANDOVER`／`CHANGELOG`／`tests/README` 同步；guard 32/32 ＋ 全量測試綠。

## 後續硬化（2026-06-16，Phase C 收尾）

Phase C 重構時，正交的 **`_search_google`（Gemini 原生）分支未一併硬化**，且為唯一無測試覆蓋的供應者。本次補上（與 `_flatten_ddg_topics` 逐項守衛同源的「單點失敗不吞整批」原則）：

- **逐模型 try/except 隔離**：原本整個 `models_to_try` fallback 迴圈共用一個外層 `try`——**第一個模型逾時/連線錯就中斷整批**，後面可用模型全被吞掉（與當初 DDG 畸形項吞整批同型缺陷）。改為每個模型各自 `try`，單一失敗只跳過該模型。
- **「200 但無文字」續試**：原本 200 卻無 text part 會回傳空白的 `--- Google Search Result ---`（假成功）；改為記錄後續試下一個模型。
- **可診斷的失敗訊息**：原本全鏈失敗一律歸咎「API 額度已達上限」（誤導）；改依最後狀態碼分流——429 額度／401·403 金鑰無效／其他狀態附 HTTP 碼／連線層例外附型別。
- **延遲匯入巢狀於 try**：`from src.config import GEMINI_API_KEY` 巢狀於 `try`（LOCAL_IMPORT_BAN 合規＋保留 monkeypatch 能力）。
- **測試**：新增 `tests/unit/test_research_google.py`（10 例：非 gemini 拒絕/缺金鑰不打網路、首模型成功、**首模型逾時隔離後第二模型勝出**、非 200 續試、空回應續試、429/403/其他/連線例外四種診斷訊息）。
- 純行為硬化，**無 schema／設定／依賴變動**；guard 34/34，後端全量 1868 綠（清淨環境重測，修正先前 mutmut 污染環境下被低估的計數）。
