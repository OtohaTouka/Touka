import { SourceFile, SyntaxKind } from 'ts-morph';

export interface Violation {
  file: string;
  line: number;
  message: string;
  code: string;
}

export function checkTimeViolations(sourceFile: SourceFile): Violation[] {
  const violations: Violation[] = [];
  const filePath = sourceFile.getFilePath();

  // 豁免名單：time_utils.ts 與 測試檔案
  if (filePath.endsWith('time_utils.ts') || 
      filePath.includes('/tests/') || 
      filePath.includes('/__tests__/') || 
      filePath.includes('/test/')) {
    return [];
  }

  // 1. 檢查 new Date()
  sourceFile.getDescendantsOfKind(SyntaxKind.NewExpression).forEach(node => {
    if (node.getExpression().getText() === 'Date') {
      const args = node.getArguments();
      // 如果沒有參數，或者是 Date.now() 類型的調用
      if (args.length === 0) {
        violations.push({
          file: filePath,
          line: node.getStartLineNumber(),
          message: '禁止直接使用 new Date() 獲取當前時間。請使用 getSystemNow() 或 formatToTaipeiTime()。',
          code: node.getText()
        });
      }
    }
  });

  // 2. 檢查 Date.now()
  sourceFile.getDescendantsOfKind(SyntaxKind.CallExpression).forEach(node => {
    const expression = node.getExpression();
    const text = expression.getText();

    if (text === 'Date.now') {
      violations.push({
        file: filePath,
        line: node.getStartLineNumber(),
        message: '禁止直接使用 Date.now()。請使用 getSystemEpoch()。',
        code: node.getText()
      });
    }

    // 3. 檢查 Date 顯示方法（繞過時間 SSOT）：toLocale*String / toDateString / toTimeString
    const forbiddenMethods = [
      'toLocaleTimeString', 'toLocaleDateString', 'toLocaleString',
      'toDateString', 'toTimeString',
    ];
    if (SyntaxKind.PropertyAccessExpression === expression.getKind()) {
      const methodName = expression.getLastChild()?.getText();
      if (methodName && forbiddenMethods.includes(methodName)) {
        violations.push({
          file: filePath,
          line: node.getStartLineNumber(),
          message: `禁止使用 .${methodName}() 顯示時間。日期請用 formatToTaipeiTime()（鎖定台北時區）；數字格式化請改用 Intl.NumberFormat。`,
          code: node.getText()
        });
      }
    }

    // 4. 檢查 Intl.DateTimeFormat
    if (text.includes('Intl.DateTimeFormat')) {
      violations.push({
        file: filePath,
        line: node.getStartLineNumber(),
        message: '禁止在 UI 中直接使用 Intl.DateTimeFormat。請使用 time_utils 中的統一工具。',
        code: node.getText()
      });
    }
  });

  return violations;
}
