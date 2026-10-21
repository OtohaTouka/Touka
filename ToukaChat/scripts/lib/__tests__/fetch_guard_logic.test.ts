import { describe, it, expect } from 'vitest';
import { Project } from 'ts-morph';
import { checkFetchViolations, DEFAULT_FETCH_GUARD_CONFIG } from '../fetch_guard_logic';

describe('Fetch SSOT Guard Logic', () => {
  const project = new Project({ useInMemoryFileSystem: true });

  // --- 違規偵測 ---

  it('should detect bare fetch() in hooks directory', () => {
    const code = `
      export const useData = () => {
        return fetch('/api/data');
      };
    `;
    const sourceFile = project.createSourceFile('src/hooks/useData.ts', code, { overwrite: true });
    const violations = checkFetchViolations(sourceFile, DEFAULT_FETCH_GUARD_CONFIG);

    expect(violations.length).toBe(1);
    expect(violations[0].message).toContain('fetchTouka');
    expect(violations[0].line).toBeGreaterThan(0);
  });

  it('should detect bare fetch() in lib directory', () => {
    const code = `
      export async function loadData() {
        const res = await fetch('/v1/models');
        return res.json();
      }
    `;
    const sourceFile = project.createSourceFile('src/lib/someUtil.ts', code, { overwrite: true });
    const violations = checkFetchViolations(sourceFile, DEFAULT_FETCH_GUARD_CONFIG);

    expect(violations.length).toBe(1);
    expect(violations[0].code).toContain("fetch(");
  });

  it('should detect bare fetch() in app directory', () => {
    const code = `
      export default function Page() {
        fetch('/v1/status');
        return null;
      }
    `;
    const sourceFile = project.createSourceFile('src/app/page.tsx', code, { overwrite: true });
    const violations = checkFetchViolations(sourceFile, DEFAULT_FETCH_GUARD_CONFIG);

    expect(violations.length).toBe(1);
  });

  it('should detect bare fetch() in contexts directory', () => {
    const code = `
      export const SomeContext = () => {
        const res = await fetch('/v1/auth/me');
      };
    `;
    const sourceFile = project.createSourceFile('src/contexts/SomeContext.tsx', code, { overwrite: true });
    const violations = checkFetchViolations(sourceFile, DEFAULT_FETCH_GUARD_CONFIG);

    expect(violations.length).toBe(1);
  });

  it('should detect multiple fetch() calls in one file', () => {
    const code = `
      export async function a() { await fetch('/v1/a'); }
      export async function b() { await fetch('/v1/b'); }
    `;
    const sourceFile = project.createSourceFile('src/lib/multi.ts', code, { overwrite: true });
    const violations = checkFetchViolations(sourceFile, DEFAULT_FETCH_GUARD_CONFIG);

    expect(violations.length).toBe(2);
  });

  // --- 合法調用不誤報 ---

  it('should NOT flag fetchTouka() calls', () => {
    const code = `
      import { fetchTouka } from '../lib/connections';
      export const useSettings = () => {
        fetchTouka(proxyUrl, '/v1/settings');
      };
    `;
    const sourceFile = project.createSourceFile('src/hooks/useSettings.ts', code, { overwrite: true });
    const violations = checkFetchViolations(sourceFile, DEFAULT_FETCH_GUARD_CONFIG);

    expect(violations.length).toBe(0);
  });

  it('should NOT flag functions with fetch as substring (fetchData, prefetch)', () => {
    const code = `
      async function fetchData() { return {}; }
      async function prefetchModel() { return {}; }
    `;
    const sourceFile = project.createSourceFile('src/lib/utils.ts', code, { overwrite: true });
    const violations = checkFetchViolations(sourceFile, DEFAULT_FETCH_GUARD_CONFIG);

    expect(violations.length).toBe(0);
  });

  // --- noqa 豁免 ---

  it('should NOT flag fetch() with // noqa: FETCH_SSOT on the same line', () => {
    const code = `
      export async function fetchTouka(proxyUrl: string, path: string) {
        const resp = await fetch(\`\${proxyUrl}\${path}\`); // noqa: FETCH_SSOT
        return resp;
      }
    `;
    const sourceFile = project.createSourceFile('src/lib/connections.ts', code, { overwrite: true });
    const violations = checkFetchViolations(sourceFile, DEFAULT_FETCH_GUARD_CONFIG);

    expect(violations.length).toBe(0);
  });

  it('should STILL flag fetch() with wrong noqa rule ID', () => {
    const code = `
      export async function load() {
        const res = await fetch('/v1/data'); // noqa: SOME_OTHER_RULE
        return res;
      }
    `;
    const sourceFile = project.createSourceFile('src/lib/wrong_noqa.ts', code, { overwrite: true });
    const violations = checkFetchViolations(sourceFile, DEFAULT_FETCH_GUARD_CONFIG);

    expect(violations.length).toBe(1);
  });

  // --- 測試沙盒排除 ---

  it('should NOT scan test files (*.test.ts)', () => {
    const code = `
      it('mocks fetch', () => {
        global.fetch = vi.fn().mockResolvedValue({ ok: true });
        fetch('/v1/test');
      });
    `;
    const sourceFile = project.createSourceFile('src/hooks/useData.test.ts', code, { overwrite: true });
    const violations = checkFetchViolations(sourceFile, DEFAULT_FETCH_GUARD_CONFIG);

    expect(violations.length).toBe(0);
  });

  it('should NOT scan test files (*.test.tsx)', () => {
    const code = `
      it('test component', () => {
        fetch('/v1/status');
      });
    `;
    const sourceFile = project.createSourceFile('src/components/Foo.test.tsx', code, { overwrite: true });
    const violations = checkFetchViolations(sourceFile, DEFAULT_FETCH_GUARD_CONFIG);

    expect(violations.length).toBe(0);
  });

  it('should NOT scan spec files (*.spec.ts)', () => {
    const code = `
      describe('spec', () => {
        fetch('/v1/spec');
      });
    `;
    const sourceFile = project.createSourceFile('src/lib/utils.spec.ts', code, { overwrite: true });
    const violations = checkFetchViolations(sourceFile, DEFAULT_FETCH_GUARD_CONFIG);

    expect(violations.length).toBe(0);
  });

  // --- 掃描範圍 ---

  it('should NOT scan files outside defined scanPaths', () => {
    // scripts/ 目錄本身不在掃描範圍
    const code = `
      await fetch('/v1/data');
    `;
    const sourceFile = project.createSourceFile('scripts/someScript.ts', code, { overwrite: true });
    const violations = checkFetchViolations(sourceFile, DEFAULT_FETCH_GUARD_CONFIG);

    expect(violations.length).toBe(0);
  });
});
