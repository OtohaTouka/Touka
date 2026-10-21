# ADR-0021: ABAC 安全等級體系與 HITL 決策矩陣 (ABAC Security Levels & HITL Matrix)

> **現況**：ReadOnly 終端白名單（ADR-0143 反轉）；四階矩陣與 MonitorLayer 降級閘（由 ADR-0246 取代）。

## 狀態 (Status)
✅ 已接受 (Accepted)

> 🔧 **§ReadOnly 終端白名單已被 [ADR-0143](0143-hitl-unification.md)（HITL 統一）反轉**：原「ReadOnly 僅允許 safe_whitelist 指令（ls/pwd）」→現行 `safety.py` ReadOnly 對 safe/dangerous 皆 DENY（只 read_only-class 工具放行）。四階矩陣、黑名單優先、auth_level 深傳、MonitorLayer 降級閘等其餘決策仍有效。

> 🔧 **§1 四階矩陣與 §3 MonitorLayer 降級閘已被 [ADR-0246](0246-model-layer-stage-2b-2c.md)（Manual／Auto／YOLO 三模式）取代**：四級改三模式，加上程式常數底線與 Auto 審查員；MonitorLayer 的降級結果會被 ToolLayer 用 session 的值覆寫、實際失效，已拿掉，模式授權改在 session 寫入。上一則所說「四階矩陣、MonitorLayer 降級閘仍有效」已不成立；黑名單優先、auth_level 深傳仍有效。

## 上下文 (Context)
在之前的實作中，系統雖然具備了基於策略的安全檢查 (Gate 2)，但存在以下問題：
1. **執行等級模糊**：`Manual` 與 `SafeAuto` 的邏輯在程式碼中重疊，導致「全手動模式」依然會自動執行白名單指令。
2. **缺乏動態靈活性**：使用者無法根據當前任務的風險程度，動態切換代理人的「自主權限」。
3. **安全鏈路斷層**：底層工具（如 `bash`）在執行時無法感知高層定義的安全等級，導致決策不一致。
4. **權限越界**：任何人都能發送高風險的安全等級請求，缺乏對「等級提升」的行政管理。

## 決策 (Decision)

### 1. 實施四階 ABAC 決策矩陣
定義全系統統一的執行安全等級，透過環境屬性 `auth_level` 動態驅動決策：
- **ReadOnly (唯讀)**：僅允許 `safe_whitelist` 內的指令，其餘 `DENY`。適用於不受信任的環境。
- **Manual (全手動)**：所有指令（含安全指令）均回傳 `CONFIRM`。確保使用者對每一行指令具備 100% 交互主權。
- **SafeAuto (半自動)**：白名單內指令 `ALLOW`，其餘 `CONFIRM`。平衡安全與效率。
- **FullYOLO (全自動)**：除了 `critical_blacklist` 內的指令 `DENY`，其餘一律 `ALLOW`。適用於受信任的長生命週期任務。

### 2. 執行鏈路深度透傳
- **ToolExecutor 擴展**：在工具調用鏈中，強制將 `auth_level` 注入 `call_kwargs`。
- **內核感知**：Terminal 工具等具備二次安全校驗的組件，必須使用傳入的 `auth_level` 進行策略決策，廢除內部硬編碼的預設值。

### 3. 安全等級變更守衛 (Auth Level Guard)
在請求進入 Pipeline 的第一時間（`MonitorLayer`）實施權限核驗：
- 檢查發起者（人類使用者）是否具備 `admin` 角色或是目標 Agent 的 `owner`。
- 若權限不足，系統會強制將請求等級降級為 `Manual` 並記錄審計日誌。

## 後果 (Consequences)
- **邏輯一致性**：精確定義了各等級行為，消除了 `Manual` 與 `SafeAuto` 的邊界模糊。
- **安全性提升**：防止了惡意使用者透過竄改請求參數獲取非法自動化權限。
- **自主性加固**：修復了 `FullYOLO` 失效問題，使 Agent 具備真正的無人值守自主工作能力。
- **可測試性**：建立了專屬的 `test_auth_level_matrix.py` 驗證全矩陣決策路徑。

---
*Created on 2026-05-10*
