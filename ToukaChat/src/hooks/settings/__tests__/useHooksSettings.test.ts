/**
 * ADR-0292：Hook 設定頁的資料層——讀設定與紀錄、存檔帶載入時的 revision、錯誤分成衝突／路徑／欄位／其他。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import type { HooksConfigResponse } from '@/lib/types';

vi.mock('@/lib/connections', () => ({ fetchTouka: vi.fn(), getActiveProxyUrl: () => 'http://x' }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

import { fetchTouka } from '@/lib/connections';
import { useHooksSettings } from '../useHooksSettings';

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;

const LOADED: HooksConfigResponse = {
    config: { enabled: true, hooks: {} },
    health: { status: 'ok', revision: 'r1', detail: null },
    wired_events: ['UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop'],
    matcher_events: ['PreToolUse', 'PostToolUse'],
};
const RUN = { at: 1, event: 'Stop', hook: 'audit', hook_type: 'http', status: 'ok', outcome: 'none', duration_ms: 2 };

function reply(body: unknown, ok = true, status = 200) {
    return Promise.resolve({ ok, status, json: async () => body });
}

function route(put?: () => Promise<unknown>) {
    mockFetch.mockImplementation((_url: string, path: string, init?: RequestInit) => {
        if (init?.method === 'PUT' && put) return put();
        if (path === '/v1/hooks/runs') return reply({ runs: [RUN] });
        return reply(LOADED);
    });
}

async function loaded() {
    const hook = renderHook(() => useHooksSettings());
    await waitFor(() => expect(hook.result.current.data).not.toBeNull());
    return hook;
}

describe('useHooksSettings（ADR-0292）', () => {
    beforeEach(() => vi.clearAllMocks());

    it('讀到設定與最近執行', async () => {
        route();
        const { result } = await loaded();
        expect(result.current.loading).toBe(false);
        expect(result.current.loadFailed).toBe(false);
        expect(result.current.data?.health.revision).toBe('r1');
        await waitFor(() => expect(result.current.runs).toEqual([RUN]));
    });

    it.each([
        ['403（不是人類管理員）', () => reply({ detail: { code: 'AUTH_ADMIN_REQUIRED' } }, false, 403)],
        ['格式不對', () => reply({ config: {} })],
        ['連不上', () => Promise.reject(new Error('network'))],
    ])('%s → 讀取失敗', async (_name, response) => {
        mockFetch.mockImplementation(response);
        const { result } = renderHook(() => useHooksSettings());
        await waitFor(() => expect(result.current.loadFailed).toBe(true));
        expect(result.current.data).toBeNull();
        expect(result.current.loading).toBe(false);
    });

    it('存檔送整份設定和載入時的 revision，成功後重新讀取', async () => {
        route(() => reply({ revision: 'r2' }));
        const { result } = await loaded();
        const config = { enabled: false, hooks: {} };
        let outcome: unknown;
        await act(async () => { outcome = await result.current.saveConfig(config); });
        expect(outcome).toEqual({ kind: 'saved' });

        const put = mockFetch.mock.calls.find(([, , init]) => init?.method === 'PUT');
        expect(put?.[1]).toBe('/v1/hooks');
        expect(JSON.parse(put?.[2].body)).toEqual({ config, revision: 'r1' });
        const reads = mockFetch.mock.calls.filter(([, path, init]) => path === '/v1/hooks' && !init);
        expect(reads.length).toBe(2);
    });

    it('存好了但重新讀取失敗 → 留著剛存的內容和新的 revision，不當成讀取失敗', async () => {
        let saved = false;
        mockFetch.mockImplementation((_u: string, path: string, init?: RequestInit) => {
            if (init?.method === 'PUT') { saved = true; return reply({ revision: 'r2' }); }
            if (path === '/v1/hooks/runs') return reply({ runs: [] });
            return saved ? Promise.reject(new Error('network')) : reply(LOADED);
        });
        const { result } = await loaded();
        const config = { enabled: false, hooks: {} };
        let outcome: unknown;
        await act(async () => { outcome = await result.current.saveConfig(config); });
        expect(outcome).toEqual({ kind: 'saved' });
        expect(result.current.loadFailed).toBe(false);
        expect(result.current.data?.config).toEqual(config);
        expect(result.current.data?.health.revision).toBe('r2');
    });

    it('沒有設定檔時 revision 明確送 null', async () => {
        const missing = { ...LOADED, health: { status: 'missing', revision: null, detail: null } };
        mockFetch.mockImplementation((_u: string, path: string, init?: RequestInit) =>
            init?.method === 'PUT' ? reply({ revision: 'r1' }) : reply(path === '/v1/hooks' ? missing : { runs: [] }));
        const { result } = await loaded();
        await act(async () => { await result.current.saveConfig(LOADED.config); });
        const put = mockFetch.mock.calls.find(([, , init]) => init?.method === 'PUT');
        expect(JSON.parse(put?.[2].body).revision).toBeNull();
    });

    it.each([
        ['衝突', { detail: { code: 'HOOK_CONFIG_CONFLICT', revision: 'r9' } }, 409, { kind: 'conflict' }],
        ['路徑被拒收',
            { detail: { code: 'HOOK_PATH_REJECTED', problems: [{ event: 'Stop', where: 'command', reason: 'agent_writable', value: '/x' }] } },
            422,
            { kind: 'path_rejected', problems: [{ event: 'Stop', where: 'command', reason: 'agent_writable', value: '/x' }] }],
        ['欄位不合規則', { detail: [{ loc: ['body', 'config', 'hooks', 'Stop'], msg: 'bad' }] }, 422,
            { kind: 'invalid', where: 'config.hooks.Stop', message: 'bad' }],
        ['其他失敗', { detail: { code: 'SYSTEM_ERROR' } }, 500, { kind: 'failed' }],
    ])('存檔%s', async (_name, body, status, expected) => {
        route(() => reply(body, false, status));
        const { result } = await loaded();
        let outcome: unknown;
        await act(async () => { outcome = await result.current.saveConfig(LOADED.config); });
        expect(outcome).toEqual(expected);
        expect(result.current.saving).toBe(false);
    });

    it('還沒讀到設定時不送出', async () => {
        mockFetch.mockImplementation(() => reply(null, false, 403));
        const { result } = renderHook(() => useHooksSettings());
        await waitFor(() => expect(result.current.loadFailed).toBe(true));
        let outcome: unknown;
        await act(async () => { outcome = await result.current.saveConfig(LOADED.config); });
        expect(outcome).toEqual({ kind: 'failed' });
        expect(mockFetch.mock.calls.some(([, , init]) => init?.method === 'PUT')).toBe(false);
    });
});
