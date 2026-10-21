import React from 'react';
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RequestScope } from '@/lib/connections';

const h = vi.hoisted(() => ({
  scope: { proxyOrigin: 'http://a', authenticatedPrincipalId: 'access:A', authEpoch: 1 } as RequestScope,
  listeners: new Set<() => void>(), fetch: vi.fn(), selected: vi.fn(), process: vi.fn(), finalize: vi.fn(),
}));
vi.mock('../../connections', () => ({
  captureRequestScope: () => h.scope,
  isRequestScopeCurrent: (scope: RequestScope | null) => JSON.stringify(scope) === JSON.stringify(h.scope),
  subscribeCredentialScope: (listener: () => void) => { h.listeners.add(listener); return () => h.listeners.delete(listener); },
  getActiveProxyUrl: () => h.scope.proxyOrigin, fetchTouka: h.fetch,
}));
vi.mock('@/hooks/useChatStream', () => ({ useChatStream: () => ({ handleSendStream: vi.fn(), handleStopGeneration: vi.fn() }) }));
vi.mock('../StreamRegistryProvider', () => ({ useStreamRegistry: () => ({ getStream: () => undefined, streamingSessionIds: [], checkSessionStream: vi.fn() }) }));
vi.mock('../ModelProvider', () => ({ useModels: () => ({ availableModels: [], selectedModel: 'synthetic', setSelectedModel: h.selected }) }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), errorMsg: (code: string) => code }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), info: vi.fn() } }));

import { SessionListProvider, useSessionList } from '../SessionListProvider';
import { ChatProvider, useChat } from '../ChatProvider';
import { useArtifacts } from '@/hooks/useArtifacts';
import { useArtifactExtractor } from '@/hooks/useArtifactExtractor';

const wrapper = ({ children }: { children: React.ReactNode }) => <SessionListProvider><ChatProvider>{children}</ChatProvider></SessionListProvider>;
const session = (title: string) => ({ id: 'same-id', title, updatedAt: 1, agent_id: 'synthetic',
  messages: { messages: { a: { id: 'a', role: 'assistant' as const, content: title, parentId: null, childrenIds: [] } }, currentId: 'a' } });
const response = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as Response;

