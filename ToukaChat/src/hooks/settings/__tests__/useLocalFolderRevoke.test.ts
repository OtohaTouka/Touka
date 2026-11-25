/**
 * ADR-0330 2b：設定頁請小幫手把開放的資料夾拿掉或改成只能讀。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

vi.mock('@/lib/connections', () => ({ fetchTouka: vi.fn(), getActiveProxyUrl: () => 'http://x' }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), getResponseErrorCode: vi.fn() }));

import { fetchTouka } from '@/lib/connections';
import { getResponseErrorCode, logSSOTError } from '@/lib/error_handler';
import { useLocalFolderRevoke } from '../useLocalFolderRevoke';

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;
const mockCode = getResponseErrorCode as ReturnType<typeof vi.fn>;
const FOLDER = { name: 'Notes', path: 'D:\\Example\\Notes' };
const DONE = { status: 'done', code: null, name: 'Notes', removed: null, writable: false };

function reply(body: unknown, ok = true, status = 200) {
    return Promise.resolve({ ok, status, json: async () => body });
}

describe('useLocalFolderRevoke（ADR-0330 2b）', () => {
    beforeEach(() => vi.clearAllMocks());

    it('送出名稱、路徑與改法，結果照原樣給畫面', async () => {
        mockFetch.mockImplementation(() => reply(DONE));
        const { result } = renderHook(() => useLocalFolderRevoke());
        let outcome: unknown;
        await act(async () => {
            outcome = await result.current.revoke(FOLDER, 'read_only');
        });
        expect(mockFetch).toHaveBeenCalledWith('http://x', '/v1/local-bridge/folders/revoke', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: 'Notes', path: 'D:\\Example\\Notes', change: 'read_only' }),
        });
        expect(outcome).toEqual({ kind: 'result', result: DONE });
        expect(result.current.outcome).toEqual(outcome);
        expect(result.current.pending).toBe(false);
    });

    it.each([403, 409])('%s：帶回拒絕的代碼', async (status) => {
        mockFetch.mockImplementation(() => reply({}, false, status));
        mockCode.mockResolvedValue('LOCAL_HELPER_NOT_PAIRED_HUMAN');
        const { result } = renderHook(() => useLocalFolderRevoke());
        await act(async () => {
            await result.current.revoke(FOLDER, 'remove');
        });
        expect(result.current.outcome).toEqual({ kind: 'denied', code: 'LOCAL_HELPER_NOT_PAIRED_HUMAN' });
    });

    it('其他錯誤與送不出去：當成不確定', async () => {
        mockFetch.mockImplementationOnce(() => reply({}, false, 500));
        const { result } = renderHook(() => useLocalFolderRevoke());
        await act(async () => {
            await result.current.revoke(FOLDER, 'remove');
        });
        expect(result.current.outcome).toEqual({ kind: 'failed' });
        mockFetch.mockImplementationOnce(() => Promise.reject(new Error('offline')));
        await act(async () => {
            await result.current.revoke(FOLDER, 'remove');
        });
        expect(result.current.outcome).toEqual({ kind: 'failed' });
        expect(logSSOTError).toHaveBeenCalledTimes(1);
    });
});
