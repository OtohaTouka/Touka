# ADR-0010: 資源管理器 I/O 擴充與最終 SSOT 執法 (I/O Expansion & Final SSOT Enforcement)

## 狀態
✅ 已接受

## 日期
2026-05-04

## 上下文 (Context)
在之前的 SSOT 重構中，我們實施了基本的路徑隔離與 `ResourceManager` 代理。然而，部分複雜的 I/O 操作（如二進位寫入、CSV 追加、日誌旋轉追加）仍散落在業務邏輯中，違反了「物理路徑禁令 (TID251)」。此外，整合測試中發現身分物件在某些場景下未能完全取代 ID 字串，導致隔離機制存在邊界漏洞。

## 決策 (Decision)
1. **ResourceManager 全能化**：擴充 `ResourceManager` 介面，新增 `write_file_binary`, `append_file_text`, `append_csv_row` 等原子操作，使其成為全系統唯一的檔案寫入入口。
2. **IO 監獄最終執法**：徹底移除 `ToukaProxy` 業務邏輯中剩餘的 `open()`, `os.mkdir`, `pathlib.Path.write_text` 調用，並透過 `lint_ssot.py` 進行強制性靜態稽核。
3. **身分物件化邊界對齊**：修正 `ToolExecutor` 與各 `Handler` 之間的介面合約，確保所有工具調用皆強制攜帶並校驗 `AgentIdentity` 物件。
4. **測試沙盒大一統**：將所有單元與整合測試的沙盒環境收攏至 `global_test_sandbox` 邏輯中，並修正 LDH (Local Dynamic Handshake) 在測試環境下的預期行為。

## 後果 (Consequences)
- **正面**：達成了 100% 的路徑 SSOT 與身分隔離。系統現在具備完美的環境無關性 (Environment Agnostic)，可隨時在宿主機與 Docker 間切換而不失控。
- **正面**：靜態檢查工具 `lint_ssot.py` 現可精確斷言系統安全性。
- **負面**：測試代碼中的 Mock 邏輯變得更加複雜，需要處理更深層的 `ResourceManager` 委派路徑。
