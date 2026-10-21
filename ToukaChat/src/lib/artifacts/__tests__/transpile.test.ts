import { describe, it, expect } from "vitest";
import { transpileArtifact } from "../transpile";

describe("transpileArtifact", () => {
  it("transpiles JSX to React.createElement (classic runtime, global React)", async () => {
    const res = await transpileArtifact("const x = <div className='a'>hi</div>;");
    expect("code" in res).toBe(true);
    if ("code" in res) {
      expect(res.code).toContain("React.createElement");
      expect(res.code).not.toContain("<div"); // JSX 已轉掉
    }
  });

  it("strips TypeScript types", async () => {
    const res = await transpileArtifact("const n: number = 1; function f(a: string): string { return a; }");
    expect("code" in res).toBe(true);
    if ("code" in res) {
      expect(res.code).not.toContain(": number");
      expect(res.code).not.toContain(": string");
    }
  });

  it("converts `export default` to commonjs exports (for iframe mount resolution)", async () => {
    const res = await transpileArtifact("export default function App() { return <div>app</div>; }");
    expect("code" in res).toBe(true);
    if ("code" in res) {
      expect(res.code).toContain("exports"); // export default → exports.default
    }
  });

  it("rewrites a USED `import` to require() (iframe shim turns it into a friendly offline error)", async () => {
    // 註：未使用的 import 會被 preset-typescript 視為可能的型別匯入而剔除（無害）；
    // 只有「實際被當值使用」的外部匯入才會變成 require()，於 iframe 內丟出「不支援匯入」。
    const res = await transpileArtifact(
      "import confetti from 'canvas-confetti';\nexport default () => { confetti(); return null; };",
    );
    expect("code" in res).toBe(true);
    if ("code" in res) {
      expect(res.code).toContain("require"); // 由 iframe 的 require shim 丟出「不支援匯入」
    }
  });

  it("returns an error (does not throw) on invalid syntax", async () => {
    const res = await transpileArtifact("const = = = broken(((");
    expect("error" in res).toBe(true);
    if ("error" in res) {
      expect(res.error.length).toBeGreaterThan(0);
      expect(res.error).not.toContain("\n"); // 已正規化為單行
    }
  });

  it("rejects oversized source without invoking Babel (DoS guard, ADR-0114)", async () => {
    const huge = "const x = 1;\n".repeat(30000); // ~360KB > 256KB cap
    const res = await transpileArtifact(huge);
    expect("error" in res).toBe(true);
    if ("error" in res) expect(res.error).toMatch(/too large/i);
  });

  it("SECURITY: only produces a string — never executes the source", async () => {
    // 若轉譯過程「執行」了使用者碼，這行會污染全域；轉譯只做字串轉換，故不應發生。
    delete (globalThis as Record<string, unknown>).__artifactPwned;
    const res = await transpileArtifact("globalThis.__artifactPwned = true; export default () => null;");
    expect("code" in res).toBe(true); // 有產出字串
    expect((globalThis as Record<string, unknown>).__artifactPwned).toBeUndefined(); // 但未被執行
  });
});
