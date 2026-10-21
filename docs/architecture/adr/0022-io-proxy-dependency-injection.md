# ADR-0022: I/O Proxy Dependency Injection (DIP Decoupling)

## 狀態
已接受 (Accepted)（部分修訂 by [ADR-0116](0116-toukamemory-integration-only.md)：依賴反轉介面 `IResourceProxy` 與 `get_resource_proxy()` 工廠**保留**，但 ToukaMemory 退役獨立 MCP 模式後，**已移除 `_FallbackResourceProxy` 降級分支**——`get_resource_proxy()` 改為一律回真實 `ResourceManager`、取不到即 fail-loud。**另：決策 #4「全面移除 ToukaMemory 對 ToukaProxy 任何匯入」現已放寬——`legacy_vault.py`／`config_utils.py` 直接 import `src.services.resource_manager`／`src.utils.time_utils`，改以 `.importlinter` 白名單管理，非「零匯入」。**）

## 上下文 (Context)
根據 `AGENTS.md` 核心守則，系統必須實施「全能 I/O 代理 (Full I/O Proxy)」，即所有檔案操作均須透過 `ToukaProxy` 的 `ResourceManager` 執行，以統一處理路徑轉譯、沙盒主權攔截與 Docker 映射。

然而，`ToukaMemory` 被設計為不僅能作為整合模組，還必須具備獨立作為 MCP Server 運行的能力。若讓 `ToukaMemory` 內部的檔案操作 (`core_file_ops.py`) 或是管理類 (`manager.py`) 直接匯入 (`import`) `ToukaProxy` 的 `ResourceManager`，將導致嚴重的架構問題：
1.  **違反依賴反轉原則 (DIP)**：底層領域模型 (`ToukaMemory`) 硬依賴高層基礎設施 (`ToukaProxy`)。
2.  **破壞 MCP 獨立性**：在沒有 `ToukaProxy` 程式碼的獨立環境中執行 `ToukaMemory` 會立即產生 `ModuleNotFoundError`。

## 決策 (Decision)
1.  **合約分離 (Protocol)**：在 `ToukaMemory` 內部定義 `IResourceProxy` (基於 `typing.Protocol`)，描述所需的 I/O 操作介面（如 `read_file_text`, `list_directory` 等），但不依賴具體實作。
2.  **依賴注入 (Dependency Injection)**：當 `ToukaProxy` 以整合模式呼叫 `ToukaMemory` 的 `vault_tools` 或 `memory_tools` 時，需主動將自身的 `resource_manager` 實例透過參數 (`resource_proxy=resource_manager`) 注入到執行鏈路中。
3.  **優雅降級 (Graceful Degradation)**：`ToukaMemory` 的 `core_file_ops.py` 等底層模組在接收到請求時，會判斷是否收到注入的 `resource_proxy`：
    *   **整合模式 (注入成功)**：優先調用 `resource_proxy` 執行 I/O，以實現全系統統一的路徑轉譯與防護。
    *   **獨立 MCP 模式 (無注入)**：降級使用 `ToukaMemory` 內建基於 `pathlib.Path` 的安全沙盒機制（透過 `_enforce_root_bound` 驗證）進行原生 I/O 操作。
4.  **消除技術債**：全面移除 `ToukaMemory` 原始碼中任何對 `ToukaProxy`（如 `src.services.*`）的絕對或相對匯入。

## 後果 (Consequences)
*   **優點**：
    *   完美遵守 SOLID 的依賴反轉原則 (DIP) 與開放封閉原則 (OCP)。
    *   保證了 `ToukaMemory` 真正的環境獨立性與 MCP 部署彈性。
    *   維持了 `ToukaProxy` 在整合環境下對 I/O 的絕對統治權 (Full I/O Proxy)。
*   **挑戰**：
    *   `ToukaMemory` 內部的 I/O 操作會變得較為冗長（需處理 `if resource_proxy: ... else: ...` 的雙軌邏輯）。
    *   `ResourceManager` 必須被擴充（加入如 `list_directory`, `glob_search` 等方法）以滿足 `IResourceProxy` 介面協議的完整性。

## 參考
- `AGENTS.md` - 全能 I/O 代理 (Full I/O Proxy) 守則
- `AGENTS.md` - 整合模式優先 (Integrated Mode Supremacy)
- `src/tools/vault_tools.py` (ToukaProxy)
- `memory_server/tools/core_file_ops.py` (ToukaMemory)
