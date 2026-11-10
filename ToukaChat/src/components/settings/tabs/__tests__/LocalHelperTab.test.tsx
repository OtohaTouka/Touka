import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';
import '@/lib/i18n';

const helper = {
    status: { paired: false, online: false, helper_version: null } as
        | { paired: boolean; online: boolean; helper_version: string | null }
        | null,
    failed: false,
    code: null as string | null,
    busy: false,
    createPairing: vi.fn(async () => true),
    revokePairing: vi.fn(async () => true),
};
vi.mock('@/hooks/settings/useLocalHelper', () => ({ useLocalHelper: () => helper }));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { toast } from 'sonner';
import { LocalHelperTab } from '../LocalHelperTab';

describe('LocalHelperTab（ADR-0311）', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        helper.status = { paired: false, online: false, helper_version: null };
        helper.failed = false;
        helper.code = null;
        helper.busy = false;
        helper.createPairing.mockResolvedValue(true);
        helper.revokePairing.mockResolvedValue(true);
    });

    it('沒配對：可以產生配對碼，沒有解除的按鈕', () => {
        render(<LocalHelperTab />);
        expect(screen.getByText('還沒有配對')).toBeDefined();
        expect(screen.queryByRole('button', { name: '解除配對' })).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: '產生配對碼' }));
        expect(helper.createPairing).toHaveBeenCalledOnce();
    });

    it('已配對但小幫手沒開', () => {
        helper.status = { paired: true, online: false, helper_version: null };
        render(<LocalHelperTab />);
        expect(screen.getByText('已配對，小幫手沒有開著')).toBeDefined();
    });

    it('已連線時顯示版本', () => {
        helper.status = { paired: true, online: true, helper_version: '1.2' };
        render(<LocalHelperTab />);
        expect(screen.getByText('已連線（小幫手版本 1.2）')).toBeDefined();
    });

    it('讀不到狀態時照實說，不顯示操作', () => {
        helper.status = null;
        helper.failed = true;
        render(<LocalHelperTab />);
        expect(screen.getByText('讀不到小幫手的狀態，請稍後再試。')).toBeDefined();
        expect(screen.queryByRole('button', { name: '產生配對碼' })).toBeNull();
    });

    it('顯示配對碼並可複製', async () => {
        helper.code = 'SECRET-CODE';
        const writeText = vi.fn(async () => undefined);
        Object.assign(navigator, { clipboard: { writeText } });
        render(<LocalHelperTab />);
        expect(screen.getByText('SECRET-CODE')).toBeDefined();
        fireEvent.click(screen.getByRole('button', { name: '複製' }));
        await waitFor(() => expect(writeText).toHaveBeenCalledWith('SECRET-CODE'));
        await waitFor(() => expect(toast.success).toHaveBeenCalled());
    });

    it('換新配對碼要先確認', () => {
        helper.status = { paired: true, online: true, helper_version: '1' };
        render(<LocalHelperTab />);
        fireEvent.click(screen.getByRole('button', { name: '換一組配對碼' }));
        expect(helper.createPairing).not.toHaveBeenCalled();
        expect(screen.getByText(/舊的配對碼馬上失效/)).toBeDefined();
        fireEvent.click(screen.getByRole('button', { name: '確定' }));
        expect(helper.createPairing).toHaveBeenCalledOnce();
    });

    it('解除配對要先確認，取消就不動', () => {
        helper.status = { paired: true, online: true, helper_version: '1' };
        render(<LocalHelperTab />);
        fireEvent.click(screen.getByRole('button', { name: '解除配對' }));
        expect(helper.revokePairing).not.toHaveBeenCalled();
        fireEvent.click(screen.getByRole('button', { name: '取消' }));
        expect(helper.revokePairing).not.toHaveBeenCalled();
        fireEvent.click(screen.getByRole('button', { name: '解除配對' }));
        fireEvent.click(screen.getByRole('button', { name: '確定' }));
        expect(helper.revokePairing).toHaveBeenCalledOnce();
    });

    it('操作失敗時提示', async () => {
        helper.createPairing.mockResolvedValue(false);
        render(<LocalHelperTab />);
        fireEvent.click(screen.getByRole('button', { name: '產生配對碼' }));
        await waitFor(() => expect(toast.error).toHaveBeenCalled());
    });

    it('處理中時按鈕停用', () => {
        helper.busy = true;
        render(<LocalHelperTab />);
        expect(screen.getByRole('button', { name: '產生配對碼' }).hasAttribute('disabled')).toBe(true);
    });
});
