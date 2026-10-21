import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { EnvGuardScanner } from '../env_guard_logic';

describe('EnvGuardScanner', () => {
  let testRoot: string;

  beforeEach(() => {
    testRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'touka-env-guard-'));
    fs.mkdirSync(path.join(testRoot, 'src/lib'), { recursive: true });
    fs.mkdirSync(path.join(testRoot, 'src/components'), { recursive: true });
  });

  afterEach(() => {
    fs.rmSync(testRoot, { recursive: true, force: true });
  });

  it('keeps its scratch folder under the system temp folder', () => {
    const fromTmp = path.relative(os.tmpdir(), testRoot);
    expect(fromTmp.startsWith('..') || path.isAbsolute(fromTmp)).toBe(false);
  });

  it('should detect process.env usage in components', async () => {
    const componentFile = path.join(testRoot, 'src/components/BadComponent.tsx');
    fs.writeFileSync(componentFile, `
      export const BadComponent = () => {
        console.log(process.env.NEXT_PUBLIC_API_URL);
        return <div>Bad</div>;
      };
    `);

    const scanner = new EnvGuardScanner(testRoot);
    const violations = await scanner.scan();

    expect(violations.length).toBe(1);
    expect(violations[0].variable).toBe('process.env.NEXT_PUBLIC_API_URL');
  });

  it('should ignore process.env usage in src/lib/config.ts', async () => {
    const configFile = path.join(testRoot, 'src/lib/config.ts');
    fs.writeFileSync(configFile, `
      export const config = {
        API_URL: process.env.NEXT_PUBLIC_API_URL
      };
    `);

    const scanner = new EnvGuardScanner(testRoot);
    const violations = await scanner.scan();

    expect(violations.length).toBe(0);
  });

  it('should respect # noqa: FRONTEND_ENV_SSOT', async () => {
    const componentFile = path.join(testRoot, 'src/components/ExemptComponent.tsx');
    fs.writeFileSync(componentFile, `
      export const ExemptComponent = () => {
        console.log(process.env.NODE_ENV); // noqa: FRONTEND_ENV_SSOT
        return <div>Exempt</div>;
      };
    `);

    const scanner = new EnvGuardScanner(testRoot);
    const violations = await scanner.scan();

    expect(violations.length).toBe(0);
  });

  it('should detect import.meta.env usage', async () => {
    const componentFile = path.join(testRoot, 'src/components/ViteComponent.tsx');
    fs.writeFileSync(componentFile, `
      const url = import.meta.env.VITE_API_URL;
    `);

    const scanner = new EnvGuardScanner(testRoot);
    const violations = await scanner.scan();

    expect(violations.length).toBe(1);
    expect(violations[0].variable).toBe('import.meta.env.VITE_API_URL');
  });
});
