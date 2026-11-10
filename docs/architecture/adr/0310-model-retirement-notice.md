# ADR-0310：模型快下架時事先提醒

- 狀態：✅ 已實作。一頁計畫由維護者核准，經三輪審查。
- 日期：2026-11-10
- 決策者：維護者
- 相關：[ADR-0292](0292-hooks.md)（回覆失敗時講原因）、[ADR-0250](0250-touka-leftovers.md)（靈魂的模型名）、`docs/rules/heartbeat.md` §1（Proxy 不得主動排程）
- 影響範圍：`ToukaProxy/src/services/models/model_lifecycle.py`、`lifecycle_alerts.py`、`src/models/model_lifecycle.py`、`src/routers/models.py`、`src/handlers/adapters/gemini_adapter.py`、`src/services/completion.py`、`src/utils/brain_persistence.py`、`ToukaChat/src/hooks/useModelLifecycleAlerts.ts`、`src/components/chat/ModelLifecycleNotice.tsx`

## 背景

廠商停用模型時，以前要等回覆失敗才知道。官方的資料來源不一樣：Anthropic、OpenAI 的模型清單有下架日期；Google 的清單沒有，只有回覆裡的 `modelStatus`；xAI、DeepSeek 查不到。

## 決定

1. **只根據官方明確標示**：欄位讀不到、格式壞掉、模型比對不到都當「不知道」，不提醒。「不在清單裡」不算要下架。不自動換模型。
2. **下架看狀態，不只看日期**：Anthropic 官方說 `deprecated` 模型的日期過了只是逾期，`lifecycle` 才是下架的依據，所以逾期顯示「已過排定的下架日，隨時可能無法使用」，`active` 就算帶日期也不提醒。OpenAI 只有 `shutdown_date`：有日期算已標停用，null 算沒排定，欄位不見或格式壞算不知道。
3. **門檻 30 天，含第 30 天**。已標停用沒有日期的顯示「日期未定」。
4. **被動觸發**：Proxy 不得主動排程，所以啟動時看一次，之後有人開畫面才看要不要查。成功一天查一次，失敗每家隔一小時才再試，不重試，同時只跑一次，都在背景，不擋回應。`MODEL_LIFECYCLE_CHECK_ENABLED=false` 就不對外查；測試與沙盒一律關閉（禁令 61）。
5. **Anthropic 不帶 `lifecycle` 查詢參數**：要列出已下架的模型得帶這個參數，但官方文件沒寫陣列怎麼放進網址（SDK 用 `lifecycle[]=`），真金鑰也只驗過不帶它的請求。送錯會讓這家整個查不到，換來的只是已下架的模型，那些實際呼叫時本來就會失敗並講原因。所以只送驗過的形狀。
6. **Gemini 讀回覆裡的 `modelStatus`**：`LEGACY` 算已標停用，`RETIRED` 算已下架，串流每個 chunk 與非串流都讀。官方沒寫哪些模型會帶，有帶才提醒。
7. **審查員、摘要員走 `complete_once`，沒有畫面可以講原因**，所以廠商回「找不到模型」時記下來提示，同一個模型之後成功就清掉。Gemini 非主迴圈原本不標失敗類型，這裡補標（不動重試）。
8. **誰看得到**：`GET /v1/models/lifecycle-alerts` 需要登入。靈魂的範圍比照 `/stats`（`can_access_session`）。審查員與摘要員的設定只回給人類管理員（`is_admin_human`）；端點本身不能用 `require_admin_human`，那樣一般使用者會收到 403，連自己靈魂的提醒都看不到。自訂連線的模型不提醒。
9. **別名窄規則**：設定寫 `claude-haiku-4-5`、清單只有 `claude-haiku-4-5-20251001` 時，只有一個帶日期的 ID 符合才對應，兩個以上不猜。
10. **畫面放聊天區上方**：右側欄預設關閉，放那裡看不到。換帳號先清空再重問，上一個人的靈魂名字不留到下一個人的畫面。

## 後果

- 看過的日期只在記憶體。下架日過後清單不再列時，提示繼續顯示「已過下架日」；重啟後才消失，那時實際用到會講原因。
- Touka 開著但沒人開畫面時不會查，提醒也只在畫面上。要做到不開畫面也通知，得加一條心跳排程，這次不做。
- OpenAI 的 `shutdown_date` 沒有實測：核對時 `.env` 沒有 OpenAI 金鑰，只照官方文件。欄位和文件不同時讀不到日期，不會誤報，也不會提醒。
- xAI、DeepSeek 沒有清單日期，靠人工巡查。DeepSeek 不存在的模型回 400，不算「找不到模型」。
- 官方沒寫清單查詢會不會收費，不生成內容，每家每天最多一次。
