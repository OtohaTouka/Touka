import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { removeLocalStorageItem, useLocalStorage } from '../useLocalStorage';

describe('a storage sanitizer', () => {
  beforeEach(() => { window.localStorage.clear(); });
  const upper = (value: string[]) => value.map(item => item.toUpperCase());

  it('keeps the value in state and stores the transformed value', () => {
    const { result } = renderHook(() => useLocalStorage<string[]>('transform-test-key', [], upper));

    act(() => result.current[1](['a']));
    act(() => result.current[1](previous => [...previous, 'b']));

    expect(result.current[0]).toEqual(['a', 'b']);
    expect(JSON.parse(window.localStorage.getItem('transform-test-key') ?? 'null')).toEqual(['A', 'B']);
  });

  it('also sanitizes the value read from storage', () => {
    window.localStorage.setItem('transform-test-key', JSON.stringify(['a']));

    const { result } = renderHook(() => useLocalStorage<string[]>('transform-test-key', [], upper));

    expect(result.current[0]).toEqual(['A']);
  });

  it('stores the value unchanged without a sanitizer', () => {
    const { result } = renderHook(() => useLocalStorage<string[]>('transform-test-key', []));

    act(() => result.current[1](['a']));

    expect(JSON.parse(window.localStorage.getItem('transform-test-key') ?? 'null')).toEqual(['a']);
  });
});

describe('exact-key storage removal', () => {
  beforeEach(() => { vi.restoreAllMocks(); window.localStorage.clear(); });
  it('removes an opaque value without reading it or touching unrelated keys', () => {
    window.localStorage.setItem('retired-test-key', '{invalid secret json');
    window.localStorage.setItem('theme', 'dark');
    const read = vi.spyOn(window.localStorage, 'getItem');
    expect(removeLocalStorageItem('retired-test-key')).toBe(true);
    expect(read).not.toHaveBeenCalled();
    expect(window.localStorage.getItem('retired-test-key')).toBeNull();
    expect(window.localStorage.getItem('theme')).toBe('dark');
  });
  it('reports denial without leaking the storage exception', () => {
    vi.spyOn(window.localStorage, 'removeItem').mockImplementation(() => { throw new Error('private test value'); });
    expect(removeLocalStorageItem('retired-test-key')).toBe(false);
  });
});
