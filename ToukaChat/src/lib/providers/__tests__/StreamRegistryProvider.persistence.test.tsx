import React from 'react';
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from '@/lib/types';
import type { SessionChange } from '@/lib/session_persistence';
import type { StartStreamParams } from '../StreamRegistryProvider';
import { getSystemEpoch } from '@/lib/time_utils';

const h = vi.hoisted(() => ({
  sessions: [] as Session[], save: vi.fn(), wire: vi.fn(), fetch: vi.fn(),
  register: vi.fn(), complete: vi.fn(), begin: vi.fn(), scope: { proxyOrigin: 'http://x', authenticatedPrincipalId: 'a', authEpoch: 1 },
}));
vi.mock('../SessionListProvider', () => ({ useSessionList: () => ({
  sessions: h.sessions,
  getSession: (id: string) => h.sessions.find(session => session.id === id),
  setSessions: (action: React.SetStateAction<Session[]>) => {
    if (typeof action === 'function') { action(h.sessions); h.sessions = action(h.sessions); } else h.sessions = action;
  },
  syncSession: h.save,
  updateSession: (id: string, change: SessionChange) => {
    const current = h.sessions.find(session => session.id === id)!;
    const first = change(current);
    expect(change(current)).toEqual(first);
    h.sessions = h.sessions.map(session => session.id === id ? first : session);
    return h.save(first);
  },
  beginProducer: h.begin, completeProducer: h.complete, scopeKey: 'scope-a',
  getPersistence: () => ({ status: 'unknown', error_code: 1307 }),
}) }));
vi.mock('../DebugProvider', () => ({ useDebug: () => ({ registerSimHandlers: h.register }) }));
vi.mock('../../connections', () => ({
  fetchChatStream: h.wire, fetchTouka: h.fetch, getActiveProxyUrl: () => 'http://x', resumeChatStream: vi.fn(),
  captureRequestScope: () => h.scope, isRequestScopeCurrent: (scope: unknown) => scope === h.scope,
}));
vi.mock('@/hooks/useArtifacts', () => ({ useArtifacts: { getState: () => ({}) } }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), errorMsg: (code: string) => code }));
vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

import { StreamRegistryProvider, useStreamRegistry } from '../StreamRegistryProvider';

