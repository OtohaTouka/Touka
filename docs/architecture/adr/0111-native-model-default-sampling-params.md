# ADR-0111：原生模型 per-model 預設取樣參數 — ModelsTab 可編、注入層精準度、供應商漏接修復

- **狀態**：✅ 已接受（**Phase 1 後端＋Phase 2 前端均經 TDD 完成並全綠驗證**：guard 37/37、後端 pytest（驗證/持久化/注入層/轉譯器轉發/能力衍生/原生判準 SSOT/單模型端點一致）、vitest（面板顯示/provider-aware top_k 隱藏/儲存/外部刷新重同步/收合確認）、openapi+api.ts 重生、兩階段各經對抗式複審）（實作位置修訂 by [ADR-0156](0156-react-core-extraction.md)：Claude/Gemini 取樣參數轉發實作移至 adapters；注入層行為不變）（D4 的 ModelsTab 參數編輯面板已由 [ADR-0250](0250-touka-leftovers.md) 修訂：2026-10-14 拿掉，因為 ADR-0241 起取樣參數不送給廠商；`ModelInfo` 參數欄位、能力旗標與 `models.json` 存放值保留）
- **日期**：2026-06-24
- **影響範圍**：ToukaProxy（`routers/models.py` ModelInfo/ModelSettingsUpdate＋persist＋能力旗標衍生、`services/models/manager.py` patch、`services/providers/registry.py` 能力與 `is_native_owner` SSOT、`middleware/layers/native_model_layer.py`＋pipeline 註冊、`handlers/claude_handler.py`＋`adapters/openai_to_claude.py`＋`adapters/openai_to_gemini.py` 供應商轉發）＋ ToukaChat（`ModelsTab` 參數編輯 UI、`ModelProvider` 型別/`updateModelSetting`、i18n）
- **關鍵字**：model parameters, temperature, top_p, top_k, native model defaults, fill-when-absent, model_fields_set, pipeline layer precedence, Claude/Gemini param forwarding, 設定頁路線圖, 建基於 ADR-0099

## 背景（Context）

取樣參數（temperature/top_p/top_k）在 **ModelStudio（自訂模型 L2）**、**SoulStudio（souls L1，僅建立時）** 已可編，各自存 `config.json`，經 `CustomModelLayer`/`SoulLayer` 注入（覆蓋 request）。但 **ModelsTab 不可編**，`ModelInfo`/`ModelSettingsUpdate` 無參數欄位。

**真正的缺口**：**原生供應商模型**（gpt-4o、claude、gemini… 聊天最常選的）**完全沒有持久參數**——只能靠 request schema 預設（temperature=1.0）。想固定某原生模型的溫度做不到。ADR-0099（Provider Registry SSOT）刻意只決定供應商/金鑰/發現路由，**未決定參數歸屬**（`ProviderSpec` 無取樣參數欄位）——本 ADR 補上。

研究另揭兩個**隱性 no-op**：`convert_openai_to_anthropic` **硬寫 `temperature=1.0`** 且漏 `top_p`/`top_k`；Gemini `generationConfig` 只送 `temperature`。故連 ModelStudio/Soul 對 Claude/Gemini 設的取樣參數於**轉譯期就被丟棄**、從未生效。

## 決策（Decision）

### D1 — 參數歸屬：原生模型 per-model 預設存 `models.json`
`ModelInfo`/`ModelSettingsUpdate` 加 `temperature`(0–2)/`top_p`(0–1)/`top_k`(1–500)（nullable，Field 範圍驗證）；`manager.patch_model_settings` 白名單持久化（顯式 `None`＝清除回退預設）。custom/soul **維持各自 `config.json`**（在 Studio 編，不重複、不雙重來源）。`_merge_models` 對既有模型只刷 `last_seen`/runtime 欄位，故參數**跨模型刷新存活**。

### D2 — 注入層 `NativeModelLayer`：fill-when-absent，精準度在 custom/soul 之下
新層註冊於 pipeline **MonitorLayer→ContextLayer→`NativeModelLayer`→CustomModelLayer→SoulLayer**。**僅對原生模型**（`owned_by` 非 custom/proxy/touka）生效；且**僅填補 request 未顯式帶的參數**（Pydantic `model_fields_set`）。因此：①不覆蓋 API 消費者的 per-request 明確值；②跑在 custom/soul 之前 → 自訂模型把 base 換成原生 id 後，custom/soul 的設定仍覆蓋本層。**精準度：per-request 明確值 > custom(L2) > soul(L1) > 原生 per-model 預設 > schema 預設**。

### D3 — 修供應商轉發漏接（讓參數真的生效）
- **Claude**：`convert_openai_to_anthropic` 改自來源請求帶 `temperature`/`top_p`/`top_k`（非硬寫 1.0）；`claude_handler` kwargs 補轉發 `top_p`/`top_k`（Anthropic 原生支援）。
- **Gemini**：`openai_to_gemini` 的 `generationConfig` 補 `topP`/`topK`（僅實際有值時）。
- 連帶修好 ModelStudio/Soul 對 Claude/Gemini 既有的潛在 no-op。

