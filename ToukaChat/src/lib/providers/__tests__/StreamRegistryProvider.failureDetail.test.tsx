/**
 * 路線圖第 8 項 PR 1：回覆失敗時的廠商原文走記憶體，不進對話樹（樹會存檔、會和存檔比對）。
 */
import React from 'react';
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GENERATION_FAILURE_KIND } from '@/hooks/useProgress';
import { useFailureDetails } from '@/lib/chat/failureDetailStore';
import type { Session } from '@/lib/types';
import type { StartStreamParams } from '../StreamRegistryProvider';

type Updater = (current: Session) => Session;

const h = vi.hoisted(() => ({
  sessions: [] as Session[], wire: vi.fn(),
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
  completeProducer: vi.fn(), prepareResume: vi.fn(), getPersistence: () => ({ status: 'unknown', error_code: 1307 }),
}) }));
vi.mock('../DebugProvider', () => ({ useDebug: () => ({ registerSimHandlers: vi.fn() }) }));
vi.mock('../../connections', () => ({
  fetchChatStream: h.wire, fetchTouka: async () => ({ ok: true, json: async () => ({ streams: [] }) }),
  resumeChatStream: vi.fn(), captureRequestScope: () => h.scope, isRequestScopeCurrent: (s: unknown) => s === h.scope,
}));
vi.mock('@/hooks/useArtifacts', () => ({ useArtifacts: { getState: () => ({}) } }));
vi.mock('@/hooks/useAgentBrainNotice', () => ({ useAgentBrainNotice: () => () => false }));
vi.mock('@/hooks/useHookBlockNotice', () => ({ useHookBlockNotice: () => () => false }));
vi.mock('@/hooks/usePromptStoppedNotice', () => ({ usePromptStoppedNotice: () => () => false }));
vi.mock('@/hooks/useOldHomeNotice', () => ({ useOldHomeNotice: () => () => false }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), errorMsg: (code: string) => code }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), warning: vi.fn() } }));

import { StreamRegistryProvider, useStreamRegistry } from '../StreamRegistryProvider';

const params: StartStreamParams = {
  sessionId: 's1', baseTree: { messages: {}, currentId: null }, userText: 'hi', resolvedInput: 'hi',
  attachment: null, parentId: null, model: 'synthetic', protocol: 'openai', proxyUrl: 'http://x',
  authLevel: 'Manual', activeSkills: [],
};
const wrapper = ({ children }: { children: React.ReactNode }) => <StreamRegistryProvider>{children}</StreamRegistryProvider>;
const VENDOR_TEXT = 'your credit balance is too low';

describe('回覆失敗的廠商原文', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    h.sessions = [{ id: 's1', title: 't', messages: [], updatedAt: 1 }];
    h.wire.mockResolvedValue(undefined);
  });
  afterEach(() => useFailureDetails.setState({ details: {} }));

  it('記在這則回覆底下；對話樹只有分類', async () => {
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream(params); });
    const assistantId = h.wire.mock.calls[0][1].ui_persistence.assistant_node_id as string;
    const onEvent = h.wire.mock.calls[0][5] as (event: Record<string, unknown>) => void;

    act(() => onEvent({ type: 'touka_progress', kind: GENERATION_FAILURE_KIND, error_type: 'billing_error', detail: VENDOR_TEXT }));

    expect(useFailureDetails.getState().details).toEqual({ [assistantId]: VENDOR_TEXT });
    const tree = result.current.getStream('s1')?.tree;
    expect(tree?.messages[assistantId]?.progress?.[GENERATION_FAILURE_KIND]).toEqual(
      { kind: GENERATION_FAILURE_KIND, error_type: 'billing_error' },
    );
    expect(JSON.stringify(tree)).not.toContain(VENDOR_TEXT);
  });
});
