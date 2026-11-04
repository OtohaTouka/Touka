import { Node, SourceFile, SyntaxKind, TypeNode } from 'ts-morph';

/** 生成合約 `src/types/api.ts` 匯出的三個根型別。 */
const CONTRACT_ROOTS = new Set(['components', 'operations', 'paths']);

const NON_PRODUCTION_PATTERNS = [
  /\.(test|spec|stories)\.tsx?$/,
  /(^|\/)__tests__\//,
  /^src\/test\//,
  /^src\/generated\//,
  /^src\/types\/api\.ts$/,
];

/** 掃描範圍：生產程式；不含測試與生成檔。路徑相對 ToukaChat，分隔符用 `/`。 */
export function isProductionSource(relativePath: string): boolean {
  return !NON_PRODUCTION_PATTERNS.some((pattern) => pattern.test(relativePath));
}

/**
 * 右側是不是直接引用生成合約：根為 components／operations／paths 的索引型別，
 * 或全部由這種型別組成的聯集。Omit、Pick、交集都會改變形狀，不算。
 */
function referencesContract(node: TypeNode): boolean {
  if (Node.isParenthesizedTypeNode(node)) return referencesContract(node.getTypeNode());
  if (Node.isUnionTypeNode(node)) return node.getTypeNodes().every(referencesContract);
  if (!Node.isIndexedAccessTypeNode(node)) return false;
  let root: TypeNode = node.getObjectTypeNode();
  while (Node.isIndexedAccessTypeNode(root)) root = root.getObjectTypeNode();
  return Node.isTypeReference(root) && CONTRACT_ROOTS.has(root.getTypeName().getText());
}

function isContractAlias(node: Node): boolean {
  if (!Node.isTypeAliasDeclaration(node) || node.getTypeParameters().length > 0) return false;
  const typeNode = node.getTypeNode();
  return typeNode !== undefined && referencesContract(typeNode);
}

/** 只收 `src/lib/types.ts` 裡真的指向生成合約的別名；同檔的手寫匯出不算合約名。 */
export function collectContractAliasNames(typesFile: SourceFile): string[] {
  return typesFile
    .getDescendantsOfKind(SyntaxKind.TypeAliasDeclaration)
    .filter(isContractAlias)
    .map((alias) => alias.getName());
}

/** 名字屬於合約、宣告卻不是合約別名的型別名稱；同一個檔重複宣告就列兩次。 */
export function findHandwrittenContractTypes(
  sourceFile: SourceFile,
  contractNames: ReadonlySet<string>,
): string[] {
  const declarations = [
    ...sourceFile.getDescendantsOfKind(SyntaxKind.InterfaceDeclaration),
    ...sourceFile.getDescendantsOfKind(SyntaxKind.TypeAliasDeclaration),
  ];
  return declarations
    .filter((node) => contractNames.has(node.getName()) && !isContractAlias(node))
    .map((node) => node.getName());
}

function countKeys(keys: readonly string[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const key of keys) counts.set(key, (counts.get(key) ?? 0) + 1);
  return counts;
}

/** 必須和基準完全相等（ADR-0278 §1）：新增要修，修好要在同一個 PR 重拍基準。 */
export function compareContractBaseline(current: readonly string[], baseline: readonly string[]): string[] {
  const now = countKeys(current);
  const allowed = countKeys(baseline);
  const problems: string[] = [];
  for (const key of [...new Set([...now.keys(), ...allowed.keys()])].sort()) {
    const nowCount = now.get(key) ?? 0;
    const allowedCount = allowed.get(key) ?? 0;
    if (nowCount > allowedCount) {
      problems.push(`[新增] ${key}：這個型別和後端合約同名卻是手寫，請改從 @/lib/types 或 @/types/api 引用`);
    } else if (nowCount < allowedCount) {
      problems.push(`[已不存在] ${key}：請重拍基準`);
    }
  }
  return problems;
}
