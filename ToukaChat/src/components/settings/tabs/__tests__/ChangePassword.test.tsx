import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';
import i18n from '@/lib/i18n';
import { ERRORS } from '@/lib/error_codes';

const mockChangePassword = vi.fn();
vi.mock('@/lib/api/authService', () => ({
    authService: { changePassword: (...args: unknown[]) => mockChangePassword(...args) },
}));
const mockLogout = vi.fn();
vi.mock('@/contexts/AuthContext', () => ({
    useAuth: () => ({ logout: mockLogout }),
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { ChangePassword } from '../ChangePassword';
import { toast } from 'sonner';

function fill(current: string, next: string, confirm: string) {
    const inputs = document.querySelectorAll('input[type="password"]');
    fireEvent.change(inputs[0], { target: { value: current } });
    fireEvent.change(inputs[1], { target: { value: next } });
    fireEvent.change(inputs[2], { target: { value: confirm } });
}

function submit() {
    fireEvent.click(screen.getByRole('button', { name: /變更密碼/ }));
}

describe('ChangePassword（自助修改密碼）', () => {
    beforeEach(() => vi.clearAllMocks());

    it('新密碼與確認不一致 → 顯示錯誤、不呼叫 API', () => {
        render(<ChangePassword />);
        fill('oldpass', 'NewPass1!', 'Different1!');
        submit();
        expect(screen.getByText('兩次輸入的新密碼不一致。')).toBeDefined();
        expect(mockChangePassword).not.toHaveBeenCalled();
    });

    it('新密碼過短 → 不呼叫 API', () => {
        render(<ChangePassword />);
        fill('oldpass', 'Ab1!', 'Ab1!');
        submit();
        expect(mockChangePassword).not.toHaveBeenCalled();
    });

    it('成功 → 呼叫 changePassword、提示成功並登出', async () => {
        mockChangePassword.mockResolvedValue({ ok: true });
        render(<ChangePassword />);
        fill('oldpass', 'NewPass1!', 'NewPass1!');
        submit();
        await waitFor(() => expect(mockChangePassword).toHaveBeenCalledWith('oldpass', 'NewPass1!'));
        await waitFor(() => expect(mockLogout).toHaveBeenCalled());
        expect(toast.success).toHaveBeenCalled();
    });

    it('目前密碼錯誤（403 AUTH_REAUTH_FAILED）→ 顯示「目前密碼不正確」、不登出', async () => {
        const { id } = ERRORS.AUTH_REAUTH_FAILED;
        mockChangePassword.mockResolvedValue(
            Response.json({ detail: { code: 'AUTH_REAUTH_FAILED', id } }, { status: 403 }),
        );
        render(<ChangePassword />);
        fill('wrong', 'NewPass1!', 'NewPass1!');
        submit();
        expect(await screen.findByText('目前密碼不正確。')).toBeDefined();
        expect(mockLogout).not.toHaveBeenCalled();
    });

    it('其他 403（例如權限）不說成密碼錯誤', async () => {
        const { id } = ERRORS.AUTH_PERMISSION_DENIED;
        mockChangePassword.mockResolvedValue(
            Response.json({ detail: { code: 'AUTH_PERMISSION_DENIED', id } }, { status: 403 }),
        );
        render(<ChangePassword />);
        fill('oldpass', 'NewPass1!', 'NewPass1!');
        submit();
        await waitFor(() => expect(mockChangePassword).toHaveBeenCalled());
        expect(await screen.findByText(i18n.t('settings.change_password.errors.generic'))).toBeDefined();
        expect(screen.queryByText('目前密碼不正確。')).toBeNull();
    });
});
