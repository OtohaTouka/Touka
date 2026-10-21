import { describe, it, expect } from 'vitest';
import { Project } from 'ts-morph';
import { checkTimeViolations } from '../../scripts/lib/time_guard_logic';

describe('Time SSOT Guard Logic', () => {
  const project = new Project({ useInMemoryFileSystem: true });

  it('應捕捉直接使用 new Date() 的行為', () => {
    const sourceFile = project.createSourceFile('test.tsx', `
      const now = new Date();
      const formatted = now.toLocaleTimeString();
    `);
    const violations = checkTimeViolations(sourceFile);
    expect(violations.length).toBeGreaterThan(0);
    expect(violations[0].message).toContain('new Date()');
  });

  it('應捕捉 .toLocaleDateString() 的行為', () => {
    const sourceFile = project.createSourceFile('test2.tsx', `
      const d = someDate.toLocaleDateString();
    `);
    const violations = checkTimeViolations(sourceFile);
    expect(violations.length).toBe(1);
    expect(violations[0].message).toContain('toLocaleDateString');
  });

  it('應允許在 time_utils.ts 中使用', () => {
    const sourceFile = project.createSourceFile('src/lib/time_utils.ts', `
      export function getNow() { return new Date(); }
    `);
    const violations = checkTimeViolations(sourceFile);
    expect(violations.length).toBe(0);
  });

  it('應允許帶有參數的 new Date(session.updatedAt) 但攔截其後的格式化', () => {
    // 雖然 new Date(param) 是合法的（用於轉換），但 .toLocaleDateString() 不合法
    const sourceFile = project.createSourceFile('test3.tsx', `
      const d = new Date(123456).toLocaleDateString();
    `);
    const violations = checkTimeViolations(sourceFile);
    expect(violations.length).toBe(1);
    expect(violations[0].message).toContain('toLocaleDateString');
  });

  it('應捕捉 .toLocaleString()（日期格式化繞過 SSOT）', () => {
    const sourceFile = project.createSourceFile('test4.tsx', `
      const s = new Date(123456).toLocaleString();
    `);
    const violations = checkTimeViolations(sourceFile);
    expect(violations.length).toBe(1);
    expect(violations[0].message).toContain('toLocaleString');
  });

  it('應捕捉經由 Date 變數的 .toLocaleString()', () => {
    const sourceFile = project.createSourceFile('test5.tsx', `
      const d = new Date(123456);
      const label = d.toLocaleString('zh-TW');
    `);
    const violations = checkTimeViolations(sourceFile);
    // new Date(param) 帶參數合法，但其後的 .toLocaleString() 應被攔
    expect(violations.some(v => v.message.includes('toLocaleString'))).toBe(true);
  });

  it('應捕捉 .toDateString() 與 .toTimeString()', () => {
    const sourceFile = project.createSourceFile('test6.tsx', `
      const a = new Date(1).toDateString();
      const b = new Date(2).toTimeString();
    `);
    const violations = checkTimeViolations(sourceFile);
    expect(violations.some(v => v.message.includes('toDateString'))).toBe(true);
    expect(violations.some(v => v.message.includes('toTimeString'))).toBe(true);
  });
});
