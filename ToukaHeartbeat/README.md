# ToukaHeartbeat (智慧節律器 - Pacemaker)

這是一個外部喚醒模組，作為 Touka 生態系的 **「智慧節律器 (Smart Pacemaker)」**。它負責接收排程信號，並在通過自主預算檢查後，物理性地從外部「喚醒」AI 執行 Pulse 任務。

## 🔗 與記憶系統的關係 (ToukaMemory Integration)

**重要澄清**：本模組與 `ToukaMemory` 的**職責彼此獨立**，但 ToukaMemory 並不是獨立服務：
- **ToukaHeartbeat** (Port 8082)：排程與自主預算決策
- **ToukaMemory**（ToukaProxy 同行程 Python 函式庫）：長期記憶與檔案庫

**通訊方式**：
- Heartbeat → Proxy：透過 HTTP Webhook 通知喚醒事件
- Proxy → Memory：透過 Python 同行程直接掛載；工具調用由 `get_resource_proxy()` 取得 Proxy 注入的真實 `ResourceManager`
- **Memory 部署契約（ADR-0116：整合模式單一部署）**：無獨立 MCP 伺服器或 `_FallbackResourceProxy`。取不到 `ResourceManager` 時必須 fail-loud。
- **身分物件化 (Identity Anchoring)**：Heartbeat 的 API 邊界保持 `agent_id: str` 以確保跨容器兼容性，但在 Proxy 端會立即物件化。

## 🛡️ 為什麼需要這個模組？

- **安全性隔離**：即便代理人擁有修改全專案原始碼的權限，由於此模組可運行於獨立進程或容器外，能有效防止 AI 自行修改「自主預算限制」。
- **防範執行通膨**：透過外部監控，防止 AI 陷入自我觸發的無限循環。

---

## 🚀 安裝與啟動

產品使用 [根安裝指南](../README_QUICKSTART.md) 同一資格發行的 Heartbeat 映像。先完成 Proxy／init，再用新的 Compose invocation 建立 Heartbeat／Chat；不要依賴一次 `up` 的 `depends_on` 重新讀到剛產生的 `.env`。`HEARTBEAT_CONFIG_DIR` 是使用者先建立的目錄，產品設定檔由 StorageService 管理，不由安裝腳本播種或覆寫。

### 開發環境

由 repo root 執行 `uv sync --locked --all-packages`，啟用 root Python 3.12 venv；完整前提見 [CONTRIBUTING](../CONTRIBUTING.md)。`awaken.py` 仍是 source 入口：從 `ToukaHeartbeat` 目錄、在使用者明確準備的開發設定下執行 `python -B awaken.py`。這不是新增獨立 installer，也不是現行服務旁可直接啟動的另一份 production。

Token 原則維持：StorageService 優先採用 `HeartbeatSettings.HEARTBEAT_TOKEN`，再讀持久化 token，最後才由產品產生；Proxy 端受管 bootstrap 成功持久化後，另一個 Compose invocation 才載入共同 token。不要手動同步或輸出真 token；空／不一致憑證不得繞過。排程 API 的 Bearer 驗證與資料保護保持。

既有本子目錄 `docker-compose.yml` 使用 host network，僅為歷史獨立開發參考，**不是**本次 loopback GHCR 產品安裝入口；不自動執行或延伸其平台支援。



### 持久化與服務憑證（ADR-0230，來源候選）

新來源保留合法舊設定欄位與 presence；只確定缺檔時初始化，讀取／解析失敗保留原件並停止新排程。
持久化與 jobs 在同一交易協調，套用失敗有原 bytes／trigger／next_run_time 補償；無法恢復時顯示
degraded／recovery_required，不能以空排程或新 token 覆蓋原件。由使用者經受管途徑處理恢復，代理不直接管理設定。

管理 API、手動 trigger、ProxyClient 共用 TokenProvider。shared setting 非空才允許 outbound；
持久 fallback／產品生成可供本機管理，不能推論另一服務接受。未 ready 時 task／HTTP 都不建立。
更新憑證需重啟兩服務以載入同一持久 .env；沒有 hot rotation。請不要把真 token 放在命令列或回報中。

Chat 排程改走 Proxy 真人權限橋接，一般 user 限 owned agents，admin 全域；更新需 If-Match，
衝突須重新讀取，timeout 表示結果未知，成功但刷新失敗不能重送。詳[介面](../docs/architecture/HEARTBEAT_INTERFACE.md)。

