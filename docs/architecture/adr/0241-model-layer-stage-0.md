# ADR-0241：模型溝通層第 0 段（測試網路封鎖、取樣參數不送、同家備援與小修）

- 狀態：✅ 已接受（維護者 2026-10-11 核准計畫與決定 A–G）
- 日期：2026-10-11
- 決策者：維護者
- 影響範圍：三個 provider adapter、`SoulLayer`、`proxy_service` 備援、研究工具 Google 搜尋、創建評分、備份／還原／子程序清理、`tests/` 護欄、`pyproject.toml`／`uv.lock`、ToukaChat 錯誤處理與送出內容
- 修訂：[ADR-0111](0111-native-model-default-sampling-params.md)（送出端退役）、[ADR-0155](0155-handler-drift-fix-batch.md)、[ADR-0157](0157-react-core-followups.md)、[ADR-0214](0214-standalone-smoke-data-and-metered-api-boundaries.md)
- 決策 4 已由 [ADR-0243](0243-model-pre-run.md) 取代（2026-10-12：Gemini 顯式快取全停，一般與無痕對話都不建）
- 決策 3 已由 [ADR-0247](0247-model-layer-stage-3a.md) 取代（2026-10-12：`claude_model_rules` 併入每模型規則表 `services/models/model_rules.py`）
- 決策 6 由 [ADR-0250](0250-touka-leftovers.md) 修訂（2026-10-14：靈魂頁主要模型分「你指定的」與「目前在用的」兩欄；只有人類改了「你指定的」或還沒有主要模型時才換，新值必須在模型清單裡，否則 404）

## 背景

模型層改寫（3a／3b）前要先清掉會讓新模型直接 400、或讓錯誤被吞掉的小問題，並讓自動測試在換 SDK 後仍不可能真的連外。各項彼此獨立，可逐項撤回。

## 決策

1. **自動測試網路封鎖**：`tests/network_guard.py`（純標準庫）以 audit hook 擋下非本機的解析與連線，並記錄違規。conftest 在任何 `src` import 前掛上、設 HF 三個離線旗標；違規（含其他執行緒、被產品吞掉的例外）讓該測試失敗。`enable_smoke_sandbox(..., allow_external_network=False)` 預設封鎖，smoke 結束時有違規即非零結束；兩者的封鎖模式都清掉代理變數並設 `NO_PROXY=*`（也擋 Windows 系統代理），避免經本機轉發代理外連。只有 `tests/smoke/manual/` 與 `scripts/run_scorecard.py`、`scripts/run_tool_eval_episode.py` 可以開放，且都必須先過 `preflight_live_api_cli`；有免同意本機模式的工具，開放值必須正好是 `LIVE_API_APPROVAL_FLAG in sys.argv`（治理測試鎖住）。實施時抓到並改成 mock 的真實外連測試 2 個。
2. **取樣參數一律不送**：Claude、OpenAI 相容（含 Ollama、Grok、DeepSeek、LiteLLM、OpenRouter、自訂連線）、Gemini 三個 adapter 都不送 `temperature`／`top_p`／`top_k`（名稱 SSOT `handlers/adapters/sampling.py`）；Gemini 不再組 `generationConfig`；心跳拿掉寫死的 0.7；ToukaChat `fetchChatStream` 所有分支（含瀏覽器直連與 ollama `options`）也不送。Pydantic 欄位、OpenAPI 與存放中的值都保留，讀取照常、值被忽略。
3. **Claude 思考與強制工具暫行規則**（`handlers/adapters/claude_model_rules.py`，3a 併入每模型規則表）：輸入是已解析的 API 模型 ID。4.7 以後、5.x、Fable／Mythos 與認不出的名稱，`thinking.enabled` 改 adaptive；非 enabled 一律拿掉 `budget_tokens`。只有版本 < 5 且沒開思考才強制 `tool_choice`；Honesty Guard 的矯正提示與閂鎖不變。
4. **Gemini 無痕不建顯式快取**：pipeline ctx `incognito` 為真時，初次與迴圈中重建都不呼叫快取服務；非無痕完全不變。宣稱範圍只是「Touka 不在 Google 端另存自己的 1 小時副本」。
5. **備援只限同一家**：`services/dispatchers/route_family.py` 依 factory 同順序判定家族（factory 的 claude／gemini 兩個判斷式改呼叫同模組只看名稱的 `native_family`）；主要模型失敗時，只有上次能用的模型與它同家族且可判定才改用，否則丟回原錯誤。
6. **沒設主要模型**：`SoulLayer` 不再偷換 `gemini-2.5-flash`，串流前丟 `AGENT_BRAIN_NOT_SET`（id 1215、HTTP 409）。前端 `ToukaApiError` 保留錯誤碼，`useAgentBrainNotice` 跳通知，按鈕切到靈魂設定頁；靈魂設定存檔時，只有代理人還沒有主要模型、且 `route_family` 判得出家族時才把存的 `base_model` 設成主要模型（已有者不變；「未配置」等顯示字串不會被存入），讓通知救得回。
7. **其他寫死預設**：研究工具 Google 搜尋只用代理人自己的 Gemini 模型送一次（不再試舊模型清單、不重試）；`TOUKA_EVAL_JUDGE_ENABLED` 為真但沒設 `TOUKA_EVAL_JUDGE_MODEL` 時不啟用評分，並記錯誤。
8. **穩定性小修**：還原拿掉 30 分鐘硬逾時（逐檔就地覆寫，中途殺掉會半新半舊）；`system_backup.run_backup_job` 讓備份鎖持有到執行緒裡的備份真的結束（四個呼叫點共用）；Windows taskkill 一律移到執行緒，取消時以 `reap_cancelled_process` 有界等待回收；演化 venv 用 `sys.executable`。B8（刪 `_has_chaining_metacharacters` 包裝）由維護者決定這次不做，列為後續小任務；穩定性小修共四個。
9. **uv 7 天冷卻期**：root `[tool.uv]` `exclude-newer = "7 days"`；`uv.lock` 只記期間，`--locked` 不隨日期失效；重鎖時 159 個套件都沒變。只管 Python，npm 不在內。要提早放行某套件只能用 `exclude-newer-package`，每個例外都要維護者逐一同意（`test_uv_dependency_cooldown.py` 鎖住 7 天且不得有例外）。
10. **實測程式**（只改 probe，不改產品）：`live_chat_probe.py` 支援 Gemini 單次請求（probe 行程內 `_MAX_RETRIES=1`、加 `generationConfig.maxOutputTokens`），新增 `--env-file`，路徑納入核准範圍。

