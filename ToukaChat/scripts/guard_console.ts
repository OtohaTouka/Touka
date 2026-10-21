import path from 'path';
import { Project } from 'ts-morph';
import glob from 'fast-glob';
import { checkConsoleViolations, DEFAULT_CONSOLE_GUARD_CONFIG, Violation } from './lib/console_guard_logic';

async function main(): Promise<void> {
  const __filename = new URL(import.meta.url).pathname.substring(
    process.platform === 'win32' ? 1 : 0,
  );
  const __dirname = path.dirname(__filename);
  const projectRoot = path.resolve(__dirname, '..');

  console.log(`🔍 Touka Frontend Console Ban Guard 正在啟動...`);
  console.log(`📂 專案目錄: ${projectRoot}\n`);

  try {
    const project = new Project();

    // 掃描 scanPaths 下的所有 .ts / .tsx 檔案
    const patterns = DEFAULT_CONSOLE_GUARD_CONFIG.scanPaths.map(
      (p) => `${p}**/*.{ts,tsx}`,
    );
    const files = await glob(patterns, {
      cwd: projectRoot,
      absolute: true,
    });

    for (const file of files) {
      project.addSourceFileAtPath(file);
    }

    const allViolations: Violation[] = [];

    for (const sourceFile of project.getSourceFiles()) {
      const violations = checkConsoleViolations(
        sourceFile,
        DEFAULT_CONSOLE_GUARD_CONFIG,
      );
      allViolations.push(...violations);
    }

    if (allViolations.length === 0) {
      console.log(`🎉 恭喜！未發現直接 console.log/warn/error 呼叫違規。`);
      process.exit(0);
    }

    console.error(`🚨 [CONSOLE_BAN] 偵測到直接 console 呼叫！`);
    console.error(
      `💡 規定：前端生產代碼禁止直接呼叫 console.log/warn/error()，請改用 logSSOTError() 或 ERRORS 常數封裝層。\n`,
    );

    for (const v of allViolations) {
      const relPath = path.relative(projectRoot, v.file);
      console.error(`  📄 檔案: ${relPath}:${v.line}`);
      console.error(`  ❌ 違規代碼: ${v.code}`);
      console.error(`  📝 說明: ${v.message}\n`);
    }

    console.error(`❌ 總計發現 ${allViolations.length} 處 CONSOLE_BAN 違規。`);
    process.exit(1);
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`💥 執行發生錯誤: ${message}`);
    process.exit(1);
  }
}

main();
