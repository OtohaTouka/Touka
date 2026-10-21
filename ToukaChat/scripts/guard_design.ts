import { Project } from 'ts-morph';
import path from 'path';
import { readFileSync } from 'node:fs';
import fg from 'fast-glob';
import { checkDesignViolations, checkCssViolations } from './lib/design_guard_logic';

async function main() {
  const project = new Project();
  // 修復路徑解析，在 Windows 上處理 URL 的 file:// 協議
  const __filename = new URL(import.meta.url).pathname.substring(process.platform === 'win32' ? 1 : 0);
  const __dirname = path.dirname(decodeURIComponent(__filename));
  const rootDir = path.resolve(__dirname, '../src');

  // 1. 準備受檢檔案 (排除 SSOT 源頭)
  const args = process.argv.slice(2);
  if (args.length > 0) {
      for (const arg of args) {
          const absolutePath = path.resolve(process.cwd(), arg).replace(/\\/g, '/');
          if (arg.endsWith('.ts') || arg.endsWith('.tsx')) {
              project.addSourceFileAtPath(absolutePath);
          } else {
              // 假設是目錄，遞歸尋找
              project.addSourceFilesAtPaths([
                  `${absolutePath}/**/*.{ts,tsx}`,
                  `!${absolutePath}/**/tokens.ts`
              ]);
          }
      }
  } else {
      const tsFiles = [`${rootDir}/**/*.{ts,tsx}`, `!${rootDir}/lib/design-system/tokens.ts`].map(p => p.replace(/\\/g, '/'));
      project.addSourceFilesAtPaths(tsFiles);
  }

  console.log('🔍 正在執行 Touka Design Token SSOT 檢查...');

  let totalViolations = 0;
  const allViolations = [];

  // 2. 檢查 TS/TSX 檔案
  for (const sourceFile of project.getSourceFiles()) {
    const violations = checkDesignViolations(sourceFile);
    if (violations.length > 0) {
      totalViolations += violations.length;
      allViolations.push(...violations);
    }
  }

  // 3. 檢查 CSS 檔案 (排除 globals.css)
  const foundCss = await fg(`${rootDir}/**/*.css`.replace(/\\/g, '/'), {
      ignore: [`${rootDir}/app/globals.css`.replace(/\\/g, '/')]
  });

  for (const cssPath of foundCss) {
      const content = readFileSync(cssPath, 'utf-8');
      const violations = checkCssViolations(content, cssPath);
      if (violations.length > 0) {
          totalViolations += violations.length;
          allViolations.push(...violations);
      }
  }

  // 4. 輸出報告
  if (totalViolations > 0) {
    console.error(`\n❌ 發現 ${totalViolations} 處設計權杖主權違規：\n`);
    allViolations.forEach(v => {
      console.error(`檔案: ${path.relative(process.cwd(), v.file)}:${v.line}`);
      console.error(`代碼: ${v.code}`);
      console.error(`原因: ${v.message}\n`);
    });
    process.exit(1);
  } else {
    console.log('\n✅ 設計權杖主權檢查通過！視覺規範符合 SSOT。');
    process.exit(0);
  }
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
