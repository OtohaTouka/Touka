/**
 * 下載單筆新對話（ADR-0300）：查工作狀態時不重抓整份清單，新出現的對話一筆一筆加。
 * 加進來的對話要帶版本：下一則才存得進去，這是這個功能最要擋的回歸。
 */
import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchTouka } from '@/lib/connections';
import type { Session } from '@/lib/types';
import { SessionListProvider, useSessionList } from '../SessionListProvider';

const scope = vi.hoisted(() => ({ current: true }));

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: () => 'http://synthetic-proxy', fetchTouka: vi.fn(),
  captureRequestScope: () => ({ proxyOrigin: 'http://synthetic-proxy', authenticatedPrincipalId: 'a', authEpoch: 1 }),
  isRequestScopeCurrent: () => scope.current, subscribeCredentialScope: () => () => {},
}));
vi.mock('@/lib/error_handler', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/error_handler')>()),
  logSSOTError: vi.fn(), errorMsg: (code: string) => code,
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

const fetchMock = vi.mocked(fetchTouka);
const json = (status: number, body: unknown): Response => new Response(
  JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } },
);
const stored = (id: string, extra: Partial<Session> = {}): Session => ({
  id, title: id, messages: { messages: {}, currentId: null }, updatedAt: 1,
  is_home: false, home_for_agent: null, agent_id: null, project_id: null,
  auth_level: 'Manual', active_skills: [], active_mcp_servers: [], context_summary: null, ...extra,
});
const OK_SAVE = { status: 'success', session_id: 'new', persistence: { status: 'saved', representation: 'full', revision: 'r2' } };
const found = (id: string, extra: Partial<Session> = {}) => json(200, {
  exists: true, revision: 'r1', session: { ...stored(id, extra), last_activity_at: 1 },
});
const wrapper = ({ children }: { children: React.ReactNode }) => <SessionListProvider>{children}</SessionListProvider>;

async function mounted(list: Session[] = [stored('a')]) {
  fetchMock.mockResolvedValueOnce(json(200, list));
  const rendered = renderHook(() => useSessionList(), { wrapper });
  await waitFor(() => expect(rendered.result.current.listLoaded).toBe(true));
  fetchMock.mockClear();
  return rendered;
}

beforeEach(() => {
  vi.clearAllMocks();
  fetchMock.mockReset();
  scope.current = true;
});

describe('listLoaded', () => {
  it('清單讀到前是 false；讀失敗也是 false', async () => {
    fetchMock.mockResolvedValueOnce(json(500, {}));
    const { result } = renderHook(() => useSessionList(), { wrapper });
    expect(result.current.listLoaded).toBe(false);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await act(async () => {});
    expect(result.current.listLoaded).toBe(false);
  });
});

