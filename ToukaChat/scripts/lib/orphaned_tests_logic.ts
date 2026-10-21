/**
 * Touka 測試收錄 SSOT。
 *
 * `TEST_INCLUDE_GLOBS` 是 vitest `test.include` 與「孤兒測試守衛」
 * (`scripts/guard_orphaned_tests.ts`) 的單一事實來源——兩者共用同一份
 * glob，確保「會被收錄執行的測試」與「守衛認定合法的測試」永不脫鉤。
 *
 * 本檔刻意零外部相依，才能被 vitest.config.ts 安全載入。
 */

/** vitest 收錄（且守衛視為合法）的測試檔位置。涵蓋 __tests__ 與同層放置。 */
export const TEST_INCLUDE_GLOBS: string[] = [
  'src/**/*.{test,spec}.{ts,tsx}',
  'tests/**/*.{test,spec}.{ts,tsx}',
  'scripts/**/*.{test,spec}.{ts,tsx}',
];

/** 掃描磁碟上「所有」測試檔的 glob（用以和收錄集合比對）。 */
export const ALL_TESTS_GLOB = '**/*.{test,spec}.{ts,tsx}';

/** 由其他 runner 收錄的測試根；不得要求 Vitest 重複收錄。 */
export const NON_VITEST_TEST_PREFIXES: string[] = ['e2e/'];

/** glob 掃描時應忽略的目錄。 */
export const TEST_GLOB_IGNORE: string[] = [
  '**/node_modules/**',
  '**/.git/**',
  '**/dist/**',
  '**/.next/**',
  '**/coverage/**',
];

function normalize(p: string): string {
  return p.replace(/\\/g, '/').replace(/^\.\//, '');
}

/**
 * 找出「孤兒測試」：存在於磁碟、卻不在 vitest 收錄集合內的測試檔。
 *
 * @param allTestFiles 磁碟上所有測試檔（相對專案根）。
 * @param collectedTestFiles vitest 依 include 實際會收錄的測試檔。
 * @returns 未被收錄的測試檔（正規化、去重、排序）。
 */
export function findOrphans(
  allTestFiles: string[],
  collectedTestFiles: string[],
): string[] {
  const collected = new Set(collectedTestFiles.map(normalize));
  const orphans = new Set(
    allTestFiles
      .map(normalize)
      .filter(
        (file) =>
          !NON_VITEST_TEST_PREFIXES.some((prefix) => file.startsWith(prefix)),
      )
      .filter((file) => !collected.has(file)),
  );
  return [...orphans].sort();
}
