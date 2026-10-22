# ADR-0090: Tavily 搜尋調校（深度／結果數／直接答案，env 可配）

**狀態 (Status):** ✅ 已接受
**日期 (Date):** 2026-06-13
**決策者 (Deciders):** 維護者

## 上下文 (Context)

`research` 工具的 Tavily 分支原本只傳 `query`、其餘全用函式庫預設
（`client.search(query=query)`）：`search_depth="basic"`、無 `include_answer`。

兩個品質缺口：
1. **深度**：`basic` 較淺，對需要綜整的問題回得不夠準；Tavily 的 `advanced` 深度明顯更佳。
2. **沒有合成答案**：三家大廠（claude.ai／Gemini／ChatGPT）的網路搜尋都會先給一段
   「AI 總結」。Tavily 的 `include_answer` 正好提供等價能力，但我們沒開。

這是 Web Search 改進 **Phase B（品質）**。緊接 Phase A（ADR-0089，唯讀工具免確認）——
A 讓搜尋能自動跑，B 讓它跑得更好。

## 決策方案 (Decision)

把三個 Tavily 參數提升為 **env SSOT（`src/config.py` Settings）**，由 `research` 讀取後
傳入 `client.search()`，並把回傳的 `answer` 置頂：

- **新增 env（皆有預設）**：
  - `TAVILY_SEARCH_DEPTH = "advanced"`（`basic` | `advanced`）
  - `TAVILY_MAX_RESULTS = 5`
  - `TAVILY_INCLUDE_ANSWER = True`
- **讀取**：`research.py` 的 Tavily 分支於既有 `try:` 內延遲匯入這三個值（與 `TAVILY_API_KEY`
  同處，維持測試可 monkeypatch；非函式體直接子句，故不違反 LOCAL_IMPORT_BAN）。
- **per-call 覆寫**：`kwargs.get("search_depth"/"max_results"/"include_answer")` 優先於 env，
  供 deep research 協調器（ADR-0087）需要時擴大檢索；未帶則用 env 預設。
- **answer 置頂**：若回傳含 `answer`，以 `📝 Answer: …` 置於逐條結果之前；「無 results」的判斷
  改為「無 results 且無 answer」，避免只有摘要時誤報無結果。

## 後果與影響 (Consequences)

- 搜尋結果更準、且每次先給一段可直接引用的合成答案，體驗對齊大廠。
- **取捨（成本）**：`advanced` 每次搜尋耗 **2 Tavily credits**（`basic` 為 1）。因 ADR-0089
  已讓 `research` 在所有 auth_level 自動執行、**無逐次確認閘門**，此 2x 倍率即套用到「AI 主動
  發起、使用者未逐次核准」的搜尋——多跳 agent 迴圈會以 **2x basic 的速率**消耗額度。成本敏感
  或低信任等級的佈署，可在 Phase C 的免金鑰 fallback 落地前先設 `TAVILY_SEARCH_DEPTH=basic`。
- **取捨（隱私）**：`include_answer=True` 會把查詢轉給 Tavily 伺服器端 LLM 做合成（**不額外
  收費**；加價的是 `include_answer="advanced"` 字串，本系統不送）。相對基礎搜尋的邊際隱私差異
  僅為「同一查詢多經一層下游 LLM 處理」，不外送查詢以外的資料。
- **無 schema／端點變動**：只動 env 與工具內部，免 openapi/api.ts 重生；guard 仍 32 道。
- 既有部署無需改檔即得新預設（env 有 fallback 預設值）；要調整再於 `.env` 覆寫。

## Action Items
1. [x] `config.py`：新增 `TAVILY_SEARCH_DEPTH`／`TAVILY_MAX_RESULTS`／`TAVILY_INCLUDE_ANSWER`（Settings + legacy 匯出）。
2. [x] `research.py`：Tavily 分支讀 env（可被 kwargs 覆寫）傳入 `client.search()`；`answer` 置頂；無結果判斷納入 answer。
3. [x] `.env.example`：文件化三個調校變數與 credit 成本提示。
4. [x] TDD：`test_research_tavily.py`（env 驅動、kwargs 覆寫、answer 置頂、僅 answer 不誤報）。
5. [x] ADR-0090 ＋ `adr/README`；`README_HANDOVER`／`CHANGELOG`／`tests/README` 同步；guard 32/32 ＋ 全量測試綠。
