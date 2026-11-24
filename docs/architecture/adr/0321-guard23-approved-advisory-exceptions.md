# ADR-0321：Guard #23 的公告例外（維護者核准、會到期）

- 狀態：✅ 已實作。維護者 2026-11-18 核准（選「現在做例外清單」）。2026-11-24：唯一一筆例外移除（公告已撤回），清單目前是空的。
- 日期：2026-11-18
- 決策者：維護者
- 相關：[ADR-0222](0222-dependency-audit-gate-hardening.md)（依賴稽核閘；本 ADR 修訂 Guard #23 的判定）、[ADR-0299](0299-dependency-cooldown-and-emergency-patches.md)（冷卻期與緊急修補、每日檢查）
- 影響範圍：`scripts/guard_node_cve.py`、`tests/unit/test_guard_node_cve.py`、`SECURITY.md`、`docs/architecture/NOQA_REGISTRY.md`、`docs/handover/tests.md`

## 背景

2026-11-17 深夜，picomatch 的弱點公告 GHSA-fc5q-39xc-vjpf（high，一種讓程式卡住的正規式問題）改成影響到 4.0.7 以下，npm 上最新就是 4.0.7，沒有修補版可以升。ToukaChat 與 ToukaIDE 的 full 範圍因此有 28 筆 high，都是開發與測試工具（vite、vitest、knip、fast-glob、打包工具 vsce 等）帶進來的；runtime 範圍兩邊都是 0。

維護者 11/08 定過：外部公告讓 Guard #22、#23 變紅時，不放寬，等依賴修好才合併，人類只決定先修還是等。這次沒有修補版可修，等下去所有要進 dev 的合併都會停住。當時另外記了一個降低等待的做法：在守衛裡放一份維護者核准、寫明公告編號、有到期日的例外清單。維護者 11/18 選了這個做法。

## 決定

- 例外寫在 `scripts/guard_node_cve.py` 的 `ADVISORY_EXCEPTIONS`：每筆寫明一則公告編號（GHSA）、核准時的嚴重度、核准時間、到期時間與核准者。改這份清單要經一般的審查與合併流程。
- 只在 full 範圍（含開發與測試工具）生效；runtime 範圍（`--omit=dev`，跑起來的 Touka 會用到的）從不套用，所以同一則公告只要出現在 runtime 範圍就照樣擋。
- 一個 high／critical 套件只有在「順著 `via` 追到的所有 high／critical 公告都在有效的例外裡」才放行。同一個套件還有別的 high 原因、`via` 指到報告裡沒有的套件、公告項目沒有網址或網址裡不是剛好一個編號、`via` 形狀讀不懂，都不放行。moderate 以下的原因不影響判定（本來就不擋）。
- 追到的公告嚴重度比核准時高（例如期限內被改成 critical），或套件本身的嚴重度比追到的原因高，都不放行。`via` 裡不認得的嚴重度算讀不懂；公告項目的網址必須正好是 `https://github.com/advisories/<GHSA>`。
- 例外格式不對就不生效：編號不合 GHSA 格式、核准的嚴重度不是 high 或 critical、時間沒有時區、核准到到期超過 `MAX_EXCEPTION_DAYS`（14 天）或不是正的、沒寫核准者。
- 只在核准時間到到期時間之間生效（核准時間 ≤ 現在 < 到期時間）。核准時間寫成未來（例如年份打錯）就不生效、照擋，不會變成從現在一直放行到那個未來的到期日，所以「最多 14 天」是實際的生效上限。到期（現在時間 ≥ 到期時間）就不再生效，守衛照常變紅。測試不看今天的日期，到期由守衛本身提醒（同 ADR-0299）。
- 輸出：
  - 開頭列出每筆例外是有效（`[EXCEPTION]`，含核准的嚴重度）、還沒開始（`[WARN] … starts`）還是已到期（`[EXPIRED]`）。
  - 放行的套件以 `  excused <嚴重度> <套件> <公告>` 列出；擋下的維持原本的 `  high <套件> …` 格式。每日檢查（`dep_audit_report.py`）只數後者，筆數和 `[FAIL] N` 對得上。
  - 有有效例外時，通過的那一行寫「例外以外沒有 HIGH/CRITICAL」。
