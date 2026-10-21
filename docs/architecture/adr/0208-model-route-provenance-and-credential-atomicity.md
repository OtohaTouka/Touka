# ADR-0208：模型路由來源與憑證原子性

- **狀態**：Accepted；implementation 已提交為 `8e9c59a5`
- **日期**：2026-08-03
- **相關**：[ADR-0099](0099-provider-registry-ssot.md)、
  [ADR-0131](0131-auth-renewal-and-ldh-containment.md)

## 脈絡

`connections.json.example` 的 `local-default` 是 ToukaChat 連入 ToukaProxy 的控制面設定，
但 `ModelManager` 曾把所有 enabled connection 都當成上游執行 `/models` 發現。Proxy 因而向
自己的 `/v1/models` 取得正式模型 ID，再以同 ID 將 `_source_url=http://localhost:8000` 混入
模型快取。Dispatcher 又分別覆寫 URL 與 key：空白自訂 key 不覆寫正式 provider key，造成
provider bearer 被送回 ToukaProxy Gate 1，並被正確判成無效 JWT（`AUTH_INVALID_TOKEN`）。

同一分離覆寫亦存在於非標準 chat payload `base_url`：合法 Touka JWT 呼叫者可指定任意端點，
而 Dispatcher 仍沿用正式 provider key。這不是 JWT 驗證錯誤；修 JWT 會破壞 LDH fail-loud 邊界。

## 決策

1. `provider=proxy` 固定代表 ToukaChat → ToukaProxy 控制面，不參與模型上游探索。
2. 自訂模型的執行期路由由 `CustomModelRoute` 保存 `connection_id`、正規化 endpoint、
   api key 與 provider。該物件不可變、只存在記憶體，不進 `models.json`、Models API 或 log。
3. `ModelManager` 以完整新快照原子替換自訂路由。刷新會移除已停用、刪除、失敗或已改成
   控制面的舊路由；舊 `_source_url`／`_api_key` 只作滾動升級清理，不再是執行來源。
4. Dispatcher 順序固定為 Human model → 明確自訂路由 → 原生 Claude/Gemini →
   Provider Registry 名稱解析。自訂 endpoint 與 key 整組採用；空 key 是明確值，禁止回退。
5. 多個自訂連線宣告相同 model ID 時保留第一個、對後續來源回報已消毒衝突，禁止
   last-writer-wins 接管路由。全面模型 ID namespace 重設不屬本批。
6. `OpenAIChatRequest`／`AnthropicRequest` 的 `base_url` 僅允許 `null`／省略；非空值在
   Pydantic 邊界以 422 拒絕。`baseUrl`、`base-url`、大小寫等語意別名同樣 422，不得被
   Pydantic extra-ignore 靜默接受。上游端點只能來自 Provider Registry 或 Owner 管理且經 SSRF
   驗證的 Connections。不存在於 schema 的 `request.api_key` 死碼一併移除。
7. JWT、refresh、Gate 1 與 LDH 完全不變：失效 bearer 仍須回 `AUTH_INVALID_TOKEN`。

## 結果與邊界

- 預設 `local-default` 可繼續供 ToukaChat 選擇 Proxy，不需修改實際 `connections.json`。
- 自訂無 key 上游（例如本機 OpenAI-compatible endpoint）不會收到任何正式 provider key。
- 自訂模型名稱不需包含 `gpt`／`grok`／`gemini` 等關鍵字，明確路由先於名稱推測。
- 非標準、曾依賴 request `base_url` 的外部 client 會收到 422，必須改用受管理 Connection。
- 本批不修改 `data/`、MCP P8-B、企業 IdP／SSO 或模型 ID namespace。

## 替代方案

- **放寬 JWT 讓 provider key 通過**：拒絕；會把模型供應商憑證誤當 Touka 身分。
- **只特判 localhost URL**：拒絕；127.0.0.1、LAN、容器 alias 與反向代理仍可繞過，且混淆
  控制面／資料面的根因未消失。
- **URL 覆寫時只清空 key**：拒絕；雖阻止 key 外洩，仍保留自我路由與錯誤端點。
- **把路由秘密繼續放模型 dict**：拒絕；模型合併以 model ID 為鍵，會再次把來源與展示資料混合。

## 驗證

- TDD 覆蓋控制面排除、空 key 原子性、明確路由優先、秘密不進 descriptor、刷新清除舊路由、
  重複 ID 非 last-writer-wins，以及 OpenAI／Anthropic 真 router 的 `base_url`／alias type matrix。
- 自動驗證：完整 pytest 4560 passed／5 skipped、ToukaChat 1150 passed／228 files、
  Ruff 與 44/44 guards 全綠；pip-audit 與 production npm audit 均為 0 vulnerabilities。
- OpenAPI／generated TypeScript types 已同步，MkDocs 一般建置成功；strict 模式仍被既有 115 個
  歷史 warning 阻擋。HUMAN_ONLY 對話場景與 DEV-B 對抗式複審仍是提交前人工閘。
- 2026-08-08 Phase 4：真 `/v1/chat/completions`／`/v1/messages` 各自驗證省略／`null` 接受，
  空字串、一般字串、數字、物件與 alias 皆 422；修復前兩個 `baseUrl` 案會誤收 200，修復後
  endpoint＋route isolation 聚焦 28 passed。Phase 4 最終全量為後端 **4622 passed／5 skipped**、
  ToukaChat **1150 passed／228 files**、候選 Ruff／MkDocs／diff check 全綠；pip-audit 通過，
  guards **43/44**，唯一紅燈為 production npm audit 5 vulnerabilities，故 DEV-B 尚未開始。
