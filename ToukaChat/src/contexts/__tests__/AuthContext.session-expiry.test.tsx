/**
 * 修復 2＋修復 3（ADR-0144）AuthContext 半邊。
 *
 * - 掛載時向 connections 註冊 session-expired handler；觸發時清認證狀態、
 *   toast 說明原因、導向 /login（07-11 事故的「死循環滿屏 401」對釘）。
 * - 卸載時反註冊（handler 不得洩漏到已卸載的元件）。
 * - 登入成功／開機還原成功 → markHasLoggedIn()（修復 3 旗標：此瀏覽器永不
 *   採納 LDH 降級卡）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import React from 'react';

const mockPush = vi.fn();
let capturedHandler: (() => void) | null = null;

vi.mock('next/navigation', () => ({
    useRouter: () => ({ push: mockPush }),
    usePathname: () => '/',
}));

const mockRefresh = vi.fn();
vi.mock('@/lib/api/authService', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/api/authService')>(),
    authService: {
        refresh: (...args: unknown[]) => mockRefresh(...args),
        login: vi.fn().mockResolvedValue({ access_token: `e30.${btoa(JSON.stringify({ sub: 'u1', type: 'access', exp: 9999999999 }))}.synthetic`, token_type: 'bearer' }),
    },
}));

const mockMarkHasLoggedIn = vi.fn();
vi.mock('@/lib/connections', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/connections')>(),
    getActiveProxyUrl: vi.fn().mockReturnValue('http://localhost:8000'),
    fetchTouka: vi.fn().mockResolvedValue({
        ok: true,
        headers: new Headers(),
        json: async () => ({ id: 'u1', email: 'a@b.c', role: 'admin' }),
    }),
        logoutTouka: vi.fn().mockResolvedValue(undefined),
    setSessionExpiredHandler: vi.fn((cb: (() => void) | null) => { capturedHandler = cb; }),
    markHasLoggedIn: (...args: unknown[]) => mockMarkHasLoggedIn(...args),
}));

const mockToastWarning = vi.fn();
vi.mock('sonner', () => ({
    toast: {
        warning: (...args: unknown[]) => mockToastWarning(...args),
        success: vi.fn(), error: vi.fn(), info: vi.fn(),
    },
}));
vi.mock('@/lib/i18n', () => ({ default: { t: (k: string) => k } }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

import { AuthProvider, useAuth } from '../AuthContext';

const syntheticToken = `e30.${btoa(JSON.stringify({ sub: 'u1', type: 'access', exp: 9999999999 }))}.synthetic`;

const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(AuthProvider, null, children);

describe('AuthContext session-expired（修復 2）', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        capturedHandler = null;
        mockRefresh.mockResolvedValue({ access_token: syntheticToken, token_type: 'bearer' });
    });

    it('掛載即註冊 handler；觸發後清認證狀態＋toast＋導向 /login', async () => {
        const { result } = renderHook(() => useAuth(), { wrapper });
        await waitFor(() => expect(result.current.isLoading).toBe(false));
        expect(result.current.isAuthenticated).toBe(true); // 開機還原成功

        expect(capturedHandler).toBeTypeOf('function');

        act(() => { capturedHandler!(); });

        expect(result.current.isAuthenticated).toBe(false);
        expect(result.current.user).toBeNull();
        expect(mockToastWarning).toHaveBeenCalledTimes(1);
        expect(mockToastWarning.mock.calls[0][0]).toBe('error.auth.session_expired');
        expect(mockPush).toHaveBeenCalledWith('/login');
    });

    it('卸載時反註冊（handler 設回 null）', async () => {
        const { result, unmount } = renderHook(() => useAuth(), { wrapper });
        await waitFor(() => expect(result.current.isLoading).toBe(false));
        expect(capturedHandler).toBeTypeOf('function');

        unmount();

        expect(capturedHandler).toBeNull();
    });
});

describe('AuthContext 曾登入旗標（修復 3）', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        capturedHandler = null;
    });

    it('開機還原成功 → markHasLoggedIn（涵蓋旗標問世前就登入的瀏覽器）', async () => {
        mockRefresh.mockResolvedValue({ access_token: syntheticToken, token_type: 'bearer' });
        const { result } = renderHook(() => useAuth(), { wrapper });
        await waitFor(() => expect(result.current.isLoading).toBe(false));

        expect(result.current.isAuthenticated).toBe(true);
        expect(mockMarkHasLoggedIn).toHaveBeenCalled();
    });

    it('登入成功 → markHasLoggedIn', async () => {
        mockRefresh.mockRejectedValue(new Error('no session'));
        const { result } = renderHook(() => useAuth(), { wrapper });
        await waitFor(() => expect(result.current.isLoading).toBe(false));
        expect(result.current.isAuthenticated).toBe(false);
        mockMarkHasLoggedIn.mockClear();

        await act(async () => { await result.current.login('a@b.c', 'Password123!'); });

        expect(result.current.isAuthenticated).toBe(true);
        expect(mockMarkHasLoggedIn).toHaveBeenCalledTimes(1);
    });

    it('開機還原失敗（未登入）→ 不標記', async () => {
        mockRefresh.mockRejectedValue(new Error('no session'));
        const { result } = renderHook(() => useAuth(), { wrapper });
        await waitFor(() => expect(result.current.isLoading).toBe(false));

        expect(mockMarkHasLoggedIn).not.toHaveBeenCalled();
    });
});
