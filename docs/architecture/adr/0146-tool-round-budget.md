# ADR-0146: 自主行動語意翻轉——移除 nudge 續圈、工具輪保險絲（互動不限／無人保底 50）

## 狀態

✅ 已接受（07-11 認證事故餘波清單 #4-Q1；維護者逐點決定）（實作位置修訂 by [ADR-0156](0156-react-core-extraction.md)：§4 保險絲/優雅著陸原「三 handler 同構」三份實作收斂為單一 react_core；行為決策不變、僅實作拓撲）

> **2026-08-14 修訂（[ADR-0214](0214-standalone-smoke-data-and-metered-api-boundaries.md)）**：
> tool coverage 將舊 `status` 呼叫改為正式 `get_status`，並釘住剩餘工具輪只能由
> `rhythm_service.get_total_rounds()`／`get_used_rounds()` 計算；不改變本 ADR 的工具輪語意。

## 日期

2026-07-12

## 背景

維護者觀察到：每次對話都觸發自主活動，而且 Sumi（grok-4.5）永遠燒滿 5 回合才停。
源碼追蹤確認系統存在**四層迴圈、名詞三重錯位**：

1. ① 心跳外層脈衝（`for i in range(pulse_count)`）——「脈衝次數」旋鈕，正常。
2. ② ReAct 主迴圈（`while True` 工具調用→結果→再調用）——**無旋鈕、無上限、
   裸奔**；成本閘只在純文字分支檢查＝狂調工具的路徑是盲區。
3. ③ nudge 自主續圈——模型回純文字時注入假 user 訊息硬推繼續；
   `allow_react = is_toukasoul or enable_autonomy` 且 `is_toukasoul` 預設 True
   ＝**靈魂的每次普通對話都觸發**；停止＝五個中文詞逐字匹配（nudge 不告知暗號，
   grok-4.5 措辭永遠對不上＝永遠燒滿）。UI 兩處「ReAct」旋鈕（安全設定「單次
   ReAct 最大步數」＋排程「ReAct 回合數 (Slots)」）其實都只管 ③、管不到 ②；
   且 heartbeat/handler 雙重初始化時 handler 以全域預設覆寫排程值＝**排程 Slots
   旋鈕實質失效**。
4. ④ 終端自動修復（指令失敗貼修復提示）——「自動修復嘗試上限」旋鈕，正常。

隱藏 bug 兩枚：`consume_quota` 在每次靈魂互動對話扣每日配額（50/day 被普通聊天
吃掉）、`check_quota` 全系統零呼叫點（配額從未執法）。

## 決策（維護者決定）

1. **③ 直接移除**：純文字＝回合結束（與主流 harness／模型 agentic 訓練慣例一致）；
   停止詞、nudge 提示詞、「自主行動模式」注入全部退役。多步能力零損失——
   工具調用本來就讓 ② 續圈，nudge 只作用在「模型已停下」之後。
2. **「ReAct 上限」自此指 ②**：slots 語意從「系統逼幾輪」翻轉為「工具輪保險絲」
   （一輪＝一次含工具調用的模型回合，不論並行幾顆工具）。修前 ② 無上限；
   本案給它第一道保險絲，同時把成本閘搬到工具分支每輪檢查（堵盲區）。
3. **分頻鐵則**（業界對照：Claude Agent SDK `max_turns` 預設不限、LangGraph
   recursion_limit=25 常被調高、長時程任務數百步是常態；維護者的實際經驗是本 repo 修復
   批次單次百餘步）：
   - **互動（有人在場）預設不限**（`react_loop.max_tool_rounds: 0`）——煞車＝
     人類中斷（每輪檢查）＋HITL 審批卡＋成本閘＋context 極限。
   - **無人 run（heartbeat 及其派生子代理）一律有限**：排程值＞全域保險絲＞
     程式保底 `HEADLESS_FALLBACK_ROUNDS = 50`（**刻意用程式常數**——設定檔
     不可弱化鐵則；全域設 0 也強制退保底）。headless 判準＝`enable_autonomy`
     ctx 旗標沿父鏈繼承（子代理不再硬編 False）。
4. **額度耗盡＝優雅著陸**：該輪工具**零執行**（fail-closed、協定佔位結果回填）
   ＋注入 `WRAP_UP_NOTE`＋以 `tool_choice=none`（anthropic `{"type":"none"}`／
   gemini `mode:NONE`）做**最後一次無工具收尾呼叫**；收尾輪若模型仍吐工具調用
   一律忽略（防收尾迴圈）。三 handler（openai/gemini/claude）同構。
5. **每日配額只計無人 pulse**：heartbeat 每 pulse 扣一次＋主迴圈前置
   `check_quota`/`is_over_budget` 閘（配額首次真正執法）；互動聊天不再扣。
6. **改鍵名＋遷移**：`react_loop.default_max_slots/stop_keywords/nudge_prompt`
   與 root 遺留鍵 → `react_loop.max_tool_rounds`；載入層就地正規化、**刻意不搬
   舊數值**（舊 5＝nudge 輪數，搬值會把互動多步砍到 5 輪）；bootstrap 啟用時
   回寫檔案（測試沙箱不落盤）。UI 標籤同步改「工具迴圈保險絲（0＝不限）」。

## 連帶退役

- **ADR-0135「停止綁看板」駁回機制**：唯一執法點掛在 claude_handler 的 nudge
  分支——隨 nudge 退役（`mission_stop_gate.py` 刪除）。新語意下沒有「宣稱完成」
  可仲裁；看板驅動的續跑改由 heartbeat init_prompt 與 `<harness_context>` 的
  mission 句引導。Phase 2 其餘機制（回報/finalize 綁看板）不受影響。
- **Guard #40 換釘**：舊「refuses to end the conversation」⇄ `board_allows_stop`
  → 新「ends your turn; there is no automatic」⇄ `rhythm_service.consume_round`；
  `<harness_context>` Autonomy 節加回合結束契約句（token 上限紀律：收尾指引由
  執行期 WRAP_UP_NOTE 傳達、不佔本塊）。
- 進度事件 `react_slots`（wire 形狀不變，Guard #38 免動）改記「有限 run 的
  工具輪 current/total」；不限 run 不發。

## 後果

- **正面**：Q1 病根結構性消滅（沒有 nudge 就沒有「停不下來」）；語言/模型無關
  （工具協議取代中文關鍵詞）；② 首次有保險絲＋成本閘全路徑生效；「單次 ReAct
  最大步數」名實相符；排程 Slots 旋鈕從失效變真旋鈕；配額語意翻正且首次執法；
  對話軌不再被「🔄 自主行動模式」假訊息污染。
- **代價／可觀察變化**：心跳「主動性」改靠模型自覺——醒來只回感想（不調工具）
  ＝該 pulse 一輪即止（誠實化；靠 `pulse_count` 與 init_prompt 引導補位）。
  修復迴圈（④）的重試消耗 ② 額度（預算就是預算）。
- **遷移**：舊 `autonomy.json` 自動就地遷移；openapi/api.ts 重生
  （AutonomyConfigModel 形狀變更）。

## 相關

- [ADR-0135](0135-structured-report-finalize-and-board-bound-stop.md)（停止綁看板——本案退役其駁回半邊）
- [ADR-0139](0139-harness-context-block.md)（harness_context——本案更新 Autonomy 節＋換釘）
- [ADR-0143](0143-hitl-unification.md)（HITL——互動煞車的另一半）
- 診斷全文：當時 HANDOVER 的「07-11 事故餘波」節（現在只留在開源前的私人歷史）；業界對照見對話（Claude Agent SDK／
  LangGraph／Anthropic long-running agents）
