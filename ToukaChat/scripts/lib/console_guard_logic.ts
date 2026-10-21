import { SourceFile, SyntaxKind } from 'ts-morph';

export interface Violation {
  file: string;
  line: number;
  message: string;
  code: string;
}

export interface ConsoleGuardConfig {
  /** noqa 豁免規則 ID（行尾 `// noqa: <ID>` 的 ID 部分） */
  noqaRuleId: string;
  /** 禁止的 console 方法名稱 */
  bannedMethods: string[];
  /** 排除掃描的檔案模式（比對 filePath 子字串） */
  excludePatterns: string[];
  /** 整個檔案豁免的白名單（basename 完全匹配） */
  whitelistFiles: string[];
  /** 要掃描的路徑前綴（filePath 須包含其一） */
  scanPaths: string[];
}

export const DEFAULT_CONSOLE_GUARD_CONFIG: ConsoleGuardConfig = {
  noqaRuleId: 'CONSOLE_BAN',
  bannedMethods: ['log', 'warn', 'error'],
  excludePatterns: [
    '.test.ts',
    '.test.tsx',
    '.spec.ts',
    '.spec.tsx',
    '.stories.ts',
    '.stories.tsx',
  ],
  whitelistFiles: [],
  scanPaths: ['src/hooks/', 'src/lib/', 'src/app/', 'src/contexts/'],
};

/**
 * 判斷 console 調用行是否有合法 noqa 豁免標記。
 * 支援格式：`// noqa: CONSOLE_BAN`
 */
function hasNoqaSuppression(lineText: string, ruleId: string): boolean {
  const pattern = new RegExp(`//\\s*noqa:\\s*${ruleId}(?:\\s|$)`);
  return pattern.test(lineText);
}

/**
 * 檢查單一 SourceFile 是否存在直接 console.log/warn/error 呼叫違規。
 * 純函式：給定 SourceFile、設定與行級豁免映射，回傳違規清單。
 *
 * @param ignoredLines 行號 → 豁免規則 ID 集合（供測試直接注入）
 */
export function checkConsoleViolations(
  sourceFile: SourceFile,
  config: ConsoleGuardConfig,
  ignoredLines: Record<number, Set<string>> = {},
): Violation[] {
  const filePath = sourceFile.getFilePath();
  const normalizedPath = filePath.replace(/\\/g, '/');

  // 1. 排除測試、spec、stories 檔案
  if (config.excludePatterns.some((p) => filePath.includes(p))) return [];

  // 2. 僅掃描 scanPaths 內的路徑
  if (!config.scanPaths.some((p) => normalizedPath.includes(p))) return [];

  // 3. 檔案級白名單（basename 完全匹配）
  const basename = normalizedPath.split('/').pop() ?? '';
  if (config.whitelistFiles.includes(basename)) return [];

  const violations: Violation[] = [];
  const sourceText = sourceFile.getFullText();
  const lines = sourceText.split('\n');

  // 4. AST 掃描：找所有 console.log/warn/error CallExpression
  const callExpressions = sourceFile.getDescendantsOfKind(SyntaxKind.CallExpression);

  for (const node of callExpressions) {
    const expressionText = node.getExpression().getText();

    // 匹配 console.log / console.warn / console.error（精確匹配，避免誤報）
    const match = /^console\.(\w+)$/.exec(expressionText);
    if (!match) continue;
    if (!config.bannedMethods.includes(match[1])) continue;

    const lineNumber = node.getStartLineNumber(); // 1-based
    const lineText = lines[lineNumber - 1] ?? '';

    // 5. 行級 noqa 豁免（inline comment 方式）
    if (hasNoqaSuppression(lineText, config.noqaRuleId)) continue;

    // 6. 行級 noqa 豁免（直接注入方式，供測試使用）
    if (ignoredLines[lineNumber]?.has(config.noqaRuleId)) continue;

    violations.push({
      file: filePath,
      line: lineNumber,
      message:
        `禁止直接呼叫 \`${expressionText}()\`。` +
        `請改用 \`logSSOTError()\` 或 ERRORS 常數封裝層（src/lib/error_handler.ts）。` +
        `如為架構必要案例，請加上 // noqa: ${config.noqaRuleId}。`,
      code: node.getText(),
    });
  }

  return violations;
}
