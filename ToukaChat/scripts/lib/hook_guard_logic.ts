import { SourceFile, SyntaxKind } from 'ts-morph';

export interface Violation {
  file: string;
  line: number;
  message: string;
  code: string;
}

export function checkHookViolations(sourceFile: SourceFile): Violation[] {
  const violations: Violation[] = [];
  const filePath = sourceFile.getFilePath();

  // 僅針對 src/components 下的檔案進行檢查
  if (!filePath.includes('/src/components/')) {
    return [];
  }

  // 1. 檢查 fetch 調用
  sourceFile.getDescendantsOfKind(SyntaxKind.CallExpression).forEach(node => {
    const expression = node.getExpression();
    const text = expression.getText();

    if (text === 'fetch') {
      violations.push({
        file: filePath,
        line: node.getStartLineNumber(),
        message: '禁止在組件中直接使用 fetch()。請使用領域專屬的 Custom Hooks 或 API 客戶端。',
        code: node.getText()
      });
    }

    // 2. 檢查 axios 調用
    if (text === 'axios' || text.startsWith('axios.')) {
      violations.push({
        file: filePath,
        line: node.getStartLineNumber(),
        message: '禁止在組件中直接使用 axios。請將 API 調用封裝至 Hooks 中。',
        code: node.getText()
      });
    }

    // 3. 檢查 localStorage/sessionStorage 調用
    const storageBanned = ['localStorage', 'sessionStorage'];
    if (SyntaxKind.PropertyAccessExpression === expression.getKind()) {
      const baseObject = expression.getFirstChild()?.getText();
      if (baseObject && storageBanned.includes(baseObject)) {
        violations.push({
          file: filePath,
          line: node.getStartLineNumber(),
          message: `禁止在組件中直接存取 ${baseObject}。請使用 useLocalStorage 等專屬 Hooks。`,
          code: node.getText()
        });
      }
    }
  });

  return violations;
}
