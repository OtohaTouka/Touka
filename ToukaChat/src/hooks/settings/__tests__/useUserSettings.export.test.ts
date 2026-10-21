import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn(),
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
vi.mock('@/lib/time_utils', () => ({
  getSystemNow: () => ({ toISOString: () => '2026-06-24T00:00:00.000Z' }),
}));

import { useUserSettings } from '../useUserSettings';
import { fetchTouka } from '@/lib/connections';

describe('useUserSettings.exportMyData (ADR-0110)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    global.URL.createObjectURL = vi.fn(() => 'blob:mock');
    global.URL.revokeObjectURL = vi.fn();
  });

  it('POSTs current_password + passphrase to /v1/auth/export-my-data and triggers a download', async () => {
    (fetchTouka as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      blob: () => Promise.resolve(new Blob(['data'])),
    });
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {});

    const { result } = renderHook(() => useUserSettings());
    let ok: boolean | undefined;
    await act(async () => {
      ok = await result.current.exportMyData('pw_123', 'passphrase-123456');
    });

    const call = (fetchTouka as unknown as ReturnType<typeof vi.fn>).mock.calls.find(
      (c) => c[1] === '/v1/auth/export-my-data',
    );
    expect(call).toBeTruthy();
    expect(call![2].method).toBe('POST');
    const body = JSON.parse(call![2].body);
    expect(body.current_password).toBe('pw_123');
    expect(body.passphrase).toBe('passphrase-123456');
    expect(global.URL.createObjectURL).toHaveBeenCalled();
    expect(clickSpy).toHaveBeenCalled();
    expect(ok).toBe(true);
    clickSpy.mockRestore();
  });

  it('returns false on backend failure and does not download', async () => {
    (fetchTouka as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      json: () => Promise.resolve({ detail: { message: 'bad password' } }),
    });
    const { result } = renderHook(() => useUserSettings());
    let ok: boolean | undefined;
    await act(async () => {
      ok = await result.current.exportMyData('wrong', 'passphrase-123456');
    });
    expect(ok).toBe(false);
    expect(global.URL.createObjectURL).not.toHaveBeenCalled();
  });
});
