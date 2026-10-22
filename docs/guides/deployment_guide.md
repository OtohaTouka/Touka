# Touka 本機部署與開發入口

使用者安裝以 [根目錄 README_QUICKSTART](https://github.com/OtohaTouka/Touka/blob/main/README_QUICKSTART.md) 為正本，並取得所選 Release 相同 source 的文件與 Compose。來源開發組態、已發布三映像及既有受管服務升級是不同操作；不再宣稱兩種模式都一鍵零設定完成。

| 元件 | 本機產品角色 | Source 開發入口 |
|---|---|---|
| ToukaProxy | 8000；內含 Memory，supervisor 管理 Dispatch | root venv＋既有 run_proxy.ps1 |
| ToukaMemory | Proxy 同行程函式庫，不獨立開埠 | root uv workspace／sandbox tests |
| ToukaDispatch | Proxy 映像內部進程，loopback 8083 `/mcp` | 已存在的 server.py；不新增安裝器 |
| ToukaHeartbeat | 排程服務，loopback 8082 | root venv＋awaken.py |
| ToukaChat | Web UI，loopback 3001 | 受鎖 Node/npm＋npm run dev |
| ToukaIDE | VS Code 擴充，非 runtime image | npm tests/typecheck/compile/package，後兩者串行 |

## 已發行產品

需要 Compose 2.24.0+、Linux amd64 containers、實際符合政策的 local filesystem 與既存 bind 目錄；非 root runtime／四個 init 掛載／loopback 不得放寬。使用者手動建立自己的 data／heartbeat-config 目錄，`.env` 只在不存在時複製範本。GHCR 主機路徑和容器內路徑要分開，不能抄固定磁碟號的歷史設定。

三映像由 `.github/workflows/ghcr-publish.yml` 建置推送：手動觸發（`workflow_dispatch`）：一律發布 `sha-<commit>`，填入 `version` 時另發布該版號與 `latest`；建議以 `.env` 的 `TOUKA_VERSION` 釘固定 tag。先建立 Proxy／init、確認 healthy 和受管 secret 持久化，再用新的 Compose invocation 建立 Heartbeat／Chat；不能從 depends_on 推定同次 env_file 會重新載入。不在這份導覽重複一組可能漂移的啟動指令，完整命令見根正本。

MCP 只使用 modern `/mcp`，舊 `/sse` 退役；固定本機 ports 不表示跨主機或公網資格。Azure／VPS 與多架構不在支援範圍。

## 開發環境

[CONTRIBUTING](https://github.com/OtohaTouka/Touka/blob/main/CONTRIBUTING.md) 指定 Python 3.12 root venv、uv.lock、Node.js 24.21.0／npm 11.19.1。uv 必須 --all-packages，安裝既定版本再加 --locked；npm lifecycle 關閉，Chat 顯式執行 gen:artifact-runtime 後才測試／build。CPU torch 已在 uv.lock，兩個舊裸 pip installer 已退役。

根 docker-compose.yml 是 source 開發組態，Proxy／Heartbeat 子目錄 Compose 則是舊的獨立開發參考；不得當成已發布本機 profile 的等價替代。沒有 installer 的元件不新增。是否保留獨立安裝策略在整體更新完成後再評估，本輪不作新支援承諾。

## 現有服務與回復

在原部署仍運行時，不直接拉 latest、prune 或 down -v。先確認目前 active project 與 image tag，由擁有者備份 data、`.env`、heartbeat-config 與 proxy-config volume，再改 `TOUKA_VERSION` 依根正本分兩階段重新 up；回復時把 `TOUKA_VERSION` 改回舊 tag 並還原備份。

全部自動化測試只跑 sandbox/mock；source 測試全綠不代表已執行真實新使用者安裝。
