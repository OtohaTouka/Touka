# ADR-0101: 計分卡生產保真 — 與聊天／心跳共用同一套 context＋身分來源

**狀態**：✅ 已接受
**日期**：2026-06-19
**影響範圍**：ToukaProxy（scorecard runner／debug router／main `build_base_context`／permissions）
**關鍵字**：scorecard, 生產保真, build_base_context, SSOT, make_real_runner, Debugger 角色, identity_service, app.state.context, memory_tools_schema, ask_user divergence, 軌B, 建基於 ADR-0100

> **2026-08-14 修訂（[ADR-0214](0214-standalone-smoke-data-and-metered-api-boundaries.md)）**：
> 計分卡會呼叫真實模型並可能執行工具，永久排除 automation。CLI 現將 app-data／workspace／
> config／logs／工具寫入導向 temp sandbox，並具 provider request hard budget；每次執行前仍須取得
> 有界的人類同意。Debug Center 正常 UI 有 `confirmToast`，但後端 endpoint 尚未消費 ADR-0214
> approval capability 或 request budget，不能宣稱整條 API 路徑已有機器強制人工閘。
>
> **2026-10-15 修訂（[ADR-0261](0261-memory-long-horizon-eval.md)）**：計分卡量不到靈魂與記憶（單輪、無痕、DebugAgent 沒有靈魂、主要模型未設定），長期記憶評估改由 `tests/memory_eval/` 與人工 `memory_eval_probe.py` 負責，不擴充 `CORE_SCENARIOS`；計分卡 CLI 同時改成主迴圈只送 1 次（見 ADR-0214 的修訂）。

---

## 背景

模型計分卡（軌B）的初衷是「拿來評估與檢查**生產狀態**是否正常」。但其 `real_runner` **手刻**一個極簡執行情境，與生產（聊天 `proxy.py`／心跳 `heartbeat.py`）走的準備流程不同源，導致模型被發到的工具**歸零**，`tool_memory_recall` 對**任何**模型都必失敗（`called: []`）。「一個正常（生產）一個異常（計分卡）」使計分卡失去意義。

兩個獨立缺口，任一個都讓工具歸零：

1. **權限缺口**：`identity_map.json` 把 `DebugAgent` 映射到 `Debugger`，但 `permissions.json` **沒定義這個角色** → 退回空權限 → ToolLayer 用 `is_tool_allowed(tool, [])` 過濾掉**全部**工具（內建＋記憶）。這是潛伏 bug：任何解析 `DebugAgent` 的路徑都拿空權限。
2. **情境缺口**：runner 手刻的 `PipelineContext` 沒有 `memory_tools_schema`／`protocol_manager`（生產經 `ctx = app.state.context.copy()` 取得）→ 即使權限放行，記憶／MCP 工具的 schema 清單本身也是空的。

## 決策

**計分卡與生產走同一套「context＋身分」來源**（讀同一份來源，而非另抄手刻副本——副本必然漂移）。資料面仍綁定 `DebugAgent`（記憶讀寫不污染真實 agent），但**機制面**與生產一致。

1. **`build_base_context()` SSOT**（`main.py`）：把 `app.state.context` 的組裝抽成單一函式；`lifespan` 與計分卡 CLI（`scripts/run_scorecard.py`）都呼叫它 → 不漂移。
2. **runner 工廠** `make_real_runner(base_ctx, identity)`：`ctx` 從 `base_ctx` 展開（記憶／MCP／內建／降級旗標皆在），身分用 `identity_service.get_identity("DebugAgent")`（SSOT 解析）。端點 `/scorecard/run` 從 `request.app.state.context.copy()` 取 `base_ctx`；保留測試注入假 runner（`_scorecard_runner`）。
3. **`Debugger` 角色定義**（`permissions.json` ＋ `.example` ＋ `DEFAULT_PERMISSIONS_CONFIG` fallback）`inherits: ["Owner"]` → `DebugAgent` 取得 Owner 等級工具面（單一來源：Owner 變動自動跟隨）。
4. **刻意保留的唯一 divergence**：不設 `resumable_stream` → 不注入 `ask_user`。計分卡自主執行、無真人可回答，注入會卡到逾時——與**心跳**完全一致。

分階段落地（每階段全綠）：Batch 1 權限對齊（`Debugger` 角色）；Batch 2 情境對齊（runner 工廠＋端點同源）；Batch 3 SSOT 收斂＋CLI 對齊（`build_base_context`）；Batch 4 零-API 回歸測試＋本 ADR。

## 後果

- ✅ 計分卡如實反映生產：模型被發到記憶／工具，`tool_memory_recall` 可真實調用記憶工具；「一個正常一個異常」消失。
- ✅ 修掉潛伏 bug：`DebugAgent`（→`Debugger`）先前在所有路徑都拿空權限。
- ✅ base context 與身分解析皆單一來源；CLI 不再手刻、與 UI 端點同源。
- ⚠️ **`Debugger` ＝ Owner 工具面**：僅 `DebugAgent` 映射至此，且所有 debug／計分卡端點本就 owner/admin gated，外人拿不到此角色。
- ⚠️ **CLI 未連線 MCP**（未跑 `initialize_all`）：記憶／內建工具齊備；需 live MCP 覆蓋請用 UI 端點。
- 安全：資料仍綁 `DebugAgent`（沙箱化）；計分卡**不進 CI**（手動觸發、花真實 API 費用），但新增的回歸測試用假 dispatch、零 API，可進 CI。

建基於 [ADR-0100](0100-tool-schema-canonical-openai.md)（canonical 工具 schema）與 [ADR-0099](0099-provider-registry-ssot.md)（Provider Registry SSOT）。
