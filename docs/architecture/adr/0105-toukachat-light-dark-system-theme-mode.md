# ADR-0105：ToukaChat 亮/暗/跟隨系統 主題模式 — mode×accent 正交、next-themes 機制、per-mode 對比度守衛

- **狀態**：✅ 已接受（2026-06-21 經 TDD 分 4 批實作完成並全綠驗證：guard_all 37/37、vitest 684、smoke 24/24）
- **日期**：2026-06-21
- **影響範圍**：ToukaChat（`design-system/tokens.ts`、`lib/ThemeContext.tsx`、`app/layout.tsx`、`app/globals.css`、`scripts/sync-design.ts`、`settings/tabs/ThemeSettingsTab.tsx`、`components/ui/sonner.tsx`、`tests/guard/a11y_contrast.test.ts`、i18n catalogs）
- **關鍵字**：light/dark mode, theme mode, prefers-color-scheme, next-themes, FOUC, WCAG 2.2 AA, mode×accent 正交, 對比度守衛 per-mode, design-token SSOT, 設定頁 P3 Item 2, 修訂 ADR-0083

## 背景（Context）

設定頁批次審查（P3 Item 2）指出 ToukaChat 缺「亮/暗/跟隨系統」主題模式選擇。經 8 維唯讀測繪 + 3 決策評審 panel，判定此為 **ADR 級設計系統工程**，非快速修。

當前已驗證的現況：

- **永久暗色**：`app/layout.tsx:26` `<html lang="zh-TW" className="dark">` 寫死、且**無** `suppressHydrationWarning`。`body`（`:31`）為 `bg-slate-50 dark:bg-[#101922] text-slate-900 dark:text-slate-100`——其實**已有亮底 baseline + dark 覆寫**，只是被永久 `.dark` 遮住。
- **tokens.ts 暗色唯一**：`THEMES` 6 個 accent 主題（touka/rose/amber/emerald/violet/slate），每個含 `{name, primary, primaryButton, bg, surface, muted, accent, border, inputBorder}`，中性值全為暗色；`FORM_INPUT_BORDER = 'rgba(255,255,255,0.4)'`（白疊層，只在暗底成立）。
- **globals.css 無亮模式**：只有 `.dark` 區塊（`:72`）＋ `@custom-variant dark`（`:5`）；**無** `.light`、**無** `color-scheme`、**無** `prefers-color-scheme`。
- **執行期機制**：`ThemeContext.applyTheme()`（`:41–64`）以 `document.documentElement.style.setProperty()` 注入 accent 耦合中性色（`--background/--surface/--card/--border/--muted/--accent` 等）；文字角色變數（`--foreground/--muted-foreground`）則來自 globals.css `.dark` 區塊。**無 mode 狀態、無 OS 偏好偵測。**
- **next-themes 潛伏 bug**：`^0.4.6` 已裝，但**只有** `components/ui/sonner.tsx:9` `const { theme = "system" } = useTheme()` 使用它，Provider 從未掛載 → `useTheme()` 回 `{}` → toast 永遠用字面 `"system"`，不追蹤真實模式。
- **WCAG 守衛只驗暗色**：`tests/guard/a11y_contrast.test.ts`（vitest，非 `guard_all.py`）以 WCAG 公式實算 6 主題的四不變式，`mutedForegroundRgb()` 寫死從 `.dark` 區塊單一 match `--muted-foreground`，並假設 `THEMES` bg 為暗色。

ADR-0083 立的是「**暗色唯一**」的 WCAG 2.2 AA 基線。本 ADR **修訂其暗色唯一假設**，擴充為雙模式並維持同等對比度執法。

## 決策（Decision）

三個子決策，皆出自 8 維測繪 + 獨立評審 panel（信心 0.85–0.90）：

### 決策① — token 結構：mode×accent **正交**（＋ per-mode 文字角色覆寫）

中性色（bg/surface/muted/accent/border/inputBorder）抽成 **MODE 軸**，accent 只保留 hue：

```
NEUTRALS = { dark: {bg, surface, muted, accent, border, inputBorder},
             light: {bg, surface, muted, accent, border, inputBorder} }   // 共 2 套，全 6 accent 共用
ACCENTS  = { touka: {primaryDark, primaryLight, primaryButtonDark, primaryButtonLight}, ... }   // per-hue × per-mode
```

`applyTheme(color, mode)` 以 `{ ...NEUTRALS[mode], ...ACCENTS[color][mode] }` 合併注入。

**必要修正（評審揭出，否則守衛會紅）**：`primary` 同時擔任「bg 上的文字色」（守衛 line 80 驗 `primary` on `bg` ≥4.5:1），故 `primary`/`primaryButton` **不能跨模式共用單值**：

