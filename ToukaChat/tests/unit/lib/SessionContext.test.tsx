import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, renderHook, screen, act, waitFor, fireEvent } from '@testing-library/react';
import React from 'react';
import { SessionProvider } from '@/lib/SessionContext';
import { useChat } from '@/lib/providers/ChatProvider';
import { useSessionList } from '@/lib/providers/SessionListProvider';
import * as connections from '@/lib/connections';
import { SessionPersistenceNotice } from '@/components/chat/SessionPersistenceNotice';
import type { Session } from '@/lib/types';
import { useDebug } from '@/lib/providers/DebugProvider';
import { useSessionActions } from '@/hooks/useSessionActions';
import { migrateFlatToTree } from '@/lib/tree_history';
import i18n from '@/lib/i18n';

const connectionState = vi.hoisted(() => ({ saveOk: true, userSaved: false, failAssistantSave: false,
  remote: new Map<string, Session>(), epoch: 1, listeners: new Set<() => void>() }));

// Mock connections.ts functions
vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  getActiveConnection: vi.fn(() => ({ protocol: 'openai' })),
  captureRequestScope: () => ({ proxyOrigin: 'http://localhost:8000', authenticatedPrincipalId: 'a', authEpoch: connectionState.epoch }),
  isRequestScopeCurrent: (scope: { authEpoch: number } | null) => scope?.authEpoch === connectionState.epoch,
  subscribeCredentialScope: (callback: () => void) => { connectionState.listeners.add(callback); return () => { connectionState.listeners.delete(callback); }; },
  setKnownAgentIds: vi.fn(),
  fetchTouka: vi.fn(async (_proxy: string, path: string, options?: RequestInit) => {
    let body: unknown = [];
    let userSaved = false;
    if (path === '/v1/models') body = { data: [{ id: 'gpt-4o', name: 'GPT-4o' }] };
    if (path === '/v1/ui/streams/active') body = { streams: [] };
    if (path.endsWith('/stream/state')) body = { state: null };
    if (path.endsWith('?snapshot=true')) {
      const id = decodeURIComponent(path.split('/').at(-1)!.split('?')[0]);
      const session = connectionState.remote.get(id);
      body = session ? { exists: true, revision: 'snapshot', session: {
        is_home: false, home_for_agent: null, agent_id: null, project_id: null, auth_level: 'Manual',
        active_mcp_servers: [], active_skills: [], context_summary: null, last_activity_at: 1, ...session,
      } } : { exists: false, revision: 'absent', session: null };
    }
    if (path.startsWith('/v1/ui/sessions/') && options?.method === 'POST') {
      const session = JSON.parse(options.body as string);
      userSaved = Object.values(session.messages?.messages ?? {}).some(node => (node as { role: string }).role === 'user');
      const hasAssistant = Object.values(session.messages?.messages ?? {}).some(node => (node as { role: string }).role === 'assistant');
      const saved = connectionState.saveOk && !(connectionState.failAssistantSave && hasAssistant);
      if (saved) connectionState.remote.set(session.id, session);
      body = { status: 'success', session_id: decodeURIComponent(path.split('/').at(-1)!),
        persistence: saved ? { status: 'saved', representation: 'full', revision: 'synthetic-revision' } :
          { status: 'unknown', error_code: 1302 } };
    }
    return { ok: true, status: 200, json: async () => {
      if (userSaved && connectionState.saveOk) connectionState.userSaved = true;
      return body;
    } };
  }),
  fetchChatStream: vi.fn(async (messages, config, onChunk, onDone, _onError, onEvent) => {
    expect(connectionState.userSaved).toBe(true);
    const coords = { session_id: config.session_id, stream_id: 'synthetic-stream',
      user_parent_id: config.ui_persistence.user_parent_id, assistant_node_id: config.ui_persistence.assistant_node_id };
    onEvent({ type: 'touka_stream_started', ...coords });
    onChunk('Hello');
    onChunk(' World');
    onEvent({ type: 'tool_start', id: 'tool', tool: 'synthetic', args: {} });
    onEvent({ type: 'tool_result', id: 'tool', tool: 'synthetic', status: 'success', result: 'rich' });
    await onDone({ type: 'touka_stream_terminal', ...coords, generation: 'completed',
      persistence: { status: 'saved', representation: 'text', revision: 'text' } });
  })
}));

// Mock artifacts
vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: {
    getState: () => ({
      clearArtifacts: vi.fn(),
      loadArtifacts: vi.fn(),
      addArtifact: vi.fn(),
      setManuallyClosed: vi.fn(),
      setCurrentArtifact: vi.fn(),
      updateArtifact: vi.fn()
    })
  }
}));

// Mock mentions
vi.mock('@/lib/mentions', () => ({
  resolveMentions: vi.fn((text) => Promise.resolve(text))
}));