async function mountA() {
  const rendered = renderHook(() => ({ list: useSessionList(), chat: useChat() }), { wrapper });
  await act(async () => {});
  act(() => rendered.result.current.list.setSessions([session('private A')]));
  act(() => rendered.result.current.chat.setActiveSessionId('same-id'));
  act(() => {
    rendered.result.current.chat.setInputValue('draft A');
    rendered.result.current.chat.setAttachment({ type: 'text', url: '', text: 'attachment A' });
  });
  return rendered;
}
describe('credential scope invalidates actual Chat view owners', () => {
  beforeEach(() => {
    vi.clearAllMocks(); h.listeners.clear();
    h.scope = { proxyOrigin: 'http://a', authenticatedPrincipalId: 'access:A', authEpoch: 1 };
    h.fetch.mockResolvedValue(response([]));
  });
  it('new B credential before AuthContext render clears A history, selection, input and attachment', async () => {
    const { result } = await mountA();
    expect(result.current.chat.history.messages.a.content).toBe('private A');
    const oldCommands = result.current.list;
    act(() => {
      h.scope = { ...h.scope, authenticatedPrincipalId: 'access:B', authEpoch: 2 };
      for (const listener of h.listeners) listener();
    });
    await act(async () => {});
    expect(result.current.chat.activeSessionId).toBe('');
    expect(result.current.chat.history).toEqual({ messages: {}, currentId: null });
    expect(result.current.chat.inputValue).toBe('');
    expect(result.current.chat.attachment).toBeNull();
    h.fetch.mockClear();
    act(() => oldCommands.setSessions([session('late A')]));
    await act(async () => { await oldCommands.syncSession(session('late A')); });
    expect(result.current.list.sessions).toEqual([]);
    expect(h.fetch).not.toHaveBeenCalled();
    act(() => result.current.list.setSessions([session('B')]));
    act(() => result.current.chat.setActiveSessionId('same-id'));
    expect(result.current.chat.history.messages.a.content).toBe('B');
    expect(result.current.chat.inputValue).toBe('');
  });
  it('late switch refresh cannot replace a new principal same-id view', async () => {
    const { result } = await mountA();
    let resolve!: (value: Response) => void;
    h.fetch.mockReturnValueOnce(new Promise<Response>(done => { resolve = done; }));
    let switching!: ReturnType<typeof result.current.chat.handleSwitchSession>;
    act(() => { switching = result.current.chat.handleSwitchSession('same-id'); });
    act(() => {
      h.scope = { ...h.scope, authenticatedPrincipalId: 'access:B', authEpoch: 2 };
      for (const listener of h.listeners) listener();
    });
    await act(async () => {});
    act(() => result.current.list.setSessions([session('B')]));
    act(() => result.current.chat.setActiveSessionId('same-id'));
    await act(async () => { resolve(response([session('late A')])); await switching; });
    expect(result.current.chat.history.messages.a.content).toBe('B');
    expect(result.current.chat.inputValue).toBe('');
    expect(result.current.list.sessions[0].title).toBe('B');
  });
  it('Proxy change resets the same view boundary without waiting for a new login', async () => {
    const { result } = await mountA();
    act(() => {
      h.scope = { ...h.scope, proxyOrigin: 'http://b' };
      window.dispatchEvent(new Event('touka_connections_updated'));
    });
    await act(async () => {});
    expect(result.current.chat.history.messages).toEqual({});
    expect(result.current.chat.inputValue).toBe('');
    expect(result.current.chat.attachment).toBeNull();
  });
  it('credential change clears global artifacts and old extraction callbacks cannot repopulate B', async () => {
    const rendered = renderHook(() => ({ list: useSessionList(), chat: useChat(),
      extraction: useArtifactExtractor('synthetic', false, { messages: {}, currentId: null }) }), { wrapper });
    await act(async () => {});
    const old = rendered.result.current.extraction;
    act(() => { old.processStreamChunk('```html\n<div>private A</div>\n```'); });
    expect(useArtifacts.getState().artifacts).not.toHaveLength(0);
    act(() => {
      h.scope = { ...h.scope, authenticatedPrincipalId: 'access:B', authEpoch: 2 };
      for (const listener of h.listeners) listener();
      expect(useArtifacts.getState().artifacts).toEqual([]);
    });
    expect(useArtifacts.getState().artifacts).toEqual([]);
    act(() => {
      old.finalizeStream('```html\n<div>late private A</div>\n```');
      expect(useArtifacts.getState().artifacts).toEqual([]);
    });
    expect(useArtifacts.getState().artifacts).toEqual([]);
  });
  it('an old extractor cannot repopulate global artifacts after its scope is retired', () => {
    const history = { messages: {}, currentId: null };
    const { result } = renderHook(() => useArtifactExtractor('a', true, history));
    const old = result.current;
    h.scope = { ...h.scope, authenticatedPrincipalId: 'access:B', authEpoch: 2 };
    useArtifacts.getState().clearArtifacts();
    old.processStreamChunk('```html\n<div>late A</div>\n```');
    old.finalizeStream('```html\n<div>late A</div>\n```');
    expect(useArtifacts.getState().artifacts).toEqual([]);
  });
  it('clearing the active session clears its artifact panel', async () => {
    const { result } = await mountA();
    act(() => { useArtifacts.getState().addArtifact({ title: 'private', type: 'code', content: 'A' }); });
    expect(useArtifacts.getState().artifacts).toHaveLength(1);
    act(() => result.current.chat.setActiveSessionId(''));
    expect(useArtifacts.getState().artifacts).toEqual([]);
  });
});
