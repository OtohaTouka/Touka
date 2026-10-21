# ADR-0044: AI Agent Dispatch Center and Cross-Identity Mission Board Access — AI Agent 工作分派中心系統與跨身分任務看板存取

**狀態**: ✅ 已接受（型別修訂 by [ADR-0045](0045-mission-board-fsm-and-artifacts-decoupling.md)：artifact_ref `str|list|None`→翌日拆為 `str|None`＋`artifact_refs`（0045 §3 已註、此處 §2 補齊部分註記）；跨身分看板存取機制不變）
**日期**: 2026-05-25
**關鍵字**: AI Agent Dispatch System, DispatchAgent, can_supervise_dispatch, MissionManager, FastMCP, Task Schema Extension, Identity Normalization, Security Enforcement

---

## 背景與問題

隨著自主代理人 (Autonomous Agents) 在長生命週期任務 (Long-running Missions) 中展現出強烈的工作看板與專注力維護需求，系統推出了自主任務看板 (Mission Board) 機制。然而，在以下幾種實踐場景中，現有機制顯現出了侷限性：

1. **AI Agent 分派中心中介需求**：
   人類使用者希望建立一個 AI Agent 分派中心（基於 `ToukaMemory` 改造），作為內部已存 Agent（例如 `Alice`）與外部 AI Coding 工具（例如 Claude Code、Aider、Antigravity CLI、Codex CLI 等）之間的溝通中介。內部 Agent 需將工作拆解為實作計畫並填入看板，而外部 AI 則需要接上系統的記憶 MCP 來取得計畫並進行執行、更新狀態與回傳檔案列表。
2. **物理路徑監獄 (Path Sovereignty Isolation) 的衝突**：
   系統實施了嚴格的物理路獄隔離原則。任何 Agent 預設僅能讀寫自己身分（`AgentIdentity`）對應的個人磁碟目錄。若要將任務派發給另一個虛擬身分 `DispatchAgent` 並對其看板進行讀寫，現有 ResourceManager 隔離將直接拋出拒絕存取異常。
3. **Task 欄位結構化與驗證閉環不足**：
   傳統任務看板僅包含 `description` 與 `artifact_ref`。對於外界 AI 執行任務而言，缺乏「如何驗證（測試命令）」、「何為邊界（法律規則）」、「執行結果（測試日誌）」與「受阻異常（阻礙細節）」的結構化欄位支持。
4. **雙模式運行（整合與 MCP）下的身份授權**：
   外界 AI 工具通常以獨立 MCP 模式連線。在此模式下，身分可能為選填（或回退至 Guest）。如何使獨立運行的 FastMCP 看板工具在符合安全認證的前提下，自動降級授權，並賦予其對 `DispatchAgent` 看板的合法讀寫特權，是系統保持韌性與防禦的關鍵。

---

## 決策

為了解決中介調度需求、擴充任務細節，並在不破壞「路徑主權隔離」前提下實現安全的跨身分存取，我們實施了以下架構設計：

### 1. 虛擬中介 `DispatchAgent` 與特權能力 `can_supervise_dispatch`
- **中介代理人**：註冊一個特殊的系統代理人 `DispatchAgent`。它本身不主動活動（不參與排程心跳），僅作為內部 Agent 與外部工具溝通的「看板載體」。
- **跨身分特權標籤**：在 `identity_map.json` 中，為具備 Supervisor 職責的已存 Agent（如 `DebugAgent`、`Alice`）配置 `"can_supervise_dispatch"` 能力（Capability）：
  - 當 `MissionManager` 接收到存取目標 `target_agent_id="DispatchAgent"` 的請求時，會檢查執行者 `identity.capabilities` 是否含有此標籤。
  - 若核驗通過，系統會動態將 `resolved_identity` 對應升級為 `DispatchAgent` 的 AgentIdentity 物件，從而使其能夠越過物理監獄，讀寫 `personal/DispatchAgent/missions/current_mission.json`。
  - 未授權的 Agent 存取將直接拋出 `PermissionError` (HTTP 401/403)。

### 2. 任務看板結構 (Task Schema) 的深度擴展
對 `Task` (Pydantic 模型) 進行非破壞性升級，新增以下選用欄位：
- **`verification_cmd`** (`str | None`)：子任務的驗證命令（如 `pytest tests/unit/test_dispatch_auth.py`）。外部 AI 工具或本體在執行任務後可直接執行此指令以進行自動化驗證。
- **`boundary_rules`** (`list[str]`)：任務邊界法律規則（如 `NO_HARDCODED_PATHS`）。
- **`artifact_ref`** (`str | list[str] | None`)：產出物引用。將類型擴充為支援 list，以使外部 AI 執行完畢後能回傳「多個更動檔案的列表」。
- **`test_log`** (`str | None`)：測試結果日誌，用於儲存自動化測試的 stdOut，建立開發、測試、回報的完整閉環。
- **`blocker_detail`** (`str | None`)：當任務更新為 `BLOCKED` 或 `FAILED` 時，儲存其具體的異常日誌以作除錯參考。

### 3. FastMCP 身份自癒降級與授權

> ⚠️ **已由 ADR-0046 移除（2026-05-26）**：此自動自癒降級機制在 C1 安全修復中被永久移除。
> 外部 AI 工具改用專用 ToukaDispatch MCP 伺服器（Port 8083，DISPATCH_TOKEN + Session Token 認證）。
> `ToukaMemory/server.py` 不再包含任何任務派發工具，本節僅作歷史紀錄保留。

- 在 `ToukaMemory` 的 FastMCP 獨立伺服器入口（`server.py`）中，若連線請求中未包含 JWT 憑證，系統會進行自動降級與自癒授權。
- 若偵測到請求為對 `DispatchAgent` 看板的合法內部操作，會自動為當前的 FallbackIdentity 注入 `can_supervise_dispatch` 權限標籤。這確保了外部 AI coding 工具在 MCP 獨立模式下，依然能夠無縫操作 `DispatchAgent` 看板。

---

## 影響

- **打通跨身分協作看板**：建立了內部 Agent 派發任務（寫入 `DispatchAgent` 看板）與外部 AI 連線接單（讀取並更新 `DispatchAgent` 看板）的透明通訊橋樑。
- **物理路徑監獄依舊牢固**：所有跨身分讀寫皆在 `MissionManager` 中進行嚴格的能力標籤核查與 ID 正規化，防止了任意路徑穿越或未授權的 Agent 竊取資料。
- **任務驗證閉環化**：外部 AI 可以清楚地知道該跑什麼測試、遵守什麼規則，並將測試 stdOut 持久化回看板，方便人類使用者與已存 Agent 稽核進度與代碼品質。
- **符合 SOLID 與 SSOT 原則**：擴展 Task 欄位時，底層模型在 `ToukaMemory` 下沉實作，Proxy 與 FastMCP 均透過 Pydantic schema 進行 SSOT 同步，測試全綠且 26/26 守衛完美通過。
