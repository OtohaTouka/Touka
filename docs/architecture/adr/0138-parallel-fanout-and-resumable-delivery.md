# ADR-0138: 多 subagent 並行 fan-out ＋ 耐久可續交付（Phase 4）

## 狀態

✅ **已接受（實作與驗證 2026-07-07）**。[in-process 多代理協調提案](../proposals/in-process-subagent-orchestration.md) **Phase 4**（規模化與可靠性收尾）——需求 1 完整 fan-out ＋ 需求 3 斷線續接韌性。**本 ADR 落地即完成整份提案的四階段增量。**

## 日期

2026-07-07

## 背景

Phase 1–3 已把「主 agent → 派子代理 → 結構化回報 → 證據接地互審 → 落盤 gate」的**單線**貫通。提案 §9 Phase 4 收尾兩件：①**需求 1 完整 fan-out**——Phase 1 的 `dispatch_subagent` 一次一個 token，缺「同時派多個各跑各的再匯總」的協調器；②**需求 3 韌性破口**——`DispatchCallbackStore` 是純記憶體 `asyncio.Event`，**重啟即失、正在等的 await 掛不回**（結果雖已算出）。提案本身已標「ADR-0078 #8 自動續跑仍 TODO」。

## 決策

1. **並行 fan-out orchestrator**（[`subagent_tools.run_subagent_group`](../../../ToukaProxy/src/tools/subagent_tools.py)）：以 `asyncio.Semaphore(concurrency)` **封頂並發**（防資源爆炸，提案 §6 邊界 5），`asyncio.gather` 同時開多個 in-process 子 loop、**單一子代理崩潰不沉全組**（其結果標 FAILED）、逐一 `parse_subagent_report` 匯總。**重用** Phase 1 `_run_subagent`＋Phase 2 回報解析＋抽出的 `_result_from_text`/`_result_from_error`（單派與 group 共用結果建構，零重複）。工具 `dispatch_subagent_group(subtasks)`：**阻塞至全完成**後回結構化匯總（completed/failed/results）；子任務數硬上限 `_MAX_GROUP=8`、預設並發 `4`（超出＝結構化 validation_error）。廣告 29→30；registry 延遲載入 lambda；**遞迴閘沿用**——子代理 Guest 權限面不含 `dispatch_subagent_group`。
2. **耐久可續交付**（[`services/subagent_results.py`](../../../ToukaProxy/src/services/subagent_results.py)）：`_run_and_deliver` 於 `deliver_result`（記憶體、喚醒 live 等待者）後**雙寫耐久落盤**——把結果寫進 requester 的 personal vault（`dispatch_results/<token>.json`，沿用 `resource_manager.get_vault_path`＋原子寫入；token 白名單化防路徑穿越）。`mission_tools(action="await_dispatch_result")` 改**耐久優先**：先讀耐久（已交付／**跨重啟**：記憶體 Event 沒了但結果還在→直接重讀、不空等到 TIMEOUT），無才等 Event，超時再撈一次耐久（跨重啟窗）。**分離原則**：`DispatchCallbackStore` 維持純記憶體（不耦合 identity/drive）；耐久落在 subagent 層，best-effort（落盤失敗廣捕、不得沉掉已交付結果）。
3. **臨時非落盤身分（收尾確認）**：提案 §7.2-3 的「並行共用 agent_id 互踩污染」在 Phase 1 設計時已根治——每個子代理 `__subagent_{uuid.hex}__` **互異**、全路徑不呼 `record_working_brain`。本 ADR 加**並行隔離測試**釘死：group 下三個並行子代理身分全互異、任一 `record_working_brain` 被呼叫即測試失敗。

## 後果

- **提案四階段全數落地**：需求 1（動態派多＋記步驟目標）、2（對抗式互審）、3（回報持久化・斷線續接）、4（看報告裁決）、5（每步同流程＋最終驗收）皆有對應實作。fan-out（本 ADR）＋互審（ADR-0137）＋finalize（ADR-0135）合成完整的「拆分→並行執行→互審→驗收→續接」。
- **韌性提升但非全自動**（誠實邊界）：耐久落盤讓結果**跨重啟可重讀**（await 不再永久卡死）——這是需求 3 的**substrate**；但**自動背景續跑驅動**（重啟後主動掃描未完 mission、自動 retrigger 下一步＝ADR-0078 #8）**仍為 TODO、明確劃出 Phase 4 範圍外**（需接 heartbeat/cron，屬另一提案）。現況：重啟後主 agent 重新 await 同 token 即可拿到耐久結果。
- **其他邊界**：①`dispatch_subagent_group` 阻塞至全完成——wall-clock 受並發上限×子代理 timeout 約束；需要非阻塞「先派了先做別的」時用 `dispatch_subagent`（token）；②耐久落盤在 requester 自己的 drive（DebugAgent 測試落 sandbox，未觸真實 data/，已核）；與 mission board 同款運行時持久化，非開發者手動管理 data/；③跨身分看板路徑歧異（`_PathIdentity` 角色猜測）為既有缺陷、已另立背景任務，不在本 ADR 範圍。
- **驗收**：ruff（改動檔）＋lint_ssot＋**guard_all 39/39**＋**後端 pytest 全綠**（新增 test_subagent_group 11／test_subagent_results 6＝17 測，含並發上限釘死／一子失敗隔離／並行身分互異不落盤／耐久 roundtrip／雙寫／await 耐久優先模擬重啟）＋前端 vitest 971（前端零改動）。NOQA registry 同步（LOCAL_IMPORT_BAN 21→23、BLE001 13→15）。

## 相關

- [提案：in-process 多代理協調](../proposals/in-process-subagent-orchestration.md) §9 Phase 4（本 ADR 完成之＝提案收官）
- [ADR-0134](0134-in-process-subagent-dispatch.md)（Phase 1：`_run_subagent`／`_run_and_deliver` 為 fan-out 與雙寫的基座）／[ADR-0135](0135-structured-report-finalize-and-board-bound-stop.md)（回報解析）
- [ADR-0051](0051-dispatch-callback-channel.md)（callback store：本 ADR 補耐久層、保持其純記憶體）；[ADR-0078](0078-resilience-and-failure-observability.md)（韌性；#8 自動續跑仍 TODO＝Phase 4 明確不做）
