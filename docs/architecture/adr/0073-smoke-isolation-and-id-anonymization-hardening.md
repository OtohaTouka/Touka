# ADR-0073: 冒煙測試隔離、SSOT 模型降級防禦與真實 ID 匿名化安全加固

> **現況**：`_clean_up_config` 自清機制已經沒有作用（ADR-0103 修訂）。

> **修訂註（[ADR-0103](0103-live-smoke-sandbox-anti-deadlock.md)）**：Decision#1 歸功的 `_clean_up_config` 自清機制已因 0103 的 smoke sandbox（import 前 `enable_smoke_sandbox()` 把 TOUKA_CONFIG_DIR 導向拋棄式 mkdtemp 目錄）而 vestigial——對本地生產組態的零污染現由 env 沙箱重導提供、非自清；其餘決策（動態模型降級防禦／真實 ID 匿名化）不變。

> **2026-08-14 修訂（[ADR-0214](0214-standalone-smoke-data-and-metered-api-boundaries.md)）**：
> 隔離由原本少數 full-lifespan smoke 擴為全部自動 smoke；runner 每 child 建 sandbox，腳本亦在
> product import 前自我驗證。真 provider probe 永久排除 automation。

## 上下文 (Context)
在 Touka 的日常演進與品質保障流程中，我們發現了以下幾個系統品質、資訊安全與靜態分析上的隱患：
1. **測試污染隱患**：創建引擎冒煙測試（`smoke_creation_engine.py`）在執行時，會在本地 `server_config.json` 中註冊測試用的臨時 MCP server。如果測試異常中斷，或沒有完善的後置清理機制，會導致測試配置殘留污染本地生產組態。
2. **動態模型預設降級洩漏**：前端模型選項頁面在重啟後，有時會多出無效的 `Model 1` 與 `Model 2` 預設占位模型。經分析，係因 `ModelManager` 載入設定或發現模型出錯時，系統會自動 fallback 降級至包含這兩個 placeholder 的 default 模型清單。
3. **敏感身分資訊洩漏**：系統的程式碼、測試（tests/）與歷史文件中，仍有部分硬編碼的真實 Agent ID。在專案開源或交付時，這可能造成 Agent 人格標識與隱私外洩。
4. **Pylance 靜態型別報錯**：在 `smoke_group_dispatcher.py` 中，初始化傳入的參數型別與內部定義不符，導致 pylance 發出 type mismatch 的紅字報錯，影響靜態分析健全度。

## 決策方案 (Decision)
為了徹底消弭上述隱患，維持全系統 **30/30 架構守衛與 18/18 冒煙測試 100% 綠色** 的高可用標準，我們採取了以下加固決策：
1. **冒煙測試自動自清洗（Self-Cleaning）**：在 `smoke_creation_engine.py` 中，使用 `resource_manager` 建立 `_clean_up_config` 機制。在測試啟動前與結束後，自動且物理性地清除測試中註冊的臨時 MCP 配置與臨時目錄，確保測試對本地生產組態零污染。
2. **模型發現 fallback 降級防線**：在 `ModelManager` 中建立安全發現機制，避免在發現為空或載入異常時隨意載入 default placeholder（Model 1/2），並釐清其成因以防止無效模型干擾生產環境。
3. **全系統真實 ID 匿名化（Anonymization）**：
   - 清查全專案 `docs/` 和 `tests/` 下的所有檔案。
   - 將所有硬編碼的真實 Agent 標識一律替換為中性化、模擬 ID（如 Alice, Bob, Charlie, DebugAgent 等）。
   - 落實「開源前不洩露人格識別資訊」的資訊安全加固。
4. **Pylance 靜態型別對齊**：修正 `smoke_group_dispatcher.py` 中調用 `__init__` 的 `request_id` 與 `session_token` 的引數型別（將錯誤傳入的 bool 值修正為符合型別定義的 str 或 None），使 Pylance 靜態檢查 100% 合規。

## 後果與影響 (Consequences)
- **環境與組態完全隔離**：冒煙測試的執行變得完全無害，不再干擾使用者的個人配置。
- **資訊安全再升級**：消除了身分識別敏感資訊外洩風險，為未來的系統開源與交付鋪平了道路。
- **代碼健全度提升**：排除了 Pylance 靜態型別的紅字，確保靜態分析工具能更高效地守護代碼品質。
- **守衛全綠**：成功通過 `guard_all.py`（30/30 Checks PASS）與全體 18/18 冒煙測試、1270+ pytest 單元與整合測試。
