import fs from 'fs';
import os from 'os';
import path from 'path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { Project } from 'ts-morph';
import {
  collectContractAliasNames,
  collectContractTypeKeys,
  compareContractBaseline,
  findHandwrittenContractTypes,
  isProductionSource,
  readContractBaseline,
  writeContractBaseline,
} from '../contract_type_guard_logic';

const CONTRACT_NAMES = new Set(['BackupInfo', 'McpHubServer', 'SessionSnapshot', 'SoulUpdateResult']);

function sourceFile(text: string, filePath = 'src/hooks/sample.ts') {
  const project = new Project({ useInMemoryFileSystem: true });
  return project.createSourceFile(filePath, text);
}

function handwritten(text: string): string[] {
  return findHandwrittenContractTypes(sourceFile(text), CONTRACT_NAMES);
}

describe('findHandwrittenContractTypes', () => {
  it.each([
    ['介面', 'export interface BackupInfo { id: string }'],
    ['物件型別', 'type BackupInfo = { id: string };'],
    ['交集', "type BackupInfo = components['schemas']['A'] & { id: string };"],
    ['Omit', "type BackupInfo = Omit<components['schemas']['A'], 'b'>;"],
    ['參考另一個型別', 'type BackupInfo = OtherType;'],
    ['根不是生成合約', "type BackupInfo = local['schemas']['A'];"],
    ['聯集裡有手寫', "type BackupInfo = components['schemas']['A'] | { id: string };"],
    ['泛型別名', "type BackupInfo<T> = components['schemas']['A'] | T;"],
    ['泛型別名，右側只有索引', "type BackupInfo<T> = components['schemas']['A'];"],
    ['函式裡的宣告', 'function f() { interface BackupInfo { id: string } return null; }'],
  ])('手寫同名會報：%s', (_label, text) => {
    expect(handwritten(text)).toEqual(['BackupInfo']);
  });

  it.each([
    ['單一索引', "export type BackupInfo = components['schemas']['BackupInfo'];"],
    ['多層索引', "type SoulUpdateResult = operations['x']['responses'][200]['content']['application/json'];"],
    ['paths 索引', "type BackupInfo = paths['/v1/x']['get'];"],
    ['全部是合約的聯集', "type SessionSnapshot = components['schemas']['A'] | components['schemas']['B'];"],
    ['括號包住', "type BackupInfo = (components['schemas']['A']);"],
    ['不是合約名', 'interface LocalOnly { id: string }'],
  ])('放行：%s', (_label, text) => {
    expect(handwritten(text)).toEqual([]);
  });

  it('同一個檔裡重複宣告就報兩次', () => {
    expect(handwritten('interface BackupInfo { a: string }\ninterface BackupInfo { b: string }')).toEqual([
      'BackupInfo',
      'BackupInfo',
    ]);
  });
});

describe('collectContractAliasNames', () => {
  it('只收真別名，手寫的同檔匯出不算', () => {
    const types = sourceFile(
      [
        "export type McpHubServer = components['schemas']['MCPHubServerItem'];",
        "export type SessionSnapshot = components['schemas']['A'] | components['schemas']['B'];",
        "export type SoulUpdateResult = operations['x']['responses'][200];",
        'export interface CustomModel { id: string }',
        'export type Handwritten = { id: string };',
        "export type Picked = Pick<components['schemas']['A'], 'b'>;",
      ].join('\n'),
      'src/lib/types.ts',
    );
    expect(collectContractAliasNames(types).sort()).toEqual([
      'McpHubServer',
      'SessionSnapshot',
      'SoulUpdateResult',
    ]);
  });

  it('在別處手寫 lib/types 的別名會報，即使它不是 schema 名', () => {
    const types = sourceFile(
      "export type McpHubServer = components['schemas']['MCPHubServerItem'];",
      'src/lib/types.ts',
    );
    const names = new Set(collectContractAliasNames(types));
    expect(findHandwrittenContractTypes(sourceFile('interface McpHubServer { id: string }'), names)).toEqual([
      'McpHubServer',
    ]);
  });
});

