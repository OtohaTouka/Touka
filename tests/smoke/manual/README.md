# 人工 Live Chat Probe

`live_chat_probe.py` 是人工診斷入口，不是自動 smoke。CI、`guard_all`、pytest 與
`run_all_smoke_tests.py` 均不得執行或自動傳入 `--human-approved-live-api`。

每次執行前都必須由人類針對該次命令明確核准 provider、精確 model 與 token 上限；
先前核准、環境中已有 API key 或 mock 測試失敗都不能沿用為下一次核准。

## 硬限制

- 固定一個合成 prompt，不傳送真實對話或使用者資料。
- 恰好一個 provider request；無工具、無 fallback、無 retry。
- `max_tokens` 預設 64，硬上限 128。
- config、logs 與其他本機 write roots 全部指向程序專屬暫存 sandbox。
- provider/model 不相符、未知路由、回應含錯誤或未精確回覆 `LIVE_PROBE_OK` 時，
  以非零狀態結束。

## Provider 證據邊界

| provider | 狀態 | 零重試依據 |
|---|---|---|
| `openai` / `deepseek` / `grok` | 可人工核准 | 共用 `OpenAIAdapter._post_once`，單次 `httpx` POST，沒有 retry loop |
| `ollama` / `litellm` | 可人工核准 | 同上；即使通常是本機／自架，仍按外部 API 規則核准 |
| `anthropic` | 可人工核准 | probe 建立專用 `AsyncAnthropic(max_retries=0)` client |
| `gemini` | 拒絕 | 生產 adapter 內含 503 retry；目前無可證明的公開零重試 seam |
| 自訂或無法由 provider registry 判定的路由 | 拒絕 | 無法在外連前證明實際 provider 與 retry 行為 |

範例（此文字只是格式，不代表已獲核准）：

```powershell
.\.venv\Scripts\python.exe tests/smoke/manual/live_chat_probe.py `
  --provider openai `
  --model gpt-5.2 `
  --max-tokens 64 `
  --human-approved-live-api
```

若離線／mock 結果顯示可能需要此 probe，先停止並回報精確命令、provider、model、
`max_tokens`、最大 request 數與資料範圍；取得該次人類同意後才可執行。

# 人工記憶評估 Probe

`memory_eval_probe.py` 跑 `tests/memory_eval/` 的情境（合成靈魂、多輪、真 pipeline），同樣不是
自動 smoke，規則同上。差別：

- 只送合成資料（合成人格、事實、對話、注入句）；驅動身分是沙盒裡的合成人類擁有者。
- 請求硬上限 1..40；請求裡的輸出上限 1..1,024，送出前核對 Touka 實際要送的值（Anthropic 照規則表，
  5.5 這類會思考的型號是 8,192；其他廠商照請求值），工具天花板 8,192，超過或缺欄就不送；每個請求送出前
  檢查「已花＋最壞花費」不超過 `--budget`（≤2）；四家 send_round 都先核銷預算；主迴圈只送 1 次；
  摘要員／審查員一律不呼叫（ADR-0268）。
- 任何一輪出錯（含 429／5xx）、最壞花費會超過上限、請求用完或到 USD 軟上限，整批停下並以非零結束。
- 結束時先印報告（每個請求的輸入、快取、輸出含思考 token；回應摘要去敏並截 200 字），再等稽核寫入、
  關紀錄檔、記憶庫、向量庫後刪沙盒；刪不掉印出路徑並以非零結束。
- 嵌入一律用替身編碼器，名稱在建庫前固定成 `touka-eval-stub-ngram`（不看 `.env`），門檻取產品對這個名稱的
  預設值；核准範圍與報告都印這一行（ADR-0280）。
- `--suite continuity`（時間軸）打開活動記錄器，每個情境先在沙盒清掉合成靈魂的事件再預放時間點，整批結束時
  關記錄器並等寫入做完；smoke 不開（ADR-0280）。
- 每一批的精確命令與揭露見 `docs/proxy/live-check-backlog.md` 的 M1／M2 與 E1／E2。

# 人工思考延遲 Probe（r55）

`thinking_latency_probe.py` 量 Claude 5.5 三型號在「少想」與「模型預設」下多久回完、花多少，同樣不是
自動 smoke，規則同上（每批另附精確命令與金額取得人類同意）。差別：

- 直接呼叫產品的 `complete_once`：minimal＝用途 summarizer（用途表少想）、default＝用途 critic（照模型
  預設）；啟動時核對用途表，對不上就不跑。只收 `claude-haiku-5-5`／`claude-sonnet-5-5`／`claude-opus-5-5`，
  路由必須是 Anthropic；`complete_once` 本來就不重試、不換廠商。
- 上限：每組重複 1..5 次、請求數 1..30（型號×強度×次數不得超過）、請求裡的 `max_tokens` 1..1,024
  （Touka 實送值另列在核准範圍：只有 Haiku／Sonnet 5.5 少想時照請求值，其餘 8,192；工具天花板 8,192）、
  `--budget` ≤ 3。每個請求送出前依序核對實送上限、「已花＋最壞花費」（該型號價、Haiku 5.5 取貴檔）、
  核銷請求數；任何一關不過都不送。任何一個請求失敗整批停、非零結束。
- 只送一段固定的合成文字（約 2,000 token）；報告每個請求的實送上限、思考設定、耗時、四欄用量與金額，
  每組的最小／中位數／最大耗時；不印回應原文。先印報告再刪沙盒（和記憶評估 probe 共用釋放流程）。

# 人工嵌入門檻校準 Probe

`embedding_threshold_probe.py` 用合成成對句子（`tests/fixtures/embedding_threshold_pairs.json`）跑真嵌入模型，
量語意查重門檻與查詢下限的分數分布（ADR-0265）。不是自動 smoke，pytest、CI、guard 與
`run_all_smoke_tests.py` 都不跑它。和上面兩支不同，它不呼叫 LLM 廠商、不收費：

- 只連 Hugging Face 下載模型；不載入專案程式、不讀金鑰檔、不送 token。
- 模型釘 commit，只抓 safetensors 一套 9 個檔（約 1.13 GB，上限 1.2 GB）；每次下載要人類同意，
  同意後才帶 `--human-approved-model-download`，沒帶時只讀快取。
- 先把 `HF_HOME`（與 `SENTENCE_TRANSFORMERS_HOME`）指到 `--cache-root`，並設 `HF_HUB_DISABLE_TELEMETRY=1`；
  Hugging Face 會寫入的位置不在 `--cache-root` 底下就不下載。跑完整個刪掉 `--cache-root`。
- 分數照產品的配對方式算（新句子或問題加 `query: `、記憶加 `passage: `）；選法寫在 probe 開頭的說明。
- 量其他模型時改 probe 的 `MODEL_ID`、`MODEL_REVISION`、`MODEL_FILES`；門檻表以 `vector.embedding_family`
  為鍵，新家族要先在那裡加判斷。
