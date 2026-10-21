/** Memory notices are optional extras: they never change the confirmed system state. */
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
const state = vi.hoisted(() => ({ url: 'http://localhost:8000', token: 'synthetic-token' }));
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ isAuthenticated: true, isLoading: false }) }));
vi.mock('@/lib/connections', () => ({
    fetchTouka: vi.fn(), getActiveProxyUrl: () => state.url, getAccessToken: () => state.token,
    captureRequestScope: () => ({ proxyOrigin: state.url, authenticatedPrincipalId: 'access:synthetic-user', authEpoch: 0 }),
    isRequestScopeCurrent: () => true,
    subscribeCredentialScope: () => () => undefined,
}));
vi.mock('@/lib/time_utils', () => ({ getSystemEpoch: () => 1000, formatToTaipeiTime: () => '2026-01-01 00:00:00' }));
import { fetchTouka } from '@/lib/connections';
import { SystemStatusProvider, useSystemStatus } from '../SystemStatusContext';
const health = { status: 'healthy', timestamp: '2026-01-01', permission_config_degraded: false, permission_config_degraded_reason: '' };
const memory = { system_degraded: false, degraded_reason: '', memory_tools_count: 7 };
const response = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as Response;
function Consumer() { const { status } = useSystemStatus(); return <pre data-testid="status">{JSON.stringify(status)}</pre>; }
const current = () => JSON.parse(screen.getByTestId('status').textContent!);
const mountWith = (body: unknown) => {
    vi.mocked(fetchTouka).mockImplementation(async (_url, path) => response(path === '/v1/health' ? health : body));
    render(<SystemStatusProvider><Consumer /></SystemStatusProvider>);
};
beforeEach(() => { vi.clearAllMocks(); });
afterEach(cleanup);
describe('memory notices', () => {
    it('passes valid notices through', async () => {
        mountWith({ ...memory, memory_notices: [{ code: 'unmigrated_legacy_facts', level: 'info', count: 2 }] });
        await waitFor(() => expect(current().checkState).toBe('ready'));
        expect(current().memoryNotices).toEqual([{ code: 'unmigrated_legacy_facts', level: 'info', count: 2 }]);
    });
    it('treats a missing field as no notices', async () => {
        mountWith(memory);
        await waitFor(() => expect(current().checkState).toBe('ready'));
        expect(current().memoryNotices).toEqual([]);
    });
    it.each(['not-a-list', [{ level: 'info' }], [42], [{ code: '', level: 'info' }], [null]])('ignores malformed notices %j without changing the state', async notices => {
        mountWith({ ...memory, memory_notices: notices });
        await waitFor(() => expect(current().checkState).toBe('ready'));
        expect(current()).toMatchObject({ mode: 'hybrid', isMemoryActive: true, memoryNotices: [] });
    });
    it('shows an unknown level as information and drops an invalid count', async () => {
        mountWith({ ...memory, memory_notices: [{ code: 'future_code', level: 'urgent', count: -1 }] });
        await waitFor(() => expect(current().checkState).toBe('ready'));
        expect(current().memoryNotices).toEqual([{ code: 'future_code', level: 'info' }]);
    });
    it('keeps notices while the memory system is degraded', async () => {
        mountWith({ ...memory, system_degraded: true, memory_notices: [{ code: 'future_code', level: 'warning' }] });
        await waitFor(() => expect(current().checkState).toBe('degraded'));
        expect(current().memoryNotices).toEqual([{ code: 'future_code', level: 'warning' }]);
    });
});
