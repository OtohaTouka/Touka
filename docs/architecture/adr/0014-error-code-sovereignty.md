# ADR 0014: 錯誤碼主權 (Error Code Sovereignty / Error SSOT)

## 狀態
已接受 (2026-05-06)

## 上下文 (Context)
在舊有的 Touka 系統中，錯誤訊息在後端是以字串形式硬編碼在 `HTTPException` 呼叫中，而在前端則是直接以字串字面量出現在 `toast.error()` 呼叫中。這導致了以下問題：
1. **合約漂移 (Contract Drift)**：後端更改了錯誤訊息，但依賴字串比對的前端邏輯因此損壞。
2. **UX 一致性差**：系統的不同部分使用不同的措辭回報相同的錯誤。
3. **審計困難**：無法靜態分析系統中所有可能的錯誤狀態。
4. **Agent 混亂**：自主 Agent 在生成新代碼時會建立自己的錯誤格式，違反了架構標準。

## 決策 (Decision)
我們實作了一套中心化的 **錯誤碼主權 (Error SSOT)** 機制：

1. **唯一事實來源 (Source of Truth)**：所有錯誤碼必須定義在單一 JSON 檔案中：`ToukaProxy/config/errors.json`。
2. **後端強制執行**：
    - 使用 `ToukaException(ErrorCode.NAME)` 代替 `HTTPException(detail="...")`。
    - `lint_ssot.py` 將靜態攔截任何使用硬編碼字串字面量的 `HTTPException` 或 `ToukaException`。
3. **前端強制執行**：
    - `scripts/sync_errors.py` 從主 JSON 生成 TypeScript 常數 (`ERRORS`)。
    - `guard_errors.ts` 靜態攔截 `toast.error` 和 `console.error` 中的硬編碼字串。
4. **格式規範**：每個錯誤回應必須遵循標準結構：
   ```json
   {
     "code": "ERROR_NAME",
     "id": 1234,
     "message": "人類可讀訊息",
     "i18n": "translation.key"
   }
   ```

## 後果 (Consequences)
- **正面影響**：全棧 100% 一致性。易於實作多國語言 (I18n)。自動化文件生成。透過唯一的錯誤 ID 改善除錯效率。
- **負面影響**：新增錯誤時有輕微的開銷（必須先更新 JSON）。需要遷移現有的硬編碼字串（前端 86+ 處，後端亦有多處）。
- **合規性**：由 `lint_ssot.py` 和 `guard_errors.ts` 強制執行。
