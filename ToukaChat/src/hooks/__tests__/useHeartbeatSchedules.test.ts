/** Human Proxy bridge contract, revision and visible mutation outcomes. */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { fetchTouka } from '@/lib/connections';
import { toast } from 'sonner';
import { useHeartbeatSchedules } from '../useHeartbeatSchedules';

vi.mock('@/lib/connections', () => ({
    getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
    getActiveHeartbeatUrl: vi.fn(() => 'http://localhost:8082'),
    fetchTouka: vi.fn(),
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }));
const mockFetch = vi.mocked(fetchTouka);
const row = { id: 'owned', agent_id: 'TestAgent', mode: 'interval', active: true };
const listResponse = () => new Response(JSON.stringify([row]), { headers: { ETag: '"revision"' } });

async function loadedHook() {
    const hook = renderHook(() => useHeartbeatSchedules());
    await waitFor(() => expect(hook.result.current.schedules).toEqual([row]));
    return hook;
}

describe('useHeartbeatSchedules Proxy contract', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockFetch.mockImplementation(async (_base, _path, options) =>
            options?.method ? new Response(JSON.stringify(row)) : listResponse());
    });
    it('loads a bare list from the human-authenticated Proxy route', async () => {
        await loadedHook();
        expect(mockFetch).toHaveBeenCalledWith('http://localhost:8000', '/v1/heartbeat/schedules');
    });
    it.each([null, 'owned'])('POST upsert uses collection route, body id and If-Match (%s)', async (id) => {
        const { result } = await loadedHook();
        await act(async () => { expect(await result.current.saveSchedule(id, { agent_id: 'TestAgent' })).toBe(true); });
        const call = mockFetch.mock.calls.find((c) => c[2]?.method === 'POST')!;
        expect(call.slice(0, 2)).toEqual(['http://localhost:8000', '/v1/heartbeat/schedules']);
        expect(new Headers(call[2]?.headers).get('If-Match')).toBe('"revision"');
        expect(JSON.parse(call[2]?.body as string)).toEqual({ agent_id: 'TestAgent', ...(id ? { id } : {}) });
    });
    it('DELETE quotes the exact id and sends If-Match', async () => {
        const { result } = await loadedHook();
        await act(async () => { expect(await result.current.deleteSchedule('owned/../custom')).toBe(true); });
        const call = mockFetch.mock.calls.find((c) => c[2]?.method === 'DELETE')!;
        expect(call[1]).toBe('/v1/heartbeat/schedules/owned%2F%2E%2E%2Fcustom');
        expect(new Headers(call[2]?.headers).get('If-Match')).toBe('"revision"');
    });
    it.each(['http', 'network', 'malformed', 'missing-revision'])('exposes load failure and blocks writes (%s)', async (failure) => {
        if (failure === 'network') mockFetch.mockRejectedValue(new TypeError('owned network failure'));
        else mockFetch.mockResolvedValue(failure === 'http' ? new Response('{}', { status: 503 }) :
            new Response(JSON.stringify(failure === 'malformed' ? { schedules: [] } : []),
                { headers: failure === 'missing-revision' ? {} : { ETag: '"revision"' } }));
        const { result } = renderHook(() => useHeartbeatSchedules());
        await waitFor(() => expect(result.current.loadError).toBeTruthy());
        await act(async () => { expect(await result.current.saveSchedule(null, { agent_id: 'TestAgent' })).toBe(false); });
        expect(mockFetch.mock.calls.every((c) => !c[2]?.method)).toBe(true);
        expect(toast.success).not.toHaveBeenCalled();
    });
    it('keeps existing list and invalidates revision after failed refresh', async () => {
        const { result } = await loadedHook();
        mockFetch.mockResolvedValue(new Response('{}', { status: 503 }));
        await act(async () => { await result.current.loadSchedules(); });
        expect(result.current.schedules).toEqual([row]);
        expect(result.current.loadError).toBeTruthy();
        mockFetch.mockClear();
        await act(async () => { expect(await result.current.deleteSchedule('owned')).toBe(false); });
        expect(mockFetch).not.toHaveBeenCalled();
    });
    it('distinguishes saved but refresh failed without repeating mutation', async () => {
        const { result } = await loadedHook();
        mockFetch.mockResolvedValueOnce(new Response(JSON.stringify(row)))
            .mockResolvedValueOnce(new Response('{}', { status: 503 }));
        await act(async () => { expect(await result.current.saveSchedule('owned', { agent_id: 'TestAgent' })).toBe(true); });
        expect(result.current.mutationState).toBe('saved_refresh_failed');
        expect(toast.warning).toHaveBeenCalled();
        expect(mockFetch.mock.calls.filter((c) => c[2]?.method === 'POST')).toHaveLength(1);
    });
    it.each(['conflict', 'unknown', 'network'])('keeps UI state after unsuccessful mutation (%s)', async (failure) => {
        const { result } = await loadedHook();
        if (failure === 'network') mockFetch.mockRejectedValueOnce(new TypeError('owned mutation timeout'));
        else mockFetch.mockResolvedValueOnce(new Response(JSON.stringify({ detail: {
            id: failure === 'conflict' ? 5107 : 5111,
        } }), { status: failure === 'conflict' ? 409 : 503 }));
        await act(async () => { expect(await result.current.deleteSchedule('owned')).toBe(false); });
        expect(result.current.schedules).toEqual([row]);
        expect(result.current.mutationError).toBeTruthy();
        expect(toast.success).not.toHaveBeenCalled();
        expect(mockFetch.mock.calls.filter((c) => c[2]?.method === 'DELETE')).toHaveLength(1);
    });
    it.each(['body', 'exception'])('never displays unknown server or exception content (%s)', async (failure) => {
        const { result } = await loadedHook();
        const sentinel = 'synthetic-private-error-sentinel';
        if (failure === 'body') mockFetch.mockResolvedValue(new Response(JSON.stringify({ detail: { message: sentinel } }), { status: 503 }));
        else mockFetch.mockRejectedValue(new Error(sentinel));
        await act(async () => { expect(await result.current.deleteSchedule('owned')).toBe(false); });
        expect(result.current.mutationError).toBeTruthy();
        expect(result.current.mutationError).not.toContain(sentinel);
        await act(async () => { await result.current.loadSchedules(); });
        expect(result.current.loadError).toBeTruthy();
        expect(result.current.loadError).not.toContain(sentinel);
        expect(JSON.stringify(vi.mocked(toast.error).mock.calls)).not.toContain(sentinel);
    });
});
