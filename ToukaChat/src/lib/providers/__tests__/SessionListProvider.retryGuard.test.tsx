/**
 * 「僅重試保存」只在存檔沒有別人的新內容時可用（ADR-0313）：
 * 重試前拿存檔和失敗那次存檔根據的底比；對不上（心跳、別的裝置、工具寫過）就不准重試，
 * 改用「改用存檔內容」換上伺服器的版本。匯入只有在確認視窗同意覆蓋的那一次才蓋過同編號的對話。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
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

const node = (id: string, role: 'user' | 'assistant', parentId: string | null, extra: object = {}) =>
  ({ id, role, content: id, parentId, childrenIds: [] as string[], ...extra });
/** 依序把節點接成一條線。 */
function chain(...nodes: ReturnType<typeof node>[]): TreeHistory {
  const messages: TreeHistory['messages'] = {};
  for (const item of nodes) {
    messages[item.id] = structuredClone(item) as TreeHistory['messages'][string];
    if (item.parentId) messages[item.parentId].childrenIds.push(item.id);
  }
  return { messages, currentId: nodes[nodes.length - 1].id } as TreeHistory;
}
const baseNodes = [node('u1', 'user', null), node('a1', 'assistant', 'u1')];
const session = (messages: TreeHistory, over: Partial<Session> = {}): Session => ({
  id: 's1', title: 't', messages, updatedAt: 1, agent_id: null, project_id: null, auth_level: 'Manual',
  active_mcp_servers: [], active_skills: [], context_summary: null, ...over });
const base = () => session(chain(...baseNodes));
const withHeartbeat = () => session(chain(...baseNodes, node('h1', 'user', 'a1'), node('h2', 'assistant', 'h1')), { updatedAt: 5 });
const appendUser = (id: string) => (current: Readonly<Session>): Session => {
  const messages = structuredClone(current.messages) as TreeHistory;
  const parent = messages.currentId!;
  messages.messages[parent].childrenIds.push(id);
  messages.messages[id] = node(id, 'user', parent) as TreeHistory['messages'][string];
  messages.currentId = id;
  return { ...current, messages, updatedAt: current.updatedAt + 1 };
};

const snapshot = (stored: Session | null, revision: string) => json(stored
  ? { exists: true, revision, session: { ...stored, is_home: false, home_for_agent: null, last_activity_at: 1 } }
  : { exists: false, revision, session: null });
const persistence = (value: object) => json({ status: 'success', session_id: 's1', persistence: value });
const failed = () => persistence({ status: 'failed', error_code: 1301 });
const saved = (revision: string) => persistence({ status: 'saved', representation: 'full', revision });
const lost = () => { throw new Error('response lost'); };
const posts = () => fetchToukaMock.mock.calls.filter(([, , options]) => options?.method === 'POST')
  .map(([, , options]) => JSON.parse(options!.body as string));

/** 讀存檔回 `stored()`（版本 `revision()`）；POST 依序用 `results`，用完就丟錯（等於回應遺失）。 */
function serve(stored: () => Session | null, revision: () => string, ...results: (() => Response)[]) {
  fetchToukaMock.mockImplementation(async (_proxy, path, options) => {
    if (options?.method === 'POST') return (results.shift() ?? lost)();
    return String(path).includes('snapshot=true') ? snapshot(stored(), revision()) : json([]);
  });
}

async function render() {
  const rendered = renderHook(() => useSessionList(), { wrapper: ({ children }: { children: React.ReactNode }) =>
    <SessionListProvider>{children}</SessionListProvider> });
  await act(async () => {});
  return rendered;
}

/** 掛上 Provider，畫面這段對話先和存檔（版本 r1）核對過。 */
async function mount() {
  serve(base, () => 'r1');
  const rendered = await render();
  await act(async () => { expect(await rendered.result.current.discoverSession('s1')).toBe('added'); });
  fetchToukaMock.mockClear();
  return rendered;
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(captureRequestScope).mockReturnValue({ proxyOrigin: 'http://x', authenticatedPrincipalId: 'access:a', authEpoch: 1 });
  vi.mocked(isRequestScopeCurrent).mockReturnValue(true);
});

