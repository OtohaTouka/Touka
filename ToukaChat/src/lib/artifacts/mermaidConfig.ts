import type { MermaidConfig } from "mermaid";

/**
 * 流程圖（mermaid）的設定（ADR-0294）。只在沙箱框裡套用（見 mermaidSandbox.ts）。
 *
 * `secure` 列的欄位，圖裡的設定指令（`%%{init}%%`、開頭的 frontmatter）改不了。mermaid 只檢查樣式欄位的
 * 大括號是否成對，所以 `themeCSS`、`fontFamily`、`altFontFamily` 能帶進載入外部網址的樣式，要加進 `secure`。
 * 傳入的 `secure` 會整份取代預設清單，所以這裡寫死一份預設項目；沙箱框裡再併上執行中版本的預設，升級後多出的也留著。
 */
const MERMAID_DEFAULT_SECURE = ["secure", "securityLevel", "startOnLoad", "maxTextSize", "suppressErrorRendering", "maxEdges"];
const MERMAID_STYLE_KEYS = ["themeCSS", "fontFamily", "altFontFamily"];

export const MERMAID_CONFIG: MermaidConfig = {
  startOnLoad: false, // 由渲染器手動觸發
  theme: "dark",
  securityLevel: "strict", // P2 fix: was 'loose' — XSS risk
  fontFamily: "monospace",
  // @ts-expect-error - Mermaid 某些版本支援此選項隱藏氣泡錯誤
  suppressError: true,
  // 隱藏報錯懸浮氣泡的 CSS 注入
  themeVariables: {
    errorBkgColor: "transparent",
    errorTextColor: "transparent",
  },
  secure: [...MERMAID_DEFAULT_SECURE, ...MERMAID_STYLE_KEYS],
};
