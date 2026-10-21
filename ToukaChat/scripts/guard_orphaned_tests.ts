import path from 'path';
import glob from 'fast-glob';
import {
  TEST_INCLUDE_GLOBS,
  ALL_TESTS_GLOB,
  TEST_GLOB_IGNORE,
  findOrphans,
} from './lib/orphaned_tests_logic';

/**
 * 孤兒測試守衛：找出存在於磁碟、卻不在 Vitest 或已登錄其他 runner 範圍內的測試檔。
 * 這類檔案會「靜默不執行」——曾導致 HeartbeatScheduler/守衛邏輯等測試從未被跑到。
 */
async function main(): Promise<void> {
  const __filename = new URL(import.meta.url).pathname.substring(
    process.platform === 'win32' ? 1 : 0,
  );
  const __dirname = path.dirname(__filename);
  const projectRoot = path.resolve(__dirname, '..');

  console.log(`🔍 Touka Orphaned Tests Guard 正在啟動...`);
  console.log(`📂 專案目錄: ${projectRoot}\n`);

  const globOpts = { cwd: projectRoot, ignore: TEST_GLOB_IGNORE };
  const allTestFiles = await glob(ALL_TESTS_GLOB, globOpts);
  const collectedTestFiles = await glob(TEST_INCLUDE_GLOBS, globOpts);
  const orphans = findOrphans(allTestFiles, collectedTestFiles);

  if (orphans.length === 0) {
    console.log(
      `🎉 孤兒測試守衛通過！全部 ${allTestFiles.length} 個測試檔皆由已登錄 runner 收錄。`,
    );
    process.exit(0);
  }

  console.error(
    `🚨 [ORPHAN_TEST] 偵測到 ${orphans.length} 個孤兒測試（存在於磁碟，但不在任何已登錄 runner 範圍內，將靜默不執行）：`,
  );
  for (const f of orphans) {
    console.error(`  📄 ${f}`);
  }
  console.error(
    `\n💡 請把它移到會被收錄的位置（見 scripts/lib/orphaned_tests_logic.ts 的 runner SSOT），` +
      `通常是對應的 __tests__/ 目錄。`,
  );
  process.exit(1);
}

main();
