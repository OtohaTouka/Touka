import { describe, it, expect } from 'vitest';
import { findOrphans, TEST_INCLUDE_GLOBS } from '../orphaned_tests_logic';

describe('orphaned tests logic — findOrphans', () => {
  it('回傳磁碟上存在、但未被收錄的測試檔', () => {
    const all = [
      'src/a/__tests__/a.test.ts',
      'src/b/b.test.tsx', // 同層放置但若 include 沒收 → 孤兒
      'tests/c.test.ts',
    ];
    const collected = ['src/a/__tests__/a.test.ts', 'tests/c.test.ts'];
    expect(findOrphans(all, collected)).toEqual(['src/b/b.test.tsx']);
  });

  it('全部被收錄時回傳空陣列', () => {
    const all = ['x.test.ts', 'y.spec.tsx'];
    expect(findOrphans(all, all)).toEqual([]);
  });

  it('比對前正規化反斜線路徑（Windows）', () => {
    expect(findOrphans(['src\\a\\a.test.ts'], ['src/a/a.test.ts'])).toEqual([]);
  });

  it('結果去重且排序', () => {
    expect(findOrphans(['b.test.ts', 'a.test.ts', 'a.test.ts'], [])).toEqual([
      'a.test.ts',
      'b.test.ts',
    ]);
  });

  it('Playwright e2e spec 不得被誤判為 Vitest 孤兒', () => {
    expect(findOrphans(['e2e/mcp-oauth.spec.ts'], [])).toEqual([]);
  });

  it('TEST_INCLUDE_GLOBS 同時涵蓋 src / tests / scripts 三處', () => {
    expect(TEST_INCLUDE_GLOBS.some((g) => g.startsWith('src/'))).toBe(true);
    expect(TEST_INCLUDE_GLOBS.some((g) => g.startsWith('tests/'))).toBe(true);
    expect(TEST_INCLUDE_GLOBS.some((g) => g.startsWith('scripts/'))).toBe(true);
  });
});
