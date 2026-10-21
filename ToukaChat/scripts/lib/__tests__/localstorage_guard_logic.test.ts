import { describe, it, expect } from 'vitest';
import { Project } from 'ts-morph';
import { checkLocalStorageViolations, DEFAULT_LOCALSTORAGE_GUARD_CONFIG } from '../localstorage_guard_logic';

describe('localStorage SSOT Guard Logic', () => {
  const project = new Project({ useInMemoryFileSystem: true });

  // ── 違規偵測 ────────────────────────────────────────────────────────────

  it('should detect localStorage.getItem() in hooks', () => {
    const code = `const val = localStorage.getItem("touka_key");`;
    const sf = project.createSourceFile('src/hooks/useData.ts', code, { overwrite: true });
    const violations = checkLocalStorageViolations(sf, DEFAULT_LOCALSTORAGE_GUARD_CONFIG);
    expect(violations.length).toBe(1);
    expect(violations[0].message).toContain('LOCALSTORAGE_SSOT');
  });

  it('should detect localStorage.setItem() in app', () => {
    const code = `localStorage.setItem("touka_key", JSON.stringify({}));`;
    const sf = project.createSourceFile('src/app/page.tsx', code, { overwrite: true });
    const violations = checkLocalStorageViolations(sf, DEFAULT_LOCALSTORAGE_GUARD_CONFIG);
    expect(violations.length).toBe(1);
  });

  it('should detect window.localStorage.getItem()', () => {
    const code = `const val = window.localStorage.getItem("touka_key");`;
    const sf = project.createSourceFile('src/app/page.tsx', code, { overwrite: true });
    const violations = checkLocalStorageViolations(sf, DEFAULT_LOCALSTORAGE_GUARD_CONFIG);
    expect(violations.length).toBe(1);
  });

  it('should detect localStorage.removeItem()', () => {
    const code = `localStorage.removeItem("touka_key");`;
    const sf = project.createSourceFile('src/hooks/useData.ts', code, { overwrite: true });
    const violations = checkLocalStorageViolations(sf, DEFAULT_LOCALSTORAGE_GUARD_CONFIG);
    expect(violations.length).toBe(1);
  });

  // ── 合法調用不誤報 ────────────────────────────────────────────────────

  it('should NOT flag whitelisted file useLocalStorage.ts', () => {
    const code = `const item = window.localStorage.getItem(key);`;
    const sf = project.createSourceFile('src/hooks/useLocalStorage.ts', code, { overwrite: true });
    const violations = checkLocalStorageViolations(sf, DEFAULT_LOCALSTORAGE_GUARD_CONFIG);
    expect(violations.length).toBe(0);
  });

  // ── H1 修復驗證：connections.ts 不再享有白名單豁免 ────────────────────────
  // NOTE: 這些測試在守衛白名單移除 connections.ts 之前為 RED（預期失敗）

  it('should flag direct localStorage.getItem() in connections.ts (no longer whitelisted)', () => {
    const code = `const saved = localStorage.getItem("touka_connections");`;
    const sf = project.createSourceFile('src/lib/connections.ts', code, { overwrite: true });
    // 使用不含 connections.ts 的已修復設定
    const fixedConfig = {
      ...DEFAULT_LOCALSTORAGE_GUARD_CONFIG,
      whitelistFiles: ['useLocalStorage.ts'],
    };
    const violations = checkLocalStorageViolations(sf, fixedConfig);
    expect(violations.length).toBe(1);
    expect(violations[0].message).toContain('LOCALSTORAGE_SSOT');
  });

  it('should NOT flag getLocalStorageItem() call in connections.ts (correct SSOT pattern)', () => {
    const code = `
      import { getLocalStorageItem } from '../hooks/useLocalStorage';
      const conns = getLocalStorageItem("touka_connections", []);
    `;
    const sf = project.createSourceFile('src/lib/connections.ts', code, { overwrite: true });
    const fixedConfig = {
      ...DEFAULT_LOCALSTORAGE_GUARD_CONFIG,
      whitelistFiles: ['useLocalStorage.ts'],
    };
    const violations = checkLocalStorageViolations(sf, fixedConfig);
    expect(violations.length).toBe(0);
  });

  it('should NOT scan test files (.test.ts)', () => {
    const code = `localStorage.getItem("test_key");`;
    const sf = project.createSourceFile('src/hooks/useData.test.ts', code, { overwrite: true });
    const violations = checkLocalStorageViolations(sf, DEFAULT_LOCALSTORAGE_GUARD_CONFIG);
    expect(violations.length).toBe(0);
  });

  it('should respect // noqa: LOCALSTORAGE_SSOT suppression via ignoredLines', () => {
    const code = `localStorage.getItem("touka_key");`;
    // 模擬第 1 行有 LOCALSTORAGE_SSOT noqa 豁免
    const ignored: Record<number, Set<string>> = { 1: new Set(['LOCALSTORAGE_SSOT']) };
    const sf = project.createSourceFile('src/hooks/useStudio.ts', code, { overwrite: true });
    const violations = checkLocalStorageViolations(sf, DEFAULT_LOCALSTORAGE_GUARD_CONFIG, ignored);
    expect(violations.length).toBe(0);
  });
});
