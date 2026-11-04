# Heartbeat規則

範圍：`ToukaHeartbeat/**`：排程與喚醒。

從根 `AGENTS.md` 搬來的全文，沒有改寫。跑 `python scripts/applicable_rules.py --print-rules` 看這次改動適用哪幾條，不必讀完整份。改規則見 [README](README.md)。

## 小節

### 1. 脈衝驅動架構 (Pulse-Driven Architecture)
* **發布前來源契約（ADR-0230）**：Heartbeat 的 storage／排程交易／TokenProvider 共用明確失敗狀態；真人排程經 Proxy fresh human 擁有權與 If-Match CAS，不重新啟用 Proxy 排程器。fallback 只供本機管理，shared setting 未就緒時不建立工作或 outbound；兩服務以共同持久設定重啟，不承諾 hot rotation。Guard32／37 只驗必要出貨範本／明列宣告；smoke 必須在 HB import 前強制 owned data/config 與 logging 邊界。
*   **ToukaHeartbeat (Port 8082)**：系統的**「智慧節律器 (Smart Pacemaker)」**。獨立負責排程、計時與脈衝強度 (pulse_count) 決策。
*   **ToukaProxy (Port 8000)**：**「執行大腦」**。被動接收 `/awaken` 指令，執行序列化多脈衝循環。絕對禁止在 Proxy 內加回主動排程邏輯。