## 代價

- 任何地方都不能再調溫度、Top P、Top K，包括 Ollama 等本機模型與 L2 自訂模型；介面拉桿仍在但沒作用（另開任務移除）；ToukaIDE 行內補全的 0.1（`InlineCompletionProvider.ts:76`）被忽略。
- 認不出的 Claude 名稱走 adaptive：只有名稱認不出又剛好是 4.6 以前的舊型號時會 400。
- 沒設主要模型時，只有聊天畫面跳通知；心跳與群聊以錯誤結束；脈絡快照變空、scorecard 回錯誤、SNS 閘道丟例外、經 proxy 的 IDE 請求收到 409。
- 研究 Google 搜尋：代理人用 Pro 級模型時每次較貴；用的是模型原名，對話能用的簡稱（如只寫「gemini」）或內部別名搜尋可能失敗；不再自動換模型。
- 家族判定不出來時（自訂連線停用、registry 不認得），同一家也不備援；評分開了但沒設模型就不啟用（以前用寫死的 haiku）。
- 卡住的還原不會自己結束，只能重啟服務；還原失敗介面沒有提示（既有缺口）。
- 緊急安全更新也要晚 7 天。

## 已知限制

- 網路封鎖管不到：子程序；Windows Proactor 上以 IP 字面值發起的 async 連線；`socket.sendmsg`／`getnameinfo` 事件。
- Gemini 兩個疑似問題（非無痕、快取啟用時）：迴圈重建可能漏舊對話；`cachedContent` 與 `tool_config` 並用可能 400。Gemini 兩項已由 1a 確認，見 ADR-0242（KF-1、KF-2、KF-2d；Owner 已選之後全停顯式快取）。
- Anthropic 形狀入口（如 IDE 的 `/v1/messages`）接非 Claude 模型（KF-9，見 ADR-0242）：OpenAI 相容端 `tool_use`／`tool_result` 原樣外送，list system 被解回 text 區塊、連 `cache_control` 一起外送（降級時才以跳脫中文的 JSON 字串外送）；Gemini 端工具紀錄與 `system_instruction` 都消失。
- 呼叫端在等備份鎖時被取消，排隊中的備份之後仍會執行（失敗會記 warning）；`dispatch_supervisor` 的同步 taskkill 仍在事件迴圈上。
- `list_souls` 對沒設主要模型的代理人顯示「未配置」，SoulStudio 因此不顯示 `base_model`。
- Docker 映像的 uv 0.10.5 能讀新鎖檔是讀原始碼推論，第一次建映像時確認。結論：0.10.5 認得兩個欄位，但在 distroless 映像內無法離線跑 `--locked`、無法判定，已換成 uv 0.12.1，見 ADR-0243。
- 既有、另開任務：研究工具模型為 None 時出錯；指令逾時分支等待子程序沒有上限；OpenAI 相容路徑把 Touka 內部欄位（如 `incognito`）一起送給廠商。

## 驗證

Claude 實測（10/11 經維護者同意，在 317bc304 以 `live_chat_probe.py` 各送 1 次，max_tokens 128、不重試）：claude-opus-5-5、claude-haiku-5-5、claude-haiku-4-5（解析為 claude-haiku-4-5-20251001）都沒有 400，回覆都是 LIVE_PROBE_OK；Opus 5.5 修正前每次都會因溫度被拒，這次直接證明修好。另經維護者逐批同意，gemini-2.5-flash、grok-4.20-0309-non-reasoning、deepseek-flash 各 1 次，都沒有 400／404、回覆都是 LIVE_PROBE_OK；擔心的內部欄位外送沒有造成 Grok、DeepSeek 拒收。OpenAI 未做 live 驗證，只靠契約測試。自動測試結果見 HANDOVER。經 3 位審查者對抗式複審，修正後由原審查者再審差異。

## 回復

不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。改為 revert 產品程式與測試，本 ADR 改「⛔ 已撤回」並在 CHANGELOG 加一列。取樣參數撤回後，存放中的值重新生效（沒有遷移、沒有刪值）；`AGENT_BRAIN_NOT_SET` 留在 `.example` 不影響運作；uv 撤回＝還原 `pyproject.toml` 與 `uv.lock`。
