import { afterEach, describe, expect, it } from "vitest";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import ts from "typescript";
// eslint-disable-next-line no-restricted-syntax -- Root build config has no equivalent @/src alias; verify the real Next selection.
import nextConfig from "../../../next.config";

const chatRoot = path.resolve(__dirname, "../../..");
const ownedParent = realpathSync(tmpdir());
const ownedRoots: string[] = [];
const contractTest = "src/lib/providers/__tests__/contract.test.ts";
const testFiles = [
  contractTest,
  "src/lib/inline.test.ts",
  "src/lib/inline-jsx.test.tsx",
  "src/lib/inline.spec.ts",
  "src/lib/inline-jsx.spec.tsx",
  "tests/plain.ts",
  "e2e/plain.ts",
  "src/test/setup.ts",
];
const fixtureImport =
  "../../../../../tests/fixtures/reliability_session_stream.json";

function write(root: string, relative: string, content: string) {
  const target = path.join(root, relative);
  mkdirSync(path.dirname(target), { recursive: true });
  writeFileSync(target, content);
}

function fixture(withSharedFixture = false): string {
  const root = mkdtempSync(path.join(ownedParent, "touka-type-scope-"));
  ownedRoots.push(root);
  const project = path.join(root, "Chat");
  for (const name of ["tsconfig.json", "tsconfig.build.json"]) {
    const source = path.join(chatRoot, name);
    if (existsSync(source)) write(project, name, readFileSync(source, "utf8"));
  }
  const files: Record<string, string> = {
    "next-env.d.ts": "export {};",
    "src/app/page.tsx":
      'export default function Page() { return "synthetic"; }',
    "next.config.ts": 'export default { output: "export" };',
    "scripts/check.ts": "export const checked: boolean = true;",
    "scripts/extra.mts": "export const checked: boolean = true;",
    ".next/types/routes.d.ts": 'export type Route = "/";',
    ".next/dev/types/routes.d.ts": 'export type DevRoute = "/";',
    [contractTest]: `import fixture from '${fixtureImport}'; export const stream = fixture;`,
    "src/lib/inline.test.ts": "export const tested = true;",
    "src/lib/inline-jsx.test.tsx": "export const tested = true;",
    "src/lib/inline.spec.ts": "export const tested = true;",
    "src/lib/inline-jsx.spec.tsx": "export const tested = true;",
    "tests/plain.ts": "export const tested = true;",
    "e2e/plain.ts": "export const tested = true;",
    "src/test/setup.ts": "export const setup = true;",
  };
  for (const [name, content] of Object.entries(files))
    write(project, name, content);
  if (withSharedFixture)
    write(
      root,
      "tests/fixtures/reliability_session_stream.json",
      '{"synthetic":true}',
    );
  return project;
}

function compile(project: string, configName: string) {
  const configPath = path.join(project, configName);
  const parsed = ts.getParsedCommandLineOfConfigFile(
    configPath,
    {},
    {
      ...ts.sys,
      onUnRecoverableConfigFileDiagnostic(diagnostic) {
        throw new Error(
          ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"),
        );
      },
    },
  );
  if (!parsed) throw new Error("Synthetic TypeScript config was not parsed");
  const program = ts.createProgram({
    rootNames: parsed.fileNames,
    options: parsed.options,
  });
  const diagnostics = [...parsed.errors, ...ts.getPreEmitDiagnostics(program)];
  return {
    options: parsed.options,
    roots: parsed.fileNames.map((name) =>
      path.relative(project, name).replaceAll("\\", "/"),
    ),
    sources: program
      .getSourceFiles()
      .map((file) =>
        path.relative(project, file.fileName).replaceAll("\\", "/"),
      ),
    diagnostics: diagnostics.map((diagnostic) => ({
      code: diagnostic.code,
      file: diagnostic.file
        ? path.relative(project, diagnostic.file.fileName).replaceAll("\\", "/")
        : null,
      message: ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"),
    })),
  };
}

function buildConfigPath(): string {
  return nextConfig.typescript?.tsconfigPath ?? "tsconfig.json";
}

afterEach(() => {
  for (const root of ownedRoots.splice(0)) {
    expect(path.dirname(realpathSync(root))).toBe(ownedParent);
    rmSync(root, { recursive: true, force: true });
  }
});

describe("production TypeScript discovery and full test coverage", () => {
  it("checks a Chat-only context without discovering an unrelated test fixture import", () => {
    const result = compile(fixture(), buildConfigPath());
    expect(result.diagnostics).toEqual([]);
    for (const file of testFiles) {
      expect(result.roots).not.toContain(file);
      expect(result.sources).not.toContain(file);
    }
  });

  it("preserves real application, config, scripts, MTS and generated type roots", () => {
    const project = fixture();
    const result = compile(project, buildConfigPath());
    for (const file of [
      "src/app/page.tsx",
      "next.config.ts",
      "scripts/check.ts",
      "scripts/extra.mts",
      ".next/types/routes.d.ts",
      ".next/dev/types/routes.d.ts",
    ]) {
      expect(result.roots).toContain(file);
      expect(result.sources).toContain(file);
    }
    const base = compile(project, "tsconfig.json");
    const { configFilePath: buildPath, ...buildOptions } = result.options;
    const { configFilePath: basePath, ...baseOptions } = base.options;
    expect(buildPath).toBeDefined();
    expect(basePath).toBeDefined();
    expect(buildOptions).toEqual(baseOptions);
    expect(nextConfig.typescript?.ignoreBuildErrors).not.toBe(true);
  });

  it("keeps all test forms and the shared fixture in the original complete typecheck", () => {
    const project = fixture(true);
    const result = compile(project, "tsconfig.json");
    expect(result.diagnostics).toEqual([]);
    for (const file of testFiles) {
      expect(result.roots).toContain(file);
      expect(result.sources).toContain(file);
    }
    expect(result.sources).toContain(
      "../tests/fixtures/reliability_session_stream.json",
    );
    write(
      project,
      "tests/plain.ts",
      'export const invalid: number = "synthetic";',
    );
    expect(compile(project, "tsconfig.json").diagnostics).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: 2322, file: "tests/plain.ts" }),
      ]),
    );
  });

  it("does not hide a real application type error in the build configuration", () => {
    const project = fixture(true);
    write(
      project,
      "src/app/page.tsx",
      'export const invalid: number = "synthetic";',
    );
    expect(compile(project, buildConfigPath()).diagnostics).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: 2322, file: "src/app/page.tsx" }),
      ]),
    );
  });

  it("still follows indirect application imports into excluded tests and rejects missing modules", () => {
    const project = fixture();
    write(
      project,
      "src/app/page.tsx",
      'export { stream } from "../lib/helper";',
    );
    write(
      project,
      "src/lib/helper.ts",
      'export { stream } from "./providers/__tests__/contract.test";',
    );
    const result = compile(project, buildConfigPath());
    expect(result.sources).toContain(contractTest);
    expect(result.diagnostics).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: 2307, file: contractTest }),
      ]),
    );
  });
});
