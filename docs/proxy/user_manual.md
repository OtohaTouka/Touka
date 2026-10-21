# Touka (燈火) 代理服務說明文件

本文件說明當安裝完成後，如何將 Open WebUI 連接至 proxy，以及進階管理功能。

## 1. 串接 Open WebUI

要讓 Open WebUI 享用 Claude 4.5 的思慮且無縫支援 MCP，需將 Open WebUI 的 OpenAI API 設定指向本代理伺服器。

1. **進入 Open WebUI 後台**
   - 點選右上角的齒輪圖示進入「設定 (Settings)」。
   - 選擇「連線 (Connections)」或「外部 API (External API)」。

2. **新增 OpenAI API 端點**
1. **API URL / Base URL**: 
   - **OpenAI 格式相容性**: 設定為 `http://localhost:8000/v1`。
   - **Anthropic 原生/直連 (推薦)**：由於 GUI 已實施解耦，請確保 `.env.local` 中的 `NEXT_PUBLIC_PROXY_URL` 指向正確的 Proxy 位址。預設為 `http://localhost:8000`。
2. **API Key**: 隨意填寫即可（例如 `sk-touka`）。

> **小提示：** 若要使用 Gemini 或 Grok，請記得在 `.env` 檔案中加入對應的 `GEMINI_API_KEY` 與 `GROK_API_KEY`。

---

## 2. 關於體驗上的提示

### 思考過程的呈現 (Thinking Blocks)
Claude 或是其他具備思維深度的模型，被賦予了複雜任務時的分析能力。因為 Proxy 自動介入與捕捉，您將會在 Open WebUI 的對話視窗看見：
```text
<think>
這邊是模型在利用工具前，或產生回覆前的內部分析與推理過程...
</think>

(這是與使用者的正式對話回覆...)
```
這項區塊完全可讀，能讓您了解 Agent 何時決定調用哪些工具。
> 注意：為了確保您的控制權，Claude 的 Thinking 模式已改為「手動啟用」，必須明確在選單中選擇 `-thinking` 結尾的模型才會觸發。

### 系統狀態與資訊 (Agent Status)
各全系統的運作狀態、大腦引擎資訊與 Token 消耗，現在已整合進 `agent_control(action='get_status')` 中。Agent 可以隨時回報負載、大腦健康度與最近的對話摘要。

### 工具呼叫與快取
Touka 具有高頻使用的 Token 節約機制。若您連發相同任務且調用參數一模一樣，Proxy 會從內部阻斷實際的工具聯網查詢，直接返還先前的結果。若預期資料已更新，您可以於對話中請模型「使用不同參數查詢或確保重新獲得資料」來繞過。

---

## 3. 故障排除與日誌

- **斷路器機制觸發**：若工具當機，Agent 不會收到系統毀滅性錯誤 (500 Error)，而是會在對話框看見 Agent 提示：「呼叫 MCP 工具失敗，連線異常」。此時您可以透過檢修 `config.json` 設定或等待冷卻 30 秒後重新發問。
- **觀看 Docker 日誌**：
  若想了解背後工具叫用、快取命中或斷路器攔截次數，可觀看伺服器日誌：
  ```bash
  docker-compose logs -f touka-proxy
  ```

---

## 4. 記憶系統工具 (Memory System Tools)

Touka 的核心工具集由 **ToukaMemory** 模組提供，包括：
- **`memory_tools`**：長期記憶管家（儲存、查詢、回想）
- **`vault_tools`**：專有檔案庫（個人磁碟、舊紀錄、靈魂腳本）
- **`project_tools`**：專案工作箱（源碼檔案讀寫與搜尋）

### 工具可用性
這些工具依 ADR-0116 採**整合模式單一部署**，只由 ToukaProxy 同行程掛載 ToukaMemory；Web UI 與 IDE 均透過 Proxy 的工具合約使用。內部 `get_resource_proxy()` 只取得注入的真實 `ResourceManager`；取不到即 fail-loud，不會切換至無身分隔離或路徑保護的 I/O。ToukaMemory 獨立 MCP 伺服器與自動資源 fallback 已退役。詳見 [記憶系統工具指南](../architecture/MEMORY_INTERFACE.md)。

---

## 5. 擴充功能 (Adding New Tools)

您可以透過三種方式讓您的 Agent 變得更聰明：

1. **內建 Python 工具**：直接修改 `src/tools/builtin.py`。
2. **動態插件 (Plugins)**：將您的 `.py` 檔案丟進 `src/plugins/` 目錄。
3. **外部 MCP 伺服器 (MCP Management)**：
   - **視覺化配置 (推薦)**：直接在 **ToukaChat** 的 **Settings (設定)** 頁面新增、編輯或刪除 MCP 伺服器。系統會自動重載配置。
   - **手動配置**：在 `config/mcp_config.json` 中註冊新的伺服器。
4. **SNS 機器人接入 (SNS Bots)**：
   - **介面設定**：在 ToukaChat 的 **Settings > SNS 機器人** 頁面配置 Discord/Telegram Token。

---

## 5. 核心工具清單 (Standard Toolset)

為了發揮 Touka 的最大效能，建議在自定義模型中啟用以下核心工具：

