/**
 * ADR-0311：本機小幫手設定頁的資料層——狀態、配對碼只在記憶體、解除配對。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

vi.mock('@/lib/connections', () => ({ fetchTouka: vi.fn(), getActiveProxyUrl: () => 'http://x' }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

import { fetchTouka } from '@/lib/connections';
import { LOCAL_HELPER_REFRESH_MS, useLocalHelper } from '../useLocalHelper';

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;
const UNPAIRED = { paired: false, online: false, helper_version: null };
const ONLINE = { paired: true, online: true, helper_version: '1.0' };

function reply(body: unknown, ok = true, status = 200) {
    return Promise.resolve({ ok, status, json: async () => body });
}

function route(handlers: { status?: unknown; post?: () => Promise<unknown>; del?: () => Promise<unknown> }) {
    mockFetch.mockImplementation((_url: string, _path: string, init?: RequestInit) => {
        if (init?.method === 'POST') return handlers.post ? handlers.post() : reply({ code: 'CODE' });
        if (init?.method === 'DELETE') return handlers.del ? handlers.del() : reply({ ok: true });
        return reply(handlers.status ?? UNPAIRED);
    });
}

async function loaded() {
    const hook = renderHook(() => useLocalHelper());
    await waitFor(() => expect(hook.result.current.status).not.toBeNull());
    return hook;
}

describe('useLocalHelper（ADR-0311）', () => {
    beforeEach(() => vi.clearAllMocks());
    afterEach(() => vi.useRealTimers());

    it('一開始讀狀態', async () => {
        route({ status: ONLINE });
        const { result } = await loaded();
        expect(result.current.status).toEqual(ONLINE);
        expect(result.current.failed).toBe(false);
        expect(mockFetch).toHaveBeenCalledWith('http://x', '/v1/local-bridge/status');
    });

    it('讀不到狀態時標失敗，不當成沒配對', async () => {
        mockFetch.mockImplementation(() => reply({}, false, 403));
        const { result } = renderHook(() => useLocalHelper());
        await waitFor(() => expect(result.current.failed).toBe(true));
        expect(result.current.status).toBeNull();
    });

    it('連線失敗也標失敗', async () => {
        mockFetch.mockImplementation(() => Promise.reject(new Error('down')));
        const { result } = renderHook(() => useLocalHelper());
        await waitFor(() => expect(result.current.failed).toBe(true));
    });

    it('產生配對碼後放在記憶體並更新狀態', async () => {
        route({ status: UNPAIRED });
        const { result } = await loaded();
        route({ status: ONLINE });
        await act(async () => {
            await result.current.createPairing();
        });
        expect(result.current.code).toBe('CODE');
        expect(result.current.status).toEqual(ONLINE);
        expect(mockFetch).toHaveBeenCalledWith('http://x', '/v1/local-bridge/pairing', { method: 'POST' });
    });

    it('產生失敗時回報，沒有配對碼', async () => {
        route({ post: () => reply({}, false, 500) });
        const { result } = await loaded();
        let ok = true;
        await act(async () => {
            ok = await result.current.createPairing();
        });
        expect(ok).toBe(false);
        expect(result.current.code).toBeNull();
    });

    it('回應沒有配對碼時算失敗', async () => {
        route({ post: () => reply({}) });
        const { result } = await loaded();
        let ok = true;
        await act(async () => {
            ok = await result.current.createPairing();
        });
        expect(ok).toBe(false);
        expect(result.current.code).toBeNull();
    });

    it('解除配對會清掉畫面上的配對碼', async () => {
        route({ status: ONLINE });
        const { result } = await loaded();
        await act(async () => {
            await result.current.createPairing();
        });
        route({ status: UNPAIRED });
        await act(async () => {
            await result.current.revokePairing();
        });
        expect(result.current.code).toBeNull();
        expect(result.current.status).toEqual(UNPAIRED);
        expect(mockFetch).toHaveBeenCalledWith('http://x', '/v1/local-bridge/pairing', { method: 'DELETE' });
    });

    it('解除失敗時保留配對碼並回報', async () => {
        route({ status: ONLINE, del: () => reply({}, false, 500) });
        const { result } = await loaded();
        await act(async () => {
            await result.current.createPairing();
        });
        let ok = true;
        await act(async () => {
            ok = await result.current.revokePairing();
        });
        expect(ok).toBe(false);
        expect(result.current.code).toBe('CODE');
    });

    it('定時更新狀態，離開後停止', async () => {
        vi.useFakeTimers();
        route({ status: UNPAIRED });
        const hook = renderHook(() => useLocalHelper());
        await act(async () => {
            await vi.advanceTimersByTimeAsync(0);
        });
        const before = mockFetch.mock.calls.length;
        await act(async () => {
            await vi.advanceTimersByTimeAsync(LOCAL_HELPER_REFRESH_MS);
        });
        expect(mockFetch.mock.calls.length).toBe(before + 1);
        hook.unmount();
        await act(async () => {
            await vi.advanceTimersByTimeAsync(LOCAL_HELPER_REFRESH_MS * 2);
        });
        expect(mockFetch.mock.calls.length).toBe(before + 1);
    });
});
