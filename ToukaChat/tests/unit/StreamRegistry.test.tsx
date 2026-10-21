/**
 * ADR-0080 背景續跑：StreamRegistryProvider 契約測試（先 Red 後 Green）。
 *
 * registry = 進行中串流的 per-session SSOT。驗證：
 * - startStream 建立 entry（status waiting，樹含 user+assistant）
 * - onChunk 路由到正確 sid 累積、不碰其他 sid（per-session 隔離＝防污染）
 * - 並發上限 5：第 6 條被擋（回 false）
 * - stopStream：abort signal + 清 entry
 * - onDone：寫回 session（syncSession）＋ 清 entry
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { StreamRegistryProvider, useStreamRegistry } from '@/lib/providers/StreamRegistryProvider';
import { TreeHistory } from '@/lib/tree_history';
import { useTranslation } from 'react-i18next';
import type { Session, SaveResult, StreamStartedEvent, StreamTerminalEvent } from '@/lib/types';
import type { SessionChange } from '@/lib/session_persistence';

vi.mock('react-i18next', async (importOriginal) => ({
  ...await importOriginal<typeof import('react-i18next')>(),
  useTranslation: vi.fn(),
}));

const mocks = vi.hoisted(() => ({
  fetchChatStream: vi.fn(),
  fetchTouka: vi.fn(async () => ({ ok: true })),
  syncSession: vi.fn(),
  setSessions: vi.fn(),
  registerSimHandlers: vi.fn(),
  completeProducer: vi.fn(),
}));

vi.mock('@/lib/connections', () => ({
  fetchChatStream: mocks.fetchChatStream,
  fetchTouka: mocks.fetchTouka,
  getActiveProxyUrl: () => 'http://localhost:8000',
  captureRequestScope: () => ({ proxyOrigin: 'http://localhost:8000', authenticatedPrincipalId: 'a', authEpoch: 1 }),
  isRequestScopeCurrent: () => true,
}));

let sessionsArr: Session[] = [];
vi.mock('@/lib/providers/SessionListProvider', () => ({
  useSessionList: () => ({
    sessions: sessionsArr,
    setSessions: vi.fn((upd: unknown) => {
      sessionsArr = typeof upd === 'function'
        ? (upd as (p: typeof sessionsArr) => typeof sessionsArr)(sessionsArr)
        : (upd as typeof sessionsArr);
      mocks.setSessions(sessionsArr);
    }),
    syncSession: mocks.syncSession,
    getPersistence: () => undefined,
    getSession: (id: string) => sessionsArr.find(session => session.id === id),
    updateSession: (id: string, change: SessionChange) => {
      const current = sessionsArr.find(session => session.id === id)!;
      const next = change(current);
      expect(change(current)).toEqual(next);
      sessionsArr = sessionsArr.map(session => session.id === id ? next : session);
      return mocks.syncSession(next);
    },
    beginProducer: (id: string) => ({ sessionId: id }),
    completeProducer: mocks.completeProducer,
  }),
}));

vi.mock('@/lib/providers/DebugProvider', () => ({
  useDebug: () => ({ registerSimHandlers: mocks.registerSimHandlers }),
}));

vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), errorMsg: (code: string) => code, getErrorCode: () => undefined }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), warning: vi.fn() } }));

interface Captured {
  onChunk: (c: string) => void;
  onDone: (terminal?: StreamTerminalEvent) => Promise<void>;
  onError: (e: unknown) => Promise<void>;
  onEvent: (e: Record<string, unknown>) => void;
  signal: AbortSignal;
  sessionId: string;
  started: StreamStartedEvent;
}

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <StreamRegistryProvider>{children}</StreamRegistryProvider>
);

const emptyTree: TreeHistory = { messages: {}, currentId: null };

function baseParams(sessionId: string) {
  return {
    sessionId,
    baseTree: emptyTree,
    userText: 'hello',
    resolvedInput: 'hello',
    attachment: null,
    parentId: null,
    model: 'test-model',
    protocol: 'openai' as const,
    proxyUrl: 'http://localhost:8000',
    authLevel: 'Manual',
    activeSkills: [] as string[],
  };
}

describe('StreamRegistryProvider', () => {
  const captures: Captured[] = [];

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useTranslation).mockReturnValue({ t: (key: string) => key } as never);
    sessionsArr = [{ id: 'sess-A', title: '新對話', messages: [], updatedAt: 1, agent_id: 'test-model' }];
    captures.length = 0;
    mocks.syncSession.mockResolvedValue({ status: 'saved', representation: 'full', revision: 'user' });
    mocks.completeProducer.mockImplementation(async (token: { sessionId: string }, change: SessionChange, result: SaveResult) => {
      const previous = sessionsArr.find(session => session.id === token.sessionId)!;
      expect(change(previous)).toEqual(change(previous));
      sessionsArr = sessionsArr.map(session => session.id === token.sessionId ? change(session) : session);
      return result;
    });
    mocks.fetchChatStream.mockImplementation(
      (_m: unknown, opts: { session_id?: string; ui_persistence: { user_parent_id: string; assistant_node_id: string } }, onChunk: Captured['onChunk'], onDone: Captured['onDone'],
       onError: Captured['onError'], onEvent: Captured['onEvent'], signal: AbortSignal) => {
        const started: StreamStartedEvent = { type: 'touka_stream_started', session_id: opts.session_id!,
          stream_id: `stream-${opts.session_id}`, user_parent_id: opts.ui_persistence.user_parent_id,
          assistant_node_id: opts.ui_persistence.assistant_node_id };
        captures.push({ onChunk, onDone, onError, onEvent, signal, sessionId: opts.session_id || '', started });
        onEvent(started);
        return new Promise<void>(() => {}); // 串流進行中（不 resolve）
      },
    );
  });

  it('startStream 建立 entry：status waiting、樹含 user+assistant', async () => {
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    let ok = false;
    await act(async () => { ok = await result.current.startStream(baseParams('sess-A')); });
    expect(ok).toBe(true);

    const s = result.current.getStream('sess-A');
    expect(s).toBeDefined();
    expect(s!.status).toBe('waiting');
    const roles = Object.values(s!.tree.messages).map(m => m.role).sort();
    expect(roles).toEqual(['assistant', 'user']);
    expect(result.current.streamingSessionIds).toContain('sess-A');
  });

  it('onChunk 只累積到對應 sid，不污染其他 session（per-session 隔離）', async () => {
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    sessionsArr = [
      { id: 'sess-A', title: 'A', messages: [], updatedAt: 1, agent_id: 'test-model' },
      { id: 'sess-B', title: 'B', messages: [], updatedAt: 2, agent_id: 'test-model' },
    ];
    await act(async () => { await result.current.startStream(baseParams('sess-A')); });
    await act(async () => { await result.current.startStream({ ...baseParams('sess-B'), userText: 'hi-B', resolvedInput: 'hi-B' }); });

    const capA = captures.find(c => c.sessionId === 'sess-A')!;
    act(() => { capA.onChunk('來自 A 的內容'); });

    const sA = result.current.getStream('sess-A')!;
    const sB = result.current.getStream('sess-B')!;
    const aAssistant = Object.values(sA.tree.messages).find(m => m.role === 'assistant')!;
    const bAssistant = Object.values(sB.tree.messages).find(m => m.role === 'assistant')!;
    expect(aAssistant.content).toBe('來自 A 的內容');
    expect(bAssistant.content).toBe(''); // B 完全未受影響
    expect(sA.status).toBe('streaming');
  });

  it('並發上限 5：第 6 條 startStream 回 false 且不建立 entry', async () => {
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    sessionsArr = Array.from({ length: 6 }, (_v, i) => ({ id: `s${i}`, title: `S${i}`, messages: [], updatedAt: i, agent_id: 'test-model' }));
    const results: boolean[] = [];
    await act(async () => {
      for (let i = 0; i < 6; i++) results.push(await result.current.startStream(baseParams(`s${i}`)));
    });
    expect(results.slice(0, 5).every(Boolean)).toBe(true);
    expect(results[5]).toBe(false);
    expect(result.current.getStream('s5')).toBeUndefined();
    expect(result.current.activeCount).toBe(5);
  });

  it('真 registry 額滿提示在切換語言後使用新翻譯', async () => {
    const { toast } = await import('sonner');
    vi.mocked(useTranslation).mockReturnValue({ t: (key: string) => `A:${key}` } as never);
    sessionsArr = Array.from({ length: 6 }, (_, index) => ({ id: `lang-${index}`, title: 'test', messages: [], updatedAt: 1 }));
    const { result, rerender } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { for (let index = 0; index < 5; index++) await result.current.startStream(baseParams(`lang-${index}`)); });
    await act(async () => { expect(await result.current.startStream(baseParams('lang-5'))).toBe(false); });
    expect(toast.warning).toHaveBeenLastCalledWith('A:hooks.use_chat_stream.background_limit_reached');
    vi.mocked(useTranslation).mockReturnValue({ t: (key: string) => `B:${key}` } as never);
    rerender();
    await act(async () => { expect(await result.current.startStream(baseParams('lang-5'))).toBe(false); });
    expect(toast.warning).toHaveBeenLastCalledWith('B:hooks.use_chat_stream.background_limit_reached');
    expect(result.current.activeCount).toBe(5);
    expect(captures).toHaveLength(5);
  });

  it('stopStream：abort signal、送精確 stream fence 並保留未確認富樹', async () => {
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream(baseParams('sess-A')); });
    const cap = captures.find(c => c.sessionId === 'sess-A')!;
    expect(cap.signal.aborted).toBe(false);

    await act(async () => { result.current.stopStream('sess-A'); });
    expect(cap.signal.aborted).toBe(true);
    expect(result.current.getStream('sess-A')?.status).toBe('error');
    expect(result.current.activeCount).toBe(0);
    expect(mocks.fetchTouka).toHaveBeenCalledWith(
      'http://localhost:8000', '/v1/ui/sessions/sess-A/stop?stream_id=stream-sess-A', expect.objectContaining({ method: 'POST' }),
    );
  });

  it('沒有 terminal 的 onDone：保留完整樹並標 unknown，不假成功或再保存', async () => {
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream(baseParams('sess-A')); });
    const cap = captures.find(c => c.sessionId === 'sess-A')!;
    act(() => { cap.onChunk('最終回應'); });
    await act(async () => { await cap.onDone(); });

    expect(result.current.getStream('sess-A')?.persistence).toMatchObject({ status: 'unknown' });
    expect(result.current.activeCount).toBe(0);
    expect(mocks.syncSession).toHaveBeenCalledTimes(1);
    const synced = sessionsArr[0];
    expect(synced.id).toBe('sess-A');
    // 寫回的 messages 為 { messages, currentId } 樹格式，且含完成的 assistant 內容
    expect(synced.messages).toHaveProperty('currentId');
    expect(synced.messages).toHaveProperty('messages');
    const finalRoles = Object.values(
      synced.messages.messages as Record<string, { role: string; content: string }>
    );
    expect(finalRoles.find(m => m.role === 'assistant')?.content).toBe('最終回應');
  });

  it('onError（非 AbortError）：toast 提示並保留非 active entry', async () => {
    const { toast } = await import('sonner');
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream(baseParams('sess-A')); });
    const cap = captures.find(c => c.sessionId === 'sess-A')!;
    await act(async () => { await cap.onError(new Error('network boom')); });
    expect(toast.error).toHaveBeenCalled();
    expect(result.current.getStream('sess-A')?.status).toBe('error');
    expect(result.current.activeCount).toBe(0);
  });

  it('onError（AbortError）：不提示並保留非 active entry', async () => {
    const { toast } = await import('sonner');
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => { await result.current.startStream(baseParams('sess-A')); });
    const cap = captures.find(c => c.sessionId === 'sess-A')!;
    const abortErr = new Error('aborted');
    abortErr.name = 'AbortError';
    await act(async () => { await cap.onError(abortErr); });
    expect(toast.error).not.toHaveBeenCalled();
    expect(result.current.getStream('sess-A')?.status).toBe('error');
    expect(result.current.activeCount).toBe(0);
  });

  // G10（守衛涵蓋缺口／ADR-0149）：onEvent 必須把 skill_creation_approval_request 與
  // skill_install_approval_request 同路由到 onSkillInstallRequest（→ EvolutionModal）。
  // OR 條件退化（漏掉 creation）→ 事件過白名單卻無 handler → 審批卡不彈、部署逾時靜默失敗。
  // 既有測試從不用 skill 事件驅動 onEvent。
  it('onEvent 路由 skill_creation/install_approval_request 到 onSkillInstallRequest', async () => {
    const onSkillInstallRequest = vi.fn();
    const { result } = renderHook(() => useStreamRegistry(), { wrapper });
    await act(async () => {
      await result.current.startStream({ ...baseParams('sess-A'), onSkillInstallRequest });
    });
    const cap = captures.find(c => c.sessionId === 'sess-A')!;

    act(() => { cap.onEvent({ type: 'skill_creation_approval_request', request_id: 'r-c' }); });
    expect(onSkillInstallRequest).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'skill_creation_approval_request' }),
    );

    act(() => { cap.onEvent({ type: 'skill_install_approval_request', request_id: 'r-i' }); });
    expect(onSkillInstallRequest).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'skill_install_approval_request' }),
    );
  });
});