- 證據：`slate.primary = slate[400] (#7c8ba1)`（tokens.ts 註解自陳 `slate[500]` 當文字僅 3.97:1，已亮一階至 5.46:1）、`amber.primary = amber[500] (#f59e0b)`——在近白亮底會掉 **<4.5:1**。亮模式須降階（如 slate→`#475569`、amber→`#b45309`）。
- `inputBorder` 白疊層 `rgba(255,255,255,0.4)` 在亮底無效，須翻成黑疊層 `rgba(0,0,0,x)`；此為**中性耦合**，歸入 `NEUTRALS` 兩套各一。

淨增 ~12 個 per-mode accent 值，**仍遠少於**「每主題亮變體」的 12 整套中性。

**被否決：每主題亮變體（12 整套中性）。** 維護面 ×6、對不寫程式的維護者漂移風險高（每次調色都可能讓 12 個亮底之一悄悄掉破 AA）、強迫 `ThemeColor` union 6→12 爆炸、System 模式無乾淨歸宿。

### 決策② — 機制：**next-themes 管 mode、ThemeContext 續管 accent**

兩軸正交、各用最擅長的工具：

- `layout.tsx` 移除寫死 `className="dark"`、加 `suppressHydrationWarning`，掛 `<ThemeProvider attribute="class" enableSystem defaultTheme="dark">`（next-themes）。
- `ThemeContext.applyTheme` 變 **mode-aware**：訂閱 next-themes `resolvedTheme`，mode 變更時以正確中性套重跑注入（**一次 `useTheme()` 訂閱，非競爭系統**）。
- 收益：FOUC pre-paint inline script、`prefers-color-scheme` + 即時 OS 變更、SSR 水合（`suppressHydrationWarning`）**全部內建**；**順手修好 sonner.tsx 潛伏 bug**（Provider 掛上後 `useTheme()` 才有真值）；淨碼**更少**。
- **SSOT 不破**：next-themes 只持有 `<html>` 的 class 字串，**不持有任何色值**；`tokens.ts` 仍是色值唯一來源，`sync-design.ts` 與對比度守衛照舊讀它。

**被否決：全自製（擴充 ThemeContext + 手刻 FOUC inline script + matchMedia 訂閱 + 水合處理）。** 純加碼又少邊界覆蓋；不修 sonner bug（該 import 變死碼）；next-themes 淪為死依賴；首屏閃爍/水合回歸風險高，且守衛驗 token 不驗首屏時序、抓不到。

### 決策③ — 守衛：**參數化 mode×theme×invariant**

四不變式（白字 on primaryButton ≥4.5、primary-as-text on bg ≥4.5、muted-foreground on bg/surface/muted ≥4.5、inputBorder on bg/muted ≥3）是**模式無關的數學**，只有輸入 token 集與 per-mode muted-foreground 改變：

- `describe.each(['dark','light'])` 包現有 `it.each(themeEntries)`——四不變式寫一次、雙模式共用、永不漂移。
- `mutedForegroundRgb(mode)` 改 **block-scoped**：先框出 `.light{…}` / `.dark{…}` 區塊再 match `--muted-foreground`（否則新增 `.light` 後第二個定義會讓現有 regex 歧義/誤配第一個）；保留 not-null 斷言使缺值/非灰階大聲失敗。
- 亮模式 CTA 文字色**不一定是白**：白字不變式須改讀 mode 對應的 `--primary-foreground`（亮底可能要深字），否則誤紅/誤綠。

**被否決：複製測試檔 per-mode。** Fork 四不變式 → 日後改數學/閾值/alpha 須兩處手動鏡像，漏一次就出貨不可讀的亮模式組合（正是守衛要防的事）；且仍要改 `mutedForegroundRgb`，沒省到。

## 後果（Consequences）

