# ADR-0093: 前端國際化與社群翻譯流程 (i18n / l10n)

**狀態 (Status):** ✅ 已接受
**日期 (Date):** 2026-06-14
**決策者 (Deciders):** 專案擁有者 + 主程

## 上下文 (Context)

開源前盤點發現 ToukaChat 的 i18n **只有空殼、實際沒接通**：前端有一個自製 i18n
Context（`Language='tc'|'en'`、`t()` 僅做路徑查找、無內插/單複數），Provider 掛了、
設定頁有切換鈕，但**幾乎沒有元件呼叫 `t()`**——整個 UI 是寫死的繁體中文（~140 個含
中文檔、約 1,000+ 條使用者可見字串）。後端 `config/errors.json`（ADR-0014）每筆錯誤
已預埋 `i18n` key，但前端從未接上。

目標：以**成熟函式庫**做真正可用、且**可由社群貢獻語言**的 i18n。維護者決定：用現成
函式庫（不自建輪子）、**只做 client 端切換**（Touka 是自架應用軟體，SEO/分享連結價值
低，不做 SSR、不在 URL 帶語言）、要能開放社群翻譯。

## 決策方案 (Decision)

採用 **react-i18next**（生態最大、社群翻譯工具鏈最成熟），client-only，分五階段落地。

### 架構與 SSOT
- **翻譯目錄 SSOT**：`ToukaChat/src/locales/<lng>/translation.json`，一語言一檔、扁平
  JSON。**`zh-TW` 為來源語言**（作者撰寫、永遠完整）；**`en` 為國際參考語言**（必須
  100% 完整，社群多由英文翻起）；其餘為社群語言。
- **語言註冊表 barrel** `src/locales/index.ts`：export `SOURCE_LNG` / `SUPPORTED_LNGS`
  / `i18nResources`（由內部 `LOCALES` 衍生）。新增語言＝放檔 + 加一行 import/LOCALES；
  i18n init 與守衛 #33 都讀這份註冊表。
- **語意 key**（`settings.sliding_window_tab.title`），非以原文當 key。資料檔中的顯示
  標籤（`config.ts` 分頁、`tokens.ts` 主題名）改存 i18n key、由消費端 `t()` 渲染——
  **design SSOT `tokens.ts` 不動**，主題名以主題 key 動態組 `theme.names.<key>`。
- **內插/單複數**：`{{var}}` 內插；英文以 i18next `key_one`/`key_other` 處理單複數。
  文字夾 JSX 用 `<Trans>`（`<1>`/具名 component placeholder）。

### 關鍵組態決定
- **`fallbackLng: 'en'`**：英文為國際參考；社群語言缺 key 回退英文最通用。zh-TW 為完整
  來源，主使用者偵測即得 zh-TW、不觸發回退。
- **語言記憶**交由 `i18next-browser-languagedetector`（寫 localStorage `touka_lang`，函式庫
  內部）；本專案自有程式碼不直接觸碰裸 localStorage，仍符合 localStorage SSOT（ADR-0026）。
- ⚠️ **刻意不開 `nonExplicitSupportedLngs`**：它會把 `zh-TW` 收斂成不存在 bundle 的
  `zh`，導致 `t()` 全部回傳原始 key（已於 config 矩陣測試證實）。僅用 `supportedLngs`。
- **測試固定 `zh-TW`**（`src/test/setup.ts`）：既有測試以中文來源字串斷言，固定來源語言
  使其無需改動即續通過。

### 錯誤訊息橋接（errors.json）
前端 catalog 新增 `error.*`（涵蓋全部後端錯誤 `i18n` key + 前端 fallback）；新增
`errorMsg(code)` helper（用全域 `i18n.t`、`defaultValue` 回退至 SSOT 的 `msg`）；
`getErrorMessage` 與所有 `ERRORS.X.msg` 呼叫點改用 bridge。後端錯誤碼主權（ADR-0014）
不變，前端只補翻譯層。

### 守衛 #33（key-sync）
新增 `scripts/guard_i18n.ts`（前端守衛 32→33，併入 `guard_all.py`）：以 zh-TW 為基準，
驗證各語言檔——多餘/拼錯 key＝錯誤、`en` 不完整＝錯誤、社群語言缺 key＝警告（容漸進
翻譯）、複數 `*_one/_other`（base 在來源）允許、檔案系統↔註冊表一致。

### 社群流程
翻譯檔與程式分離；接 TMS（建議**自架 Weblate**，最搭 Touka 自架取向；替代 Crowdin
OSS 免費），志工網頁翻譯 → 自動開 PR → 守衛 #33 在 CI 把關。文件見
`docs/chat/CONTRIBUTING-translations.md`。

## 後果與影響 (Consequences)

- **全 UI 可在地化**：~770 條字串中英齊備、parity 自動驗證（`{{var}}`/`<Trans>` markup
  零落差）；新增語言「丟檔 + 一行」即生效。
- **社群可貢獻**：標準 i18next JSON + TMS 生態 + CI 守衛，降低貢獻門檻、防止 key 漂移。
- **錯誤訊息一致在地化**：消滅原本中英混雜的硬編錯誤訊息。
- **取捨**：翻譯檔隨語言增加打包體積（client-only bundle）；無 SSR 故首次載入以偵測語言
  渲染（自架應用軟體可接受）；函式庫不縮短「抽字串 + 翻譯」的大宗工，只升級正確性/
  維護性/生態。
- **退役**：自製 i18nContext 完全移除（knip 無孤兒）；原 homegrown ~100 個 catalog key
  經新 namespace 取代後清為孤兒並移除。
- **未在地化（刻意）**：持久化資料預設值（預設會話標題/模型設定，與邏輯耦合）、Next.js
  Server Component 字串（skip link、metadata；client-only 取向）、語言 endonym。

## Action Items
1. [x] **Phase 0**：react-i18next 地基——deps、`locales/{zh-TW,en}`、init（client-only、
   `nonExplicitSupportedLngs` 雷已證實並避開）、退役自製 i18nContext、切換鈕接
   `changeLanguage`。新 `i18n.test.ts`。
2. [x] **Phase 1**：Workflow 一檔一 agent 並行抽字串 + 主程集中合併 catalog；全 app UI
   中文進 catalog（含 `<Trans>`/內插/`config.ts` 分頁 key/`theme.names.*`）；測試固定
   zh-TW；HAX/a11y 源碼掃描守衛隨批再錨定至 i18n key。
3. [x] **Phase 2**：並行翻譯 → en；自動 parity 驗證（var/tag 零落差）；英文
   單複數；`fallbackLng` 改 `'en'`；holistic QA。
4. [x] **Phase 3**：語言註冊 barrel、**守衛 #33**（ja stub 實測 stale→FAIL/partial→WARN）、
   `errors.json` 橋接（`errorMsg` + 呼叫點轉換）、`CONTRIBUTING-translations.md`。
5. [x] **Phase 4**：本 ADR、`SSOT_MASTER_MAP` 更新、孤兒 key 清理、CHANGELOG/README 收尾；
   guard 33/33、vitest 全綠。TMS 實際 hosting 待擁有者。
