import { Project, SyntaxKind } from 'ts-morph';
import path from 'path';

async function main() {
  const project = new Project();
  const __filename = new URL(import.meta.url).pathname.substring(process.platform === 'win32' ? 1 : 0);
  const __dirname = path.dirname(decodeURIComponent(__filename));
  const rootDir = path.resolve(__dirname, '../src');
  
  project.addSourceFilesAtPaths(`${rootDir}/**/*.{ts,tsx}`);

  console.log('🔍 正在執行 Touka Error SSOT 檢查...');

  let violations = 0;

  for (const sourceFile of project.getSourceFiles()) {
    // 檢查 toast.error() 或 benzeri 調用
    sourceFile.getDescendantsOfKind(SyntaxKind.CallExpression).forEach(node => {
      const expression = node.getExpression().getText();
      if (expression.includes('toast.error') || expression.includes('console.error')) {
        const args = node.getArguments();
        if (args.length > 0) {
          const firstArg = args[0];
          let isViolation = false;
          let violationText = '';
          let reason = '';

          if (firstArg.getKind() === SyntaxKind.StringLiteral) {
            isViolation = true;
            violationText = firstArg.getText();
            reason = '硬編碼錯誤字串字面量';
          } else {
            const targetNodes = [firstArg, ...firstArg.getDescendants()];
            for (const tNode of targetNodes) {
              const kind = tNode.getKind();
              if (
                kind === SyntaxKind.StringLiteral ||
                kind === SyntaxKind.NoSubstitutionTemplateLiteral ||
                kind === SyntaxKind.TemplateExpression
              ) {
                const text = tNode.getText();
                if (/[\u4e00-\u9fa5]/.test(text)) {
                  isViolation = true;
                  violationText = text;
                  reason = '硬編碼中文錯誤字串';
                  break;
                }
              }
            }
          }

          if (isViolation) {
            console.error(`🚨 [ERROR_SSOT_VIOLATION]`);
            console.error(`  📄 檔案: ${path.relative(process.cwd(), sourceFile.getFilePath())}:${node.getStartLineNumber()}`);
            console.error(`  ❌ ${reason}: ${violationText}`);
            console.error(`  💡 建議: 請使用 lib/error_codes.ts 中的 ERRORS 常數。\n`);
            violations++;
          }
        }
      }
    });
  }

  if (violations === 0) {
    console.log('✅ 錯誤碼主權檢查通過！');
    process.exit(0);
  } else {
    console.error(`❌ 總計發現 ${violations} 處錯誤碼違規。`);
    process.exit(1);
  }
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
