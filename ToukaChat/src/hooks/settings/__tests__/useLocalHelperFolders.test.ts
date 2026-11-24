/**
 * ADR-0330：設定頁讀小幫手回報的開放名單——只在需要時讀、不是配對的人就停、只放記憶體。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

vi.mock('@/lib/connections', () => ({ fetchTouka: vi.fn(), getActiveProxyUrl: () => 'http://x' }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

import { fetchTouka } from '@/lib/connections';
import { LOCAL_HELPER_REFRESH_MS } from '../useLocalHelper';
import { useLocalHelperFolders } from '../useLocalHelperFolders';

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;
const LISTED = {
    state: 'ok',
    exposed: false,
    folders: [{ name: 'Notes', path: 'D:\\Example\\Notes', writable: false, usable: true }],
    omitted: 0,
};

function reply(body: unknown, ok = true, status = 200) {
    return Promise.resolve({ ok, status, json: async () => body });
}

describe('useLocalHelperFolders（ADR-0330）', () => {
    beforeEach(() => vi.clearAllMocks());
    afterEach(() => vi.useRealTimers());

    it('需要時才讀，讀到的清單照原樣給畫面', async () => {
        mockFetch.mockImplementation(() => reply(LISTED));
        const { result } = renderHook(() => useLocalHelperFolders(true));
        await waitFor(() => expect(result.current.kind).toBe('loaded'));
        expect(result.current).toEqual({ kind: 'loaded', list: LISTED });
        expect(mockFetch).toHaveBeenCalledWith('http://x', '/v1/local-bridge/folders');
    });

    it('沒配對或小幫手沒開時不讀', async () => {
        mockFetch.mockImplementation(() => reply(LISTED));
        const { result } = renderHook(() => useLocalHelperFolders(false));
        await act(async () => {
            await Promise.resolve();
        });
        expect(mockFetch).not.toHaveBeenCalled();
        expect(result.current.kind).toBe('loading');
    });

    it('不是配對的人：說清楚，之後也不再一直讀', async () => {
        vi.useFakeTimers();
        mockFetch.mockImplementation(() => reply({}, false, 403));
        const { result } = renderHook(() => useLocalHelperFolders(true));
        await act(async () => {
            await vi.advanceTimersByTimeAsync(0);
        });
        expect(result.current.kind).toBe('denied');
        const calls = mockFetch.mock.calls.length;
        await act(async () => {
            await vi.advanceTimersByTimeAsync(LOCAL_HELPER_REFRESH_MS * 3);
        });
        expect(mockFetch.mock.calls.length).toBe(calls);
    });

    it('拿掉或改成只能讀之後（重讀編號變了）馬上再讀一次（ADR-0330 2b）', async () => {
        mockFetch.mockImplementation(() => reply(LISTED));
        const { rerender } = renderHook(({ refresh }) => useLocalHelperFolders(true, refresh), {
            initialProps: { refresh: 0 },
        });
        await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(1));
        rerender({ refresh: 1 });
        await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(2));
    });

    it('讀不到時標失敗', async () => {
        mockFetch.mockImplementation(() => Promise.reject(new Error('down')));
        const { result } = renderHook(() => useLocalHelperFolders(true));
        await waitFor(() => expect(result.current.kind).toBe('failed'));
    });

    it('定時更新，離開後停止', async () => {
        vi.useFakeTimers();
        mockFetch.mockImplementation(() => reply(LISTED));
        const hook = renderHook(() => useLocalHelperFolders(true));
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
