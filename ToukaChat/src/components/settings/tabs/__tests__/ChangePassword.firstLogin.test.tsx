/**
 * 首次登入改密碼：管理員用一次性初始密碼登入後，在「變更密碼」換掉它。
 * 用真的 authService 與 fetchTouka（只換掉瀏覽器的 fetch）：目前密碼打錯時後端回 403
 * `AUTH_REAUTH_FAILED`，前端不續期、不重送（密碼只驗一次），顯示「目前密碼不正確」；
 * 打對時成功並登出（後端已撤銷 refresh token）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';
import { ERRORS } from '@/lib/error_codes';
import { clearAccessToken, setAccessToken } from '@/lib/connections';
import { getSystemEpoch } from '@/lib/time_utils';

const mockLogout = vi.fn();
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ logout: mockLogout }) }));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { ChangePassword } from '../ChangePassword';
import { toast } from 'sonner';

const fetchMock = vi.fn();

function validJwt(): string {
    const b64url = (o: object) =>
        btoa(JSON.stringify(o)).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
    return `h.${b64url({ sub: 'admin', exp: Math.floor(getSystemEpoch() / 1000) + 3600 })}.s`;
}

function fill(current: string, next: string) {
    const inputs = document.querySelectorAll('input[type="password"]');
    fireEvent.change(inputs[0], { target: { value: current } });
    fireEvent.change(inputs[1], { target: { value: next } });
    fireEvent.change(inputs[2], { target: { value: next } });
    fireEvent.click(screen.getByRole('button', { name: /變更密碼/ }));
}

describe('ChangePassword 首次登入（一次性初始密碼）', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        fetchMock.mockReset();
        vi.stubGlobal('fetch', fetchMock);
        setAccessToken(validJwt());
    });
    afterEach(() => {
        vi.unstubAllGlobals();
        clearAccessToken();
    });

    it('初始密碼打錯：只送一次、不續期，顯示「目前密碼不正確」、不登出', async () => {
        const { id } = ERRORS.AUTH_REAUTH_FAILED;
        fetchMock.mockResolvedValue(
            Response.json({ detail: { code: 'AUTH_REAUTH_FAILED', id } }, { status: 403 }),
        );
        render(<ChangePassword />);
        fill('wrong-initial', 'NewPass1!');

        expect(await screen.findByText('目前密碼不正確。')).toBeDefined();
        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(String(fetchMock.mock.calls[0][0])).toContain('/v1/auth/change-password');
        expect(mockLogout).not.toHaveBeenCalled();
    });

    it('初始密碼打對：成功並登出', async () => {
        fetchMock.mockResolvedValue(Response.json({ status: 'success' }));
        render(<ChangePassword />);
        fill('initial-pass', 'NewPass1!');

        await waitFor(() => expect(mockLogout).toHaveBeenCalled());
        expect(toast.success).toHaveBeenCalled();
        const body = JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body));
        expect(body).toEqual({ current_password: 'initial-pass', new_password: 'NewPass1!' });
    });
});
