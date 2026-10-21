import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

vi.mock('@/lib/connections', () => ({
    fetchTouka: vi.fn(),
    getActiveProxyUrl: () => 'http://localhost:8000',
}));
vi.mock('@/lib/error_handler', () => ({
    logSSOTError: vi.fn(),
    getErrorMessage: vi.fn().mockResolvedValue('err'),
    errorMsg: () => 'err',
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { fetchTouka } from '@/lib/connections';
import { useUserSettings } from '../useUserSettings';

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;

describe('useUserSettings.updateOwnProfile（自助個資 PATCH /v1/auth/me，P3）', () => {
    beforeEach(() => vi.clearAllMocks());

    it('PATCH /v1/auth/me 帶 profile 欄位，成功回 true，且不打 admin /users', async () => {
        mockFetch.mockResolvedValue({ ok: true, json: async () => ({}) });
        const { result } = renderHook(() => useUserSettings());
        let ok = false;
        await act(async () => { ok = await result.current.updateOwnProfile({ display_name: 'N', bio: 'b' }); });
        expect(ok).toBe(true);
        const call = mockFetch.mock.calls.find(c => c[1] === '/v1/auth/me');
        expect(call).toBeTruthy();
        expect(call![2].method).toBe('PATCH');
        expect(JSON.parse(call![2].body)).toEqual({ display_name: 'N', bio: 'b' });
        // 自助路徑不得呼叫 admin-only 端點
        expect(mockFetch.mock.calls.some(c => String(c[1]).includes('/v1/admin/users'))).toBe(false);
    });

    it('後端失敗（如 403）時回 false', async () => {
        mockFetch.mockResolvedValue({ ok: false, status: 403, json: async () => ({}) });
        const { result } = renderHook(() => useUserSettings());
        let ok = true;
        await act(async () => { ok = await result.current.updateOwnProfile({ display_name: 'N' }); });
        expect(ok).toBe(false);
    });
});

describe('useUserSettings.updateUser（修復 0：空密碼＝保持不變）', () => {
    beforeEach(() => vi.clearAllMocks());

    it('password 為空字串時，PATCH body 不得含 password 鍵（2026-07-11 事故對釘）', async () => {
        mockFetch.mockResolvedValue({ ok: true, json: async () => ({}) });
        const { result } = renderHook(() => useUserSettings());
        await act(async () => {
            await result.current.updateUser('u1', { owned_agents: ['Sumi'], password: '' });
        });
        const call = mockFetch.mock.calls.find(c => c[1] === '/v1/admin/users/u1');
        expect(call).toBeTruthy();
        const body = JSON.parse(call![2].body);
        expect(body).toEqual({ owned_agents: ['Sumi'] });
        expect('password' in body).toBe(false);
    });

    it('password 非空時照常送出（真密碼變更不受影響）', async () => {
        mockFetch.mockResolvedValue({ ok: true, json: async () => ({}) });
        const { result } = renderHook(() => useUserSettings());
        await act(async () => {
            await result.current.updateUser('u1', { password: 'BrandNewPass456!' });
        });
        const call = mockFetch.mock.calls.find(c => c[1] === '/v1/admin/users/u1');
        const body = JSON.parse(call![2].body);
        expect(body.password).toBe('BrandNewPass456!');
    });
});
