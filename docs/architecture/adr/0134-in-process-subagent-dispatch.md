# ADR-0134: In-Process 子代理派發——dispatch_subagent（Phase 1 MVP）

> **現況**：enable_autonomy 改成繼承（ADR-0146 修訂）；後果③「唯讀」本來就不準；子代理改用 Subagent 角色（ADR-0245）。

## 狀態

✅ **已接受（實作與驗證 2026-07-07、全綠——guard_all 39/39、後端 pytest 2745 passed/4 skipped（+11）、前端 vitest 971；擁有者尚未 commit）**。[in-process 多代理協調提案](../proposals/in-process-subagent-orchestration.md) §9 **Phase 1 MVP**：主 agent 在 ReAct loop 內派「一個」in-process 子代理跑單一任務、拿回結果——補上全庫缺席的「agent-as-tool」原語，驗證「in-process 取代外派」的核心假設。

> 🔧 **兩處決策/後果描述已過時（部分修訂）**：
> 1. **決策 3 子 ctx `enable_autonomy=False` 硬編 → 沿父鏈繼承**：[ADR-0146](0146-tool-round-budget.md) 後 `subagent_tools.py` 改 `ctx["enable_autonomy"] = bool(base_ctx.get("enable_autonomy"))`（headless 判準沿父鏈；`is_toukasoul=False` 仍硬編）；nudge 續圈那半已隨 0146 移除。
> 2. **後果邊界③「子代理工具面＝Guest（唯讀 auth 檔案動詞）」自始不準**：`permissions.json` 的 Guest 角色實含 `write/edit/insert/undo/delete`（接 mutation verbs），子代理工具面**可寫非唯讀**；其所依賴的 `default_auth_level: ReadOnly` 欄位早被 [ADR-0143](0143-hitl-unification.md) 判為死碼並於 2026-07-11 移除。遞迴雙閘（權限+能力剝除、剝 `dispatch_subagent`）本身仍有效。

> 🔧 **子代理角色改為 Subagent（[ADR-0245](0245-model-layer-stage-2a.md)，2026-10-12）**：決策 3、4 與後果邊界③所說的 Guest，現為子代理專屬的 `Subagent` 角色，工具清單逐項照抄原 Guest（13 項，不含 `dispatch_subagent`）；Guest 角色已退役。遞迴雙閘不變。

## 日期

2026-07-07

## 背景

提案盤點（2026-07-06 調查）確認：ReAct 迴圈、Mission Board、`get_stream_generator`、`DispatchCallbackStore` 等地基成熟，但**全樹無任何工具能在 loop 內開子 loop**（需求 1 最大缺口）。既有外派路徑（ToukaDispatch，[ADR-0051](0051-dispatch-callback-channel.md)／[ADR-0082](0082-dispatch-in-proxy-consolidation.md)）把任務派給**外部 AI**；Phase 1 要的是同語義但**整個在 Touka 內部完成**的新分支——外派路徑原地不動（提案 §6 邊界 3）。

## 決策

1. **非同步 token 形（與外派同語義）**：[`subagent_tools.dispatch_subagent`](../../../ToukaProxy/src/tools/subagent_tools.py) 立即回 `build_tool_success(callback_token=...)`，背景 `asyncio.Task` 開子 loop；完成（或失敗）後 `DispatchCallbackStore.deliver_result` **in-process 直呼**（繞過 HTTP 回呼路由）。主 agent 以**既有** `mission_tools(action="await_dispatch_result")` 等待——等待工具**零改動**重用，模型面與外派流程語義一致（dispatch → 可先做別的 → await）。失敗也必 deliver（`FAILED`＋例外摘要），等待者不會空等到 TIMEOUT。
2. **組裝走真實 pipeline（ADR-0133 harness 同款）**：`_assemble_subagent_request` 以子代理身分跑 `proxy_service.pipeline.execute`（局部匯入破重循環，同 [ADR-0133](0133-context-snapshot-real-assembly-rewrite.md)）——子代理拿到與其角色相符的工具面與系統塊，零手工同步；組裝後（a）強制指定 brain（不依賴未知臨時身分的解析）、（b）**剝除 `dispatch_subagent`**。
3. **臨時身分不落盤（提案 §7.2-3）**：`__subagent_{hex}__`＋**Guest** 角色（`group.py` 先例）；全路徑不呼 `record_working_brain`／soul 寫入（三個既有呼叫點 proxy_service:96／dispatcher:204／group.py:343 皆不在子路徑上）。**衍生 request_id `{parent}:sub{n}`**：不與父共用 rhythm/slot 鍵；**無 session_id**：不碰 session 狀態與中斷旗標；子 ctx 固定 `is_toukasoul=False`／`enable_autonomy=False`＝不耗自主配額、不觸發續命 nudge（工具執行不受影響——ReAct-on-tool_use 無條件）。
4. **遞迴根絕＝既有雙閘，零新機制**：權限面——Guest `allowed_tools` 本就不含 `dispatch_subagent`（Gate 1 不廣告＋Gate 2 拒絕；`permissions.json` **零改動**，Owner/Debugger 走 `"*"` 自動涵蓋）；能力面——組裝後剝除 schema。子 loop 硬上限 `asyncio.wait_for(600s)`。
5. **executor 增注 `_ctx`**：[`tool_executor`](../../../ToukaProxy/src/services/tool_executor.py) `call_kwargs` 加 `"_ctx": ctx`（additive，`_emit_event` 同慣例：工具以 `**kwargs` 取用、不需要者忽略）——子代理據此衍生 ctx（`PipelineContext.model_copy()` 淺複製共享 client 連線）。
6. **廣告 28→29**：`DISPATCH_SUBAGENT_SCHEMA` 定義於 [schemas.py](../../../ToukaProxy/src/tools/builtin/schemas.py)（純資料 SSOT、含「何時不要用」）；registry 走**延遲載入 lambda**（`send_message`／`execute_command` 同慣例）——避免 `builtin/__init__` 頂層 import factory/handlers 連鎖。

