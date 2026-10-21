import { describe, it, expect } from 'vitest';
import { Project } from 'ts-morph';
import { checkConsoleViolations, DEFAULT_CONSOLE_GUARD_CONFIG } from '../console_guard_logic';

describe('Console Ban Guard Logic', () => {
  const project = new Project({ useInMemoryFileSystem: true });

  // ── 違規偵測 ─────────────────────────────────────────────────────────────

  it('should detect console.log in src/lib/', () => {
    const code = `console.log("[Touka] Connections updated...");`;
    const sf = project.createSourceFile('src/lib/SystemStatusContext.tsx', code, { overwrite: true });
    const violations = checkConsoleViolations(sf, DEFAULT_CONSOLE_GUARD_CONFIG);
    expect(violations.length).toBe(1);
    expect(violations[0].message).toContain('CONSOLE_BAN');
  });

  it('should detect console.warn in src/hooks/', () => {
    const code = `console.warn("Could not resolve mention");`;
    const sf = project.createSourceFile('src/hooks/useSomething.ts', code, { overwrite: true });
    const violations = checkConsoleViolations(sf, DEFAULT_CONSOLE_GUARD_CONFIG);
    expect(violations.length).toBe(1);
  });

  it('should detect console.error in src/contexts/', () => {
    const code = `console.error("Auth failed");`;
    const sf = project.createSourceFile('src/contexts/AuthContext.tsx', code, { overwrite: true });
    const violations = checkConsoleViolations(sf, DEFAULT_CONSOLE_GUARD_CONFIG);
    expect(violations.length).toBe(1);
  });

  it('should detect console.error in src/app/', () => {
    const code = `console.error(err);`;
    const sf = project.createSourceFile('src/app/login/page.tsx', code, { overwrite: true });
    const violations = checkConsoleViolations(sf, DEFAULT_CONSOLE_GUARD_CONFIG);
    expect(violations.length).toBe(1);
  });

  // ── 合法調用不誤報 ────────────────────────────────────────────────────────

  it('should flag error_handler.ts if it does not have noqa comment', () => {
    const code = `console.error(\`[\${context}] Error:\`, msg, error);`;
    const sf = project.createSourceFile('src/lib/error_handler.ts', code, { overwrite: true });
    const violations = checkConsoleViolations(sf, DEFAULT_CONSOLE_GUARD_CONFIG);
    expect(violations.length).toBe(1);
  });

  it('should NOT scan .test.ts files', () => {
    const code = `console.log("debug test output");`;
    const sf = project.createSourceFile('src/hooks/useData.test.ts', code, { overwrite: true });
    const violations = checkConsoleViolations(sf, DEFAULT_CONSOLE_GUARD_CONFIG);
    expect(violations.length).toBe(0);
  });

  it('should NOT scan .stories.tsx files', () => {
    const code = `console.log('Undo');`;
    const sf = project.createSourceFile('src/components/ui/sonner.stories.tsx', code, { overwrite: true });
    const violations = checkConsoleViolations(sf, DEFAULT_CONSOLE_GUARD_CONFIG);
    expect(violations.length).toBe(0);
  });

  it('should NOT flag files outside scanPaths (e.g. src/components/)', () => {
    const code = `console.log("in components");`;
    const sf = project.createSourceFile('src/components/SomeComponent.tsx', code, { overwrite: true });
    const violations = checkConsoleViolations(sf, DEFAULT_CONSOLE_GUARD_CONFIG);
    expect(violations.length).toBe(0);
  });

  // ── noqa 豁免機制 ────────────────────────────────────────────────────────

  it('should respect ignoredLines noqa injection', () => {
    const code = `console.warn("Could not resolve mention");`;
    const ignored: Record<number, Set<string>> = { 1: new Set(['CONSOLE_BAN']) };
    const sf = project.createSourceFile('src/lib/mentions.ts', code, { overwrite: true });
    const violations = checkConsoleViolations(sf, DEFAULT_CONSOLE_GUARD_CONFIG, ignored);
    expect(violations.length).toBe(0);
  });
});