// Mock parsers
vi.mock('@/lib/parsers/artifact-extractor', () => ({
  extractArtifacts: vi.fn(() => [])
}));

// Mock localStorage
const localStorageMock = (() => {
  let store: Record<string, string> = {};
  return {
    getItem: vi.fn((key: string) => store[key] || null),
    setItem: vi.fn((key: string, value: string) => {
      store[key] = value.toString();
    }),
    clear: vi.fn(() => {
      store = {};
    }),
    removeItem: vi.fn((key: string) => {
      delete store[key];
    })
  };
})();
Object.defineProperty(window, 'localStorage', { value: localStorageMock });

// Fetch mock
const globalFetch = vi.fn();
global.fetch = globalFetch as any;

const TestComponent = () => {
  const { sessions } = useSessionList();
  const debug = useDebug();
  const { 
    activeSessionId, handleNewSession, 
    inputValue, setInputValue, handleSend, isSending, history, handleSetProject
  } = useChat();
  
  return (
    <div>
      <div data-testid="active-id">{activeSessionId}</div>
      <div data-testid="sessions-count">{sessions.length}</div>
      <div data-testid="sending-status">{isSending ? 'Sending' : 'Ready'}</div>
      <div data-testid="history">{JSON.stringify(history)}</div>
      <input 
        data-testid="input" 
        value={inputValue} 
        onChange={(e) => setInputValue(e.target.value)} 
      />
      <button data-testid="new-session" onClick={() => handleNewSession()}>New</button>
      <button data-testid="send-btn" onClick={() => handleSend()}>Send</button>
      <button data-testid="clear-project" onClick={() => handleSetProject(activeSessionId, null)}>Clear project</button>
      <button data-testid="debug" onClick={() => debug.setLastRequestPayload({ private: 'A' })}>Debug</button>
      <div data-testid="debug-payload">{JSON.stringify(debug.lastRequestPayload)}</div>
      <SessionPersistenceNotice key={activeSessionId} sessionId={activeSessionId} />
    </div>
  );
};

