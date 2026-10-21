import { SourceFile, SyntaxKind } from 'ts-morph';

export interface Violation {
  file: string;
  line: number;
  message: string;
  code: string;
}

export interface LocalStorageGuardConfig {
  /** noqa 豁免規則 ID（行尾 `// noqa: <ID>` 的 ID 部分） */
  noqaRuleId: string;
  /** 排除掃描的檔案模式（比對 filePath 子字串） */
  excludePatterns: string[];
  /** 整個檔案豁免的白名單（basename 完全匹配） */
  whitelistFiles: string[];
  /** 要掃描的路徑前綴（filePath 須包含其一） */
  scanPaths: string[];
}

export const DEFAULT_LOCALSTORAGE_GUARD_CONFIG: LocalStorageGuardConfig = {
  noqaRuleId: 'LOCALSTORAGE_SSOT',
  excludePatterns: ['.test.ts', '.test.tsx', '.spec.ts', '.spec.tsx'],
  // SSOT 定義層：useLocalStorage.ts 是通用 Hook 本體（唯一合法直接操作 localStorage 的地方）
  // H1 fix (2026-05-24)：connections.ts 已移除直接 localStorage 存取，改用 getLocalStorageItem()
  whitelistFiles: ['useLocalStorage.ts'],
  scanPaths: ['src/hooks/', 'src/lib/', 'src/app/', 'src/contexts/'],
};

/**
 * 判斷 localStorage 調用行是否有合法 noqa 豁免標記。
 * 支援格式：`// noqa: LOCALSTORAGE_SSOT`
 */
function hasNoqaSuppression(lineText: string, ruleId: string): boolean {
  const pattern = new RegExp(`//\\s*noqa:\\s*${ruleId}(?:\\s|$)`);
  return pattern.test(lineText);
}

/**
 * 檢查單一 SourceFile 是否存在直接 localStorage 存取違規。
 * 純函式：給定 SourceFile、設定與行級豁免映射，回傳違規清單。
 *
 * @param ignoredLines 行號 → 豁免規則 ID 集合（供測試直接注入，生產由 parse_noqa_comments 生成）
 */
export function checkLocalStorageViolations(
  sourceFile: SourceFile,
  config: LocalStorageGuardConfig,
  ignoredLines: Record<number, Set<string>> = {},
): Violation[] {
  const filePath = sourceFile.getFilePath();
  const normalizedPath = filePath.replace(/\\/g, '/');

  // 1. 排除測試與 spec 檔案
  if (config.excludePatterns.some((p) => filePath.includes(p))) return [];

  // 2. 僅掃描 scanPaths 內的路徑
  if (!config.scanPaths.some((p) => normalizedPath.includes(p))) return [];

  // 3. 檔案級白名單（basename 完全匹配）
  const basename = normalizedPath.split('/').pop() ?? '';
  if (config.whitelistFiles.includes(basename)) return [];

  const violations: Violation[] = [];
  const sourceText = sourceFile.getFullText();
  const lines = sourceText.split('\n');

  // 4. AST 掃描：找所有含 `localStorage.` 的 CallExpression
  //    涵蓋 `localStorage.getItem(...)` 與 `window.localStorage.getItem(...)` 兩種形式
  const callExpressions = sourceFile.getDescendantsOfKind(SyntaxKind.CallExpression);

  for (const node of callExpressions) {
    const expressionText = node.getExpression().getText();

    // `localStorage.` 子字串匹配，不誤報 `someOtherLocalStorage.*`
    if (!expressionText.includes('localStorage.')) continue;

    const lineNumber = node.getStartLineNumber(); // 1-based
    const lineText = lines[lineNumber - 1] ?? '';

    // 5. 行級 noqa 豁免（inline comment 方式）
    if (hasNoqaSuppression(lineText, config.noqaRuleId)) continue;

    // 6. 行級 noqa 豁免（直接注入方式，供測試使用）
    if (ignoredLines[lineNumber]?.has(config.noqaRuleId)) continue;

    violations.push({
      file: filePath,
      line: lineNumber,
      message: `禁止直接調用 \`${expressionText}\`。請改用 \`useLocalStorage\` hook 或領域專屬的 Custom Hook（如 \`useConnectionSettings\`）。如為核心定義或架構特殊案例，請加上 // noqa: ${config.noqaRuleId}。`,
      code: node.getText(),
    });
  }

  return violations;
}
