import { Project } from 'ts-morph';
import path from 'path';
import { checkThemeViolations } from './lib/theme_guard_logic';

async function main() {
  const project = new Project();
  // 修復路徑解析，在 Windows 上處理 URL 的 file:// 協議
  const __filename = new URL(import.meta.url).pathname.substring(process.platform === 'win32' ? 1 : 0);
  const __dirname = path.dirname(decodeURIComponent(__filename));
  const rootDir = path.resolve(__dirname, '../src');

  // 1. 準備受檢檔案
  const args = process.argv.slice(2);
  if (args.length > 0) {
      for (const arg of args) {
          const absolutePath = path.resolve(process.cwd(), arg).replace(/\\/g, '/');
          if (arg.endsWith('.ts') || arg.endsWith('.tsx')) {
              project.addSourceFileAtPath(absolutePath);
          } else {
              project.addSourceFilesAtPaths([
                  `${absolutePath}/**/*.{ts,tsx}`
              ]);
          }
      }
  } else {
      const tsFiles = [
          `${rootDir}/**/*.{ts,tsx}`,
          `!${rootDir}/lib/design-system/tokens.ts` // 排除源頭
      ].map(p => p.replace(/\\/g, '/'));
      project.addSourceFilesAtPaths(tsFiles);
  }

  console.log('🔍 正在執行 Touka Theme SSOT 檢查 (Aesthetic Guard)...');

  let totalViolations = 0;
  const allViolations = [];

  // 2. 檢查 TS/TSX 檔案
  for (const sourceFile of project.getSourceFiles()) {
    const violations = checkThemeViolations(sourceFile);
    if (violations.length > 0) {
      totalViolations += violations.length;
      allViolations.push(...violations);
    }
  }

  // 3. 輸出報告
  if (totalViolations > 0) {
    console.error(`\n❌ 發現 ${totalViolations} 處主題主權違規：\n`);
    allViolations.forEach(v => {
      console.error(`檔案: ${path.relative(process.cwd(), v.file)}:${v.line}`);
      console.error(`代碼: ${v.code}`);
      console.error(`原因: ${v.message}\n`);
    });
    process.exit(1);
  } else {
    console.log('\n✅ 主題主權檢查通過！視覺組件完全支援動態主題。');
    process.exit(0);
  }
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
