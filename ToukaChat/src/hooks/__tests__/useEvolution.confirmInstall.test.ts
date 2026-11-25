/**
 * ADR-0148 Bug B — confirmInstall 的 env payload 形狀對釘。
 *
 * 後端 /terminal/approve 以 TerminalApprovalRequest 解析，只有 `payload` 欄位。
 * 舊送 { env_vars } 會被 pydantic 丟棄 → env 空 → MCP server 無金鑰啟動 → 安裝回
 * Success 但 runtime 失敗。本測試釘死 body ＝ { payload: { env_vars } }。
 *
 * 失敗時一律丟帶錯誤碼的 ToukaApiError，視窗才能說出原因（不是管理員、申請過期、斷線…）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

vi.mock('@/lib/connections', () => ({
    fetchTouka: vi.fn(),
    getActiveProxyUrl: () => 'http://localhost:8000',
}));
vi.mock('@/lib/error_handler', async () => {
    const actual = await vi.importActual<typeof import('@/lib/error_handler')>('@/lib/error_handler');
    return { ...actual, logSSOTError: vi.fn() };
});

import { fetchTouka } from '@/lib/connections';
import { errorMsg, getErrorCode, ToukaApiError } from '@/lib/error_handler';
import { useEvolution } from '../useEvolution';

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;

const failing = (status: number, code: string) =>
    new Response(JSON.stringify({ detail: { code } }), { status, headers: { 'Content-Type': 'application/json' } });

async function confirmAndCatch(): Promise<{ caught: unknown; isInstalling: boolean }> {
    const { result } = renderHook(() => useEvolution());
    let caught: unknown;
    await act(async () => {
        caught = await result.current.confirmInstall('install_x_9', {}).then(
            () => undefined,
            (err: unknown) => err,
        );
    });
    return { caught, isInstalling: result.current.isInstalling };
}

describe('useEvolution.confirmInstall（Bug B：env payload 形狀）', () => {
    beforeEach(() => vi.clearAllMocks());

    it('POST /terminal/approve body 須為 { payload: { env_vars } }（非裸 { env_vars }）', async () => {
        mockFetch.mockResolvedValueOnce(new Response('{}', { status: 200 }));
        const { result } = renderHook(() => useEvolution());

        let ok: boolean | undefined;
        await act(async () => {
            ok = await result.current.confirmInstall('install_x_1', { API_KEY: 'secret' });
        });

        expect(ok).toBe(true);
        expect(result.current.isInstalling).toBe(false);
        const call = mockFetch.mock.calls.find(c => String(c[1]).includes('/terminal/approve/install_x_1'));
        expect(call).toBeTruthy();
        expect(call![2].method).toBe('POST');
        const body = JSON.parse(call![2].body);
        expect(body).toEqual({ payload: { env_vars: { API_KEY: 'secret' } } });
        // env 不得裸放在頂層（會被後端丟棄）
        expect('env_vars' in body).toBe(false);
    });
});

describe('useEvolution.confirmInstall 失敗時丟帶錯誤碼的錯誤', () => {
    beforeEach(() => vi.clearAllMocks());

    it.each([
        [403, 'AUTH_ADMIN_REQUIRED'],
        [404, 'TERMINAL_APPROVAL_NOT_FOUND'],
        [403, 'AUTH_PERMISSION_DENIED'],
    ])('後端回 %i %s → 錯誤碼照後端給的', async (status, code) => {
        mockFetch.mockResolvedValueOnce(failing(status, code));
        const { caught, isInstalling } = await confirmAndCatch();

        expect(caught).toBeInstanceOf(ToukaApiError);
        expect(getErrorCode(caught)).toBe(code);
        expect((caught as ToukaApiError).status).toBe(status);
        expect((caught as Error).message).toBe(errorMsg(code as Parameters<typeof errorMsg>[0]));
        expect(isInstalling).toBe(false);
    });

    it('回應不是 JSON（例如 500 的 HTML）→ CONFIG_SAVE_FAILED', async () => {
        mockFetch.mockResolvedValueOnce(new Response('<html>oops</html>', { status: 500 }));
        const { caught, isInstalling } = await confirmAndCatch();

        expect(getErrorCode(caught)).toBe('CONFIG_SAVE_FAILED');
        expect((caught as ToukaApiError).status).toBe(500);
        expect(isInstalling).toBe(false);
    });

    it('fetchTouka 丟例外（斷線）→ NETWORK_CONNECTION_ERROR，不帶瀏覽器的英文原文', async () => {
        mockFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'));
        const { caught, isInstalling } = await confirmAndCatch();

        expect(getErrorCode(caught)).toBe('NETWORK_CONNECTION_ERROR');
        expect((caught as ToukaApiError).status).toBe(0);
        expect((caught as Error).message).toBe(errorMsg('NETWORK_CONNECTION_ERROR'));
        expect(isInstalling).toBe(false);
    });
});
