# ToukaProxy 安裝與開發

## 產品安裝正本

完整步驟見 [根目錄快速入門](https://github.com/OtohaTouka/Touka/blob/main/README_QUICKSTART.md)；執行時選取已發布 Release 的 **相同 source** 文件與 Compose，不混搭 main／舊映像。Proxy、Heartbeat、Chat 是由同一 source 建置的三映像（`.github/workflows/ghcr-publish.yml`），Proxy 已整合 Memory 與 Dispatch；沒有獨立 Memory MCP 安裝。

本機 profile 僅發布 loopback 的 8000／8083／8082／3001，runtime 為 Linux amd64、非 root UID/GID 1001。Docker Compose 至少 2.24.0；主機支援以該發行的實測為準，不以泛稱 Linux/macOS/Windows 推導全部相容。

部署者手動建立自己的 data／heartbeat-config 目錄並確認 filesystem 與權限，保留 unknown 的 fail-closed 語義；不透過開發腳本 mkdir 或修改既有資料。`.env` 不存在才複製範本，既有設定不可覆寫。GHCR 的 host source 使用 SANCTUARY_DATA_PATH，container 內 MEMORY_APP_DATA_DIR 由 Compose 指定；不要複製以前的固定磁碟路徑、額外 Memory source 掛載或獨立 Compose 片段。

先啟動 Proxy 及 init，確認 init exit0／Proxy healthy 與受管 token 持久化成功；再以新 Compose invocation、--no-deps 建立 Heartbeat／Chat，讀取更新的 env_file。一次 up 的 depends_on 不能證明此跨服務 token 同步。初始管理員登入、持久化位置與後續 UI 設定都依根指南，不將密碼寫進共享輸出。

## Source 開發

依 [CONTRIBUTING](https://github.com/OtohaTouka/Touka/blob/main/CONTRIBUTING.md) 使用 root uv workspace（Python 3.12）及鎖定 Node/npm。`uv sync --locked --all-packages` 包含 CPU torch，不另跑 pip 的 torch／CUDA／ROCm 選單。

既有 `ToukaProxy/setup.ps1`／`setup.sh` 只是同一 uv 佈建的便利入口，按 script location 定位 root，不初始化 .env、config、data 或 Docker。`run_proxy.ps1` 是明確的 Windows native 開發 launcher，使用 root venv、loopback、有效 PORT、--no-access-log 並透傳失敗碼。沒有 run_proxy.sh，也不新增。真正啟動產品會使用受管設定；所有開發驗證必須先隔離為 mock/sandbox。

原 `ToukaProxy/docker-compose.yml` 為單模組開發參考，與已發行 GHCR 的掛載／env／ports 不同，不是新使用者產品安裝指令。`scripts/sync_dev_venv.py` 與 `backup_soul.ps1` 已不存在，不能繼續引用；本頁不引導降低整機 PowerShell 政策或停止／終止未知服務。

## 失敗與既有部署

缺目錄、filesystem unknown、init 權限失敗、缺憑證或埠衝突都先停止並保留錯誤，不改 root、移除安全檢查或清資料重裝。正常使用者可在自己選定的隔離環境處理前提；開發代理不直接管理 data/.env。

升級依根指南：由資料擁有者先備份 data、`.env`、heartbeat-config 與 proxy-config volume，再改 `.env` 的 `TOUKA_VERSION` 並分兩階段重新 up；回復時改回舊 tag 並還原備份。不要 down -v、prune 或執行不存在的備份腳本。

本輪文件／腳本測試為 sandbox，未執行實體 fresh-user 安裝；是否維持各子系統分開安裝留待整合完成後評估。
