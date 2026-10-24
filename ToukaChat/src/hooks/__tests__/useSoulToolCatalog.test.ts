import { describe, expect, it, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useSoulToolCatalog } from '../useSoulToolCatalog';

// ADR-0281 T2：換連線就重讀；別的連線讀到的清單不給；讀取中與讀不到分開回報。
const { mockFetchTouka } = vi.hoisted(() => ({ mockFetchTouka: vi.fn() }));
vi.mock('@/lib/connections', () => ({ fetchTouka: mockFetchTouka }));

const STATS_A = { builtin_schemas: [{ function: { name: 'read' } }] };

function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>(r => { resolve = r; });
    return { promise, resolve };
}

describe('useSoulToolCatalog', () => {
    beforeEach(() => vi.clearAllMocks());

    it('never hands one proxy the list another proxy returned', async () => {
        const late = deferred<Response>();
        mockFetchTouka.mockImplementation((proxy: string) =>
            proxy === 'http://a' ? Promise.resolve(Response.json(STATS_A)) : late.promise);
        const { result, rerender } = renderHook(({ proxy }) => useSoulToolCatalog(proxy), {
            initialProps: { proxy: 'http://a' },
        });
        await waitFor(() => expect(result.current.status).toBe('ready'));

        // 換到 B：B 還沒讀完時不能拿 A 的清單；B 讀不到時也不能
        rerender({ proxy: 'http://b' });
        expect(result.current.status).toBe('loading');
        expect(result.current.tools).toEqual([]);
        late.resolve(new Response(null, { status: 500 }));
        await waitFor(() => expect(result.current.status).toBe('failed'));
        expect(result.current.tools).toEqual([]);
    });

    it('reports ready with the list, and failed when the request fails', async () => {
        mockFetchTouka.mockResolvedValueOnce(Response.json(STATS_A));
        const ready = renderHook(() => useSoulToolCatalog('http://a'));
        await waitFor(() => expect(ready.result.current.status).toBe('ready'));
        expect(ready.result.current.tools.map(entry => entry.key)).toEqual(['read']);

        mockFetchTouka.mockResolvedValueOnce(new Response(null, { status: 401 }));
        const failed = renderHook(() => useSoulToolCatalog('http://b'));
        await waitFor(() => expect(failed.result.current.status).toBe('failed'));
        expect(failed.result.current.tools).toEqual([]);
    });
});
