# ADR-0261：記憶長期穩定性評估（不花錢的框架＋人工實測）

- 狀態：✅ 已實作（評估框架與人工 probe；真模型實測每批另經人類同意，尚未執行）
- 日期：2026-10-15
- 決策者：維護者（2026-10-15 核准記憶系統第二批計畫，段 E1）
- 影響範圍：`tests/memory_eval/`、`tests/smoke/manual/memory_eval_probe.py`、`tests/live_api_policy.py`、
  `tests/smoke/manifest.py`、`tests/proxy/upstream/test_long_horizon_prompt_invariants.py`、
  `scripts/run_scorecard.py`、`scripts/run_tool_eval_episode.py`、`docs/proxy/live-check-backlog.md`
- 相關：[ADR-0101](0101-scorecard-production-fidelity.md)（計分卡）、
  [ADR-0214](0214-standalone-smoke-data-and-metered-api-boundaries.md)（計量 API 人工閘）、
  [ADR-0136](0136-review-eval-rails.md)（評估軌）；本 ADR 修訂 ADR-0101 與 ADR-0214 各一點（兩份都已回指）
- 後續：[ADR-0268](0268-memory-eval-thinking-models.md) 修訂決策 5、6——輸出上限改比對 Touka 實際送出值
  （工具天花板 8,192）、送前檢查最壞花費、改名與句尾判定修正、結束前先放開 handle 再刪沙盒

## 背景

記憶相關功能（生命週期、事件時間軸、連續性、摘要時間）要長期穩定，需要能重複量的東西：人格會不會被歷史
內容改掉、跨 session 記不記得、經過時間說得對不對、注入句會不會被照做。既有工具都量不到：計分卡是單輪、
無痕、DebugAgent 沒有靈魂、主要模型未設定；tool_eval 只選工具、沒有記憶工具。

## 決策

1. 不擴充計分卡的 `CORE_SCENARIOS`；新建 `tests/memory_eval/`：情境（純資料）、規則判定（純函式）、
   合成資料、多輪 driver。情境與判定不載入產品程式；送出點可注入，pytest 一律用假串流。
2. 合成靈魂不是 DebugAgent（人格要測靈魂本身），所有寫入只在 pytest 沙盒或人工 probe 的程序專屬暫存沙盒；
   寫入目標必須在沙盒根之下，否則拒絕。嵌入用決定性替身編碼器（字元 1／2-gram 雜湊、L2 正規化），不下載。
3. 驅動身分是沙盒裡的「合成人類擁有者」：Monitor 依前景規則（ADR-0154）代打成合成靈魂，工具、記憶、
   vault 都歸靈魂。若用 DebugAgent 驅動，因為它不是人類，不會代打，工具與記憶會落在 DebugAgent。
4. session 模式設 Auto；審查員模型不設（dangerous 直接拒，不觸發審查呼叫）。
5. 規則判定：含／不含、自稱改名、防洩漏字串與內部 tag、工具軌跡、經過時間（抽數字與單位比區間；
   「月」只收「個月」、不收「日」，避免把日期當時長）、「昨天」。送出失敗的一輪任何規則都不通過。
   風格與自我一致性這類要 LLM 評審的判定不做（只在完整批、另經同意時再議）。
6. 人工 probe `tests/smoke/manual/memory_eval_probe.py` 照 `live_chat_probe.py`：最早期人工閘、
   `--env-file` 綁進核准範圍、沙盒先於任何產品 import；四家 send_round 在外連前先核對送出欄位的輸出上限
   （缺欄或超過就不送），再核銷請求預算；主迴圈 `MAX_SEND_ATTEMPTS=1`；Anthropic `max_retries=0`；
   不讀上一個可用模型；摘要員與審查員的 `complete_once` 換成「不送出、回結構化非成功並記下」；
   任何一輪出錯（含 429／5xx）、請求用完或到 USD 軟上限，整批停下並以非零結束。請求硬上限 1..40、
   輸出上限 1..1,024、USD 軟上限 ≤2。檔名不符合 pytest 與 smoke runner 的自動找檔規則，manifest 列為
   `MANUAL_LIVE`、`metered_api=True`。
7. 組裝不變式（A1）放在上游錄製台：真 SoulLayer＋沙盒 session DB 的家對話與前情摘要＋真 MemoryManager
   查記憶結果當工具結果＋`<continuity>`，錄 Claude／OpenAI 相容／Gemini 三種送出格式，釘五條不變式：
   人格在任何歷史資料之前；注入字串只在界線內、假關閉 tag 被中和；`<continuity>` 只出現一次且在最後一則
   user；有無毒記憶人格區塊逐字相同；Gemini 的人格在 `system_instruction`、查記憶結果在 `functionResponse`
   裡仍被包住。另加「送出的每則訊息只能有該格式允許的鍵」。實作當時 OpenAI 相容格式有兩格不成立，先用
   `xfail(strict=True)` 標出；ADR-0260 修正後兩格轉成 XPASS，已拿掉 xfail，現在三種格式全綠（見「已知缺陷」）。
8. 順手修：計分卡與 tool_eval episode 兩支人工 CLI 揭露寫 `provider_retry=0`，但 ADR-0248 後重試在主
   迴圈（預設 3 次），兩支都沒關。現在裝預算時把 `react_core.MAX_SEND_ATTEMPTS` 設成 1，揭露加
   `main_loop_send_attempts=1`。

## 已知缺陷（已由 ADR-0260 修正）

A1 實作時在 `ToukaProxy/src/services/soul_service.py` 的 `inject_home_messages`（OpenAI 形狀分支）抓到兩件，
都已由 [ADR-0260](0260-home-message-outbound-whitelist.md) 修正，對應的兩格 `xfail(strict=True)` 已拿掉、
改成一般測試：

- 家對話接在整個訊息清單最前面，OpenAI 相容格式的送出順序是 [家對話 user, 家對話 assistant, system 人格＋
  前情摘要, 歷史…, 本輪 user]（Claude 與 Gemini 會把 system 抽出，不受影響）。現在家對話插在開頭的 system 之後。
- 家對話節點的 `id`／`parentId`／`childrenIds`／`timestamp` 原樣進 OpenAI 相容 payload。現在只送角色與內容，
  送出邊界另有逐則白名單。（家對話仍以 dict 放進 OpenAI 形狀請求，所以 Pydantic 的序列化警告還在，但外送的鍵
  只剩 `role`／`content`。）

## 後果

- 真模型實測分批：M1 最小冒煙（四家各一批，smoke 11 情境、預計 20 請求、硬上限 40）、M2 完整（記憶功能
  合併後）；精確揭露在 `docs/proxy/live-check-backlog.md`。方案核准不等於同意執行（禁令 61）。
- 情境只有 smoke 套件；完整套件（多次取樣、真跑 100 輪、換模型、心跳恢復）等記憶功能合併後再加。
- USD 軟上限依串流回報的估計成本、在每輪開始前檢查，單一請求可能略超；硬界是請求數。
- 計分卡與 tool_eval episode CLI 拒絕 Gemini 的說明（「Gemini adapter 目前含內建 retry」）已不準，真正原因
  是兩支 CLI 只包了 Claude／OpenAI 相容的 send_round；本 ADR 沒改，仍維持拒絕。