## 後果

- **需求 1 的最小閉環打通**：主 agent 可 dispatch → await → 讀結果 → 自行 `update_status`（看板更新仍由主 agent、主身分執行——不碰 §7.2-2 權限牆）。Phase 2（結構化回報 schema）與 Phase 3（互審，見[設計文件](../proposals/adversarial-review-subsystem-design.md)）都在此原語上疊加。
- **外派路徑零改動**：ToukaDispatch／HTTP 回呼／`dispatch_supervisor` 原地不動；in-process 是 callback store 的新交付路徑，非替換。
- **邊界（誠實）**：①deliver 後無人 await 的 token 會留在 in-memory dict（`wait_for_result` 才清）——MVP 接受，量級小；②背景子 loop 不隨父請求中斷自動取消（無 session 綁定是刻意的隔離取捨）——長跑治理屬 Phase 4（續跑韌性）；③子代理工具面＝Guest（唯讀 auth 檔案動詞＋research）——夠不夠用待真實任務實測，Phase 4 權限收尾再議；④**live E2E（真模型）待擁有者手動 smoke**——單元契約以 fake stream 全覆蓋，真實端到端需跑服務＋API 金鑰。
- **驗收**：ruff（改動檔）＋lint_ssot＋**guard_all 39/39**（NOQA registry：`LOCAL_IMPORT_BAN` 19→20）＋**後端 pytest 2745 passed/4 skipped**（新增 [test_dispatch_subagent.py](../../../tests/unit/test_dispatch_subagent.py) 11 測：註冊／廣告／Guest 排除／token 契約／in-process 喚醒／FAILED 交付／隔離契約／遞迴剝除／參數防呆／executor 接線）＋**前端 vitest 971**（前端零改動）。

## 相關

- [提案：in-process 多代理協調](../proposals/in-process-subagent-orchestration.md)（§9 Phase 1 邊界＝本 ADR 範圍）；[設計文件：對抗式互審](../proposals/adversarial-review-subsystem-design.md)（Phase 3 將重用本原語開 critic 子 loop）
- [ADR-0051](0051-dispatch-callback-channel.md) — callback store 語義（本 ADR 新增 in-process 交付路徑）；[ADR-0082](0082-dispatch-in-proxy-consolidation.md) — 外派路徑（原地不動）
- [ADR-0133](0133-context-snapshot-real-assembly-rewrite.md) — 真實 pipeline harness 模式（組裝復用）；[ADR-0111](0111-native-model-default-sampling-params.md) — 群聊 bypass pipeline 直呼 `get_stream_generator` 先例
- [ADR-0124](0124-structured-success-and-applied-state-echo.md) — 結構化成功封套（`callback_token` 掛 `**fields`）
- [ADR-0146](0146-tool-round-budget.md) — 自主語意翻轉（**修訂決策 3：子 ctx `enable_autonomy` 改沿父鏈繼承、nudge 那半退役**）；[ADR-0143](0143-hitl-unification.md) — HITL 統一（**佐證後果邊界③「唯讀」不成立：`default_auth_level` 死碼移除**）
