import { Project, SyntaxKind } from 'ts-morph';
import path from 'path';
import glob from 'fast-glob';

export interface EnvViolation {
  file: string;
  line: number;
  variable: string;
  content: string;
}

export class EnvGuardScanner {
  constructor(private projectRoot: string) {}

  async scan(): Promise<EnvViolation[]> {
    const project = new Project();
    const files = await glob('src/**/*.{ts,tsx}', { cwd: this.projectRoot, absolute: true });
    const violations: EnvViolation[] = [];

    const EXEMPT_FILE = path.normalize('src/lib/config.ts');

    for (const file of files) {
      const relativePath = path.relative(this.projectRoot, file);
      if (path.normalize(relativePath) === EXEMPT_FILE) {
        continue;
      }

      const sourceFile = project.addSourceFileAtPath(file);
      const propertyAccessExpressions = sourceFile.getDescendantsOfKind(SyntaxKind.PropertyAccessExpression);

      for (const expr of propertyAccessExpressions) {
        const text = expr.getText();
        
        // 檢查是否為 process.env.XXX 或 import.meta.env.XXX
        if (text.startsWith('process.env.') || text.startsWith('import.meta.env.')) {
          const line = expr.getStartLineNumber();
          const lineText = sourceFile.getFullText().split('\n')[line - 1];

          // 支援 // noqa: FRONTEND_ENV_SSOT 或 # noqa: FRONTEND_ENV_SSOT
          if (lineText.includes('noqa: FRONTEND_ENV_SSOT')) {
            continue;
          }

          violations.push({
            file: relativePath,
            line,
            variable: text,
            content: lineText.trim()
          });
        }
      }
    }
    return violations;
  }
}
