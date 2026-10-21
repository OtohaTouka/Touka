import { describe, it, expect } from 'vitest';
import { Project, SourceFile } from 'ts-morph';
import {
  checkConsoleViolations,
  DEFAULT_CONSOLE_GUARD_CONFIG,
  ConsoleGuardConfig,
} from '../../scripts/lib/console_guard_logic';

/** 動態建立一個以指定路徑為 filePath 的 SourceFile，供測試使用 */
function makeSourceFile(project: Project, filePath: string, content: string): SourceFile {
  return project.createSourceFile(filePath, content, { overwrite: true });
}

describe('console_guard_logic', () => {
  // ────────────────────────────────────────
  // M7 目標：error_handler.ts 不應在整檔白名單中
  // ────────────────────────────────────────
  it('DEFAULT_CONSOLE_GUARD_CONFIG 不應將 error_handler.ts 列在整檔白名單中', () => {
    expect(DEFAULT_CONSOLE_GUARD_CONFIG.whitelistFiles).not.toContain('error_handler.ts');
  });

  it('error_handler.ts 內的 console.error 若無 noqa 標記，應被偵測為違規', () => {
    const project = new Project({ useInMemoryFileSystem: true });
    // 模擬在 src/lib 路徑下（符合 scanPaths）
    const sf = makeSourceFile(
      project,
      '/project/src/lib/error_handler.ts',
      `export function logSSOTError(error: unknown, context: string) {
  const msg = error instanceof Error ? error.message : String(error);
  console.error(\`[\${context}] Error:\`, msg, error);
}`,
    );

    const violations = checkConsoleViolations(sf, DEFAULT_CONSOLE_GUARD_CONFIG);
    expect(violations.length).toBe(1);
    expect(violations[0].line).toBe(3);
  });

  it('error_handler.ts 內的 console.error 若有行級 noqa 豃免標記，應被忽略', () => {
    const project = new Project({ useInMemoryFileSystem: true });
    // 將 noqa 註解拆成兩段以避免被 NOQA Registry Guard 誤操為真實豃免
    const noqaComment = '/' + '/ noqa: CONSOLE_BAN';
    const sf = makeSourceFile(
      project,
      '/project/src/lib/error_handler.ts',
      `export function logSSOTError(error: unknown, context: string) {
  const msg = error instanceof Error ? error.message : String(error);
  console.error(\`[\${context}] Error:\`, msg, error); ${noqaComment}
}`,
    );

    const violations = checkConsoleViolations(sf, DEFAULT_CONSOLE_GUARD_CONFIG);
    expect(violations.length).toBe(0);
  });

  it('不在 scanPaths 下的檔案應直接略過，不產生違規', () => {
    const project = new Project({ useInMemoryFileSystem: true });
    const sf = makeSourceFile(
      project,
      '/project/scripts/some_script.ts',
      `console.error('direct error call');`,
    );

    const violations = checkConsoleViolations(sf, DEFAULT_CONSOLE_GUARD_CONFIG);
    expect(violations.length).toBe(0);
  });

  it('測試檔案（.test.ts）應被 excludePatterns 排除，不產生違規', () => {
    const project = new Project({ useInMemoryFileSystem: true });
    const sf = makeSourceFile(
      project,
      '/project/src/lib/foo.test.ts',
      `console.error('test direct call');`,
    );

    const violations = checkConsoleViolations(sf, DEFAULT_CONSOLE_GUARD_CONFIG);
    expect(violations.length).toBe(0);
  });

  it('whitelistFiles 中的檔案應整個略過（白名單功能本身需正常運作）', () => {
    const project = new Project({ useInMemoryFileSystem: true });
    const config: ConsoleGuardConfig = {
      ...DEFAULT_CONSOLE_GUARD_CONFIG,
      whitelistFiles: ['my_legacy.ts'],
    };
    const sf = makeSourceFile(
      project,
      '/project/src/lib/my_legacy.ts',
      `console.error('legacy direct call');`,
    );

    const violations = checkConsoleViolations(sf, config);
    expect(violations.length).toBe(0);
  });
});
