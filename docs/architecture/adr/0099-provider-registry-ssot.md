# ADR-0099: Provider Registry SSOT 與供應商系統統一（含 Parity Guard #36）

**狀態**：✅ 已接受
**日期**：2026-06-18
**影響範圍**：ToukaProxy（registry／dispatchers／models／routers／config）、ToukaChat（providerKeys）、scripts/（守衛）
**關鍵字**：Provider Registry, SSOT, 硬編碼消除, 路由, 動態發現, 健康檢查, 金鑰遮罩, SSRF, 即時金鑰（免重啟）, 探測, Guard #36, parity, DeepSeek

---

## 背景

「系統認得哪些 AI 供應商」這份事實，原本散落在 **7–8 處彼此不一致的硬編碼**：路由 `get_provider_info` 的 if 鏈、動態發現 `refresh_all_dynamic_models` 的 `asyncio.gather` 清單（且 `main.py` 啟動時又複製一份）、`get_models` 的健康白名單、`fetcher.py` 的具名抓取器、`config.py` 的金鑰、前端 `providerKeys.ts`／`providerCatalog.ts`。新增一家供應商得同時改好幾處，漏一處就「能路由不能發現」或反之——**DeepSeek 即實例**（加了路由與金鑰卻不出現在模型清單）。

伴隨的資安/體驗缺口：連線 `apiKey` 經 GET 明文回前端；金鑰存進 .env 需重啟才生效；金鑰無效/未支援時靜默失敗（看起來只是「0 個模型」）；自架連線網址無 SSRF 防護。

## 決策

建立 **`src/services/providers/registry.py`** 作為供應商事實的單一來源（`PROVIDER_SPECS` 宣告式清單），路由、發現、健康檢查、金鑰解析全部由它衍生。配套五個面向（分階段落地）：

1. **路由／發現／健康衍生**：`get_provider_info`、`refresh_all_dynamic_models`（含 `main.py` 啟動）、`get_models` 健康白名單皆改為迭代 registry；殺掉重複的 gather 清單。新增供應商 = registry 加一筆。
2. **金鑰遮罩 SSOT**（`utils/secrets.py`）：連線 `apiKey` GET 遮罩、POST write-only；與 `/config/env` 共用。
3. **SSRF 守衛**（`utils/ssrf_guard.py`）：連線/發現對外網址擋雲端 metadata/link-local、非 http(s)、各式 IP 編碼（含 `inet_aton` 正規化）；**刻意放行 loopback/私網**（自架 Ollama/LiteLLM/區網為合法目標）。
4. **不靜默失敗**：發現錯誤分類（auth/not_found/...）逐供應商沙化回報；未支援 `*_API_KEY` 稽核。
5. **金鑰即時生效（免重啟）＋ 探測**：`config.apply_runtime_env` 使 `registry.get_key`（單一 chokepoint）即時讀到新值；`POST /config/env` 存 provider 金鑰後定向探測（自動上架模型＋回 valid/invalid/cleared）；清除金鑰 → 模型由健康邏輯自動離線。

### 金鑰存取單一 chokepoint

所有金鑰一律經 `registry.get_key(spec)` 讀取（fetchers 改收 `api_key` 參數，不再各自 `from config import KEY`）。這讓「免重啟即時生效」只需 `apply_runtime_env` 更新一處來源，全鏈路即時可見。

### Guard #36 — Provider Parity（本 ADR 的強制機制）

新增 `scripts/guard_provider_parity.py`：**後端 registry 每個可發現供應商的主要金鑰 env 名，必須出現在前端 `providerKeys.ts`**（backend ⊆ frontend，單向；前端可有額外非模型金鑰如 Tavily）。後端加供應商卻忘了前端金鑰 UI → fail-closed。以 registry 自身 API 為事實來源、前端純讀檔擷取，無網路、決定性。

## 對齊原則

| 原則 | 落實方式 |
|------|---------|
| **SSOT** | registry 為供應商事實唯一來源；金鑰存取單一 chokepoint；遮罩邏輯單一 util |
| **TDD** | 每階段先測後實作（registry 路由/發現/健康、SSRF、探測、parity 註冊） |
| **角色分離** | 官方上游供應商 → 系統金鑰（Provider Keys）；連線頁 → 自架/代理/聚合端點 |
| **安全預設** | `ENABLE_DYNAMIC_MODELS` 預設 false；金鑰永不回前端；SSRF 擋 metadata；對抗式複審 |

## 後果

### 正面
- 供應商事實單一來源；新增供應商 = registry 一筆（＋前端金鑰 UI，由 #36 強制）。
- DeepSeek 等新供應商自動可路由＋可發現；金鑰免重啟即時生效並當場探測回報。
- 連線金鑰不再明文外洩；自架網址有 SSRF 防護（不破壞 Ollama/LiteLLM/區網）。
- 守衛數 35 → **36**。

### 權衡
- registry 帶 `api_format`/`discovery_format`，因應 Gemini/Anthropic 非 OpenAI 相容格式，非「一律 openai」。
- parity 守衛為單向（backend ⊆ frontend）：前端 model-provider 金鑰若無後端對應，不被攔（容許 Tavily 等非模型金鑰，取捨可接受）。
- SSRF 守衛刻意放行私網（自架威脅模型下的有意決策）；殘餘的「網域名稱 DNS-rebind」未在驗證時解析（admin-gated ＋ follow_redirects=False 緩解）。

### 建基於
- 呼應 **ADR-0090/0091**（研究供應商鏈）、**ADR-0030**（NOQA Registry doc-vs-reality 同步精神）；與 **ADR-0097/0098** 同屬靜態守衛防線族。
