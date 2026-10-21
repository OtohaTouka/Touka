import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import contract from '../../../../../tests/fixtures/reliability_session_stream.json';
import type { Session } from '@/lib/types';
import type { ChatAttachment } from '@/lib/chat/attachment';
import { setAccessToken, clearAccessToken } from '@/lib/connections';
import { SessionListProvider, useSessionList } from '../SessionListProvider';
import { DebugProvider } from '../DebugProvider';
import { StreamRegistryProvider, useStreamRegistry } from '../StreamRegistryProvider';
import { ChatProvider, useChat } from '../ChatProvider';

vi.mock('../ModelProvider', () => ({ useModels: () => ({
  availableModels: [], selectedModel: 'test-model', setSelectedModel: vi.fn(),
}) }));

vi.mock('@/hooks/useLocalStorage', () => ({
  getLocalStorageItem: (key: string, fallback: unknown) => key === 'touka_connections'
    ? [{ id: 'proxy', provider: 'proxy', enabled: true, url: 'http://synthetic.invalid', protocol: 'openai' }] : fallback,
  setLocalStorageItem: vi.fn(),
}));
vi.mock('@/lib/time_utils', () => ({ getSystemEpoch: () => 1000 }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), errorMsg: (code: string) => code }));
vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

const wrapper = ({ children }: { children: React.ReactNode }) => <SessionListProvider>
  <DebugProvider><StreamRegistryProvider>{children}</StreamRegistryProvider></DebugProvider>
</SessionListProvider>;
const jsonResponse = (body: unknown) => new Response(JSON.stringify(body), {
  status: 200, headers: { 'Content-Type': 'application/json' },
});
const assertFullPayload = (payload: unknown) => expect(payload).toEqual({
  ...contract.full_payload, revision: contract.aliases.revision,
});