### D4 — Phase 2：ModelsTab provider-aware 編輯 UI（前端）＋能力旗標 SSOT
- **能力自後端衍生（不在前端硬編供應商知識）**：`ModelInfo` 加 `params_editable`/`supports_top_k`/`temperature_max`，由 `registry.model_param_caps(owned_by)` 自 Provider Registry SSOT 衍生（延續 ADR-0099 消除多處硬編碼）。`supports_top_k` 僅 anthropic／google（Gemini）為真——OpenAI 相容路徑會 pop top_k；`temperature_max` anthropic 為 1.0。前端只渲染後端說了算的能力，零供應商分支。
- **原生判準單一 SSOT（`registry.is_native_owner`）**：「該注入／可持久化／可編輯」三處（`NativeModelLayer`、`patch_model_settings`、ModelsTab `params_editable`）統一改用 `is_native_owner`（有 ProviderSpec＝原生），取代先前 `NativeModelLayer`／patch 各自的 `{custom,proxy,touka}` blocklist——杜絕兩套定義對 `openrouter` 等自訂連線 owned_by 漂移（複審 #2）。`GET /models/{id}` 亦改自 registry 衍生能力，與列表端點一致（複審 #3）。
- **`ModelParamPanel`（手風琴，一次一個）**：temperature/top_p 滑桿＋top_k **僅 `supports_top_k` 才顯示**；草稿 null＝「預設」、可重設；顯式儲存（PATCH，null 清除、未動不送），`updateModelSetting` 回傳 ok 布林供成功/失敗 toast；接入 C5 未存變更守衛（`useDirtyGuard`）。
- **草稿生命週期穩健性**：①外部刷新（強制重掃／他人工作階段）改了伺服器持久值且使用者未編輯 → 以 React「render 期依 prop 快照調整 state」模式重新同步，避免陳舊草稿覆蓋（複審 #1 lost-update）；②收合／切換有未存草稿的面板前以 `confirmToast` 確認（複審 #4，C5 一致、不靜默丟棄）。

### 替代方案（Rejected）
- **ModelsTab 也編 custom 模型**：與 ModelStudio 重複、雙重來源 → 否決（custom 留 Studio）。
- **per-conversation 參數**：需 session 級新儲存、無既有 seam → 否決（per-request 已可即時覆蓋）。
- **per-soul 建立後編輯**：`data/soul/` 為保護路徑、且屬「碰既存 agent 設定」→ 本 ADR 不納（未來另議）。

## 後果（Consequences）

- ✅ 原生模型可在 ModelsTab 設持久取樣參數；填補真正缺口、constraint-clean（不碰 agent/soul）。
- ✅ 修好 Claude/Gemini 的參數轉發 no-op，惠及 ModelStudio/Soul。
- ✅ fill-when-absent + `model_fields_set` → 不破壞 API 消費者的明確 per-request 值。
- ⚠️ 注入層每請求掃 `get_models()`（記憶體清單、數十項，可忽略）。
- ⚠️ `top_k` 非 OpenAI Chat 標準參數 → OpenAI/Grok/DeepSeek 路徑會 pop 掉（僅 Anthropic/Gemini 生效）；**Phase 2 UI 已依 `supports_top_k` 隱藏不支援的 top_k 滑桿**。
- ✅ Phase 2：原生模型可在 ModelsTab 直接編 per-model 預設參數，provider-aware；能力與原生判準皆自 registry SSOT 衍生（前端零供應商分支、三處定義同源）。

## 驗證
參數範圍驗證、持久化（含清除/非原生略過）、注入層情境（填補/不覆蓋/跳過 custom/未知 no-op）、Claude/Gemini 轉譯器轉發、`convert_to_openai` 轉發皆經測試覆蓋（13 測）。

**Phase 1 對抗式複審（3 個維度）→ 修 7、駁回 1**：①【High】`convert_to_openai`（Anthropic→OpenAI，預設 UI /messages 路徑）只帶 temperature → 原生 Gemini/OpenAI 相容與 custom/soul 的 top_p/top_k 於轉換期靜默丟失 → 補轉發；②【High】top_k 洩漏到 OpenAI/Grok/DeepSeek → HTTP 400 → `openai_handler` pop top_k；③【Med】`AnthropicRequest.top_p` 預設 1.0 → 改 None；④【Low】temperature le=2 對 Anthropic 會 400 → claude_handler 夾 1.0；⑤【Low】GET /models 列表漏帶參數 → 補；⑥【Med】群聊 per-agent bypass pipeline → 抽 `apply_native_defaults` 共用、群聊補呼叫；⑦【Low】非原生模型存取樣參數＝dead data → 略過。駁回 1（models.json 手改髒值＝operator self-harm、out-of-threat-model、fail-loud）。

**Phase 2 對抗式複審（4 個維度；15 findings → 逐項對抗式驗證 → 4 confirmed、11 駁回）→ 全修**：①【Med】`ModelParamPanel` 草稿僅 mount 時 seed、面板開啟期間外部刷新（強制重掃／他人工作階段）改了伺服器值 → 以陳舊草稿覆蓋＝靜默 lost-update → render 期 prop-快照重新同步（未編輯才採用）；②【Low】`params_editable`（registry allowlist）與 `NativeModelLayer`／patch（`{custom,proxy,touka}` blocklist）兩套「原生」定義對 `openrouter` 等自訂連線 owned_by 漂移 → 統一 `registry.is_native_owner`；③【Low】`GET /models/{id}` 能力旗標回退 schema 預設、與列表矛盾 → 改自 registry 衍生；④【Low】收合／切換有未存草稿的面板靜默丟棄、繞過 C5 → 收合前 `confirmToast` 確認。無 High。

> **補記（2026-10-11，[ADR-0241](0241-model-layer-stage-0.md)）**：送出端退役——三個 adapter 與 ToukaChat 都不再把溫度／top_p／top_k 送給廠商；本 ADR 的欄位、設定 API、注入層與 UI 拉桿保留但沒有作用，清理另開任務。
