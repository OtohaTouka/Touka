/**
 * ADR-0319：送出時，上一輪存下的工具步驟寫成工具紀錄放在助理那則前面；群聊（GROUP_CHAT_ID）不加。
 * 這裡驗 StreamRegistryProvider 真的把開關接到 buildApiHistory，送出的訊息才看得出差別。
 */
import React from 'react';
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from '@/lib/types';
import type { ChatMessage } from '@/lib/connections';
import type { StartStreamParams } from '../StreamRegistryProvider';
import { GROUP_CHAT_ID } from '@/lib/chat/messageNavigation';

const h = vi.hoisted(() => ({
  sessions: [] as Session[], wire: vi.fn(),
  scope: { proxyOrigin: 'http://x', authenticatedPrincipalId: 'a', authEpoch: 1 },
}));
vi.mock('../SessionListProvider', () => ({ useSessionList: () => ({
  sessions: h.sessions,
  getSession: (id: string) => h.sessions.find(session => session.id === id),
  // 存檔照真的套用：送出的歷史取自存檔後的對話
  updateSession: async (id: string, update: (current: Session) => Session) => {
    h.sessions = h.sessions.map(session => (session.id === id ? update(session) : session));
    return { status: 'saved', representation: 'full', revision: 'user' };
  },
  beginProducer: () => ({ sessionId: 's1', scope: h.scope, generation: 0 }),
  completeProducer: async () => ({ status: 'unknown', error_code: 1302 }),
  prepareResume: vi.fn(), getPersistence: () => ({ status: 'unknown', error_code: 1307 }),
}) }));
vi.mock('../DebugProvider', () => ({ useDebug: () => ({ registerSimHandlers: vi.fn() }) }));
vi.mock('../../connections', () => ({
  fetchChatStream: h.wire, fetchTouka: async () => ({ ok: true, json: async () => ({ streams: [] }) }),
  resumeChatStream: vi.fn(), captureRequestScope: () => h.scope, isRequestScopeCurrent: (s: unknown) => s === h.scope,
}));
vi.mock('@/hooks/useArtifacts', () => ({ useArtifacts: { getState: () => ({}) } }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), errorMsg: (code: string) => code }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), warning: vi.fn() } }));

import { StreamRegistryProvider, useStreamRegistry } from '../StreamRegistryProvider';

const baseTree: StartStreamParams['baseTree'] = {
  messages: {
    u1: { id: 'u1', parentId: null, childrenIds: ['a1'], role: 'user', content: 'save it' },
    a1: {
      id: 'a1', parentId: 'u1', childrenIds: [], role: 'assistant', content: 'done',
      steps: [{ id: 'c1', tool: 'write', args: { path: 'n.md' }, status: 'success', result: 'saved' }],
    },
  },
  currentId: 'a1',
};
const wrapper = ({ children }: { children: React.ReactNode }) => <StreamRegistryProvider>{children}</StreamRegistryProvider>;

async function sentMessages(model: string): Promise<ChatMessage[]> {
  const { result } = renderHook(() => useStreamRegistry(), { wrapper });
  await act(async () => {
    await result.current.startStream({
      sessionId: 's1', baseTree, userText: 'next', resolvedInput: 'next', attachment: null, parentId: 'a1',
      model, protocol: 'openai', proxyUrl: 'http://x', authLevel: 'Manual', activeSkills: [],
    });
  });
  return h.wire.mock.calls[0][0] as ChatMessage[];
}

describe('StreamRegistryProvider sends tool records (ADR-0319)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    h.sessions = [{ id: 's1', title: 't', messages: baseTree, updatedAt: 1 }];
    h.wire.mockResolvedValue(undefined);
  });

  it('puts the record before the earlier reply for an ordinary chat', async () => {
    const sent = await sentMessages('synthetic');
    expect(sent.map(m => m.role)).toEqual(['user', 'assistant', 'user']);
    expect(sent[1].content).toMatch(/^〔工具紀錄/);
    expect(sent[1].content).toContain('write');
    expect(sent[1].content.endsWith('done')).toBe(true);
  });

  it('sends the earlier reply unchanged in group chat', async () => {
    const sent = await sentMessages(GROUP_CHAT_ID);
    expect(sent.map(m => m.content)).toEqual(['save it', 'done', 'next']);
  });
});