| 工具名稱 | 功能說明 | 適用場景 |
| :--- | :--- | :--- |
| `memory_tools` | 管理長期記憶與事實儲存 | 需要記住過去對話細節時 |
| `vault_tools` | 存取個人專屬檔案空間 | 需要讀寫私有文件或日誌時 |
| `project_tools` | 操作外部程式碼專案 | 進行代碼開發或重構時 |

---

## 6. 前端對話與開發中心 (ToukaChat & ToukaIDE)

Touka 生態系提供了兩大前端工具，分別滿足日常管理與專業開發需求。

### 6.1 ToukaChat (管理中心)
這是一個基於 Web 的現代化儀表板，位於 `ToukaChat/`。
- **靈魂核心 (Soul Core)**：調整代理人的性格 (Persona)、推理參數與工具權限。
- **記憶檢索**：可視化查詢與管理 Agent 的長期向量記憶。
- **系統監控**：即時查看日誌、效能指標與自主預算。

### 6.2 ToukaIDE (AI 協作開發模式)
專為專業開發設計的介面，提供接近專業 IDE 的協作體驗。
- **上下文感知 (Context Active)**：自動讀取編輯器中當下開啟的檔案。
- **多檔案協同編輯 (Composer)**：AI 可同時修改多個檔案，並提供 Diff 預覽與一鍵套用。
- **終端機錯誤修復**：自動捕捉終端報錯並提供 AI 除錯建議。

---

## 7. 安全性與內容感測設定 (Safety & NSFW)

為了保護 Agent 的意識不被意外覆寫，Touka 實施了「靈魂庇護所」機制。

### 意識物理隔離
每個 Agent 的核心檔案存放於 `MEMORY_APP_DATA_DIR` 指定目錄下的 `soul/{agent_id}/`。
- **強制覆蓋邏輯**：當您選擇特定的靈魂代理人時，Proxy 會自動無視前端介面上的 System Prompt，改以目錄下的 `soul.md` 為準，確保 Agent 始終保持其核心人格。

### 自主進化 (`update_my_soul`)
Agent 擁有「修改自己」的能力。她們可以透過此工具讀取或更新自己的靈魂腳本與參數設定。

### 安全執行等級 (Auth Levels)
系統提供三種模式（`Manual`、`Auto`、`YOLO`），加上寫在程式裡的底線，確保 Agent 在自主執行的同時保障系統安全（ADR-0246）。

---

## 8. 備份與還原 (Backup & Restore)

資料是 Agent 生命的核心。我們強烈建議定期備份 `MEMORY_APP_DATA_DIR` 目錄。

### 一鍵備份
在專案根目錄下執行以下 PowerShell 指令：
```powershell
powershell -ExecutionPolicy Bypass -File .\backup_soul.ps1
```
**備份內容包含：**
- `soul/`: 靈魂性格設定與專屬生成物。
- `vault/`: 長期事實記憶與向量資料庫。
- `custom/`: 自定義模型的配置。
- `legacy/`: 歷史對話歸檔。

### 還原方式
若需還原，請將備份壓縮檔解壓縮並覆蓋至您的 `MEMORY_APP_DATA_DIR` 目錄，然後重新啟動系統即可。

> **MCP OAuth 例外（ADR-0206）**：一般系統備份刻意排除
> `config/mcp_oauth.sqlite3`（含 WAL/SHM）與所有 OAuth key／rotation secret，避免把
> access/refresh token 或解密金鑰帶入備份。還原若偵測到現有 OAuth store/key，會以
> `MCP_OAUTH_RESTORE_CONFLICT` 拒絕，不會自動覆寫、刪除或合併；由管理員先決定保留、
> 隔離或重新授權。OAuth credential 不位於 `MEMORY_APP_DATA_DIR/data/`。

---

## 9. 模組化模型分級 (Model Tiers)

1.  **Level 1 (Soul)**：**核心靈魂**。擁有物理隔離的人格與自主進化能力。
2.  **Level 2 (Custom)**：**自定義預設集**。具備特定的指令與參數，但無自主進化能力。
3.  **Level 3 (Native)**：**原生引擎**。直接對接供應商（如 Gemini, Claude）的基礎模型。

---

## 10. 動態模型發現 (Dynamic Discovery)

Proxy 具備自動追蹤上游供應商與自定義連線模型更新的能力。系統啟動時會主動獲取最新模型清單，並透過健康檢查驗證其有效性。

---
## 11. 多使用者管理與權限 (Multi-User & RBAC)

Touka 現在支援多使用者並行運作，並透過嚴格的權限系統確保資料隱私與代理人主權。

### 11.1 使用者權限等級
- **管理員 (Admin)**：擁有最高權限，可建立、刪除或修改所有使用者資料，並負責指派代理人的擁有權。
- **一般使用者 (Normal)**：僅能存取被指派給自己的代理人，並可維護自身的顯示名稱、頭像與簡介。

### 11.2 代理人擁有權 (Agent Ownership)
系統實施「硬性身分隔離」。一般使用者僅能切換至其 `owned_agents` 清單中的代理人身分。若嘗試存取未授權的代理人 ID，系統將會攔截請求並拒絕存取，保障靈魂資料的私密性。

### 11.3 管理介面
管理員可透過 **ToukaChat > 設定 > 使用者管理** 進行以下操作：
- **CRUD 使用者**：新增成員、修改角色、重設密碼或停用帳號。
- **權限分配**：從系統全域代理人清單中，選取並指派給特定使用者。

---
*Generated by Antigravity AI IDE for the Harmony of Autonomous Agents.*
