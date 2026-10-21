/** Confirmed capabilities, URL SSOT and late-result fences. */
import { act, render, screen, waitFor, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
const state = vi.hoisted(() => ({ url: 'http://localhost:8000', token: 'synthetic-token' as string | null,
    epoch: 0, authenticated: true, loading: false, listeners: new Set<() => void>() }));
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ isAuthenticated: state.authenticated, isLoading: state.loading }) }));
vi.mock('@/lib/connections', () => ({
    fetchTouka: vi.fn(), getActiveProxyUrl: () => state.url,
    getActiveConnection: vi.fn(() => null), getAccessToken: () => state.token,
    captureRequestScope: () => state.token ? ({ proxyOrigin: state.url, authenticatedPrincipalId: 'access:synthetic-user', authEpoch: state.epoch }) : null,
    isRequestScopeCurrent: (scope: { proxyOrigin: string; authEpoch: number } | null) =>
        !!scope && !!state.token && scope.proxyOrigin === state.url && scope.authEpoch === state.epoch,
    subscribeCredentialScope: (listener: () => void) => { state.listeners.add(listener); return () => state.listeners.delete(listener); },
}));
vi.mock('@/lib/time_utils', () => ({ getSystemEpoch: () => 1000, formatToTaipeiTime: () => '2026-01-01 00:00:00' }));
import { fetchTouka, getActiveConnection } from '@/lib/connections';
import { SystemStatusProvider, useSystemStatus } from '../SystemStatusContext';
const health = { status: 'healthy', timestamp: '2026-01-01', permission_config_degraded: false, permission_config_degraded_reason: '' };
const memory = { system_degraded: false, degraded_reason: '', memory_tools_count: 7 };
const response = (body: unknown, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body }) as Response;
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; };
function Consumer() { const { status } = useSystemStatus(); return <pre data-testid="status">{JSON.stringify(status)}</pre>; }
const mount = () => render(<SystemStatusProvider><Consumer /></SystemStatusProvider>);
const current = () => JSON.parse(screen.getByTestId('status').textContent!);
beforeEach(() => {
    vi.clearAllMocks(); state.url = 'http://localhost:8000'; state.token = 'synthetic-token'; state.epoch = 0;
    state.authenticated = true; state.loading = false; state.listeners.clear();
    vi.mocked(getActiveConnection).mockReturnValue({ id: 'proxy', enabled: true } as never);
    vi.mocked(fetchTouka).mockImplementation(async (_url, path) => response(path === '/v1/health' ? health : memory));
});
afterEach(cleanup);
describe('confirmed system capabilities', () => {
    it.each(['http://localhost:8000', ''])('uses default or same-origin URL %s without a saved connection', async url => {
        state.url = url; vi.mocked(getActiveConnection).mockReturnValue(null); mount(); await waitFor(() => expect(current().mode).toBe('hybrid'));
        expect(fetchTouka).toHaveBeenCalledWith(url, '/v1/health', expect.any(Object));
        expect(fetchTouka).toHaveBeenCalledWith(url, '/v1/memory', expect.any(Object));
    });
    it('keeps URL normalization in getActiveProxyUrl', async () => {
        vi.mocked(getActiveConnection).mockReturnValue({ id: 'proxy', url: 'http://localhost:8000/v1', enabled: true } as never);
        mount(); await waitFor(() => expect(current().mode).toBe('hybrid'));
        expect(vi.mocked(fetchTouka).mock.calls[0].slice(0, 2)).toEqual(['http://localhost:8000', '/v1/health']);
    });
    it.each(['loading', 'no-card', 'not-authenticated'])('auth not ready: %s', async kind => {
        state.loading = kind === 'loading'; state.authenticated = kind !== 'not-authenticated'; if (kind === 'no-card') state.token = null;
        mount(); await waitFor(() => expect(current().checkState).toBe('auth_required'));
        expect(current().mode).toBe('unknown'); expect(vi.mocked(fetchTouka).mock.calls.map(call => call[1])).toEqual(['/v1/health']);
    });
    it('checks memory once auth is ready without polling', async () => {
        state.token = null; mount(); await waitFor(() => expect(fetchTouka).toHaveBeenCalledTimes(1)); state.token = 'synthetic-token';
        await act(async () => { window.dispatchEvent(new Event('touka_auth_changed')); });
        await waitFor(() => expect(current().mode).toBe('hybrid')); expect(fetchTouka).toHaveBeenCalledTimes(3);
    });
    it.each([401, 403, 500])('does not call memory HTTP %s a mode', async status => {
        vi.mocked(fetchTouka).mockImplementation(async (_url, path) => response(path === '/v1/health' ? health : { detail: 'secret-sentinel' }, path === '/v1/health' ? 200 : status));
        mount(); await waitFor(() => expect(current().checkState).toBe(status === 401 ? 'auth_required' : status === 403 ? 'forbidden' : 'unavailable'));
        expect(current().mode).toBe('unknown'); expect(current().isMemoryActive).toBe(false);
        expect(screen.getByTestId('status').textContent).not.toContain('secret-sentinel');
    });
    it.each([null, {}, { ...memory, memory_tools_count: '7' }, { ...memory, system_degraded: 'false' }, { ...memory, memory_tools_count: -1 }])('rejects malformed memory %j', async body => {
        vi.mocked(fetchTouka).mockImplementation(async (_url, path) => response(path === '/v1/health' ? health : body));
        mount(); await waitFor(() => expect(current().checkState).toBe('unavailable'));
        expect(current().mode).toBe('unknown'); expect(current().isMemoryActive).toBe(false);
    });
    it.each(['json', 'network'])('classifies %s failures safely', async kind => {
        vi.mocked(fetchTouka).mockImplementation(async (_url, path) => {
            if (path === '/v1/health') return response(health);
            if (kind === 'network') throw new Error('secret-sentinel');
            return { ok: true, status: 200, json: async () => { throw new Error('secret-sentinel'); } } as unknown as Response;
        });
        mount(); await waitFor(() => expect(current().checkState).toBe('unavailable'));
        expect(current().mode).toBe('unknown'); expect(screen.getByTestId('status').textContent).not.toContain('secret-sentinel');
    });
    it('validates health body before claiming a reachable Proxy', async () => {
        vi.mocked(fetchTouka).mockResolvedValue(response({ status: 'synthetic-invalid' })); mount();
        await waitFor(() => expect(current().checkState).toBe('unavailable'));
        expect(current().isConnected).toBe(false); expect(fetchTouka).toHaveBeenCalledTimes(1);
    });
    it('only explicit Memory degradation confirms Standalone and its warning', async () => {
        vi.mocked(fetchTouka).mockImplementation(async (_url, path) => response(path === '/v1/health' ? health : { ...memory, system_degraded: true, degraded_reason: 'Memory System module not found.' }));
        mount(); await waitFor(() => expect(current().checkState).toBe('degraded'));
        expect(current()).toMatchObject({ mode: 'standalone', systemDegraded: true, isMemoryActive: false });
    });
    it('zero tools is unconfirmed capability', async () => {
        vi.mocked(fetchTouka).mockImplementation(async (_url, path) => response(path === '/v1/health' ? health : { ...memory, memory_tools_count: 0 }));
        mount(); await waitFor(() => expect(current().checkState).toBe('unavailable')); expect(current().mode).toBe('unknown');
    });
});
describe('status generation fence', () => {
    it.each(['origin', 'credential', 'logout', 'new-check'])('discards late JSON after %s', async change => {
        const body = deferred<unknown>(); let held = false;
        // Reach the old status path as well, so this specifically exercises the race.
        vi.mocked(getActiveConnection).mockReturnValue({ id: 'proxy', enabled: true } as never);
        vi.mocked(fetchTouka).mockImplementation(async (_url, path) => {
            if (path === '/v1/health') return response(health);
            if (!held) { held = true; return { ok: true, status: 200, json: () => body.promise } as Response; }
            return response({}, 403);
        });
        mount(); await waitFor(() => expect(held).toBe(true)); const firstSignal = vi.mocked(fetchTouka).mock.calls[1][2]?.signal;
        await act(async () => {
            if (change === 'origin') state.url = 'https://other.invalid';
            if (change === 'credential') state.epoch++;
            if (change === 'logout') { state.token = null; state.authenticated = false; state.epoch++; }
            if (change === 'credential' || change === 'logout') state.listeners.forEach(listener => listener());
            else window.dispatchEvent(new Event('touka_connections_updated'));
        });
        await act(async () => { body.resolve(memory); });
        expect(current().isMemoryActive).toBe(false);
        await waitFor(() => expect(current().checkState).toBe(change === 'logout' ? 'auth_required' : 'forbidden'));
        expect(current().mode).toBe('unknown'); expect(firstSignal?.aborted).toBe(true);
    });
    it('unmount aborts pending requests', async () => {
        vi.mocked(getActiveConnection).mockReturnValue({ id: 'proxy', enabled: true } as never);
        vi.mocked(fetchTouka).mockReturnValue(new Promise(() => {}));
        const view = mount(); await waitFor(() => expect(fetchTouka).toHaveBeenCalledTimes(1));
        const signal = vi.mocked(fetchTouka).mock.calls[0][2]?.signal; view.unmount();
        expect(signal?.aborted).toBe(true); expect(state.listeners.size).toBe(0);
    });
});
