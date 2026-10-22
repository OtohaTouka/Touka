# ADR-0114：互動式 React artifact 預覽 — 父層轉譯 ＋ 沙箱 iframe 內預建 runtime（離線、不放寬隔離）

- **狀態**：✅ 已接受
- **日期**：2026-06-25
- **影響範圍**：ToukaChat（artifacts 預覽：transpile／sandbox SSOT／runtime-entry＋esbuild 產物／ArtifactView ReactRenderer／extractor 偵測／i18n）
- **關鍵字**：artifact, React, JSX, TSX, 互動預覽, @babel/standalone, esbuild, 沙箱 iframe, srcdoc, CSP, connect-src none, allow-same-origin, 離線, 選項 A, 建基於 P2/P12/DA10

## 背景（Context）

Artifact 預覽原本只能渲染**純 HTML + inline JS**（`WebRenderer` 把內容塞進沙箱 iframe 的 srcdoc）。AI 寫的 **React/JSX/TSX 元件**不會被轉譯/執行——只會顯示壞掉的標記或原始碼，無法像 claude.ai 那樣「點擊、互動」。

擁有者要求達到「類 claude.ai 互動」。評估後分三層：L1 純 HTML 互動（已有）、**L2 React 元件真的跑起來且離線**、L3 外部函式庫/CDN/網路。本 ADR 實作 **L2（選項 A）**；L3 明確留待「獨立沙箱 origin」（選項 B）另案，因 L3 需放寬隔離＋新增 origin 基礎設施，牴觸 local-first／精簡。

claude.ai 的互動能力來自**跨來源隔離**（artifact 跑在獨立 cookieless origin，故敢放寬能力）。Touka 不走那條路，改採「**鎖死換簡單**」：在既有同頁沙箱 iframe 內，僅多跑 inline JS（React），**完全不放寬隔離、不開網路**——保住 Touka 的核心資安強項「不能外連→不能外洩」。

## 決策（Decision）

**在父層 ToukaChat 把 React/JSX/TSX 轉譯成純 JS 字串，連同預建的 React runtime 注入既有沙箱 iframe 由 React 渲染；隔離模型完全不變。**

- **父層轉譯**（`lib/artifacts/transpile.ts`）：lazy `@babel/standalone`（~3MB，僅開啟 React artifact 時載入，不進初始 bundle）以 preset-react（classic runtime→`React.createElement`）＋ preset-typescript（剝型別）＋ transform-modules-commonjs 轉譯。`export default`→`exports.default`、`import`→`require()`。
- **預建 runtime**（`runtime-entry.ts` → esbuild → gitignore 的 `src/generated/artifactRuntime.ts` 字串常數）：因 React 19 無 UMD，用既有 esbuild 把 React 19＋`createRoot` 打成單一 IIFE，設 iframe 全域 `window.React` 與 `__touka_mount`。由 `dev`/`prebuild` 自動生成。
- **掛載慣例**：iframe 內提供 `module`/`exports`/`require` shim（`require` 對任何匯入丟出「離線不支援匯入」友善錯誤）；解析 `module.exports.default` → `module.exports.App` → 全域 `App`；都無 → 友善錯誤。
- **ReactRenderer**（ArtifactView 內）：轉譯成功 → `buildReactSrcDoc` 組 srcdoc → 既有沙箱 iframe；轉譯失敗顯示父層 i18n 錯誤；iframe 內執行錯誤由內嵌 `window.onerror` 顯示器處理。i18n：`artifacts.react.*`（zh-TW／en）。
- **extractor 偵測**：`tsx/jsx`、`import React`、`export default`、hooks 呼叫、`className=`、大寫開頭 function/const＝React 訊號→新 `'react'` 型別；完整 HTML 文件（`lang=html`/`<html>`/`<!doctype>`）仍走 `'web'`。SVG/mermaid/markdown 不變。

### 四條安全不變式（以測試鎖死）

