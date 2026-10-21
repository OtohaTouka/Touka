import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@/lib/i18n';
import type { fetchTouka } from '@/lib/connections';

const PREVIEW = {
    dry_run: true, mode: 'merge', applied: ['autonomy', 'models'],
    warnings: [], skipped_secrets: [], requires_restart: ['mcp_servers'], pre_import_backup: null,
};
const APPLIED = { ...PREVIEW, dry_run: false, pre_import_backup: { message: 'ok' } };

function readRequestBody(options: RequestInit | undefined) {
    if (typeof options?.body !== 'string') {
        throw new Error('Expected a JSON request body');
    }
    return JSON.parse(options.body);
}

const mockFetchTouka = vi.fn((...[_url, path, opts]: Parameters<typeof fetchTouka>) => {
    if (path === '/v1/system/settings/export') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ export_kind: 'settings_snapshot', sections: {} }) });
    }
    if (path === '/v1/system/settings/import') {
        const body = readRequestBody(opts);
        return Promise.resolve({ ok: true, json: () => Promise.resolve(body.dry_run ? PREVIEW : APPLIED) });
    }
    return Promise.resolve({ ok: false, status: 404 });
});

vi.mock('@/lib/connections', () => ({
    getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
    fetchTouka: (...args: Parameters<typeof fetchTouka>) => mockFetchTouka(...args),
}));
vi.mock('@/lib/error_handler', () => ({
    logSSOTError: vi.fn(),
    errorMsg: (c: string) => c,
    getErrorMessage: () => Promise.resolve('err'),
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { SettingsSnapshotCard } from '../SettingsSnapshotCard';

function selectFile(container: HTMLElement, data: object) {
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File([JSON.stringify(data)], 'snap.json', { type: 'application/json' });
    fireEvent.change(input, { target: { files: [file] } });
}

describe('SettingsSnapshotCard（ADR-0112 Phase 2）', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        URL.createObjectURL = vi.fn(() => 'blob:x');
        URL.revokeObjectURL = vi.fn();
        vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    });

    it('匯出 → 以 encrypt:false 呼叫匯出端點', async () => {
        render(<SettingsSnapshotCard />);
        fireEvent.click(screen.getByRole('button', { name: '匯出' }));
        await waitFor(() => expect(mockFetchTouka).toHaveBeenCalledWith(
            'http://localhost:8000', '/v1/system/settings/export', expect.objectContaining({ method: 'POST' }),
        ));
        const call = mockFetchTouka.mock.calls.find(c => c[1] === '/v1/system/settings/export');
        const body = readRequestBody(call?.[2]);
        expect(body.encrypt).toBe(false);
    });

    it('選檔 → 自動乾跑預覽並顯示涵蓋設定區', async () => {
        const { container } = render(<SettingsSnapshotCard />);
        selectFile(container, { export_kind: 'settings_snapshot', sections: {} });
        expect(await screen.findByText(/涵蓋設定區/)).toBeDefined();
        // 預覽用 dry_run:true 呼叫
        const call = mockFetchTouka.mock.calls.find(c => c[1] === '/v1/system/settings/import');
        expect(readRequestBody(call?.[2]).dry_run).toBe(true);
    });

    it('切換覆蓋模式 → 顯示破壞性警告', async () => {
        const { container } = render(<SettingsSnapshotCard />);
        selectFile(container, { export_kind: 'settings_snapshot', sections: {} });
        await screen.findByText(/涵蓋設定區/);
        fireEvent.click(screen.getByRole('button', { name: '覆蓋' }));
        expect(await screen.findByText(/覆蓋模式會以快照內容整區取代/)).toBeDefined();
    });

    it('套用 → 密碼確認後以 dry_run:false 呼叫匯入端點', async () => {
        const { container } = render(<SettingsSnapshotCard />);
        selectFile(container, { export_kind: 'settings_snapshot', sections: {} });
        await screen.findByText(/涵蓋設定區/);
        fireEvent.click(screen.getByRole('button', { name: /套用設定/ }));
        const pwd = await screen.findByPlaceholderText(/你的帳號密碼/);
        fireEvent.change(pwd, { target: { value: 'secret' } });
        fireEvent.click(screen.getByRole('button', { name: /確認套用/ }));
        await waitFor(() => {
            const applyCall = mockFetchTouka.mock.calls.find(
                c => c[1] === '/v1/system/settings/import' && readRequestBody(c[2]).dry_run === false,
            );
            expect(applyCall).toBeTruthy();
            expect(readRequestBody(applyCall?.[2]).current_password).toBe('secret');
        });
    });
});
