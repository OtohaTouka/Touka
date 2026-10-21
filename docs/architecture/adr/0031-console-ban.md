# ADR-0031: CONSOLE_BAN — 前端 console 直接呼叫禁令

**狀態**: ✅ 已接受（機制細節修訂：文中 error_handler.ts 白名單機制已改行內 noqa（M7 測試斷言與 ADR 相反）；CONSOLE_BAN 前端日誌主權規則本身不變）  
**日期**: 2026-05-17  
**關鍵字**: console.log, console.warn, console.error, 日誌主權, LaC, 前端

---

## 背景與問題

後端已有 `PRINT_BAN` 禁止 `print()` 直接呼叫（ADR-0025），但前端缺少對稱規則。
前端生產代碼中存在 10 處直接 `console.log/warn/error()` 呼叫，無任何靜態守衛攔截。

直接呼叫 `console.*` 的問題：
- 訊息字串散落各處，無法統一 i18n 或格式化
- 繞過 `logSSOTError()` 的結構化錯誤封裝層
- 無法被監控系統追蹤

---

## 決策

建立靜態守衛 `ToukaChat/scripts/guard_console.ts`，掃描 `src/hooks/**`、`src/lib/**`、`src/app/**`、`src/contexts/**`。

**核心邏輯**（`scripts/lib/console_guard_logic.ts`）：
- AST 偵測 `CallExpression`，匹配 `/^console\.(log|warn|error)$/`
- 白名單：`error_handler.ts`（`logSSOTError()` 的 SSOT 封裝層，唯一授權呼叫 console 的出口）
- 排除：`.test.ts`、`.test.tsx`、`.spec.*`、`.stories.*`
- 豁免格式：`// noqa: CONSOLE_BAN`

**整合**：`guard_all.py` 第 17 道關卡（`npm run guard:console`）。

---

## 初始違規處置

初次掃描發現 10 處現有違規，全數以 `// noqa: CONSOLE_BAN` 豁免並記錄為「可修復（低優先）」，待逐步遷移至 `logSSOTError()`：

| 檔案 | 行 | 方法 |
|---|---|---|
| `src/hooks/useHeartbeatSchedules.ts` | L29 | console.error |
| `src/hooks/useLegacySync.ts` | L31 | console.error |
| `src/hooks/useLocalStorage.ts` | L15, L29 | console.error |
| `src/lib/mentions.ts` | L32 | console.warn |
| `src/lib/SystemStatusContext.tsx` | L106 | console.log |
| `src/contexts/AuthContext.tsx` | L38, L72 | console.error |
| `src/app/create/page.tsx` | L51 | console.warn |
| `src/app/login/page.tsx` | L25 | console.error |

---

## 後果

**正向影響**：
- 與後端 PRINT_BAN 對稱，前後端日誌主權一致
- 未來新代碼的 console 呼叫在 CI 階段被攔截
- 測試檔案（`.test.ts`）和故事檔（`.stories.tsx`）自動豁免，不產生誤報

**限制**：
- 10 處現有違規以 noqa 豁免，需未來逐步遷移
- 未覆蓋 `src/components/**`（由 `guard_hooks.ts` 的 DIP 守衛負責）
