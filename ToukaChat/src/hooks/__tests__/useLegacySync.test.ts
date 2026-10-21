/** Legacy sync only reports success when the backend really succeeded. */
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('@/lib/connections', () => ({ fetchTouka: vi.fn(), getActiveProxyUrl: () => 'http://localhost:8000' }));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
import { toast } from 'sonner';
import { fetchTouka } from '@/lib/connections';
import { useLegacySync } from '../useLegacySync';

const reply = (status: number, body: unknown) =>
    ({ ok: status >= 200 && status < 300, status, json: async () => body }) as Response;
const NO_AGENTS: string[] = [];
const syncFailed = { detail: { code: 'SYNC_FAILED', id: 1200, message: 'Data synchronization failed', i18n: 'error.system.sync_failed' } };
const stats = { status: 'success', agent_id: 'DebugAgent', message_count: 1, session_count: 1, archived_files: 0, pending_files: 0 };

function route(syncReply: Response, statsReply: Response = reply(200, stats)) {
    vi.mocked(fetchTouka).mockImplementation(async (_url, path) => (String(path).endsWith('/sync') ? syncReply : statsReply));
}

beforeEach(() => { vi.clearAllMocks(); });

describe('useLegacySync', () => {
    it.each([
        ['an error status', reply(500, syncFailed)],
        ['a 200 body that is not success', reply(200, { status: 'error', message: 'Sync process failed' })],
    ])('does not show success for %s', async (_label, syncReply) => {
        route(syncReply);
        const { result } = renderHook(() => useLegacySync(NO_AGENTS));
        let outcome: boolean | undefined;
        await act(async () => { outcome = await result.current.handleSync('DebugAgent'); });
        expect(outcome).toBe(false);
        expect(toast.success).not.toHaveBeenCalled();
        expect(toast.error).toHaveBeenCalled();
    });

    it('shows success only for a real success', async () => {
        route(reply(200, { status: 'success', result: { synced: 1, errors: 0 } }));
        const { result } = renderHook(() => useLegacySync(NO_AGENTS));
        let outcome: boolean | undefined;
        await act(async () => { outcome = await result.current.handleSync('DebugAgent'); });
        expect(outcome).toBe(true);
        expect(toast.success).toHaveBeenCalledTimes(1);
        expect(toast.error).not.toHaveBeenCalled();
    });

    it('shows a stats failure instead of ignoring it', async () => {
        route(reply(200, {}), reply(500, { detail: { code: 'SYSTEM_ERROR', id: 1000, message: 'Internal System Error', i18n: 'error.system.internal' } }));
        const { result } = renderHook(() => useLegacySync(NO_AGENTS));
        await act(async () => { await result.current.fetchStats('DebugAgent'); });
        expect(toast.error).toHaveBeenCalledTimes(1);
        expect(result.current.stats).toEqual({});
    });
});
