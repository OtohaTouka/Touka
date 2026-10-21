import React from 'react';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const state = vi.hoisted(() => ({ storage: new Map<string, unknown>(), push: vi.fn() }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: state.push }) }));
vi.mock('@/hooks/useLocalStorage', () => ({
    getLocalStorageItem: (key: string, fallback: unknown) => state.storage.get(key) ?? fallback,
    setLocalStorageItem: (key: string, value: unknown) => state.storage.set(key, value),
}));
vi.mock('sonner', () => ({ toast: { warning: vi.fn() } }));
import { AuthProvider, useAuth } from '../AuthContext';
import { clearAccessToken, getAccessToken, getCredentialGeneration, refreshAccessToken, setAccessToken } from '@/lib/connections';
const wire = vi.fn();
const token = (id: string) => `e30.${btoa(JSON.stringify({ sub: id, type: 'access', exp: 9999999999 }))}.synthetic`;
const profile = (id: string) => ({ id, email: `${id}@example.test`, role: 'normal', status: 'active', owned_agents: [],
    display_name: null, avatar_url: null, bio: null, refresh_token_exp: null, last_active_at: null, created_at: null });
const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const loginReply = (id: string) => reply({ access_token: token(id), token_type: 'bearer' });
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; };
const wrapper = ({ children }: { children: React.ReactNode }) => <AuthProvider>{children}</AuthProvider>;
const baseWire = async (url: string, options: RequestInit) => {
    if (url.endsWith('/auth/refresh')) return reply({}, 401);
    if (url.endsWith('/auth/me')) return reply(profile(new Headers(options.headers).get('Authorization') === `Bearer ${token('B')}` ? 'B' : 'A'));
    return reply({ status: 'success' });
};
async function ready() {
    const view = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(view.result.current.isLoading).toBe(false)); return view;
}
beforeEach(() => {
    vi.clearAllMocks(); state.storage.clear(); clearAccessToken();
    state.storage.set('touka_connections', [{ id: 'proxy', provider: 'proxy', enabled: true, url: 'https://a.invalid' }]);
    state.storage.set('touka_active_connection_id', 'proxy');
    wire.mockImplementation(baseWire); vi.stubGlobal('fetch', wire);
});
afterEach(() => { cleanup(); clearAccessToken(); vi.unstubAllGlobals(); });

