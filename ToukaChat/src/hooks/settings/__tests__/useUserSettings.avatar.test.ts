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

const JPEG = new File([new Uint8Array([0xff, 0xd8, 0xff, 0xe0])], 'a.jpg', { type: 'image/jpeg' });

describe('useUserSettings avatar (ADR-0106)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('uploadOwnAvatar POSTs multipart FormData to /v1/auth/avatar', async () => {
    const { result } = renderHook(() => useUserSettings());
    await act(async () => { await result.current.uploadOwnAvatar(JPEG); });

    const call = (fetchTouka as unknown as ReturnType<typeof vi.fn>).mock.calls
      .find((c) => c[1] === '/v1/auth/avatar');
    expect(call).toBeTruthy();
    expect(call![2].method).toBe('POST');
    expect(call![2].body).toBeInstanceOf(FormData);
  });

  it('deleteOwnAvatar DELETEs /v1/auth/avatar', async () => {
    const { result } = renderHook(() => useUserSettings());
    await act(async () => { await result.current.deleteOwnAvatar(); });

    const call = (fetchTouka as unknown as ReturnType<typeof vi.fn>).mock.calls
      .find((c) => c[1] === '/v1/auth/avatar');
    expect(call).toBeTruthy();
    expect(call![2].method).toBe('DELETE');
  });
});