- 一次執行只取一次現在時間，四個範圍看到的例外一致。守衛不載入專案程式、拿不到 `time_utils`，所以 `_current_time` 直接用 `datetime.now(UTC)`，以 `# noqa: TIME_SSOT_VIOLATION` 豁免並登記在 NOQA_REGISTRY（這條規則第一處豁免）；測試以 monkeypatch 換掉。
- 第一筆：GHSA-fc5q-39xc-vjpf，2026-11-18 核准，原訂到 2026-11-25 結束。這則公告 2026-11-18 晚上被撤回（GitHub 標 withdrawn、仍沒有修補版），npm audit 從此不再回報，例外沒有東西可放行；2026-11-24 移除，清單目前是空的。

## 沒選的做法

- **繼續等修補版**：沒有修補版時會無限期擋住所有合併，維護者沒有選。
- **改 lock 換掉或覆寫 picomatch**：沒有不受影響的版本可換；降版會碰到更早的公告。
- **npm audit 的忽略選項或 `.npmrc` 設定**：npm audit 沒有依公告忽略的選項；寫進 `.npmrc` 也會影響安裝，而且 Guard #23 會比對 `.npmrc`。
- **整個 full 範圍降成只警告**：會連其他公告一起放掉。

## 後果

- 例外期間，開發與測試工具裡的 picomatch 仍有這個弱點。要利用它，攻擊者得能提供比對規則（glob），而這些工具用的規則都來自 Touka 自己的設定檔，所以風險低。runtime 範圍若出現同一則公告，守衛照樣擋。
- 守衛全部通過時，`guard_all.py` 只顯示通過，不顯示例外那幾行；要看例外得看 Guard #23 自己的輸出或這份清單。到期後守衛變紅，每日檢查會開 issue。
- 「runtime 範圍 0 筆」不等於跑起來的 Touka 完全沒有 picomatch：Next 在 `next/dist/compiled/picomatch` 內嵌了一份，npm audit 看不到；它用在圖片網址比對，規則來自設定，而 Chat 設了 `images.unoptimized: true`、正式映像是靜態匯出，實際沒有影響。另外，「runtime 範圍＝會出貨的程式」是前提：打包工具會打包任何被匯入的套件，不分 dev 或 prod；picomatch 和帶它進來的套件（fast-glob、micromatch、tinyglobby 等）目前只被測試與守衛匯入；Chat 的 `globals.css` 會把 dev 套件 tailwindcss、tw-animate-css 編進出貨的 CSS，那裡沒有 picomatch（2026-11-18 審查查證）。
- 已知限制：保留的證據（`--evidence-dir`）沒有記下當時的時間、有效例外與放行的套件，事後只看 npm 原始輸出會看到「有 high 卻通過」；被放行的套件身上若還有 moderate 公告，期間內不算進 lower-severity 計數。
- 例外只在合併了本 ADR 的分支生效：`main` 在維護者把 dev 合進去之前，每日檢查仍會因這則公告開 issue。
- 修補版出來後照緊急通道升級，並刪掉這筆例外；公告撤回、npm audit 不再回報時也刪掉（第一筆就是這樣）。

## 驗證

- `tests/unit/test_guard_node_cve.py`：
  - 有效例外在 full 範圍放行整條依賴鏈、runtime 範圍不放行；
  - 有別的 high 原因照擋、moderate 原因不影響；
  - 到期（剛好到期、已過期）照擋；核准當下就生效、核准時間在未來時不生效；比核准時嚴重（公告改成 critical、套件比原因嚴重）照擋；同一則公告重複登記取較嚴的；讀不懂的原因（報告裡沒有的套件、沒有網址、網址不是 GitHub 公告頁、兩個編號、不認得的嚴重度、怪形狀、空 `via`、沒有 `via`）與只有 moderate 原因的 high 套件照擋；`via` 有循環時只追一次；
  - 格式不對的例外不生效（在有效期間內檢查，含嚴重度）；
  - 實際清單的格式（不看日期）；開頭列出有效與到期的例外、通過那一行的說法；
  - 每日檢查解析時只數擋下的、筆數完整；
  - 四個範圍用同一個時間。
- 2026-11-18 實際跑 Guard #23：28 筆都只追到 GHSA-fc5q-39xc-vjpf，全部放行，守衛通過；runtime 範圍兩邊都是 0。
