# ADR-0017: 入口點引導特權與路徑主權豁免 (Entry Point Bootstrap Privilege)

## 狀態
已接受 (Accepted)（具名例子修訂 by [ADR-0116](0116-toukamemory-integration-only.md)：run_server.py 已刪除、main.py 靠 PYTHONPATH、awaken.py 扁平 import；白名單引導特權機制本身不變）

## 日期
2026-05-08

## 上下文 (Context)
Touka 系統實施嚴格的 **Bootstrap SSOT** 與 **Path SSOT**，禁止在代碼中透過 `sys.path.insert` 或 `os.environ` 動態修改執行環境。這在業務邏輯層 (Service/Router) 是必要的，以防止環境污染。

然而，當子專案（如 `ToukaMemory`）需要以 **MCP (Model Context Protocol)** 模式單獨運作，或開發者需要直接執行某個進入點腳本時，若不手動設置 `PYTHONPATH`，系統常因找不到 `src/` 目錄而崩潰。強制使用者每次手動設置環境變數會降低開發體驗與系統的可攜性。

## 決策 (Decision)
我們決定建立 **「引導特權 (Bootstrap Privilege)」** 機制：
1.  **特定進入點豁免**：允許位於根目錄的進入點腳本（如 `run_server.py`, `main.py`, `awaken.py`）使用 `sys.path.insert` 將本地 `src/` 加入搜尋路徑。
2.  **白名單管控**：這些檔案必須顯式註冊於 `ruff.toml` 的 `per-file-ignores` 與 `lint_ssot.py` 的 `WHITELIST_FILES` 中。
3.  **邊界隔離**：此豁免僅限於「啟動引導階段」，一旦進入 `src/` 內部的業務代碼，嚴禁再進行任何路徑操作。

## 後果 (Consequences)
- **優點**：
  - **相容性**：確保 `ToukaMemory` 在 MCP 模式下可以「一鍵啟動」，無需外部配置。
  - **開發體驗**：開發者可以直接執行 `python run_server.py` 進行測試。
  - **法規明確**：將「引導邏輯」與「業務邏輯」解耦，讓 SSOT 執法更精確，而非一昧禁止。
- **缺點**：
  - 如果白名單過於膨脹，可能會導致路徑管理的混亂（需透過架構審計嚴格控制白名單長度）。
