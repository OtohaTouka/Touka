import { describe, it, expect } from "vitest";
import { decideMarkdownImage } from "../markdownImagePolicy";

/**
 * ADR-0294：模型輸出的 Markdown 圖片只自動載入 Touka 自己的來源。
 * 網址一律先 `new URL` 解析再比來源，字串開頭判斷擋不住 `//evil`、`/\evil`、夾 tab 的寫法。
 */
const PAGE = "http://localhost:3001";
const BACKEND = "http://localhost:8000";

describe("decideMarkdownImage", () => {
  it.each([
    ["https://evil.example/a.png?d=secret", "evil.example"],
    ["//evil.example/a.png", "evil.example"],
    ["/\\evil.example/a.png", "evil.example"],
    ["/\t/evil.example/a.png", "evil.example"],
    ["htt\nps://evil.example/a.png", "evil.example"],
    ["HTTPS://EVIL.EXAMPLE/a.png", "evil.example"],
    ["http://localhost:9999/a.png", "localhost:9999"],
    ["http://127.0.0.1:8001/a.png", "127.0.0.1:8001"],
    ["https://localhost:8000/a.png", "localhost:8000"],
  ])("blocks an outside image and offers a link: %j", (src, host) => {
    expect(decideMarkdownImage(src, PAGE, BACKEND)).toEqual({
      kind: "blocked",
      host,
      href: new URL(src, PAGE).href,
    });
  });

  it.each([
    "https://user:pass@evil.example/a.png",
    "http://user@localhost:8000/v1/outputs/a.png",
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "blob:https://evil.example/0f7c",
    "",
  ])("blocks without a link: %j", (src) => {
    const decision = decideMarkdownImage(src, PAGE, BACKEND);
    expect(decision.kind).toBe("blocked");
    expect(decision).toMatchObject({ href: null });
  });

  it("blocks a missing src", () => {
    expect(decideMarkdownImage(undefined, PAGE, BACKEND)).toEqual({ kind: "blocked", host: "", href: null });
  });

  it.each([
    ["/v1/outputs/Alice/a.png", "http://localhost:3001/v1/outputs/Alice/a.png"],
    ["http://localhost:3001/x.png", "http://localhost:3001/x.png"],
    ["http://localhost:8000/v1/outputs/Alice/a.png", "http://localhost:8000/v1/outputs/Alice/a.png"],
    ["http://127.0.0.1:8000/v1/outputs/Alice/a.png", "http://127.0.0.1:8000/v1/outputs/Alice/a.png"],
    ["http://[::1]:8000/v1/outputs/Alice/a.png", "http://[::1]:8000/v1/outputs/Alice/a.png"],
    ["data:image/png;base64,iVBORw0KGgo=", "data:image/png;base64,iVBORw0KGgo="],
    ["blob:http://localhost:3001/0f7c", "blob:http://localhost:3001/0f7c"],
  ])("loads a Touka image: %j", (src, resolved) => {
    expect(decideMarkdownImage(src, PAGE, BACKEND)).toEqual({ kind: "load", src: resolved });
  });

  it("accepts loopback aliases only when the backend itself is loopback", () => {
    const lan = "https://touka.lan:8443";
    expect(decideMarkdownImage("https://touka.lan:8443/v1/outputs/a.png", PAGE, lan).kind).toBe("load");
    expect(decideMarkdownImage("https://127.0.0.1:8443/v1/outputs/a.png", PAGE, lan).kind).toBe("blocked");
  });

  it("falls back to the page origin when the backend address is unusable", () => {
    expect(decideMarkdownImage("/x.png", PAGE, "").kind).toBe("load");
    expect(decideMarkdownImage("http://localhost:8000/x.png", PAGE, "not a url").kind).toBe("blocked");
  });
});
