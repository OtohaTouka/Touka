/** iso-c2 U2h：/health 的 system_notices（通用提示管道）——只是提示，不改確認過的系統狀態。 */
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
const state = vi.hoisted(() => ({ url: 'http://localhost:8000', token: 'synthetic-token' as string | null }));
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ isAuthenticated: true, isLoading: false }) }));
vi.mock('@/lib/connections', () => ({
    fetchTouka: vi.fn(), getActiveProxyUrl: () => state.url, getAccessToken: () => state.token,
    captureRequestScope: () => state.token ? ({ proxyOrigin: state.url, authenticatedPrincipalId: 'access:synthetic-user', authEpoch: 0 }) : null,
    isRequestScopeCurrent: () => true,
    subscribeCredentialScope: () => () => undefined,
}));
vi.mock('@/lib/time_utils', () => ({ getSystemEpoch: () => 1000, formatToTaipeiTime: () => '2026-01-01 00:00:00' }));
import { fetchTouka } from '@/lib/connections';
import { SystemStatusProvider, useSystemStatus } from '../SystemStatusContext';
const health = { status: 'degraded', timestamp: '2026-01-01', permission_config_degraded: true, permission_config_degraded_reason: 'auth_policies_unreadable' };
const memory = { system_degraded: false, degraded_reason: '', memory_tools_count: 7 };
const response = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as Response;
function Consumer() { const { status } = useSystemStatus(); return <pre data-testid="status">{JSON.stringify(status)}</pre>; }
const current = () => JSON.parse(screen.getByTestId('status').textContent!);
const mountWith = (healthBody: unknown) => {
    vi.mocked(fetchTouka).mockImplementation(async (_url, path) => response(path === '/v1/health' ? healthBody : memory));
    render(<SystemStatusProvider><Consumer /></SystemStatusProvider>);
};
const AUTH = { code: 'auth_policies_unreadable', level: 'warning' };
const AUTONOMY = { code: 'autonomy_config_unreadable', level: 'warning' };
beforeEach(() => { vi.clearAllMocks(); state.token = 'synthetic-token'; });
afterEach(cleanup);
describe('system notices from /health', () => {
    it('passes valid notices through without changing the memory state', async () => {
        mountWith({ ...health, system_notices: [AUTONOMY, AUTH] });
        await waitFor(() => expect(current().checkState).toBe('ready'));
        expect(current()).toMatchObject({ mode: 'hybrid', systemNotices: [AUTONOMY, AUTH] });
    });
    it('treats a missing field (older proxy) as no notices', async () => {
        mountWith(health);
        await waitFor(() => expect(current().checkState).toBe('ready'));
        expect(current().systemNotices).toEqual([]);
    });
    it.each(['not-a-list', [{ level: 'warning' }], [42], [{ code: '', level: 'warning' }], [null]])('ignores malformed notices %j', async notices => {
        mountWith({ ...health, system_notices: notices });
        await waitFor(() => expect(current().checkState).toBe('ready'));
        expect(current().systemNotices).toEqual([]);
    });
    it('keeps the notices before sign-in (they come from the public health check)', async () => {
        state.token = null;
        mountWith({ ...health, system_notices: [AUTH] });
        await waitFor(() => expect(current().checkState).toBe('auth_required'));
        expect(current().systemNotices).toEqual([AUTH]);
    });
});