const params = (): StartStreamParams => ({
  sessionId: 's1', baseTree: { messages: {}, currentId: null }, userText: 'user', resolvedInput: 'user',
  attachment: null, parentId: null, model: 'synthetic', protocol: 'openai', proxyUrl: 'http://x',
  authLevel: 'Manual', activeSkills: [],
});
const wrapper = ({ children }: { children: React.ReactNode }) => <StreamRegistryProvider>{children}</StreamRegistryProvider>;
describe('actual registry user-turn persistence barrier', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    h.sessions = [{ id: 's1', title: 'title', messages: [], updatedAt: 1 }];
    h.fetch.mockResolvedValue({ ok: true, json: async () => ({ streams: [] }) });
    h.wire.mockResolvedValue(undefined);
    h.begin.mockReturnValue({ sessionId: 's1', scope: h.scope, generation: 0 });
    h.save.mockResolvedValue({ status: 'saved', representation: 'full', revision: 'user' });
    h.complete.mockResolvedValue({ status: 'unknown', error_code: 1302 });
  });
  it('waits for a full user save; repeated start during the wait sends only once', async () => {
    let finish!: (value: unknown) => void;
    h.save.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    let start!: ReturnType<typeof result.current.startStream>;
    act(() => { start = result.current.startStream(params()); });
    await act(async () => {});
    const sentEarly = h.wire.mock.calls.length;
    let second: unknown;
    await act(async () => { second = await result.current.startStream(params()); });
    await act(async () => { finish({ status: 'saved', representation: 'full', revision: 'user' }); await start; });
    expect(sentEarly).toBe(0);
    expect(h.save).toHaveBeenCalledTimes(1);
    expect(second).toBe(false);
    expect(h.wire).toHaveBeenCalledTimes(1);
  });
  it.each([
    { status: 'failed', error_code: 1302 }, { status: 'unknown', error_code: 1302 },
    { status: 'saved', representation: 'text', revision: 'text' }, { status: 'skipped', reason: 'no_text' },
  ])('does not send a model request after $status/$representation/$reason', async outcome => {
    h.save.mockResolvedValue(outcome);
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    let started: unknown;
    await act(async () => { started = await result.current.startStream(params()); });
    expect(h.wire).not.toHaveBeenCalled();
    expect(started).toBe(false);
    expect(h.sessions[0].messages.currentId).toBeTruthy();
  });
  it.each(['null', 'throw'])('producer admission %s stops the active indicator without a model call', async mode => {
    if (mode === 'null') h.begin.mockReturnValue(null);
    else h.begin.mockImplementation(() => { throw new Error('synthetic admission failure'); });
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    let outcome: unknown;
    await act(async () => { outcome = await result.current.startStream(params()).catch(() => 'rejected'); });
    expect(h.wire).not.toHaveBeenCalled();
    expect(result.current.activeCount).toBe(0);
    expect(result.current.getStream('s1')?.status).toBe('error');
    expect(outcome).toBe(false);
  });
  it('a late user save cannot recreate an entry removed by stop', async () => {
    let finish!: (value: unknown) => void;
    h.save.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    let pending!: Promise<boolean>;
    act(() => { pending = result.current.startStream(params()); });
    act(() => result.current.stopStream('s1'));
    await act(async () => { finish({ status: 'saved', representation: 'full', revision: 'r1' }); await pending; });
    expect(h.wire).not.toHaveBeenCalled();
    expect(result.current.getStream('s1')).toBeUndefined();
    expect(result.current.activeCount).toBe(0);
  });
  it('user append preserves nodes newer than the displayed base snapshot', async () => {
    const node = { id: 'new', role: 'assistant', content: 'newer', parentId: null, childrenIds: [] };
    h.sessions[0].messages = { messages: { new: node }, currentId: 'new' };
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream(params()); });
    expect(h.save.mock.calls[0][0].messages.messages.new).toEqual(node);
    expect(result.current.getStream('s1')?.tree.messages.new).toEqual(node);
  });
  it('user turn and finished reply carry their times; existing times are never replaced', async () => {
    const node = { id: 'hb', role: 'assistant', content: 'heartbeat', parentId: null, childrenIds: [], timestamp: 42 };
    h.sessions[0].messages = { messages: { hb: node }, currentId: 'hb' };
    const before = getSystemEpoch();
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream({ ...params(), parentId: 'hb' }); });
    const state = result.current.getStream('s1')!;
    const userId = state.tree.messages[state.assistantMsgId].parentId!;
    const saved = h.save.mock.calls[0][0].messages;
    const sentAt = saved.messages[userId].timestamp;
    expect(sentAt).toBeGreaterThanOrEqual(before);
    expect(saved.messages.hb).toEqual({ ...node, childrenIds: [userId] });
    const started = { type: 'touka_stream_started', session_id: 's1', stream_id: 'stream',
      user_parent_id: userId, assistant_node_id: state.assistantMsgId };
    await act(async () => {
      h.wire.mock.calls[0][5](started);
      h.wire.mock.calls[0][2]('answer');
      await h.wire.mock.calls[0][3]({ ...started, type: 'touka_stream_terminal', generation: 'completed',
        persistence: { status: 'saved', representation: 'text', revision: 'text' } });
    });
    const change: SessionChange = h.complete.mock.calls[0][1];
    const final = change(h.sessions[0]).messages;
    expect(final.messages[state.assistantMsgId].timestamp).toBeGreaterThanOrEqual(sentAt);
    expect(final.messages[userId].timestamp).toBe(sentAt);
    expect(final.messages.hb).toEqual({ ...node, childrenIds: [userId] });
  });
  it('actual request carries the confirmed revision and fixed user/assistant ids', async () => {
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream(params()); });
    const state = result.current.getStream('s1')!;
    expect(h.wire.mock.calls[0][1]).toMatchObject({ requestScope: h.scope, ui_persistence: {
      mode: 'persistent', revision: 'user', assistant_node_id: state.assistantMsgId,
      user_parent_id: state.tree.messages[state.assistantMsgId].parentId,
    } });
  });
  it('actual typed terminal waits for full persistence before removing the rich entry', async () => {
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream(params()); });
    const state = result.current.getStream('s1')!;
    const started = { type: 'touka_stream_started', session_id: 's1', stream_id: 'stream',
      user_parent_id: state.tree.messages[state.assistantMsgId].parentId, assistant_node_id: state.assistantMsgId };
    let resolve!: (value: unknown) => void;
    h.complete.mockReturnValue(new Promise(done => { resolve = done; }));
    let terminal!: Promise<void>;
    act(() => {
      h.wire.mock.calls[0][5](started);
      h.wire.mock.calls[0][2]('answer');
      terminal = h.wire.mock.calls[0][3]({ ...started, type: 'touka_stream_terminal', generation: 'completed',
        persistence: { status: 'saved', representation: 'text', revision: 'text' } });
    });
    expect(h.complete).toHaveBeenCalledWith(expect.anything(), expect.any(Function),
      { status: 'saved', representation: 'text', revision: 'text' }, 'completed');
    expect(result.current.getStream('s1')?.tree.messages[state.assistantMsgId].content).toBe('answer');
    await act(async () => { resolve({ status: 'saved', representation: 'full', revision: 'full' }); await terminal; });
    expect(result.current.getStream('s1')).toBeUndefined();
  });
  it.each(['error', 'cancelled'] as const)('incognito %s has skipped persistence and can start another turn', async generation => {
    h.sessions[0].incognito = true;
    h.save.mockResolvedValue({ status: 'skipped', reason: 'incognito' });
    h.complete.mockResolvedValue({ status: 'skipped', reason: 'incognito' });
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream({ ...params(), incognito: true }); });
    const state = result.current.getStream('s1')!;
    const started = { type: 'touka_stream_started', session_id: 's1', stream_id: 'stream',
      user_parent_id: state.tree.messages[state.assistantMsgId].parentId, assistant_node_id: state.assistantMsgId };
    await act(async () => {
      h.wire.mock.calls[0][5](started);
      await h.wire.mock.calls[0][3]({ ...started, type: 'touka_stream_terminal', generation,
        persistence: { status: 'skipped', reason: 'incognito' } });
    });
    expect(h.complete.mock.calls[0][3]).toBe(generation);
    expect(result.current.activeCount).toBe(0);
    expect(h.fetch.mock.calls.some(([, path]) => /\/sessions\//.test(path))).toBe(false);
    await act(async () => { expect(await result.current.startStream({ ...params(), incognito: true })).toBe(true); });
    expect(h.wire).toHaveBeenCalledTimes(2);
  });
  it('stopping a known stream fences the request and retains its unsaved tree', async () => {
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream(params()); });
    const state = result.current.getStream('s1')!;
    act(() => {
      h.wire.mock.calls[0][5]({ type: 'touka_stream_started', session_id: 's1', stream_id: 'stream',
        user_parent_id: state.tree.messages[state.assistantMsgId].parentId, assistant_node_id: state.assistantMsgId });
      h.wire.mock.calls[0][2]('partial');
    });
    h.fetch.mockClear().mockResolvedValue({ ok: true, json: async () => ({ status: 'success' }) });
    await act(async () => { result.current.stopStream('s1'); });
    expect(h.fetch.mock.calls[0][1]).toBe('/v1/ui/sessions/s1/stop?stream_id=stream');
    expect(h.fetch.mock.calls[0][2]).toMatchObject({ requestScope: h.scope, method: 'POST' });
    expect(result.current.getStream('s1')?.tree.messages[state.assistantMsgId].content).toBe('partial');
    expect(result.current.activeCount).toBe(0);
  });
  it.each([true, false])('stop before started resolves the exact admitted coordinates; matching=%s', async matching => {
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream(params()); });
    const state = result.current.getStream('s1')!;
    const started = { type: 'touka_stream_started', session_id: 's1', stream_id: 'stream',
      user_parent_id: state.tree.messages[state.assistantMsgId].parentId,
      assistant_node_id: matching ? state.assistantMsgId : 'different-turn' };
    h.fetch.mockClear().mockResolvedValue({ ok: true, status: 200, json: async () => ({ state: {
      session_id: 's1', stream_id: 'stream', status: 'active', last_seq: 0, started_at: 1, started, terminal: null,
    } }) });
    await act(async () => { result.current.stopStream('s1'); result.current.stopStream('s1'); });
    expect(h.fetch.mock.calls.filter(([, path]) => path.endsWith('/stream/state'))).toHaveLength(1);
    const stops = h.fetch.mock.calls.filter(([, , options]) => options?.method === 'POST');
    expect(stops).toHaveLength(matching ? 1 : 0);
    if (matching) expect(stops[0][1]).toBe('/v1/ui/sessions/s1/stop?stream_id=stream');
    expect(h.complete).toHaveBeenCalledTimes(1);
    expect(state.controller.signal.aborted).toBe(true);
    expect(result.current.activeCount).toBe(0);
  });
  it('stop before any stream is registered still tells the server to stop this conversation', async () => {
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream(params()); });
    const state = result.current.getStream('s1')!;
    h.fetch.mockClear().mockResolvedValue({ ok: true, status: 200, json: async () => ({ state: null }) });
    await act(async () => { result.current.stopStream('s1'); });
    const stops = h.fetch.mock.calls.filter(([, , options]) => options?.method === 'POST');
    expect(stops).toHaveLength(1);
    expect(stops[0][1]).toBe('/v1/ui/sessions/s1/stop');
    expect(stops[0][2]).toMatchObject({ requestScope: h.scope, method: 'POST' });
    expect(state.controller.signal.aborted).toBe(true);
    expect(result.current.activeCount).toBe(0);
  });
  it.each([
    ['the conversation cannot be read yet (incognito)', () => ({ ok: false, status: 403, json: async () => ({}) })],
    ['only the previous turn\'s finished stream is left', () => ({ ok: true, status: 200, json: async () => ({ state: {
      session_id: 's1', stream_id: 'old', status: 'done', last_seq: 3, started_at: 1, terminal: null,
      started: { type: 'touka_stream_started', session_id: 's1', stream_id: 'old', user_parent_id: 'old-user', assistant_node_id: 'old-reply' },
    } }) })],
  ])('stop with no running stream still sends the conversation stop when %s', async (_case, stateResponse) => {
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream(params()); });
    h.fetch.mockClear().mockImplementation(async (_origin: string, path: string) =>
      path.endsWith('/stream/state') ? stateResponse() : { ok: true, status: 200, json: async () => ({ status: 'success' }) });
    await act(async () => { result.current.stopStream('s1'); });
    const stops = h.fetch.mock.calls.filter(([, , options]) => options?.method === 'POST');
    expect(stops.map(([, path]) => path)).toEqual(['/v1/ui/sessions/s1/stop']);
  });
  it.each([true, false])('a stream that starts between reading and stopping is stopped by name only if it is this turn; matching=%s', async matching => {
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream(params()); });
    const state = result.current.getStream('s1')!;
    const started = { type: 'touka_stream_started', session_id: 's1', stream_id: 'stream',
      user_parent_id: state.tree.messages[state.assistantMsgId].parentId,
      assistant_node_id: matching ? state.assistantMsgId : 'different-turn' };
    let reads = 0;
    h.fetch.mockClear().mockImplementation(async (_origin: string, path: string) => {
      if (path.endsWith('/stream/state')) {
        reads += 1;
        return { ok: true, status: 200, json: async () => ({ state: reads === 1 ? null : {
          session_id: 's1', stream_id: 'stream', status: 'active', last_seq: 0, started_at: 1, started, terminal: null,
        } }) };
      }
      return path.endsWith('/stop') ? { ok: false, status: 404, json: async () => ({}) }
        : { ok: true, status: 200, json: async () => ({ status: 'success' }) };
    });
    await act(async () => { result.current.stopStream('s1'); });
    const stops = h.fetch.mock.calls.filter(([, , options]) => options?.method === 'POST');
    expect(stops.map(([, path]) => path)).toEqual(matching
      ? ['/v1/ui/sessions/s1/stop', '/v1/ui/sessions/s1/stop?stream_id=stream']
      : ['/v1/ui/sessions/s1/stop']);
  });
});
