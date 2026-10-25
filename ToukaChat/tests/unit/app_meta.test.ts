/**
 * HAX G18（通知系統變更）— 版本號 SSOT。
 * app_meta.APP_VERSION 的唯一事實來源必須是 package.json，前端不得另存第二份。
 * 版本號由 next.config 的 `env` 在建置時注入；前端只拿到這個字串，
 * 不把整份 package.json（scripts、相依、overrides）打包進前端。
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join, relative } from 'node:path';
import { APP_VERSION, APP_NAME } from '@/lib/app_meta';
// eslint-disable-next-line no-restricted-syntax -- Root build config has no equivalent @/src alias; read the real env injection.
import nextConfig from '../../next.config';

const chatRoot = join(__dirname, '../..');
const pkg = JSON.parse(readFileSync(join(chatRoot, 'package.json'), 'utf-8')) as {
  version: string;
};

const SOURCE_FILE = /\.(?:ts|tsx|js|jsx|mjs|mts)$/;
const TEST_FILE = /\.(?:test|spec)\.[^.]+$/;
const PACKAGE_JSON_IMPORT = /(?:from|import|require)\s*\(?\s*['"][^'"]*package\.json['"]/;

/** 前端產品原始碼（不含測試與測試設定），也就是會被打包進前端的檔案。 */
function frontendSources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      return entry.name === '__tests__' || path === join(chatRoot, 'src', 'test')
        ? []
        : frontendSources(path);
    }
    return SOURCE_FILE.test(entry.name) && !TEST_FILE.test(entry.name) ? [path] : [];
  });
}

describe('app_meta（HAX G18 版本 SSOT）', () => {
  it('APP_VERSION 與 package.json.version 完全一致', () => {
    expect(APP_VERSION).toBe(pkg.version);
  });

  it('APP_VERSION 為合法的語意化版本格式', () => {
    expect(APP_VERSION).toMatch(/^\d+\.\d+\.\d+/);
  });

  it('APP_NAME 為非空字串', () => {
    expect(typeof APP_NAME).toBe('string');
    expect(APP_NAME.length).toBeGreaterThan(0);
  });

  it('next.config 只把版本號注入前端', () => {
    expect(nextConfig.env).toEqual({ NEXT_PUBLIC_APP_VERSION: pkg.version });
  });

  // vitest 在執行時給 env，Next 是在建置時替換字面的 process.env.NEXT_PUBLIC_APP_VERSION；
  // 解構之類的寫法 vitest 照樣讀得到，建置後卻會變成空字串，所以另外釘住原文寫法。
  it('config.ts 以建置時能被替換的寫法讀取版本號', () => {
    const config = readFileSync(join(chatRoot, 'src', 'lib', 'config.ts'), 'utf-8');
    expect(config).toContain('process.env.NEXT_PUBLIC_APP_VERSION');
  });

  it('前端程式都不直接匯入 package.json', () => {
    const offenders = frontendSources(join(chatRoot, 'src'))
      .filter((path) => PACKAGE_JSON_IMPORT.test(readFileSync(path, 'utf-8')))
      .map((path) => relative(chatRoot, path));
    expect(offenders).toEqual([]);
  });
});
