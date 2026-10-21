/**
 * Artifact 預覽 iframe 的沙箱 SSOT（選項 A，ADR-0114）。
 *
 * CSP 與 sandbox 屬性集中於此，WebRenderer 與 ReactRenderer 共用，避免漂移（安全不變式 #3）。
 * 核心保證：`connect-src 'none'`（無網路→不可外洩）、無外部 script host（無 CDN）、
 * `sandbox` **永不含 allow-same-origin**（→ 拿不到父視窗 token/cookie，安全不變式 #2）。
 * 沿襲歷次資安加固 P2 / P12 / DA10。
 */

// 預覽 iframe 的 CSP（離線、無外連、僅 inline）。
// ADR-0114 複審：img/font/media-src 不放行 `https:`——否則惡意 artifact 可把資料編碼進
// 遠端 GET（Image().src / @font-face / <video>）外洩，繞過 connect-src 'none'；而 Option A 的
// 互動 React 會放大此風險（可擷取預覽內鍵入內容逐字外送）。收斂為 `data: blob:` 使「完全離線」
// 名副其實——代價：artifact 無法載入遠端圖片（請改用 data: 內嵌）。
export const ARTIFACT_CSP =
  "default-src 'none'; " +
  "script-src 'unsafe-inline'; " +
  "style-src 'unsafe-inline'; " +
  "img-src data: blob:; " +
  "font-src data:; " +
  "media-src data: blob:; " +
  "connect-src 'none'; " +
  "form-action 'none'; " +
  "base-uri 'none';";

/** 預覽 iframe 的 sandbox 屬性。**永不**加 allow-same-origin / allow-forms / allow-downloads。 */
export const ARTIFACT_SANDBOX = "allow-scripts";

/**
 * 轉義注入 `<script>` 的字串，防止內容中的 `</script>` 提前關閉腳本標籤而逸出沙箱。
 * 在 JS 字串中 `<\/script>` 與 `</script>` 等義，但 HTML 解析器不會視之為關閉標籤。
 */
export function escapeForScript(s: string): string {
  return s.replace(/<\/(script)/gi, "<\\/$1");
}

// 預覽 iframe 文件的基礎 CSS。此處硬編色（#333/#fff）為**隔離 iframe 文件**的可讀性 fallback
// ——iframe 是獨立 document、拿不到 app 的 Design Token/CSS 變數，故無法（也不應）走 tokens.ts；
// 與既有 WebRenderer 一致（內聯於含 ${} 的樣板字串內，非 app UI 範疇）。
const SHARED_BODY_STYLE = (scaleMode: "fit" | "original"): string =>
  `*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:transparent;padding:20px;font-family:sans-serif;color:#333;margin:0;${
    scaleMode === "fit" ? "overflow:hidden;max-width:100vw;" : ""
  }}
@media (prefers-color-scheme: dark){body{color:#fff}}
img,svg{max-width:100%;height:auto}
${scaleMode === "original" ? "body{width:max-content}" : ""}`;

/** 純 HTML/SVG 片段預覽文件（WebRenderer 用；維持既有行為，改吃共用 CSP）。 */
export function buildWebSrcDoc(content: string, scaleMode: "fit" | "original"): string {
  return `<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${ARTIFACT_CSP}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>${SHARED_BODY_STYLE(scaleMode)}</style>
</head><body>
${content}
</body></html>`;
}

/** ReactRenderer 在 iframe 內顯示錯誤訊息用的本地化字串（由父層 t() 傳入注入）。 */
export interface ReactDocMessages {
  /** 找不到可渲染的元件（無 export default、無 App）。 */
  noComponent: string;
  /** 不支援匯入外部模組的前綴（後接模組名）。 */
  noImportPrefix: string;
  /** React runtime 未載入。 */
  runtimeUnavailable: string;
}

/**
 * 組裝互動式 React artifact 的 iframe srcdoc（純函式、可單元測試）。
 *
 * 順序：error 顯示器 → 預建 runtime（設 window.React / __touka_mount）→ commonjs shim
 * （module/exports/require，require 對任何匯入丟出離線錯誤）＋ 轉譯後使用者碼 ＋ 掛載。
 * runtime 與使用者碼皆過 `escapeForScript` 防 `</script>` 逸出。
 */
export function buildReactSrcDoc(opts: {
  runtime: string;
  code: string;
  scaleMode: "fit" | "original";
  messages: ReactDocMessages;
}): string {
  const rt = escapeForScript(opts.runtime);
  const code = escapeForScript(opts.code);
  // 訊息經 JSON.stringify 注入 inline <script>；JSON.stringify 不轉義 `/`，故仍先過
  // escapeForScript 防萬一 i18n 文案含 </script>（縱深防禦，escapeForScript 冪等；ADR-0114 複審）。
  const m = {
    noComponent: escapeForScript(opts.messages.noComponent),
    noImportPrefix: escapeForScript(opts.messages.noImportPrefix),
    runtimeUnavailable: escapeForScript(opts.messages.runtimeUnavailable),
  };
  return `<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${ARTIFACT_CSP}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>${SHARED_BODY_STYLE(opts.scaleMode)}
#__artifact_error{display:none;white-space:pre-wrap;font-family:monospace;font-size:12px;color:#dc2626;padding:8px;border:1px solid #dc262633;border-radius:6px}</style>
</head><body>
<div id="root"></div>
<div id="__artifact_error"></div>
<script>
window.__artifactError=function(m){var e=document.getElementById("__artifact_error");if(e){e.style.display="block";e.textContent=String(m);}var r=document.getElementById("root");if(r)r.style.display="none";};
window.onerror=function(m){window.__artifactError(m);return true;};
</script>
<script>${rt}</script>
<script>
(function(){"use strict";
var module={exports:{}};var exports=module.exports;
function require(n){throw new Error(${JSON.stringify(m.noImportPrefix)}+n);}
try{
${code}
var C=module.exports.default||module.exports.App||(typeof App!=="undefined"?App:null);
if(!C){window.__artifactError(${JSON.stringify(m.noComponent)});return;}
if(!window.React||!window.__touka_mount){window.__artifactError(${JSON.stringify(m.runtimeUnavailable)});return;}
window.__touka_mount(window.React.createElement(C),document.getElementById("root"));
}catch(e){window.__artifactError(e&&e.message?e.message:String(e));}
})();
</script>
</body></html>`;
}
