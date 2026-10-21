/**
 * refreshSessions merge 語意（批3 ⑤b）。
 *
 * 根因：舊版 refreshSessions 以後端清單「整批覆蓋」sessions state，而無痕 session
 * 依 ADR-0086 永不落盤（僅存前端記憶體）→ 任何刷新（掛載、每次切換對話）都把進行
 * 中的無痕對話從側欄洗掉——「切走即消失、串流完成又跳回」的反覆閃爍。
 *
 * 修法：merge——後端清單仍為 persisted session 的權威（更新/刪除以後端為準），
 * 本地僅保留 incognito session；合併後按 updatedAt desc 排序（對齊後端排序與
 * useSessionEvents 匯入慣例）；id 防禦性去重（無痕理論上不可能出現在後端，若有
 * 殘留以本地無痕為準）。回傳值維持後端清單（呼叫端語意不變：handleSwitchSession
 * 以其查 persisted 最新訊息）。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { SessionListProvider, useSessionList } from '../SessionListProvider';
import { fetchTouka } from '@/lib/connections';
import type { Session } from '@/lib/types';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://x'),
  fetchTouka: vi.fn(),
  captureRequestScope: () => ({ proxyOrigin: 'http://x', authenticatedPrincipalId: 'a', authEpoch: 1 }),
  isRequestScopeCurrent: () => true, subscribeCredentialScope: () => () => {},
}));
vi.mock('@/lib/error_handler', () => ({
  logSSOTError: vi.fn(),
  getErrorMessage: vi.fn(async () => 'x'),
  errorMsg: vi.fn(() => 'x'),
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

const fetchToukaMock = vi.mocked(fetchTouka);

function okJson(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <SessionListProvider>{children}</SessionListProvider>
);

const sess = (id: string, updatedAt: number, over: Partial<Session> = {}): Session =>
  ({ id, title: id, messages: [], updatedAt, ...over }) as Session;

async function mountWithLocal(local: Session[]) {
  fetchToukaMock.mockResolvedValue(okJson([])); // 掛載 GET 回空
  const rendered = renderHook(() => useSessionList(), { wrapper });
  await waitFor(() => expect(fetchToukaMock).toHaveBeenCalled());
  act(() => rendered.result.current.setSessions(local));
  return rendered;
}

describe('refreshSessions merge（批3 ⑤b）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('本地無痕 session 不被後端清單洗掉', async () => {
    const { result } = await mountWithLocal([
      sess('incog-1', 300, { incognito: true }),
      sess('old-a', 100),
    ]);
    fetchToukaMock.mockResolvedValueOnce(okJson([sess('a', 200), sess('b', 150)]));

    await act(async () => { await result.current.refreshSessions(); });

    const ids = result.current.sessions.map(s => s.id);
    expect(ids).toContain('incog-1');           // 無痕保留
    expect(ids).toEqual(expect.arrayContaining(['a', 'b']));
    expect(ids).not.toContain('old-a');          // 非無痕以後端為權威（已不在後端＝移除）
  });

  it('合併後按 updatedAt desc 排序（無痕插在正確時間位置）', async () => {
    const { result } = await mountWithLocal([sess('incog-1', 250, { incognito: true })]);
    fetchToukaMock.mockResolvedValueOnce(okJson([sess('new', 300), sess('old', 100)]));

    await act(async () => { await result.current.refreshSessions(); });

    expect(result.current.sessions.map(s => s.id)).toEqual(['new', 'incog-1', 'old']);
  });

  it('後端版本覆蓋本地同 id 的非無痕 session（persisted 權威不變）', async () => {
    const { result } = await mountWithLocal([sess('a', 100, { title: 'stale-local' })]);
    fetchToukaMock.mockResolvedValueOnce(okJson([sess('a', 200, { title: 'fresh-backend' })]));

    await act(async () => { await result.current.refreshSessions(); });

    expect(result.current.sessions).toHaveLength(1);
    expect(result.current.sessions[0].title).toBe('fresh-backend');
  });

  it('防禦性去重：本地無痕與後端同 id → 只留本地無痕一筆', async () => {
    const { result } = await mountWithLocal([sess('dup', 300, { incognito: true, title: 'local-incog' })]);
    fetchToukaMock.mockResolvedValueOnce(okJson([sess('dup', 200, { title: 'backend-ghost' })]));

    await act(async () => { await result.current.refreshSessions(); });

    const dups = result.current.sessions.filter(s => s.id === 'dup');
    expect(dups).toHaveLength(1);
    expect(dups[0].incognito).toBe(true);
  });

  it('回傳值與合併狀態相同，保留本機無痕供切換頁面使用', async () => {
    const { result } = await mountWithLocal([sess('incog-1', 300, { incognito: true })]);
    fetchToukaMock.mockResolvedValueOnce(okJson([sess('a', 200)]));

    let returned: Session[] = [];
    await act(async () => { returned = await result.current.refreshSessions(); });

    expect(returned).toEqual(result.current.sessions);
    expect(returned.map(s => s.id)).toContain('a');
    expect(returned.some(s => s.incognito)).toBe(true);
  });
});
