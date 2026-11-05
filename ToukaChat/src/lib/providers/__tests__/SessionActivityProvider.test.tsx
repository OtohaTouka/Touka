/**
 * 工作狀態的來源（ADR-0300）：查詢怎麼轉成狀態、什麼時候留小點、什麼時候下載新對話。
 * 最要擋的回歸：查狀態的過程不能重抓整份清單（這裡的 useSessionList 根本沒有 refreshSessions）。
 */
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';
import { ACTIVITY_POLL_MS, MAX_ACTIVITY_FAILURES, MAX_DISCOVERIES_PER_POLL } from '@/lib/session_activity';
import type { Session, SessionActivity } from '@/lib/types';
import { SessionActivityProvider, useSessionActivity } from '../SessionActivityProvider';

const state = vi.hoisted(() => ({
  sessions: [] as Session[],
  listLoaded: true,
  discoverSession: vi.fn(),
  streaming: [] as string[],
  awaiting: [] as string[],
  pending: [] as string[],
  active: '',
  ide: false,
  userId: 'user-1' as string | undefined,
  scopeCurrent: true,
  credentialListeners: [] as (() => void)[],
}));

vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: state.userId ? { id: state.userId } : null }) }));
vi.mock('@/lib/connections', () => ({
  fetchTouka: vi.fn(),
  captureRequestScope: () => ({ proxyOrigin: 'http://synthetic-proxy', authenticatedPrincipalId: 'a', authEpoch: 1 }),
  isRequestScopeCurrent: () => state.scopeCurrent,
  subscribeCredentialScope: (listener: () => void) => {
    state.credentialListeners.push(listener);
    return () => { state.credentialListeners = state.credentialListeners.filter(item => item !== listener); };
  },
}));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));
vi.mock('@/lib/bridge', () => ({ isIDE: () => state.ide }));
vi.mock('../SessionListProvider', () => ({
  useSessionList: () => ({ sessions: state.sessions, listLoaded: state.listLoaded, discoverSession: state.discoverSession }),
}));
vi.mock('../StreamRegistryProvider', () => ({
  useStreamRegistry: () => ({ streamingSessionIds: state.streaming, awaitingApprovalSessionIds: state.awaiting }),
}));
vi.mock('../ChatProvider', () => ({
  useChat: () => ({ activeSessionId: state.active, pendingRequestSessionIds: state.pending }),
}));

const fetchMock = vi.mocked(fetchTouka);
const session = (id: string, extra: Partial<Session> = {}): Session => ({ id, title: id, messages: [], updatedAt: 1, ...extra });
const activity = (extra: Partial<SessionActivity> = {}): SessionActivity => ({ streams: [], background: [], finished: [], cursor: 100, ...extra });
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
const heartbeat = (id: string, current = 2, total = 5) => ({ session_id: id, kind: 'heartbeat', current, total });

let value: ReturnType<typeof useSessionActivity>;
function Probe() {
  value = useSessionActivity();
  return null;
}
const tree = (chatVisible: boolean) => (
  <SessionActivityProvider chatVisible={chatVisible}><Probe /></SessionActivityProvider>
);

async function flush(ms = 0) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

/** 掛上去並跑完第一次查詢（第一次只拿游標）。 */
async function mount(first: Partial<SessionActivity> = {}, chatVisible = true) {
  fetchMock.mockResolvedValueOnce(ok(activity(first)));
  const rendered = render(tree(chatVisible));
  await flush();
  return rendered;
}

async function poll(body: Partial<SessionActivity> | Response) {
  fetchMock.mockResolvedValueOnce(body instanceof Response ? body : ok(activity(body)));
  await flush(ACTIVITY_POLL_MS);
}

const urls = () => fetchMock.mock.calls.map(call => call[1]);
const stored = () => JSON.parse(window.localStorage.getItem('touka_session_unseen:user-1') ?? '[]');

