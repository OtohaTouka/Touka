/**
 * 存檔被聊天側剪裁推進之後的保存（ADR-0302）：以剪過的存檔為底接上去、存一次，不跳提示；
 * 不是剪裁造成的照舊提示。一般保存、收尾保存、「僅重試保存」用同一套接法。
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import fixture from '../../../../../tests/fixtures/sliding_window_trim.json';
import { SessionListProvider, useSessionList } from '../SessionListProvider';
import { captureRequestScope, fetchTouka, isRequestScopeCurrent } from '@/lib/connections';
import { toast } from 'sonner';
import type { Session } from '@/lib/types';
import type { TreeHistory } from '@/lib/tree_history';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://x'),
  fetchTouka: vi.fn(),
  captureRequestScope: vi.fn(() => ({ proxyOrigin: 'http://x', authenticatedPrincipalId: 'access:a', authEpoch: 1 })),
  isRequestScopeCurrent: vi.fn(() => true),
  subscribeCredentialScope: vi.fn(() => () => {}),
}));
vi.mock('@/lib/error_handler', () => ({
  logSSOTError: vi.fn(),
  getErrorMessage: vi.fn(async () => '對話儲存失敗'),
  errorMsg: vi.fn(() => '網路連線異常'),
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

const fetchToukaMock = vi.mocked(fetchTouka);
const json = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as unknown as Response;
const tree = (value: unknown): TreeHistory => structuredClone(value) as TreeHistory;
const session = (messages: TreeHistory, over: Partial<Session> = {}): Session => ({
  id: 's1', title: 'home', messages, updatedAt: 1, agent_id: null, project_id: null, auth_level: 'Manual',
  active_mcp_servers: [], active_skills: [], context_summary: 'old summary', ...over });
const trimmed = (edit: (messages: TreeHistory) => void = () => {}) => {
  const messages = tree(fixture.after);
  edit(messages);
  return session(messages, { context_summary: 'new summary' });
};
const snapshot = (stored: Session, revision: string) => json({ exists: true, revision,
  session: { ...stored, is_home: true, home_for_agent: null, last_activity_at: 1 } });
const persistence = (value: object) => json({ status: 'success', session_id: 's1', persistence: value });
const failed = () => persistence({ status: 'failed', error_code: 1301 });
const saved = (revision: string) => persistence({ status: 'saved', representation: 'full', revision });
const appendNext = (current: Readonly<Session>): Session => {
  const messages = structuredClone(current.messages) as TreeHistory;
  messages.messages.n7.childrenIds = [...messages.messages.n7.childrenIds, 'u8'];
  messages.messages.u8 = { id: 'u8', role: 'user', content: 'next', parentId: 'n7', childrenIds: [] };
  messages.currentId = 'u8';
  return { ...current, messages };
};
const posts = () => fetchToukaMock.mock.calls.filter(([, , options]) => options?.method === 'POST')
  .map(([, , options]) => JSON.parse(options!.body as string));

/** 依序回 POST 的結果；讀存檔一律回 `stored`。 */
function serve(stored: Session, ...results: Response[]) {
  fetchToukaMock.mockImplementation(async (_proxy, _path, options) =>
    options?.method === 'POST' ? results.shift()! : snapshot(stored, 'r3'));
}

/** 掛上 Provider，畫面這段對話先和存檔（版本 `revision`）核對過。 */
async function mount(confirmed: Session, revision: string) {
  vi.clearAllMocks();
  vi.mocked(captureRequestScope).mockReturnValue({ proxyOrigin: 'http://x', authenticatedPrincipalId: 'access:a', authEpoch: 1 });
  vi.mocked(isRequestScopeCurrent).mockReturnValue(true);
  fetchToukaMock.mockImplementation(async (_proxy, path) =>
    String(path).includes('snapshot=true') ? snapshot(confirmed, revision) : json([]));
  const rendered = renderHook(() => useSessionList(), { wrapper: ({ children }: { children: React.ReactNode }) =>
    <SessionListProvider>{children}</SessionListProvider> });
  await act(async () => {});
  await act(async () => { expect(await rendered.result.current.discoverSession('s1')).toBe('added'); });
  fetchToukaMock.mockClear();
  return rendered;
}