describe('discoverSession', () => {
  it('下載單筆加進清單，帶版本與輪數；不重抓整份', async () => {
    const { result } = await mounted();
    const messages = {
      currentId: 'r',
      messages: {
        u: { id: 'u', role: 'user', content: 'hi', parentId: null, childrenIds: ['r'] },
        r: { id: 'r', role: 'assistant', content: 'yo', parentId: 'u', childrenIds: [] },
      },
    } as unknown as Session['messages'];
    fetchMock.mockResolvedValueOnce(found('new', { messages }));
    let outcome: unknown;
    await act(async () => { outcome = await result.current.discoverSession('new'); });
    expect(outcome).toBe('added');
    expect(result.current.getSession('new')).toMatchObject({ id: 'new', revision: 'r1', round_count: 1 });
    expect(fetchMock.mock.calls.map(call => call[1])).toEqual(['/v1/ui/sessions/new?snapshot=true']);
  });

  it('加進來之後再送一則：直接用下載到的版本存，不被當成未確認', async () => {
    const { result } = await mounted();
    fetchMock.mockResolvedValueOnce(found('new'));
    await act(async () => { await result.current.discoverSession('new'); });
    fetchMock.mockClear();
    fetchMock.mockResolvedValueOnce(json(200, OK_SAVE));
    let saved: unknown;
    await act(async () => { saved = await result.current.updateSession('new', current => ({ ...current, title: '改過' })); });
    expect(saved).toMatchObject({ status: 'saved' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/v1/ui/sessions/new');
    expect(JSON.parse(init?.body as string).revision).toBe('r1');
  });

  it('清單拿掉又下載回來：閒置的舊 writer 用新的版本存', async () => {
    const { result } = await mounted();
    fetchMock.mockResolvedValueOnce(found('new'));
    await act(async () => { await result.current.discoverSession('new'); });
    fetchMock.mockResolvedValueOnce(json(200, OK_SAVE));
    await act(async () => { await result.current.updateSession('new', current => ({ ...current, title: '第一次' })); });

    fetchMock.mockResolvedValueOnce(json(200, [stored('a')]));
    await act(async () => { await result.current.refreshSessions(); });
    expect(result.current.getSession('new')).toBeUndefined();

    fetchMock.mockResolvedValueOnce(json(200, { exists: true, revision: 'r9', session: { ...stored('new'), last_activity_at: 1 } }));
    await act(async () => { await result.current.discoverSession('new'); });
    fetchMock.mockClear();
    fetchMock.mockResolvedValueOnce(json(200, { ...OK_SAVE, persistence: { status: 'saved', representation: 'full', revision: 'r10' } }));
    await act(async () => { await result.current.updateSession('new', current => ({ ...current, title: '第二次' })); });
    expect(JSON.parse(fetchMock.mock.calls[0][2]?.body as string).revision).toBe('r9');
  });

  it('清單已經有了：不下載', async () => {
    const { result } = await mounted();
    let outcome: unknown;
    await act(async () => { outcome = await result.current.discoverSession('a'); });
    expect(outcome).toBe('known');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('下載途中清單已經有了（重新整理帶回來）：不蓋掉較新的內容', async () => {
    const { result } = await mounted();
    let release!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>(resolve => { release = resolve; }));
    const pending = result.current.discoverSession('new');
    act(() => result.current.setSessions(current => [stored('new', { title: '較新', updatedAt: 9 }), ...current]));
    let outcome: unknown;
    await act(async () => {
      release(found('new', { title: '舊的' }));
      outcome = await pending;
    });
    expect(outcome).toBe('known');
    expect(result.current.getSession('new')?.title).toBe('較新');
  });

  it('不存在：回報 absent，不加進清單', async () => {
    const { result } = await mounted();
    fetchMock.mockResolvedValueOnce(json(200, { exists: false, revision: 'absent', session: null }));
    let outcome: unknown;
    await act(async () => { outcome = await result.current.discoverSession('gone'); });
    expect(outcome).toBe('absent');
    expect(result.current.getSession('gone')).toBeUndefined();
  });

  it.each([
    ['500', () => json(500, {}), 'retry'],
    ['斷線', () => { throw new Error('offline'); }, 'retry'],
    ['403', () => json(403, {}), 'denied'],
    ['舊格式被嚴格讀取擋下', () => json(500, { detail: { code: 'SESSION_SAVE_FAILED' } }), 'denied'],
    ['200 但內容看不懂', () => json(200, { nonsense: true }), 'denied'],
  ])('%s → 回報原因，不加進清單', async (_name, respond, expected) => {
    const { result } = await mounted();
    fetchMock.mockImplementationOnce(async () => respond());
    let outcome: unknown;
    await act(async () => { outcome = await result.current.discoverSession('new'); });
    expect(outcome).toBe(expected);
    expect(result.current.getSession('new')).toBeUndefined();
  });

  it('回應回來時身分換了：丟掉', async () => {
    const { result } = await mounted();
    let release!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>(resolve => { release = resolve; }));
    const pending = result.current.discoverSession('new');
    scope.current = false;
    let outcome: unknown;
    await act(async () => {
      release(found('new'));
      outcome = await pending;
    });
    expect(outcome).toBe('retry');
    expect(result.current.sessions.map(session => session.id)).toEqual(['a']);
  });
});

describe('刪掉後重建同一個編號（心跳對話的編號固定）', () => {
  it('舊的 writer 不留著：重建的對話下載後再存一次，用新的版本', async () => {
    const { result } = await mounted();
    fetchMock.mockResolvedValueOnce(json(200, { exists: true, revision: 'r0', session: { ...stored('a'), last_activity_at: 1 } }))
      .mockResolvedValueOnce(json(200, { status: 'success', session_id: 'a' }));
    let deleted: unknown;
    await act(async () => { deleted = await result.current.deleteSession('a'); });
    expect(deleted).toBe(true);
    expect(result.current.getPersistence('a')).toBeUndefined();

    fetchMock.mockClear();
    fetchMock.mockResolvedValueOnce(found('a', { title: '重建' }));
    await act(async () => { await result.current.discoverSession('a'); });
    fetchMock.mockClear();
    fetchMock.mockResolvedValueOnce(json(200, { ...OK_SAVE, session_id: 'a' }));
    let saved: unknown;
    await act(async () => { saved = await result.current.updateSession('a', current => ({ ...current, title: '再改' })); });
    expect(saved).toMatchObject({ status: 'saved' });
    expect(JSON.parse(fetchMock.mock.calls[0][2]?.body as string).revision).toBe('r1');
  });
});
