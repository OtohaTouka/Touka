# ADR-0214：Standalone smoke 資料隔離與計量 API 人工閘

- 狀態：✅ 已實作；經維護者人工審查後提交為 `a8f63e33`
- 日期：2026-08-14
- 決策者：維護者（計畫與成果都由維護者親自審查）
- 影響範圍：`tests/smoke/`、manual live probes、tool eval／scorecard CLI、
  ToukaMemory app-data resolver／vector lifecycle、Memory gateway、測試與安全文件
- 修訂：[ADR-0006](0006-unified-testing-suite.md)、
  [ADR-0073](0073-smoke-isolation-and-id-anonymization-hardening.md)、
  [ADR-0101](0101-scorecard-production-fidelity.md)、
  [ADR-0103](0103-live-smoke-sandbox-anti-deadlock.md)、
  [ADR-0136](0136-review-eval-rails.md)、
  [ADR-0209](0209-hermetic-test-config-hydration.md)
- 2026-10-15 修訂（[ADR-0261](0261-memory-long-horizon-eval.md)）：ADR-0248 後重試在主迴圈，計分卡與 tool_eval episode CLI 原本的 `provider_retry=0` 不成立；兩支現在都把主迴圈設成只送 1 次並揭露 `main_loop_send_attempts=1`。第 5 點的人工入口新增 `tests/smoke/manual/memory_eval_probe.py`（請求 1..40、輸出 1..1,024、USD 軟上限 ≤2、零重試／備援、摘要員與審查員不呼叫）。

## 背景

P11-B 全系統 smoke 驗證發現 24 支 `smoke_*.py` 只有 4 支在產品 import 前啟用
sandbox。`smoke_memory_tools.py` 已把 app-data 解析到實體 DebugAgent 目錄並寫入
`smoke_test.txt`；Memory／Qdrant 初始化與 mission 寫入的完成程度未知。代理人依
`DATA_PROTECTION` 停止，未檢查、修改或清理 `data/`。

這不是 P11-B regression，而是既有 smoke governance 缺口：master runner 只繼承 parent env，
表面上的 read 亦可能在 constructor 中建目錄、SQLite schema／index、Qdrant store 或執行 legacy
migration；`smoke_chat.py` 更被裸 `smoke_*.py` glob 自動納入，會呼叫真實 provider、消耗額度，
且吞掉例外後仍可能 exit 0 假綠。

維護者決定測試可以讀取實體資料，人工 live probe 亦可寫精確 DebugAgent 邏輯範圍；但任何自動化
寫入都必須進 sandbox／mock，非 DebugAgent／全域寫入永遠不得套用例外。由於目前 read path
可能隱含 migration／index／lock 等寫入，本批不嘗試 selective live-read，而讓全部自動 smoke
採更嚴格、可證明的全 sandbox。

## 決策

1. `tests/smoke/manifest.py` 是 smoke 執行分類的機器可讀 SSOT。每一項明列 path、
   execution class、data access、是否計量與 provider surface；所有 top-level `smoke_*.py` 必須
   恰好分類為 `AUTOMATED_SANDBOXED`。Manual live probe 亦須分類，但永遠不進自動執行集合。
   Runner 不再以裸 glob 決定要執行的腳本。
2. 全部 24 支自動 smoke 使用 runner＋standalone 雙保護：runner 為每個 child 建立唯一 temp
   sandbox；腳本在任何 `src`／`memory_server` import 前呼叫 `enable_smoke_sandbox()`，驗證或重用
   runner root。`MEMORY_APP_DATA_DIR`、`SANCTUARY_DATA_PATH`、`TOUKA_CONFIG_DIR`、
   `TOUKA_ENV_FILE` 全錨在該 root，設定只從 tracked examples hydration；cleanup 失敗必須使該
   smoke fail-loud。
3. 自動 child 不載入 repo／Proxy 真實 `.env`，移除 credential-shaped 環境變數，並
   停用 bootstrap、dynamic model discovery、dispatch autospawn、Creation ModelJudge。模型／embedding
   依賴使用 deterministic fake；離線環境旗標是第二道防線，冷 cache 不得觸發下載。
   這裡的 sandbox／offline 精確指：已知本機 write roots 錨定逐程序 OS temp、provider 路徑使用
   deterministic mock、credential-shaped 環境變數被移除，並設定 HuggingFace／Transformers offline
   flags。Child 仍在一般 OS 網路命名空間內執行，且保留非憑證型 host environment；這不是
   OS-level egress jail，也不是「所有秘密皆已清除」的證明。
