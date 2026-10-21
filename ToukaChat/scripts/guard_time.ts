import { Project } from 'ts-morph';
import path from 'path';
import { checkTimeViolations } from './lib/time_guard_logic';

async function main() {
  const project = new Project();
  const __filename = new URL(import.meta.url).pathname.substring(process.platform === 'win32' ? 1 : 0);
  const __dirname = path.dirname(__filename);
  const rootDir = path.resolve(__dirname, '../src');
  project.addSourceFilesAtPaths(`${rootDir}/**/*.{ts,tsx}`);

  console.log('🔍 正在執行 Touka Time SSOT 檢查...');

  let totalViolations = 0;
  const allViolations = [];

  for (const sourceFile of project.getSourceFiles()) {
    const violations = checkTimeViolations(sourceFile);
    if (violations.length > 0) {
      totalViolations += violations.length;
      allViolations.push(...violations);
    }
  }

  if (totalViolations > 0) {
    console.error(`\n❌ 發現 ${totalViolations} 處時間主權違規：\n`);
    allViolations.forEach(v => {
      console.error(`檔案: ${path.relative(process.cwd(), v.file)}:${v.line}`);
      console.error(`代碼: ${v.code}`);
      console.error(`原因: ${v.message}\n`);
    });
    process.exit(1);
  } else {
    console.log('\n✅ 時間主權檢查通過！全系統時區同步良好。');
    process.exit(0);
  }
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
