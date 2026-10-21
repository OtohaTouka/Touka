import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchTouka, getActiveProxyUrl } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';
import { useSnsBotSettings, type SnsBotsConfig } from '../useSnsBotSettings';

vi.mock('@/lib/connections', () => ({
  fetchTouka: vi.fn(), getActiveProxyUrl: vi.fn(),
}));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), errorMsg: () => '儲存失敗' }));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const mockFetch = vi.mocked(fetchTouka);
const secret = 'test-only-sns-credential';
const saved: SnsBotsConfig = {
  discord: [{ id: 'one', token: '••••••••', agent_id: 'Alice', enabled: true }], telegram: [],
};
const draft = (): SnsBotsConfig => ({
  discord: [{ ...saved.discord[0], token: secret, enabled: false }], telegram: [],
});
const reply = (ok = true, data: unknown = saved) => ({ ok, json: async () => data }) as Response;

describe('SNS credentials remain in the current form only', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.resetAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
    mockFetch.mockResolvedValue(reply());
    vi.mocked(getActiveProxyUrl).mockReturnValue('http://localhost:8000');
  });

  it('never hydrates a raw credential from retired browser storage', () => {
    window.localStorage.setItem('touka_sns_bots', JSON.stringify(draft()));
    const { result } = renderHook(useSnsBotSettings);
    expect(JSON.stringify(result.current.snsBots)).not.toContain(secret);
  });

  it('loads masked server configuration without writing browser storage', async () => {
    const localWrite = vi.spyOn(window.localStorage, 'setItem');
    const sessionWrite = vi.spyOn(window.sessionStorage, 'setItem');
    const { result } = renderHook(useSnsBotSettings);
    await act(async () => { await result.current.fetchSnsBots(); });
    expect(result.current.snsBots).toEqual(saved);
    expect(localWrite).not.toHaveBeenCalled();
    expect(sessionWrite).not.toHaveBeenCalled();
  });

  it('editing credentials never writes either browser storage', async () => {
    const { result } = renderHook(useSnsBotSettings);
    await act(async () => { await result.current.fetchSnsBots(); });
    const localWrite = vi.spyOn(window.localStorage, 'setItem');
    const sessionWrite = vi.spyOn(window.sessionStorage, 'setItem');
    act(() => result.current.setSnsBots(draft()));
    expect(result.current.snsBots.discord[0].token).toBe(secret);
    expect(localWrite).not.toHaveBeenCalled();
    expect(sessionWrite).not.toHaveBeenCalled();
  });

  it('submits a changed token once then reloads canonical masked configuration', async () => {
    const { result } = renderHook(useSnsBotSettings);
    await act(async () => { await result.current.fetchSnsBots(); });
    act(() => result.current.setSnsBots(draft()));
    mockFetch.mockClear();
    await act(async () => { expect(await result.current.saveSnsBots(draft())).toBe(true); });
    expect(mockFetch.mock.calls[0][2]?.body).toBe(JSON.stringify(draft()));
    expect(mockFetch.mock.calls[1]?.[1]).toBe('/v1/bots');
    expect(result.current.snsBots).toEqual(saved);
    expect(window.localStorage.getItem('touka_sns_bots')).toBeNull();
  });

  it.each(['non-2xx', 'throw'])('clears raw drafts after POST %s without logging secrets', async (failure) => {
    const { result } = renderHook(useSnsBotSettings);
    await act(async () => { await result.current.fetchSnsBots(); });
    act(() => result.current.setSnsBots(draft()));
    if (failure === 'throw') mockFetch.mockRejectedValueOnce(new Error(secret));
    else mockFetch.mockResolvedValueOnce(reply(false));
    await act(async () => { expect(await result.current.saveSnsBots(draft())).toBe(false); });
    expect(result.current.snsBots.discord[0].token).toBe(saved.discord[0].token);
    expect(result.current.snsBots.discord[0].enabled).toBe(false);
    expect(vi.mocked(logSSOTError).mock.calls).toEqual([]);
  });

  it('cannot overwrite the backend before any successful GET', async () => {
    const { result } = renderHook(useSnsBotSettings);
    await act(async () => { expect(await result.current.saveSnsBots({ discord: [], telegram: [] })).toBe(false); });
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('blocks writes when the canonical GET after successful POST fails', async () => {
    const { result } = renderHook(useSnsBotSettings);
    await act(async () => { await result.current.fetchSnsBots(); });
    act(() => result.current.setSnsBots(draft()));
    mockFetch.mockResolvedValueOnce(reply()).mockResolvedValueOnce(reply(false));
    await act(async () => { await result.current.saveSnsBots(draft()); });
    expect(JSON.stringify(result.current.snsBots)).not.toContain(secret);
    expect(result.current.loadState).toBe('error');
    mockFetch.mockClear();
    await act(async () => { expect(await result.current.saveSnsBots(saved)).toBe(false); });
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('does not accept malformed or partial configuration as a known empty list', async () => {
    mockFetch.mockResolvedValue(reply(true, { discord: [] }));
    const { result } = renderHook(useSnsBotSettings);
    await act(async () => { expect(await result.current.fetchSnsBots()).toBe(false); });
    expect(result.current.loadState).toBe('error');
    mockFetch.mockClear();
    await act(async () => { expect(await result.current.saveSnsBots(saved)).toBe(false); });
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('preserves stable ids and unchanged masked credentials in the outgoing payload', async () => {
    const { result } = renderHook(useSnsBotSettings);
    await act(async () => { await result.current.fetchSnsBots(); });
    const updated = { discord: [{ ...saved.discord[0], session_mode: 'home', is_multi_agent: true }], telegram: [] };
    act(() => result.current.setSnsBots(updated));
    mockFetch.mockClear();
    await act(async () => { await result.current.saveSnsBots(updated); });
    expect(JSON.parse(mockFetch.mock.calls[0][2]?.body as string)).toEqual(updated);
  });

  it('clears an unsaved new bot token after rejection while retaining nonsecret fields', async () => {
    const { result } = renderHook(useSnsBotSettings);
    await act(async () => { await result.current.fetchSnsBots(); });
    const added = { discord: [], telegram: [{ id: 'new', token: secret, agent_id: 'Alice', enabled: true }] };
    act(() => result.current.setSnsBots(added));
    mockFetch.mockResolvedValueOnce(reply(false));
    await act(async () => { await result.current.saveSnsBots(added); });
    expect(result.current.snsBots.telegram).toEqual([{ ...added.telegram[0], token: '' }]);
    expect(result.current.tokenDraftCleared).toBe(true);
  });

  it('rejects edits and duplicate submissions while a POST is in flight', async () => {
    const { result } = renderHook(useSnsBotSettings);
    await act(async () => { await result.current.fetchSnsBots(); });
    act(() => result.current.setSnsBots(draft()));
    let finish!: (value: Response) => void;
    mockFetch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    let pending!: Promise<boolean>;
    act(() => { pending = result.current.saveSnsBots(draft()); });
    act(() => result.current.setSnsBots(saved));
    expect(result.current.snsBots.discord[0].token).toBe(secret);
    await act(async () => { expect(await result.current.saveSnsBots(saved)).toBe(false); });
    await act(async () => { finish(reply(false)); await pending; });
    expect(result.current.snsBots.discord[0].token).not.toBe(secret);
  });

  it('leaving a form clears drafts and invalidates a late POST response', async () => {
    const { result } = renderHook(useSnsBotSettings);
    await act(async () => { await result.current.fetchSnsBots(); });
    act(() => result.current.setSnsBots(draft()));
    let finish!: (value: Response) => void;
    mockFetch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    let pending!: Promise<boolean>;
    act(() => { pending = result.current.saveSnsBots(draft()); });
    act(() => result.current.resetSnsDrafts());
    expect(result.current.snsBots).toEqual(saved);
    mockFetch.mockClear();
    await act(async () => { finish(reply()); expect(await pending).toBe(false); });
    expect(result.current.snsBots).toEqual(saved);
    expect(result.current.loadState).toBe('idle');
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('a late GET after leaving the form cannot restore its configuration', async () => {
    let finish!: (value: Response) => void;
    mockFetch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const { result } = renderHook(useSnsBotSettings);
    let pending!: Promise<boolean>;
    act(() => { pending = result.current.fetchSnsBots(); });
    act(() => result.current.setSnsBots(draft()));
    expect(result.current.snsBots.discord).toEqual([]);
    act(() => result.current.resetSnsDrafts());
    await act(async () => { finish(reply()); expect(await pending).toBe(false); });
    expect(result.current.snsBots.discord).toEqual([]);
    expect(result.current.loadState).toBe('idle');
  });

  it('an unmounted hook ignores late responses and a new mount starts empty', async () => {
    let finish!: (value: Response) => void;
    mockFetch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const old = renderHook(useSnsBotSettings);
    let pending!: Promise<boolean>;
    act(() => { pending = old.result.current.fetchSnsBots(); });
    old.unmount();
    await act(async () => { finish(reply()); expect(await pending).toBe(false); });
    const fresh = renderHook(useSnsBotSettings);
    expect(fresh.result.current.snsBots).toEqual({ discord: [], telegram: [] });
  });

  it('never submits configuration loaded from a different proxy', async () => {
    const { result } = renderHook(useSnsBotSettings);
    await act(async () => { await result.current.fetchSnsBots(); });
    act(() => result.current.setSnsBots(draft()));
    vi.mocked(getActiveProxyUrl).mockReturnValue('http://localhost:9000');
    mockFetch.mockClear();
    await act(async () => { expect(await result.current.saveSnsBots(draft())).toBe(false); });
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('a replaced GET cannot overwrite a newer canonical response', async () => {
    let finish!: (value: Response) => void;
    mockFetch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const { result } = renderHook(useSnsBotSettings);
    let pending!: Promise<boolean>;
    act(() => { pending = result.current.fetchSnsBots(); });
    await act(async () => { await result.current.fetchSnsBots(); });
    await act(async () => { finish(reply(true, { discord: [], telegram: [] })); expect(await pending).toBe(false); });
    expect(result.current.snsBots).toEqual(saved);
  });

  it('ignores a POST completed after the hook unmounts', async () => {
    const { result, unmount } = renderHook(useSnsBotSettings);
    await act(async () => { await result.current.fetchSnsBots(); });
    act(() => result.current.setSnsBots(draft()));
    let finish!: (value: Response) => void;
    mockFetch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    let pending!: Promise<boolean>;
    act(() => { pending = result.current.saveSnsBots(draft()); });
    unmount();
    mockFetch.mockClear();
    await act(async () => { finish(reply()); expect(await pending).toBe(false); });
    expect(mockFetch).not.toHaveBeenCalled();
  });
});
