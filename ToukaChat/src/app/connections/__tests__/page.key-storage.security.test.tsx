/**
 * 舊的 /connections 頁只寫本機、不送後端，所以不再收第三方金鑰：沒有金鑰欄位，
 * 用它存清單時，本機原本留著的真值也會換成遮罩。
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/components/LeftSidebar', () => ({ default: () => null }));

import ConnectionsPage from '@/app/connections/page';
import { SECRET_MASK } from '@/lib/secretMask';

const LEGACY_KEY = 'legacy-key-stored-by-old-version';
const KEY = 'touka_connections';
const stored = () => window.localStorage.getItem(KEY) ?? '';

describe('舊的 /connections 頁', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.localStorage.setItem(KEY, JSON.stringify([
      { id: 'p1', name: 'p1', url: 'http://backend.test:8000', apiKey: '', provider: 'proxy', protocol: 'openai', enabled: true },
      { id: 'c1', name: 'c1', url: 'http://c1.test', apiKey: LEGACY_KEY, provider: 'custom', protocol: 'openai', enabled: true },
    ]));
  });

  it('沒有金鑰欄位', () => {
    const { container } = render(<ConnectionsPage />);

    expect(container.querySelector('input[type="password"]')).toBeNull();
    expect(screen.queryByPlaceholderText('sk-...')).toBeNull();
  });

  it('切換連線時，本機清單裡舊的真值換成遮罩', async () => {
    render(<ConnectionsPage />);

    await act(async () => { fireEvent.click(screen.getAllByRole('switch')[1]); });

    expect(stored()).not.toContain(LEGACY_KEY);
    expect(stored()).toContain(SECRET_MASK);
  });

  it('編輯後存檔，金鑰保留成遮罩，不會被清空', async () => {
    render(<ConnectionsPage />);

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '編輯連線 c1' })); });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '儲存變更' })); });

    const c1 = JSON.parse(stored()).find((conn: { id: string }) => conn.id === 'c1');
    expect(c1.apiKey).toBe(SECRET_MASK);
  });
});
