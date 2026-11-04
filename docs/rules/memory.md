# 記憶規則

範圍：`ToukaMemory/**`：記憶、舊對話、任務看板。

從根 `AGENTS.md` 搬來的全文，沒有改寫。跑 `python scripts/applicable_rules.py --print-rules` 看這次改動適用哪幾條，不必讀完整份。改規則見 [README](README.md)。

## 禁令

*   **禁令 1**：除非使用者明確要求，否則**嚴禁**將內部的記憶通訊改為 MCP 模式。專案內部必須使用整合模式。
*   **禁令 3**：在修改 `ToukaMemory` 的工具時，必須同步更新其 `tools/__init__.py` 的導出清單。
*   **禁令 6**：**嚴禁**在 ToukaMemory 模組中直接導入 `src.services.resource_manager`。改用 `memory_server.config_utils.get_resource_proxy()` 工廠函數（回傳 ToukaProxy 的 `ResourceManager`；ADR-0116 後為整合模式單一部署、取不到即 fail-loud，不再有 MCP 獨立降級代理）。所有 I/O 操作應透過該代理執行。
*   **禁令 14**：**嚴禁** ToukaMemory 直接 import ToukaProxy 的 `src.services.*`、`src.utils.*` 等內部模組，必須改用 `get_resource_proxy()` 工廠函數／`IResourceProxy` 依賴反轉介面。`guard_module_boundaries.py`（Guard #21）會自動偵測。**此邊界保留以維持子系統解耦**（ADR-0116 退役獨立 MCP 模式後，正當性由「MCP 獨立性」改為「依賴反轉解耦」；`config_utils`/`legacy_vault`/`vector` 的 `ResourceManager`/時間工具注入點於 `.importlinter` 明列豁免）。（ADR-0032／ADR-0116）
*   **禁令 29 (跨身分分派代理與路徑防禦)**：**嚴禁**破壞或繞過 `can_supervise_dispatch` 的跨身分路徑代理授權。所有涉及對 `DispatchAgent` 看板的讀寫存取，必須通過 `MissionManager` 及 `get_resource_proxy()` 以身分能力（Capability）方式進行校驗，嚴禁使用硬編碼的身分特權，或在未經驗證的情況下開放跨身分存取。（ADR-0044）
*   **禁令 30 (任務狀態流轉 FSM 約束)**：自主任務看板狀態更新**必須**強制執行有限狀態機 (FSM) 轉換矩陣，嚴禁任意跳過 `DOING` 步驟直接跳轉為 `DONE`；且 `DONE` 狀態為終端狀態，封鎖後續任何修改。（ADR-0045）
*   **禁令 31 (FAILED 重置特權限制)**：任務進入 `FAILED` 狀態後，**嚴禁**無特權的外部 AI 代理人自行重置回 `TODO`。必須由具有 `can_supervise_dispatch` 特權能力的 Touka Agent，或角色為 `Owner`/`Debugger`/`admin` 的特權身份才能重設。（ADR-0045）
*   **禁令 32 (產出物欄位解耦安全)**：產出物欄位**必須**徹底解耦為單一產物 `artifact_ref` (str) 與多產物列表 `artifact_refs` (list[str])，物理儲存互不干涉，且均需經過嚴格的絕對路徑及穿越防禦，以確保前後端型別安全與 UI 渲染穩定，防止覆蓋踩踏。（ADR-0045）
*   **禁令 35 (BLOCKED 狀態必填 blocker_detail)**：任務狀態轉換為 `BLOCKED` 時，`blocker_detail` 為**必填**非空字串。`MissionManager.update_task()` 必須驗證此欄位，缺失或空值時以 `ValueError` 拒絕轉換。（ADR-0046 H1 修復）
*   **禁令 36 (Task 賦值時觸發驗證)**：`Task` Pydantic 模型**必須**保持 `model_config = ConfigDict(validate_assignment=True)`。嚴禁移除此設定，否則直接屬性賦值將繞過 `@field_validator`，導致路徑穿越防禦失效。（ADR-0046 H2 修復）

## 小節

### 7. 資料結構與主權 (Data Structure & Sovereignty)
*   **靈魂資料 (Soul)**：存儲於 `data/soul/{agent_id}`。受 `DATA_PROTECTION` 保護。
*   **用戶配置 (Custom)**：存儲於 `data/custom/{model_id}`。
*   **舊對話存儲庫 (Legacy)**：存儲於 `data/legacy/{agent_id}/legacy_history.db`。已實體 SQLite 化，統一透過 `vault_tools(vault="legacy")` 進行高效檢索。
路徑主權隔離的其他子項留在根 `AGENTS.md`，這裡只放搬過來的部分：
    - **身分正規化與糾偏 (Identity Normalization)**：底層執行層（ToolExecutor）與內部系統（MemoryManager）具備最高身分判定與糾正權。系統會自動將 Agent 可能產生的變形名稱（如：小愛/愛）統一歸一化至正式 ID（Alice），確保實體目錄定位的強一致性，杜絕安全漏洞與資料夾散落。
    - **執行生命週期原子性 (Lifecycle Atomicity)** ⭐：身分驗證僅發生於「門禁點 (Gate 1)」。一旦脈衝任務啟動並成功獲取 `AgentIdentity` 物件，該任務的後續執行（多輪 ReAct 與內部工具調用）應錨定於此物件，不受原始 JWT Token 在任務中途過期之影響。這確保了長生命週期任務的穩定性。
    - **資源配額防禦 (Resource Guarding)** ⭐：所有自主演化工具的執行均受動態資源限制。系統強制實施「時間配額 (Execution Timeout)」、「空間配額 (Storage Quota)」與「記憶體配額」，防止 Agent 產生的錯誤代碼或惡意行為耗盡系統資源。**OS 級強制（ADR-0076 / DA28）**：演化沙箱子進程在 POSIX 下以 `preexec_fn` 套用核心級 `RLIMIT_FSIZE`（儲存，取代可繞的 in-process JailedWriter）、`RLIMIT_AS`（記憶體）、`RLIMIT_CPU`（CPU 秒）；Windows 無 `resource` 模組時優雅降級回逾時＋JailedWriter。
    - **物理鎖定**：系統強制將操作範圍鎖定在該 ID 所持有的子目錄。禁止越權存取。
*   **資產主權 (Asset Sovereignty) ⭐**：所有 Agent 生成的資產必須透過 `/v1/outputs/{agent_id}/{filename}` 動態路由服務。禁止直接暴露物理路徑。
*   **記憶庫是正本（ADR-0256）**：`core_memory.db` 是記憶的正本；向量索引可以重建，索引失敗只標待補、不刪記憶。記憶庫升級在 Proxy 啟動時做（啟動時沒建成、或換了嵌入模型，第一次使用時會再試一次），先備份、可重跑，失敗就停在舊版；離線工具不升級。
*   **記憶會演變（ADR-0263）**：記憶被新的取代時不刪，舊的保留並連到新的。
*   **個人空間目錄（ADR-0255）**：個人空間的 `.touka/` 由系統維護，可以從實際檔案重建。檔案動詞不能寫入；用其他方式寫入的內容，讀取時以實際檔案為準。
