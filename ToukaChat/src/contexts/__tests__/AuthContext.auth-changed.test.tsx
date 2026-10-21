/**
 * ADR-0153：認證狀態變動 → 發 window 事件 touka_auth_changed。
 *
 * SystemStatusContext 據此重探需認證的 /v1/memory——修「auth 還原前送出無卡探針被 401、
 * hybrid/standalone 徽章卡在 standalone」的啟動競態。契約：
 *   - 掛載初值（isAuthenticated=false）不誤發；
 *   - 開機還原成功（false→true）發一次；
 *   - 還原失敗（維持 false、無變動）不發；
 *   - 登出（true→false）發。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import React from 'react';

vi.mock('next/navigation', () => ({
    useRouter: () => ({ push: vi.fn() }),
    usePathname: () => '/',
}));

const mockRefresh = vi.fn();
vi.mock('@/lib/api/authService', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/api/authService')>(),
    authService: {
        refresh: (...a: unknown[]) => mockRefresh(...a),
        login: vi.fn().mockResolvedValue({ access_token: `e30.${btoa(JSON.stringify({ sub: 'u1', type: 'access', exp: 9999999999 }))}.synthetic`, token_type: 'bearer' }),
    },
}));
vi.mock('@/lib/connections', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/connections')>(),
    getActiveProxyUrl: vi.fn().mockReturnValue('http://localhost:8000'),
    fetchTouka: vi.fn().mockResolvedValue({
        ok: true,
        headers: new Headers(),
        json: async () => ({ id: 'u1', email: 'a@b.c', role: 'admin' }),
    }),
        logoutTouka: vi.fn().mockResolvedValue(undefined),
    setSessionExpiredHandler: vi.fn(),
    markHasLoggedIn: vi.fn(),
}));
vi.mock('sonner', () => ({
    toast: { warning: vi.fn(), success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));
vi.mock('@/lib/i18n', () => ({ default: { t: (k: string) => k } }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

import { AuthProvider, useAuth } from '../AuthContext';

const syntheticToken = `e30.${btoa(JSON.stringify({ sub: 'u1', type: 'access', exp: 9999999999 }))}.synthetic`;

const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(AuthProvider, null, children);

describe('AuthContext touka_auth_changed（ADR-0153）', () => {
    let events: number;
    const handler = () => { events += 1; };

    beforeEach(() => {
        vi.clearAllMocks();
        events = 0;
        window.addEventListener('touka_auth_changed', handler);
    });
    afterEach(() => window.removeEventListener('touka_auth_changed', handler));

    it('開機還原成功（false→true）→ 發事件；掛載初值不誤發', async () => {
        mockRefresh.mockResolvedValue({ access_token: syntheticToken, token_type: 'bearer' });
        const { result } = renderHook(() => useAuth(), { wrapper });

        expect(events).toBe(0); // 掛載當下（還原未完成）不誤發

        await waitFor(() => expect(result.current.isAuthenticated).toBe(true));
        await waitFor(() => expect(events).toBeGreaterThanOrEqual(1));
    });

    it('還原失敗（維持 false、無變動）→ 不發事件', async () => {
        mockRefresh.mockRejectedValue(new Error('no session'));
        const { result } = renderHook(() => useAuth(), { wrapper });
        await waitFor(() => expect(result.current.isLoading).toBe(false));
        expect(result.current.isAuthenticated).toBe(false);
        await new Promise(r => setTimeout(r, 30));
        expect(events).toBe(0);
    });

    it('登出（true→false）→ 發事件', async () => {
        mockRefresh.mockResolvedValue({ access_token: syntheticToken, token_type: 'bearer' });
        const { result } = renderHook(() => useAuth(), { wrapper });
        await waitFor(() => expect(result.current.isAuthenticated).toBe(true));
        await waitFor(() => expect(events).toBeGreaterThanOrEqual(1));
        const before = events;

        await act(async () => { await result.current.logout(); });

        await waitFor(() => expect(events).toBeGreaterThan(before));
    });
});