1. **父層絕不執行使用者碼**：transpile 只做字串轉換，禁 `eval(`／`new Function(`（靜態源碼掃描 ＋ 行為測試：轉譯會污染全域的字串，確認未被執行）。
2. **iframe 永不 `allow-same-origin`**：`ARTIFACT_SANDBOX = "allow-scripts"` SSOT；靜態掃描禁字串字面含 `allow-same-origin`（→ 拿不到父視窗 token/cookie）。
3. **CSP 不變**：`connect-src 'none'`（無 fetch/XHR/WS→不可外洩）、無外部 script host（無 CDN）；`ARTIFACT_CSP` SSOT，WebRenderer 與 ReactRenderer 共用防漂移。
4. **拒絕外部 import**：`require` shim 對任何匯入丟出離線錯誤（友善訊息）。

另：注入 `<script>` 的 runtime 與使用者碼皆過 `escapeForScript`，防內容中的 `</script>` 提前關閉標籤逸出。

**威脅模型結論**：相對現況**無新增權限面**——只是讓既有沙箱 iframe 多跑 inline JS（本來就允許 `script-src 'unsafe-inline'`）；token 偷不到（無 same-origin）、資料送不出（無網路）；Babel 在父層只做字串轉換、永不執行。

### 被否決的方案

- **獨立沙箱 origin（claude.ai 路線／選項 B）**：可達 L3（外部庫/網路），但需新增 cookieless origin 基礎設施＋放寬隔離＋對開源使用者的部署負擔，且一開網路就回到 artifact-exfiltration 風險。留待 L3 真有需求時另案。
- **第三方 in-browser runtime（Sandpack/WebContainers）**：多半需連網/COOP-COEP，牴觸 local-first／隱私。
- **esbuild-wasm 在父層 in-browser bundle 全部**：較省體積但需 Next 服務 wasm＋虛擬 FS 解析，複雜度高於「build-time runtime + Babel classic」。

## 後果（Consequences）

- ✅ AI 寫的 React/JSX 元件在 artifact 面板可渲染、可點擊、可互動（state/事件/canvas），**完全離線**。
- ✅ 資安強項保留：無網路、無 same-origin、無外部資源；四不變式有測試保護。
- ✅ 純 HTML/SVG/mermaid/markdown 行為不變；WebRenderer 改吃共用 CSP/sandbox SSOT。
- ✅ 開源零負擔：自包含、無新基礎設施；runtime bundle 由 dev/prebuild 生成（gitignore）。
- ⚠️ **已知限制（文件化）**：Tailwind class 不生效（無建置/CDN→引導 AI 用 inline style）；外部函式庫不可用（屬 L3/選項 B）；僅單檔元件；`@babel/standalone` 為新增 runtime 相依（lazy）；**遠端圖片/字型/媒體不載入**（複審後 CSP 收斂為 `img-src data: blob:` 等、移除 `https:` 使「離線」名副其實——含既有 WebRenderer；請用 `data:` 內嵌資源）。
- ⚠️ 乾淨 clone 首次須跑 `npm run dev`/`build`（或 `gen:artifact-runtime`）生成 runtime bundle，否則 `@/generated/artifactRuntime` 不存在。

## 驗證

- TDD：transpile（6）、sandbox/SSOT＋逸出（7）、extractor 偵測（8）、ReactRenderer 整合（2）、安全不變式靜態掃描（3）。
- 對抗式複審（6 個視角＋懷疑者驗證）**證實四不變式全數成立**（無父層 RCE、無沙箱逃逸/token 竊取、無外部 import、`</script>` 已逸出），並據複審加固：①CSP 移除 `https:`（img/font/media）杜絕遠端 GET beacon 外洩——互動 React 會放大此風險（可擷取預覽內鍵入內容）；②串流期轉譯**防抖 300ms**（消除每 chunk 主執行緒 O(n²) 凍結）＋③轉譯前 **256KB 上限**（病態 artifact DoS）；④i18n 訊息亦過 `escapeForScript`（縱深）；⑤顯式 `<touka_artifact type>` 白名單驗證＋大小寫正規化＋react-wrap-HTML 降級；⑥extractor 改用 `match.index`。
- `guard_all` 全綠（含 #20 knip、#23 npm audit、#27 TS、#33 i18n、#35 ESLint ratchet）、vitest 全綠、smoke 不受影響（純前端）。
- preview 實機開 React artifact 證明可互動。
- 全程於 3.12 venv 跑後端守衛。建基於既有 artifact 資安加固 P2/P12/DA10。
