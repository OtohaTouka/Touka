/**
 * ADR-0081 — StreamRegistry 重整接回測試（先 Red 後 Green）。
 *
 * - startStream 必須「先持久化 user 回合」（syncSession 含 user 節點的樹）再開串流
 * - resumeActiveStreams：依 GET /streams/active 清單為每條進行中串流重建 registry entry，
 *   以 resumeChatStream 從頭重播重建樹；無終態的 EOF 保留未確認富樹
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { StreamRegistryProvider, useStreamRegistry } from '@/lib/providers/StreamRegistryProvider';
import { TreeHistory } from '@/lib/tree_history';
import type { Session, SaveResult } from '@/lib/types';
import type { SessionChange } from '@/lib/session_persistence';

const mocks = vi.hoisted(() => ({
  fetchChatStream: vi.fn(),
  resumeChatStream: vi.fn(),
  fetchTouka: vi.fn(),
  syncSession: vi.fn(),
  registerSimHandlers: vi.fn(),
  completeProducer: vi.fn(),
  prepareResume: vi.fn(),
}));

let sessionsArr: Session[] = [];

vi.mock('@/lib/connections', () => ({
  fetchChatStream: mocks.fetchChatStream,
  resumeChatStream: mocks.resumeChatStream,
  fetchTouka: mocks.fetchTouka,
  getActiveProxyUrl: () => 'http://localhost:8000',
  captureRequestScope: () => ({ proxyOrigin: 'http://localhost:8000', authenticatedPrincipalId: 'a', authEpoch: 1 }),
  isRequestScopeCurrent: () => true,
}));

vi.mock('@/lib/providers/SessionListProvider', () => ({
  useSessionList: () => ({
    sessions: sessionsArr,
    setSessions: vi.fn((upd: unknown) => {
      sessionsArr = typeof upd === 'function'
        ? (upd as (p: typeof sessionsArr) => typeof sessionsArr)(sessionsArr)
        : (upd as typeof sessionsArr);
    }),
    syncSession: mocks.syncSession,
    getSession: (id: string) => sessionsArr.find(session => session.id === id),
    updateSession: (id: string, change: SessionChange) => {
      const current = sessionsArr.find(session => session.id === id)!;
      const next = change(current);
      expect(change(current)).toEqual(next);
      sessionsArr = sessionsArr.map(session => session.id === id ? next : session);
      return mocks.syncSession(next);
    },
    beginProducer: (id: string) => ({ sessionId: id }),
    completeProducer: mocks.completeProducer,
    prepareResume: mocks.prepareResume,
    getPersistence: () => ({ status: 'unknown', error_code: 1307 }),
  }),
}));

vi.mock('@/lib/providers/DebugProvider', () => ({
  useDebug: () => ({ registerSimHandlers: mocks.registerSimHandlers }),
}));

vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: { getState: () => ({ setActiveTab: vi.fn(), setIsOpen: vi.fn() }) },
}));

vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), warning: vi.fn() } }));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <StreamRegistryProvider>{children}</StreamRegistryProvider>
);

const emptyTree: TreeHistory = { messages: {}, currentId: null };

function startParams(sessionId: string) {
  return {
    sessionId, baseTree: emptyTree, userText: 'hello', resolvedInput: 'hello',
    attachment: null, parentId: null, model: 'm1', protocol: 'openai' as const,
    proxyUrl: 'http://localhost:8000', authLevel: 'Manual', activeSkills: [] as string[],
  };
}

describe('StreamRegistry × ADR-0081', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionsArr = [];
    mocks.syncSession.mockResolvedValue({ status: 'saved', representation: 'full', revision: 'user' });
    mocks.prepareResume.mockImplementation(async (id: string) => sessionsArr.find(session => session.id === id));
    mocks.completeProducer.mockImplementation(async (token: { sessionId: string }, change: SessionChange, result: SaveResult) => {
      const previous = sessionsArr.find(session => session.id === token.sessionId)!;
      expect(change(previous)).toEqual(change(previous));
      sessionsArr = sessionsArr.map(session => session.id === token.sessionId ? change(session) : session);
      return result;
    });
    mocks.fetchChatStream.mockReturnValue(new Promise<void>(() => {}));
    mocks.resumeChatStream.mockReturnValue(new Promise<void>(() => {}));
    // 預設：無進行中串流
    mocks.fetchTouka.mockResolvedValue({ ok: true, json: async () => ({ streams: [] }) });
  });

  it('startStream 先 syncSession 持久化 user 回合（含 user 節點、currentId=user）再開串流', async () => {
    sessionsArr = [{ id: 'sess-A', title: 'A', messages: [], updatedAt: 1, agent_id: 'm1' }];
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });

    await act(async () => { await result.current.startStream(startParams('sess-A')); });

    expect(mocks.syncSession).toHaveBeenCalled();
    const synced = mocks.syncSession.mock.calls[0][0];
    expect(synced.id).toBe('sess-A');
    const tree = synced.messages as { messages: Record<string, { role: string }>; currentId: string };
    const roles = Object.values(tree.messages).map(m => m.role);
    expect(roles).toEqual(['user']); // 只有 user 回合，不含 assistant 佔位
    expect(tree.messages[tree.currentId].role).toBe('user');

    // 順序：user 回合持久化先於串流開始
    const syncOrder = mocks.syncSession.mock.invocationCallOrder[0];
    const streamOrder = mocks.fetchChatStream.mock.invocationCallOrder[0];
    expect(syncOrder).toBeLessThan(streamOrder);
  });

  it('resumeActiveStreams：依 active 清單重建樹，無終態的 EOF 保留富樹並解除 active', async () => {
    // 後端 session 已含 user 回合（startStream 先行持久化的結果）
    sessionsArr = [{
      id: 'sess-R', title: 'R', agent_id: 'm1', updatedAt: 1,
      messages: {
        messages: { u1: { id: 'u1', parentId: null, childrenIds: [], role: 'user', content: '重整前的問題' } },
        currentId: 'u1',
      },
      revision: 'user',
    }];
    mocks.fetchTouka.mockResolvedValue({
      ok: true,
      json: async () => ({ streams: [{ session_id: 'sess-R', stream_id: 'st1', status: 'active', last_seq: 3,
        started_at: 1, terminal: null, started: { type: 'touka_stream_started', session_id: 'sess-R', stream_id: 'st1',
          user_parent_id: 'u1', assistant_node_id: 'a1' } }] }),
    });

    const { result } = renderHook(() => useStreamRegistry(), { wrapper });

    await waitFor(() => expect(mocks.resumeChatStream).toHaveBeenCalledTimes(1));

    // 接回呼叫：對 sess-R 從頭（-1）重播
    const callArgs = mocks.resumeChatStream.mock.calls[0];
    expect(callArgs[1]).toBe('sess-R');
    expect(callArgs[2]).toBe(-1);
    expect(mocks.prepareResume).toHaveBeenCalledWith('sess-R');
    expect(callArgs[9]).toMatchObject({ started: { stream_id: 'st1', user_parent_id: 'u1', assistant_node_id: 'a1' },
      requestScope: { authenticatedPrincipalId: 'a' } });

    // entry 已建立且可見於 streamingSessionIds（側欄 spinner 來源）
    await waitFor(() => expect(result.current.streamingSessionIds).toContain('sess-R'));
    const entry = result.current.getStream('sess-R')!;
    expect(entry.tree.messages.u1.content).toBe('重整前的問題');

    // 重播 chunk 重建 assistant 內容
    const onChunk = callArgs[3] as (c: string) => void;
    act(() => { onChunk('恢復的'); onChunk('內容'); });
    const rebuilt = result.current.getStream('sess-R')!;
    const assistant = Object.values(rebuilt.tree.messages).find(m => m.role === 'assistant')!;
    expect(assistant.id).toBe('a1');
    expect(assistant.content).toBe('恢復的內容');

    // EOF 不是保存證明；保留同一棵富樹，不自動 POST。
    const onDone = callArgs[4] as () => Promise<void>;
    await act(async () => { await onDone(); });
    expect(mocks.completeProducer).toHaveBeenCalledOnce();
    expect(mocks.syncSession).not.toHaveBeenCalled();
    expect(result.current.getStream('sess-R')?.persistence?.status).toBe('unknown');
    expect(result.current.getStream('sess-R')?.tree.messages[assistant.id].content).toBe('恢復的內容');
    expect(result.current.streamingSessionIds).not.toContain('sess-R');
  });

  it('resumeActiveStreams 對已有 entry 的 session 不重複接回', async () => {
    sessionsArr = [{ id: 'sess-A', title: 'A', messages: [], updatedAt: 1, agent_id: 'm1' }];
    mocks.fetchTouka.mockResolvedValue({
      ok: true,
      json: async () => ({ streams: [{ session_id: 'sess-A', stream_id: 's1', status: 'active', last_seq: 0 }] }),
    });

    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    // 先以一般路徑開了串流（entry 已存在）
    await act(async () => { await result.current.startStream(startParams('sess-A')); });

    await act(async () => { await new Promise(r => setTimeout(r, 20)); });
    expect(mocks.resumeChatStream).not.toHaveBeenCalled();
  });
  it('selecting one completed session queries its retained terminal once without active polling', async () => {
    sessionsArr = [{ id: 'completed', title: 'done', updatedAt: 1, revision: 'text', messages: {
      messages: { u: { id: 'u', parentId: null, childrenIds: [], role: 'user', content: 'q' } }, currentId: 'u',
    } }];
    const started = { type: 'touka_stream_started', session_id: 'completed', stream_id: 's', user_parent_id: 'u', assistant_node_id: 'a' };
    const state = { session_id: 'completed', stream_id: 's', status: 'done', last_seq: 4, started_at: 1, started,
      terminal: { ...started, type: 'touka_stream_terminal', generation: 'completed',
        persistence: { status: 'saved', representation: 'text', revision: 'text' } } };
    mocks.fetchTouka.mockImplementation(async (_url: string, path: string) => ({ ok: true, status: 200,
      json: async () => path.endsWith('/state') ? { state } : { streams: [] } }));
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.checkSessionStream('completed'); await result.current.checkSessionStream('completed'); });
    expect(mocks.fetchTouka.mock.calls.filter(([, path]) => path.endsWith('/state'))).toHaveLength(1);
    expect(mocks.resumeChatStream).toHaveBeenCalledTimes(1);
    expect(result.current.getStream('completed')?.assistantMsgId).toBe('a');
  });
  it('six simultaneous canonical snapshot completions admit at most five active streams', async () => {
    sessionsArr = Array.from({ length: 6 }, (_, i) => ({ id: `s${i}`, title: 't', updatedAt: 1, messages: {
      messages: { u: { id: 'u', parentId: null, childrenIds: [], role: 'user' as const, content: 'q' } }, currentId: 'u' } }));
    const streams = sessionsArr.map(session => ({ session_id: session.id, stream_id: `stream-${session.id}`,
      status: 'active', last_seq: 0, started_at: 1, terminal: null,
      started: { type: 'touka_stream_started', session_id: session.id, stream_id: `stream-${session.id}`,
        user_parent_id: 'u', assistant_node_id: 'a' } }));
    mocks.fetchTouka.mockResolvedValue({ ok: true, json: async () => ({ streams }) });
    const finishes: (() => void)[] = [];
    mocks.prepareResume.mockImplementation((id: string) => new Promise<Session | undefined>(resolve => {
      finishes.push(() => resolve(sessionsArr.find(session => session.id === id)));
    }));
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await waitFor(() => expect(finishes).toHaveLength(6));
    await act(async () => { finishes.forEach(finish => finish()); });
    expect(result.current.activeCount).toBe(5);
    expect(mocks.resumeChatStream).toHaveBeenCalledTimes(5);
  });
});