describe('isProductionSource', () => {
  it.each([
    ['src/lib/types.ts', true],
    ['src/components/chat/Foo.tsx', true],
    ['src/types/api.ts', false],
    ['src/generated/artifactRuntime.ts', false],
    ['src/hooks/useFoo.test.ts', false],
    ['src/components/Foo.spec.tsx', false],
    ['src/components/__tests__/helper.ts', false],
    ['src/components/ui/button.stories.tsx', false],
    ['src/test/setup.ts', false],
  ])('%s → %s', (relativePath, expected) => {
    expect(isProductionSource(relativePath)).toBe(expected);
  });
});

describe('整個前端的掃描與基準', () => {
  let root: string;
  const schemaPath = () => path.join(root, 'openapi.json');

  function write(relativePath: string, text: string) {
    const file = path.join(root, relativePath);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, text, 'utf-8');
  }

  beforeEach(() => {
    root = fs.mkdtempSync(path.join(os.tmpdir(), 'touka-contract-guard-'));
    write('openapi.json', JSON.stringify({ components: { schemas: { BackupInfo: {}, SoulConfig: {} } } }));
    write('src/lib/types.ts', "export type McpHubServer = components['schemas']['MCPHubServerItem'];\n");
    write('src/hooks/zeta.ts', 'export interface SoulConfig { id: string }\n');
    write('src/components/alpha.tsx', 'interface BackupInfo { id: string }\ninterface McpHubServer { id: string }\n');
    write('src/hooks/useFoo.test.ts', 'interface BackupInfo { id: string }\n');
    write('src/types/api.ts', 'export interface BackupInfo { id: string }\n');
    write('src/generated/x.ts', 'export interface BackupInfo { id: string }\n');
    write('src/hooks/fine.ts', "export type BackupInfo = components['schemas']['BackupInfo'];\n");
  });

  afterEach(() => {
    fs.rmSync(root, { recursive: true, force: true });
  });

  it('只收生產程式，鍵是排序好的相對路徑加名稱', async () => {
    expect(await collectContractTypeKeys(root, schemaPath())).toEqual([
      'src/components/alpha.tsx:BackupInfo',
      'src/components/alpha.tsx:McpHubServer',
      'src/hooks/zeta.ts:SoulConfig',
    ]);
  });

  it('沒有基準時全部算新增；重拍之後才相等', async () => {
    const current = await collectContractTypeKeys(root, schemaPath());
    expect(readContractBaseline(root)).toEqual([]);
    expect(compareContractBaseline(current, readContractBaseline(root))).toHaveLength(3);

    fs.mkdirSync(path.join(root, 'scripts'));
    writeContractBaseline(root, current);
    expect(compareContractBaseline(current, readContractBaseline(root))).toEqual([]);
  });

  it('guard_frontend 真的跑這道，違規會影響結束碼', () => {
    const guard = fs.readFileSync(path.resolve(__dirname, '../../guard_frontend.ts'), 'utf-8');
    expect(guard).toContain('await checkContractTypes(projectRoot, schemaPath');
    expect(guard).toContain('violations === 0 && contractViolations === 0');
  });
});

describe('compareContractBaseline', () => {
  const baseline = ['src/a.ts:BackupInfo', 'src/b.ts:SoulConfig'];

  it('完全相等才通過，順序不算', () => {
    expect(compareContractBaseline([...baseline].reverse(), baseline)).toEqual([]);
  });

  it('新增的手寫同名會紅', () => {
    const problems = compareContractBaseline([...baseline, 'src/c.ts:McpHubServer'], baseline);
    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain('src/c.ts:McpHubServer');
  });

  it('基準內的名字搬到別的檔會紅', () => {
    const problems = compareContractBaseline(['src/moved.ts:BackupInfo', 'src/b.ts:SoulConfig'], baseline);
    expect(problems).toHaveLength(2);
    expect(problems.join('\n')).toContain('src/moved.ts:BackupInfo');
    expect(problems.join('\n')).toContain('src/a.ts:BackupInfo');
  });

  it('修好但沒有重拍基準會紅', () => {
    const problems = compareContractBaseline(['src/b.ts:SoulConfig'], baseline);
    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain('src/a.ts:BackupInfo');
  });

  it('同一個鍵多出一份會紅', () => {
    const problems = compareContractBaseline([...baseline, baseline[0]], baseline);
    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain('src/a.ts:BackupInfo');
  });
});
