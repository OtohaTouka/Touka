/**
 * model-p0 U7：聊天送出的 onError 交給 useAgentBrainNotice；它處理了（沒設主要模型）
 * 就不再疊一個「網路連線錯誤」，其他錯誤照舊顯示。被 hook 擋下的訊息（ADR-0292）同樣交給
 * useHookBlockNotice，處理了就不顯示網路錯誤。
 */
import React from 'react';
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { toast } from 'sonner';
import type { Session } from '@/lib/types';
import type { StartStreamParams } from '../StreamRegistryProvider';

const h = vi.hoisted(() => ({
  sessions: [] as Session[], wire: vi.fn(), notice: vi.fn(), hookNotice: vi.fn(),
  scope: { proxyOrigin: 'http://x', authenticatedPrincipalId: 'a', authEpoch: 1 },
}));
vi.mock('../SessionListProvider', () => ({ useSessionList: () => ({
  sessions: h.sessions,
  getSession: (id: string) => h.sessions.find(session => session.id === id),
  updateSession: async () => ({ status: 'saved', representation: 'full', revision: 'user' }),
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
vi.mock('@/hooks/useAgentBrainNotice', () => ({ useAgentBrainNotice: () => h.notice }));
vi.mock('@/hooks/useHookBlockNotice', () => ({ useHookBlockNotice: () => h.hookNotice }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), errorMsg: (code: string) => code }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), warning: vi.fn() } }));

import { StreamRegistryProvider, useStreamRegistry } from '../StreamRegistryProvider';

const params: StartStreamParams = {
  sessionId: 's1', baseTree: { messages: {}, currentId: null }, userText: 'u', resolvedInput: 'u',
  attachment: null, parentId: null, model: 'synthetic', protocol: 'openai', proxyUrl: 'http://x',
  authLevel: 'Manual', activeSkills: [],
};
const wrapper = ({ children }: { children: React.ReactNode }) => <StreamRegistryProvider>{children}</StreamRegistryProvider>;

async function failStream(err: unknown) {
  const { result } = renderHook(() => useStreamRegistry(), { wrapper });
  await act(async () => { await result.current.startStream(params); });
  await act(async () => { await h.wire.mock.calls[0][4](err); });
}

describe('聊天 onError → useAgentBrainNotice（model-p0 U7）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    h.sessions = [{ id: 's1', title: 't', messages: [], updatedAt: 1 }];
    h.wire.mockResolvedValue(undefined);
    h.hookNotice.mockReturnValue(false);
  });

  it('通知處理了（沒設主要模型）→ 不再顯示網路錯誤', async () => {
    h.notice.mockReturnValue(true);
    const err = new Error('brain');
    await failStream(err);
    expect(h.notice).toHaveBeenCalledWith(err);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('其他錯誤 → 照舊顯示網路錯誤', async () => {
    h.notice.mockReturnValue(false);
    await failStream(new Error('other'));
    expect(h.notice).toHaveBeenCalledTimes(1);
    expect(h.hookNotice).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledTimes(1);
  });

  it('被 hook 擋下（ADR-0292）→ 交給 hook 通知，不再顯示網路錯誤', async () => {
    h.notice.mockReturnValue(false);
    h.hookNotice.mockReturnValue(true);
    const err = new Error('blocked');
    await failStream(err);
    expect(h.hookNotice).toHaveBeenCalledWith(err);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('使用者中止 → 兩者都不顯示', async () => {
    await failStream({ name: 'AbortError' });
    expect(h.notice).not.toHaveBeenCalled();
    expect(h.hookNotice).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });
});
