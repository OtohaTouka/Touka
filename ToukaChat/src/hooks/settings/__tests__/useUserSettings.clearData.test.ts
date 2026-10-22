import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve([]) }),
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

import { useUserSettings } from '../useUserSettings';
import { fetchTouka } from '@/lib/connections';
import { ERRORS } from '@/lib/error_codes';
import i18n from '@/lib/i18n';
import { toast } from 'sonner';

describe('useUserSettings.clearMyData (ADR-0107)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('POSTs current_password to /v1/auth/clear-my-data', async () => {
    (fetchTouka as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ status: 'cleared', cleared: { project_settings: 2 } }),
    });
    const { result } = renderHook(() => useUserSettings());
    let ok: boolean | undefined;
    await act(async () => { ok = await result.current.clearMyData('pw_123'); });

    const call = (fetchTouka as unknown as ReturnType<typeof vi.fn>).mock.calls
      .find((c) => c[1] === '/v1/auth/clear-my-data');
    expect(call).toBeTruthy();
    expect(call![2].method).toBe('POST');
    expect(JSON.parse(call![2].body).current_password).toBe('pw_123');
    expect(ok).toBe(true);
  });

  it('密碼打錯（403 AUTH_REAUTH_FAILED）：回 false、顯示在地化的錯誤，不自己重送', async () => {
    (fetchTouka as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 403,
      json: () => Promise.resolve({ detail: {
        code: 'AUTH_REAUTH_FAILED', id: ERRORS.AUTH_REAUTH_FAILED.id, message: ERRORS.AUTH_REAUTH_FAILED.msg,
      } }),
    });
    const { result } = renderHook(() => useUserSettings());
    let ok: boolean | undefined;
    await act(async () => { ok = await result.current.clearMyData('wrong'); });
    expect(ok).toBe(false);
    expect(toast.error).toHaveBeenCalledWith(i18n.t('error.auth.reauth_failed'));
    expect(fetchTouka).toHaveBeenCalledTimes(1);
  });

  it('returns false on backend failure', async () => {
    (fetchTouka as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      json: () => Promise.resolve({ detail: { message: 'bad' } }),
    });
    const { result } = renderHook(() => useUserSettings());
    let ok: boolean | undefined;
    await act(async () => { ok = await result.current.clearMyData('wrong'); });
    expect(ok).toBe(false);
  });
});
