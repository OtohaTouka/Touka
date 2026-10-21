# ADR-0042: Legacy History Compatibility and Error Sovereignty — 歷史對話資料與轉換相容性及錯誤碼主權加固

**狀態**: ✅ 已接受
**日期**: 2026-05-24
**關鍵字**: migrateFlatToTree, TreeHistory, Custom Exception, RAISE_STRING_BAN, Error SSOT

---

## 背景與問題

隨著系統的快速演進，對話歷史資料結構與錯誤防禦體系面臨了兩大兼容性與合規性挑戰：
1. **舊版歷史對話載入崩潰 (M2/M6)**：在對話樹 `TreeHistory` 機制上線前，有大量的歷史對話是以扁平數組格式存儲。當前端讀取這些舊數據並運行 `migrateFlatToTree` 時，如果數組中存在缺失 `id` 或未關聯 `parentId` 的節點，轉換邏輯將無法正確建立樹狀雙向連結。這會使 `currentId` 被解析為 `null`，進而引發前端 React 視圖渲染白畫面崩潰。
2. **後端異常處理違反字串禁令**：在後端 Pydantic 模型（如 `TreeHistory`、`SlidingWindowConfig`）及低層通訊管理層（`ProtocolManager`）中，原本使用原生 `ValueError("...")` 拋出錯誤。這違反了後端 `RAISE_STRING_BAN`（禁止 raise 硬編碼字串）與 Ruff 的 `TRY003` 規定，需要大量使用 `# noqa` 進行行級豁免，阻礙了後端異常架構的強型別化。

---

## 決策

為了保證歷史數據的完美兼容並消除靜態代碼違規，我們實施了對話樹遷移升級與異常強型別化重構：

### 1. 歷史對話遷移演算法優化 (M2/M6)
重構前端 `migrateFlatToTree` 轉換管道，改為四階段的強健流：
- **階段一：標識補全**。先拷貝扁平數組，若有節點缺失 `id` 則自動為其注入 `crypto.randomUUID()`，保證每個節點擁有唯一識別符。
- **階段二：父節點回溯**。遍歷處理後的數組，若節點無 `parentId` 則動態指派前一節點的 `id` 作為父節點，使扁平數組能正確平滑降級為線性單支樹狀。
- **階段三：雙向關聯**。安全地根據 `parentId` 在對照表 `messages` 中為父節點追填 `childrenIds` 列表，重建雙向指針。
- **階段四：當前指針兜底**。若所有節點均有子節點（無葉節點），將 `currentId` 安全退回至數組最後一個元素的 `id`，絕不回傳 `null`。

### 2. 後端 Pydantic 與協議層自定義異常重構
全面廢除 `ValueError("...")` 與相應的 `// noqa` 豁免，在後端引進階層式強型別異常：
- 建立專屬異常基類與其子類：
  - `HistoryValidationError` -> `CurrentIdNotFoundError`
  - `SlidingWindowValidationError` -> `SlidingWindowKeepRoundError` / `SlidingWindowLimitRoundError`
  - `ProtocolError` -> `ServerNotFoundError`
- 所有硬編碼錯誤字串均封裝至對應子類的建構子內部。模型驗證器中改為 `raise CurrentIdNotFoundError(self.currentId)` 等強型別呼叫。

### 3. 前端/後端錯誤碼 ID 重複修復 (H5)
- 排查並修復前端 `error_codes.ts` 內重複定義的錯誤碼（SSOT 衝突），並配合 `sync_errors.py` 與 `openapi.json` 進行同步，確保前端與後端錯誤代碼字典完全一致。

---

## 影響

- **歷史對話免崩潰防護**：用戶載入過去任何版本的歷史會話時均能穩定渲染，舊數據自動轉換為線性樹狀結構，保障了 UX 的連續性。
- **後端異常架構合規與擴充**：徹底移除了 `models/history.py`、`models/sliding_window.py` 與 `protocols/manager.py` 內的行級 noqa 標記，後端靜態守衛 100% 綠燈，且讓應用層可以根據異常型別進行精確的例外攔截（例如 FastAPI 的 exception_handler）。
