import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import i18n from '@/lib/i18n';
import type { fetchTouka } from '@/lib/connections';
import { toast } from 'sonner';
import { ERRORS } from '@/lib/error_codes';

let resetFailure: Response | null = null;

const mockFetch = vi.fn((...[_u, path]: Parameters<typeof fetchTouka>) => {
    if (path === '/v1/models') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: [
            { id: 'jin-soul', name: 'Jin', is_agent: true },
            { id: 'gpt-4o', name: 'GPT-4o', is_agent: false },
            { id: 'DebugAgent', name: 'DebugAgent', is_agent: true },
        ] }) });
    }
    if (path.startsWith('/v1/admin/agents/')) {
        if (resetFailure) return Promise.resolve(resetFailure);
        return Promise.resolve({ ok: true, json: () => Promise.resolve({
            agent_id: 'Jin', clean: true, stores: [{ name: 'sessions', purged: 2, remaining: 0, error: '' }],
            retained: [{ name: 'legacy', reason: '...' }],
        }) });
    }
    return Promise.resolve({ ok: false, status: 404 });
});

vi.mock('@/lib/connections', () => ({
    getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
    fetchTouka: (...args: Parameters<typeof fetchTouka>) => mockFetch(...args),
}));
vi.mock('@/lib/error_handler', async (importOriginal) => ({
    getResponseErrorCode: (await importOriginal<typeof import('@/lib/error_handler')>()).getResponseErrorCode,
    logSSOTError: vi.fn(), errorMsg: (c: string) => c, getErrorMessage: () => Promise.resolve('err'),
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { AgentDataResetCard } from '../AgentDataResetCard';

describe('AgentDataResetCard（ADR-0113）', () => {
    beforeEach(() => { vi.clearAllMocks(); resetFailure = null; });

    async function submitReset() {
        render(<AgentDataResetCard />);
        fireEvent.click(await screen.findByRole('button', { name: /重置資料/ }));
        fireEvent.change(await screen.findByPlaceholderText('jin-soul'), { target: { value: 'jin-soul' } });
        fireEvent.change(screen.getByPlaceholderText(/你的帳號密碼/), { target: { value: 'pw' } });
        fireEvent.click(screen.getByRole('button', { name: /確認重置/ }));
    }

    it('快照沒做成（SYSTEM_BACKUP_FAILED）時明講重置沒執行、不是籠統的失敗', async () => {
        const { id } = ERRORS.SYSTEM_BACKUP_FAILED;
        resetFailure = Response.json({ detail: { code: 'SYSTEM_BACKUP_FAILED', id } }, { status: 500 });
        await submitReset();
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith(i18n.t('settings.agent_reset.backup_failed')));
        expect(i18n.t('settings.agent_reset.backup_failed')).toMatch(/沒有執行/);
    });

    it('其他重置錯誤仍顯示錯誤碼對應的訊息', async () => {
        const { id } = ERRORS.AUTH_REAUTH_FAILED;
        resetFailure = Response.json({ detail: { code: 'AUTH_REAUTH_FAILED', id } }, { status: 403 });
        await submitReset();
        await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
        expect(toast.error).not.toHaveBeenCalledWith(i18n.t('settings.agent_reset.backup_failed'));
    });

    it('只列出可重置的代理人（排除非-agent 與 DebugAgent）', async () => {
        render(<AgentDataResetCard />);
        expect(await screen.findByText('Jin')).toBeDefined();
        expect(screen.queryByText('GPT-4o')).toBeNull();
        expect(screen.queryByText('DebugAgent')).toBeNull();
    });

    it('打字確認＋密碼前，確認鈕停用；齊備後以正確 body 呼叫重置端點', async () => {
        render(<AgentDataResetCard />);
        fireEvent.click(await screen.findByRole('button', { name: /重置資料/ }));
        // 對話框開啟
        expect(await screen.findByText(/重置代理人/)).toBeDefined();
        const confirmBtn = screen.getByRole('button', { name: /確認重置/ });
        expect((confirmBtn as HTMLButtonElement).disabled).toBe(true);

        // 只打 ID、無密碼 → 仍停用
        fireEvent.change(screen.getByPlaceholderText('jin-soul'), { target: { value: 'jin-soul' } });
        expect((confirmBtn as HTMLButtonElement).disabled).toBe(true);

        // 補密碼 → 啟用
        fireEvent.change(screen.getByPlaceholderText(/你的帳號密碼/), { target: { value: 'pw' } });
        expect((confirmBtn as HTMLButtonElement).disabled).toBe(false);

        fireEvent.click(confirmBtn);
        await waitFor(() => {
            const call = mockFetch.mock.calls.find(c => String(c[1]).startsWith('/v1/admin/agents/'));
            expect(call).toBeTruthy();
            if (!call) throw new Error('Expected an agent reset request');
            expect(String(call[1])).toContain('jin-soul');
            const requestBody = call[2]?.body;
            if (typeof requestBody !== 'string') throw new Error('Expected a JSON reset request body');
            const body = JSON.parse(requestBody);
            expect(body.confirm).toBe(true);
            expect(body.confirm_agent_id).toBe('jin-soul');
            expect(body.current_password).toBe('pw');
        });
    });

    it('確認畫面提醒還有備份：在哪裡、怎麼徹底刪', async () => {
        render(<AgentDataResetCard />);
        fireEvent.click(await screen.findByRole('button', { name: /重置資料/ }));
        expect(await screen.findByText(/「jin-soul」的資料還留在系統快照裡/)).toBeDefined();
        expect(screen.getByText(/Touka_AutoBackup_/)).toBeDefined();
        expect(screen.getByText(/vault\/core_memory_backups/)).toBeDefined();
        expect(screen.getByText(/建議：重置時勾選/)).toBeDefined();
        expect(screen.getByText(/刪整份快照/)).toBeDefined();
    });

    it('完成畫面也提醒還有備份', async () => {
        await submitReset();
        expect(await screen.findByText(i18n.t('settings.agent_reset.result_clean'))).toBeDefined();
        expect(screen.queryByRole('button', { name: /確認重置/ })).toBeNull();
        expect(screen.getByText(/「jin-soul」的資料還留在系統快照裡/)).toBeDefined();
        expect(screen.getByText(/所有靈魂的記憶都退回/)).toBeDefined();
    });

    it('打錯 ID → 確認鈕保持停用（防點錯列）', async () => {
        render(<AgentDataResetCard />);
        fireEvent.click(await screen.findByRole('button', { name: /重置資料/ }));
        await screen.findByText(/重置代理人/);
        fireEvent.change(screen.getByPlaceholderText('jin-soul'), { target: { value: 'jin' } }); // 不完全相符
        fireEvent.change(screen.getByPlaceholderText(/你的帳號密碼/), { target: { value: 'pw' } });
        expect((screen.getByRole('button', { name: /確認重置/ }) as HTMLButtonElement).disabled).toBe(true);
    });
});
