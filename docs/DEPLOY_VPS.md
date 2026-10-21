# Touka VPS 雲端部署指南

本文件說明如何在個人 VPS 上部署 Touka 系統，使用 Docker Compose + nginx 反向代理。

> ⚠️ **重要安全提示**：Touka 預設設計為本機部署。暴露到公網前，請務必完成本文件的所有步驟，尤其是設定 `CLOUD_MODE=true` 和 nginx HTTPS。

---

## 前置條件

| 項目 | 建議規格 |
|------|---------|
| VPS 系統 | Ubuntu 22.04 LTS（或同等）|
| CPU / RAM | 2 核心 / 4GB（最低 1 核 / 2GB）|
| 磁碟 | 20GB+ |
| 對外埠 | 80 (HTTP)、443 (HTTPS) |
| 已安裝 | Docker、Docker Compose v2、nginx、Certbot |

### 安裝必要工具

```bash
# Docker
curl -fsSL https://get.docker.com | bash
sudo usermod -aG docker $USER && newgrp docker

# nginx + Certbot
sudo apt update && sudo apt install -y nginx certbot python3-certbot-nginx
```

---

## 步驟一：取得代碼

```bash
git clone https://github.com/YOUR_GITHUB/touka.git
cd touka
```

---

## 步驟二：設定環境變數

```bash
cp .env.example .env
nano .env   # 或使用任何文字編輯器
```

**必填項目：**

```bash
# 資料存儲路徑（建議使用 VPS 的絕對路徑）
SANCTUARY_DATA_PATH=/opt/touka/data
MEMORY_APP_DATA_DIR=/opt/touka/data

# JWT 簽名金鑰（隨機生成，絕對不要公開）
TOUKA_AUTH_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# Heartbeat Token（啟動後由系統自動生成，此處可留空）
HEARTBEAT_TOKEN=

# ── 雲端模式 ──────────────────────────────────────
CLOUD_MODE=true

# 你的域名（前端）
ALLOWED_ORIGINS=https://yourdomain.com
NEXT_PUBLIC_PROXY_URL=https://api.yourdomain.com
NEXT_PUBLIC_HEARTBEAT_URL=   # 通常不對外暴露，可留空

# Cookie 安全（公網必須設為 true）
SECURE_COOKIES=true
```

### 選配：啟用 MCP 互動式 OAuth（ADR-0206）

P8-A 預設關閉。只有在建立 `ToukaProxy/config/mcp_oauth_profiles.json` 並設定公開 HTTPS callback origin 後才啟用：

```bash
TOUKA_MCP_OAUTH_USER_ENABLED=true
TOUKA_MCP_OAUTH_CALLBACK_BASE_URL=https://api.yourdomain.com
TOUKA_MCP_OAUTH_CALLBACK_RESPONSE_MODE=form_post
TOUKA_MCP_OAUTH_KEY_SOURCE=managed
```

`managed` 會以 atomic `.env` writer 產生並持久化專用 32-byte key；多實例或外部秘密管理
請改用 `external` 並由部署端注入相同 key。不得重用 JWT、Heartbeat 或 Dispatch key。
公開 callback 必須是 HTTPS，且 nginx 範本中的兩個 exact callback location 必須保留
`access_log off`；複製設定後先跑 `nginx -t`。query callback 預設禁止，未完成 ingress
canary 不得把 `TOUKA_MCP_OAUTH_QUERY_CALLBACK_CANARY_VERIFIED` 設為 true。

**建立資料目錄：**

```bash
sudo mkdir -p /opt/touka/data
sudo chown -R $USER:$USER /opt/touka/data
```

---

## 步驟三：啟動服務

```bash
# 使用雲端覆寫設定啟動
docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d

# 確認服務狀態
docker compose ps
docker compose logs --tail=20
```

服務啟動後確認：

```bash
curl http://localhost:8000/health   # Proxy
curl http://localhost:3000          # Chat
```

---

## 步驟四：設定 nginx 與 HTTPS

```bash
# 複製 nginx 設定範本
sudo cp nginx/nginx.conf.example /etc/nginx/sites-available/touka

# 將所有 yourdomain.com 替換為你的實際域名
sudo sed -i 's/yourdomain.com/your-actual-domain.com/g' /etc/nginx/sites-available/touka
sudo sed -i 's/api.yourdomain.com/api.your-actual-domain.com/g' /etc/nginx/sites-available/touka

# 啟用設定
sudo ln -s /etc/nginx/sites-available/touka /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 取得 SSL 憑證
sudo certbot --nginx -d your-actual-domain.com -d api.your-actual-domain.com

# 確認 nginx 運行
sudo systemctl status nginx
```

範本只替「代理人資料重置」端點（`/v1/admin/agents/<id>/reset` 與根路徑）把讀取逾時拉長到 30 分鐘：勾選「一併清除備份裡的記憶」時，伺服器要逐份處理每一份快照，可能跑好幾分鐘。其他 API（含其他 admin）仍是 nginx 預設 60 秒。已經照舊範本架好的 VPS，要把這一段 location 補進去；沒補時清除超過 60 秒，重置卡會顯示「結果不明」（伺服器照樣跑完，稽核照樣寫）。

---

## 步驟五：首次系統設定

服務啟動後，管理員初始密碼會寫入**一次性檔** `ToukaProxy/config/INITIAL_ADMIN_PASSWORD.txt`（DA21；日誌只記錄該檔路徑、**不含明文**）。讀取方式：

```bash
# config 目錄通常以 volume 對映，從容器內讀取最保險
docker compose exec touka-proxy cat /app/config/INITIAL_ADMIN_PASSWORD.txt
```

> ⚠️ 此檔含 `email` 與 `password` 兩行。**請立即登入、修改密碼，然後刪除此檔**——首次改密或下次啟動（admin 已非臨時密碼）時系統也會自動清除它。

登入 `https://yourdomain.com` 後：
1. 使用 `admin@touka.io` 和檔案中的初始密碼登入
2. 前往設定頁面修改密碼（修改後該一次性檔即被自動刪除）

---

## 日常維護

```bash
# 更新代碼
git pull && docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d --build

# 查看日誌
docker compose logs -f touka-proxy
docker compose logs -f touka-heartbeat

# 停止服務
docker compose down

# 備份資料
tar -czf touka-backup-$(date +%Y%m%d).tar.gz /opt/touka/data
```

---

## 常見問題

**Q：啟動後無法連線**
- 確認防火牆放行 80 和 443 埠：`sudo ufw allow 80,443/tcp`
- 確認 nginx 正在運行：`sudo systemctl status nginx`

**Q：管理員密碼忘記了**
- 刪除 `ToukaProxy/config/users.json`，重新啟動服務會重新生成；新的初始密碼會再次寫入 `ToukaProxy/config/INITIAL_ADMIN_PASSWORD.txt`（不再印於日誌）

**Q：Heartbeat 無法喚醒 Agent**
- 確認 `HEARTBEAT_TOKEN` 已設定（或讓系統自動生成）
- 查看日誌：`docker compose logs touka-heartbeat`

**Q：更新後 Agent 記憶消失**
- 確認 `SANCTUARY_DATA_PATH` 在更新前後指向同一個目錄
- 資料存在 `/opt/touka/data`，不會因為重建容器而消失

---

## 安全建議

- 定期執行 `docker compose pull` 更新基礎映像
- 設定 fail2ban 防止暴力登入
- 定期備份 `/opt/touka/data`
- 避免在 VPS 上使用 `YOLO` 模式