describe('SessionContext (Baseline)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    connectionState.saveOk = true;
    connectionState.userSaved = false;
    connectionState.failAssistantSave = false;
    connectionState.remote.clear();
    connectionState.epoch = 1;
    connectionState.listeners.clear();
    localStorageMock.clear();
    globalFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve([])
    });
  });

  it('initializes and manages sessions', async () => {
    render(
      <SessionProvider>
        <TestComponent />
      </SessionProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('sessions-count').textContent).toBe('0');
    });

    const newBtn = screen.getByTestId('new-session');
    await act(async () => {
        newBtn.click();
    });

    expect(screen.getByTestId('sessions-count').textContent).toBe('1');
    expect(screen.getByTestId('active-id').textContent).toMatch(/session-/);
  });

  it('handles message sending and updates history', async () => {
    render(
      <SessionProvider>
        <TestComponent />
      </SessionProvider>
    );

    // Create session
    await act(async () => {
      screen.getByTestId('new-session').click();
    });

    // Set input
    const input = screen.getByTestId('input');
    fireEvent.change(input, { target: { value: 'Hello' } });

    // Send
    await act(async () => {
      screen.getByTestId('send-btn').click();
    });

    await waitFor(() => {
      expect(screen.getByTestId('sending-status').textContent).toBe('Ready');
    });
    
    expect(connections.fetchChatStream).toHaveBeenCalledOnce();
    expect(screen.getByTestId('history').textContent).toContain('Hello World');
    const calls = vi.mocked(connections.fetchTouka).mock.calls;
    const userSaveIndex = calls.findIndex(([, path, options]) => {
      if (typeof path !== 'string' || !path.startsWith('/v1/ui/sessions/') || options?.method !== 'POST') return false;
      const session = JSON.parse(options.body as string);
      return Object.values(session.messages.messages ?? {}).some(node => (node as { role: string }).role === 'user');
    });
    expect(userSaveIndex).toBeGreaterThanOrEqual(0);
    expect(vi.mocked(connections.fetchTouka).mock.invocationCallOrder[userSaveIndex])
      .toBeLessThan(vi.mocked(connections.fetchChatStream).mock.invocationCallOrder[0]);
  });

  it('保存結果未知保留輸入且模型呼叫為零', async () => {
    connectionState.saveOk = false;
    render(<SessionProvider><TestComponent /></SessionProvider>);
    await act(async () => { screen.getByTestId('new-session').click(); });
    fireEvent.change(screen.getByTestId('input'), { target: { value: 'unsaved synthetic' } });
    await act(async () => { screen.getByTestId('send-btn').click(); });
    expect(connections.fetchChatStream).not.toHaveBeenCalled();
    expect(screen.getByTestId('input')).toHaveValue('unsaved synthetic');
    expect(screen.getByTestId('sending-status').textContent).toBe('Ready');
  });
  it('rich save failure, readback and save-only retry allow exactly one new model turn', async () => {
    render(<SessionProvider><TestComponent /></SessionProvider>);
    await act(async () => { screen.getByTestId('new-session').click(); });
    connectionState.failAssistantSave = true;
    fireEvent.change(screen.getByTestId('input'), { target: { value: 'first' } });
    await act(async () => { screen.getByTestId('send-btn').click(); });
    expect(connections.fetchChatStream).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('history')).toHaveTextContent('rich');
    await act(async () => { screen.getByRole('button', { name: i18n.t('chat.persistence.recheck') }).click(); });
    expect(connections.fetchChatStream).toHaveBeenCalledTimes(1);
    connectionState.failAssistantSave = false;
    await act(async () => { screen.getByRole('button', { name: i18n.t('chat.persistence.retry') }).click(); });
    expect(connections.fetchChatStream).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('history')).toHaveTextContent('rich');
    fireEvent.change(screen.getByTestId('input'), { target: { value: 'second' } });
    await act(async () => { screen.getByTestId('send-btn').click(); });
    expect(connections.fetchChatStream).toHaveBeenCalledTimes(2);
  });
  it('actual SessionContext resets Debug payload at the credential scope boundary', async () => {
    render(<SessionProvider><TestComponent /></SessionProvider>);
    await act(async () => { screen.getByTestId('debug').click(); });
    expect(screen.getByTestId('debug-payload')).toHaveTextContent('private');
    act(() => { connectionState.epoch++; for (const listener of connectionState.listeners) listener(); });
    expect(screen.getByTestId('debug-payload')).toHaveTextContent('null');
  });
  it('project unlink sends explicit null through the actual writer', async () => {
    render(<SessionProvider><TestComponent /></SessionProvider>);
    await act(async () => { screen.getByTestId('new-session').click(); });
    vi.mocked(connections.fetchTouka).mockClear();
    await act(async () => { screen.getByTestId('clear-project').click(); });
    const call = vi.mocked(connections.fetchTouka).mock.calls.find(([, , options]) => options?.method === 'POST')!;
    expect(JSON.parse(call[2]!.body as string)).toHaveProperty('project_id', null);
  });
  it('a delayed split export cannot shrink a session changed while export was pending', async () => {
    const { result } = renderHook(() => ({ list: useSessionList(), actions: useSessionActions() }), {
      wrapper: ({ children }) => <SessionProvider>{children}</SessionProvider>,
    });
    await act(async () => {});
    const session: Session = { id: 'split', title: 'split', updatedAt: 1, revision: 'original', messages:
      migrateFlatToTree(Array.from({ length: 64 }, (_, index) => ({ id: `n${index}`,
        role: index % 2 ? 'assistant' as const : 'user' as const, content: `synthetic ${index}`,
        parentId: null, childrenIds: [] }))) };
    act(() => result.current.list.setSessions([session]));
    let finish!: (response: Response) => void;
    vi.mocked(connections.fetchTouka).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    let pending!: Promise<void>;
    act(() => { pending = result.current.actions.handleSplitExport(session); });
    await act(async () => { await result.current.list.updateSessionTitle('split', 'changed while exporting'); });
    const preserved = structuredClone(result.current.list.getSession('split'));
    const before = vi.mocked(connections.fetchTouka).mock.calls.filter(([, , options]) => options?.method === 'POST').length;
    await act(async () => { finish({ ok: true } as Response); await pending; });
    expect(result.current.list.getSession('split')).toEqual(preserved);
    expect(vi.mocked(connections.fetchTouka).mock.calls.filter(([, , options]) => options?.method === 'POST')).toHaveLength(before);
  });
  it('a no-revision list snapshot can bind the same content and save a successful split', async () => {
    const { result } = renderHook(() => ({ list: useSessionList(), actions: useSessionActions() }), {
      wrapper: ({ children }) => <SessionProvider>{children}</SessionProvider>,
    });
    await act(async () => {});
    const session: Session = { id: 'split', title: 'split', updatedAt: 1, messages:
      migrateFlatToTree(Array.from({ length: 64 }, (_, index) => ({ id: `n${index}`,
        role: index % 2 ? 'assistant' as const : 'user' as const, content: `synthetic ${index}`,
        parentId: null, childrenIds: [] }))) };
    connectionState.remote.set('split', session);
    act(() => result.current.list.setSessions([session]));
    vi.mocked(connections.fetchTouka).mockReturnValueOnce(Promise.resolve({ ok: true } as Response));
    await act(async () => { await result.current.actions.handleSplitExport(session); });
    expect(Object.keys(migrateFlatToTree(result.current.list.getSession('split')!.messages).messages)).toHaveLength(60);
    expect(result.current.list.getPersistence('split')).toMatchObject({ status: 'saved', representation: 'full' });
  });
});







