import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

vi.mock('@/lib/connections', () => ({
    fetchTouka: vi.fn(),
    getActiveProxyUrl: () => 'http://localhost:8000',
}));
vi.mock('@/lib/error_handler', () => ({
    logSSOTError: vi.fn(),
    errorMsg: () => '儲存失敗',
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { fetchTouka } from '@/lib/connections';
import { toast } from 'sonner';
import { useConnectionSettings } from '../useConnectionSettings';

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;

describe('useConnectionSettings 健康狀態與存檔回饋', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('fetchConnectionStatus 從 /status 填入連線狀態（以 id 為 key）', async () => {
        mockFetch.mockImplementation((_u: string, path: string) => {
            if (path === '/v1/ui/connections/status') {
                return Promise.resolve({ ok: true, json: async () => ({ 'conn-1': 'connection_error' }) });
            }
            return Promise.resolve({ ok: true, json: async () => [] });
        });

        const { result } = renderHook(() => useConnectionSettings());
        await act(async () => { await result.current.fetchConnectionStatus(); });
        await waitFor(() => expect(result.current.connectionStatus).toEqual({ 'conn-1': 'connection_error' }));
    });

    it('saveConnections 成功時提示成功並回傳 true', async () => {
        mockFetch.mockResolvedValue({ ok: true, json: async () => [] });
        const { result } = renderHook(() => useConnectionSettings());

        let ok = false;
        await act(async () => { ok = await result.current.saveConnections([]); });
        expect(ok).toBe(true);
        expect(toast.success).toHaveBeenCalled();
        expect(toast.error).not.toHaveBeenCalled();
    });

    it('saveConnections 失敗時提示錯誤、回傳 false（不再靜默）', async () => {
        mockFetch.mockImplementation((_u: string, path: string) => {
            if (path === '/v1/ui/connections' ) {
                return Promise.resolve({ ok: false, status: 500, json: async () => ({}) });
            }
            return Promise.resolve({ ok: true, json: async () => [] });
        });
        const { result } = renderHook(() => useConnectionSettings());

        let ok = true;
        await act(async () => { ok = await result.current.saveConnections([{ id: 'c1', name: 'A', url: 'http://a', apiKey: '', provider: 'custom', protocol: 'openai', enabled: true }]); });
        expect(ok).toBe(false);
        expect(toast.error).toHaveBeenCalled();
        expect(toast.success).not.toHaveBeenCalled();
    });

    it('testConnection 無掃描錯誤時提示成功', async () => {
        mockFetch.mockImplementation((_u: string, path: string) => {
            if (path === '/v1/ui/connections/status') {
                return Promise.resolve({ ok: true, json: async () => ({ c1: 'ok' }) });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });
        const { result } = renderHook(() => useConnectionSettings());
        await act(async () => { await result.current.testConnection('c1'); });
        expect(toast.success).toHaveBeenCalled();
        expect(toast.error).not.toHaveBeenCalled();
    });

    it('testConnection 偵測到 connection_error 時提示失敗', async () => {
        mockFetch.mockImplementation((_u: string, path: string) => {
            if (path === '/v1/ui/connections/status') {
                return Promise.resolve({ ok: true, json: async () => ({ c1: 'connection_error' }) });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });
        const { result } = renderHook(() => useConnectionSettings());
        await act(async () => { await result.current.testConnection('c1'); });
        expect(toast.error).toHaveBeenCalled();
        expect(toast.success).not.toHaveBeenCalled();
    });
});
