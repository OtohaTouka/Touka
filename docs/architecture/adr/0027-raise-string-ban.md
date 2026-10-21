# ADR-0027: RAISE_STRING_BAN — 後端 raise 硬編碼字串禁令

**狀態**: ✅ 已接受（實作位置修訂 by [ADR-0156](0156-react-core-extraction.md)：文中 raise 修復點 gemini_handler.py 已隨 react-core 抽取移至 gemini_adapter.py；RAISE_STRING_BAN 規則本身不變）  
**日期**: 2026-05-17  
**關鍵字**: 錯誤碼主權, raise, ValueError, RuntimeError, ToukaException, SSOT

---

## 背景與問題

現有 `ErrorSovereigntyRule` 僅攔截 `HTTPException(detail="...")` 的情形，但 `raise ValueError("...")` / `raise RuntimeError("...")` 帶硬編碼字串的寫法同樣違反 AGENTS.md 第 18 節「嚴禁在 Exception 中使用硬編碼字串」：

```python
# 違規
raise ValueError("Cannot identify Gemini model")  # 繞過 ErrorCode SSOT

# 合規
raise ToukaException(ErrorCode.MODEL_NOT_FOUND, detail_override="Context...")
```

繞過 `errors.json → ErrorCode → ToukaException` 的 SSOT 鏈路，導致錯誤訊息無法跨端同步、無法 i18n。

---

## 決策

在 `lint_ssot.py` 中：
1. 在 `BaseRule` 新增 `check_Raise()` 空鉤子（OCP：不修改現有規則）
2. 在 `SSOTGuard` 新增 `visit_Raise()` 分發器
3. 新增 `RaiseHardcodedStringRule`（Rule ID: `RAISE_STRING_BAN`）

**禁止清單**：`ValueError`、`RuntimeError`、`TypeError`  
**偵測對象**：第一個位置參數為 `ast.Constant`（字串字面量）或 `ast.JoinedStr`（f-string）  
**不禁止**：傳入變數（`raise ValueError(msg)`，AST 無法靜態判定值）、`KeyError`、`NotImplementedError`、`ConnectionError` 等  
**路徑過濾**：僅掃描 `/src/` 目錄（排除 tests/、scripts/）

---

## 違規處置結果

| 處置 | 檔案 | 說明 |
|---|---|---|
| **修復** | `handlers/gemini_handler.py:92` | → `ToukaException(ErrorCode.MODEL_NOT_FOUND)` |
| **修復** | `services/dispatchers/utils.py:83` | → `ToukaException(ErrorCode.MODEL_NOT_FOUND)` |
| **noqa** | `models/history.py:28` | Pydantic model_validator，ToukaException(HTTPException) 語義不符 |
| **noqa** | `protocols/manager.py:106` | 低層協議錯誤，語義不符 |

---

## 後果

**正向影響**：
- 所有業務錯誤訊息走 `errors.json → ErrorCode → ToukaException` 鏈路，可跨端同步
- 違規可在 CI/CD 中被 `lint_ssot.py` 攔截

**限制**：
- 使用 `msg = "..."; raise ValueError(msg)` 變數模式不被 AST 偵測，為已知靜態分析邊界
- ToukaMemory 獨立容器無法 import ToukaProxy 的 ToukaException，這些使用保留 noqa
