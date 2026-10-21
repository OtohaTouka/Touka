import { SourceFile, SyntaxKind, VariableDeclaration } from 'ts-morph';

export interface Violation {
  file: string;
  line: number;
  message: string;
  code: string;
}

export interface EventSourceGuardConfig {
  /** noqa 豁免規則 ID */
  noqaRuleId: string;
  /** 排除掃描的檔案模式 */
  excludePatterns: string[];
  /** 要掃描的路徑前綴 */
  scanPaths: string[];
}

export const DEFAULT_EVENTSOURCE_GUARD_CONFIG: EventSourceGuardConfig = {
  noqaRuleId: 'EVENTSOURCE_SSOT',
  excludePatterns: ['.test.ts', '.test.tsx', '.spec.ts', '.spec.tsx'],
  scanPaths: ['src/hooks/', 'src/lib/', 'src/app/', 'src/contexts/'],
};

function hasNoqaSuppression(lineText: string, ruleId: string): boolean {
  const pattern = new RegExp(`//\\s*noqa:\\s*${ruleId}(?:\\s|$)`);
  return pattern.test(lineText);
}

/**
 * 檢查單一 SourceFile 是否存在 EventSource 不帶 Token 的違規。
 */
export function checkEventSourceViolations(
  sourceFile: SourceFile,
  config: EventSourceGuardConfig,
  ignoredLines: Record<number, Set<string>> = {},
): Violation[] {
  const filePath = sourceFile.getFilePath();
  const normalizedPath = filePath.replace(/\\/g, '/');

  // 1. 排除測試與 spec 檔案
  if (config.excludePatterns.some((p) => filePath.includes(p))) return [];

  // 2. 僅掃描 scanPaths 內的路徑
  if (!config.scanPaths.some((p) => normalizedPath.includes(p))) return [];

  const violations: Violation[] = [];
  const sourceText = sourceFile.getFullText();
  const lines = sourceText.split('\n');

  // 3. AST 掃描 NewExpression
  const newExpressions = sourceFile.getDescendantsOfKind(SyntaxKind.NewExpression);

  for (const node of newExpressions) {
    const expressionText = node.getExpression().getText().trim();
    if (expressionText !== 'EventSource') continue;

    const lineNumber = node.getStartLineNumber(); // 1-based
    const lineText = lines[lineNumber - 1] ?? '';

    // 4. noqa 豁免
    if (hasNoqaSuppression(lineText, config.noqaRuleId)) continue;
    if (ignoredLines[lineNumber]?.has(config.noqaRuleId)) continue;

    // 方案 3 改造：全面禁用原生 EventSource 以防止 Token 洩漏至 URL 與 Access Logs
    violations.push({
      file: filePath,
      line: lineNumber,
      message: `禁止直接使用原生 EventSource 串流，這會導致安全 Token 洩漏於 URL 與 Access Logs 中。請改用基於 Header 驗證的 fetch + ReadableStream 串流。如為特殊非敏感案例，請加上 // noqa: ${config.noqaRuleId}。`,
      code: node.getText(),
    });
  }

  return violations;
}
