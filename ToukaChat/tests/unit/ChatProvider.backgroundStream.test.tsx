/**
 * ADR-0080 背景續跑：ChatProvider × StreamRegistry 整合測試。
 *
 * 驗證「切走後背景續跑、切回看到結果」端到端：
 * - 切換對話不中止串流（離開的對話仍在 registry）
 * - per-session isSending（切到沒在跑的對話 → false）
 * - 背景 chunk 進原對話、不污染切換後的顯示
 * - 切回顯示累積內容
 * - 完成寫回 session（syncSession）並清除 registry entry
 *
 * 取代上一輪 review #1 的 abort-based 測試（switchAbort/pollution）——
 * 防污染改由 per-session 隔離達成。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { StreamRegistryProvider } from '@/lib/providers/StreamRegistryProvider';
import { ChatProvider, useChat } from '@/lib/providers/ChatProvider';
import type { Session, SaveResult, StreamStartedEvent, StreamTerminalEvent } from '@/lib/types';
import type { SessionChange } from '@/lib/session_persistence';

const mocks = vi.hoisted(() => ({ syncSession: vi.fn(), simHandlers: { current: null as null | { onChunk: (c: string) => void } } }));

let sessionsArr: Session[] = [];

vi.mock('@/lib/providers/ModelProvider', () => ({
  useModels: () => ({ availableModels: [{ id: 'm1', is_agent: false, status: 'online' }], selectedModel: 'm1', setSelectedModel: vi.fn() }),
}));
vi.mock('@/lib/providers/SessionListProvider', () => ({
  useSessionList: () => ({
    sessions: sessionsArr,
    setSessions: vi.fn((upd: unknown) => {
      sessionsArr = typeof upd === 'function' ? (upd as (p: typeof sessionsArr) => typeof sessionsArr)(sessionsArr) : (upd as typeof sessionsArr);
    }),
    syncSession: mocks.syncSession,
    getSession: (id: string) => sessionsArr.find(session => session.id === id),
    getPersistence: () => undefined,
    updateSession: (id: string, change: SessionChange) => {
      const next = change(sessionsArr.find(session => session.id === id)!);
      sessionsArr = sessionsArr.map(session => session.id === id ? next : session);
      return mocks.syncSession(next);
    },
    beginProducer: (id: string) => ({ sessionId: id }),
    completeProducer: async (token: { sessionId: string }, change: SessionChange, result: SaveResult) => {
      const next = change(sessionsArr.find(session => session.id === token.sessionId)!);
      sessionsArr = sessionsArr.map(session => session.id === token.sessionId ? next : session);
      if (result.status !== 'saved') return result;
      return mocks.syncSession(next);
    },
    refreshSessions: vi.fn(async () => sessionsArr),
  }),
}));
vi.mock('@/lib/providers/DebugProvider', () => ({
  useDebug: () => ({
    registerSimHandlers: vi.fn((h: { onChunk: (c: string) => void }) => { mocks.simHandlers.current = h; }),
    setLastRequestPayload: vi.fn(), setIsSimulationMode: vi.fn(),
  }),
}));
vi.mock('@/hooks/useArtifactExtractor', () => ({
  useArtifactExtractor: () => ({ processStreamChunk: vi.fn(), finalizeStream: vi.fn() }),
}));
vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: { getState: () => ({ setManuallyClosed: vi.fn(), setActiveTab: vi.fn(), setIsOpen: vi.fn() }) },
}));
vi.mock('@/lib/mentions', () => ({ resolveMentions: vi.fn((v: string) => Promise.resolve(v)) }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));
vi.mock('@/lib/error_codes', () => ({ ERRORS: { NETWORK_CONNECTION_ERROR: { msg: 'net' } } }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), warning: vi.fn() } }));

interface Cap { onChunk: (c: string) => void; onDone: (terminal: StreamTerminalEvent) => Promise<void>; sessionId: string; started: StreamStartedEvent }
const captures: Cap[] = [];

vi.mock('@/lib/connections', () => ({
  getActiveConnection: () => ({ protocol: 'openai' }),
  getActiveProxyUrl: () => 'http://localhost:8000',
  captureRequestScope: () => ({ proxyOrigin: 'http://localhost:8000', authenticatedPrincipalId: 'a', authEpoch: 1 }),
  isRequestScopeCurrent: () => true,
  fetchTouka: vi.fn(async () => ({ ok: true, json: async () => ({ state: null, streams: [] }) })),
  fetchChatStream: vi.fn((_m: unknown, opts: { session_id?: string; ui_persistence: { user_parent_id: string; assistant_node_id: string } }, onChunk: Cap['onChunk'], onDone: Cap['onDone'], _onError: unknown, onEvent: (event: Record<string, unknown>) => void) => {
    const started: StreamStartedEvent = { type: 'touka_stream_started', session_id: opts.session_id!, stream_id: `stream-${opts.session_id}`,
      user_parent_id: opts.ui_persistence.user_parent_id, assistant_node_id: opts.ui_persistence.assistant_node_id };
    captures.push({ onChunk, onDone, sessionId: opts.session_id || '', started });
    onEvent(started);
    return new Promise<void>(() => {});
  }),
}));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <StreamRegistryProvider><ChatProvider>{children}</ChatProvider></StreamRegistryProvider>
);

const assistantContent = (history: { messages: Record<string, { role: string; content: string }> }) =>
  Object.values(history.messages).find(m => m.role === 'assistant')?.content;

describe('ChatProvider × StreamRegistry 背景續跑整合（ADR-0080）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    captures.length = 0;
    mocks.simHandlers.current = null;
    mocks.syncSession.mockResolvedValue({ status: 'saved', representation: 'full', revision: 'full' });
    sessionsArr = [
      { id: 'sess-A', title: 'A', messages: [], updatedAt: 1, agent_id: 'm1' },
      { id: 'sess-B', title: 'B', messages: [], updatedAt: 2, agent_id: 'm1' },
    ];
  });

  it('切走後背景續跑、切回看到累積、完成寫回', async () => {
    const { result } = renderHook(() => useChat(), { wrapper });

    // 進入 A 並送出
    act(() => { result.current.setActiveSessionId('sess-A'); });
    await act(async () => { await result.current.handleSend('hello-A'); });
    expect(captures.find(c => c.sessionId === 'sess-A')).toBeTruthy();
    expect(result.current.isSending).toBe(true);

    const capA = captures.find(c => c.sessionId === 'sess-A')!;
    act(() => { capA.onChunk('部分A'); });
    expect(assistantContent(result.current.history)).toBe('部分A');

    // 切到 B：A 不中止、仍在 registry；B 沒在跑
    await act(async () => { await result.current.handleSwitchSession('sess-B'); });
    expect(result.current.streamingSessionIds).toContain('sess-A');
    expect(result.current.isSending).toBe(false);

    // A 在背景續跑：chunk 進 A，B 顯示不受影響
    act(() => { capA.onChunk('更多A'); });
    expect(assistantContent(result.current.history)).toBeUndefined(); // B 無 assistant 節點

    // 切回 A：看到累積內容
    await act(async () => { await result.current.handleSwitchSession('sess-A'); });
    expect(result.current.isSending).toBe(true);
    expect(assistantContent(result.current.history)).toBe('部分A更多A');

    // 完成：寫回 session（syncSession）、清除 registry entry、isSending 轉 false
    await act(async () => { await capA.onDone({ ...capA.started, type: 'touka_stream_terminal', generation: 'completed',
      persistence: { status: 'saved', representation: 'text', revision: 'text' } }); });
    expect(mocks.syncSession).toHaveBeenCalledTimes(2);
    const syncedA = mocks.syncSession.mock.calls[1][0];
    expect(syncedA).toBeTruthy();
    expect(syncedA.id).toBe('sess-A');
    expect(assistantContent(syncedA.messages)).toBe('部分A更多A');
    expect(result.current.streamingSessionIds).not.toContain('sess-A');
    expect(result.current.isSending).toBe(false);
  });

  it('兩個對話可同時背景生成（per-session 隔離，不互相污染）', async () => {
    const { result } = renderHook(() => useChat(), { wrapper });

    act(() => { result.current.setActiveSessionId('sess-A'); });
    await act(async () => { await result.current.handleSend('a'); });
    await act(async () => { await result.current.handleSwitchSession('sess-B'); });
    await act(async () => { await result.current.handleSend('b'); });

    expect(result.current.streamingSessionIds).toEqual(expect.arrayContaining(['sess-A', 'sess-B']));

    const capA = captures.find(c => c.sessionId === 'sess-A')!;
    const capB = captures.find(c => c.sessionId === 'sess-B')!;
    act(() => { capA.onChunk('AAA'); });
    act(() => { capB.onChunk('BBB'); });

    // 當前在 B，顯示 BBB
    expect(assistantContent(result.current.history)).toBe('BBB');
    // 切回 A，顯示 AAA（未被 B 污染）
    await act(async () => { await result.current.handleSwitchSession('sess-A'); });
    expect(assistantContent(result.current.history)).toBe('AAA');
  });
});
