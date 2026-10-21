import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import { VaultDashboard } from '../VaultDashboard';
import zhTW from '@/locales/zh-TW/translation.json';

// 記憶寶庫列表讀取失敗（例如記憶庫讀不到時後端回 SYSTEM_ERROR）：頁面要顯示錯誤，
// 不能顯示成空的記憶寶庫，否則人類會以為沒有記憶；同時請側欄重抓記憶提示。
const state = vi.hoisted(() => ({
    listStatus: 500,
    checkStatus: vi.fn(() => Promise.resolve()),
    bodies: {
        500: { detail: { code: 'SYSTEM_ERROR', id: 1000, message: 'Internal System Error', i18n: 'error.system.internal' } },
        403: { detail: { code: 'AUTH_PERMISSION_DENIED', id: 1102, message: 'Permission denied', i18n: 'error.auth.permission_denied' } },
    } as Record<number, unknown>,
}));

vi.mock('@/lib/connections', () => ({
    getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
    fetchTouka: vi.fn((_url: string, path: string) => {
        if (path.includes('/memory/list')) {
            return Promise.resolve(new Response(JSON.stringify(state.bodies[state.listStatus]), { status: state.listStatus }));
        }
        return Promise.resolve(
            new Response(JSON.stringify({ facts: true, vector: true, legacy: true, personal: true }), { status: 200 }),
        );
    }),
}));

vi.mock('@/lib/SystemStatusContext', () => ({
    useSystemStatus: () => ({ status: {}, checkStatus: state.checkStatus }),
}));

vi.mock('@/components/settings/tabs/LegacyMonitorTab', () => ({
    LegacyMonitorTab: () => null,
}));

const vault = (zhTW as { dashboard: { vault_dashboard: Record<string, string> } }).dashboard.vault_dashboard;
const sidebarHint = vault.list_failed.split('）')[1];

describe('VaultDashboard 列表讀取失敗', () => {
    beforeEach(() => {
        state.checkStatus.mockClear();
    });

    it('shows the failure instead of an empty vault and refreshes the sidebar notices', async () => {
        state.listStatus = 500;
        render(<VaultDashboard />);

        const failure = await screen.findByTestId('vault-list-error');
        expect(failure).toHaveAttribute('role', 'alert');
        expect(failure).toHaveTextContent('系統內部錯誤');
        expect(failure).toHaveTextContent(sidebarHint);
        expect(failure.textContent).not.toContain('dashboard.vault_dashboard');
        expect(screen.queryByText(vault.empty_state)).toBeNull();
        expect(screen.queryByText('0')).toBeNull();
        expect(state.checkStatus).toHaveBeenCalled();
    });

    it('shows a permission error as is, without sending people to the sidebar', async () => {
        state.listStatus = 403;
        render(<VaultDashboard />);

        const failure = await screen.findByTestId('vault-list-error');
        expect(failure.textContent).toBe('沒有存取此資源的權限');
        expect(screen.queryByText(vault.empty_state)).toBeNull();
        expect(state.checkStatus).not.toHaveBeenCalled();
    });
});
