import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useMediaQuery } from '@/hooks/useMediaQuery';

type ChangeListener = (e: { matches: boolean }) => void;

// 安裝一個可程式化觸發 'change' 的 matchMedia mock（jsdom 預設未實作 matchMedia）。
function installMatchMedia(initialMatches: boolean) {
  const listeners = new Set<ChangeListener>();
  const mql = {
    matches: initialMatches,
    media: '',
    onchange: null,
    addEventListener: vi.fn((_type: string, cb: ChangeListener) => listeners.add(cb)),
    removeEventListener: vi.fn((_type: string, cb: ChangeListener) => listeners.delete(cb)),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  };
  const matchMedia = vi.fn((query: string) => {
    mql.media = query;
    return mql;
  });
  Object.defineProperty(window, 'matchMedia', { writable: true, configurable: true, value: matchMedia });
  const fireChange = (matches: boolean) => {
    mql.matches = matches;
    listeners.forEach((cb) => cb({ matches }));
  };
  return { mql, matchMedia, fireChange };
}

describe('useMediaQuery', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('初始值取自 mql.matches', () => {
    installMatchMedia(true);
    const { result } = renderHook(() => useMediaQuery('(max-width: 768px)'));
    expect(result.current).toBe(true);
  });

  it("訂閱 MediaQueryList 的 'change' 事件（而非 window resize），結果改變時更新", () => {
    const { mql, fireChange } = installMatchMedia(false);
    const { result } = renderHook(() => useMediaQuery('(max-width: 768px)'));

    expect(mql.addEventListener).toHaveBeenCalledWith('change', expect.any(Function));
    expect(result.current).toBe(false);

    act(() => fireChange(true));
    expect(result.current).toBe(true);
  });

  it('卸載時移除 change 監聽（無洩漏）', () => {
    const { mql } = installMatchMedia(false);
    const { unmount } = renderHook(() => useMediaQuery('(max-width: 768px)'));
    unmount();
    expect(mql.removeEventListener).toHaveBeenCalledWith('change', expect.any(Function));
  });

  it('query 變更時重新由新的 mql.matches 同步', () => {
    const { mql } = installMatchMedia(false);
    const { result, rerender } = renderHook(({ q }) => useMediaQuery(q), {
      initialProps: { q: '(max-width: 768px)' },
    });
    expect(result.current).toBe(false);

    mql.matches = true;
    rerender({ q: '(min-width: 1024px)' });
    expect(result.current).toBe(true);
  });
});
