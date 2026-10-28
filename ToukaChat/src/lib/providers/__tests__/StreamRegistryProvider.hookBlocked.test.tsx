/**
 * ADR-0292：送出時 UserPromptSubmit hook 擋下訊息 → 存回的樹把這則 user 訊息標成 hookBlocked，
 * 下一輪組歷史時就不會送給模型；其他錯誤不標。
 */
import React from 'react';
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from '@/lib/types';
import type { TreeHistory } from '@/lib/tree_history';
import type { StartStreamParams } from '../StreamRegistryProvider';

type Updater = (current: Session) => Session;

const h = vi.hoisted(() => ({
  sessions: [] as Session[], wire: vi.fn(), hookNotice: vi.fn(), complete: vi.fn(),
  scope: { proxyOrigin: 'http://x', authenticatedPrincipalId: 'a', authEpoch: 1 },
}));
vi.mock('../SessionListProvider', () => ({ useSessionList: () => ({
  sessions: h.sessions,
  getSession: (id: string) => h.sessions.find(session => session.id === id),
  updateSession: async (id: string, update: Updater) => {
    h.sessions = h.sessions.map(session => (session.id === id ? update(session) : session));
    return { status: 'saved', representation: 'full', revision: 'user' };
  },
  beginProducer: () => ({ sessionId: 's1', scope: h.scope, generation: 0 }),
  completeProducer: async (_producer: unknown, update: Updater) => {
    h.complete(update);
    return { status: 'unknown', error_code: 1302 };
  },
  prepareResume: vi.fn(), getPersistence: () => ({ status: 'unknown', error_code: 1307 }),
}) }));
vi.mock('../DebugProvider', () => ({ useDebug: () => ({ registerSimHandlers: vi.fn() }) }));
vi.mock('../../connections', () => ({
  fetchChatStream: h.wire, fetchTouka: async () => ({ ok: true, json: async () => ({ streams: [] }) }),
  resumeChatStream: vi.fn(), captureRequestScope: () => h.scope, isRequestScopeCurrent: (s: unknown) => s === h.scope,
}));
vi.mock('@/hooks/useArtifacts', () => ({ useArtifacts: { getState: () => ({}) } }));
vi.mock('@/hooks/useAgentBrainNotice', () => ({ useAgentBrainNotice: () => () => false }));
vi.mock('@/hooks/useHookBlockNotice', () => ({ useHookBlockNotice: () => h.hookNotice }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), errorMsg: (code: string) => code }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), warning: vi.fn() } }));

import { StreamRegistryProvider, useStreamRegistry } from '../StreamRegistryProvider';

const params: StartStreamParams = {
  sessionId: 's1', baseTree: { messages: {}, currentId: null }, userText: 'secret', resolvedInput: 'secret',
  attachment: null, parentId: null, model: 'synthetic', protocol: 'openai', proxyUrl: 'http://x',
  authLevel: 'Manual', activeSkills: [],
};
const wrapper = ({ children }: { children: React.ReactNode }) => <StreamRegistryProvider>{children}</StreamRegistryProvider>;

async function savedTreeAfterError(err: unknown): Promise<TreeHistory> {
  const { result } = renderHook(() => useStreamRegistry(), { wrapper });
  await act(async () => { await result.current.startStream(params); });
  await act(async () => { await h.wire.mock.calls[0][4](err); });
  const update = h.complete.mock.calls[0][0] as Updater;
  return update(h.sessions[0]).messages as TreeHistory;
}

function userNode(tree: TreeHistory) {
  return Object.values(tree.messages).find(node => node.role === 'user');
}

describe('hook 擋下的訊息存成 hookBlocked（ADR-0292）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    h.sessions = [{ id: 's1', title: 't', messages: [], updatedAt: 1 }];
    h.wire.mockResolvedValue(undefined);
  });

  it('擋下 → 這則 user 訊息標成 hookBlocked', async () => {
    h.hookNotice.mockReturnValue(true);
    const tree = await savedTreeAfterError(new Error('blocked'));
    expect(userNode(tree)?.content).toBe('secret');
    expect(userNode(tree)?.hookBlocked).toBe(true);
  });

  it('其他錯誤 → 不標', async () => {
    h.hookNotice.mockReturnValue(false);
    const tree = await savedTreeAfterError(new Error('network'));
    expect(userNode(tree)?.hookBlocked).toBeUndefined();
  });
});
