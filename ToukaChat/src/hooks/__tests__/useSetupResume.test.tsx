/**
 * 精靈改完密碼後的接續點（ADR-0053 補記 2026-10-23）。
 * 只記使用者編號、步驟與時間；同一位使用者、1 小時內才接續；壞掉或過期的整筆刪掉。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { StrictMode, type ReactNode } from 'react';
import { getSystemEpoch } from '@/lib/time_utils';
import {
  SETUP_RESUME_TTL_MS, peekSetupResume, saveSetupResume, takeSetupResume, useSetupResumeAfterLogin,
} from '../useSetupResume';

vi.mock('@/lib/time_utils', () => ({ getSystemEpoch: vi.fn() }));

const KEY = 'touka_setup_resume';
const NOW = 1_800_000_000_000;
const now = vi.mocked(getSystemEpoch);
const stored = () => window.localStorage.getItem(KEY);
const store = (value: unknown) => window.localStorage.setItem(KEY, JSON.stringify(value));

describe('接續點：存與取', () => {
  beforeEach(() => {
    window.localStorage.clear();
    now.mockReturnValue(NOW);
  });

  it('同一位使用者取得步驟，取過就刪', () => {
    saveSetupResume('admin-1', 'api_keys');
    expect(JSON.parse(stored()!)).toEqual({ userId: 'admin-1', stepId: 'api_keys', savedAt: NOW });
    expect(takeSetupResume('admin-1')).toBe('api_keys');
    expect(stored()).toBeNull();
    expect(takeSetupResume('admin-1')).toBeNull();
  });

  it('別人的接續點不取也不刪', () => {
    saveSetupResume('admin-1', 'api_keys');
    expect(takeSetupResume('admin-2')).toBeNull();
    expect(stored()).not.toBeNull();
    expect(takeSetupResume('admin-1')).toBe('api_keys');
  });

  it.each([
    ['剛好 1 小時內', NOW - SETUP_RESUME_TTL_MS, 'api_keys'],
    ['超過 1 小時', NOW - SETUP_RESUME_TTL_MS - 1, null],
    ['時間在未來（時鐘被調回）', NOW + 1, null],
  ])('%s', (_label, savedAt, expected) => {
    store({ userId: 'admin-1', stepId: 'api_keys', savedAt });
    expect(takeSetupResume('admin-1')).toBe(expected);
    expect(stored()).toBeNull();
  });

  it.each([
    ['不是物件', 'api_keys'],
    ['陣列', ['admin-1', 'api_keys', NOW]],
    ['使用者編號不是字串', { userId: 1, stepId: 'api_keys', savedAt: NOW }],
    ['使用者編號空字串', { userId: '', stepId: 'api_keys', savedAt: NOW }],
    ['不存在的步驟', { userId: 'admin-1', stepId: 'no_such_step', savedAt: NOW }],
    ['時間不是數字', { userId: 'admin-1', stepId: 'api_keys', savedAt: String(NOW) }],
  ])('格式不對（%s）整筆刪掉、不接續', (_label, value) => {
    store(value);
    expect(takeSetupResume('admin-1')).toBeNull();
    expect(stored()).toBeNull();
  });

  it('沒有紀錄時不寫也不刪', () => {
    const setItem = vi.spyOn(window.localStorage, 'setItem');
    const removeItem = vi.spyOn(window.localStorage, 'removeItem');
    expect(takeSetupResume('admin-1')).toBeNull();
    expect(peekSetupResume()).toBe(false);
    expect(setItem).not.toHaveBeenCalled();
    expect(removeItem).not.toHaveBeenCalled();
    setItem.mockRestore();
    removeItem.mockRestore();
  });

  it('登入頁只看不取：有效時為 true 且紀錄還在；過期為 false', () => {
    saveSetupResume('admin-1', 'api_keys');
    expect(peekSetupResume()).toBe(true);
    expect(stored()).not.toBeNull();
    now.mockReturnValue(NOW + SETUP_RESUME_TTL_MS + 1);
    expect(peekSetupResume()).toBe(false);
  });
});

describe('登入後取接續點的 hook', () => {
  beforeEach(() => {
    window.localStorage.clear();
    now.mockReturnValue(NOW);
  });

  it('同一位使用者登入後拿到步驟，紀錄被取走', () => {
    saveSetupResume('admin-1', 'api_keys');
    const { result } = renderHook(() => useSetupResumeAfterLogin('admin-1'));
    expect(result.current.resumeStepId).toBe('api_keys');
    expect(stored()).toBeNull();
  });

  it('還沒登入（null）不取', () => {
    saveSetupResume('admin-1', 'api_keys');
    const { result } = renderHook(() => useSetupResumeAfterLogin(null));
    expect(result.current.resumeStepId).toBeNull();
    expect(stored()).not.toBeNull();
  });

  it('從未登入變成登入時才取', () => {
    saveSetupResume('admin-1', 'api_keys');
    const { result, rerender } = renderHook(({ id }) => useSetupResumeAfterLogin(id), {
      initialProps: { id: null as string | null },
    });
    expect(result.current.resumeStepId).toBeNull();
    rerender({ id: 'admin-1' });
    expect(result.current.resumeStepId).toBe('api_keys');
  });

  it('StrictMode 跑兩次 effect 時，第二次讀不到也不會清掉已拿到的步驟', () => {
    saveSetupResume('admin-1', 'api_keys');
    const wrapper = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>;
    const { result } = renderHook(() => useSetupResumeAfterLogin('admin-1'), { wrapper });
    expect(result.current.resumeStepId).toBe('api_keys');
  });

  it('clearResume 之後就沒有步驟', () => {
    saveSetupResume('admin-1', 'api_keys');
    const { result, rerender } = renderHook(() => useSetupResumeAfterLogin('admin-1'));
    act(() => result.current.clearResume());
    rerender();
    expect(result.current.resumeStepId).toBeNull();
  });
});
