import { describe, it, expect } from "vitest";
import {
  ARTIFACT_CSP,
  ARTIFACT_SANDBOX,
  escapeForScript,
  buildReactSrcDoc,
  buildWebSrcDoc,
} from "../sandbox";

const MSGS = {
  noComponent: "no component",
  noImportPrefix: "no import: ",
  runtimeUnavailable: "no runtime",
};

describe("artifact sandbox SSOT", () => {
  it("SECURITY: CSP blocks ALL network egress (no https anywhere) and external scripts", () => {
    expect(ARTIFACT_CSP).toContain("connect-src 'none'"); // 無 fetch/XHR/WS
    expect(ARTIFACT_CSP).toContain("form-action 'none'");
    expect(ARTIFACT_CSP).toContain("default-src 'none'");
    expect(ARTIFACT_CSP).toContain("script-src 'unsafe-inline'"); // inline only，無外部 host
    // ADR-0114 複審：img/font/media-src 不放行 https: → 無遠端 GET beacon 外洩通道。
    expect(ARTIFACT_CSP).toContain("img-src data: blob:");
    expect(ARTIFACT_CSP).not.toContain("https:");
  });

  it("SECURITY: sandbox never grants same-origin / forms / downloads", () => {
    expect(ARTIFACT_SANDBOX).toBe("allow-scripts");
    expect(ARTIFACT_SANDBOX).not.toContain("allow-same-origin");
    expect(ARTIFACT_SANDBOX).not.toContain("allow-forms");
    expect(ARTIFACT_SANDBOX).not.toContain("allow-downloads");
  });

  it("escapeForScript neutralizes </script> breakout", () => {
    expect(escapeForScript("a</script>b")).toBe("a<\\/script>b");
    expect(escapeForScript("</SCRIPT >")).toBe("<\\/SCRIPT >");
    expect(escapeForScript("no closing tag here")).toBe("no closing tag here");
  });

  it("buildWebSrcDoc embeds content under the shared CSP", () => {
    const doc = buildWebSrcDoc("<p>hi</p>", "fit");
    expect(doc).toContain(ARTIFACT_CSP);
    expect(doc).toContain("<p>hi</p>");
  });

  it("buildReactSrcDoc wires runtime + user code + mount under CSP", () => {
    const doc = buildReactSrcDoc({
      runtime: "/*RUNTIME*/window.React={};",
      code: "module.exports.default = function(){return null;};",
      scaleMode: "fit",
      messages: MSGS,
    });
    expect(doc).toContain(ARTIFACT_CSP);
    expect(doc).toContain("/*RUNTIME*/");
    expect(doc).toContain("module.exports.default");
    expect(doc).toContain("__touka_mount"); // 掛載呼叫
    expect(doc).toContain("no import: "); // 本地化訊息已注入
  });

  it("SECURITY: buildReactSrcDoc escapes </script> in runtime, user code, AND i18n messages", () => {
    const doc = buildReactSrcDoc({
      runtime: "var a='</script>';",
      code: "var s = '</script><img src=x onerror=alert(1)>';",
      scaleMode: "fit",
      // 縱深防禦：即便 i18n 文案含 </script> 也不得逸出
      messages: { noComponent: "x</script>y", noImportPrefix: "no import", runtimeUnavailable: "no rt" },
    });
    expect(doc).toContain("<\\/script>");
    expect(doc).not.toContain("</script><img");
    expect(doc).not.toContain("</script>';"); // runtime 內的也被轉義
    expect(doc).not.toContain("x</script>y"); // 訊息內的也被轉義
  });
});
