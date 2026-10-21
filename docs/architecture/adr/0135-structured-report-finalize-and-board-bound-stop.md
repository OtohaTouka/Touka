# ADR-0135: 結構化回報＋finalize 閉環＋停止綁看板（Phase 2）

> **現況**：決策 #3「停止綁看板」已移除（ADR-0146）。

## 狀態

✅ **已接受（實作與驗證 2026-07-07；擁有者尚未 commit）**。[in-process 多代理協調提案](../proposals/in-process-subagent-orchestration.md) §9 **Phase 2**：讓弱模型路徑穩定——需求 4（程式化裁決）、需求 5（最終驗收閉環）、目標 b（防「說完成了」偏移）。

> 🗑️ **決策 #3「停止綁看板」（`mission_stop_gate.board_allows_stop` + claude_handler 接線）已被 [ADR-0146](0146-tool-round-budget.md)（自主語意翻轉，2026-07-12）移除**：隨 nudge 續圈退役，`ToukaProxy/src/services/mission_stop_gate.py` 整檔刪除、`board_allows_stop` 全 repo 零引用、`tests/unit/test_mission_stop_gate.py` 亦刪。看板驅動的續跑改由 heartbeat `init_prompt` / `<harness_context>` 軟提示承接（非硬性程式閘）。**決策 #1（結構化回報 `subagent_report`）與決策 #2（`finalize_mission` 閉環）仍完全有效。**

## 日期

2026-07-07

## 背景

Phase 1（[ADR-0134](0134-in-process-subagent-dispatch.md)）打通 agent-as-tool 原語後，三個弱模型破口仍在（提案 §5）：①子代理回報只有**純文字摘要**——主 agent 難程式化裁決「真的完成沒」；②mission 全 DONE 只是機械式 `all(status==DONE)`，**無對照原始 goal 的最終驗收原語**；③自主迴圈停止條件靠 **stop keyword 子字串比對**——弱模型「亂喊完成」即可提前停（正是目標 b 要防的偏移）。

## 決策

1. **結構化子代理回報**（[`subagent_report.py`](../../../ToukaProxy/src/services/subagent_report.py)）：子代理任務指示（`_subagent_user_prompt`）附 `REPORT_INSTRUCTION`——要求在輸出「最後」附 ```json 回報塊（`status`／`claims`／`artifact_refs`／`confidence`）。`parse_subagent_report` 取**最後一個**塊解析，**誠實 fallback**：缺塊／壞 JSON／頂層非物件 → `unverified`＋`structured=False`＋`summary=全文`，欄位級型別錯逐欄丟棄（claims 非列表→空、confidence 非數字→None、clamp [0,1]）——**絕不假裝有結構**。deliver payload 以 `status`（傳輸層：子 loop 是否跑完）vs `report_status`（自我宣稱層：模型說做完了）**分開承載**；`await_dispatch_result` 渲染 Report／Claims／Artifacts 行（缺欄位＝舊格式直通，外派路徑零影響）。
2. **mission 級 finalize 原語**（需求 5 閉環）：`mission_tools` 新 action `finalize`（廣告數學不變——mission_tools 的 action enum 擴充，非新工具）。**無 verdict** → 驗收清單：原始 goal＋逐任務 artifacts／test_log 有無／`verification_cmd` 揭示（**明標「系統未代跑」——刻意不自動執行**，證據執行屬 Phase 3 [EvidenceCollector](../proposals/adversarial-review-subsystem-design.md)，避免繞過指令審批架構）；未全 DONE → 拒絕並列出未完成任務。**帶 verdict** → [`MissionManager.finalize_mission`](../../../ToukaMemory/src/memory_server/services/mission_manager.py)：鎖內載入→三重校驗（非空 verdict／有 mission／全 DONE）→寫入 `MissionState.final_verdict`／`finalized_at`（新增第一級欄位，additive、舊檔 pydantic 預設相容）→原子落盤。重複 finalize 以最新結論覆寫。
3. **停止條件綁看板完成度**（`mission_stop_gate.board_allows_stop`＋[`claude_handler`](../../../ToukaProxy/src/handlers/claude_handler.py) 接線）：stop keyword 命中後**多問一次看板**——活躍 mission 仍有**可推進**任務（TODO/DOING）→ 駁回早停、以看板提示前置到 nudge 續跑（提示含未完任務 id＋「無法進行請如實標 BLOCKED/FAILED」）。權威放結構化 gate 而非模型自律（提案 §6 邊界 2）。三個刻意邊界：**只擋早停不擋該停**（slots/成本上限仍是硬界）；剩餘任務全 BLOCKED/FAILED＝無法推進 → 放行（否則無限 nudge）；看板查詢失敗 **fail-open**（停止閘壞掉不得把 agent 鎖死在迴圈）。無 mission 的一般對話行為零變更。

## 後果

- **弱模型路徑三閘齊備**：說「做完了」要過看板（gate 3）；子代理回報可程式化比對（gate 1）；收案要對照原始 goal 記錄結論（gate 2）。Phase 3 互審將把 gate 1 的 claims 交給 critic 對照 EvidenceBundle、把 gate 2 的 finalize 升級為第二道總複審點——本 ADR 是其接點。
- **邊界（誠實）**：①回報塊依賴模型配合——不配合時 fallback 是「誠實的無結構」而非失敗，主 agent 仍可讀 `structured=False` 自行judge；**降幻覺效果未實測**（邊界 1 的 eval 屬 Phase 3 前置工程）；②finalize 的 verdict 是主 agent 的自由文字結論——本階段不驗證其真實性（防自我赦免屬 Phase 3 互審 gate）；③停止閘只覆蓋 keyword 短路路徑——slots 耗盡的停止不經看板（預算是更高優先的硬界，設計如此）。
- **驗收**：ruff（改動檔）＋lint_ssot＋**guard_all 39/39**（NOQA registry：`RAISE_STRING_BAN` 6→9、`BLE001` 11→12）＋**後端 pytest 全綠**（新增 [test_subagent_report.py](../../../tests/unit/test_subagent_report.py) 8 測＋[test_mission_finalize.py](../../../tests/unit/test_mission_finalize.py) 9 測＋test_mission_stop_gate.py 9 測（含 handler 接線 2 測，仿 budget-cap harness）＋test_dispatch_subagent.py +4 測）＋**前端 vitest**（前端零改動）。

## 相關

- [ADR-0134](0134-in-process-subagent-dispatch.md) — Phase 1 agent-as-tool 原語（本 ADR 的 deliver payload 即其擴充）
- [提案：in-process 多代理協調](../proposals/in-process-subagent-orchestration.md) §9 Phase 2／§7.2-4；[設計文件：對抗式互審](../proposals/adversarial-review-subsystem-design.md)（Phase 3 消費本 ADR 三個 gate 的接點）
- [ADR-0045](0045-mission-board-fsm-and-artifacts-decoupling.md) — mission FSM（finalize 疊加其上、不改 `VALID_TRANSITIONS` 拓撲）
- [ADR-0012](0012-autonomy-governance-and-static-mandates.md) — 自主預算（停止閘與 slots/成本上限的分工：閘擋早停、預算擋總量）
- [ADR-0146](0146-tool-round-budget.md) — 自主語意翻轉（**移除本 ADR 決策 #3「停止綁看板」：`mission_stop_gate.py` 隨 nudge 退役刪除，續跑改軟提示**）
