import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { toast } from 'sonner';
import { fetchTouka } from '@/lib/connections';
import type { Session } from '@/lib/types';
import { SessionListProvider, useSessionList } from '../SessionListProvider';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: () => 'http://synthetic-proxy', fetchTouka: vi.fn(),
  captureRequestScope: () => ({ proxyOrigin: 'http://synthetic-proxy', authenticatedPrincipalId: 'a', authEpoch: 1 }),
  isRequestScopeCurrent: () => true, subscribeCredentialScope: () => () => {},
}));
vi.mock('@/lib/error_handler', () => ({
  logSSOTError: vi.fn(), errorMsg: (code: string) => code,
  getErrorMessage: vi.fn(async () => 'safe error'),
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

const fetchMock = vi.mocked(fetchTouka);
const session = (id: string, extra: Partial<Session> = {}): Session => ({
  id, title: id, messages: { messages: {}, currentId: null }, updatedAt: 1,
  is_home: false, home_for_agent: null, agent_id: null, project_id: null,
  auth_level: 'Manual', active_skills: [], active_mcp_servers: [], context_summary: null, ...extra,
});
const response = (status: number, body: unknown): Response => new Response(
  JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } },
);
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <SessionListProvider>{children}</SessionListProvider>
);
const snapshot = () => response(200, { exists: true, revision: 'confirmed-a',
  session: { ...session('a'), last_activity_at: 1 } });
function expectDeleteSent() {
  expect(fetchMock).toHaveBeenCalledWith('http://synthetic-proxy', '/v1/ui/sessions/a',
    expect.objectContaining({ method: 'DELETE', headers: { 'If-Match': 'confirmed-a' } }));
}

async function mounted(incognito = false) {
  fetchMock.mockResolvedValue(response(200, [session('a'), session('b')]));
  const rendered = renderHook(() => useSessionList(), { wrapper });
  await waitFor(() => expect(rendered.result.current.sessions).toHaveLength(2));
  if (incognito) act(() => rendered.result.current.setSessions([session('a', { incognito }), session('b')]));
  fetchMock.mockClear();
  return rendered;
}

describe('刪除只有後端確認才能移除', () => {
  beforeEach(() => vi.clearAllMocks());

  it.each([403, 404, 500])('HTTP %i 保留項目並顯示固定錯誤', async status => {
    const { result } = await mounted();
    fetchMock.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce(response(status, { detail: 'synthetic-sensitive-detail' }));
    let deleted: unknown;
    await act(async () => { deleted = await result.current.deleteSession('a'); });
    expect(result.current.sessions.map(s => s.id)).toEqual(['a', 'b']);
    expectDeleteSent();
    expect(toast.error).toHaveBeenCalledTimes(1);
    const observedMessages = JSON.stringify(vi.mocked(toast.error).mock.calls);
    expect(observedMessages).not.toContain('synthetic-sensitive-detail');
    expect(deleted).toBe(false);
  });

  it.each([{}, { status: 'success' }, { status: 'success', session_id: 'another' }])(
    '200 非合法本筆成功內容 %j 不能移除', async body => {
      const { result } = await mounted();
      fetchMock.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce(response(200, body));
      let deleted: unknown;
      await act(async () => { deleted = await result.current.deleteSession('a'); });
      expect(result.current.sessions.map(s => s.id)).toEqual(['a', 'b']);
      expectDeleteSent();
      expect(toast.error).toHaveBeenCalledTimes(1);
      expect(deleted).toBe(false);
    },
  );

  it('斷線或無法解析的成功 body 保留資料，不反射原始例外', async () => {
    const { result } = await mounted();
    fetchMock.mockResolvedValueOnce(snapshot()).mockRejectedValueOnce(new Error('synthetic-sensitive-network-error'));
    let disconnected: unknown;
    await act(async () => { disconnected = await result.current.deleteSession('a'); });
    expect.soft(result.current.sessions.map(s => s.id)).toEqual(['a', 'b']);
    expectDeleteSent();
    fetchMock.mockResolvedValueOnce(new Response('{not json', { status: 200 }));
    let malformed: unknown;
    await act(async () => { malformed = await result.current.deleteSession('a'); });
    expect(result.current.sessions.map(s => s.id)).toEqual(['a', 'b']);
    expect(fetchMock.mock.calls.filter(([, , options]) => options?.method === 'DELETE')).toHaveLength(2);
    expect(toast.error).toHaveBeenCalledTimes(2);
    const observedMessages = JSON.stringify(vi.mocked(toast.error).mock.calls);
    expect(observedMessages).not.toContain('synthetic-sensitive');
    expect(disconnected).toBe(false);
    expect(malformed).toBe(false);
  });

  it('等待中保留項目，成功只刪指定 ID、不回滾其他對話新修改', async () => {
    const { result } = await mounted();
    let resolveDelete!: (value: Response) => void;
    fetchMock.mockResolvedValueOnce(snapshot()).mockReturnValueOnce(new Promise<Response>(resolve => { resolveDelete = resolve; }));
    let pending!: Promise<unknown>;
    act(() => { pending = result.current.deleteSession('a'); });
    await waitFor(() => expectDeleteSent());
    expect(result.current.sessions.map(s => s.id)).toEqual(['a', 'b']);
    act(() => result.current.setSessions(prev => prev.map(s => s.id === 'b' ? { ...s, title: 'newer' } : s)));
    let deleted: unknown;
    await act(async () => {
      resolveDelete(response(200, { status: 'success', session_id: 'a' }));
      deleted = await pending;
    });
    expect(result.current.sessions).toEqual([session('b', { title: 'newer' })]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(deleted).toBe(true);
  });

  it('失敗回來時也不回滾其他對話的新變更', async () => {
    const { result } = await mounted();
    let resolveDelete!: (value: Response) => void;
    fetchMock.mockResolvedValueOnce(snapshot()).mockReturnValueOnce(new Promise<Response>(resolve => { resolveDelete = resolve; }));
    let pending!: Promise<unknown>;
    act(() => { pending = result.current.deleteSession('a'); });
    await waitFor(() => expectDeleteSent());
    act(() => result.current.setSessions(prev => [...prev, session('c')]));
    await act(async () => { resolveDelete(response(500, {})); await pending; });
    expect(result.current.sessions.map(s => s.id)).toEqual(['a', 'b', 'c']);
  });

  it('無痕只本地移除，POST／DELETE HTTP 為零', async () => {
    const { result } = await mounted(true);
    let deleted: unknown;
    await act(async () => { deleted = await result.current.deleteSession('a'); });
    expect(result.current.sessions.map(s => s.id)).toEqual(['b']);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
    expect(deleted).toBe(true);
  });
});
