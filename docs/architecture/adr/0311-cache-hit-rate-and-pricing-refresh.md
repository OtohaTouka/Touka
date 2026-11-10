# ADR-0311：快取命中率與價目表更新

- 狀態：✅ 已實作。一頁計畫由維護者核准。
- 日期：2026-11-10
- 決策者：維護者
- 相關：[ADR-0244](0244-model-layer-stage-1b.md)（四欄用量）、[ADR-0271](0271-claude-5-5-thinking-by-purpose.md)（分檔計價）、[ADR-0310](0310-model-retirement-notice.md)（在用的模型）
- 影響範圍：`ToukaProxy/src/services/telemetry.py`、`src/services/providers/pricing.py`、`src/services/models/pricing_status.py`、`src/services/models/lifecycle_alerts.py`、`src/routers/stats.py`、`src/handlers/adapters/responses_adapter.py`、`ToukaChat/src/hooks/useSystemMonitoring.ts`、`src/components/sidebar/monitoring/CacheHitRates.tsx`、`PricingNote.tsx`

## 背景

後端只有累計用量，看不出快取命中率最近有沒有掉。價目表是手寫的，已經過時：Gemini 3 以後、GPT-5 以後、Grok 4.3 以後都沒有自己的價，落到家族價或預設價，有些被低估，費用上限因此不夠保守。

## 決定

1. **每個用途每天記一份**：`token_usage.json` 每個鍵底下多一個 `daily`，鍵是 UTC 的日序號，值是四欄用量，保留 14 天。累計數字與成本不動，舊檔照常讀。`daily` 壞掉只重建，不能擋住累計，否則預算上限看不到花費。
2. **命中率**＝快取讀 ÷（未快取輸入＋快取讀＋快取寫），範圍是今天與前 6 天，由後端算好放在 `/stats` 的 `cache_hit_rates`，範圍和 `token_usage` 相同。這段時間沒有輸入回 `null`，畫面顯示「—」，和 0% 分開。
3. **鍵不是廠商**：可能是廠商標籤、旁路用途（`summarizer` 等）或自訂連線名稱。畫面照鍵分列，已知的用途給好懂的名字。
4. **價目表繼續手寫，標查證日期**：不接社群價格檔。費用上限靠這個價格，社群資料錯了上限就失效。`PRICING_VERIFIED_AT` 顯示在系統監控；`model_pricing.json` 有覆蓋時說明日期不含那些。
5. **沒有具體價格就用該家已知最高價，寧高勿低**：只比對到家族鍵（`claude`、`claude-fable`、`claude-mythos`、`deepseek`、`gemini`、`gpt`、`grok`）時，`resolve` 回 `family`，稽核帳本的 `rate_source` 分得出來。家族價一定不低於表上同家任何型號（含長檔、已公告的漲價與快取讀寫價），由測試釘住。`gpt-oss` 是開放權重模型，沒有官方價，看代管的地方，一樣標 `family`，但維持保守預設，不算 OpenAI 的價。
6. **在用的模型沒有具體價格就列出來**（`family` 與 `fallback` 都算）：範圍和模型下架提醒相同（靈魂比照 `/stats`，審查員與摘要員只給人類 admin，自訂連線不列）。
7. **`gpt-5` 只比完整名稱，表上拿掉 `grok-4`**：子字串比對會把 `gpt-5.7`、`grok-4.8` 這類沒列的型號算成舊的便宜價，而且標成有具體價格。使用者自己寫的覆蓋照舊最長子字串。
8. **已公告的漲價到日子才換**（`_SCHEDULED_PRICING`）：`gemini-4-argon` 12/01、`gemini-3.8-flash` 與 `gemini-3.6-flash` 2027-01-01。官方沒寫時區，取 UTC 當天 0 點。
9. **分檔照官方**：OpenAI 超過 27.2 萬、Google 超過 20 萬、xAI 達到 20 萬（差一個 token，門檻寫 199,999）。DeepSeek 有尖峰與離峰，一律用尖峰價。
10. **OpenAI 的快取寫入照官方計**：GPT-5.6 起預設 30 分鐘的寫入是輸入價的 1.25 倍，表上有這一欄，送前最壞估算才會取較貴的價。Responses 的 `input_tokens` 含快取讀與寫，`cached_tokens` 不含寫，所以 `cache_write_tokens` 也要從未快取輸入拆出來；沒帶這個欄位就當 0，和以前一樣。
11. **Mythos Preview 不在價格頁**：價格取自 `anthropic.com/glasswing` 的 $25/$125，快取價官方沒寫，照一般的 0.1 倍、1.25 倍。`claude`、`claude-mythos` 家族價因此是這個價。

## 後果

- 歷史累計不回溯重算，只有升級後的用量用新價。沒有 `daily` 的舊資料顯示「—」，有新用量後才有數字。
- 只比完整名稱的 `gpt-5` 帶日期的版本（`gpt-5-2025-08-07`）會落到家族最高價，高估但會列出來。
- `gpt-5.6-sol` 目前是優惠價，到 2026-11-30，官方沒寫之後多少，要靠巡查跟上。
- `gemini-2.5-pro` 12/01 起官方會轉到 `gemini-3.8-flash` 並照它計價，表上留原價，這段會高估；`gemini-2.5-flash` 轉過去的價格相同。
- 官方已不列的舊型號留著舊價，家族最高價把它們算在內。
- `cache_write_tokens` 只有官方文件，沒有用真金鑰核對。官方沒寫 Chat Completions 有沒有這個欄位，走那條路的 GPT-5.6 以後型號，寫入照輸入價算，最多少算未快取輸入的 25%。
- 只涵蓋文字模型。圖片、語音型號（例如 `gemini-3.1-flash-lite-image`）靠子字串落到文字價，可能偏低。
- 名字含 `gpt` 但不是官方型號的，會落到 OpenAI 家族最高價並列出來；自架模型可以在 `model_pricing.json` 標價。
- 人工探測工具（`memory_eval_probe.py`）對沒列的 `gpt-*` 以前會拒跑，現在算 `family_estimate` 照跑，最壞估價用家族貴檔，不會低估。ADR-0284「查不到就拒跑」的範圍因此窄了一點。
- 沒有人開畫面時不看命中率，掉了也不會自動告警（一頁計畫的「不做」）。

## 回復方式

撤掉本 ADR 對應的 commit。`token_usage.json` 裡多出來的 `daily` 沒有人讀，留著無害；稽核帳本已寫的 `family` 只是文字。
