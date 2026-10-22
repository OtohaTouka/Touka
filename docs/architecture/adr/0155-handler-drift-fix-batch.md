# ADR-0155: 三 Handler 漂移修復批（防幻覺/記憶訊號/計時/Gemini 快取——抽取重構前置步驟 0）

## 狀態

✅ 已接受（提案 [handler-drift-fix-batch](../proposals/handler-drift-fix-batch.md) 維護者核准；決定 P1=B、P2=含檔案讀取面、P3=納入 D7/D8、P4=照 C1–C6；工具名已對照 ADR-0119 重設計現況校驗後執行）

## 日期

2026-07-13

## 背景

三 handler（openai 410 行/claude 507/gemini 498）約 60% 為同一 ReAct 狀態機的三份拷貝（12+ 處逐位元相同），**每次修復只落在其中一份**，累積六個經逐行覆核確認的漂移 bug（解剖審計＋synthesis 交叉驗證）。本批只修漂移、不做抽取重構（另案），使後續「單一 ReAct 核心＋Provider Protocol」重構獲得行為保持的乾淨基線。

## 決策與實作（C1–C5，各 commit 紅→綠）

- **C1（D2＋D6）防幻覺 SSOT 統一**：`collect_known_tool_names` 自 claude_handler 私有副本**下沉 `guard_service`**（含動態 MCP 工具、列舉失敗退回靜態集），三 handler 一律呼叫＝**恢復禁令 59(c) 合規**（openai/gemini 原靜態三 registry＝MCP 幻覺假陰性盲區）；claude inline 偵測副本退役（跨段落 DOTALL 誤殺修復未跟上＋未 escape 工具名＝MCP 名含 regex 特殊字元即 re.error）。禁令 59(c) 文字同步改指 `guard_service.collect_known_tool_names`。
- **C2（D3＋D4）記憶/誠實訊號結構化**：訊號生產單點＝`tool_executor.record_tool_usage`（帶 `action` 參數的聚合工具同時記入限定名 `name.action`，如 `memory_tools.save`；九裸動詞依名計——其內部委派 vault_tools 為 Python 呼叫不經 executor，故查證集顯式列裸動詞）。`guard_service` 判定改讀 SSOT：記憶承諾閘=`MEMORY_SAVE_SIGNAL`、查證閘=`VERIFICATION_TOOLS`（`memory_tools.query`＋裸動詞 `read/list/find/grep`＋`vault_tools` 讀側 shim）——舊閘掛在已退役的工具名（save_memory/query_memory/access_personal_drive 僅存為內部函式或全不存在）＝openai/gemini 誤報恆真、查證恆不知、**警告文案自己教幻覺名**（一併改引實存工具）。e_index 檢索訊號三家統一讀 `memory_tools.query`（舊 string-sniff＝分量恆 0 死碼）；claude 私有 block-walking 旗標退役。
- **C3（D1）Gemini re-cache 保 system/tools**：初始快取啟用會把 `system_instruction`/`tools` 自 `gemini_req` 刪除，迴圈中途 re-cache（contents>10）回頭讀恆 None → 新快取不含 system/工具＋contents 塌縮＝**該輪起模型人格消失＋不會用工具**。修＝迴圈前保存原始值、re-cache 讀保存值。紅測以假 cache/httpx 驅動 5 輪工具循環實測觸發。
- **C4（D5，拍板 B）`pure_gen_ms` 語意統一**＝各輪上游呼叫耗時累加（送出→回應到達；gemini 含 503 退避＝誠實上游延遲；不含工具執行/本地處理）。舊制三家相反：openai/gemini 每輪重置 t2＝死值 ≈0、claude 量整圈含工具。假時鐘紅測數值精準對齊預測（0/0/31000 → 21000）。e_index 消費同源 gen_time。**消費點已全庫核**：CSV 稽核照錄、前端零消費、無閾值邏輯。
- **C5（D7＋D8）**：Gemini 工具呼叫合成穩定 `call_id`（`gcall_{seq}`）——ToolCallEvent（ADR-0122）逐呼叫關聯不再獨缺 Gemini 路；`is_toukasoul` 預設三家統一 fail-closed `False`（生產 ctx＝`PipelineContext` 欄位恆存在→預設值永不觸發＝零行為變化，純防禦）。

## 後果（誠實行為變化）

- openai/gemini 開始抓 MCP 工具幻覺（新警告可能出現）；記憶幻覺誤報停止、漏報開始被抓；**e_index 上升**（檢索分量活了）；`pure_gen_ms` 從近 0 變真值（無下游閾值依賴，已核）；claude 工具幻覺警告文案改 guard_service 版（字面微異）；Gemini 長對話（contents>10）快取內容包含 system/tools＝略大、換正確性。
- **測試面**：`test_react_memory_detection.py`（跨輪語意守衛）mock 執行器改呼叫真的 `record_tool_usage`＝持續追蹤生產訊號機制；靜態源碼確認一律 `inspect.getsource`（非檔案 I/O，符 I/O 監獄）。
- **驗收終態**：pytest 全套 **3469 passed**（基線 3440，+29 淨新增；含 integration）／guard_all **41/41**／lint_ssot clean／ruff 基線持平（零新增）／openapi 零變動（不動 wire）／vitest 不動（純後端）。
- **NOQA**：零新增。

## 相關

- 提案：[handler-drift-fix-batch](../proposals/handler-drift-fix-batch.md)（含工具名對齊校驗表）
- [ADR-0119](0119-unified-tool-interface-redesign.md)（原生工具重設計＝訊號名對齊的現況基準）
- [ADR-0122](0122-tool-event-telemetry-envelope.md)（ToolCallEvent 封套＝D7 關聯 id 的消費者）
- [ADR-0146](0146-tool-round-budget.md)（「三 handler 同構」的保險絲＝本批漂移證據的來源之一）
- [ADR-0152](0152-reasoning-channel-and-fold.md)（思考分頻；Gemini 路徑零 reasoning 處理＝已知遺留、屬抽取重構 parse_round 範圍，非本批）
- 後續：單一 ReAct 核心＋Provider Protocol 抽取重構（審計 synthesis 絞殺者計畫，另案提案）

> **補記（2026-10-11，[ADR-0241](0241-model-layer-stage-0.md)）**：C3 的顯式快取在無痕對話一律不建（初次與迴圈重建皆跳過）；非無痕不變。
>
> **補記（2026-10-12，[ADR-0243](0243-model-pre-run.md)）**：Gemini 顯式快取已全停，C3 的保存值隨快取分支一併刪除；每輪照常送 `system_instruction`／`tools`。
