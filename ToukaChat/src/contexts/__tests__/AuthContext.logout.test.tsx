/**
 * AuthContext.logout() 行為修復測試
 *
 * Bug 修復：AuthContext.logout() 須在清空 React state 後呼叫
 * router.push('/login')，以確保 AuthGuard 正確導向登入頁。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import React from 'react';

const mockPush = vi.fn();

vi.mock('next/navigation', () => ({
    useRouter: () => ({ push: mockPush }),
    usePathname: () => '/',
}));

vi.mock('@/lib/api/authService', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/api/authService')>(),
    authService: {
        refresh: vi.fn().mockRejectedValue(new Error('no session')),
        login: vi.fn(),
    },
}));

vi.mock('@/lib/connections', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/connections')>(),
    getActiveProxyUrl: vi.fn().mockReturnValue('http://localhost:8000'),
    fetchTouka: vi.fn().mockResolvedValue({
        ok: true,
        headers: new Headers(),
        json: async () => ({}),
    }),
        // 修復後 logoutTouka 只清 token，不做 reload
    logoutTouka: vi.fn().mockResolvedValue(undefined),
    // 修復 2/3（ADR-0144）新增介面
    setSessionExpiredHandler: vi.fn(),
    markHasLoggedIn: vi.fn(),
}));

vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

import { AuthProvider, useAuth } from '../AuthContext';

const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(AuthProvider, null, children);

describe('AuthContext.logout()', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('logout() 後 isAuthenticated 變為 false', async () => {
        const { result } = renderHook(() => useAuth(), { wrapper });
        await waitFor(() => expect(result.current.isLoading).toBe(false));

        await act(async () => {
            await result.current.logout();
        });

        expect(result.current.isAuthenticated).toBe(false);
    });

    it('logout() 後 user 變為 null', async () => {
        const { result } = renderHook(() => useAuth(), { wrapper });
        await waitFor(() => expect(result.current.isLoading).toBe(false));

        await act(async () => {
            await result.current.logout();
        });

        expect(result.current.user).toBeNull();
    });

    it('logout() 呼叫 router.push("/login")', async () => {
        const { result } = renderHook(() => useAuth(), { wrapper });
        await waitFor(() => expect(result.current.isLoading).toBe(false));

        await act(async () => {
            await result.current.logout();
        });

        expect(mockPush).toHaveBeenCalledWith('/login');
    });
});