describe('「僅重試保存」只在存檔沒有別人的新內容時可用（ADR-0313）', () => {
  it('心跳寫過：不能重試、也不送出；改用存檔內容後看得到心跳的內容，下一則照存檔版本送出', async () => {
    const { result } = await mount();
    serve(withHeartbeat, () => 'r2', failed);
    await act(async () => { expect(await result.current.updateSession('s1', appendUser('u2'))).toMatchObject({ status: 'failed' }); });
    await act(async () => { expect(await result.current.recheckSession('s1')).toMatchObject({ status: 'failed' }); });
    expect(result.current.canRetrySave('s1')).toBe(false);
    expect(result.current.canAdoptStored('s1')).toBe(true);
    const sent = posts().length;
    await act(async () => { expect(await result.current.retrySaveSession('s1')).toMatchObject({ status: 'failed' }); });
    expect(posts()).toHaveLength(sent);

    await act(async () => { expect(await result.current.adoptStoredSession('s1')).toMatchObject({ status: 'saved', revision: 'r2' }); });
    const adopted = result.current.getSession('s1')!;
    expect(Object.keys(adopted.messages.messages).sort()).toEqual(['a1', 'h1', 'h2', 'u1']);
    expect(adopted.round_count).toBe(2);
    expect(result.current.getPersistence('s1')).toMatchObject({ status: 'saved', revision: 'r2' });
    expect(result.current.canAdoptStored('s1')).toBe(false);

    serve(withHeartbeat, () => 'r2', () => saved('r3'));
    await act(async () => { expect(await result.current.updateSession('s1', appendUser('u3'))).toMatchObject({ status: 'saved', revision: 'r3' }); });
    const next = posts().at(-1);
    expect(next.revision).toBe('r2');
    expect(Object.keys(next.messages.messages).sort()).toEqual(['a1', 'h1', 'h2', 'u1', 'u3']);
  });

  it('存檔沒人動過（送出時回應遺失），後端重啟過版本變了：仍可重試，帶存檔的版本送出', async () => {
    const { result } = await mount();
    serve(base, () => 'r9');
    await act(async () => { expect(await result.current.updateSession('s1', appendUser('u2'))).toMatchObject({ status: 'unknown' }); });
    await act(async () => { expect(await result.current.recheckSession('s1')).toMatchObject({ status: 'failed' }); });
    expect(result.current.canRetrySave('s1')).toBe(true);
    serve(base, () => 'r9', () => saved('r10'));
    await act(async () => { expect(await result.current.retrySaveSession('s1')).toMatchObject({ status: 'saved', revision: 'r10' }); });
    expect(posts().at(-1)).toMatchObject({ revision: 'r9' });
    expect(Object.keys(posts().at(-1).messages.messages).sort()).toEqual(['a1', 'u1', 'u2']);
  });

  it('這一頁新建、欄位不齊的對話：回應遺失後存檔沒人動過，仍可重試', async () => {
    serve(() => null, () => 'none');
    const { result } = await render();
    const fresh: Session = { id: 's1', title: 'new', messages: chain(...baseNodes), updatedAt: 1 };
    serve(() => null, () => 'absent', () => saved('r1'));
    await act(async () => { expect(await result.current.syncSession(fresh)).toMatchObject({ status: 'saved', revision: 'r1' }); });
    const stored = () => ({ ...base(), title: 'new' });
    serve(stored, () => 'r9');
    await act(async () => { expect(await result.current.updateSession('s1', appendUser('u2'))).toMatchObject({ status: 'unknown' }); });
    await act(async () => { expect(await result.current.recheckSession('s1')).toMatchObject({ status: 'failed' }); });
    expect(result.current.canRetrySave('s1')).toBe(true);
  });

  it('存檔前讀存檔失敗（清單讀到的對話沒有版本號）：存檔沒人動過，仍可重試', async () => {
    fetchToukaMock.mockImplementation(async (_proxy, path) => json(String(path).includes('snapshot=true') ? null : [base()]));
    const { result } = await render();
    await act(async () => { await result.current.refreshSessions(); });
    fetchToukaMock.mockImplementation(async () => { throw new Error('offline'); });
    await act(async () => { expect(await result.current.updateSession('s1', appendUser('u2'))).toMatchObject({ status: 'unknown' }); });
    serve(base, () => 'r1', () => saved('r2'));
    await act(async () => { expect(await result.current.recheckSession('s1')).toMatchObject({ status: 'failed' }); });
    expect(result.current.canRetrySave('s1')).toBe(true);
    await act(async () => { expect(await result.current.retrySaveSession('s1')).toMatchObject({ status: 'saved' }); });
    expect(posts().at(-1)).toMatchObject({ revision: 'r1' });
  });

  it('工具在存檔裡改了設定（啟用技能）：不能重試，不會默默撤銷', async () => {
    const { result } = await mount();
    serve(() => ({ ...base(), active_skills: ['web'] }), () => 'r2');
    await act(async () => { await result.current.updateSession('s1', appendUser('u2')); });
    await act(async () => { expect(await result.current.recheckSession('s1')).toMatchObject({ status: 'failed' }); });
    expect(result.current.canRetrySave('s1')).toBe(false);
    await act(async () => { await result.current.retrySaveSession('s1'); });
    expect(posts()).toHaveLength(1);
  });

  it('改用存檔內容會重讀一次，不用重新核對時留下的舊快照', async () => {
    const { result } = await mount();
    let stored = withHeartbeat();
    let revision = 'r2';
    serve(() => stored, () => revision, failed);
    await act(async () => { await result.current.updateSession('s1', appendUser('u2')); });
    await act(async () => { await result.current.recheckSession('s1'); });
    stored = { ...withHeartbeat(), title: 'renamed elsewhere' };
    revision = 'r3';
    await act(async () => { expect(await result.current.adoptStoredSession('s1')).toMatchObject({ status: 'saved', revision: 'r3' }); });
    expect(result.current.getSession('s1')).toMatchObject({ title: 'renamed elsewhere', revision: 'r3' });
  });

  it('改用存檔內容時存檔已被刪掉：回報找不到，不換內容', async () => {
    const { result } = await mount();
    let stored: Session | null = withHeartbeat();
    serve(() => stored, () => 'r2', failed);
    await act(async () => { await result.current.updateSession('s1', appendUser('u2')); });
    await act(async () => { await result.current.recheckSession('s1'); });
    stored = null;
    vi.mocked(toast.error).mockClear();
    await act(async () => { expect(await result.current.adoptStoredSession('s1')).toMatchObject({ status: 'failed' }); });
    expect(result.current.getSession('s1')!.messages.messages.u2).toBeDefined();
    expect(toast.error).toHaveBeenCalled();
  });

  describe('回覆收尾存檔失敗', () => {
    const rich = () => chain(...baseNodes, node('u2', 'user', 'a1'), node('a2', 'assistant', 'u2', { steps: [{ tool: 'x' }] }));
    const text = () => session(chain(...baseNodes, node('u2', 'user', 'a1'), node('a2', 'assistant', 'u2')), { updatedAt: 3 });
    async function closingFailed(storedRevision: string) {
      const rendered = await mount();
      const { result } = rendered;
      const producer = result.current.beginProducer('s1')!;
      serve(text, () => storedRevision, failed);
      await act(async () => {
        expect(await result.current.completeProducer(producer, current => ({ ...current, messages: rich() }),
          { status: 'saved', representation: 'text', revision: 'text-rev' })).toMatchObject({ status: 'failed' });
      });
      await act(async () => { expect(await result.current.recheckSession('s1')).toMatchObject({ status: 'failed' }); });
      return rendered;
    }
    it('存檔還是後端先存的文字版（版本相同）：可重試，換成這一頁的完整版', async () => {
      const { result } = await closingFailed('text-rev');
      expect(result.current.canRetrySave('s1')).toBe(true);
      serve(text, () => 'text-rev', () => saved('r4'));
      await act(async () => { expect(await result.current.retrySaveSession('s1')).toMatchObject({ status: 'saved' }); });
      expect(posts().at(-1).messages.messages.a2.steps).toEqual([{ tool: 'x' }]);
    });
    it('文字版之後有人寫過（版本不同）：不能重試', async () => {
      const { result } = await closingFailed('later-rev');
      expect(result.current.canRetrySave('s1')).toBe(false);
    });
  });

  describe('匯入：只有確認視窗同意覆蓋的那一次才蓋過同編號的對話', () => {
    const imported = () => session(chain(node('i1', 'user', null)), { title: 'imported', updatedAt: 9 });
    const other = () => session(chain(node('o1', 'user', null)), { title: 'other', updatedAt: 4 });
    it('同意覆蓋：先讀存檔，帶它的版本送出，存好算這一頁存過', async () => {
      serve(() => null, () => 'none');
      const { result } = await render();
      act(() => result.current.setSessions([imported()]));
      serve(other, () => 'r4', () => saved('r5'));
      await act(async () => { expect(await result.current.syncSession(imported(), undefined, { overwrite: true })).toMatchObject({ status: 'saved', revision: 'r5' }); });
      expect(posts().at(-1)).toMatchObject({ title: 'imported', revision: 'r4' });
      serve(imported, () => 'r5', () => saved('r6'));
      await act(async () => { expect(await result.current.updateSessionTitle('s1', 'next')).toMatchObject({ status: 'saved' }); });
      expect(posts().at(-1)).toMatchObject({ title: 'next', revision: 'r5' });
    });
    it('沒同意覆蓋：照舊要求存檔裡沒有，不送出，也不能靠重試蓋過去', async () => {
      serve(() => null, () => 'none');
      const { result } = await render();
      act(() => result.current.setSessions([imported()]));
      serve(other, () => 'r4', () => saved('r5'));
      await act(async () => { expect(await result.current.syncSession(imported())).toMatchObject({ status: 'failed' }); });
      await act(async () => { await result.current.recheckSession('s1'); });
      expect(result.current.canRetrySave('s1')).toBe(false);
      await act(async () => { await result.current.retrySaveSession('s1'); });
      expect(posts()).toHaveLength(0);
    });
    it('同意覆蓋但讀完到送出之間有人寫：被擋下，之後也不能重試', async () => {
      serve(() => null, () => 'none');
      const { result } = await render();
      act(() => result.current.setSessions([imported()]));
      let stored = other();
      let revision = 'r4';
      serve(() => stored, () => revision, failed);
      await act(async () => { expect(await result.current.syncSession(imported(), undefined, { overwrite: true })).toMatchObject({ status: 'failed' }); });
      stored = { ...other(), title: 'heartbeat wrote' };
      revision = 'r6';
      await act(async () => { await result.current.recheckSession('s1'); });
      expect(result.current.canRetrySave('s1')).toBe(false);
    });
    it('同意覆蓋但送出時回應遺失、存檔還是讀到的那份：可以重試，比的是覆蓋前讀到的存檔', async () => {
      serve(() => null, () => 'none');
      const { result } = await render();
      act(() => result.current.setSessions([imported()]));
      serve(other, () => 'r4');
      await act(async () => { expect(await result.current.syncSession(imported(), undefined, { overwrite: true })).toMatchObject({ status: 'unknown' }); });
      serve(other, () => 'r9', () => saved('r10'));
      await act(async () => { await result.current.recheckSession('s1'); });
      expect(result.current.canRetrySave('s1')).toBe(true);
      await act(async () => { expect(await result.current.retrySaveSession('s1')).toMatchObject({ status: 'saved' }); });
      expect(posts().at(-1)).toMatchObject({ title: 'imported', revision: 'r9' });
    });
  });
});
