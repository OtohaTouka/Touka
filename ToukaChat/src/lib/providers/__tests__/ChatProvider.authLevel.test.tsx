/**
 * ChatProvider — auth_level 前端持久化鏈（四功能區調查 P4／ADR-0143 H1 前端半段）。
 *
 * 病史：後端 test_session_auth_level_persistence 鎖住「API→DB→ToolLayer 讀出」，但
 * 前端半段（handleSetAuthLevel 更新 session.auth_level＋syncSession、切 session 從
 * sess.auth_level 還原）零測。本檔補齊——這也是 deepResearch 那類「前端放進 session
 * 卻沒真正持久化」的對照防線。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

const syncSession = vi.fn().mockResolvedValue({ status: 'saved', representation: 'full', revision: 'r1' });
// 模型選單：is_agent 的才是靈魂；setSelectedModel 共用一個 spy，才能檢查 effect 有沒有把選擇拉回
let models: { availableModels: { id: string; is_agent?: boolean }[]; selectedModel: string } = { availableModels: [], selectedModel: 'm1' };
const setSelectedModel = vi.fn();
const handleSendStream = vi.fn();
let sessions: Record<string, unknown>[] = [];
let replay = false;
let streamStatus: string | undefined;
// 執行 updater（handleSetAuthLevel 的 syncSession 呼叫在 setSessions 的 updater 內部）
const setSessions = vi.fn((u: unknown) => {
  if (typeof u === 'function') {
    if (replay) (u as (p: typeof sessions) => typeof sessions)(sessions);
    sessions = (u as (p: typeof sessions) => typeof sessions)(sessions);
  }
});
const updateSession = vi.fn((id: string, change: (current: typeof sessions[number]) => typeof sessions[number]) => {
  const previous = sessions.find(session => session.id === id)!;
  const first = change(previous);
  expect(change(previous)).toEqual(first);
  sessions = sessions.map(session => session.id === id ? first : session);
  return syncSession(first);
});

vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: Object.assign(() => ({}), { getState: () => ({ setManuallyClosed: vi.fn() }) }),
}));
vi.mock('@/hooks/useArtifactExtractor', () => ({
  useArtifactExtractor: () => ({ processStreamChunk: vi.fn(), finalizeStream: vi.fn() }),
}));
vi.mock('@/hooks/useChatStream', () => ({
  useChatStream: () => ({ handleSendStream, handleStopGeneration: vi.fn() }),
}));
vi.mock('../ModelProvider', () => ({
  useModels: () => ({ ...models, setSelectedModel }),
}));
// 存檔後重讀清單：預設回傳目前的本地清單（後端照存），個別測試改成後端存下的模式。
const refreshSessions = vi.fn(async () => sessions);
vi.mock('../SessionListProvider', () => ({
  useSessionList: () => ({ sessions, setSessions, syncSession, updateSession, refreshSessions }),
}));
vi.mock('../StreamRegistryProvider', () => ({
  useStreamRegistry: () => ({ getStream: () => streamStatus ? { status: streamStatus, tree: { messages: {}, currentId: null } } : undefined, streamingSessionIds: [], checkSessionStream: vi.fn() }),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
vi.mock('../../connections', () => ({ getActiveProxyUrl: () => 'http://x', fetchTouka: vi.fn() }));
vi.mock('sonner', () => ({ toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() } }));

import { toast } from 'sonner';
import { ChatProvider, useChat } from '../ChatProvider';

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <ChatProvider>{children}</ChatProvider>
);

describe('ChatProvider auth_level 前端持久化鏈（P4/ADR-0143 H1）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    replay = false;
    streamStatus = undefined;
    models = { availableModels: [], selectedModel: 'm1' };
    sessions = [{ id: 's1', title: 's1', messages: {}, updatedAt: 1, auth_level: 'YOLO' }];
  });
  it('user 保存期間仍是 sending，不能再次送出', () => {
    streamStatus = 'saving';
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => result.current.setActiveSessionId('s1'));
    expect(result.current.isSending).toBe(true);
  });

  it('整樹修改將未知保存結果傳回直接 consumer', async () => {
    const unknown = { status: 'unknown', error_code: 1302 };
    syncSession.mockResolvedValueOnce(unknown);
    const { result } = renderHook(() => useChat(), { wrapper });
    await act(async () => {
      expect(await result.current.handleUpdateSessionMessages('s1', [])).toEqual(unknown);
    });
  });
  it('重播 auth_level updater 不重複持久化同一操作', () => {
    replay = true;
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => result.current.setActiveSessionId('s1'));
    act(() => result.current.handleSetAuthLevel('Auto'));
    expect(syncSession).toHaveBeenCalledTimes(1);
  });

  it('切到某 session 時從 sess.auth_level 還原 authLevel', () => {
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    expect(result.current.authLevel).toBe('YOLO');
  });

  it('handleSetAuthLevel 更新 session.auth_level 並經 syncSession 持久化', () => {
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    act(() => { result.current.handleSetAuthLevel('Auto'); });

    expect(result.current.authLevel).toBe('Auto');
    // 持久化：syncSession 收到帶正確 auth_level 的 session（前端半段的落盤觸發點）
    expect(syncSession).toHaveBeenCalledWith(
      expect.objectContaining({ id: 's1', auth_level: 'Auto' }),
    );
  });

  it('新對話還沒有 agent_id：切模式時一起帶上目前選的靈魂（後端照樣驗證擁有權，R-2c ②#1）', () => {
    models = { availableModels: [{ id: 'Alice', is_agent: true }], selectedModel: 'Alice' };
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    act(() => { result.current.handleSetAuthLevel('Auto'); });
    expect(syncSession).toHaveBeenCalledWith(
      expect.objectContaining({ id: 's1', auth_level: 'Auto', agent_id: 'Alice' }),
    );
  });

  it('選的是一般模型時切模式不綁 agent_id（一般模型沒有工具，模式對它沒有意義）', () => {
    models = { availableModels: [{ id: 'gpt-x', is_agent: false }], selectedModel: 'gpt-x' };
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    act(() => { result.current.handleSetAuthLevel('Auto'); });
    expect(syncSession).toHaveBeenCalledTimes(1);
    expect(syncSession.mock.calls[0][0].agent_id).toBeUndefined();
  });

  it('先切模式再選靈魂：對話清單更新後選擇不被拉回預設模型，第一句送的是靈魂（R-2c ②）', () => {
    models = {
      availableModels: [{ id: 'gpt-x', is_agent: false }, { id: 'Alice', is_agent: true }],
      selectedModel: 'gpt-x',
    };
    const { result, rerender } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    act(() => { result.current.handleSetAuthLevel('Auto'); });
    models = { ...models, selectedModel: 'Alice' };
    sessions = sessions.map(s => ({ ...s })); // 任何存檔或 SSE 都會換掉清單
    rerender();
    expect(setSelectedModel).not.toHaveBeenCalledWith('gpt-x');
  });

  it('還沒有訊息的對話，第一句前換模型不會被 effect 拉回綁定的靈魂', () => {
    sessions[0].agent_id = 'Alice';
    models = { availableModels: [{ id: 'Alice', is_agent: true }, { id: 'Bob', is_agent: true }], selectedModel: 'Bob' };
    const { result, rerender } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    sessions = sessions.map(s => ({ ...s }));
    rerender();
    expect(setSelectedModel).not.toHaveBeenCalledWith('Alice');
  });

  it('已有訊息的對話照舊依綁定的靈魂還原選擇', () => {
    const root = { id: 'u', parentId: null, childrenIds: [], role: 'user', content: 'hi' };
    sessions[0] = { ...sessions[0], agent_id: 'Alice', messages: { messages: { u: root }, currentId: 'u' } };
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    expect(setSelectedModel).toHaveBeenCalledWith('Alice');
  });

  it('已綁靈魂的對話切模式時不改 agent_id', () => {
    sessions[0].agent_id = 'Alice';
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    act(() => { result.current.handleSetAuthLevel('YOLO'); });
    expect(syncSession).toHaveBeenCalledWith(
      expect.objectContaining({ id: 's1', auth_level: 'YOLO', agent_id: 'Alice' }),
    );
  });

  it('非授權者選 Auto：存檔後立即顯示後端實際存下的 Manual，並提示原因', async () => {
    refreshSessions.mockResolvedValueOnce([{ id: 's1', title: 's1', messages: {}, updatedAt: 2, auth_level: 'Manual' }]);
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    await act(async () => { result.current.handleSetAuthLevel('Auto'); });

    expect(refreshSessions).toHaveBeenCalledTimes(1);
    expect(result.current.authLevel).toBe('Manual');
    expect(toast.warning).toHaveBeenCalledWith('chat.auth_switcher.downgraded');
  });

  it('後端照存 YOLO 時維持 YOLO、不提示', async () => {
    sessions[0].auth_level = 'Manual';
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    await act(async () => { result.current.handleSetAuthLevel('YOLO'); });

    expect(refreshSessions).toHaveBeenCalledTimes(1);
    expect(result.current.authLevel).toBe('YOLO');
    expect(toast.warning).not.toHaveBeenCalled();
  });

  it('之後又換了模式時，舊的存檔回覆不提示也不覆蓋顯示', async () => {
    let release: (value: Record<string, unknown>[]) => void = () => {};
    refreshSessions.mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    await act(async () => { result.current.handleSetAuthLevel('Auto'); });
    await act(async () => { result.current.handleSetAuthLevel('YOLO'); });
    await act(async () => { release([{ id: 's1', title: 's1', messages: {}, updatedAt: 2, auth_level: 'Manual' }]); });

    expect(result.current.authLevel).toBe('YOLO');
    expect(toast.warning).not.toHaveBeenCalled();
  });

  it('存檔沒成功或選 Manual 時不重讀清單', async () => {
    syncSession.mockResolvedValueOnce({ status: 'failed', error_code: 1302 });
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    await act(async () => { result.current.handleSetAuthLevel('Auto'); });
    await act(async () => { result.current.handleSetAuthLevel('Manual'); });

    expect(syncSession).toHaveBeenCalledTimes(2);
    expect(refreshSessions).not.toHaveBeenCalled();
  });
  it.each(['delete', 'branch'])('%s 的樹轉換可重播而只發出一次保存命令', action => {
    const root = { id: 'u', parentId: null, childrenIds: ['a', 'b'], role: 'user', content: 'root' };
    const a = { id: 'a', parentId: 'u', childrenIds: [], role: 'assistant', content: 'A' };
    const b = { id: 'b', parentId: 'u', childrenIds: [], role: 'assistant', content: 'B', thinking: 'keep' };
    sessions[0].messages = { messages: { u: root, a, b }, currentId: 'a' };
    replay = true;
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => result.current.setActiveSessionId('s1'));
    act(() => action === 'delete' ? result.current.handleDeleteMessage('a') : result.current.handleSwitchMessage('b'));
    expect(updateSession).toHaveBeenCalledTimes(1);
    expect(syncSession).toHaveBeenCalledTimes(1);
    const stored = syncSession.mock.calls[0][0];
    expect(stored.messages.messages.b.thinking).toBe('keep');
    if (action === 'delete') expect(stored.messages.messages.a).toBeUndefined();
    else expect(stored.messages.currentId).toBe('b');
  });
});

describe('ChatProvider 空對話換模型後送出（R-2c ② 第 3 次複審）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    replay = false;
    streamStatus = undefined;
    models = { availableModels: [{ id: 'Alice', is_agent: true }, { id: 'gpt-x' }], selectedModel: 'gpt-x' };
    sessions = [{ id: 's1', title: 's1', messages: {}, updatedAt: 1, auth_level: 'Auto', agent_id: 'Alice' }];
  });

  it('還沒有訊息、綁的是別的 agent：先重綁並顯示後端存下的模式，再送出', async () => {
    refreshSessions.mockResolvedValueOnce([{ ...sessions[0], agent_id: 'gpt-x', auth_level: 'Manual' }]);
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    await act(async () => { await result.current.handleSend('hi'); });
    expect(syncSession).toHaveBeenCalledWith(expect.objectContaining({ id: 's1', agent_id: 'gpt-x' }));
    expect(result.current.authLevel).toBe('Manual');
    expect(handleSendStream).toHaveBeenCalledTimes(1);
    expect(syncSession.mock.invocationCallOrder[0]).toBeLessThan(handleSendStream.mock.invocationCallOrder[0]);
  });

  it('重綁沒存成功就不送出', async () => {
    syncSession.mockResolvedValueOnce({ status: 'failed', error_code: 1302 });
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    await act(async () => { await result.current.handleSend('hi'); });
    expect(handleSendStream).not.toHaveBeenCalled();
  });

  it.each([
    ['已有訊息', { messages: { messages: { u: { id: 'u', parentId: null, childrenIds: [], role: 'user', content: 'x' } }, currentId: 'u' } }],
    ['綁的就是選的模型', { agent_id: 'gpt-x' }],
  ])('%s：不重綁，直接送出', async (_label, patch) => {
    sessions[0] = { ...sessions[0], ...patch };
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    await act(async () => { await result.current.handleSend('hi'); });
    expect(syncSession).not.toHaveBeenCalled();
    expect(handleSendStream).toHaveBeenCalledTimes(1);
  });
});

describe('ChatProvider isIncognito（ADR-0246 U2c-6）', () => {
  it('目前對話是無痕時為 true，一般對話為 false', () => {
    sessions = [
      { id: 's1', title: 's1', messages: {}, updatedAt: 1, auth_level: 'Manual' },
      { id: 'inc', title: 'inc', messages: {}, updatedAt: 1, incognito: true },
    ];
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    expect(result.current.isIncognito).toBe(false);
    act(() => { result.current.setActiveSessionId('inc'); });
    expect(result.current.isIncognito).toBe(true);
  });

  it.each(['Auto', 'YOLO'])('無痕對話要設成 %s：留在 Manual、不存檔，並提示原因（U2c-9）', (level) => {
    vi.clearAllMocks();
    sessions = [{ id: 'inc', title: 'inc', messages: {}, updatedAt: 1, incognito: true }];
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('inc'); });
    act(() => { result.current.handleSetAuthLevel(level); });
    expect(result.current.authLevel).toBe('Manual');
    expect(updateSession).not.toHaveBeenCalled();
    expect(toast.warning).toHaveBeenCalledWith('chat.auth_switcher.incognito_manual');
  });
});
