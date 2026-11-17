/**
 * ADR-0320：從設定頁請小幫手開放資料夾的資料層。先看 HTTP 狀態，再看代碼。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

vi.mock('@/lib/connections', () => ({ fetchTouka: vi.fn(), getActiveProxyUrl: () => 'http://x' }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

import { fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';
import { OPEN_REQUEST_TIMEOUT_MS, cleanFolderPath, useLocalOpenRequest } from '../useLocalOpenRequest';

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;
const OPENED = { status: 'opened', code: null, name: '相片', writable: false };

function reply(body: unknown, status = 200) {
    return Promise.resolve({ ok: status >= 200 && status < 300, status, json: async () => body });
}

describe('cleanFolderPath（ADR-0320）', () => {
    it.each([
        ['  D:\\相片  ', 'D:\\相片'],
        ['"D:\\相片"', 'D:\\相片'],
        [' "D:\\相片" ', 'D:\\相片'],
        ['"D:\\相片', '"D:\\相片'],
        ['""', ''],
    ])('%s → %s', (raw, expected) => {
        expect(cleanFolderPath(raw)).toBe(expected);
    });
});

describe('useLocalOpenRequest（ADR-0320）', () => {
    beforeEach(() => vi.clearAllMocks());
    afterEach(() => vi.useRealTimers());

    it('送出 JSON 到開放請求的端點，帶自己的中止訊號', async () => {
        mockFetch.mockImplementation(() => reply(OPENED));
        const { result } = renderHook(() => useLocalOpenRequest());
        await act(async () => {
            await result.current.requestOpen(' "D:\\相片" ');
        });
        expect(mockFetch).toHaveBeenCalledOnce();
        const [url, path, init] = mockFetch.mock.calls[0];
        expect(url).toBe('http://x');
        expect(path).toBe('/v1/local-bridge/open-requests');
        expect(init.method).toBe('POST');
        expect(JSON.parse(init.body)).toEqual({ path: 'D:\\相片' });
        expect(init.headers).toEqual({ 'Content-Type': 'application/json' });
        expect(init.signal).toBeInstanceOf(AbortSignal);
        expect(result.current.outcome).toEqual({ kind: 'result', result: OPENED });
        expect(result.current.pending).toBe(false);
    });

    it('空的路徑不送', async () => {
        const { result } = renderHook(() => useLocalOpenRequest());
        await act(async () => {
            await result.current.requestOpen('  ""  ');
        });
        expect(mockFetch).not.toHaveBeenCalled();
        expect(result.current.outcome).toBeNull();
    });

    it('等待中標成進行中', async () => {
        let release: (value: unknown) => void = () => undefined;
        mockFetch.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
        const { result } = renderHook(() => useLocalOpenRequest());
        let done: Promise<void> = Promise.resolve();
        act(() => {
            done = result.current.requestOpen('D:\\相片');
        });
        await waitFor(() => expect(result.current.pending).toBe(true));
        await act(async () => {
            release({ ok: true, status: 200, json: async () => OPENED });
            await done;
        });
        expect(result.current.pending).toBe(false);
    });

    it.each([
        [403, { detail: { code: 'LOCAL_HELPER_NOT_PAIRED_HUMAN' } }, { kind: 'denied', code: 'LOCAL_HELPER_NOT_PAIRED_HUMAN' }],
        [403, { detail: { code: 'AUTH_PERMISSION_DENIED' } }, { kind: 'denied', code: 'AUTH_PERMISSION_DENIED' }],
        [409, { detail: { code: 'LOCAL_HELPER_NOT_PAIRED' } }, { kind: 'denied', code: 'LOCAL_HELPER_NOT_PAIRED' }],
        [403, {}, { kind: 'denied', code: null }],
        [422, { detail: [{ loc: ['body', 'path'] }] }, { kind: 'invalid' }],
        [401, { detail: { code: 'AUTH_INVALID_TOKEN' } }, { kind: 'failed' }],
        [500, { detail: { code: 'SYSTEM_ERROR' } }, { kind: 'failed' }],
    ])('HTTP %s 先看狀態再看代碼', async (status, body, expected) => {
        mockFetch.mockImplementation(() => reply(body, status));
        const { result } = renderHook(() => useLocalOpenRequest());
        await act(async () => {
            await result.current.requestOpen('D:\\相片');
        });
        expect(result.current.outcome).toEqual(expected);
    });

    it('連線失敗是沒送出，並記錄錯誤', async () => {
        mockFetch.mockImplementation(() => Promise.reject(new Error('down')));
        const { result } = renderHook(() => useLocalOpenRequest());
        await act(async () => {
            await result.current.requestOpen('D:\\相片');
        });
        expect(result.current.outcome).toEqual({ kind: 'failed' });
        expect(logSSOTError).toHaveBeenCalledOnce();
    });

    it('自己等太久就當成可能已經開放', async () => {
        vi.useFakeTimers();
        mockFetch.mockImplementation(
            (_url: string, _path: string, init: RequestInit) =>
                new Promise((_resolve, reject) => {
                    init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
                }),
        );
        const { result } = renderHook(() => useLocalOpenRequest());
        let done: Promise<void> = Promise.resolve();
        act(() => {
            done = result.current.requestOpen('D:\\相片');
        });
        await act(async () => {
            vi.advanceTimersByTime(OPEN_REQUEST_TIMEOUT_MS);
            await done;
        });
        expect(result.current.outcome).toEqual({ kind: 'timeout' });
        expect(result.current.pending).toBe(false);
        expect(logSSOTError).not.toHaveBeenCalled();
    });

    it('等得比 Touka 久', () => {
        expect(OPEN_REQUEST_TIMEOUT_MS).toBeGreaterThan(150_000);
    });

    it('離開頁面就中止，不記錯誤', async () => {
        let seen: AbortSignal | undefined;
        mockFetch.mockImplementation(
            (_url: string, _path: string, init: RequestInit) =>
                new Promise((_resolve, reject) => {
                    seen = init.signal ?? undefined;
                    init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
                }),
        );
        const { result, unmount } = renderHook(() => useLocalOpenRequest());
        let done: Promise<void> = Promise.resolve();
        act(() => {
            done = result.current.requestOpen('D:\\相片');
        });
        unmount();
        await done;
        expect(seen?.aborted).toBe(true);
        expect(logSSOTError).not.toHaveBeenCalled();
    });
});
