import { beforeEach, describe, expect, it, vi } from 'vitest';
import { removeLocalStorageItem } from '../useLocalStorage';

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