describe('real login and credential operation fence', () => {
    it('same-origin connection settings updates preserve authentication', async () => {
        wire.mockImplementation((url, options) => url.endsWith('/auth/login') ? Promise.resolve(loginReply('A')) : baseWire(url, options));
        const { result } = await ready();
        await act(async () => { await result.current.login('A@example.test', 'synthetic-password'); });
        const epoch = getCredentialGeneration();
        await act(async () => {
            state.storage.set('touka_connections', [{ id: 'proxy', provider: 'proxy', enabled: true, url: 'https://a.invalid', requestTimeoutMs: 1234 }]);
            window.dispatchEvent(new Event('touka_connections_updated'));
        });
        expect(getAccessToken()).toBe(token('A'));
        expect(result.current.isAuthenticated).toBe(true);
        expect(result.current.user?.id).toBe('A');
        expect(getCredentialGeneration()).toBe(epoch);
    });
    it('profile refresh uses the renewed same-principal token at the actual wire', async () => {
        wire.mockImplementation((url, options) => url.endsWith('/auth/login') ? Promise.resolve(loginReply('A')) : baseWire(url, options));
        const { result } = await ready();
        await act(async () => { await result.current.login('A@example.test', 'synthetic-password'); });
        const renewed = `${token('A')}renewed`;
        const epoch = getCredentialGeneration();
        wire.mockImplementation((url, options) => url.endsWith('/auth/refresh') ?
            Promise.resolve(reply({ access_token: renewed, token_type: 'bearer' })) :
            url.endsWith('/auth/me') ? Promise.resolve(reply({ ...profile('A'), display_name: 'Updated' })) : baseWire(url, options));
        await act(async () => { await refreshAccessToken('https://a.invalid'); });
        wire.mockClear();
        await act(async () => { await result.current.refreshUser(); });
        expect(wire.mock.calls.filter(call => call[0].endsWith('/auth/me'))).toHaveLength(1);
        expect(new Headers(wire.mock.calls[0][1].headers).get('Authorization')).toBe(`Bearer ${renewed}`);
        expect(result.current.user?.display_name).toBe('Updated');
        expect(result.current.isAuthenticated).toBe(true);
        expect(result.current.accessToken).toBe(renewed);
        expect(getCredentialGeneration()).toBe(epoch);
    });
    it.each(['origin', 'credential', 'logout'])('late login cannot restore A after %s', async change => {
        const pending = deferred<Response>();
        wire.mockImplementation((url, options) => url.endsWith('/auth/login') ? pending.promise : baseWire(url, options));
        const { result } = await ready(); let login!: Promise<unknown>;
        await act(async () => { login = result.current.login('A@example.test', 'synthetic-password').catch(error => error); });
        await act(async () => {
            if (change === 'origin') {
                state.storage.set('touka_connections', [{ id: 'proxy', provider: 'proxy', enabled: true, url: 'https://b.invalid' }]);
                window.dispatchEvent(new Event('touka_connections_updated'));
            } else if (change === 'credential') setAccessToken(token('B'));
            else await result.current.logout();
        });
        await act(async () => { pending.resolve(loginReply('A')); await login; });
        expect(getAccessToken()).toBe(change === 'credential' ? token('B') : null);
        expect(result.current.user?.id).not.toBe('A');
        expect(result.current.isAuthenticated).toBe(false);
        expect(wire.mock.calls.filter(call => call[0].endsWith('/auth/me'))).toHaveLength(0);
    });
    it('later login wins while an earlier response is pending', async () => {
        const old = deferred<Response>();
        wire.mockImplementation((url, options) => url.endsWith('/auth/login') ?
            (JSON.parse(options.body).email.startsWith('A') ? old.promise : Promise.resolve(loginReply('B'))) : baseWire(url, options));
        const { result } = await ready(); let earlier!: Promise<unknown>;
        await act(async () => { earlier = result.current.login('A@example.test', 'synthetic-password').catch(error => error); });
        await act(async () => { await result.current.login('B@example.test', 'synthetic-password'); });
        await act(async () => { old.resolve(loginReply('A')); await earlier; });
        expect(getAccessToken()).toBe(token('B')); expect(result.current.user?.id).toBe('B');
    });
    it('late profile JSON cannot publish A or clear a new B credential', async () => {
        const body = deferred<unknown>();
        wire.mockImplementation((url, options) => {
            if (url.endsWith('/auth/login')) return Promise.resolve(loginReply('A'));
            if (url.endsWith('/auth/me')) return Promise.resolve({ ok: true, status: 200, headers: new Headers(), json: () => body.promise });
            return baseWire(url, options);
        });
        const { result } = await ready(); let login!: Promise<unknown>;
        await act(async () => { login = result.current.login('A@example.test', 'synthetic-password').catch(error => error); });
        await waitFor(() => expect(wire.mock.calls.some(call => call[0].endsWith('/auth/me'))).toBe(true));
        await act(async () => { setAccessToken(token('B')); body.resolve(profile('A')); await login; });
        expect(getAccessToken()).toBe(token('B')); expect(result.current.user).toBeNull(); expect(result.current.isAuthenticated).toBe(false);
    });
    it('initial refresh cannot overwrite a completed explicit login', async () => {
        const refresh = deferred<Response>();
        wire.mockImplementation((url, options) => url.endsWith('/auth/refresh') ? refresh.promise :
            url.endsWith('/auth/login') ? Promise.resolve(loginReply('B')) : baseWire(url, options));
        const { result } = renderHook(() => useAuth(), { wrapper });
        await act(async () => { await result.current.login('B@example.test', 'synthetic-password'); });
        await act(async () => { refresh.resolve(loginReply('A')); });
        await waitFor(() => expect(result.current.isLoading).toBe(false));
        expect(getAccessToken()).toBe(token('B')); expect(result.current.user?.id).toBe('B');
    });
    it.each([{}, { ...profile('A'), owned_agents: 'secret-sentinel' }, { ...profile('A'), id: 'B' }])('rejects malformed or mismatched profile %j', async body => {
        wire.mockImplementation((url, options) => url.endsWith('/auth/login') ? Promise.resolve(loginReply('A')) :
            url.endsWith('/auth/me') ? Promise.resolve(reply(body)) : baseWire(url, options));
        const { result } = await ready(); let error: unknown;
        await act(async () => { error = await result.current.login('A@example.test', 'synthetic-password').catch(value => value); });
        expect(result.current.isAuthenticated).toBe(false); expect(getAccessToken()).toBeNull();
        expect(error).toMatchObject({ code: 'AUTH_PROFILE_FETCH_FAILED' });
        expect(JSON.stringify(error)).not.toContain('secret-sentinel');
    });
    it('profile HTTP failure has a distinct safe classification', async () => {
        wire.mockImplementation((url, options) => url.endsWith('/auth/login') ? Promise.resolve(loginReply('A')) :
            url.endsWith('/auth/me') ? Promise.resolve(reply({ detail: 'secret-sentinel' }, 500)) : baseWire(url, options));
        const { result } = await ready(); let error: unknown;
        await act(async () => { error = await result.current.login('A@example.test', 'synthetic-password').catch(value => value); });
        expect(result.current.isAuthenticated).toBe(false); expect(getAccessToken()).toBeNull();
        expect(error).toMatchObject({ code: 'AUTH_PROFILE_FETCH_FAILED' });
    });
});
