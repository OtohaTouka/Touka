/**
 * ADR-0249（R-4 ② HIGH）：回合正常結束後自動送出未送達的補充，新的 user 節點必須掛在剛答完的
 * assistant 節點下面，送給模型的歷史要包含剛答完的那一問一答（不能分岔）。
 *
 * 用真的 ChatProvider＋StreamRegistryProvider，只 mock 對話清單、模型與連線（仿照 reviewer 的探針）。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { StreamRegistryProvider } from '@/lib/providers/StreamRegistryProvider';
import { ChatProvider, useChat } from '@/lib/providers/ChatProvider';
import { useInterjectionComposer } from '@/hooks/useInterjectionComposer';
import { useInterjectionStore } from '@/lib/chat/interjectionStore';
import type { Session, SaveResult, StreamStartedEvent, StreamTerminalEvent } from '@/lib/types';
import type { SessionChange } from '@/lib/session_persistence';

const mocks = vi.hoisted(() => ({ syncSession: vi.fn() }));

let sessionsArr: Session[] = [];

vi.mock('@/lib/providers/ModelProvider', () => ({
  useModels: () => ({ availableModels: [{ id: 'm1', is_agent: false, status: 'online' }], selectedModel: 'm1',
    setSelectedModel: vi.fn() }),
}));
vi.mock('@/lib/providers/SessionListProvider', () => ({
  useSessionList: () => ({
    sessions: sessionsArr,
    setSessions: vi.fn(),
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
  useDebug: () => ({ registerSimHandlers: vi.fn(), setLastRequestPayload: vi.fn(), setIsSimulationMode: vi.fn() }),
}));
vi.mock('@/hooks/useArtifactExtractor', () => ({
  useArtifactExtractor: () => ({ processStreamChunk: vi.fn(), finalizeStream: vi.fn() }),
}));
vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: { getState: () => ({ setManuallyClosed: vi.fn(), setActiveTab: vi.fn(), setIsOpen: vi.fn() }) },
}));
vi.mock('@/lib/mentions', () => ({ resolveMentions: vi.fn((v: string) => Promise.resolve(v)) }));
vi.mock('@/lib/error_handler', () => ({
  logSSOTError: vi.fn(), errorMsg: vi.fn(() => 'e'), getErrorMessage: vi.fn(async () => 'e'),
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), warning: vi.fn() } }));

interface Capture {
  onChunk: (chunk: string) => void;
  onDone: (terminal: StreamTerminalEvent) => Promise<void>;
  started: StreamStartedEvent;
  messages: { role: string; content: unknown }[];
}
const captures: Capture[] = [];

vi.mock('@/lib/connections', () => ({
  getActiveConnection: () => ({ protocol: 'openai' }),
  getActiveProxyUrl: () => 'http://localhost:8000',
  captureRequestScope: () => ({ proxyOrigin: 'http://localhost:8000', authenticatedPrincipalId: 'a', authEpoch: 1 }),
  isRequestScopeCurrent: () => true,
  fetchTouka: vi.fn(async () => ({ ok: true, json: async () => ({ state: null, streams: [] }) })),
  fetchChatStream: vi.fn((messages: Capture['messages'],
    opts: { session_id?: string; ui_persistence: { user_parent_id: string; assistant_node_id: string } },
    onChunk: Capture['onChunk'], onDone: Capture['onDone'], _onError: unknown,
    onEvent: (event: Record<string, unknown>) => void) => {
    const started: StreamStartedEvent = { type: 'touka_stream_started', session_id: opts.session_id!,
      stream_id: `stream-${captures.length}`, user_parent_id: opts.ui_persistence.user_parent_id,
      assistant_node_id: opts.ui_persistence.assistant_node_id };
    captures.push({ onChunk, onDone, started, messages });
    onEvent(started);
    return new Promise<void>(() => {});
  }),
}));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <StreamRegistryProvider><ChatProvider>{children}</ChatProvider></StreamRegistryProvider>
);

function useChatWithComposer() {
  const chat = useChat();
  useInterjectionComposer({
    sessionId: chat.activeSessionId, isSending: chat.isSending, disabled: false,
    inputValue: chat.inputValue, setInputValue: chat.setInputValue,
    send: chat.handleSend, stop: chat.handleStopGeneration,
  });
  return chat;
}

type Tree = { messages: Record<string, { parentId: string | null; role: string; content: string }> };

async function answerWithLeftover(question: string, answer: string, leftover: string) {
  const { result } = renderHook(() => useChatWithComposer(), { wrapper });
  act(() => { result.current.setActiveSessionId('sess-A'); });
  await act(async () => { await result.current.handleSend(question); });
  const first = captures[0];
  act(() => {
    useInterjectionStore.getState().add('sess-A', { id: 'loc-1', text: leftover, urgent: false,
      status: 'undelivered', step: null, created_at: 1, local: true });
  });
  act(() => { first.onChunk(answer); });
  await act(async () => {
    await first.onDone({ ...first.started, type: 'touka_stream_terminal', generation: 'completed',
      persistence: { status: 'saved', representation: 'text', revision: 'text' } });
  });
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  expect(captures).toHaveLength(2);
  const second = captures[1];
  const tree = sessionsArr[0].messages as unknown as Tree;
  return {
    firstAssistant: first.started.assistant_node_id,
    autoUser: tree.messages[second.started.user_parent_id],
    sent: second.messages.map(m => `${m.role}:${String(m.content)}`),
  };
}

describe('回合結束自動送出：掛在剛答完的 assistant 節點下', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    captures.length = 0;
    useInterjectionStore.setState({ bySession: {} });
    mocks.syncSession.mockResolvedValue({ status: 'saved', representation: 'full', revision: 'full' });
  });

  it('新對話', async () => {
    sessionsArr = [{ id: 'sess-A', title: 'A', messages: [], updatedAt: 1, agent_id: 'm1' }];
    const { firstAssistant, autoUser, sent } = await answerWithLeftover('first question', 'answer one', 'follow-up');
    expect(autoUser.content).toBe('follow-up');
    expect(autoUser.parentId).toBe(firstAssistant);
    expect(sent).toEqual(['user:first question', 'assistant:answer one', 'user:follow-up']);
  });

  it('已有歷史', async () => {
    sessionsArr = [{ id: 'sess-A', title: 'A', updatedAt: 1, agent_id: 'm1', messages: {
      currentId: 'a0', messages: {
        u0: { id: 'u0', parentId: null, childrenIds: ['a0'], role: 'user', content: 'q0' },
        a0: { id: 'a0', parentId: 'u0', childrenIds: [], role: 'assistant', content: 'r0' },
      } } as unknown as Session['messages'] }];
    const { firstAssistant, autoUser, sent } = await answerWithLeftover('q1', 'r1', 'more');
    expect(autoUser.parentId).toBe(firstAssistant);
    expect(sent).toEqual(['user:q0', 'assistant:r0', 'user:q1', 'assistant:r1', 'user:more']);
  });
});
