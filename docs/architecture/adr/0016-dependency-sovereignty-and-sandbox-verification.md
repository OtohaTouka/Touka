# ADR 0016: 依賴主權與沙盒化完整性驗證 (Dependency Sovereignty & Sandbox Verification)

> **現況**：requirements.txt 的依賴主權機制改成 uv workspace 單一 uv.lock（由 ADR-0117 取代）。

## 狀態
已通過 (Accepted)（依賴機制取代 by [ADR-0117](0117-uv-workspace-single-lock-migration.md)：per-子專案 requirements.txt 依賴 SSOT→uv workspace 單一 uv.lock；沙盒改 `uv sync --frozen --package`。依賴主權原則不變、僅載體更換）

## 背景
在 Touka 系統中，由於採用多子專案 (Proxy, Memory, Heartbeat) 且各自具備獨立的 `requirements.txt` 與 Docker 容器，經常發生「開發環境正常，但 Docker 打包後因缺少依賴而重啟」的問題。這通常是因為開發機已安裝了某個套件，但開發者忘了將其寫入專案的 `requirements.txt`。

## 決策
實施「依賴主權 (Dependency Sovereignty)」原則，將依賴完整性納入架構法規 (LaC)：

1.  **SSOT 來源**：各專案目錄下的 `requirements.txt` 為該執行環境所需依賴的唯一事實來源。
2.  **沙盒化驗證 (Sandbox Verification)**：新增 `scripts/guard_dependencies.py` 工具，在打包前建立完全隔離的虛擬環境 (venv)，並僅安裝 `requirements.txt` 中的套件來測試模組匯入。
3.  **匯入完整性測試**：建立 `tests/unit/test_imports_integrity.py` 作為核心法規，確保所有進入點與依賴注入型別在執行時期皆可解析。
4.  **緩存機制**：透過雜湊 (Hash) 校驗實施高效檢查，僅在依賴文件變動時執行重裝。

## 後果
- **優點**：
    - 將依賴錯誤的發現時間點從「部署後」提前至「開發期」。
    - 確保 `requirements.txt` 的絕對準確性。
    - 解決 Python 版本差異導致的啟動崩潰問題。
- **缺點**：
    - 首次執行或依賴變動時，沙盒建立與套件安裝需耗費額外時間。

## 相關文件
- [SSOT_MASTER_MAP.md](../SSOT_MASTER_MAP.md)
- [scripts/guard_dependencies.py](https://github.com/OtohaTouka/Touka/blob/main/scripts/guard_dependencies.py)
