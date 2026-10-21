import { Project } from 'ts-morph';
import path from 'path';
import { checkHookViolations } from './lib/hook_guard_logic';

async function main() {
  const project = new Project();
  const __filename = new URL(import.meta.url).pathname.substring(process.platform === 'win32' ? 1 : 0);
  const __dirname = path.dirname(__filename);
  const rootDir = path.resolve(__dirname, '../src');
  
  // 僅加入 src/components 下的檔案
  project.addSourceFilesAtPaths(`${rootDir}/components/**/*.{ts,tsx}`);

  console.log('🔍 正在執行 Touka Hook Sovereignty (DIP) 檢查...');

  let totalViolations = 0;
  const allViolations = [];

  for (const sourceFile of project.getSourceFiles()) {
    const violations = checkHookViolations(sourceFile);
    if (violations.length > 0) {
      totalViolations += violations.length;
      allViolations.push(...violations);
    }
  }

  if (totalViolations > 0) {
    console.error(`\n❌ 發現 ${totalViolations} 處 Hook 主權違規：\n`);
    allViolations.forEach(v => {
      console.error(`檔案: ${path.relative(process.cwd(), v.file)}:${v.line}`);
      console.error(`代碼: ${v.code}`);
      console.error(`原因: ${v.message}\n`);
    });
    console.error('💡 解決方案：請將資料存取邏輯遷移至 src/hooks/ 或 src/lib/ 中。');
    process.exit(1);
  } else {
    console.log('\n✅ Hook 主權檢查通過！組件與資料層解耦良好。');
    process.exit(0);
  }
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
