/**
 * 舊版把連線的第三方 API 金鑰真值存在瀏覽器；載入時換成遮罩佔位（ADR-0224 的做法）。
 * 網址與其他欄位不動、不通知換網址；別的分頁寫回真值時再換一次；寫不回去時顯示提示，不帶金鑰。
 */
import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SECRET_MASK } from '@/lib/secretMask';
import { StoredConnectionKeyCleanup } from '../StoredConnectionKeyCleanup';

const LEGACY_KEY = 'legacy-key-stored-by-old-version';
const KEY = 'touka_connections';
const legacy = () => [
  { id: 'proxy', name: 'proxy', url: 'http://backend.test:8000', apiKey: '', provider: 'proxy', protocol: 'openai', enabled: true },
  { id: 'c1', name: 'c1', url: 'http://c1.test', apiKey: LEGACY_KEY, provider: 'custom', protocol: 'openai', enabled: false },
];
const stored = () => JSON.parse(window.localStorage.getItem(KEY) ?? 'null');

describe('stored connection key cleanup', () => {
  beforeEach(() => { vi.restoreAllMocks(); window.localStorage.clear(); });

  it('masks a key an older version stored and keeps everything else', () => {
    window.localStorage.setItem(KEY, JSON.stringify(legacy()));
    window.localStorage.setItem('theme', 'dark');
    const updated = vi.fn();
    window.addEventListener('touka_connections_updated', updated);

    render(<StoredConnectionKeyCleanup />);
    window.removeEventListener('touka_connections_updated', updated);

    expect(window.localStorage.getItem(KEY)).not.toContain(LEGACY_KEY);
    expect(stored()).toEqual(legacy().map(c => (c.apiKey ? { ...c, apiKey: SECRET_MASK } : c)));
    expect(window.localStorage.getItem('theme')).toBe('dark');
    // 網址沒變，不能讓別的元件以為換了後端。
    expect(updated).not.toHaveBeenCalled();
  });

  it('does not write when nothing needs masking', () => {
    window.localStorage.setItem(KEY, JSON.stringify([{ ...legacy()[1], apiKey: SECRET_MASK }]));
    const write = vi.spyOn(window.localStorage, 'setItem');

    render(<StoredConnectionKeyCleanup />);

    expect(write).not.toHaveBeenCalled();
  });

  it.each([['not json', '{broken'], ['not a list', '{"apiKey":"x"}'], ['empty', '']])(
    'leaves a stored value it cannot read alone (%s)',
    (_name, value) => {
      window.localStorage.setItem(KEY, value);
      const write = vi.spyOn(window.localStorage, 'setItem');

      render(<StoredConnectionKeyCleanup />);

      expect(write).not.toHaveBeenCalled();
      expect(screen.queryByRole('alert')).toBeNull();
    },
  );

  it('masks again when another tab writes a key back', () => {
    render(<StoredConnectionKeyCleanup />);
    window.localStorage.setItem(KEY, JSON.stringify(legacy()));

    act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'theme' })));
    expect(window.localStorage.getItem(KEY)).toContain(LEGACY_KEY);
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: KEY })));

    expect(window.localStorage.getItem(KEY)).not.toContain(LEGACY_KEY);
  });

  it('shows a translated alert without the key when the browser refuses the write', () => {
    window.localStorage.setItem(KEY, JSON.stringify(legacy()));
    vi.spyOn(window.localStorage, 'setItem').mockImplementation(() => { throw new Error(LEGACY_KEY); });

    render(<StoredConnectionKeyCleanup />);

    expect(screen.getByRole('alert')).toHaveTextContent('連線金鑰');
    expect(screen.getByRole('alert')).not.toHaveTextContent(LEGACY_KEY);
  });

  it('removes its event listener on unmount', () => {
    const { unmount } = render(<StoredConnectionKeyCleanup />);
    unmount();
    window.localStorage.setItem(KEY, JSON.stringify(legacy()));

    act(() => window.dispatchEvent(new StorageEvent('storage', { key: KEY })));

    expect(window.localStorage.getItem(KEY)).toContain(LEGACY_KEY);
  });
});