describe('剪裁之後的保存（ADR-0302）', () => {
  it('下一則因剪裁被擋：接在剪過的存檔上存一次，不跳提示，畫面換成剪過的樣子', async () => {
    const before = session(tree(fixture.before));
    const { result } = await mount(before, 'r2');
    serve(trimmed(), failed(), saved('r4'));
    await act(async () => { expect(await result.current.updateSession('s1', appendNext)).toMatchObject({ status: 'saved', revision: 'r4' }); });
    const [, grafted] = posts();
    expect(grafted).toMatchObject({ revision: 'r3', context_summary: 'new summary' });
    expect(Object.keys(grafted.messages.messages).sort()).toEqual(['n4', 'n5', 'n6', 'n7', 'u8']);
    expect(Object.keys(result.current.getSession('s1')!.messages.messages).sort()).toEqual(['n4', 'n5', 'n6', 'n7', 'u8']);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('收尾保存等不到、剪裁先剪了文字版：接上本機的富版，不跳提示', async () => {
    const confirmed = session(tree(fixture.before));
    delete confirmed.messages.messages.n7;
    confirmed.messages.messages.n6.childrenIds = [];
    confirmed.messages.currentId = 'n6';
    const { result } = await mount(confirmed, 'r0');
    const producer = result.current.beginProducer('s1')!;
    serve(trimmed(messages => { delete messages.messages.n7.steps; }), failed(), saved('r4'));
    await act(async () => {
      expect(await result.current.completeProducer(producer, current => ({ ...current, messages: tree(fixture.before) }),
        { status: 'saved', representation: 'text', revision: 'r1' })).toMatchObject({ status: 'saved' });
    });
    const [first, grafted] = posts();
    expect(first.revision).toBe('r1');
    expect(grafted.messages.messages.n7.steps).toEqual([{ tool: 'x' }]);
    expect(grafted.messages.messages.n0).toBeUndefined();
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('不是剪裁造成的（例如心跳多寫了一則）：照舊提示，只送一次', async () => {
    const before = session(tree(fixture.before));
    const { result } = await mount(before, 'r2');
    serve(trimmed(messages => {
      messages.messages.n7.childrenIds = ['h8'];
      messages.messages.h8 = { id: 'h8', role: 'user', content: 'heartbeat', parentId: 'n7', childrenIds: [] };
      messages.currentId = 'h8';
    }), failed(), saved('r4'));
    await act(async () => { expect(await result.current.updateSession('s1', appendNext)).toMatchObject({ status: 'failed' }); });
    expect(posts()).toHaveLength(1);
    expect(toast.error).toHaveBeenCalled();
  });

  it('「僅重試保存」也接在剪過的存檔上，不把剪掉的寫回去', async () => {
    const before = session(tree(fixture.before));
    const { result } = await mount(before, 'r2');
    fetchToukaMock.mockImplementation(async () => { throw new Error('network'); });
    await act(async () => { expect(await result.current.updateSession('s1', appendNext)).toMatchObject({ status: 'unknown' }); });
    serve(trimmed(), saved('r4'));
    await act(async () => { expect(await result.current.recheckSession('s1')).toMatchObject({ status: 'failed' }); });
    await act(async () => { expect(await result.current.retrySaveSession('s1')).toMatchObject({ status: 'saved' }); });
    const [lost, retried] = posts();
    expect(lost.revision).toBe('r2');
    expect(retried.revision).toBe('r3');
    expect(Object.keys(retried.messages.messages).sort()).toEqual(['n4', 'n5', 'n6', 'n7', 'u8']);
  });
});