- ✅ 工程量集中且可控：主要在 `tokens.ts` 結構重整、`globals.css` 加 `.light`、`ThemeContext` mode 訂閱、守衛參數化、`ThemeSettingsTab` 加模式控件、i18n。app 多數元件靠**語意 CSS 變數**（會隨變數翻轉自動正確），非靠大量 `dark:` 工具。
- ⚠️ **第二個 FOUC 源（既有，本 ADR 記錄不擴大）**：accent 注入跑在 client `useEffect`（水合後），首屏會短暫顯示 `:root/.dark` 預設值才被 effect 蓋掉。next-themes 只修 **mode-class** flash；此 accent flash 兩方案皆存，可另案以 inline-script / CSS 預設緩解。
- ✅ **硬寫 light 色點＝零修改（實測結論）**：實作時以 grep 全掃，發現 app **全面語意 token 驅動**——暗底 hex 寫死 0 處、寫死亮灰文字 0 處；45 個 `text-white` 全在 `bg-primary`/`hover:bg-primary-button` 深色按鈕上（兩模式皆白字正確）；3 個 `bg-white/bg-slate-50` 均為正確雙模式或開關鈕白點。這是長期 `guard_theme`/`guard_design` 主權守衛的回報：中性背景/文字隨 CSS 變數自動翻轉，無破版點需修。
- ℹ️ **sync-design.ts 主權（修正）**：`.dark`／`.light` 區塊為**手寫並列**，sync-design.ts **不涉**——它只重生 `@theme`／`:root` 兩個標記區塊。故 `.light` 為手寫；勿手改 `@theme`/`:root` 標記區塊內容（應改 tokens.ts 重生），亦勿移除 css-integrity 要求的 `--color-*` 映射。
- ⚠️ **修訂 ADR-0083**：其「暗色唯一 WCAG 基線」擴為雙模式。per-mode 文字角色 accent 覆寫**須明文記錄於本 ADR**，否則未來貢獻者把 per-mode primary 簡化回共用值，會悄悄重引 slate/amber 亮底文字失敗。

## Action Items（TDD 分 4 批執行完成，2026-06-21）

1. [x] **Batch 1** `tokens.ts`：新增 `LIGHT_THEMES`（與 `THEMES` **同 key 形狀**＝低血量；中性灰白 `LIGHT_NEUTRALS` 6 主題共用、per-accent 深階 `primary`/`primaryButton`、`rose[700]=#be123c` 新階、light `inputBorder` 黑疊層）。＊實作採「並列 LIGHT_THEMES」而非 NEUTRALS/ACCENTS 拆分，讓 sync-design/守衛/applyTheme 擴充而非重寫。
2. [x] **Batch 1** `globals.css`（**手寫**，與 `.dark` 並列；非經 sync-design.ts）：`.light` 區塊鏡像 `.dark`（`--foreground` oklch(0.21)、`--muted-foreground` oklch(0.50)、`--input` rgba(0,0,0,0.5)…）＋ `@custom-variant light` ＋ `color-scheme`（亮暗各補）。
3. [x] **Batch 2** 機制：`layout.tsx` 去寫死 `dark` + `suppressHydrationWarning` + 掛 `next-themes`（`attribute="class" enableSystem defaultTheme="dark" storageKey="touka_theme_mode"`）包住自製 ThemeProvider；`ThemeContext` 訂閱 `resolvedTheme`、`applyTheme(color, mode)` 分支 `THEMES`/`LIGHT_THEMES`。
4. [x] **Batch 2** `sonner.tsx`：潛伏 bug 隨 Provider 掛載自動修復（`useTheme()` 由回 `{}` 變回真值）。
5. [x] **Batch 1** 守衛 `a11y_contrast.test.ts`：`describe.each(['dark','light'])` 參數化 ＋ `mutedForegroundRgb(mode)` block-scoped ＋ 白字 CTA 兩模式皆白（深階實心按鈕）。先紅後綠；**48 斷言全綠**（2 模式 × 6 主題 × 4 不變式）。
6. [x] **Batch 3** `ThemeSettingsTab`：亮/暗/跟隨系統 分段控件（accent picker 之上，`aria-pressed`，呼叫 next-themes `setTheme`）；測試新增、既有斷言不破。
7. [x] **Batch 3** i18n：`theme_mode_heading`/`mode_system`/`mode_light`/`mode_dark` 進 zh-TW（source）＋ en；守衛 #33 綠（1018 key）。
8. [x] **Batch 4** 硬寫色點審計＝零修改（app 全語意 token 驅動）；真實瀏覽器驗證亮/暗機制（`html.light`/`color-scheme`/`.light` 變數逐項實測正確、無 console error/水合警告）。
9. [x] **Batch 4** 全綠關卡：guard_all **37/37**、vitest **684**、smoke **24/24**、ESLint 0 error、ruff N/A（零 .py）。＊人工 commit 待維護者指示；**六主題 × 雙模式實機美學驗收仍待維護者**（需後端的運行實例）。

## 建基於 / 關聯

- **修訂 ADR-0083**（ToukaChat WCAG 2.2 AA 暗色基線 → 雙模式；對比度守衛 per-mode 化）。
- 設計研究：8 維唯讀測繪 ＋ 3 決策評審 panel（mode×accent 正交、next-themes 機制、守衛參數化），2026-06-21。
- 沿用設計系統 SSOT 鏈：`tokens.ts` → `sync-design.ts` → `globals.css` → 對比度守衛。
