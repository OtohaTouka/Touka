import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * 靜態源碼掃描，鎖死 ADR-0114 的安全不變式（防未來改動悄悄破壞）：
 *   #1 父層轉譯器絕不執行使用者碼（禁 eval / new Function）。
 *   #2 artifact 預覽 iframe 絕不取得 same-origin。
 */
const read = (rel: string) => readFileSync(resolve(__dirname, rel), "utf8");

describe("artifact security invariants (static source scan, ADR-0114)", () => {
  // 比對「實際用法」而非解說性註解：呼叫需帶括號、sandbox 值需在字串字面內。
  it("#1 transpile.ts never executes user code (no eval(...) / new Function(...) call)", () => {
    const src = read("../transpile.ts");
    expect(src).not.toMatch(/\beval\s*\(/);
    expect(src).not.toMatch(/\bnew\s+Function\s*\(/);
  });

  it("#2 sandbox.ts never grants same-origin inside a string value", () => {
    expect(read("../sandbox.ts")).not.toMatch(/["'][^"'\n]*allow-same-origin/);
  });

  it("#2 ArtifactView never hardcodes allow-same-origin in a sandbox value", () => {
    expect(read("../../../components/artifacts/ArtifactView.tsx")).not.toMatch(
      /["'][^"'\n]*allow-same-origin/,
    );
  });
});
