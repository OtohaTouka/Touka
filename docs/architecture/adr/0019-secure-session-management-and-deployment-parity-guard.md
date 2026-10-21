# ADR 0019: 安全會話管理與部署等價性守衛 (Secure Session & Deployment Parity)

## 狀態
已通過 (Accepted) - 2026-05-10；2026-08-01 修訂 Guard #16 pytest-residue preflight

## 上下文 (Context)
在系統演進過程中，我們遇到了兩個關鍵的邊界問題：
1.  **會話碎片化 (Session Fragmentation)**：由於 `refresh_token` Cookie 的路徑不一致，且前端跨來源請求 (CORS) 未正確攜帶憑證，導致使用者登出後仍能自動重新登入。
2.  **部署環境不對等 (Deployment Parity Mismatch)**：在 `ToukaHeartbeat` 的重構過程中，由於 `Dockerfile` 採用了白名單式手動複製，導致新模組在容器環境中缺失，而本地測試環境卻因持有完整物理目錄而誤判為通過。
3.  **建置上下文遭測試殘留阻斷**：Windows 上顯式、repo-local 的 pytest `--basetemp` 留下受限 ACL 目錄，使 BuildKit 在讀取 root build context 時先於 Dockerfile 執行失敗。這不是依賴或 Dockerfile 內容錯誤；需要在 pytest 產生殘留時預防，並在 Guard #16 的任何依賴快取工作前 fail-fast。

## 決策 (Decisions)

### 1. 會話主權與 Cookie SSOT
*   **路徑統一**：全系統（Login/Logout/Refresh）之 `refresh_token` Cookie 必須強制設定為 `path="/"`。
*   **憑證透傳**：前端 `fetchTouka` 必須統一開啟 `credentials: 'include'`，確保瀏覽器在跨來源 API 呼叫時能正確處理 HttpOnly Cookie 的發放與銷毀。

### 2. 部署等價性與物理守衛
*   **Docker 資源策略**：微服務之 `Dockerfile` 應優先採用 `COPY . .` (搭配 `.dockerignore`) 的「黑名單」模式，而非單一檔案的「白名單」模式，以確保代碼與部署環境自動同步。
*   **部署守衛 (Deployment Guard)**：在 `guard_dependencies.py` 中新增靜態分析邏輯，驗證 `Dockerfile` 中的 `COPY` 範疇是否與本地 Python 模組匯入鏈匹配。
*   **pytest basetemp 預防**：root `tests/conftest.py` 載入 `pytest_basetemp_plugin.py`。只要 pytest 收到顯式 `--basetemp`（CLI、`PYTEST_ADDOPTS` 或 pytest 設定），就以 pytest cwd 為相對路徑基準；解析後等於 repo root 或位於其下者拒絕。Windows 的 drive-relative、UNC、reparse parent、解析失敗或其他無法證明在 repo 外的情況一律 `PYTEST_BASETEMP_UNCERTAIN` fail-closed；可證明位於 repo 外的路徑允許。未顯式指定時仍沿用 pytest 的 OS-temp 預設。
*   **Guard #16 規範合約（本 ADR 為 SSOT）**：`guard_dependencies.py` 必須先呼叫 `pytest_residue_safety.scan_pytest_residue()`，且在建立、重建或讀取依賴 sandbox cache 前完成。掃描範圍只限 repo-local `.pytest_cache`，不得擴張成「整個 Docker context 可讀」的宣稱；實際 Docker build 仍是較廣的第二道證明。
    * 掃描只讀 metadata，開啟一般檔案後只做 `fstat` 身分／權限驗證，**不讀任何內容**。
    * symlink、Windows junction／任意 reparse point、hard link、identity race、解析或存取錯誤皆為決定性 violation；不得遍歷 redirect，因此不會進入 root `data/` 或 `ToukaProxy/data/`。
    * 診斷只輸出必要的 repo-relative path 與穩定 code，不輸出檔案內容或不必要的絕對路徑。
    * Guard 只偵測與阻擋，不自動改 ACL、不刪除或搬移殘留；復原是 owner 核准的獨立人工維運程序。

## 後果 (Consequences)
*   **優點**：解決了登出失效的安全漏洞；消除了「本地測試通過、部署失敗」的風險。
*   **影響**：開發者在新增專案根目錄下的 `.py` 檔案時，需注意是否被 `.dockerignore` 排除；前端 API 呼叫必須遵循憑證傳遞規範；所有顯式 pytest basetemp 必須位於可證明安全的 repo 外路徑。
