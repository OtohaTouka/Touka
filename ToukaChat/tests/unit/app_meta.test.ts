/**
 * HAX G18（通知系統變更）— 版本號 SSOT。
 * app_meta.APP_VERSION 的唯一事實來源必須是 package.json，前端不得另存第二份。
 * 版本號由 next.config 的 `env` 在建置時注入；前端只拿到這個字串，
 * 不把整份 package.json（scripts、相依、overrides）打包進前端。
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { APP_VERSION, APP_NAME } from '@/lib/app_meta';
// eslint-disable-next-line no-restricted-syntax -- Root build config has no equivalent @/src alias; verify the real Next selection.
import nextConfig from '../../next.config';

const pkg = JSON.parse(
  readFileSync(join(__dirname, '../../package.json'), 'utf-8'),
) as { version: string };

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

  it('前端程式不直接匯入 package.json', () => {
    const source = readFileSync(join(__dirname, '../../src/lib/app_meta.ts'), 'utf-8');
    expect(source).not.toMatch(/(?:from|import|require)\s*\(?\s*['"][^'"]*package\.json['"]/);
  });
});
