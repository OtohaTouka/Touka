/**
 * ADR-0148 Bug B — confirmInstall 的 env payload 形狀對釘。
 *
 * 後端 /terminal/approve 以 TerminalApprovalRequest 解析，只有 `payload` 欄位。
 * 舊送 { env_vars } 會被 pydantic 丟棄 → env 空 → MCP server 無金鑰啟動 → 安裝回
 * Success 但 runtime 失敗。本測試釘死 body ＝ { payload: { env_vars } }。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

vi.mock('@/lib/connections', () => ({
    fetchTouka: vi.fn(),
    getActiveProxyUrl: () => 'http://localhost:8000',
}));
vi.mock('@/lib/error_handler', () => ({
    logSSOTError: vi.fn(),
    errorMsg: () => 'err',
}));

import { fetchTouka } from '@/lib/connections';
import { useEvolution } from '../useEvolution';

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;

describe('useEvolution.confirmInstall（Bug B：env payload 形狀）', () => {
    beforeEach(() => vi.clearAllMocks());

    it('POST /terminal/approve body 須為 { payload: { env_vars } }（非裸 { env_vars }）', async () => {
        mockFetch.mockResolvedValue({ ok: true });
        const { result } = renderHook(() => useEvolution());

        await act(async () => {
            await result.current.confirmInstall('install_x_1', { API_KEY: 'secret' });
        });

        const call = mockFetch.mock.calls.find(c => String(c[1]).includes('/terminal/approve/install_x_1'));
        expect(call).toBeTruthy();
        expect(call![2].method).toBe('POST');
        const body = JSON.parse(call![2].body);
        expect(body).toEqual({ payload: { env_vars: { API_KEY: 'secret' } } });
        // env 不得裸放在頂層（會被後端丟棄）
        expect('env_vars' in body).toBe(false);
    });

    it('後端非 ok → 拋錯（呼叫端可據此顯示失敗）', async () => {
        mockFetch.mockResolvedValue({ ok: false });
        const { result } = renderHook(() => useEvolution());

        await expect(
            act(async () => {
                await result.current.confirmInstall('install_x_2', {});
            })
        ).rejects.toThrow();
    });
});
