/**
 * ADR-0292：送出時 UserPromptSubmit hook 擋下訊息 → 存回的樹拿掉這一輪（伺服器端之後讀存檔的
 * 地方都看不到它）。後端在送模型前就拒絕、什麼都沒存，所以和沒有文字的完成走同一條存檔路徑
 * （照 revision 存回），並通知呼叫端把文字放回輸入框；其他錯誤照舊不存。
 */
import React from 'react';
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from '@/lib/types';
import type { TreeHistory } from '@/lib/tree_history';
import type { StartStreamParams } from '../StreamRegistryProvider';

type Updater = (current: Session) => Session;

const h = vi.hoisted(() => ({
  sessions: [] as Session[], wire: vi.fn(), hookNotice: vi.fn(), complete: vi.fn(), onHookBlocked: vi.fn(),
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
  completeProducer: async (_producer: unknown, update: Updater, terminal: unknown, generation: unknown) => {
    h.complete(update, terminal, generation);
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
  authLevel: 'Manual', activeSkills: [], onHookBlocked: h.onHookBlocked,
};
const wrapper = ({ children }: { children: React.ReactNode }) => <StreamRegistryProvider>{children}</StreamRegistryProvider>;

async function savedAfterError(err: unknown) {
  const { result } = renderHook(() => useStreamRegistry(), { wrapper });
  await act(async () => { await result.current.startStream(params); });
  await act(async () => { await h.wire.mock.calls[0][4](err); });
  const [update, terminal, generation] = h.complete.mock.calls[0] as [Updater, unknown, unknown];
  const saved = update(h.sessions[0]);
  return { tree: saved.messages as TreeHistory, title: saved.title, terminal, generation };
}

function userNode(tree: TreeHistory) {
  return Object.values(tree.messages).find(node => node.role === 'user');
}

describe('hook 擋下的這一輪從存檔拿掉（ADR-0292）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    h.sessions = [{ id: 's1', title: 't', messages: [], updatedAt: 1 }];
    h.wire.mockResolvedValue(undefined);
  });

  it('擋下 → 照「後端沒存文字」的完成流程存回沒有這則訊息的樹，並通知放回文字', async () => {
    h.hookNotice.mockReturnValue(true);
    const saved = await savedAfterError(new Error('blocked'));
    expect(saved.tree).toEqual({ messages: {}, currentId: null });
    expect(saved.terminal).toEqual({ status: 'skipped', reason: 'no_text' });
    expect(saved.generation).toBe('completed');
    expect(h.onHookBlocked).toHaveBeenCalledTimes(1);
  });

  it('擋下 → 用被擋文字建的標題換回去，自己取的標題留著', async () => {
    h.hookNotice.mockReturnValue(true);
    h.sessions = [{ id: 's1', title: 'secret', messages: [], updatedAt: 1 }];
    expect((await savedAfterError(new Error('blocked'))).title).toBe('新對話');
    vi.clearAllMocks();
    h.wire.mockResolvedValue(undefined);
    h.hookNotice.mockReturnValue(true);
    h.sessions = [{ id: 's1', title: '我的對話', messages: [], updatedAt: 1 }];
    expect((await savedAfterError(new Error('blocked'))).title).toBe('我的對話');
  });

  it('其他錯誤 → 訊息照舊留著、不當成完成，不通知', async () => {
    h.hookNotice.mockReturnValue(false);
    const saved = await savedAfterError(new Error('network'));
    expect(userNode(saved.tree)?.content).toBe('secret');
    expect(saved.generation).toBe('error');
    expect(h.onHookBlocked).not.toHaveBeenCalled();
  });
});
