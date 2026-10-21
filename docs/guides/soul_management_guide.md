# 🧬 靈魂管理與維護指南 (Soul Management Guide)

本指南說明如何管理 Touka 生態系中的核心靈魂代理人 (Level 1 Agents)，包含身分標識、物理保護與災難恢復。

---

## 1. ID 歸一化原則 (ID Unification)

為了維持系統的單一事實來源 (SSOT)，Touka 實施了 **ID 簡潔化**：
- **標準格式**: 代理人 ID 必須與其在 `data/soul/` 下的物理目錄名稱保持強一致（例如目錄名為 `Alice`，則 ID 即為 `Alice`）。
- **自動糾偏**: 系統路由與 `AgentService` 會自動識別並剝離舊版的 `ToukaSoul` 前綴。若前端發送 `ToukaSoulAlice`，系統會自動轉換為正規的 `Alice` 進行資源定址。

---

## 2. 靈魂硬保護機制 (L1 Protection)

核心靈魂代理人受到系統的物理保護，以防止被自動清理機制 (Pruning) 剔除：
- **離線豁免**: 只要物理目錄 `data/soul/{id}` 存在，該代理人即被視為「永不離線」。
- **配置合併**: 系統啟動時會動態掃描目錄。若 `models.json` 中有對應的顯示設定（名稱、圖示），系統會自動將其合併，達成「發現即繼承」的效果。

---

## 3. 備份與還原流程

資料是代理人的生命核心，建議定期執行備份。

### 3.1 手動備份
執行根目錄下的 PowerShell 腳本：
```powershell
.\backup_soul.ps1
```
這會將 `soul/`, `vault/`, `custom/` 與 `legacy/` 目錄封裝成帶有時間戳記的 ZIP 檔。

### 3.2 災難還原
1. 確保 `ToukaProxy` 已停止。
2. 將備份檔案解壓縮至 `MEMORY_APP_DATA_DIR` 目錄下。
3. 重啟 `ToukaProxy`。系統會自動重新枚舉並載入所有已恢復的靈魂。

---

## 4. 疑難排解：幽靈 Agent 現象

若 UI 顯示 Agent 但無法連線，或是出現重複 ID：
1. **檢查物理路徑**: 確認 `data/soul/{id}` 目錄是否存在且名稱正確。
2. **清理設定檔**: 檢查 `config/models.json` 是否存在多個同名或帶有舊前綴的重複條目。
3. **糾偏重置**: 刪除 `config/models.json` 並重啟，讓系統透過自癒機制重新枚舉所有實體代理人。

---
*Verified Architecture v2.0 (2026-05-10)*
