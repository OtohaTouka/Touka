/** 減少動態（ADR-0300）：App 設定優先，「跟隨系統」才看系統。 */
import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useReducedMotionPreference } from '../useReducedMotionPreference';

const state = vi.hoisted(() => ({ setting: 'system', system: false }));
vi.mock('@/lib/ThemeContext', () => ({ useTheme: () => ({ reduceMotion: state.setting }) }));
vi.mock('../useMediaQuery', () => ({
  useMediaQuery: (query: string) => query === '(prefers-reduced-motion: reduce)' && state.system,
}));

describe('useReducedMotionPreference', () => {
  it.each([
    ['on', false, true],
    ['on', true, true],
    ['off', false, false],
    ['off', true, false],
    ['system', false, false],
    ['system', true, true],
  ])('App 設定 %s、系統 %s → %s', (setting, system, expected) => {
    state.setting = setting;
    state.system = system;
    expect(renderHook(() => useReducedMotionPreference()).result.current).toBe(expected);
  });
});
