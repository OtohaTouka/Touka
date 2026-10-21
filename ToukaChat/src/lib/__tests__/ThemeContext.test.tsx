import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render } from '@testing-library/react';
import { THEMES, LIGHT_THEMES } from '@/lib/design-system/tokens';
import { formatToTaipeiTime, setTimeFormat12h } from '@/lib/time_utils';

// 以 next-themes 的 resolvedTheme 驅動模式（ADR-0105）：mode 軸由 next-themes 管。
const mockResolved = { current: 'dark' as 'dark' | 'light' };
vi.mock('next-themes', () => ({
  useTheme: () => ({ resolvedTheme: mockResolved.current }),
}));

import { ThemeProvider } from '../ThemeContext';

describe('ThemeContext mode-aware 中性色注入（ADR-0105）', () => {
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.removeAttribute('style');
    document.documentElement.className = '';
  });

  it('暗模式：applyTheme 注入 THEMES 中性色（--background = touka 暗底）', () => {
    mockResolved.current = 'dark';
    render(<ThemeProvider><div /></ThemeProvider>);
    expect(document.documentElement.style.getPropertyValue('--background')).toBe(THEMES.touka.bg);
  });

  it('亮模式：applyTheme 注入 LIGHT_THEMES 中性色（--background 取灰白底）', () => {
    mockResolved.current = 'light';
    render(<ThemeProvider><div /></ThemeProvider>);
    expect(document.documentElement.style.getPropertyValue('--background')).toBe(LIGHT_THEMES.touka.bg);
  });

  it('亮模式：accent 與 mode 正交——primary 取當前主題的亮階文字色', () => {
    mockResolved.current = 'light';
    render(<ThemeProvider><div /></ThemeProvider>);
    expect(document.documentElement.style.getPropertyValue('--primary')).toBe(LIGHT_THEMES.touka.primary);
  });
});

describe('ThemeContext 外觀打磨包偏好（密度/泡泡/時間制/減少動態）', () => {
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.className = '';
  });
  afterEach(() => setTimeFormat12h(true)); // 還原全域時間制，避免污染其他測試

  it('reduceMotion=on → <html> 掛 motion-reduce class（強制降動畫）', () => {
    window.localStorage.setItem('touka_reduce_motion', 'on');
    render(<ThemeProvider><div /></ThemeProvider>);
    expect(document.documentElement.classList.contains('motion-reduce')).toBe(true);
    expect(document.documentElement.classList.contains('motion-full')).toBe(false);
  });

  it('reduceMotion=off → <html> 掛 motion-full class（忽略 OS 偏好）', () => {
    window.localStorage.setItem('touka_reduce_motion', 'off');
    render(<ThemeProvider><div /></ThemeProvider>);
    expect(document.documentElement.classList.contains('motion-full')).toBe(true);
    expect(document.documentElement.classList.contains('motion-reduce')).toBe(false);
  });

  it('reduceMotion 預設 system → 不掛任何 motion class', () => {
    render(<ThemeProvider><div /></ThemeProvider>);
    expect(document.documentElement.classList.contains('motion-reduce')).toBe(false);
    expect(document.documentElement.classList.contains('motion-full')).toBe(false);
  });

  it('timeFormat=24h → 同步全域時間制（formatToTaipeiTime 不含 上午/下午）', () => {
    window.localStorage.setItem('touka_time_format', '24h');
    render(<ThemeProvider><div /></ThemeProvider>);
    const out = formatToTaipeiTime(1_735_968_300_000, { hour: 'numeric', minute: 'numeric' });
    expect(out).not.toMatch(/上午|下午/);
  });
});
