import DOMPurify, { type DOMPurify as DOMPurifyInstance } from "dompurify";

/**
 * 流程圖（mermaid）輸出的第二層消毒（ADR-0294）：不載入任何外部的圖片或樣式，圖上的字照常顯示。
 *
 * 用專屬的 DOMPurify 實例，掛鉤只影響這裡、不影響全域。規則套用在所有元素與屬性上：
 * - 會載入網址的屬性只留 `#` 開頭的內部參照；`<a>` 的 http(s) 連結保留（點了才導覽），並在新分頁開、不帶 Referer。
 * - 其他屬性的值含外部 `url(`、`image-set(`、`@import` 或反斜線跳脫，整個屬性移除。
 * - `<style>` 與 `style` 屬性以 `;`、`{`、`}` 切段，只拿掉命中的那一段，分隔符號與其他樣式照常。
 * - `foreignObject` 保留（mermaid 的文字標籤放在裡面）。
 */
const URL_ATTRIBUTES = new Set([
  "href", "xlink:href", "src", "poster", "background", "action", "formaction", "data",
  "cite", "longdesc", "usemap", "codebase", "archive", "manifest", "icon", "profile", "lowsrc", "dynsrc",
]);
/** 一個值裡可以列多個網址的屬性：整個移除，不留內部參照的例外。 */
const URL_LIST_ATTRIBUTES = new Set(["srcset", "imagesrcset", "ping"]);
const LINK_ATTRIBUTES = new Set(["href", "xlink:href"]);
const WEB_PROTOCOLS = new Set(["http:", "https:"]);
const CSS_LOADERS = /@import|image-set\(|\\/i;
const CSS_URL = /url\(/gi;

const PURIFY_CONFIG = {
  USE_PROFILES: { svg: true, svgFilters: true, html: true },
  ADD_TAGS: ["foreignObject"],
  HTML_INTEGRATION_POINTS: { foreignobject: true },
};

function loadsOutside(value: string): boolean {
  if (CSS_LOADERS.test(value)) return true;
  for (const match of value.matchAll(CSS_URL)) {
    const rest = value.slice((match.index ?? 0) + match[0].length).replace(/^[\s'"]+/, "");
    if (!rest.startsWith("#")) return true;
  }
  return false;
}

function stripCss(css: string): string {
  return css.split(/([;{}])/).map((part) => (loadsOutside(part) ? "" : part)).join("");
}

function isWebLink(value: string): boolean {
  try {
    return WEB_PROTOCOLS.has(new URL(value.trim()).protocol);
  } catch {
    return false;
  }
}

let purifier: DOMPurifyInstance | null = null;

function getPurifier(): DOMPurifyInstance {
  if (purifier) return purifier;
  const instance = DOMPurify(window);
  instance.addHook("uponSanitizeElement", (node, data) => {
    if (data.tagName === "style") node.textContent = stripCss(node.textContent ?? "");
  });
  instance.addHook("uponSanitizeAttribute", (node, data) => {
    const name = data.attrName;
    const value = data.attrValue;
    if (name === "style") {
      data.attrValue = stripCss(value);
    } else if (URL_LIST_ATTRIBUTES.has(name)) {
      data.keepAttr = false;
    } else if (URL_ATTRIBUTES.has(name)) {
      const internal = value.trim().startsWith("#");
      const link = node.nodeName.toLowerCase() === "a" && LINK_ATTRIBUTES.has(name) && isWebLink(value);
      if (!internal && !link) data.keepAttr = false;
    } else if (loadsOutside(value)) {
      data.keepAttr = false;
    }
  });
  instance.addHook("afterSanitizeAttributes", (node) => {
    if (node.nodeName.toLowerCase() !== "a") return;
    const href = node.getAttribute("href") ?? node.getAttribute("xlink:href");
    if (href && isWebLink(href)) {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
  });
  purifier = instance;
  return instance;
}

export function sanitizeMermaidSvg(svg: string): string {
  if (!svg || typeof window === "undefined") return "";
  return getPurifier().sanitize(svg, PURIFY_CONFIG);
}
