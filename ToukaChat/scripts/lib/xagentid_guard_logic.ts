import { SourceFile, SyntaxKind } from 'ts-morph';

export interface Violation {
  file: string;
  line: number;
  message: string;
  code: string;
}

export interface XAgentIdGuardConfig {
  /** noqa 豁免規則 ID */
  noqaRuleId: string;
  /** 排除掃描的檔案模式 */
  excludePatterns: string[];
  /** 要掃描的路徑前綴 */
  scanPaths: string[];
}

export const DEFAULT_XAGENTID_GUARD_CONFIG: XAgentIdGuardConfig = {
  noqaRuleId: 'IDENTITY_SSOT',
  excludePatterns: ['.test.ts', '.test.tsx', '.spec.ts', '.spec.tsx'],
  scanPaths: ['src/hooks/', 'src/lib/', 'src/app/', 'src/contexts/', 'src/components/'],
};

function hasNoqaSuppression(lineText: string, ruleId: string): boolean {
  const pattern = new RegExp(`//\\s*noqa:\\s*${ruleId}(?:\\s|$)`);
  return pattern.test(lineText);
}

/**
 * 檢查單一 SourceFile 是否存在 X-Agent-ID 身份主權違規。
 */
export function checkXAgentIdViolations(
  sourceFile: SourceFile,
  config: XAgentIdGuardConfig,
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

  // 3. 掃描 PropertyAssignment（如 `{ "X-Agent-ID": "DebugAgent" }`）
  const propertyAssignments = sourceFile.getDescendantsOfKind(SyntaxKind.PropertyAssignment);
  for (const node of propertyAssignments) {
    const nameNode = node.getNameNode();
    const nameText = nameNode.getText().replace(/['"`]/g, '').trim();

    if (nameText.toLowerCase() === 'x-agent-id') {
      const lineNumber = node.getStartLineNumber();
      const lineText = lines[lineNumber - 1] ?? '';

      if (hasNoqaSuppression(lineText, config.noqaRuleId)) continue;
      if (ignoredLines[lineNumber]?.has(config.noqaRuleId)) continue;

      violations.push({
        file: filePath,
        line: lineNumber,
        message: `禁止手動注入 \`X-Agent-ID\` Header。身份必須由 Bearer Token 攜帶以維持身份主權。如為特殊案例，請加上 // noqa: ${config.noqaRuleId}。`,
        code: node.getText(),
      });
    }
  }

  // 4. 掃描 ElementAccessExpression（如 `headers["X-Agent-ID"] = ...`）
  const elementAccesses = sourceFile.getDescendantsOfKind(SyntaxKind.ElementAccessExpression);
  for (const node of elementAccesses) {
    const argumentExpression = node.getArgumentExpression();
    if (argumentExpression) {
      const text = argumentExpression.getText().replace(/['"`]/g, '').trim();

      if (text.toLowerCase() === 'x-agent-id') {
        const lineNumber = node.getStartLineNumber();
        const lineText = lines[lineNumber - 1] ?? '';

        if (hasNoqaSuppression(lineText, config.noqaRuleId)) continue;
        if (ignoredLines[lineNumber]?.has(config.noqaRuleId)) continue;

        violations.push({
          file: filePath,
          line: lineNumber,
          message: `禁止手動注入 \`X-Agent-ID\` Header。身份必須由 Bearer Token 攜帶以維持身份主權。如為特殊案例，請加上 // noqa: ${config.noqaRuleId}。`,
          code: node.getText(),
        });
      }
    }
  }

  return violations;
}
