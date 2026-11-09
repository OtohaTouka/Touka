/**
 * ChatProvider — 設家與取消家：後端只收正式靈魂 ID；同一個靈魂的舊寫法（ToukaSoul 前綴）也算舊家。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import type { Session } from '../../types';
import { applySetHome } from '../../session_persistence';

let sessions: Session[] = [];
const applySavedHome = vi.fn();
const fetchTouka = vi.fn();
const refreshSessions = vi.fn(async () => []);

vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: Object.assign(() => ({}), { getState: () => ({ setManuallyClosed: vi.fn() }) }),
}));
vi.mock('@/hooks/useArtifactExtractor', () => ({
  useArtifactExtractor: () => ({ processStreamChunk: vi.fn(), finalizeStream: vi.fn() }),
}));
vi.mock('@/hooks/useChatStream', () => ({
  useChatStream: () => ({ handleSendStream: vi.fn(), handleStopGeneration: vi.fn() }),
}));
vi.mock('../ModelProvider', () => ({
  useModels: () => ({ availableModels: [], selectedModel: '', setSelectedModel: vi.fn() }),
}));
vi.mock('../SessionListProvider', () => ({
  useSessionList: () => ({ sessions, setSessions: vi.fn(), applySavedHome, syncSession: vi.fn(), updateSession: vi.fn(), refreshSessions }),
}));
vi.mock('../StreamRegistryProvider', () => ({
  useStreamRegistry: () => ({ getStream: () => undefined, streamingSessionIds: [], checkSessionStream: vi.fn() }),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
vi.mock('../../connections', () => ({
  getActiveProxyUrl: () => 'http://x',
  fetchTouka: (...args: unknown[]) => fetchTouka(...args),
  captureRequestScope: vi.fn(),
  isRequestScopeCurrent: () => true,
}));
vi.mock('sonner', () => ({ toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() } }));

import { ChatProvider, useChat } from '../ChatProvider';
import { toast } from 'sonner';

const wrapper = ({ children }: { children: React.ReactNode }) => <ChatProvider>{children}</ChatProvider>;

const session = (id: string, agentId: string | undefined, isHome = false): Session => ({
  id, title: id, messages: [], updatedAt: 1, agent_id: agentId, is_home: isHome, home_for_agent: isHome ? agentId : null,
});

const sentBody = () => JSON.parse(fetchTouka.mock.calls[0][2].body as string) as { session_id: string; agent_id: string; is_home: boolean };

function applyLastUpdate(): Record<string, boolean | undefined> {
  const [id, agentId, isHome] = applySavedHome.mock.calls[0] as [string, string, boolean];
  return Object.fromEntries(applySetHome(sessions, id, agentId, isHome).map(item => [item.id, item.is_home]));
}

describe('ChatProvider 設家與取消家', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchTouka.mockResolvedValue({ ok: true });
  });

  it('對話是前綴寫法：送正式 ID', async () => {
    sessions = [session('prefixed', 'ToukaSoulAlice')];
    const { result } = renderHook(() => useChat(), { wrapper });
    await act(async () => { await result.current.handleSetHome('prefixed'); });
    expect(sentBody()).toEqual({ session_id: 'prefixed', agent_id: 'Alice', is_home: true });
  });

  it('取消家也送正式 ID', async () => {
    sessions = [session('prefixed', 'ToukaSoulAlice', true)];
    const { result } = renderHook(() => useChat(), { wrapper });
    await act(async () => { await result.current.handleUnsetHome('prefixed'); });
    expect(sentBody()).toEqual({ session_id: 'prefixed', agent_id: 'Alice', is_home: false });
    expect(applyLastUpdate()).toEqual({ prefixed: false });
  });

  it('舊家是前綴寫法、新家是正式 ID：算同一個靈魂，舊家取消；別的靈魂的家不動', async () => {
    sessions = [
      session('old', 'ToukaSoulAlice', true),
      session('new', 'Alice'),
      session('other', 'Bob', true),
    ];
    const { result } = renderHook(() => useChat(), { wrapper });
    await act(async () => { await result.current.handleSetHome('new'); });
    expect(applyLastUpdate()).toEqual({ old: false, new: true, other: true });
  });

  it('舊家是正式 ID、新家是前綴寫法：一樣算同一個靈魂', async () => {
    sessions = [session('old', 'Alice', true), session('new', 'ToukaSoulAlice')];
    const { result } = renderHook(() => useChat(), { wrapper });
    await act(async () => { await result.current.handleSetHome('new'); });
    expect(applyLastUpdate()).toEqual({ old: false, new: true });
  });

  it('已經是家的再設一次：還是家', async () => {
    sessions = [session('home', 'Alice', true)];
    const { result } = renderHook(() => useChat(), { wrapper });
    await act(async () => { await result.current.handleSetHome('home'); });
    expect(applyLastUpdate()).toEqual({ home: true });
  });

  it('沒有綁模型的對話：不送請求（不再猜成群聊）', async () => {
    sessions = [session('bare', undefined)];
    const { result } = renderHook(() => useChat(), { wrapper });
    await act(async () => {
      await result.current.handleSetHome('bare');
      await result.current.handleUnsetHome('bare');
    });
    expect(fetchTouka).not.toHaveBeenCalled();
  });

  it('後端拒絕：畫面不動', async () => {
    fetchTouka.mockResolvedValue({ ok: false });
    sessions = [session('old', 'Alice', true), session('new', 'Alice')];
    const { result } = renderHook(() => useChat(), { wrapper });
    await act(async () => { await result.current.handleSetHome('new'); });
    expect(applySavedHome).not.toHaveBeenCalled();
  });
});

describe('ChatProvider 設家的結果', () => {
  const home = () => renderHook(() => useChat(), { wrapper }).result;

  beforeEach(() => {
    vi.clearAllMocks();
    sessions = [session('a', 'Alice')];
    fetchTouka.mockResolvedValue({ ok: true });
  });

  it.each([['handleSetHome'], ['handleUnsetHome']] as const)('%s 成功後重讀清單：舊家由後端推導', async (action) => {
    const result = home();
    await act(async () => { await result.current[action]('a'); });
    expect(refreshSessions).toHaveBeenCalledTimes(1);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('被拒絕：提示原因，畫面不跟著改', async () => {
    fetchTouka.mockResolvedValue(new Response(JSON.stringify({ detail: { code: 'SESSION_OLD_HOME_READ_ONLY' } }), { status: 409 }));
    const result = home();
    await act(async () => { await result.current.handleSetHome('a'); });
    expect(applySavedHome).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledTimes(1);
  });

  it('連線失敗也要有提示', async () => {
    fetchTouka.mockRejectedValue(new Error('network'));
    const result = home();
    await act(async () => { await result.current.handleUnsetHome('a'); });
    expect(applySavedHome).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledTimes(1);
  });

  it('對話沒有靈魂：按了也要有提示', async () => {
    sessions = [session('bare', undefined)];
    const result = home();
    await act(async () => { await result.current.handleSetHome('bare'); });
    expect(fetchTouka).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledTimes(1);
  });

  it('不再提供「家」按鈕的舊入口', () => {
    expect(home().current).not.toHaveProperty('handleHomeSession');
  });
});
