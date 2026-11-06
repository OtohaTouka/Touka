// @vitest-environment node
import path from 'path';
import { ESLint } from 'eslint';
import { beforeAll, describe, expect, it } from 'vitest';

const LOAD_TIMEOUT_MS = 120_000;
const RESTRICTED_IMPORTS = 'no-restricted-imports';
const RESTRICTED_SYNTAX = 'no-restricted-syntax';
const ERROR = 2;
const WARN = 1;

describe('ESLint 的元件與匯入規則（禁令 50、51；ADR-0298）', () => {
  let eslint: ESLint;

  beforeAll(() => {
    eslint = new ESLint({ cwd: path.resolve(__dirname, '../../..') });
  });

  async function severityOf(rule: string, code: string, filePath: string): Promise<number> {
    const [result] = await eslint.lintText(code, { filePath });
    return Math.max(0, ...result.messages.filter((m) => m.ruleId === rule).map((m) => m.severity));
  }

  it.each([
    ['import { Dialog } from "radix-ui";'],
    ['import { Dialog } from "radix-ui/internal";'],
    ['import * as Dialog from "@radix-ui/react-dialog";'],
  ])('封裝層外不得直接引用：%s', async (code) => {
    const severity = await severityOf(RESTRICTED_IMPORTS, code, 'src/components/Foo.tsx');
    expect(severity).toBe(ERROR);
  }, LOAD_TIMEOUT_MS);

  it('名字含 radix-ui 的專案內路徑不算', async () => {
    const code = 'import { Dialog } from "@/components/radix-ui";';
    const severity = await severityOf(RESTRICTED_IMPORTS, code, 'src/components/Foo.tsx');
    expect(severity).toBe(0);
  }, LOAD_TIMEOUT_MS);

  it.each([
    ['import { Dialog } from "radix-ui";'],
    ['import * as Dialog from "@radix-ui/react-dialog";'],
  ])('封裝層本身可以引用：%s', async (code) => {
    const severity = await severityOf(RESTRICTED_IMPORTS, code, 'src/components/ui/dialog.tsx');
    expect(severity).toBe(0);
  }, LOAD_TIMEOUT_MS);

  it('深層相對匯入的警告沒有被後面的區塊蓋掉', async () => {
    const code = 'import { x } from "../../lib/x";\n';
    const severity = await severityOf(RESTRICTED_SYNTAX, code, 'src/components/chat/Foo.tsx');
    expect(severity).toBe(WARN);
  }, LOAD_TIMEOUT_MS);
});
