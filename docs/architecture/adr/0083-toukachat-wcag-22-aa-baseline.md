# ADR-0083: ToukaChat WCAG 2.2 AA 無障礙基線與對比度守衛

> **現況**：inputBorder 從單值改成雙模（ADR-0105 修訂）。

**狀態 (Status):** ✅ 已接受（設計 token 修訂 by [ADR-0105](0105-toukachat-light-dark-system-theme-mode.md)：inputBorder 單一全域值→加 light 模式 LIGHT_FORM_INPUT_BORDER 雙模；WCAG 2.2 AA 對比度基線本身不變）
**日期 (Date):** 2026-06-12
**決策者 (Deciders):** 專案擁有者 + 主程

## 上下文 (Context)

公開發佈前稽核確認 ToukaChat 不符 WCAG 2.2 AA：13 類違規、30+ 處（icon 按鈕無名、表單無
label、串流對螢幕閱讀器無聲、會話切換鍵盤不可達、自製彈窗無焦點陷阱、主色按鈕白字 3.45:1、
表單邊框 1.29:1、目標尺寸 <24px、零 reduced-motion 支援…）。違規集中於自寫元件（Radix 部分
天生合規），採五批修復（A1–A5），每批 TDD＋guard_all 全綠＋人工 commit。

## 決策方案 (Decision)

### 基線範圍（A1–A5 全數落地）
1. **A1 語意**：icon 按鈕 `aria-label`（14+ 處）；login/register sr-only label＋`autocomplete`＋
   真 h1；狀態訊息 live region（串流「正在產生回覆／回覆已完成」sr-only 宣告器＝節流策略，
   不逐 token 轟炸 SR）；六路由獨立標題（client page 經 segment layout）。
2. **A2 鍵盤**：會話切換鍵盤化；遮罩 Esc 替代＋`aria-hidden`；ToolStepTracker button＋
   `aria-expanded`；focus-visible ring；skip link；span 連結改 button。
3. **A3 彈窗**：新 `ui/dialog.tsx`（Radix 薄封裝、低主見、`overlayClassName`）；Evolution／
   ImportAgent／SoulStudio 三彈窗零視覺遷移（焦點陷阱／Esc／焦點歸位內建）。附帶修正：
   eslint「禁直引 Radix」規則對封裝層 `src/components/ui/**` 自相矛盾 → 範圍豁免。
4. **A4 視覺（兩項擁有者決策）**：
   - **決策①** 各主題加深階 CTA 底色 `primaryButton`（白字實算 4.70–7.58:1）：touka `#1d6fd1`、
     rose `#e11d48`、amber `#b45309`、emerald `#047857`、violet `#7c3aed`、slate `#475569`；
     SSOT 鏈＝tokens.ts → sync-design.ts → `--primary-button` → Button 元件＋45 檔實心 CTA 批次替換。
   - **決策②** 僅表單控件邊框提亮：`--input: rgba(255,255,255,0.4)`（最差襯底 3.62:1），
     `border-input` 只套 input/select/textarea；裝飾性卡片邊框維持。
   - slate 主題主色既有違規（3.97:1）→ 亮一階 `#7c8ba1`（5.46:1）。低 alpha 文字（/30–/70）
     全數轉正＋守衛禁令。目標尺寸 ≥24px。`prefers-reduced-motion` 全域＋`MotionConfig`。
5. **A5 實機驗證（Claude Preview＋axe-core 4.10）**：
   - 對當前源碼 prod build 與運行實例注入 axe 實掃，**抓到並修復 7 類靜態稽核漏網**：
     會話選單觸發鈕等 10 個無名按鈕（非 `size="icon"` 形態）、`opacity-30/70` 疊加文字實測
     不及格 3 處、導覽 active 態 4.44:1 差一線（wash /10→/5）、heading-order、雙 aside 地標
     無名、login/register 缺 main 地標、**`focus:not-sr-only` 在 Tailwind v4 失效**（skip link
     聚焦後仍 1×1px——改 globals `.skip-link` 自訂類）。
   - **nested-interactive 根治**：SessionItem 重構為「純容器＋標題真 `<button>`＋選單兄弟節點」
     （原 A2 的 row role="button" 含內部按鈕為 axe serious）。
   - 六主題截圖驗收（深階 CTA／亮邊框／slate 新灰階）；行動版 Esc 實測；NVDA 5 分鐘人工
     清單交付（`docs/chat/accessibility.md`）。

### 長期執法（守衛測試）
- `tests/guard/a11y_contrast.test.ts`：以 WCAG 公式**實算全部主題**之 CTA 白字／primary 文字／
  muted-fg／表單邊框配對（muted-fg 從 globals oklch 源頭解析）——未來任何調色不達標即紅。
- `tests/guard/a11y_basics.test.ts`：icon 命名／表單契約／路由標題／live region／鍵盤契約／
  modal 禁手寫覆蓋層／低 alpha 文字禁令／main 地標等源碼掃描。
- 行為測試：`A11yKeyboardA2`、`A11yModalsA3`、`AuthPagesA11y`。

## 後果與影響 (Consequences)

- 可見視覺變化（擁有者已驗收六主題截圖）：實心 CTA 深一階（含使用者訊息泡泡）、表單邊框
  明顯提亮、slate 灰階變亮、空狀態提示文字由 30% 透明改合格 token。
- `npm run build`／docker 重建後生效於部署實例；既有 docker 快照為 A4 狀態。
- 殘留（記錄不修）：NVDA 人工清單待擁有者執行；`lang="zh-TW"` 混語字串（3.1.2）待 i18n
  全面化；ESLint 既有基線（71 errors，較開工時 79 淨減 8）另案。
- 新增本機 preview 啟動設定（dev／static 兩組，autoPort；設定檔未入版控）。

## Action Items
1. [x] A1–A4 四批修復＋守衛（vitest 458、guard_all 31/31）。
2. [x] A5 axe 實機掃描＋7 類漏網修復＋SessionItem nested-interactive 根治。
3. [x] 六主題截圖驗收材料、行動版 Esc 實測、skip link Tailwind v4 修復。
4. [x] `docs/chat/accessibility.md`（基線聲明＋鍵盤速覽＋NVDA 清單）。
5. [x] 擁有者執行 NVDA 5 分鐘清單（2026-06-12）——**全數通過、無發現項**。
6. [ ] docker 重建後以 axe 對部署實例複掃一次（預期僅剩第三方/誤報級）。
