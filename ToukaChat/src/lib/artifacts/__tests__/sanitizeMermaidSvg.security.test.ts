import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import mermaid from "mermaid";
import { sanitizeMermaidSvg } from "../sanitizeMermaidSvg";
import { MERMAID_CONFIG } from "../mermaidConfig";

/**
 * ADR-0294：流程圖不載入外部的圖片或樣式，圖上的字照常顯示。
 *
 * fixtures/mermaid/*.svg 是 mermaid 11.16.1 的真實輸出：在無頭 Edge 以 MERMAID_CONFIG 渲染
 * （所有網路請求擋掉）。兩個 *-injection-unprotected 用的是「沒有加保護欄位」的設定，
 * 圖開頭的設定區（frontmatter）寫了 themeCSS／fontFamily，實際把外部網址帶進了樣式。
 * *-protected 是同樣的注入（含 `%%{init}%%` 寫法）改用 MERMAID_CONFIG 的輸出。
 */
const fixture = (name: string) => readFileSync(resolve(__dirname, "fixtures/mermaid", `${name}.svg`), "utf8");
// 產品用 innerHTML 放進頁面，這裡也用 HTML 解析器讀（消毒結果是 HTML 序列化，不一定是合法 XML）。
const parse = (svg: string) => new DOMParser().parseFromString(svg, "text/html");
const styleText = (svg: string) => [...parse(svg).querySelectorAll("style")].map((s) => s.textContent).join("\n");
const textOf = (svg: string) => parse(svg).body.textContent ?? "";
const labelBoxes = (svg: string) => parse(svg).querySelectorAll("foreignObject").length;

const NORMAL: Array<[string, string[]]> = [
  ["flowchart", ["開始 Start", "判斷", "結果一", "結果二"]],
  ["sequence", ["Alice", "Bob", "你好", "收到"]],
  ["class", ["Animal", "Dog"]],
  ["gantt", ["計畫", "任務一", "任務二"]],
  ["state", ["待命", "執行", "開始"]],
  ["er", ["USER", "ORDER", "places", "name"]],
  ["mindmap", ["中心", "分支一", "分支二"]],
];

describe("sanitizeMermaidSvg — normal output is untouched", () => {
  it.each(NORMAL)("%s keeps its styles and text", (name, labels) => {
    const raw = fixture(name);
    const clean = sanitizeMermaidSvg(raw);
    expect(styleText(clean)).toBe(styleText(raw));
    expect(labelBoxes(clean)).toBe(labelBoxes(raw));
    for (const label of labels) expect(textOf(clean)).toContain(label);
  });

  it("keeps internal references and data attributes", () => {
    const clean = sanitizeMermaidSvg(fixture("flowchart"));
    expect(clean).toMatch(/marker-end="url\(#fx-flowchart_flowchart-v2-pointEnd\)"/);
    expect(clean).toContain('data-id="L_A_B_0"');
  });

  it("keeps a click link and opens it safely", () => {
    const link = parse(sanitizeMermaidSvg(fixture("flowchart"))).querySelector("a");
    expect(link?.getAttribute("xlink:href") ?? link?.getAttribute("href")).toBe("https://example.com/docs");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
  });
});