4. 自動 `smoke_chat.py` 保留完整 pipeline contract，但 provider dispatch 改為 deterministic mock；
   真實 chat connectivity 拆至 `tests/smoke/manual/live_chat_probe.py`。人工 probe 的 model 必填、
   provider request 固定一個、`max_tokens` 有界、retry／fallback 固定為零、本機寫入仍進 temp
   sandbox，任何失敗以非零狀態結束。
5. 下列入口會或可能觸發 provider／搜尋服務，永久排除所有 automation：
   - `tests/smoke/manual/live_chat_probe.py`：真實模型 token／額度；
   - `scripts/run_scorecard.py`：每個 scenario 呼叫真模型；程序內 provider request hard budget 為
     1..64、零 retry／fallback、Gemini fail-closed，並拒絕 `web_search`／`docgen_xlsx`。USD budget
     仍是 scenario 間 soft cap，不能取代 request hard bound；所有本機寫入進 temp sandbox；
   - Debug Center scorecard：既有人工產品入口，正常 UI 有費用警示與 `confirmToast`；但後端
     `POST /v1/debug/scorecard/run` 目前只有角色閘，沒有 ADR-0214 approval capability、provider request
     hard budget 或 retry／fallback 強制。政策 registry 對它只做分類，不構成後端執法；
   - `scripts/record_tool_eval_baseline.py --model ...`：固定 12 cases，samples／turns 各自硬上限 4，
     最多 48 個 provider requests；每 request `max_tokens` 預設 128、硬上限 512，結果只寫 OS temp；
     `--list-models` 通常不耗 token，但仍是外部 provider API／quota；只有 `--dry-run` 是本機零 API；
   - `scripts/run_tool_eval_episode.py`：單一 case；requests 預設 3／硬上限 16、每輪 tokens 預設
     128／硬上限 512、wall timeout 預設 180／硬上限 600 秒；Gemini、`research-auto`、非 DebugAgent
     皆 fail-closed，所有工具與 artifact 寫入進 temp sandbox；
   - Creation `ModelJudge`：`TOUKA_EVAL_JUDGE_ENABLED=true` 與 `eval_scoring=True` 同時成立時會呼叫
     真模型；自動環境固定關閉。
   `scripts/measure_tool_token_cost.py` 只做本機 schema token estimate，不消耗 provider 額度。
6. 每次 live 執行前都必須取得該 invocation 或明列 bounded batch 的人類同意。代理人須先揭露
   exact command、script、provider、model、case／scenario／retry／最大 request 數、budget 或 token
   上限、送出內容類型與本機寫入範圍。方案核准、已有 API key、FullYOLO、錯誤發生、同一 thread
   先前同意都不是本次執行授權；未知是否計量時按計量處理。共同 CLI policy 以顯式
   `--human-approved-live-api` fail-closed；CLI 取得的 `LiveApiApproval` 必須與實際 provider、model、
   token/request scope 精確相符，transport 在每次 request 前核銷 budget，且不得自動 fallback／retry
   至未核准範圍。這是程序內防誤用 capability，不是身分認證或不可偽造的人類意志證明；Debug
   Center 的既有 endpoint 尚未消費此 capability。
7. 自動報告對 manual／metered 項目只能顯示
   `NOT RUN — HUMAN APPROVAL REQUIRED`；不得算 PASS，也不得因此把自動 suite 算 FAIL。Mock 無法
   判定 provider auth／connectivity／routing 時，正確處置是停下揭露並請求人類同意，不是自動升級。
8. 本事故同批修正一組使 sandbox smoke 無法可信驗證的窄缺陷：
   - Memory 端 Docker app-data resolver 對齊 ADR-0209：顯式 absolute override 保留，只有 relative
     值才回 `/app/data`；
   - `ToukaProxy/src/tools/execution_context.py` 是 ToolExecutor execution metadata key／builder SSOT；
     `identity` 不屬於該集合。Memory gateway 不得把 `_ctx`、
     `_interactive`、`_hitl_preapproved` 等傳入嚴格 Memory dispatcher；
   - `stop_command`、`dispatch_subagent`、`dispatch_subagent_group` 在 tool coverage 只驗 wiring，
     不於 smoke 真執行；
   - `VectorStore` 提供明確、冪等、清 cache 且錯誤可見的 process/test shutdown API，由 memory smoke
     `finally` 呼叫。不得把 close 接入 `MemoryManager.reset_singletons()`，因 production embedding
     model switch 會呼叫該方法，無排空／鎖定設計時關共享 client 會引入競態；
   - `FactualStore` 的 SQLite 操作統一經 transaction／close context；`close_all_connections()` 嘗試
     關閉全部 active handles，失敗 handle 重新登記供下一次 cleanup 重試，並拋出第一個錯誤。這是
     process／test shutdown seam，不宣稱 production 跨執行緒排空；
   - `forget_memory()` 與 duplicate diagnostics 對齊同步的 `MemoryManager.resolve_duplicate()` 契約，
     移除錯誤 `await`；
   - tool coverage 使用合法 `agent_control(action="get_status")`，domain-level `Unknown action/tool` 固定
     判 FAIL；`get_status` 依 ADR-0146 從 `rhythm_service` 計算剩餘工具輪，不再呼叫不存在的 slots API。
