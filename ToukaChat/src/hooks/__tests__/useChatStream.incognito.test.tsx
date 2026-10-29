/**
 * HAX G17 加固（批2 ⑤a）：無痕旗標來源不得依賴 sessions 清單（wipe 競態防護）。
 *
 * 根因：無痕 session 僅存於前端記憶體（syncSession 依 ADR-0086 永不落盤），而
 * refreshSessions 是整批覆蓋——任何刷新都會把無痕 session 從清單洗掉。舊版
 * handleSendStream 發送時從 `sessions.find(...)` 回查 incognito 旗標，session 被洗掉
 * 時查到 undefined → 該則訊息以「一般模式」送出 → 後端零殘留閘門（記憶寫入/soul
 * 載入）全部失守（隱私缺口）。
 *
 * 修法：ChatProvider 於 handleNewSession「誕生即登記」無痕 id（ref Set），經
 * `isIncognitoSession` 兜底查詢傳入 useChatStream——旗標來源 = 清單 ∪ 誕生登記。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useChatStream } from '../useChatStream';
import type { Session } from '@/lib/types';
import type { StartStreamParams } from '@/lib/providers/StreamRegistryProvider';

const h = vi.hoisted(() => ({
  startStream: vi.fn<(params: StartStreamParams) => Promise<boolean>>(),
  sessions: [] as Session[],
  save: vi.fn(), current: true,
}));

vi.mock('@/lib/providers/ModelProvider', () => ({
  useModels: () => ({ selectedModel: 'gemini-2.5-flash' }),
}));
vi.mock('@/lib/providers/SessionListProvider', () => ({
  useSessionList: () => ({ sessions: h.sessions,
    getSession: (id: string) => h.sessions.find(session => session.id === id),
    setSessions: (action: React.SetStateAction<Session[]>) => {
      if (typeof action === 'function') { action(h.sessions); h.sessions = action(h.sessions); } else h.sessions = action;
    },
    syncSession: (session: Session) => { h.sessions = [session, ...h.sessions.filter(item => item.id !== session.id)]; return h.save(session); },
    updateSession: (id: string, change: (session: Session) => Session) => {
      const current = h.sessions.find(session => session.id === id)!;
      const next = change(current);
      expect(change(current)).toEqual(next);
      h.sessions = h.sessions.map(session => session.id === id ? next : session);
      return h.save(next);
    },
  }),
}));
vi.mock('@/lib/providers/DebugProvider', () => ({
  useDebug: () => ({ setLastRequestPayload: vi.fn(), setIsSimulationMode: vi.fn() }),
}));
vi.mock('@/lib/providers/StreamRegistryProvider', () => ({
  useStreamRegistry: () => ({ startStream: h.startStream, stopStream: vi.fn() }),
}));
vi.mock('@/lib/mentions', () => ({ resolveMentions: async (t: string) => t }));
vi.mock('@/lib/connections', () => ({ getActiveConnection: () => ({}), getActiveProxyUrl: () => 'http://x',
  captureRequestScope: () => ({}), isRequestScopeCurrent: () => h.current }));

function renderStream(activeSessionId: string, isIncognitoSession?: (id: string) => boolean) {
  return renderHook(() =>
    useChatStream({
      staticHistory: { messages: {}, currentId: null },
      activeSessionId,
      setActiveSessionId: vi.fn(),
      setPendingInstall: vi.fn(),
      setPendingAsk: vi.fn(),
      isIncognitoSession,
    }),
  );
}

describe('useChatStream 無痕旗標來源（HAX G17 wipe 競態防護）', () => {
  beforeEach(() => {
    h.startStream.mockClear();
    h.startStream.mockResolvedValue(true);
    h.save.mockReset().mockImplementation(async (session: Session) => session.incognito
      ? { status: 'skipped', reason: 'incognito' } : { status: 'saved', representation: 'full', revision: 'r1' });
    h.current = true;
    h.sessions = [];
    window.localStorage.clear();
  });

  it('無痕 session 在清單中 → incognito: true（基線）', async () => {
    h.sessions = [
      { id: 'session-1', title: '無痕對話', messages: [], updatedAt: 1, incognito: true },
    ];
    const { result } = renderStream('session-1');

    await act(async () => { await result.current.handleSendStream('hi', null); });

    expect(h.startStream).toHaveBeenCalledWith(expect.objectContaining({ incognito: true }));
  });

  it('無痕 session 已被 refresh 洗掉（不在清單）→ 誕生登記兜底，incognito 仍為 true', async () => {
    h.sessions = []; // refreshSessions 整批覆蓋後，無痕 session 消失
    const { result } = renderStream('session-1', (id) => id === 'session-1');

    await act(async () => { await result.current.handleSendStream('hi', null); });

    expect(h.startStream).toHaveBeenCalledWith(expect.objectContaining({ incognito: true }));
  });

  it('一般 session 不在清單、登記也查無 → 不推測存在或自動重建', async () => {
    h.sessions = [];
    const { result } = renderStream('session-2', () => false);

    await act(async () => { await result.current.handleSendStream('hi', null); });

    expect(h.startStream).not.toHaveBeenCalled();
  });
  it('建立保存未知時不送模型，也不清輸入', async () => {
    h.save.mockResolvedValue({ status: 'unknown', error_code: 1302 });
    const { result } = renderStream('');
    const reset = vi.fn();
    await act(async () => { await result.current.handleSendStream('draft', null, undefined, reset); });
    expect(h.startStream).not.toHaveBeenCalled();
    expect(reset).not.toHaveBeenCalled();
  });
  it('綁定 agent 的純操作只保存一次，等待結果才送模型與清輸入', async () => {
    h.sessions = [{ id: 's1', title: 's1', messages: [], updatedAt: 1 }];
    let finish!: (value: unknown) => void;
    h.save.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const { result } = renderStream('s1');
    const reset = vi.fn();
    let pending!: Promise<void>;
    act(() => { pending = result.current.handleSendStream('draft', null, undefined, reset); });
    await act(async () => {});
    const early = { start: h.startStream.mock.calls.length, reset: reset.mock.calls.length, save: h.save.mock.calls.length };
    await act(async () => { finish({ status: 'saved', representation: 'full', revision: 'r1' }); await pending; });
    expect(early).toEqual({ start: 0, reset: 0, save: 1 });
    expect(h.startStream).toHaveBeenCalledTimes(1);
    expect(reset).toHaveBeenCalledTimes(1);
  });
  it('hook 擋下時把送出的對話 id 交給呼叫端（ADR-0292）', async () => {
    h.sessions = [{ id: 's1', title: 's1', messages: [], updatedAt: 1, agent_id: 'agent' }];
    const { result } = renderStream('s1');
    const blocked = vi.fn();
    await act(async () => { await result.current.handleSendStream('draft', null, undefined, vi.fn(), blocked); });
    h.startStream.mock.calls[0][0].onHookBlocked?.();
    expect(blocked).toHaveBeenCalledWith('s1');
  });
  it('registry 回報未啟動時保留輸入', async () => {
    h.sessions = [{ id: 's1', title: 's1', messages: [], updatedAt: 1, agent_id: 'agent' }];
    h.startStream.mockResolvedValue(false);
    const { result } = renderStream('s1');
    const reset = vi.fn();
    await act(async () => { await result.current.handleSendStream('draft', null, undefined, reset); });
    expect(reset).not.toHaveBeenCalled();
  });
});
