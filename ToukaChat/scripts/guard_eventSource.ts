import path from 'path';
import { Project } from 'ts-morph';
import glob from 'fast-glob';
import { checkEventSourceViolations, DEFAULT_EVENTSOURCE_GUARD_CONFIG, Violation } from './lib/eventsource_guard_logic';

async function main(): Promise<void> {
  const __filename = new URL(import.meta.url).pathname.substring(
    process.platform === 'win32' ? 1 : 0,
  );
  const __dirname = path.dirname(__filename);
  const projectRoot = path.resolve(__dirname, '..');

  console.log(`🔍 Touka Frontend EventSource Token Guard 正在啟動...`);
  console.log(`📂 專案目錄: ${projectRoot}\n`);

  try {
    const project = new Project();

    const patterns = DEFAULT_EVENTSOURCE_GUARD_CONFIG.scanPaths.map(
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
      const violations = checkEventSourceViolations(
        sourceFile,
        DEFAULT_EVENTSOURCE_GUARD_CONFIG,
      );
      allViolations.push(...violations);
    }

    if (allViolations.length === 0) {
      console.log(`🎉 恭喜！未發現原生 EventSource 使用違規。`);
      process.exit(0);
    }

    console.error(`🚨 [EVENTSOURCE_SSOT_VIOLATION] 偵測到原生 EventSource 使用！`);
    console.error(
      `💡 規定（ADR-0040）：前端全面禁用原生 EventSource——其 URL 會把身份 token 洩漏至瀏覽器/代理 Access Logs。請改用基於 Header 驗證的 fetch + ReadableStream 串流（Authorization 標頭帶 token，token 絕不進 URL）。特殊非敏感案例（如本地 LSP 探針、不含敏感資料且無須認證）依 ADR-0040 規範以 EVENTSOURCE_SSOT 豁免標記放行，並登記於 NOQA_REGISTRY.md。\n`,
    );

    for (const v of allViolations) {
      const relPath = path.relative(projectRoot, v.file);
      console.error(`  📄 檔案: ${relPath}:${v.line}`);
      console.error(`  ❌ 違規代碼: ${v.code}`);
      console.error(`  📝 說明: ${v.message}\n`);
    }

    console.error(`❌ 總計發現 ${allViolations.length} 處 EventSource 違規。`);
    process.exit(1);
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`💥 執行發生錯誤: ${message}`);
    process.exit(1);
  }
}

main();
