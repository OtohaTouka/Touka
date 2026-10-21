import { SourceFile, SyntaxKind } from 'ts-morph';

export interface DesignViolation {
  file: string;
  line: number;
  code: string;
  message: string;
}

const COLOR_REGEX = /#([A-Fa-f0-9]{3}){1,2}\b|rgba?\s*\([^)]+\)|hsla?\s*\([^)]+\)/i;
const ARBITRARY_VALUE_REGEX = /(?<!data|aria|supports|z|group|peer|has|variant)-\[(?!(inherit|auto|1fr|max-content|min-content|var|--|\.|color|box-shadow|transform|opacity|background|border|[0-9]+(vh|vw|%))).+?\]/i;

/**
 * 檢查單個檔案的設計主權違規項。
 */
export function checkDesignViolations(sourceFile: SourceFile): DesignViolation[] {
  const violations: DesignViolation[] = [];
  const filePath = sourceFile.getFilePath();

  // 1. 檢查所有字串字面量 (String Literals)
  sourceFile.getDescendantsOfKind(SyntaxKind.StringLiteral).forEach(node => {
    const text = node.getLiteralText();
    
    // 檢查硬編碼顏色
    if (COLOR_REGEX.test(text)) {
      // 排除特定的合規字串 (例如 "transparent", "inherit", 或來自 tokens 的變數名稱)
      // 但如果是具體的色碼，則報錯
      violations.push({
        file: filePath,
        line: node.getStartLineNumber(),
        code: text,
        message: '🚫 [Design Token SSOT] 發現硬編碼顏色。請使用 Design Tokens 或 CSS 變數。',
      });
    }

    // 檢查 Tailwind 任意值
    if (ARBITRARY_VALUE_REGEX.test(text)) {
      violations.push({
        file: filePath,
        line: node.getStartLineNumber(),
        code: text,
        message: '🚫 [Design Token SSOT] 發現 Tailwind 任意值。請使用 tokens.ts 中定義的標準值。',
      });
    }
  });

  // 2. 檢查 Template Literals (反引號字串)
  sourceFile.getDescendantsOfKind(SyntaxKind.NoSubstitutionTemplateLiteral).forEach(node => {
      const text = node.getLiteralText();
      if (COLOR_REGEX.test(text)) {
        violations.push({
            file: filePath,
            line: node.getStartLineNumber(),
            code: text,
            message: '🚫 [Design Token SSOT] 發現硬編碼顏色。',
        });
      }
      if (ARBITRARY_VALUE_REGEX.test(text)) {
          violations.push({
              file: filePath,
              line: node.getStartLineNumber(),
              code: text,
              message: '🚫 [Design Token SSOT] 發現 Tailwind 任意值。',
          });
      }
  });

  return violations;
}

/**
 * 針對 CSS 檔案的簡單 Regex 檢查 (因為 ts-morph 不支援 CSS)
 */
export function checkCssViolations(content: string, filePath: string): DesignViolation[] {
    const violations: DesignViolation[] = [];
    const lines = content.split('\n');

    lines.forEach((line, index) => {
        // 排除註解行
        if (line.trim().startsWith('/*') || line.trim().startsWith('*')) return;

        if (COLOR_REGEX.test(line)) {
            // 允許 CSS 變數賦值 (通常在 :root 或 @theme 中)
            // 如果這行包含 "--" 且是賦值，則跳過 (這是同步腳本的工作)
            if (line.includes('--') && line.includes(':')) return;

            violations.push({
                file: filePath,
                line: index + 1,
                code: line.trim(),
                message: '🚫 [Design Token SSOT] CSS 中發現硬編碼顏色。請遷移至 tokens.ts。',
            });
        }
    });

    return violations;
}