### 4. 組件架構 (SOLID)
本專案已重構為解耦架構，便於單元測試：
- `awaken.py`: 主程式、API 路由與排程分發。
- `storage_service.py`: 負責 `config.json` 的持久化與 SSOT 管理。
- `proxy_client.py`: 負責與 `ToukaProxy` 的單次 HTTP 通訊。
- `schedule_service.py`: revision／jobs 交易與補償。
- `token_provider.py`: 管理與 outbound 共用的憑證解析。
- `models.py`: 純排程模型 SSOT，生成 Proxy 投影。

### 5. 測試驗證
本模組具備獨立測試套件：
- 單元測試: `pytest tests/unit/test_heartbeat_logic.py`
- 整合測試: `pytest tests/integration/test_heartbeat_api.py`

---

## ⚙️ 管理與自主預算 (Autonomy Budget)

本模組與 `AutonomyService` 深度連動：
- **配置主權 (Env SSOT)**：本模組已完全遷移至 Pydantic `BaseSettings` 配置體系。所有設定（如 Webhook 位址、Token 等）由 `config.py` 驅動，嚴禁直接訪問環境變數。此外，系統支援環境感知（Environment-Aware）的 `.env` 互斥載入機制：明示 `TOUKA_ENV_FILE` 時優先且不回退；未明示時保留原整合／獨立路徑選擇，避免設定衝突。
- **Slot 判定**：優先遵循 `autonomy.json` 中的全域配置。
- **預算守衛**：當每日自主次數達上限時，節律器將拒絕觸發新的喚醒請求。
- **降級機制**：若未指定 Slot 數，系統將自動套用「系統保底預算 (Default: 5 Slots)」。

---

## 🔒 部署建議

1. **實體路徑禁令**：請遵循 **TID251** 法律。
2. **獨立權限**：建議將此模組放置於代理人無法直接寫入的「受保護區域」。
3. **部署等價性 (Deployment Parity)**：Docker 映像標記必須包含所有重構組件。執行受管建置前務必通過 `guard_dependencies.py` 的實體完整性檢查。
4. **SSOT 原則**：排程資訊由 StorageService 管理的設定檔保存；`identity_map.json` 的 `capabilities` 是喚醒能力授權來源，並非排程資料。

---

## 🔄 運作流程
1. `ToukaHeartbeat` 排程器到達預定時間。
2. 檢查交易／degraded 與共享憑證狀態。
3. 驗證 Token 並比對 `AutonomyService` 剩餘預算。
4. 正式呼叫 `ToukaProxy/v1/heartbeat/awaken` 啟動自主脈衝。

---

## 🪟 PostHeartbeat Hook — 滑動視窗觸發

自 2026-05-21 起，`ToukaProxy` 的 `_process_heartbeat_loop` 在 `finally` 區塊中新增 PostHeartbeat hook：

- 所有 pulse completed（或失敗）後，若為**家對話模式**（`is_home_mode=True`），自動觸發 `SlidingWindowService.check_and_trim()`
- 子對話（`is_home_mode=False`）完全跳過
- Hook 失敗不影響心跳整體結果（異常被吞噬並記錄）

此行為由 `ToukaProxy/config/sliding_window.json` 控制（預設 `enabled: false`）。詳見 ADR-0035。

> **ADR-0078（當機復原可見化）**：`_process_heartbeat_loop` 起點於 `autonomy_log.json` 落盤 `heartbeat_in_flight` 標記、`finally` 清除；`ToukaProxy` 啟動掃描殘留標記 → `GET /v1/stats` 的 `interrupted_runs` → 前端「上次有 N 個未完成的背景任務」通知，可一鍵 `POST /v1/heartbeat/retrigger`（人類擁有者驗證、強制 Auto，**非**本服務的 service-token 排程路徑）重跑中斷的背景脈衝。

- **工作分派中心協作**：虛擬中介 `DispatchAgent` 雖不參與定時排程，但其被 Supervisor Agent 喚醒時的脈衝執行依然受到 `verify_heartbeat_token()` 與 `AutonomyService` 剩餘預算的監控，確保分派任務時的系統穩定性。

---
*Created by Antigravity — The Beat of Autonomy.*
