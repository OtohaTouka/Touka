import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { RetiredSnsStorageCleanup } from '../RetiredSnsStorageCleanup';

describe('retired SNS credential cleanup', () => {
  beforeEach(() => { vi.restoreAllMocks(); window.localStorage.clear(); });
  it('purges the retired key on startup without reading or rewriting it', () => {
    window.localStorage.setItem('touka_sns_bots', 'unparsed secret test value');
    window.localStorage.setItem('theme', 'dark');
    const read = vi.spyOn(window.localStorage, 'getItem');
    const write = vi.spyOn(window.localStorage, 'setItem');
    render(<RetiredSnsStorageCleanup />);
    expect(read).not.toHaveBeenCalled();
    expect(write).not.toHaveBeenCalled();
    expect(window.localStorage.getItem('touka_sns_bots')).toBeNull();
    expect(window.localStorage.getItem('theme')).toBe('dark');
  });
  it('handles an old tab writing the retired key again and ignores other keys', () => {
    render(<RetiredSnsStorageCleanup />);
    const remove = vi.spyOn(window.localStorage, 'removeItem');
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'theme' })));
    expect(remove).not.toHaveBeenCalled();
    window.localStorage.setItem('touka_sns_bots', 'another private test value');
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'touka_sns_bots' })));
    expect(remove).toHaveBeenCalledExactlyOnceWith('touka_sns_bots');
    expect(window.localStorage.getItem('touka_sns_bots')).toBeNull();
  });
  it('shows a safe alert when storage cleanup is denied', () => {
    vi.spyOn(window.localStorage, 'removeItem').mockImplementation(() => { throw new Error('private test value'); });
    render(<RetiredSnsStorageCleanup />);
    expect(screen.getByRole('alert')).toHaveTextContent('無法清除');
    expect(screen.getByRole('alert')).not.toHaveTextContent('private test value');
  });
  it('removes its event listener on unmount', () => {
    const { unmount } = render(<RetiredSnsStorageCleanup />);
    const remove = vi.spyOn(window.localStorage, 'removeItem');
    unmount();
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'touka_sns_bots' })));
    expect(remove).not.toHaveBeenCalled();
  });
});
