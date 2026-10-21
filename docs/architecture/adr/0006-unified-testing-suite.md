# ADR-0006: 大一統測試套件 (Unified Testing Suite)

## 狀態
已接受 (Accepted)（措辭修正：實為單一 session-scoped 沙盒＋per-test 清理隔離，非「各測試獨立臨時目錄」；`ToukaChat/tests` 前端 vitest 為範疇外例外。統一 root/沙盒護實體資料的核心保證不變）

> **2026-08-14 修訂（[ADR-0214](0214-standalone-smoke-data-and-metered-api-boundaries.md)）**：
> pytest／Vitest 的嚴格沙盒不變；同一保證擴及全部自動 standalone smoke。實體資料唯讀與
> DebugAgent 狹義寫入例外只屬人工 live probe，不得套用於自動化。

## 日期
2026-04-19

## 上下文 (Context)
原本各子專案（Proxy, Memory）各自持有測試代碼，導致整合測試時路徑管理混亂，且難以實施全系統級別的安全沙盒驗證。

## 決策 (Decision)
將全系統所有邏輯、安全性與整合驗證測試統一收攏於根目錄 `tests/`。
1. 實施「自動化沙盒」：透過 `conftest.py` 確保每個測試都在獨立的臨時目錄運行。
2. 廢除子專案內部的 `tests/` 目錄。

## 後後果 (Consequences)
- **優點**：
  - **一處調研，全域綠色**：簡化了 CI/CD 與開發者的測試流程。
  - **安全性保證**：強制性的沙盒隔離防止了測試過程對實體資料的破壞。
- **缺點**：
  - `conftest.py` 變得相對複雜，需要處理多種環境變數重定向。