describe('shared Python producer / real TS decoder, registry and writer contract', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    setAccessToken(`synthetic.${btoa(JSON.stringify({ sub: contract.owner_user_id, type: 'access', exp: 9999999999 }))}.signature`);
    vi.spyOn(crypto, 'randomUUID').mockReturnValueOnce('user' as ReturnType<typeof crypto.randomUUID>)
      .mockReturnValueOnce('assistant' as ReturnType<typeof crypto.randomUUID>);
  });
  afterEach(() => { act(() => clearAccessToken()); window.history.replaceState({}, '', '/'); vi.unstubAllGlobals(); });

  it('URL-selected completed stream is checked once after the delayed first list becomes available', async () => {
    window.history.replaceState({}, '', '/?session=ui');
    let releaseList!: (response: Response) => void;
    const listing = new Promise<Response>(resolve => { releaseList = resolve; });
    const session = { ...contract.full_payload, messages: contract.user_tree,
      is_home: false, home_for_agent: null, project_id: null, auth_level: 'Manual',
      active_skills: [], active_mcp_servers: [], context_summary: null, last_activity_at: 1000 };
    const calls: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const url = new URL(String(input));
      const path = url.pathname;
      calls.push(path);
      if (path === '/v1/ui/sessions') return listing;
      if (path === '/v1/ui/streams/active') return jsonResponse({ streams: [] });
      if (path.endsWith('/stream/state')) return jsonResponse({ state: {
        session_id: 'ui', stream_id: contract.aliases.stream_id, status: 'done', last_seq: 6, started_at: 1,
        started: contract.started, terminal: contract.terminal,
      } });
      if (url.search === '?snapshot=true') return jsonResponse({ exists: true, revision: 'resume-base', session });
      if (path.endsWith('/stream')) return new Response([
        `data: ${JSON.stringify(contract.started)}\n\n`, ...contract.generator_frames,
        `data: ${JSON.stringify(contract.terminal)}\n\n`,
      ].join(''), { headers: { 'Content-Type': 'text/event-stream' } });
      if (path === '/v1/ui/sessions/ui' && options?.method === 'POST') return jsonResponse({
        status: 'success', session_id: 'ui', persistence: { status: 'saved', representation: 'full', revision: 'full' },
      });
      throw new Error(`Unexpected synthetic path: ${path}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    const chatWrapper = ({ children }: { children: React.ReactNode }) => wrapper({ children: <ChatProvider>{children}</ChatProvider> });
    const { result } = renderHook(() => ({ chat: useChat(), list: useSessionList() }), { wrapper: chatWrapper });
    await act(async () => {});
    expect(result.current.chat.activeSessionId).toBe('ui');
    expect(calls.filter(path => path.endsWith('/stream/state'))).toHaveLength(0);
    await act(async () => { releaseList(jsonResponse([session])); });
    await waitFor(() => expect(result.current.chat.history.messages.assistant?.content).toBe('繁體回答'));
    await waitFor(() => expect(result.current.list.getPersistence('ui')).toMatchObject({ status: 'saved', representation: 'full' }));
    expect(result.current.chat.history.messages.assistant.steps).toEqual(contract.full_payload.messages.messages.assistant.steps);
    expect(calls.filter(path => path.endsWith('/stream/state'))).toHaveLength(1);
    expect(calls.filter(path => path.endsWith('/stream'))).toHaveLength(1);
    expect(calls.some(path => path.includes('/chat/completions'))).toBe(false);
  });

  it.each([false, true])('retains full extensions, tool result, branch and attachment; incognito=%s', async incognito => {
    const posts: Record<string, unknown>[] = [];
    let modelCalls = 0;
    const initial = { id: contract.session_id, title: contract.title, messages: structuredClone(contract.base_tree),
      updatedAt: contract.epoch, agent_id: contract.agent_id, revision: 'initial', ...(incognito ? { incognito: true } : {}) } as Session;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const path = new URL(String(input)).pathname;
      if (path === '/v1/ui/sessions') return jsonResponse(incognito ? [] : [initial]);
      if (path === '/v1/ui/streams/active') return jsonResponse({ streams: [] });
      if (path === '/v1/ui/sessions/ui' && options?.method === 'POST') {
        const payload = JSON.parse(String(options.body));
        posts.push(payload);
        return jsonResponse({ status: 'success', session_id: contract.session_id, persistence: {
          status: 'saved', representation: 'full', revision: posts.length === 1 ? 'user-revision' : 'full-revision',
        } });
      }
      if (path === '/v1/chat/completions') {
        modelCalls++;
        const request = JSON.parse(String(options?.body));
        expect(request.ui_persistence).toEqual({ mode: incognito ? 'incognito' : 'persistent',
          user_parent_id: 'user', assistant_node_id: 'assistant', ...(incognito ? {} : { revision: 'user-revision' }) });
        if (!incognito) expect(posts[0].messages).toEqual(contract.user_tree);
        const terminal = incognito ? contract.incognito_terminal : contract.terminal;
        const wire = `data: ${JSON.stringify(contract.started)}\n\n${contract.generator_frames.join('')}data: ${JSON.stringify(terminal)}`;
        const bytes = new TextEncoder().encode(wire);
        // One-byte chunks cross every UTF-8 boundary; final event has no newline.
        return new Response(new ReadableStream({ start(controller) {
          for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
          controller.close();
        } }), { headers: { 'Content-Type': 'text/event-stream' } });
      }
      throw new Error(`Unexpected synthetic transport path: ${path}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => ({ list: useSessionList(), registry: useStreamRegistry() }), { wrapper });
    await act(async () => {});
    if (incognito) act(() => result.current.list.setSessions([initial]));
    await waitFor(() => expect(result.current.list.getSession('ui')).toBeDefined());
    await act(async () => { expect(await result.current.registry.startStream({
      sessionId: 'ui', baseTree: initial.messages, userText: 'hi', resolvedInput: 'hi',
      attachment: contract.attachment as ChatAttachment, parentId: null, model: contract.agent_id,
      protocol: 'openai', proxyUrl: 'http://synthetic.invalid', authLevel: 'Manual', activeSkills: [], incognito,
    })).toBe(true); });
    await waitFor(() => expect(result.current.registry.getStream('ui')).toBeUndefined());
    expect(modelCalls).toBe(1);
    expect(result.current.list.getSession('ui')?.messages).toEqual(contract.full_payload.messages);
    expect(result.current.list.sessions[0].messages).toEqual(contract.full_payload.messages);
    if (incognito) {
      expect(posts).toEqual([]);
      expect(fetchMock.mock.calls.some(([input]) => new URL(String(input)).pathname.startsWith('/v1/ui/sessions/'))).toBe(false);
      expect(result.current.list.getPersistence('ui')).toEqual({ status: 'skipped', reason: 'incognito' });
    } else {
      expect(posts).toHaveLength(2);
      assertFullPayload(posts[1]);
      const dropped = structuredClone(posts[1]) as typeof contract.full_payload & { revision: string };
      dropped.messages.messages.assistant.steps[0].result = {} as { present: null };
      expect(() => assertFullPayload(dropped)).toThrow();
      expect(result.current.list.getPersistence('ui')).toEqual({ status: 'saved', representation: 'full', revision: 'full-revision' });
    }
  });
});