9. 本批不新增或修改 FastAPI request／response model、OpenAPI、ToukaChat contract、production
   provider retry policy或 app lifespan；也不執行任何 live／計量 API 驗證。

## 資料邊界

- 開發者與代理人仍不得直接瀏覽、清理或管理 `data/`。受管測試若要讀實體資料，必須證明完整
  執行路徑物理唯讀；constructor、migration、schema／index、cache、lock、log 都算寫入。
- 人工 probe 的 live write 只允許 `AgentIdentity("DebugAgent")` jail 專屬路徑、強制
  `agent_id="DebugAgent"` predicate 的共享 row，以及精確 `memory_DebugAgent` collection。
  全域 config／setting、schema／migration、registry、其他 Agent 與任何 `soul/` 寫入都不屬例外。
- 本批所有自動 smoke 與新 manual chat probe 實際上都採 temp sandbox；政策允許的 DebugAgent live
  write 不是本批自動驗證的依賴。
- Provider prompt／schema／tool result 會離開本機；每次核准揭露必須描述資料類型，且禁止傳送
  secret、Bearer、未必要的真實記憶或其他 Agent 資料。

## 後果

- 自動 smoke 由「依賴當前 host env」變成可重現、離線、逐程序隔離的契約測試；direct execution
  與 master runner 都不再有漏用 sandbox 的路徑。
- Provider connectivity 不再冒充自動 smoke 穩定性；mock Green 只證 pipeline contract，manual
  live probe 的 `NOT RUN` 誠實保留 deployment／credential residual。
- Manifest 與 CLI policy 分別成為執行分類與人工核准的 SSOT，新增測試時不再靠檔名慣例或文件
  記憶辨識費用風險。
- 全 sandbox 比維護者允許的 live-read 更保守；若日後需要 production-fidelity read probe，必須
  另案提供真正 read-only connection／preinitialized store 的機械證據。

## 驗證狀態

整合 focused 為 **122 passed**，Guard #1 窄修後的最終 focused 為 **103 passed**；最新自動
smoke 為 **24/24 Green**。Manual live probe 明列
**NOT RUN — HUMAN APPROVAL REQUIRED**。完整 Python 3.12 tests 以一個隨機順序及固定 seed 1、2、3
各完成一次，四次皆為 **4855 passed／7 skipped**（warnings 依序為 1／1／3／1）。此前另有一次開啟
逐案 INFO log 的 full run 非零，但輸出截斷未留下 failure summary，後續四次未重現；此 residual
不冒充 Green 或已定位問題。本機 Guard #1–21、#24–44 為 **42/42 Green**；外部 Guard #22
pip-audit／#23 npm audit 均為 **NOT RUN**。所有計量／外部 API probe 固定 **NOT RUN**，不能列入
Green 數字。P11-B／ADR-0214 經維護者人工審查，精確 95-path candidate 提交為
`a8f63e33e4dde352aa6dd686d002cf4cf53a890e`。逐程序 temp write-root、mock／offline 與 credential
scrub 不等於 OS-level egress jail；Debug Center backend gate 與不可重現 INFO-heavy run 仍保留為
後續 residual。

## 回滾

可一起移除 manifest、runner／script sandbox 強制、mock chat、manual probe、live API policy、窄
production repairs、focused tests 與本 ADR／文件索引。回滾不得恢復裸 glob 自動執行真 provider、
載入真 `.env` 的 automatic smoke、吞例外假綠、寫入實體非 DebugAgent 資料，或以「先跑再問」取代
逐次人類同意。

> **補記（2026-10-11，[ADR-0241](0241-model-layer-stage-0.md)）**：pytest 與自動 smoke 另加外部網路封鎖（`tests/network_guard.py`）；上文「不等於 OS-level egress jail」仍成立（子程序不在封鎖內）。
