# 測試規則

範圍：`tests/**` 與靜態檢查測試用的合成樣本。

從根 `AGENTS.md` 搬來的全文，沒有改寫。跑 `python scripts/applicable_rules.py --print-rules` 看這次改動適用哪幾條，不必讀完整份。改規則見 [README](README.md)。

## 小節

### 17. 測試基導大一統與虛擬診斷員 (Unified Testing & Virtual Debugger) ⭐
*   **測試主權唯一化 (Test Sovereignty)**：全系統測試統一收攏於根目錄 `tests/`。
*   **DebugAgent 職責大一統 (Identity Consolidation)**：全系統唯一的開發診斷與自動化測試身分定位為 `DebugAgent`。
*   **實體工具調度能力 (Real-time Execution)** ⭐：除錯介面必須具備「真實工具調用」能力。透過 `/v1/debug/execute` 實體執行指令並將結果注入聊天流，實現「開發、除錯、驗證」的閉環。
*   **動態角色提升 (Dynamic Role Handshake)** ⭐：系統具備「開發者友善」的握手機制。在受信任的本地網域下，自動將 `DebugAgent` 識別為 `Debugger` 角色。
*   **Full-lifespan smoke 反死鎖 (ADR-0103)** ⭐：standalone smoke 若用 `with TestClient(app)` 觸發完整 lifespan，**必須**在 import 任何 `src` 之前呼叫 `lib.sandbox_env.enable_smoke_sandbox(...)`。否則真實重量級啟動（背景 bots／動態模型發現）會在 **Windows ProactorEventLoop** 留下殘留任務/執行緒，使 teardown 死鎖、卡死整套（無 Linux CI 故易漏）。`run_all_smoke_tests.py` 已有每測逾時防呆作為安全網。
*   **自動 smoke 全隔離（ADR-0214）** ⭐：所有 `smoke_*.py` 都屬 `AUTOMATED_SANDBOXED`，runner 必須為每個 child 建立獨立 sandbox，腳本也必須在任何 `src`／`memory_server` import 前自行啟用並驗證 sandbox。自動 smoke 禁止載入真實 `.env`、provider／搜尋 API key 或下載模型；真實供應商連線只能放在不符合自動 discovery pattern 的人工 live probe。