function setVisibility(visibility: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: visibility });
  document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => {
  vi.useFakeTimers();
  window.localStorage.clear();
  fetchMock.mockReset();
  vi.mocked(logSSOTError).mockClear();
  state.sessions = [session('a'), session('b')];
  state.listLoaded = true;
  state.discoverSession = vi.fn(async () => 'added');
  state.streaming = [];
  state.awaiting = [];
  state.pending = [];
  state.active = '';
  state.ide = false;
  state.userId = 'user-1';
  state.scopeCurrent = true;
  state.credentialListeners = [];
  setVisibility('visible');
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('查詢的節奏', () => {
  it('第一次不帶 since，之後帶上一次的游標', async () => {
    await mount({ cursor: 111 });
    await poll({ cursor: 222 });
    await poll({ cursor: 222 });
    expect(urls()).toEqual(['/v1/ui/activity', '/v1/ui/activity?since=111', '/v1/ui/activity?since=222']);
  });

  it('分頁藏起來不查，回到分頁馬上查一次', async () => {
    await mount();
    setVisibility('hidden');
    await flush(ACTIVITY_POLL_MS * 3);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fetchMock.mockResolvedValueOnce(ok(activity()));
    setVisibility('visible');
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('同時只跑一個請求：上一個還沒回來就不發下一個', async () => {
    fetchMock.mockReturnValueOnce(new Promise(() => {}));
    render(tree(true));
    await flush(ACTIVITY_POLL_MS * 3);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('卸載後不再查', async () => {
    const { unmount } = await mount();
    unmount();
    await flush(ACTIVITY_POLL_MS * 2);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('IDE 不查，只用本地登記', async () => {
    state.ide = true;
    state.streaming = ['a'];
    render(tree(true));
    await flush(ACTIVITY_POLL_MS);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(value.statusOf('a')).toEqual({ kind: 'replying' });
  });
});

describe('憑證換了', () => {
  it('還在路上的舊查詢取消，馬上重查，游標重來', async () => {
    await mount({ cursor: 111 });
    let signal: AbortSignal | null | undefined;
    fetchMock.mockImplementationOnce((_origin, _path, init) => {
      signal = init?.signal;
      return new Promise<Response>(() => {});
    });
    await flush(ACTIVITY_POLL_MS);
    fetchMock.mockResolvedValueOnce(ok(activity()));
    act(() => state.credentialListeners.forEach(listener => listener()));
    await flush();
    expect(signal?.aborted).toBe(true);
    expect(urls().at(-1)).toBe('/v1/ui/activity');
  });

  it('舊身分的回應晚到：丟掉', async () => {
    await mount();
    let deliver!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>(resolve => { deliver = resolve; }));
    await flush(ACTIVITY_POLL_MS);
    state.scopeCurrent = false;
    deliver(ok(activity({ background: [heartbeat('a')] })));
    await flush();
    expect(value.statusOf('a')).toBeNull();
  });

  it('換帳號：別處的狀態與小點都不留', async () => {
    const { rerender } = await mount({ background: [heartbeat('a')] });
    await poll({ background: [heartbeat('a')], finished: [{ session_id: 'b', finished_at: 1 }] });
    expect(value.statusOf('b')).toEqual({ kind: 'unseen' });
    state.userId = 'user-2';
    fetchMock.mockResolvedValueOnce(ok(activity()));
    rerender(tree(true));
    await flush();
    expect(value.statusOf('a')).toBeNull();
    expect(value.statusOf('b')).toBeNull();
  });
});

describe('狀態', () => {
  it('心跳帶進度；優先序是等您處理 ＞ 工作中 ＞ 小點', async () => {
    await mount({ background: [heartbeat('a')] });
    expect(value.statusOf('a')).toEqual({ kind: 'heartbeat', current: 2, total: 5 });
    state.awaiting = ['a'];
    await flush(ACTIVITY_POLL_MS);
    expect(value.statusOf('a')).toEqual({ kind: 'heartbeat', current: 2, total: 5 });
  });

  it('核准卡與提問都算等您處理，蓋過工作中', async () => {
    state.streaming = ['a', 'b'];
    state.awaiting = ['a'];
    state.pending = ['b'];
    await mount();
    expect(value.statusOf('a')).toEqual({ kind: 'awaiting' });
    expect(value.statusOf('b')).toEqual({ kind: 'awaiting' });
    expect(value.attention).toEqual({ kind: 'awaiting' });
  });

  it('看到在工作的對話記下時刻，結束紀錄用 finished_at', async () => {
    await mount();
    const before = Date.now();
    await poll({ background: [heartbeat('a')], finished: [{ session_id: 'b', finished_at: 500 }] });
    expect(value.activityTimeOf('a')).toBeGreaterThanOrEqual(before);
    expect(value.activityTimeOf('b')).toBe(500);
  });
});

describe('什麼時候留小點', () => {
  it('第一次查詢不留，之後的結束紀錄才留；開著的不留', async () => {
    state.active = 'a';
    await mount();
    expect(value.attention).toBeNull();
    await poll({ finished: [{ session_id: 'a', finished_at: 1 }, { session_id: 'b', finished_at: 2 }] });
    expect(value.statusOf('a')).toBeNull();
    expect(value.statusOf('b')).toEqual({ kind: 'unseen' });
    expect(stored()).toEqual(['b']);
  });

  it('心跳活動消失但沒有結束紀錄：不留（只看工作結束的轉換）', async () => {
    await mount({ background: [heartbeat('a')] });
    await poll({});
    expect(value.statusOf('a')).toBeNull();
  });

  it('別處的串流消失：留小點', async () => {
    await mount({ streams: ['a'] });
    expect(value.statusOf('a')).toEqual({ kind: 'replying' });
    await poll({});
    expect(value.statusOf('a')).toEqual({ kind: 'unseen' });
  });

  it('本分頁的串流結束：看著的不留，別段對話留', async () => {
    state.active = 'a';
    state.streaming = ['a', 'b'];
    const { rerender } = await mount();
    state.streaming = [];
    rerender(tree(true));
    await flush();
    expect(value.statusOf('a')).toBeNull();
    expect(value.statusOf('b')).toEqual({ kind: 'unseen' });
  });

  it('看著的對話結束：連一下都沒寫進瀏覽器（不只是之後被清掉）', async () => {
    state.active = 'a';
    state.streaming = ['a'];
    const { rerender } = await mount();
    const writes = vi.spyOn(window.localStorage, 'setItem');
    state.streaming = [];
    rerender(tree(true));
    await flush();
    expect(writes.mock.calls.filter(([key, written]) => key.startsWith('touka_session_unseen') && written.includes('"a"'))).toEqual([]);
    writes.mockRestore();
  });

  it('本分頁的串流查詢也看得到：結束時馬上停，不等下一次查詢', async () => {
    state.streaming = ['a'];
    const { rerender } = await mount({ streams: ['a'] });
    await poll({ streams: ['a'] });
    expect(value.statusOf('a')).toEqual({ kind: 'replying' });
    state.streaming = [];
    rerender(tree(true));
    await flush();
    expect(value.statusOf('a')).toEqual({ kind: 'unseen' });
  });

  it('本地串流結束前發出的查詢晚到：不多亮、不留假小點', async () => {
    state.active = 'a';
    state.streaming = ['a'];
    const { rerender } = await mount();
    let resolveLate!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>(resolve => { resolveLate = resolve; }));
    await flush(ACTIVITY_POLL_MS);
    // 回覆結束、馬上切走
    state.streaming = [];
    rerender(tree(true));
    await flush();
    state.active = 'b';
    rerender(tree(true));
    await flush();
    resolveLate(ok(activity({ streams: ['a'] })));
    await flush();
    expect(value.statusOf('a')).toBeNull();
    // 下一次查詢（結束之後才發出）後端已經沒有它，也不算別處的串流結束
    await poll({});
    expect(value.statusOf('a')).toBeNull();
  });

  it('在設定畫面時目前對話算沒在看；回到聊天清掉', async () => {
    state.active = 'a';
    const { rerender } = await mount({}, false);
    await poll({ finished: [{ session_id: 'a', finished_at: 1 }] });
    expect(value.statusOf('a')).toEqual({ kind: 'unseen' });
    rerender(tree(true));
    await flush();
    expect(value.statusOf('a')).toBeNull();
    expect(stored()).toEqual([]);
  });

  it('無痕的小點不寫進瀏覽器', async () => {
    state.sessions = [session('secret', { incognito: true }), session('a')];
    state.active = 'a';
    state.streaming = ['secret'];
    const { rerender } = await mount();
    state.streaming = [];
    rerender(tree(true));
    await flush();
    expect(value.statusOf('secret')).toEqual({ kind: 'unseen' });
    expect(window.localStorage.getItem('touka_session_unseen:user-1') ?? '').not.toContain('secret');
  });

  it('無痕的小點打開也會清', async () => {
    state.sessions = [session('secret', { incognito: true }), session('a')];
    state.active = 'a';
    state.streaming = ['secret'];
    const { rerender } = await mount();
    state.streaming = [];
    rerender(tree(true));
    await flush();
    expect(value.statusOf('secret')).toEqual({ kind: 'unseen' });
    state.active = 'secret';
    rerender(tree(true));
    await flush();
    expect(value.statusOf('secret')).toBeNull();
  });

  it('還沒下載到的新對話先記著小點；進清單才看得到', async () => {
    state.discoverSession.mockResolvedValueOnce('retry');
    const { rerender } = await mount();
    await poll({ finished: [{ session_id: 'new', finished_at: 1 }] });
    expect(stored()).toEqual(['new']);
    // 清單因為別的事變了（別段新對話）：還在排隊的不能被當成已刪掉
    state.sessions = [...state.sessions, session('other')];
    rerender(tree(true));
    await flush();
    expect(stored()).toEqual(['new']);
    state.sessions = [...state.sessions, session('new')];
    rerender(tree(true));
    await flush();
    expect(value.statusOf('new')).toEqual({ kind: 'unseen' });
  });

  it('小點留著重新掛上還在；對話被刪就清掉編號', async () => {
    window.localStorage.setItem('touka_session_unseen:user-1', JSON.stringify(['b', 'gone']));
    const { rerender } = await mount();
    expect(value.statusOf('b')).toEqual({ kind: 'unseen' });
    expect(stored()).toEqual(['b']);
    state.sessions = [session('a')];
    rerender(tree(true));
    await flush();
    expect(stored()).toEqual([]);
  });

  it('各帳號分開記', async () => {
    window.localStorage.setItem('touka_session_unseen:user-2', JSON.stringify(['b']));
    await mount();
    expect(value.statusOf('b')).toBeNull();
  });
});

describe('查不到時', () => {
  const failure = () => new Response('{}', { status: 500 });

  it(`連續 ${MAX_ACTIVITY_FAILURES} 次才清掉心跳；不留小點，只記 log`, async () => {
    await mount({ background: [heartbeat('a')], streams: ['b'] });
    for (let attempt = 1; attempt < MAX_ACTIVITY_FAILURES; attempt++) await poll(failure());
    expect(value.statusOf('a')).not.toBeNull();
    expect(logSSOTError).not.toHaveBeenCalled();
    await poll(failure());
    expect(value.statusOf('a')).toBeNull();
    expect(value.statusOf('b')).toBeNull();
    expect(logSSOTError).toHaveBeenCalledTimes(1);
  });

  it('連線錯誤和看不懂的回應都算失敗；中間成功一次就重算', async () => {
    await mount({ background: [heartbeat('a')] });
    fetchMock.mockRejectedValueOnce(new Error('offline'));
    await flush(ACTIVITY_POLL_MS);
    await poll(ok({ nonsense: true }));
    await poll({ background: [heartbeat('a')] });
    await poll(failure());
    await poll(failure());
    expect(value.statusOf('a')).not.toBeNull();
  });

  it('本地串流的狀態不受影響；恢復後結束紀錄照常', async () => {
    state.streaming = ['a'];
    await mount();
    for (let attempt = 0; attempt < MAX_ACTIVITY_FAILURES; attempt++) await poll(failure());
    expect(value.statusOf('a')).toEqual({ kind: 'replying' });
    await poll({ finished: [{ session_id: 'b', finished_at: 5 }] });
    expect(value.statusOf('b')).toEqual({ kind: 'unseen' });
  });

  it('游標在失敗時不動：恢復後補得到中間結束的', async () => {
    await mount({ cursor: 111 });
    await poll(failure());
    await poll({});
    expect(urls().at(-1)).toBe('/v1/ui/activity?since=111');
  });
});

describe('新對話', () => {
  it('清單沒有才下載單筆；已知的不下載', async () => {
    await mount();
    await poll({ background: [heartbeat('new'), heartbeat('a')], streams: ['new2'], finished: [{ session_id: 'new3', finished_at: 1 }] });
    expect(state.discoverSession.mock.calls.map(call => call[0]).sort()).toEqual(['new', 'new2', 'new3']);
  });

  it('清單還沒讀到先不下載；讀到才處理', async () => {
    state.listLoaded = false;
    const { rerender } = await mount({ background: [heartbeat('new')] });
    expect(state.discoverSession).not.toHaveBeenCalled();
    state.listLoaded = true;
    rerender(tree(true));
    await flush();
    expect(state.discoverSession).toHaveBeenCalledWith('new');
  });

  it('暫時失敗留在佇列，下一次再試；游標照常前進', async () => {
    state.discoverSession.mockResolvedValueOnce('retry');
    await mount({ cursor: 111 });
    await poll({ finished: [{ session_id: 'new', finished_at: 1 }], cursor: 222 });
    expect(state.discoverSession).toHaveBeenCalledTimes(1);
    await poll({ cursor: 222 });
    expect(state.discoverSession).toHaveBeenCalledTimes(2);
    expect(urls().at(-1)).toBe('/v1/ui/activity?since=222');
  });

  it('還在工作時查不到不算不存在；工作結束還查不到就放棄並清掉小點', async () => {
    state.discoverSession.mockResolvedValue('absent');
    await mount({ background: [heartbeat('new')] });
    await poll({ background: [heartbeat('new')] });
    const whileWorking = state.discoverSession.mock.calls.length;
    await poll({ finished: [{ session_id: 'new', finished_at: 1 }] });
    expect(state.discoverSession.mock.calls.length).toBe(whileWorking + 1);
    expect(stored()).toEqual([]);
    await poll({});
    expect(state.discoverSession.mock.calls.length).toBe(whileWorking + 1);
  });

  it('別處的串流還沒存檔時查不到：結束後再試一次，不能就此放棄', async () => {
    state.discoverSession.mockResolvedValueOnce('absent');
    await mount();
    await poll({ streams: ['new'] });
    expect(state.discoverSession).toHaveBeenCalledTimes(1);
    await poll({});
    expect(state.discoverSession).toHaveBeenCalledTimes(2);
  });

  it('上一批還沒下載完：下一次查詢不重複下載', async () => {
    state.discoverSession.mockReturnValueOnce(new Promise(() => {}));
    await mount();
    await poll({ background: [heartbeat('new')] });
    await poll({ background: [heartbeat('new')] });
    expect(state.discoverSession).toHaveBeenCalledTimes(1);
  });

  it('永久拒絕：這次掛載內不再試', async () => {
    state.discoverSession.mockResolvedValue('denied');
    await mount();
    await poll({ background: [heartbeat('new')] });
    await poll({ background: [heartbeat('new')] });
    expect(state.discoverSession).toHaveBeenCalledTimes(1);
  });

  it(`每次查詢最多下載 ${MAX_DISCOVERIES_PER_POLL} 筆，其餘留到下一次`, async () => {
    const many = Array.from({ length: MAX_DISCOVERIES_PER_POLL + 2 }, (_, index) => heartbeat(`new-${index}`));
    state.discoverSession.mockImplementation(async (id: string) => {
      state.sessions = [...state.sessions, session(id)];
      return 'added';
    });
    await mount();
    await poll({ background: many });
    expect(state.discoverSession).toHaveBeenCalledTimes(MAX_DISCOVERIES_PER_POLL);
    await poll({ background: many });
    expect(state.discoverSession).toHaveBeenCalledTimes(MAX_DISCOVERIES_PER_POLL + 2);
  });
});

describe('開著的對話有新內容', () => {
  it('別處寫入開著的對話：不再工作才提示；換對話或內容更新後消失', async () => {
    state.active = 'a';
    const { rerender } = await mount({ background: [heartbeat('a')] });
    expect(value.staleSessionId).toBeNull();
    await poll({ finished: [{ session_id: 'a', finished_at: 1 }] });
    expect(value.staleSessionId).toBe('a');
    state.sessions = [session('a', { revision: 'newer' }), session('b')];
    rerender(tree(true));
    await flush();
    expect(value.staleSessionId).toBeNull();
    await poll({ finished: [{ session_id: 'a', finished_at: 2 }] });
    expect(value.staleSessionId).toBe('a');
    state.active = 'b';
    rerender(tree(true));
    await flush();
    expect(value.staleSessionId).toBeNull();
  });

  it('別段對話結束：開著這段的提示不動', async () => {
    state.active = 'a';
    await mount();
    await poll({ finished: [{ session_id: 'a', finished_at: 1 }] });
    expect(value.staleSessionId).toBe('a');
    await poll({ finished: [{ session_id: 'b', finished_at: 2 }] });
    expect(value.staleSessionId).toBe('a');
  });

  it('還在工作時不提示；本分頁自己的回覆結束也不提示', async () => {
    state.active = 'a';
    state.streaming = ['a'];
    const { rerender } = await mount();
    await poll({ finished: [{ session_id: 'a', finished_at: 1 }] });
    expect(value.staleSessionId).toBeNull();
    state.streaming = [];
    rerender(tree(true));
    await flush();
    expect(value.staleSessionId).toBe('a');
    act(() => value.clearStale());
    state.streaming = ['a'];
    rerender(tree(true));
    await flush();
    state.streaming = [];
    rerender(tree(true));
    await flush();
    expect(value.staleSessionId).toBeNull();
  });
});

describe('沒有掛 Provider', () => {
  it('什麼都不亮（只讀清單的元件在別處也能用）', () => {
    render(<Probe />);
    expect(value.statusOf('a')).toBeNull();
    expect(value.attention).toBeNull();
  });
});
