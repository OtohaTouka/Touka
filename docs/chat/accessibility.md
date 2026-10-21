# ToukaChat 無障礙（Accessibility）基線

**標準**：WCAG 2.2 AA（W3C）
**建立**：2026-06-12（ADR-0083；分批修復 A1–A5）
**狀態**：✅ 基線達成——靜態守衛執法中＋axe 實機掃描通過＋NVDA 人工驗證通過（2026-06-12，全清單無發現項）

---

## 已達成的基線

| 面向 | 內容 | 長期執法 |
|---|---|---|
| 可及名稱 | 所有 icon-only 按鈕具 `aria-label`（含會話選單、視圖切換、模型選擇器） | `tests/guard/a11y_basics.test.ts`（源碼掃描） |
| 表單 | login/register 程式化 label＋`autocomplete`＋真實 heading | 同上＋`tests/auth/AuthPagesA11y.test.tsx`（渲染驗證） |
| 狀態宣告 | 串流「正在產生回覆／回覆已完成」sr-only 宣告器；監控元件 `role="status"` | 守衛掃描 |
| 鍵盤 | 會話切換為原生 `<button>`（無巢狀互動）；Esc 關行動側欄；skip link；focus-visible ring | `tests/unit/A11yKeyboardA2.test.tsx` |
| 彈窗 | 三個自製 modal 遷移 Radix Dialog（焦點陷阱／Esc／焦點歸位） | `tests/unit/A11yModalsA3.test.tsx`＋守衛 |
| 對比 | 六主題深階 CTA 底色（白字 ≥4.5:1）、表單邊框 ≥3:1、低 alpha 文字禁令 | `tests/guard/a11y_contrast.test.ts`（WCAG 公式實算全主題） |
| 目標尺寸 | 訊息操作鈕／分支切換／附件關閉 ≥24×24px（WCAG 2.5.8） | 程式碼註解＋審查 |
| 減少動態 | `prefers-reduced-motion` 全域降級＋framer-motion `MotionConfig reducedMotion="user"` | 守衛掃描 |
| 頁面結構 | 各路由獨立標題、地標具名（兩側欄 aria-label）、標題層級修正 | 守衛掃描 |

## 鍵盤操作速覽

| 操作 | 按鍵 |
|---|---|
| 跳過側欄直達主內容 | 頁面載入後第一下 `Tab` → `Enter` |
| 切換會話 | `Tab` 至會話列 → `Enter` |
| 會話選單（重命名／匯出／刪除） | `Tab` 至「會話選項」鈕 → `Enter` |
| 關閉行動版側欄／彈窗 | `Esc` |
| 訊息送出 | 輸入框 `Enter`（`Shift+Enter` 換行） |

---

## 🎧 NVDA 5 分鐘人工驗證清單（待擁有者執行）

> 工具無法代聽螢幕閱讀器——以下由真人完成。NVDA 免費：https://www.nvaccess.org/download/

1. **安裝並啟動 NVDA**（`Ctrl+Alt+N`；暫停朗讀 `Ctrl`）。
2. **登入頁**：開 `/login` → `Tab` 巡欄位 → 應聽到「Email 編輯區」「Password 編輯區」（**不是只有「編輯區」**）；密碼管理器應能自動填入。
3. **主介面著陸**：應聽到頁面標題「Touka Console」；第一下 `Tab` 應聽到「跳至主內容 連結」。
4. **送訊息（核心驗證）**：輸入訊息送出 → 生成開始時應聽到「**正在產生回覆**」、結束時「**回覆已完成**」（A1 串流宣告器）。
5. **會話列**：`Tab` 至左側欄會話 → 應聽到「會話標題 ＋ 按鈕」；當前會話應帶「目前項目」語意；旁邊應聽到「會話選項：〈標題〉 按鈕」。
6. **彈窗**：開「技能進化授權」或匯入對話彈窗 → 應聽到標題朗讀；`Esc` 關閉後焦點應回原處（NVDA 會重唸觸發鈕）。
7. **背景活動**：有背景脈衝時，側欄「背景跑中 脈衝 N/M」變化應被動態朗讀（status 區域）。

❌ 任一步沒聽到預期內容 → 記下步驟編號回報，列入下一批修復。

## 已知殘留（記錄於 ADR-0083）

- ~~NVDA 人工驗證~~ ✅ 已於 2026-06-12 由擁有者完成，全清單通過（上方清單保留作回歸驗證用）。
- 3.1.2 混語字串（`lang="zh-TW"` 下的英文 UI 詞）——影響極低，待 i18n 全面化一併。
- ESLint 既有基線（71 errors，與 a11y 無關）另案清理。
