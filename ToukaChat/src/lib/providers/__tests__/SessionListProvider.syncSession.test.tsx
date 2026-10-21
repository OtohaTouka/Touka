/**
 * syncSession 儲存失敗可見化（批2 ④）。
 *
 * 根因：`fetch()` 對 4xx/5xx 不 reject，舊版 syncSession 不檢 `res.ok`、catch 只記
 * log——後端授權失敗（403）時儲存被靜默吞掉，使用者以為已存檔，實則從未落盤
 * （重整即消失；本系統曾因此整個 ui_sessions.db 空白數月無人察覺）。
 *
 * 修法：檢 `res.ok`，失敗 logSSOTError ＋ toast（errors.json SSOT、固定 toast id
 * 防背景高頻同步轟炸）；網路例外同樣可見。無痕跳過（ADR-0086 收口）行為不變。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { SessionListProvider, useSessionList } from '../SessionListProvider';
import { fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';
import { toast } from 'sonner';
import type { Session } from '@/lib/types';
import { captureRequestScope, isRequestScopeCurrent, subscribeCredentialScope } from '@/lib/connections';
import { ERRORS } from '@/lib/error_codes';
import { buildUserTurnTree } from '@/lib/stream_tree';
import { migrateFlatToTree } from '@/lib/tree_history';

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

function okJson(body: unknown = []): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <SessionListProvider>{children}</SessionListProvider>
);

const sess = (over: Partial<Session> = {}): Session =>
  ({ id: 's1', title: 't', messages: [], updatedAt: 1, ...over }) as Session;

describe('R2 snapshot binding and explicit recovery', () => {
  const local = () => sess({ messages: { messages: {}, currentId: null }, is_home: false,
    home_for_agent: null, agent_id: null, project_id: null, auth_level: 'Manual',
    active_mcp_servers: [], active_skills: [], context_summary: null });
  const present = (session: Session, revision = 'base') => okJson({ exists: true, revision,
    session: { ...local(), ...session, last_activity_at: 1 } });
  const saved = (revision = 'next') => okJson({ status: 'success', session_id: 's1',
    persistence: { status: 'saved', representation: 'full', revision } });
  async function mount() {
    vi.clearAllMocks();
    vi.mocked(isRequestScopeCurrent).mockReturnValue(true);
    fetchToukaMock.mockResolvedValue(okJson([]));
    const rendered = renderHook(() => useSessionList(), { wrapper });
    await act(async () => {});
    act(() => rendered.result.current.setSessions([local()]));
    fetchToukaMock.mockClear();
    return rendered;
  }
  const appendAtBranch = (current: Readonly<Session>): Session => ({ ...current, messages:
    buildUserTurnTree(migrateFlatToTree(current.messages), {
      parentId: 'branch', userMsgId: 'fixed-user', userText: 'queued user', attachment: null,
    }).tree });
  const withBranch = () => ({ ...local(), revision: 'base', messages: { messages: {
    branch: { id: 'branch', role: 'assistant' as const, content: 'base', parentId: null, childrenIds: [] },
  }, currentId: 'branch' } });
  it('an invalid optimistic append is typed failed and leaves no rejected tail or pending slot', async () => {
    const { result } = await mount();
    fetchToukaMock.mockClear();
    let outcome: unknown;
    await act(async () => {
      try { outcome = await result.current.updateSession('s1', appendAtBranch); } catch { outcome = 'thrown'; }
    });
    expect(outcome).toMatchObject({ status: 'failed' });
    expect(result.current.getSession('s1')?.messages).toEqual(local().messages);
    expect(fetchToukaMock).not.toHaveBeenCalled();
    expect(result.current.beginProducer('s1')).toBeNull();
    fetchToukaMock.mockResolvedValue(present(local()));
    await act(async () => { expect(await result.current.recheckSession('s1')).toMatchObject({ status: 'saved' }); });
    fetchToukaMock.mockResolvedValue(saved());
    await act(async () => { expect(await result.current.updateSessionTitle('s1', 'next')).toMatchObject({ status: 'saved' }); });
  });
  it('an invalid incognito transformation cannot poison the next memory-only command', async () => {
    const { result } = await mount();
    act(() => result.current.setSessions([{ ...local(), incognito: true }]));
    await act(async () => { expect(await result.current.updateSession('s1', appendAtBranch)).toMatchObject({ status: 'failed' }); });
    await act(async () => { expect(await result.current.updateSessionTitle('s1', 'next')).toEqual({ status: 'skipped', reason: 'incognito' }); });
    expect(result.current.getSession('s1')?.title).toBe('next');
    expect(fetchToukaMock).not.toHaveBeenCalled();
  });
  it('confirmed replay with a deleted parent resolves failed and releases queued work', async () => {
    const { result } = await mount();
    act(() => result.current.setSessions([withBranch()]));
    const producer = result.current.beginProducer('s1')!;
    let queued!: Promise<unknown>;
    act(() => { queued = result.current.updateSession('s1', appendAtBranch); });
    fetchToukaMock.mockClear().mockResolvedValue(saved('producer-full'));
    let completion: unknown;
    await act(async () => {
      try {
        completion = await result.current.completeProducer(producer,
          current => ({ ...current, messages: { messages: {}, currentId: null } }),
          { status: 'saved', representation: 'text', revision: 'producer-text' });
      } catch { completion = 'thrown'; }
    });
    expect(completion).toMatchObject({ status: 'failed' });
    await act(async () => { expect(await queued).toMatchObject({ status: 'failed' }); });
    expect(fetchToukaMock.mock.calls.filter(([, , options]) => options?.method === 'POST')).toHaveLength(1);
    expect(result.current.getSession('s1')?.messages.messages['fixed-user'].content).toBe('queued user');
    fetchToukaMock.mockResolvedValue(present({ ...local(), ...result.current.getSession('s1')! }));
    await act(async () => { expect(await result.current.recheckSession('s1')).toMatchObject({ status: 'saved' }); });
    expect(result.current.beginProducer('s1')).not.toBeNull();
  });
  it('binds matching snapshot content and revision before the semantic POST', async () => {
    const { result } = await mount();
    fetchToukaMock.mockImplementation(async (_proxy, path, options) => options?.method === 'POST' ? saved() : present(local()));
    await act(async () => { await result.current.updateSessionTitle('s1', 'edited'); });
    expect(fetchToukaMock.mock.calls[0][1]).toBe('/v1/ui/sessions/s1?snapshot=true');
    expect(JSON.parse(fetchToukaMock.mock.calls[1][2]!.body as string)).toMatchObject({ title: 'edited', revision: 'base' });
    expect(result.current.getPersistence('s1')).toMatchObject({ status: 'saved', revision: 'next' });
  });
  it('a conditional whole-tree command rechecks its base after the producer barrier', async () => {
    const { result } = await mount();
    fetchToukaMock.mockImplementation(async (_proxy, _path, options) => options?.method === 'POST' ? saved() : present(local()));
    await act(async () => { await result.current.updateSessionTitle('s1', 'ready'); });
    const base = result.current.getSession('s1')!;
    let token!: NonNullable<ReturnType<typeof result.current.beginProducer>>;
    act(() => { token = result.current.beginProducer('s1')!; });
    fetchToukaMock.mockClear();
    let command!: Promise<unknown>;
    act(() => { command = result.current.updateSession('s1', current => ({ ...current, title: 'stale replacement' }), {}, base); });
    expect(result.current.getSession('s1')?.title).toBe('ready');
    await act(async () => {
      await result.current.completeProducer(token, current => ({ ...current, title: 'newer producer' }),
        { status: 'saved', representation: 'text', revision: 'text' });
      expect(await command).toMatchObject({ status: 'failed' });
    });
    expect(result.current.getSession('s1')?.title).toBe('newer producer');
    expect(fetchToukaMock.mock.calls.filter(([, , options]) => options?.method === 'POST')).toHaveLength(1);
  });
  it('never pairs a fresh revision with a stale list tree', async () => {
    const { result } = await mount();
    fetchToukaMock.mockImplementation(async (_proxy, _path, options) => options?.method === 'POST' ? saved() : present({ ...local(), title: 'newer remote' }));
    await act(async () => { await result.current.updateSessionTitle('s1', 'draft'); });
    expect(fetchToukaMock.mock.calls.filter(([, , options]) => options?.method === 'POST')).toHaveLength(0);
    expect(result.current.getSession('s1')?.title).toBe('draft');
    expect(result.current.getPersistence('s1')).toMatchObject({ status: 'failed' });
  });
  it('new snapshot/import cannot overwrite a same-id existing session', async () => {
    const { result } = await mount();
    fetchToukaMock.mockImplementation(async (_proxy, _path, options) => options?.method === 'POST' ? saved() : present(local()));
    await act(async () => { await result.current.syncSession({ ...local(), title: 'imported' }); });
    expect(fetchToukaMock.mock.calls.some(([, , options]) => options?.method === 'POST')).toBe(false);
    expect(result.current.getSession('s1')?.title).toBe('imported');
  });
  it('DELETE carries only the revision of matching content', async () => {
    const { result } = await mount();
    fetchToukaMock.mockImplementation(async (_proxy, _path, options) => options?.method === 'DELETE'
      ? okJson({ status: 'success', session_id: 's1' }) : present(local()));
    await act(async () => { await result.current.deleteSession('s1'); });
    const request = fetchToukaMock.mock.calls.find(([, , options]) => options?.method === 'DELETE');
    expect(new Headers(request?.[2]?.headers).get('If-Match')).toBe('base');
    expect(result.current.getSession('s1')).toBeUndefined();
  });
  it('unknown only rechecks; different content requires an explicit save-only retry', async () => {
    const { result } = await mount();
    let remote = local();
    fetchToukaMock.mockImplementation(async (_proxy, _path, options) => {
      if (options?.method !== 'POST') return present(remote, 'checked');
      throw new Error('synthetic lost response');
    });
    await act(async () => { await result.current.updateSessionTitle('s1', 'draft'); });
    const postCount = () => fetchToukaMock.mock.calls.filter(([, , options]) => options?.method === 'POST').length;
    expect(postCount()).toBe(1);
    await act(async () => { await result.current.recheckSession('s1'); });
    expect(postCount()).toBe(1);
    expect(result.current.getSession('s1')?.title).toBe('draft');
    expect(result.current.getPersistence('s1')).toMatchObject({ status: 'failed' });
    fetchToukaMock.mockImplementation(async (_proxy, _path, options) => {
      if (options?.method !== 'POST') return present(remote, 'checked');
      remote = JSON.parse(options.body as string);
      return saved();
    });
    await act(async () => { await result.current.retrySaveSession('s1'); });
    expect(postCount()).toBe(2);
    expect(remote.title).toBe('draft');
    expect(result.current.getPersistence('s1')).toMatchObject({ status: 'saved' });
  });
  it('readback of the exact intended content confirms save without resending', async () => {
    const { result } = await mount();
    let remote = local();
    fetchToukaMock.mockImplementation(async (_proxy, _path, options) => {
      if (options?.method !== 'POST') return present(remote);
      remote = JSON.parse(options.body as string);
      throw new Error('commit response lost');
    });
    await act(async () => { await result.current.updateSessionTitle('s1', 'saved remotely'); });
    await act(async () => { await result.current.recheckSession('s1'); });
    expect(result.current.getPersistence('s1')).toMatchObject({ status: 'saved', representation: 'full' });
    expect(fetchToukaMock.mock.calls.filter(([, , options]) => options?.method === 'POST')).toHaveLength(1);
  });
  function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>(done => { resolve = done; });
    return { promise, resolve };
  }
  async function unknownDraft() {
    const rendered = await mount();
    fetchToukaMock.mockImplementation(async (_proxy, _path, options) => {
      if (options?.method !== 'POST') return present(local());
      throw new Error('synthetic unknown');
    });
    await act(async () => { await rendered.result.current.updateSessionTitle('s1', 'draft'); });
    fetchToukaMock.mockClear();
    return rendered;
  }
  it('delayed readback cannot confirm or replace a newer local intent', async () => {
    const { result } = await unknownDraft();
    const response = deferred<Response>();
    fetchToukaMock.mockReturnValue(response.promise);
    let check!: Promise<unknown>;
    act(() => { check = result.current.recheckSession('s1'); });
    await act(async () => { await result.current.updateSessionTitle('s1', 'newer draft'); });
    await act(async () => { response.resolve(present({ ...local(), title: 'draft' })); await check; });
    expect(result.current.sessions[0].title).toBe('newer draft');
    expect(result.current.getSession('s1')?.title).toBe('newer draft');
    expect(result.current.getPersistence('s1')?.status).not.toBe('saved');
    expect(result.current.canRetrySave('s1')).toBe(false);
    expect(fetchToukaMock).toHaveBeenCalledTimes(1);
  });
  it('save-only retry keeps and sends a semantic command queued during the POST exactly once', async () => {
    const { result } = await unknownDraft();
    fetchToukaMock.mockResolvedValue(present(local(), 'checked'));
    await act(async () => { await result.current.recheckSession('s1'); });
    fetchToukaMock.mockClear();
    const response = deferred<Response>();
    fetchToukaMock.mockReturnValueOnce(response.promise).mockResolvedValue(saved('later'));
    let retry!: Promise<unknown>;
    let queued!: Promise<unknown>;
    act(() => { retry = result.current.retrySaveSession('s1'); });
    await act(async () => {});
    act(() => { queued = result.current.updateSession('s1', current => ({ ...current, active_skills: ['new'] })); });
    expect(result.current.sessions[0].active_skills).toEqual(['new']);
    await act(async () => { response.resolve(saved('retried')); await Promise.all([retry, queued]); });
    expect(result.current.sessions[0].active_skills).toEqual(['new']);
    expect(result.current.getSession('s1')?.active_skills).toEqual(['new']);
    expect(fetchToukaMock).toHaveBeenCalledTimes(2);
    expect(JSON.parse(fetchToukaMock.mock.calls[1][2]!.body as string)).toMatchObject({
      title: 'draft', active_skills: ['new'], revision: 'retried' });
  });
  it('readback and retry cannot compete while either request is in progress', async () => {
    const { result } = await unknownDraft();
    fetchToukaMock.mockResolvedValue(present(local(), 'checked'));
    await act(async () => { await result.current.recheckSession('s1'); });
    const checkResponse = deferred<Response>();
    fetchToukaMock.mockClear().mockReturnValue(checkResponse.promise);
    let check!: Promise<unknown>;
    act(() => { check = result.current.recheckSession('s1'); });
    let deniedRetry!: Promise<unknown>;
    act(() => { deniedRetry = result.current.retrySaveSession('s1'); });
    await act(async () => {});
    expect(fetchToukaMock.mock.calls.some(([, , options]) => options?.method === 'POST')).toBe(false);
    await act(async () => { checkResponse.resolve(present(local(), 'checked-again')); await Promise.all([check, deniedRetry]); });
    const postResponse = deferred<Response>();
    fetchToukaMock.mockClear().mockReturnValue(postResponse.promise);
    let retry!: Promise<unknown>;
    act(() => { retry = result.current.retrySaveSession('s1'); });
    let deniedCheck!: Promise<unknown>;
    act(() => { deniedCheck = result.current.recheckSession('s1'); });
    await act(async () => {});
    expect(fetchToukaMock).toHaveBeenCalledTimes(1);
    expect(fetchToukaMock.mock.calls[0][2]?.method).toBe('POST');
    await act(async () => { postResponse.resolve(saved()); await Promise.all([retry, deniedCheck]); });
  });
  it('confirmed absence is failed, preserves the draft and never offers recreation', async () => {
    const { result } = await unknownDraft();
    fetchToukaMock.mockResolvedValue(okJson({ exists: false, revision: 'absent', session: null }));
    await act(async () => { await result.current.recheckSession('s1'); });
    expect(result.current.getPersistence('s1')).toEqual({ status: 'failed', error_code: ERRORS.SESSION_NOT_FOUND.id });
    expect(result.current.getSession('s1')?.title).toBe('draft');
    expect(result.current.canRetrySave('s1')).toBe(false);
    await act(async () => { await result.current.retrySaveSession('s1'); });
    expect(fetchToukaMock).toHaveBeenCalledTimes(1);
  });
  it('initial no-revision delete still awaits explicit DELETE success after confirmed absence', async () => {
    const { result } = await mount();
    const response = deferred<Response>();
    fetchToukaMock.mockImplementation(async (_proxy, _path, options) => options?.method === 'DELETE'
      ? response.promise : okJson({ exists: false, revision: 'absent', session: null }));
    let deletion!: Promise<boolean>;
    act(() => { deletion = result.current.deleteSession('s1'); });
    await act(async () => {});
    expect(result.current.getSession('s1')).toBeDefined();
    expect(fetchToukaMock.mock.calls.some(([, , options]) => options?.method === 'DELETE')).toBe(true);
    await act(async () => { response.resolve(okJson({ status: 'success', session_id: 's1' })); expect(await deletion).toBe(true); });
    expect(result.current.getSession('s1')).toBeUndefined();
    expect(fetchToukaMock.mock.calls.some(([, , options]) => options?.method === 'POST')).toBe(false);
  });
  it('public state ingress cannot replace an unknown rich draft but still changes memory-only flags', async () => {
    const { result } = await unknownDraft();
    act(() => result.current.setSessions(previous => previous.map(item => ({ ...local(), deepResearch: !item.deepResearch }))));
    expect(result.current.getSession('s1')).toMatchObject({ title: 'draft', deepResearch: true });
    expect(result.current.sessions[0].title).toBe('draft');
    act(() => result.current.setSessions([]));
    expect(result.current.sessions[0].title).toBe('draft');
    expect(fetchToukaMock).not.toHaveBeenCalled();
  });
  it('external changed content cannot borrow the last saved revision', async () => {
    const { result } = await mount();
    fetchToukaMock.mockImplementation(async (_proxy, _path, options) => options?.method === 'POST' ? saved() : present(local()));
    await act(async () => { await result.current.updateSessionTitle('s1', 'saved title'); });
    act(() => result.current.setSessions(previous => previous.map(item => ({ ...item, title: 'external draft' }))));
    fetchToukaMock.mockClear().mockImplementation(async (_proxy, _path, options) => options?.method === 'POST'
      ? saved() : present({ ...local(), title: 'saved title' }, 'newer'));
    await act(async () => { await result.current.updateSession('s1', current => ({ ...current, active_skills: ['new'] })); });
    expect(fetchToukaMock.mock.calls.some(([, , options]) => options?.method === 'POST')).toBe(false);
    expect(result.current.getSession('s1')?.title).toBe('external draft');
    expect(result.current.getPersistence('s1')?.status).toBe('failed');
  });
  it('resume adopts the canonical snapshot content and revision together without a write', async () => {
    const { result } = await mount();
    const canonical = { ...local(), title: 'canonical' };
    fetchToukaMock.mockResolvedValue(present(canonical, 'canonical-revision'));
    let prepared: Session | undefined;
    await act(async () => { prepared = await result.current.prepareResume('s1'); });
    expect(prepared).toMatchObject({ title: 'canonical', revision: 'canonical-revision' });
    expect(result.current.getSession('s1')).toMatchObject({ title: 'canonical', revision: 'canonical-revision' });
    expect(fetchToukaMock.mock.calls.some(([, , options]) => options?.method === 'POST')).toBe(false);
  });
  it('resume cannot replace an unknown local draft with a server snapshot', async () => {
    const { result } = await unknownDraft();
    fetchToukaMock.mockResolvedValue(present(local()));
    await act(async () => { expect(await result.current.prepareResume('s1')).toBeUndefined(); });
    expect(result.current.getSession('s1')?.title).toBe('draft');
    expect(fetchToukaMock).not.toHaveBeenCalled();
  });
  it.each([false, true])('resume protects unconfirmed external content with existing writer=%s', async existing => {
    const { result } = await mount();
    if (existing) {
      fetchToukaMock.mockImplementation(async (_proxy, _path, options) => options?.method === 'POST' ? saved() : present(local()));
      await act(async () => { await result.current.updateSessionTitle('s1', 'saved title'); });
    }
    act(() => result.current.setSessions(previous => previous.map(item => ({ ...item, title: 'external draft' }))));
    fetchToukaMock.mockClear().mockResolvedValue(present(local()));
    await act(async () => { expect(await result.current.prepareResume('s1')).toBeUndefined(); });
    expect(result.current.getSession('s1')?.title).toBe('external draft');
    expect(fetchToukaMock).not.toHaveBeenCalled();
  });
  it.each([false, true])('a second ingress and refresh retain an unconfirmed draft; existing writer=%s', async existing => {
    const { result } = await mount();
    if (existing) {
      fetchToukaMock.mockImplementation(async (_proxy, _path, options) => options?.method === 'POST' ? saved() : present(local()));
      await act(async () => { await result.current.updateSessionTitle('s1', 'saved title'); });
    }
    act(() => result.current.setSessions(previous => previous.map(item => ({ ...item, title: 'external draft' }))));
    act(() => result.current.setSessions([local()]));
    expect(result.current.getSession('s1')?.title).toBe('external draft');
    act(() => result.current.setSessions([]));
    expect(result.current.sessions[0].title).toBe('external draft');
    fetchToukaMock.mockResolvedValue(okJson([]));
    await act(async () => {
      expect((await result.current.refreshSessions())[0].title).toBe('external draft');
    });
    expect(result.current.getSession('s1')?.revision).toBeUndefined();
  });
});

describe('syncSession 失敗可見化（批2 ④）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 掛載時 refreshSessions 的 GET：預設回空清單
    fetchToukaMock.mockResolvedValue(okJson([]));
  });

  it('後端 4xx（授權失敗）→ logSSOTError + toast.error（固定 id 防轟炸）', async () => {
    const { result } = renderHook(() => useSessionList(), { wrapper });
    await act(async () => {});
    fetchToukaMock.mockResolvedValueOnce(okJson({ exists: false, revision: 'absent', session: null }))
      .mockResolvedValueOnce({ ok: false, status: 403, json: async () => ({}) } as unknown as Response);

    await act(async () => { await result.current.syncSession(sess()); });

    expect(logSSOTError).toHaveBeenCalledWith(expect.anything(), expect.stringContaining('syncSession:s1'));
    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith(expect.any(String), { id: 'session-sync-failed' });
  });

  it('後端 200 合法全樹保存結果 → 不 toast', async () => {
    const { result } = renderHook(() => useSessionList(), { wrapper });
    await act(async () => {});
    fetchToukaMock.mockResolvedValueOnce(okJson({ exists: false, revision: 'absent', session: null }))
      .mockResolvedValueOnce(okJson({ status: 'success', session_id: 's1',
      persistence: { status: 'saved', representation: 'full', revision: 'r1' } }));

    await act(async () => { await result.current.syncSession(sess()); });

    expect(toast.error).not.toHaveBeenCalled();
  });

  it('網路例外 → logSSOTError + toast.error', async () => {
    const { result } = renderHook(() => useSessionList(), { wrapper });
    await act(async () => {});
    fetchToukaMock.mockResolvedValueOnce(okJson({ exists: false, revision: 'absent', session: null }))
      .mockRejectedValueOnce(new Error('ECONNREFUSED'));

    await act(async () => { await result.current.syncSession(sess()); });

    expect(logSSOTError).toHaveBeenCalledWith(expect.anything(), expect.stringContaining('syncSession:s1'));
    expect(toast.error).toHaveBeenCalledWith(expect.any(String), { id: 'session-sync-failed' });
  });

  it('無痕 session → 不發任何 POST（ADR-0086 收口不變）', async () => {
    const { result } = renderHook(() => useSessionList(), { wrapper });
    await waitFor(() => expect(fetchToukaMock).toHaveBeenCalled()); // 等掛載 GET 完成
    fetchToukaMock.mockClear();

    await act(async () => { await result.current.syncSession(sess({ incognito: true })); });

    expect(fetchToukaMock).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });
});

describe('R2 controlled session writes', () => {
  const saved = (revision: string) => okJson({ status: 'success', session_id: 's1',
    persistence: { status: 'saved', representation: 'full', revision } });
  function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>(done => { resolve = done; });
    return { promise, resolve };
  }
  async function mount(initial = sess({ revision: 'base' })) {
    fetchToukaMock.mockResolvedValue(okJson([]));
    const rendered = renderHook(() => useSessionList(), { wrapper });
    await act(async () => {});
    act(() => rendered.result.current.setSessions([initial]));
    fetchToukaMock.mockClear();
    return rendered;
  }
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(captureRequestScope).mockReturnValue({ proxyOrigin: 'http://x', authenticatedPrincipalId: 'access:a', authEpoch: 1 });
    vi.mocked(isRequestScopeCurrent).mockReturnValue(true);
  });

  it('returns a validated result; a bare 200 is unknown and preserves the draft', async () => {
    const { result } = await mount();
    fetchToukaMock.mockResolvedValue(okJson({ status: 'success' }));
    let outcome: unknown;
    await act(async () => { outcome = await result.current.syncSession(sess({ title: 'draft' })); });
    expect(result.current.sessions[0].title).toBe('draft');
    expect(outcome).toMatchObject({ status: 'unknown' });
  });
  it('does not expose a backend secret in an invalid success result', async () => {
    const { result } = await mount();
    fetchToukaMock.mockResolvedValue(okJson({ status: 'success', session_id: 's1',
      persistence: { status: 'unknown', error_code: 1302, detail: 'private-sentinel' } }));
    let outcome: unknown;
    await act(async () => { outcome = await result.current.syncSession(sess()); });
    expect(outcome).toMatchObject({ status: 'unknown' });
    expect(JSON.stringify(outcome)).not.toContain('private-sentinel');
    const observedMessages = JSON.stringify(vi.mocked(toast.error).mock.calls);
    expect(observedMessages).not.toContain('private-sentinel');
  });
  it('both refresh state and returned list keep a pending and then unknown rich draft', async () => {
    const { result } = await mount();
    const pending = deferred<Response>();
    fetchToukaMock.mockImplementation(async (_url, _path, options) => options?.method === 'POST'
      ? pending.promise : okJson([sess({ title: 'remote', messages: [] })]));
    const rich = { messages: { a: { id: 'a', steps: [{ detail: 'rich' }] } }, currentId: 'a' };
    let write!: Promise<unknown>;
    act(() => { write = result.current.syncSession(sess({ title: 'draft', messages: rich })); });
    let refreshed: Session[] = [];
    await act(async () => { refreshed = await result.current.refreshSessions(); });
    expect(refreshed[0].messages).toEqual(rich);
    expect(result.current.sessions[0].messages).toEqual(rich);
    await act(async () => { pending.resolve(okJson({})); await write; });
    await act(async () => { refreshed = await result.current.refreshSessions(); });
    expect(refreshed[0].messages).toEqual(rich);
  });
  it('serializes semantic changes from the last confirmed snapshot, preserving later local changes', async () => {
    const { result } = await mount();
    const first = deferred<Response>();
    fetchToukaMock.mockReturnValueOnce(first.promise).mockResolvedValue(saved('r2'));
    let one!: Promise<unknown>;
    let two!: Promise<unknown>;
    act(() => {
      one = result.current.updateSession('s1', current => ({ ...current, title: 'renamed' }));
      two = result.current.updateSession('s1', current => ({ ...current, active_skills: ['s'] }));
    });
    await act(async () => {});
    expect(fetchToukaMock).toHaveBeenCalledTimes(1);
    expect(result.current.sessions[0]).toMatchObject({ title: 'renamed', active_skills: ['s'] });
    await act(async () => { first.resolve(saved('r1')); await Promise.all([one, two]); });
    expect(fetchToukaMock).toHaveBeenCalledTimes(2);
    const body = JSON.parse(fetchToukaMock.mock.calls[1][2]!.body as string);
    expect(body).toMatchObject({ title: 'renamed', active_skills: ['s'] });
  });
  it('the same logical operation is sent once but distinct operations with equal content are not collapsed', async () => {
    const { result } = await mount();
    fetchToukaMock.mockResolvedValue(saved('r1'));
    const key = {};
    const rename = (current: Readonly<Session>) => ({ ...current, title: 'same' });
    await act(async () => {
      await Promise.all([result.current.updateSession('s1', rename, key), result.current.updateSession('s1', rename, key)]);
      await result.current.updateSession('s1', rename, {});
    });
    expect(fetchToukaMock).toHaveBeenCalledTimes(2);
  });
  it('unknown halts subsequent automatic writes and keeps both local changes', async () => {
    const { result } = await mount();
    fetchToukaMock.mockResolvedValue(okJson({}));
    await act(async () => {
      await result.current.updateSession('s1', current => ({ ...current, title: 'draft' }));
      await result.current.updateSession('s1', current => ({ ...current, agent_id: 'agent' }));
    });
    expect(fetchToukaMock).toHaveBeenCalledTimes(1);
    expect(result.current.sessions[0]).toMatchObject({ title: 'draft', agent_id: 'agent' });
  });
  it('all state ingress shares the immediate ref and never invokes HTTP from the updater', async () => {
    const { result } = await mount();
    const pure = vi.fn((previous: Session[]) => previous.map(current => ({ ...current, title: 'local' })));
    act(() => { result.current.setSessions(pure); });
    expect(fetchToukaMock).not.toHaveBeenCalled();
    expect(pure).toHaveBeenCalledTimes(1);
    fetchToukaMock.mockResolvedValue(okJson({}));
    await act(async () => { await result.current.updateSession('s1', current => ({ ...current, agent_id: 'a' })); });
    expect(result.current.getSession('s1')?.title).toBe('local');
    expect(fetchToukaMock.mock.calls[0][1]).toContain('snapshot=true');
    expect(fetchToukaMock.mock.calls.some(([, , options]) => options?.method === 'POST')).toBe(false);
  });
  it('incognito returns skipped and never enters the wire queue', async () => {
    const { result } = await mount(sess({ incognito: true }));
    let outcome: unknown;
    await act(async () => { outcome = await result.current.syncSession(sess({ incognito: true })); });
    expect(fetchToukaMock).not.toHaveBeenCalled();
    expect(outcome).toEqual({ status: 'skipped', reason: 'incognito' });
  });
  it.each([
    { status: 'saved', representation: 'text', revision: 'r1' },
    { status: 'skipped', reason: 'no_text' },
    { status: 'skipped', reason: 'incognito' },
  ])('a normal full save does not clear dirty on $status/$representation/$reason', async persistence => {
    const { result } = await mount();
    fetchToukaMock.mockResolvedValue(okJson({ status: 'success', session_id: 's1', persistence }));
    await act(async () => { await result.current.syncSession(sess({ title: 'draft' })); });
    expect(result.current.getPersistence('s1')).toMatchObject({ status: 'unknown' });
    fetchToukaMock.mockResolvedValue(okJson([sess({ title: 'remote' })]));
    await act(async () => { await result.current.refreshSessions(); });
    expect(result.current.sessions[0].title).toBe('draft');
  });
  it('external state ingress after a saved write requires renewed base verification', async () => {
    const { result } = await mount();
    fetchToukaMock.mockResolvedValue(saved('r1'));
    await act(async () => { await result.current.syncSession(sess()); });
    act(() => result.current.setSessions(previous => previous.map(item => ({ ...item, title: 'new-ingress' }))));
    fetchToukaMock.mockResolvedValue(okJson({}));
    await act(async () => { await result.current.updateSession('s1', current => ({ ...current, agent_id: 'agent' })); });
    expect(fetchToukaMock.mock.calls[1][1]).toContain('snapshot=true');
    expect(result.current.getSession('s1')).toMatchObject({ title: 'new-ingress', agent_id: 'agent' });
    expect(fetchToukaMock.mock.calls.filter(([, , options]) => options?.method === 'POST')).toHaveLength(1);
  });
  it('a producer barrier stores the rich tree before replaying queued metadata and rejects duplicate completion', async () => {
    const { result } = await mount();
    const rich = { messages: { a: { id: 'a', steps: [{ detail: 'rich' }] } }, currentId: 'a' };
    let token!: ReturnType<typeof result.current.beginProducer>;
    act(() => { token = result.current.beginProducer('s1'); });
    expect(token).not.toBeNull();
    let queued!: Promise<unknown>;
    act(() => { queued = result.current.updateSession('s1', current => ({ ...current, title: 'later' })); });
    await act(async () => {});
    expect(fetchToukaMock).not.toHaveBeenCalled();
    fetchToukaMock.mockResolvedValueOnce(saved('full')).mockResolvedValueOnce(saved('metadata'));
    await act(async () => {
      await result.current.completeProducer(token!, current => ({ ...current, messages: rich }),
        { status: 'saved', representation: 'text', revision: 'text' });
      await queued;
      await result.current.completeProducer(token!, current => ({ ...current, messages: [] }),
        { status: 'saved', representation: 'text', revision: 'text' });
    });
    expect(fetchToukaMock).toHaveBeenCalledTimes(2);
    const bodies = fetchToukaMock.mock.calls.map(call => JSON.parse(call[2]!.body as string));
    expect(bodies[0]).toMatchObject({ title: 't', messages: rich, revision: 'text' });
    expect(bodies[1]).toMatchObject({ title: 'later', messages: rich, revision: 'full' });
    expect(result.current.sessions[0]).toMatchObject({ title: 'later', messages: rich });
  });
  it('scope invalidation rejects old command handles before they can insert A into B', async () => {
    const { result } = await mount();
    const old = result.current;
    const nextScope = { proxyOrigin: 'http://x', authenticatedPrincipalId: 'access:b', authEpoch: 2 };
    vi.mocked(captureRequestScope).mockReturnValueOnce(nextScope).mockReturnValueOnce(nextScope);
    const notify = vi.mocked(subscribeCredentialScope).mock.calls.at(-1)![0];
    fetchToukaMock.mockResolvedValue(okJson([]));
    act(() => { notify(); });
    vi.mocked(captureRequestScope).mockReturnValue(nextScope);
    await act(async () => {});
    fetchToukaMock.mockClear();
    act(() => { old.setSessions([sess({ title: 'private A' })]); });
    await act(async () => { await old.syncSession(sess({ title: 'private A' })); });
    expect(result.current.sessions).toEqual([]);
    expect(fetchToukaMock).not.toHaveBeenCalled();
    vi.mocked(captureRequestScope).mockReturnValue({ proxyOrigin: 'http://x', authenticatedPrincipalId: 'access:a', authEpoch: 1 });
    vi.mocked(isRequestScopeCurrent).mockReturnValue(true);
  });
  it('the same operation token is scoped to each session', async () => {
    const { result } = await mount();
    act(() => result.current.setSessions([sess({ revision: 'base1' }), sess({ id: 's2', revision: 'base2' })]));
    fetchToukaMock.mockImplementation(async (_url, path) => okJson({ status: 'success',
      session_id: path.endsWith('s2') ? 's2' : 's1', persistence: { status: 'saved', representation: 'full', revision: 'r1' } }));
    const key = {};
    await act(async () => { await Promise.all([
      result.current.updateSession('s1', current => ({ ...current, title: 'one' }), key),
      result.current.updateSession('s2', current => ({ ...current, title: 'two' }), key),
    ]); });
    expect(fetchToukaMock).toHaveBeenCalledTimes(2);
    expect(result.current.getSession('s2')?.title).toBe('two');
  });
  it('body parsing cannot return saved after the credential scope is invalidated', async () => {
    const { result } = await mount();
    const body = deferred<unknown>();
    fetchToukaMock.mockResolvedValue({ ok: true, status: 200, json: () => body.promise } as Response);
    let pending!: Promise<unknown>;
    act(() => { pending = result.current.syncSession(sess()); });
    await act(async () => {});
    vi.mocked(isRequestScopeCurrent).mockReturnValue(false);
    let outcome: unknown;
    await act(async () => {
      body.resolve({ status: 'success', session_id: 's1', persistence: { status: 'saved', representation: 'full', revision: 'late' } });
      outcome = await pending;
    });
    expect(outcome).toMatchObject({ status: 'unknown' });
  });
  it('scope invalidation aborts the old in-flight write signal', async () => {
    const { result } = await mount();
    const response = deferred<Response>();
    fetchToukaMock.mockReturnValue(response.promise);
    let pending!: Promise<unknown>;
    act(() => { pending = result.current.syncSession(sess()); });
    await act(async () => {});
    const signal = fetchToukaMock.mock.calls[0][2]?.signal;
    const nextScope = { proxyOrigin: 'http://x', authenticatedPrincipalId: 'access:b', authEpoch: 2 };
    vi.mocked(captureRequestScope).mockReturnValue(nextScope);
    fetchToukaMock.mockResolvedValue(okJson([]));
    act(() => vi.mocked(subscribeCredentialScope).mock.calls.at(-1)![0]());
    await act(async () => { response.resolve(saved('late')); await pending; });
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(signal!.aborted).toBe(true);
  });
  it('completed no_text still saves a rich tree against the confirmed user revision', async () => {
    const { result } = await mount();
    act(() => result.current.setSessions([sess({ revision: 'user' })]));
    const token = result.current.beginProducer('s1')!;
    const rich = { messages: { a: { steps: ['tool-only'] } }, currentId: 'a' };
    fetchToukaMock.mockResolvedValue(saved('full'));
    await act(async () => { await result.current.completeProducer(token, current => ({ ...current, messages: rich }),
      { status: 'skipped', reason: 'no_text' }); });
    expect(fetchToukaMock).toHaveBeenCalledTimes(1);
    expect(JSON.parse(fetchToukaMock.mock.calls[0][2]!.body as string)).toMatchObject({ revision: 'user', messages: rich });
    expect(result.current.getPersistence('s1')).toMatchObject({ status: 'saved', representation: 'full' });
  });
  it.each(['error', 'cancelled'] as const)('%s no_text does not automatically save partial rich output', async generation => {
    const { result } = await mount();
    const token = result.current.beginProducer('s1')!;
    fetchToukaMock.mockResolvedValue(saved('full'));
    await act(async () => { await result.current.completeProducer(token, current => ({ ...current,
      messages: { messages: { a: { steps: ['partial'] } }, currentId: 'a' } }),
    { status: 'skipped', reason: 'no_text' }, generation); });
    expect(fetchToukaMock).not.toHaveBeenCalled();
    expect(result.current.getPersistence('s1')).toMatchObject({ status: 'unknown' });
    expect(result.current.sessions[0].messages.messages.a.steps).toEqual(['partial']);
  });
});
