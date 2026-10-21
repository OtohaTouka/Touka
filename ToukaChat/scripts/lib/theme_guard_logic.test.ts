import { describe, expect, it } from 'vitest';
import { Project, ScriptKind } from 'ts-morph';
import { checkThemeViolations } from './theme_guard_logic';

function check(source: string) {
  const project = new Project({ useInMemoryFileSystem: true });
  return checkThemeViolations(project.createSourceFile('fixture.tsx', source, { scriptKind: ScriptKind.TSX }));
}

describe('P11 theme guard JSX 型別收窄行為', () => {
  it('仍攔截 JSX 物件中的硬編碼顏色並保留位置', () => {
    const violations = check('const view = <div style={{ color: "#fff" }} />;');
    expect(violations).toHaveLength(1);
    expect(violations[0]).toMatchObject({ line: 1, code: '{ color: "#fff" }' });
    expect(violations[0].message).toContain('內聯樣式');
  });

  it('放行 CSS 變數與語義化類別', () => {
    expect(check('const view = <div className="text-primary" style={{ color: "var(--primary)" }} />;')).toEqual([]);
  });

  it('忽略非物件 JSX expression、字串屬性與沒有 initializer 的屬性', () => {
    expect(check('const view = <><div style={styles} /><div style="inherit" /><div style /></>;')).toEqual([]);
  });

  it('仍攔截硬編碼 Tailwind 類別', () => {
    expect(check('const view = <div className="text-red-500" />;')[0].code).toBe('text-red-500');
  });
});
