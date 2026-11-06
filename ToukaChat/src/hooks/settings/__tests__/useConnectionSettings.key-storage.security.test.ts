/**
 * 連線頁輸入的第三方 API 金鑰不能留在瀏覽器：存進 localStorage 的清單一律是遮罩佔位，
 * 送給後端的 POST 才帶這次輸入的真值（後端收到遮罩當作沒變）。
 * 涵蓋新增、編輯換金鑰、POST 失敗與換後端四種寫入，以及舊版留在本機的真值不會被讀出來再送出。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

vi.mock('@/lib/connections', async importOriginal => ({
    ...await importOriginal<typeof import('@/lib/connections')>(),
    fetchTouka: vi.fn(),
    getActiveProxyUrl: vi.fn(() => ''),
    isProxyReachable: vi.fn(async () => true),
    logoutTouka: vi.fn(async () => undefined),
    captureRequestScope: vi.fn(() => ({ proxyOrigin: '' })),
    isRequestScopeCurrent: vi.fn(() => true),
}));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), errorMsg: () => '儲存失敗' }));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), loading: vi.fn(), dismiss: vi.fn() } }));
vi.mock('@/hooks/useConfirmToast', () => ({
    useConfirmToast: () => ({ confirmToast: (_m: string, onConfirm: () => void) => { void onConfirm(); } }),
}));

import { fetchTouka, keepActiveProxy } from '@/lib/connections';
import { SECRET_MASK } from '@/lib/secretMask';
import { useConnectionSettings } from '../useConnectionSettings';
import type { Connection } from '@/lib/types';

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;
const TYPED_KEY = 'typed-in-this-page-test-value';
const LEGACY_KEY = 'legacy-key-stored-by-old-version';
const stored = () => window.localStorage.getItem('touka_connections') ?? '';
const custom = (id: string, apiKey: string): Connection =>
    ({ id, name: id, url: `http://${id}.test`, apiKey, provider: 'custom', protocol: 'openai', enabled: true });
const posted = () => mockFetch.mock.calls
    .filter(([, path, init]) => path === '/v1/ui/connections' && init?.method === 'POST')
    .map(([, , init]) => String(init.body));

function backend(saveOk = true) {
    mockFetch.mockImplementation(async (_url: string, path: string, init?: RequestInit) => {
        if (path === '/v1/ui/connections' && init?.method === 'POST') {
            return { ok: saveOk, status: saveOk ? 200 : 500, json: async () => ({}) };
        }
        if (path === '/v1/ui/connections/status') return { ok: true, json: async () => ({}) };
        // 後端回空清單時不會蓋掉本機清單：最容易留下真值的情況。
        return { ok: true, json: async () => [] };
    });
}

describe('連線金鑰不留在瀏覽器', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        window.localStorage.clear();
    });

    it('新增：本機只存遮罩，POST 帶真值', async () => {
        backend();
        const { result } = renderHook(() => useConnectionSettings());

        await act(async () => {
            result.current.addConnection({ name: 'n', url: 'http://n.test', apiKey: TYPED_KEY, provider: 'custom', protocol: 'openai', enabled: true });
        });
        await waitFor(() => expect(posted()).toHaveLength(1));

        expect(stored()).not.toContain(TYPED_KEY);
        expect(stored()).toContain(SECRET_MASK);
        expect(posted()[0]).toContain(TYPED_KEY);
    });

    it('編輯時換金鑰：本機只存遮罩，POST 帶新的真值', async () => {
        window.localStorage.setItem('touka_connections', JSON.stringify([custom('c1', SECRET_MASK)]));
        backend();
        const { result } = renderHook(() => useConnectionSettings());

        await act(async () => {
            result.current.updateConnection('c1', { name: 'c1', url: 'http://c1.test', provider: 'custom', protocol: 'openai', apiKey: TYPED_KEY }, true);
        });
        await waitFor(() => expect(posted()).toHaveLength(1));

        expect(stored()).not.toContain(TYPED_KEY);
        expect(posted()[0]).toContain(TYPED_KEY);
    });

    it('編輯時不換金鑰：送回遮罩，後端保留原本的真值', async () => {
        window.localStorage.setItem('touka_connections', JSON.stringify([custom('c1', SECRET_MASK)]));
        backend();
        const { result } = renderHook(() => useConnectionSettings());

        await act(async () => {
            result.current.updateConnection('c1', { name: 'renamed', url: 'http://c1.test', provider: 'custom', protocol: 'openai', apiKey: '' }, false);
        });
        await waitFor(() => expect(posted()).toHaveLength(1));

        expect(JSON.parse(posted()[0])[0].apiKey).toBe(SECRET_MASK);
    });

    it('舊版留在本機的真值不會被送出：編輯時送的是遮罩', async () => {
        window.localStorage.setItem('touka_connections', JSON.stringify([custom('c1', LEGACY_KEY)]));
        backend();
        const { result } = renderHook(() => useConnectionSettings());

        await act(async () => {
            result.current.updateConnection('c1', { name: 'renamed', url: 'http://c1.test', provider: 'custom', protocol: 'openai', apiKey: '' }, false);
        });
        await waitFor(() => expect(posted()).toHaveLength(1));

        expect(posted()[0]).not.toContain(LEGACY_KEY);
        expect(JSON.parse(posted()[0])[0].apiKey).toBe(SECRET_MASK);
        expect(stored()).not.toContain(LEGACY_KEY);
    });

    it('換成後端的清單時，留下的本機 Proxy 項目也不帶真值', () => {
        const local = { ...custom('proxy', LEGACY_KEY), provider: 'proxy', url: 'http://a.test:8000' };
        window.localStorage.setItem('touka_connections', JSON.stringify([local]));

        const merged = keepActiveProxy([{ ...custom('proxy', ''), provider: 'proxy', url: 'http://b.test:9000' }]);

        expect(JSON.stringify(merged)).not.toContain(LEGACY_KEY);
        expect(merged[0].apiKey).toBe(SECRET_MASK);
    });

    it('POST 失敗：還原成記憶體裡上一份清單（含真值）時，本機也只有遮罩', async () => {
        // 第一次存檔成功，但後端回空清單，記憶體裡的上一份清單就還留著真值。
        backend();
        const { result } = renderHook(() => useConnectionSettings());
        await act(async () => { await result.current.saveConnections([custom('c1', TYPED_KEY)]); });

        backend(false);
        let ok = true;
        await act(async () => {
            ok = await result.current.saveConnections([custom('c1', TYPED_KEY), custom('c2', 'second-typed-test-value')]);
        });

        expect(ok).toBe(false);
        expect(posted()[1]).toContain('second-typed-test-value');
        expect(result.current.connections.map(c => c.id)).toEqual(['c1']);
        expect(stored()).not.toContain(TYPED_KEY);
        expect(stored()).not.toContain('second-typed-test-value');
        expect(stored()).toContain(SECRET_MASK);
    });

    it('換後端：只寫本機的清單也是遮罩，網址照常換', async () => {
        backend();
        const { result } = renderHook(() => useConnectionSettings());
        const next = [{ ...custom('proxy', ''), provider: 'proxy', url: 'http://new.test:9000' }, custom('c1', TYPED_KEY)];

        await act(async () => { await result.current.saveConnections(next); });
        await waitFor(() => expect(stored()).toContain('http://new.test:9000'));

        expect(stored()).not.toContain(TYPED_KEY);
        expect(stored()).toContain(SECRET_MASK);
        expect(posted()).toEqual([]);
    });

    it('通知其他元件的事件也不帶真值', async () => {
        backend();
        const details: unknown[] = [];
        const record = (event: Event) => details.push((event as CustomEvent).detail);
        window.addEventListener('touka_connections_updated', record);
        const { result } = renderHook(() => useConnectionSettings());

        await act(async () => { await result.current.saveConnections([custom('c1', TYPED_KEY)]); });
        window.removeEventListener('touka_connections_updated', record);

        expect(details.length).toBeGreaterThan(0);
        expect(JSON.stringify(details)).not.toContain(TYPED_KEY);
    });
});
