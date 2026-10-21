import { Node, SourceFile, SyntaxKind } from 'ts-morph';

export interface ThemeViolation {
  file: string;
  line: number;
  code: string;
  message: string;
}

/**
 * 匹配硬編碼的 Tailwind 顏色類別。
 * 排除語義化變數 (background, primary, secondary, muted, accent, destructive, popover, card, border, input, ring, sidebar)
 * 排除基礎原色 (white, black, transparent, inherit, current)
 */
const TAILWIND_HARDCODED_COLOR_REGEX = /\b(bg|text|border|ring|outline|from|to|via|decoration|fill|stroke)-(slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-(\d+)\b/i;

const INLINE_STYLE_COLOR_REGEX = /(color|backgroundColor|borderColor|outlineColor|fill|stroke)\s*:\s*['"](#|rgb|hsl|oklch)/i;

/**
 * 檢查單個檔案的主題主權違規項。
 */
export function checkThemeViolations(sourceFile: SourceFile): ThemeViolation[] {
  const violations: ThemeViolation[] = [];
  const filePath = sourceFile.getFilePath();

  // 1. 檢查所有字串字面量 (通常包含 className)
  sourceFile.getDescendantsOfKind(SyntaxKind.StringLiteral).forEach(node => {
    const text = node.getLiteralText();
    
    if (TAILWIND_HARDCODED_COLOR_REGEX.test(text)) {
      violations.push({
        file: filePath,
        line: node.getStartLineNumber(),
        code: text,
        message: '🚫 [Theme SSOT] 發現硬編碼 Tailwind 顏色。請使用語義化類別 (如 bg-background, text-primary)。',
      });
    }
  });

  // 2. 檢查模板字面量
  sourceFile.getDescendantsOfKind(SyntaxKind.NoSubstitutionTemplateLiteral).forEach(node => {
      const text = node.getLiteralText();
      if (TAILWIND_HARDCODED_COLOR_REGEX.test(text)) {
        violations.push({
            file: filePath,
            line: node.getStartLineNumber(),
            code: text,
            message: '🚫 [Theme SSOT] 發現硬編碼 Tailwind 顏色。',
        });
      }
  });

  // 3. 檢查 JSX 屬性中的內聯樣式 (JsxAttribute)
  sourceFile.getDescendantsOfKind(SyntaxKind.JsxAttribute).forEach(attr => {
      const name = attr.getNameNode().getText();
      if (name === 'style') {
          const initializer = attr.getInitializer();
          if (initializer && Node.isJsxExpression(initializer)) {
              const expression = initializer.getExpression();
              if (expression && expression.getKind() === SyntaxKind.ObjectLiteralExpression) {
                  const styleText = expression.getText();
                  if (INLINE_STYLE_COLOR_REGEX.test(styleText)) {
                      violations.push({
                          file: filePath,
                          line: attr.getStartLineNumber(),
                          code: styleText,
                          message: '🚫 [Theme SSOT] 內聯樣式中發現硬編碼顏色。請遷移至 CSS 變數。',
                      });
                  }
              }
          }
      }
  });

  return violations;
}
