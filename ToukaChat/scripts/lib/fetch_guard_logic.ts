import { SourceFile, SyntaxKind } from 'ts-morph';

export interface Violation {
  file: string;
  line: number;
  message: string;
  code: string;
}

export interface FetchGuardConfig {
  /** 被禁止的全域 HTTP 呼叫函式名稱（精確匹配，不含衍生名稱） */
  bannedCallers: string[];
  /** noqa 豁免規則 ID（行尾 `// noqa: <ID>` 的 ID 部分） */
  noqaRuleId: string;
  /** 排除掃描的檔案模式（glob fragment，比對 filePath） */
  excludePatterns: string[];
  /** 要掃描的路徑前綴（filePath 須包含其一） */
  scanPaths: string[];
}

export const DEFAULT_FETCH_GUARD_CONFIG: FetchGuardConfig = {
  bannedCallers: ['fetch'],
  noqaRuleId: 'FETCH_SSOT',
  excludePatterns: ['.test.ts', '.test.tsx', '.spec.ts', '.spec.tsx'],
  scanPaths: ['src/hooks/', 'src/lib/', 'src/app/', 'src/contexts/'],
};

/**
 * 判斷該行是否附有合法的 noqa 豁免標記。
 * 支援格式：`// noqa: RULE_ID`（規則 ID 大小寫敏感）
 */
function hasNoqaSuppression(lineText: string, ruleId: string): boolean {
  const pattern = new RegExp(`//\\s*noqa:\\s*${ruleId}(?:\\s|$)`);
  return pattern.test(lineText);
}

/**
 * 判斷 CallExpression 的被呼叫名稱是否為「精確的全域 fetch」，
 * 排除 fetchTouka、prefetch 等含 fetch 子字串的衍生命名。
 */
function isBannedCaller(calleeName: string, bannedCallers: string[]): boolean {
  return bannedCallers.includes(calleeName);
}

/**
 * 檢查單一 SourceFile 是否存在 fetch() SSOT 違規。
 * 純函式：給定 SourceFile 與設定，回傳違規清單。
 */
export function checkFetchViolations(
  sourceFile: SourceFile,
  config: FetchGuardConfig,
): Violation[] {
  const filePath = sourceFile.getFilePath();
  const violations: Violation[] = [];

  // 1. 排除測試與 spec 檔案
  const isExcluded = config.excludePatterns.some((pattern) =>
    filePath.includes(pattern),
  );
  if (isExcluded) {
    return [];
  }

  // 2. 僅掃描設定的路徑前綴（使用正斜線標準化比對）
  const normalizedPath = filePath.replace(/\\/g, '/');
  const isInScanPath = config.scanPaths.some((scanPath) =>
    normalizedPath.includes(scanPath),
  );
  if (!isInScanPath) {
    return [];
  }

  const sourceText = sourceFile.getFullText();
  const lines = sourceText.split('\n');

  // 3. AST 掃描 CallExpression
  const callExpressions = sourceFile.getDescendantsOfKind(
    SyntaxKind.CallExpression,
  );

  for (const node of callExpressions) {
    const expression = node.getExpression();
    const calleeName = expression.getText().trim();

    if (!isBannedCaller(calleeName, config.bannedCallers)) {
      continue;
    }

    // 4. 檢查行級 noqa 豁免
    const lineNumber = node.getStartLineNumber(); // 1-based
    const lineText = lines[lineNumber - 1] ?? '';

    if (hasNoqaSuppression(lineText, config.noqaRuleId)) {
      continue;
    }

    violations.push({
      file: filePath,
      line: lineNumber,
      message: `禁止直接調用 \`${calleeName}()\`。請改用 \`fetchTouka()\`（src/lib/connections.ts）以確保 Auth Header 注入、Cookie 透傳與 LDH 自動領卡。如為核心定義或架構特殊案例，請加上 // noqa: ${config.noqaRuleId}。`,
      code: node.getText(),
    });
  }

  return violations;
}
