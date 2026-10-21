# ADR-0219：ToukaIDE Webview 純文字安全邊界

- 狀態：✅ implementation committed（Owner `HUMAN_ONLY` DEV-B；commit `94aa17976b7262bdda54df98dc1254dae072590c`）；本機lightweight `v0.2.0`同指該commit
- 日期：2026-08-30（2026-09-02 狀態同步）
- 決策者：Owner
- 影響範圍：ToukaIDE Webview CSP、動態內容渲染、0.2.0 使用者介面、Marketplace 候選與測試
- 相關決策：[ADR-0202](0202-toukaide-node-v2-streamable-http.md)、[ADR-0218](0218-toukaide-first-marketplace-release.md)

## 背景

ToukaIDE `0.2.0` 的 Webview 曾從未釘版本的 jsDelivr URL 執行 `marked.min.js`，CSP 同時允許
外部 script、connect 與 inline script。使用者訊息、歷史訊息、模型串流、計畫標題／步驟及
Agent／工作區 mention 又會經 `marked.parse()` 或 template literal 寫入 `innerHTML`。Marked 不負責
輸出消毒，因此不可信內容可被瀏覽器當作 HTML、script 或事件處理器執行，並取得 Webview 的
VS Code message bridge 能力。

此問題是在首份 `f9e78c3934d5` 候選的 Windows manual-smoke preflight 發現；該候選因此固定為
`DO NOT PUBLISH`。Owner 選擇以最小、無新增 npm 依賴的方式先恢復安全發行路徑。

## 決策

### 1. Webview 只執行 VSIX 內的本機資源

- `index.html` 不得引用任何遠端 JavaScript、CDN 或 runtime package URL。
- CSP 固定 `default-src 'none'`，script／style 只允許 `${cspSource}`，`connect-src 'none'`。
- 不允許 inline script 或 inline style；靜態樣式全部由封裝內 `style.css` 提供。

### 2. Extension→Webview 的動態值一律視為不可信文字

- 訊息、歷史、串流、錯誤、計畫、Agent 名稱與工作區檔名只能以 DOM `createElement()`＋
  `textContent` 建構。
- 禁止 `innerHTML`、`outerHTML`、`insertAdjacentHTML`、`document.write` 與等價 HTML sink。
- 清空容器使用 `replaceChildren()`；message role 只允許固定 `user`／`assistant` 映射，外部值不得
  擴張 CSS class。

### 3. 保留原始文字語意

動態文字使用 `white-space: pre-wrap` 與 bounded wrapping，保留原始換行並避免長字串撐破側欄。
`<script>`、事件屬性及 `**粗體**` 等字串都以字面文字顯示。

### 4. ToukaIDE 0.2.0 暫不支援 Markdown

本版不引入 DOMPurify、Markdown renderer 或其他新 dependency。未來若要恢復 Markdown，必須另開
DEV-A，使用封裝內、精確版本的 renderer，加上明確 sanitizer policy、惡意 payload 測試與新候選；
不得恢復 CDN 或把 parser output 直接寫入 HTML sink。

### 5. 安全不變式由現有 Vitest 執法

`ToukaIDE/src/__tests__/webviewSecurity.test.ts` 以 Node `vm` 和最小 fake DOM 執行真實 `main.js`，
驗證 hostile history／stream／plan／mention 只產生文字，並靜態鎖定 CSP、零遠端 script、零 HTML
sink 與換行 CSS。測試不新增 DOM library，也不接觸網路或 `data/`。

## 後果

- 優點：移除 runtime CDN 供應鏈與 HTML 注入面，package／lockfile保持不變，離線建置更可重現。
- 取捨：0.2.0 不顯示 Markdown 樣式、code block 或富文字；標記會直接顯示。
- 發行：任何在本 ADR 修復前建立的 VSIX 都不可發布。修復已分別通過 DEV-B、commit、tag、
  create-only candidate 與隔離 Windows smoke gates；舊候選仍不得覆寫。
- 殘留：Webview message payload schema／來源驗證是另一個 defense-in-depth 議題，不在本次最小修復；
  本 ADR 不宣稱 extension host 完全無網路能力。

## 驗證

- TDD 初始有效 RED：**5 failed／5 total**；收緊零 inline-style 契約後另取得 RED：
  **1 failed／4 passed**；最終聚焦 GREEN：**5 passed**。
- ToukaIDE：**119 passed／19 files**；typecheck、compile、package exit 0；三個 dist Webview 檔與 source
  逐檔 SHA-256 全等。
- 完整 Python 3.12：**5178 passed／9 skipped／3 warnings**、exit 0（243.10s）。結束時 OS 暫存
  Qdrant `.lock` 仍被占用，fixture 三次 bounded cleanup 後留給 OS 回收，未強制刪除。
- Local non-audit guards：**42/42 Green**。Guard #22 pip-audit／#23 npm audit 未執行，因此完整
  44-guard 狀態仍為 **INCOMPLETE**，不宣稱完整供應鏈漏洞審計。
- Exact修復已提交為`94aa17976b7262bdda54df98dc1254dae072590c`，本機lightweight `v0.2.0`
  同指該commit；新local exact VSIX SHA-256為
  `688d69d0b6bb71bcbd3f7d689d838cd34c4999265c606adb507d735d4a678a62`。
- Owner人工見證隔離VS Code smoke、Marketplace手動upload，以及READBACK-R1從Marketplace安裝
  `touka-ai.toukaide@0.2.0`後的人工smoke均符合。這不證明Marketplace遠端bytes與local VSIX全等，
  不構成receipt v3或public-release authority activation；P11-C provider activation／sunset仍BLOCKED，
  Git push未發生。舊`f9e78c3934d5`候選持續固定`DO NOT PUBLISH`。

## 回復

修復已提交；若需回復，必須另做可稽核revert並建立新版本／候選決策。任何回復都不會讓已知不安全的
`f9e78c3934d5`候選重新具備發布資格，也不得再次發布含舊Webview邊界的VSIX。
