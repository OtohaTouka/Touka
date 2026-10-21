# ADR-0038: Init Container Pattern for Volume Permissions and Config Self-Healing — 初始化容器解決掛載權限與組態自癒

> **現況**：heartbeat 單檔自癒改成目錄掛載（由 ADR-0150 取代）。

**狀態**: ✅ 已接受
**日期**: 2026-05-24
**關鍵字**: Docker Compose, Init Container, Permission Denied, Host Volume, IsADirectoryError, Self-Healing

> 🗑️ **heartbeat 單檔自癒/播種機制已被 [ADR-0150](0150-heartbeat-config-dir-mount.md)（設定改掛目錄）取代**：compose 現以 `${HEARTBEAT_CONFIG_DIR}:/app/config` 掛**目錄**、`touka-init` 改 `mkdir -p`＋遷移舊單檔＋播種 config.json（避免「目錄疊單檔」啟動崩潰）；舊「單檔 bind-mount 自動建目錄」邏輯已改。init 容器解決掛載權限的核心模式仍有效。

---

## 背景與問題

系統部署中存在兩大與 Docker Host Volume 掛載相關的物理衝突問題：

1. **非 root 使用者執行與 Host Volume 權限衝突 (Permission Denied)**：
   為了安全考量（P8 規範），`touka-proxy` 以非 root 使用者 `appuser` (uid 1001) 執行。然而，當 `docker-compose.yml` 掛載宿主機的 `./ToukaProxy/config` 與 `./data` 到容器內時，若該目錄在宿主機上的擁有者非 uid 1001，當 `AuthService` 試圖寫入建立 `users.json` 時，將觸發 `PermissionError` 崩潰，導致容器陷入無休止的重啟循環。

2. **單檔案掛載不存在導致 `IsADirectoryError`**：
   在 `touka-heartbeat` 服務中，`docker-compose.yml` 掛載單個檔案 `./ToukaHeartbeat/config.json`。若啟動時該檔案在主機上不存在，Docker Daemon 預設會在主機上建立一個同名「目錄」掛載進容器，導致容器內的 Python 程式碼讀取該路徑時因 `IsADirectoryError` 崩潰。

由於非 root 執行身分限制，容器內程序在掛載後無法在無 root 權限下動態 `chown` 主機目錄或刪除掛載目錄點，這是 Docker 部署的經典限制。

---

## 決策

我們決定引入並擴充 **Init Container (初始化容器) 模式**，利用 Docker Compose 的啟動生命週期拓撲與權限隔離，在業務容器啟動前自動解決權限與目錄衝突。

### 1. 宣告 `touka-init` 服務
新增一個以 `root` 執行、基於極輕量 Alpine 映像檔的初始化容器：
```yaml
  touka-init:
    image: alpine:latest
    container_name: touka-init
    user: "root"
    volumes:
      - ./ToukaProxy/config:/app/config
      - ${SANCTUARY_DATA_PATH:-./data}:/app/data
      - ./ToukaHeartbeat:/app/heartbeat
    command: >
      sh -c "
      chown -R 1001:1001 /app/config /app/data || true;
      chmod -R 775 /app/config /app/data || true;
      if [ -d /app/heartbeat/config.json ]; then
        rmdir /app/heartbeat/config.json || rm -rf /app/heartbeat/config.json;
      fi;
      if [ ! -f /app/heartbeat/config.json ]; then
        cp /app/heartbeat/config.json.example /app/heartbeat/config.json || true;
      fi;
      chown 1001:1001 /app/heartbeat/config.json || true;
      chmod 664 /app/heartbeat/config.json || true;
      "
```

### 2. 移除錯誤目錄與自癒機制
藉由掛載 `ToukaHeartbeat` 的父目錄 `/app/heartbeat`，`touka-init` 能夠在宿主機 config.json 被掛載為單檔案之前，以 root 權限安全地將被 Docker 錯誤生成的 `config.json` 目錄刪除，並自 `.example` 模板完成檔案自癒，保證單檔案隨後掛載成功。

### 3. 下游服務啟動拓撲
`touka-proxy` 與 `touka-heartbeat` 設定 `depends_on` 依賴，確保在 `touka-init` 執行成功並順利退出 (`service_completed_successfully`) 後才安全啟動：
```yaml
    depends_on:
      touka-init:
        condition: service_completed_successfully
```

---

## 影響

- **100% 自動化與零設定**：使用者部署時無須在宿主機上手動執行任何 `chown` 命令，也不用預先手動建立 config 檔案，所有權限配置與組態自癒完全自動化完成。
- **維持非 root 安全防禦**：業務容器 `touka-proxy` 與 `touka-heartbeat` 保持以非 root 使用者安全執行，僅在啟動極短暫的 init container 中使用 root 做權限對齊，防禦性與安全性達到生產環境最高標準。
- **跨平台相容**：加入 `|| true` 保障在 Windows Host volume (WSL2 / Docker Desktop) 虛擬掛載系統下，即使 `chown` 無效或失敗亦不會阻擋容器鏈的成功引導啟動。