describe("sanitizeMermaidSvg — outside addresses are removed", () => {
  it.each(["themecss-injection-unprotected", "fontfamily-injection-unprotected"])(
    "%s: real injected styles lose the outside address only",
    (name) => {
      const raw = fixture(name);
      expect(raw).toContain("evil.example");
      const clean = sanitizeMermaidSvg(raw);
      expect(clean).not.toContain("evil.example");
      expect(styleText(clean)).toContain("font-size:16px");
      expect(textOf(clean)).toContain("A");
    },
  );

  const ADDED = [
    '<image href="https://evil.example/i.png" width="1" height="1"/>',
    '<image xlink:href="https://evil.example/i2.png" width="1" height="1"/>',
    '<use href="https://evil.example/u.svg#x"/>',
    '<rect width="1" height="1" fill="url(https://evil.example/r.png)"/>',
    `<rect width="1" height="1" fill="URL( 'https://evil.example/r2.png' )"/>`,
    '<rect width="1" height="1" style="fill:url(https://evil.example/s.png)"/>',
    `<rect width="1" height="1" style="fill:red;background:image-set('https://evil.example/is.png' 1x)"/>`,
    '<foreignObject width="1" height="1"><div xmlns="http://www.w3.org/1999/xhtml"><img srcset="https://evil.example/ss.png 1x"/>ok</div></foreignObject>',
    '<foreignObject width="1" height="1"><div xmlns="http://www.w3.org/1999/xhtml"><img src="https://evil.example/fi.png"/>ok</div></foreignObject>',
    '<foreignObject width="1" height="1"><div xmlns="http://www.w3.org/1999/xhtml"><video poster="https://evil.example/vp.png"></video>ok</div></foreignObject>',
    '<foreignObject width="1" height="1"><div xmlns="http://www.w3.org/1999/xhtml"><input type="image" src="https://evil.example/in.png"/>ok</div></foreignObject>',
    '<foreignObject width="1" height="1"><div xmlns="http://www.w3.org/1999/xhtml"><iframe src="https://evil.example/if.html"></iframe><link rel="stylesheet" href="https://evil.example/l.css"/>ok</div></foreignObject>',
    '<filter id="fe"><feImage href="https://evil.example/fe.png"/></filter>',
    '<filter id="fe2"><feImage xlink:href="https://evil.example/fe2.png"/></filter>',
    "<style>@IMPORT url('https://evil.example/a.css'); .q{color:red}</style>",
    "<style>@IMPORT 'https://evil.example/b.css'; .q{color:red}</style>",
    "<style>.w{background:u\\72l(https://evil.example/w.png)} .v{color:blue}</style>",
    '<a href="javascript:alert(1)"><text>bad</text></a>',
  ];

  it.each(ADDED)("removes %s", (piece) => {
    const raw = fixture("flowchart").replace("</svg>", `${piece}</svg>`);
    const clean = sanitizeMermaidSvg(raw);
    expect(clean).not.toContain("evil.example");
    expect(clean).not.toMatch(/javascript:/i);
  });

  it("drops only the matching declaration or rule", () => {
    const raw = fixture("flowchart").replace(
      "</svg>",
      "<style>@IMPORT url('https://evil.example/a.css'); .q{color:red;background:URL( 'https://evil.example/b.png' )} .w{background:u\\72l(x)} .v{color:blue}</style></svg>",
    );
    const css = styleText(sanitizeMermaidSvg(raw));
    expect(css).toContain(".q{color:red;}");
    expect(css).toContain(".v{color:blue}");
    expect(css).not.toContain("u\\72l");
  });

  it("treats quoted and spaced internal references as internal", () => {
    const raw = fixture("flowchart").replace(
      "</svg>",
      `<rect width="1" height="1" fill="url( '#fx-flowchart-gradient' )"/><style>.k{fill:URL( "#g" )}</style></svg>`,
    );
    const clean = sanitizeMermaidSvg(raw);
    expect(clean).toContain(`fill="url( '#fx-flowchart-gradient' )"`);
    expect(styleText(clean)).toContain('.k{fill:URL( "#g" )}');
  });
});

describe("MERMAID_CONFIG — protected keys", () => {
  it.each(["themecss-injection-protected", "fontfamily-injection-protected", "init-themecss-protected"])(
    "%s: the diagram's own settings never put an outside address in the output",
    (name) => {
      const raw = fixture(name);
      expect(raw).not.toContain("evil.example");
      expect(textOf(raw)).toContain("A");
      expect(styleText(sanitizeMermaidSvg(raw))).toBe(styleText(raw));
    },
  );
});

describe("MERMAID_CONFIG", () => {
  it("keeps every default secure key and adds the three style keys", () => {
    const defaults = mermaid.mermaidAPI.defaultConfig.secure ?? [];
    expect(defaults).toEqual(expect.arrayContaining(["securityLevel", "maxTextSize"]));
    expect(MERMAID_CONFIG.secure).toEqual(
      expect.arrayContaining([...defaults, "themeCSS", "fontFamily", "altFontFamily"]),
    );
    expect(MERMAID_CONFIG.securityLevel).toBe("strict");
  });
});
