# ADR-0120: 指令執行非同步審批（bash → execute_command Async Approval）

## 狀態

✅ **已接受（實作完成、全綠——guard_all 37/37、後端 pytest 2600 passed/4 skipped）**。本 ADR 落地 [ADR-0119](0119-unified-tool-interface-redesign.md) 的刻意遞延項之一，延續其雙軌 shim 原則與 Tier 1 結構化錯誤（禁令 59）。

> 🗑️ **非同步審批機制已被 [ADR-0143](0143-hitl-unification.md)（HITL 統一，2026-07-11）反轉（含行為回退）**：互動路徑改由統一聊天審批 broker（`tool_executor._await_human_approval`，Gate 2）在 `execute_command` 被呼叫**之前**同步阻塞等待審批（最多 `approval_service.DEFAULT_TIMEOUT_S=180s`），通過後才以 `_hitl_preapproved=True` 執行；terminal 自帶的 `approval_pending → get_command_result` 流在正常互動下不再被觸及（headless 因前端不訂閱 `/terminal/stream` 而 fast-fail）。**故本 ADR 的兩項核心後果——「不阻塞 ReAct 迴圈」與「前端零改動」——已不成立**（0143 新增 `ToolApprovalCard.tsx` 及相關前端接線）。`execute_command` / `get_command_result` / 背景執行（`background=true`）工具面本身仍有效。

## 日期

2026-07-04

## 背景

`bash` 的危險指令（政策 CONFIRM）走**同步 HITL**：工具呼叫在 `request_approval` 上阻塞——每次等待 60 秒、最多 3 次＝**ReAct 迴圈卡死最多 180 秒**，模型什麼都不能做，只能等人按核准。提案（工具介面重設計 §7）定案改為「非同步審批：`approval_pending` 讓模型可繼續而非卡死」。

## 決策

1. **`execute_command`（正名，廣告）**：安全指令（ALLOW）行為不變——立即執行、直接回結果；危險指令（CONFIRM）**立即回傳**結構化 `{"status": "approval_pending", "execution_id": ..., "next_step": ...}`，審批等待與核准後執行移入 `asyncio` 背景任務。**審批機制本體不變**：沿用既有 `request_approval`（60s×3）、`/terminal/approve|reject` 端點、`bash_approval_request`／`bash_result` SSE 事件——**前端零改動**（演化管線等其他 `request_approval` 消費者亦不受影響）。
2. **`get_command_result(execution_id, wait_seconds=0)`**：取回非同步結果——待核准＝`approval_pending`、執行中＝`running`、完成＝原 markdown 包裝輸出、拒絕／逾時＝結構化 `permission_denied`。`wait_seconds` 有界等待（上限 60 秒、預設 0）避免又變回阻塞工具。**結果依 agent 隔離**：execution_id 綁定發起者 `agent_id`，跨身分查詢一律回 `resource_not_found`（不洩漏存在性）。結果存放區為行程內、上限 100 筆 FIFO、重啟即清。
3. **執行核心抽取 `_execute_core`**：子進程執行（乾淨環境、行程群組連坐殺、30s 逾時、live 串流）單一實作，inline 與背景路徑共用（零重複）。背景路徑不用 per-request 串流（emit=None），結果經 Terminal SSE 面板呈現。
4. **Tier 1 一致化**：BLOCK 與「無 UI 相容模式」由自由文字（`❌ 執行攔截`／`🚫 相容模式`）改為結構化 `permission_denied`（附 reason 與可行建議）。
5. **`tool_errors.is_structured_payload`**：時間尾綴注入的跳過條件由「僅錯誤 JSON」擴為「錯誤或狀態 JSON」（`{"error_class"` 或 `{"status"` 前綴）——`approval_pending`／`running` 回覆保持可解析。
6. **雙軌**：`bash` 保留為 shim（可執行、不再廣告、委派 `execute_command`——舊名同享非同步語義、零邏輯重複）。`CRITICAL_TOOLS` 納入三名（空參即拒）。

## 後果

- 危險指令不再凍結對話：模型收到 pending 後可先做其他工作，之後以 `get_command_result` 收割；使用者在 ToukaUI 的核准流程與畫面完全不變。
- **語義變更（接受）**：舊 `bash` 呼叫者在 CONFIRM 情境從「阻塞後拿到結果」變成「先拿 pending、需再取結果」——結構化 payload 附 `next_step` 指引，模型可自我修正；ALLOW 情境完全不變。
- eval（select-only、8 案例）：grok-4.3 維持 **100%（選錯 0%）**——廣告面（28→29）改動零回歸；deepseek-v4-flash（n=3）66.7%——歸因＝慢性 `mission-initialize` ＋ notes.txt 案例穩定改採「先 list 後寫」的**多步規劃**（全為無害唯讀探索、幻覺/無效參數 0%）。後者暴露 **select-only 單回合計分的結構限制**（合理的多步策略被記選錯）——多回合 eval 列為後續工作，不為分數改案例。
- 重啟遺失 pending 結果（行程內存放）：可接受——審批本就綁 UI 會話；重啟後模型查詢會得到可讀的 `resource_not_found`。

## 相關

- [ADR-0119](0119-unified-tool-interface-redesign.md) — 原生工具介面重設計（本 ADR 為其遞延項落地）
- [ADR-0051](0051-dispatch-callback-channel.md) — 回呼等待骨架（`await_dispatch_result` 的既有先例）
- [ADR-0143](0143-hitl-unification.md) — HITL 統一（**反轉本 ADR 的非同步審批：互動改 Gate-2 同步阻塞 broker，`approval_pending → get_command_result` 流不再被觸及**）
- 禁令 59（工具失敗可見性）；剩餘遞延項：演化管線 4→2、舊 shim 軟下架時機
