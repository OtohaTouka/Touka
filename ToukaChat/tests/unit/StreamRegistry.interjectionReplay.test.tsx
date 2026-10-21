/**
 * ADR-0249（R-4 ② LOW 3）：重整後重播時，助理節點的 interruptions[]／interjections[] 先清空再由事件重建，
 * 存檔裡已有上一次重播的結果時也不會重複。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { StreamRegistryProvider, useStreamRegistry } from '@/lib/providers/StreamRegistryProvider';
import { useInterjectionStore } from '@/lib/chat/interjectionStore';
import type { Session } from '@/lib/types';

const mocks = vi.hoisted(() => ({ resumeChatStream: vi.fn(), fetchTouka: vi.fn() }));
let sessionsArr: Session[] = [];

vi.mock('@/lib/connections', () => ({
  fetchChatStream: vi.fn(),
  resumeChatStream: mocks.resumeChatStream,
  fetchTouka: mocks.fetchTouka,
  getActiveProxyUrl: () => 'http://localhost:8000',
  captureRequestScope: () => ({ proxyOrigin: 'http://localhost:8000', authenticatedPrincipalId: 'a', authEpoch: 1 }),
  isRequestScopeCurrent: () => true,
}));
vi.mock('@/lib/providers/SessionListProvider', () => ({
  useSessionList: () => ({
    sessions: sessionsArr,
    getSession: (id: string) => sessionsArr.find(session => session.id === id),
    beginProducer: (id: string) => ({ sessionId: id }),
    completeProducer: vi.fn(),
    prepareResume: async (id: string) => sessionsArr.find(session => session.id === id),
    getPersistence: () => undefined,
  }),
}));
vi.mock('@/lib/providers/DebugProvider', () => ({ useDebug: () => ({ registerSimHandlers: vi.fn() }) }));
vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: { getState: () => ({ setActiveTab: vi.fn(), setIsOpen: vi.fn() }) },
}));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), warning: vi.fn() } }));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <StreamRegistryProvider>{children}</StreamRegistryProvider>
);
const delivered = { id: 'i1', text: '補充', urgent: true, status: 'delivered' as const, step: 1, created_at: 1 };

describe('重播不重複插話標記', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useInterjectionStore.setState({ bySession: {} });
    mocks.resumeChatStream.mockReturnValue(new Promise<void>(() => {}));
  });

  it('存檔已有上一次重播的 interruptions／interjections：重播後各只有一筆', async () => {
    sessionsArr = [{ id: 'sess-R', title: 'R', agent_id: 'm1', updatedAt: 1, revision: 'r', messages: {
      currentId: 'a1', messages: {
        u1: { id: 'u1', parentId: null, childrenIds: ['a1'], role: 'user', content: 'q' },
        a1: { id: 'a1', parentId: 'u1', childrenIds: [], role: 'assistant', content: '舊的',
          interruptions: [{ step: 1 }], interjections: [delivered] },
      } } as unknown as Session['messages'] }];
    mocks.fetchTouka.mockResolvedValue({ ok: true, json: async () => ({ streams: [{ session_id: 'sess-R',
      stream_id: 'st1', status: 'active', last_seq: 3, started_at: 1, terminal: null,
      started: { type: 'touka_stream_started', session_id: 'sess-R', stream_id: 'st1',
        user_parent_id: 'u1', assistant_node_id: 'a1' } }] }) });
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await waitFor(() => expect(mocks.resumeChatStream).toHaveBeenCalledTimes(1));
    const onEvent = mocks.resumeChatStream.mock.calls[0][6] as (event: Record<string, unknown>) => void;
    act(() => {
      onEvent({ type: 'round_interrupted_by_user', step: 1 });
      onEvent({ type: 'interjection_delivered', interjection_id: 'i1', step: 1, urgent: true, text: '補充' });
    });
    const node = result.current.getStream('sess-R')!.tree.messages.a1;
    expect(node.interruptions).toEqual([{ step: 1 }]);
    expect(node.interjections?.map(i => i.id)).toEqual(['i1']);
  });
});
