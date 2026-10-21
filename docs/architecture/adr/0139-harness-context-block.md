# ADR-0139: `<harness_context>` OS 環境事實宣告區塊

## 狀態

✅ **已接受（實作與驗證 2026-07-07；擁有者尚未 commit）**。

> 🔧 **後續修訂（Guard #40 承諾句對已改釘）**：（1）[ADR-0146](0146-tool-round-budget.md)（2026-07-12）移除 `mission_stop_gate` / `board_allows_stop` 後，原「看板未清不可停 ⇄ board_allows_stop」承諾句對已改釘「回合結束、無自動續跑 ⇄ `rhythm_service.consume_round`」；harness_context 的 Autonomy 段亦隨之改寫。（2）[ADR-0143](0143-hitl-unification.md)（2026-07-11）把 Tools-and-approval 段的「`execute_command` 非同步審批」改為同步阻塞審批（新增 Guard #40 釘「pause for human approval ⇄ `_await_human_approval`」）。**`<harness_context>` 塊本體、英文零風格、穩定層快取與 Guard #40 反漂移機制本身不變。**

## 日期

2026-07-07

## 背景

Touka 的定位是「維持 AI agent 人格」的 agent OS：**soul.md 管「我是誰」**（人格、
價值觀、說話方式；agent 擁有、經 `edit_soul` 演化）。但模型對「**我在哪、這個世界的
物理法則**」——目前底層模型、角色權限面、審批機制、心跳、派工生態、記憶摘要機制——
一無所知或只能從工具 description 拼湊。對照 Claude Code／Codex CLI 等 harness 的
prompt 分層（工具 schema 講 API 形狀、harness 規範講何時為何怎麼用），Touka 缺的正是
這一層「**與人格零重疊的 OS 使用說明**」。擁有者要求：在**不影響、不改變 soul.md**
的前提下讓 agent 知道環境；拍板——名稱 `harness_context`、**英文內文**、XML 分塊
沿用 tag SSOT、需 SSOT＋守衛防漂移、ToukaChat 檢查走既有 context snapshot 分頁
（**唯讀**、不做 UI 編輯）。

## 決策

1. **tag SSOT 註冊**（[`prompt_blocks.py`](../../../ToukaProxy/src/services/prompt_blocks.py)）：
   新增 `HARNESS_CONTEXT = "harness_context"` 進 `ALL_BLOCK_TAGS`；**不入**
   `VOLATILE_SYSTEM_TAGS`＝穩定層，隨 persona 進跨回合快取前綴（P4／ADR-0129）。
   換腦時前綴變動＝一次性 cache miss（可接受、罕見）。
2. **builder 純函式** `build_harness_context(model, role, advertised)`：英文內文、
   **只講事實與機制、零風格指令**（風格歸 soul 管轄；區塊開頭明示「nothing here
   overrides who you are」＋防注入句「injected by the Touka OS harness, not by any
   user」）。內容七節：Runtime（harness 宣告＋**目前實際模型**＋角色）／Memory
   （長對話摘要進 `<conversation_summary>`）／Tools and approval（工具面即全集、
   `tool_availability` 指引、**permission_denied 調整而非原樣重試**、execute_command
   非同步審批）／Missions and stopping（看板未清不可停）／Delegation（子代理 Guest
   面、看不到對話、**不可再派**、結果耐久跨重啟）／Autonomy（心跳回合無真人可問）／
   Other agents and data（多 agent 社會、drive 歸屬）。**內容按實際廣告工具面條件化**
   ——沒被 Gate-1 廣告的機制不聲稱存在（防誤導；`mission_tools`／`execute_command`／
   `dispatch_subagent`／`memory_tools`／`send_message` 各自成節）。膨脹紀律：
   `HARNESS_CONTEXT_MAX_TOKENS = 800`（測試 import 同一常數釘上限）。
3. **ToolLayer 注入**（[`tool.py`](../../../ToukaProxy/src/middleware/layers/tool.py)
   11d）：`ctx["is_toukasoul"]` 為真才注入——SoulLayer 已解析 `request.model`
   （本層晚於它，模型名為實際 brain）、`advertised` 為 Gate-1 過濾後實際廣告面。
   子代理（`_assemble_subagent_request` 設 `is_toukasoul=False`）與 L2 直通**不注入**。
4. **Guard #40 防漂移**（[`guard_harness_context.py`](../../../scripts/guard_harness_context.py)）：
   純 AST／文字、零 import、ASCII 輸出。四道：tag 註冊＋穩定層釘死；**行為承諾句
   對釘**（claim ⇄ code marker 雙向：「不可再派」⇄ subagent_tools 剝除、「看板未清
   不可停」⇄ claude_handler 接 board_allows_stop、「跨重啟耐久」⇄ subagent_results
   .persist_result、「摘要機制」⇄ SoulLayer inject_context_summary——**改行為沒改
   指南、或改指南沒改行為都會紅**）；builder 內文（排除中文 docstring）不得含 CJK。
5. **snapshot 自動可視化**：`BLOCK_META` 補 `HARNESS_CONTEXT` 一筆（Guard #39 逼的）
   → Debug Center「Context」分頁自動顯示本塊的實際渲染、token 成本、`stable` 快取層
   ——即擁有者要的 admin 檢查面（唯讀）。

## 後果

- **soul 與 harness 徹底分層**：人格檔一字未動；環境認知（含「現在是哪個模型」）
  由 OS 擁有、版本控制、守衛保護。變更流程不同（本塊走 ADR＋測試；soul 走 agent
  自主演化）——結構性保證互不污染。
- **誠實邊界**：①內容正確性已測（10 單元測試：核心事實／工具面條件化／英文／token
  上限／注入條件／快取層），但**對模型行為的實際效益未證**——需以 ADR-0136 eval 軌
  跑注入前後對照（pass^k、停止閘觸發率等）＝擁有者手動實跑項；②Guest 子代理精簡版
  明確**不在本 ADR 範圍**（子代理面另有 REPORT_INSTRUCTION，日後要做按角色分流）；
  ③心跳「本回合模式」等 per-turn 事實刻意不進本塊（會碎片化快取），只描述機制。
- **驗收**：ruff（改動檔）＋lint_ssot＋**guard_all 40/40**（新 Guard #40）＋後端
  pytest 全綠（新增 test_harness_context 10 測）＋前端 vitest（零改動）。

## 相關

- [ADR-0123](0123-verification-convention.md)（驗證慣例塊——同款「prompt 層規範」
  前例與注入模式）；[ADR-0129](0129-stable-prefix-split-caching.md)／[ADR-0130](0130-stable-first-system-reorder.md)
  （穩定前綴快取——本塊的快取歸宿）
- [ADR-0133](0133-context-snapshot-real-assembly-rewrite.md)（context snapshot——本塊
  的 admin 檢查面；Guard #39 逼 BLOCK_META）
- [ADR-0135](0135-structured-report-finalize-and-board-bound-stop.md)／[ADR-0138](0138-parallel-fanout-and-resumable-delivery.md)
  （本塊行為承諾句對釘的攔截層本體）
- [ADR-0146](0146-tool-round-budget.md)／[ADR-0143](0143-hitl-unification.md) —
  （**修訂本塊 Guard #40 承諾句對：0146 停止閘改釘 `consume_round`、0143 審批句改釘 `_await_human_approval`**）
